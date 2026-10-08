"""Explicit isolated R1.1 snapshots in existing shadow tables, without auto activation."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from investor_core.execution import StrictModel
from investor_core.ledger import LedgerError
from investor_core.r11_candidates import CandidateInput, calculate_candidates
from investor_core.r11_inputs import COMPUTATION_VERSION, DEFINITION, MODEL_VERSION, VERSION
from investor_core.r11_macro import MacroInput, calculate_macro, stable_state
from investor_core.r11_provenance import publication_matches, reconcile
from investor_core.r11_rules import rules
from investor_core.research import ResearchService
from investor_core.scheduler import digest, stamp
from investor_core.shadow_models import ModelDefinition, ShadowService


class R11Request(StrictModel):
    method: Literal["C", "MEDICAL", "A500"]
    bundle_evidence_id: str
    idempotency_key: str = Field(min_length=1, max_length=200)
    supersedes: str | None = None


class R11SourceBinding(StrictModel):
    """Additional source subject data remain archived, never accepted as PASS."""

    published_at: datetime
    first_retrieved_at: datetime

    @model_validator(mode="after")
    def aware(self) -> R11SourceBinding:
        if self.published_at.tzinfo is None or self.first_retrieved_at.tzinfo is None:
            raise ValueError("source timezone required")
        return self


class R11Service:
    def __init__(self, research: ResearchService) -> None:
        self.research = research
        self.shadow = ShadowService(research)

    @staticmethod
    def definition(method: str, dataset_kind: str = "REAL") -> ModelDefinition:
        return ModelDefinition(
            model_key=("r11-simulation-" if dataset_kind == "SYNTHETIC" else "r11-")
            + method.lower(),
            version=MODEL_VERSION,
            model_type="MACRO_REGIME" if method == "C" else "SATELLITE_RANKING",
            scope={
                "C": "REGION:CN_A_BROAD",
                "MEDICAL": "SECTOR:CN_MEDICAL_C",
                "A500": "STYLE:CN_A500_FEEDER_A",
            }[method],
            rationale=json.dumps(
                dict(
                    DEFINITION, dataset_kind=dataset_kind, computation_version=COMPUTATION_VERSION
                ),
                sort_keys=True,
            ),
        )

    @staticmethod
    def _evidence(c: Any, evidence_id: str) -> dict[str, Any]:
        row = c.execute(
            "SELECT * FROM market_research_evidence WHERE id=?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise LedgerError("R11_EVIDENCE_MISSING", "Archive input evidence first")
        return dict(row)

    def _load(
        self, c: Any, request: R11Request
    ) -> tuple[MacroInput | CandidateInput, dict[str, Any]]:
        bundle = self._evidence(c, request.bundle_evidence_id)
        facts = json.loads(bundle["facts_json"])
        body = facts.get("facts", {}).get("r11_input")
        if not isinstance(body, dict):
            raise LedgerError("R11_INPUT_BUNDLE_MISSING", "Archived typed R1.1 input required")
        data = (
            MacroInput.model_validate(body)
            if request.method == "C"
            else CandidateInput.model_validate(body)
        )
        if isinstance(data, CandidateInput) and data.cohort != request.method:
            raise LedgerError("R11_METHOD_MISMATCH", "Bundle belongs to a different cohort")
        if data.context.as_of > self.research._now():
            raise LedgerError("R11_FUTURE_CUTOFF", "Cannot observe a future cutoff")
        snapshots = {}
        for key, source in data.context.sources.items():
            archive = self._evidence(c, source.archive_id)
            archived = json.loads(archive["facts_json"])
            stored = archived.get("facts", {})
            # Binding is exact. Date-only original archives are not upgraded to
            # precise publication times. The parser must archive precision explicitly.
            source_binding = R11SourceBinding.model_validate(stored.get("r11_source_binding", {}))
            if (
                source.document_hash != archived.get("original_sha256")
                or source.url != archive["source_ref"]
                or source.lineage != archive["source_lineage"]
                or source.quality != archived.get("quality")
                or source.first_retrieved_at != source_binding.first_retrieved_at
                or source.published_at != source_binding.published_at
                or source.publication_timezone != stored.get("publication_timezone")
                or source.publication_precision != stored.get("publication_precision")
                or source.first_retrieved_at != datetime.fromisoformat(archived["retrieved_at"])
                or source.account_ref != stored.get("account_id")
            ):
                raise LedgerError(
                    "R11_SOURCE_BINDING_MISMATCH", "Source provenance differs from archive"
                )
            if not publication_matches(source, archived):
                raise LedgerError(
                    "R11_PUBLICATION_CONFLICT", "Original publication date/precision conflict"
                )
            if (
                data.context.dataset_kind == "REAL"
                and data.context.evidence_class != "H"
                and datetime.fromisoformat(archive["created_at"]) > data.context.as_of
            ):
                raise LedgerError(
                    "R11_SOURCE_CAPTURE_LATE", "No real source archive existed at cutoff"
                )
            if stored.get("dataset_kind") != data.context.dataset_kind:
                raise LedgerError(
                    "R11_DATASET_KIND_MISMATCH", "Synthetic and real evidence cannot mix"
                )
            snapshots[key] = archive
        if data.context.evidence_class == "F" and data.context.dataset_kind == "REAL":
            # Forward inputs must actually have been archived by the declared
            # cutoff, after the definition approval. Later computation is replay
            # of that frozen capture, not permission to backfill old bundles.
            captured = datetime.fromisoformat(bundle["created_at"])
            approved = datetime.fromisoformat(DEFINITION["approved_at"])
            if not approved <= captured <= data.context.as_of:
                raise LedgerError(
                    "R11_FORWARD_CAPTURE_MISSING",
                    "Input bundle was not frozen in time; use historical diagnosis",
                )
        extraction = reconcile(
            data.model_dump(mode="json"),
            snapshots,
            facts.get("facts", {}).get("r11_bindings", {}),
        )
        return data, dict(bundle=bundle, sources=snapshots, extraction=extraction)

    def runs(self, method: str, *, current_version: bool = False) -> list[dict[str, Any]]:
        with self.research._connect() as c:
            rows = c.execute(
                "SELECT r.payload_json FROM shadow_records r "
                "JOIN shadow_models m ON m.id=r.model_id "
                "WHERE m.model_key IN (?,?) AND (? IS NULL OR m.version=?) "
                "AND r.kind=? ORDER BY r.rowid",
                (
                    "r11-" + method.lower(),
                    "r11-simulation-" + method.lower(),
                    MODEL_VERSION if current_version else None,
                    MODEL_VERSION,
                    "R11_OBSERVATION",
                ),
            ).fetchall()
            return [json.loads(r[0]) for r in rows]

    def observe(self, request: R11Request) -> dict[str, Any]:
        # Explicit caller request creates research metadata only. No production
        # configuration, models, accounts, strategy or scheduler registration.
        body = request.model_dump(mode="json")
        with self.research._connect() as c:
            old = c.execute(
                "SELECT payload_json FROM shadow_records WHERE request_key=?",
                ("R11_OBSERVATION:" + request.idempotency_key,),
            ).fetchone()
            if old:
                saved = json.loads(old[0])
                if saved["request_hash"] != digest(body):
                    raise LedgerError(
                        "SHADOW_KEY_CONFLICT", "Same key has different immutable input"
                    )
                return saved  # type: ignore[no-any-return]
            bundle = self._evidence(c, request.bundle_evidence_id)
            dataset = (
                json.loads(bundle["facts_json"])
                .get("facts", {})
                .get("r11_input", {})
                .get("context", {})
                .get("dataset_kind")
            )
        if dataset not in {"REAL", "SYNTHETIC"}:
            raise LedgerError(
                "R11_DATASET_KIND_MISSING", "Explicit real or synthetic dataset required"
            )
        model = self.shadow.register(self.definition(request.method, dataset))
        body = request.model_dump(mode="json")
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = self.shadow._rows(c, model["id"])
            old = next(
                (
                    r
                    for r in rows
                    if r.get("request_hash") == digest(body)
                    and r.get("request_key") == request.idempotency_key
                    and r["kind"] == "R11_OBSERVATION"
                ),
                None,
            )
            if old:
                return old
            key = c.execute(
                "SELECT payload_json FROM shadow_records WHERE request_key=?",
                ("R11_OBSERVATION:" + request.idempotency_key,),
            ).fetchone()
            if key:
                raise LedgerError("SHADOW_KEY_CONFLICT", "Same key has different immutable input")
            if self.shadow._gate(c, model["id"], "SHADOW")["current"] not in {"SHADOW", "ADVISORY"}:
                raise LedgerError("SHADOW_PAUSED", "New observations paused")
            try:
                data, evidence = self._load(c, request)
            except (ValidationError, KeyError, ValueError) as exc:
                raise LedgerError(
                    "R11_ARCHIVED_INPUT_INVALID", "Archived input/provenance is invalid"
                ) from exc
            prior = [
                r
                for r in rows
                if r["kind"] == "R11_OBSERVATION"
                and r["output"]["evidence_class"] == data.context.evidence_class
                and r["output"]["dataset_kind"] == data.context.dataset_kind
            ]
            same_week = [r for r in prior if r["output"]["as_of"] == data.context.as_of.isoformat()]
            if same_week:
                if request.supersedes != same_week[-1]["id"]:
                    raise LedgerError(
                        "R11_REVISION_REQUIRED", "Reference the current same-week revision"
                    )
            elif request.supersedes:
                raise LedgerError("R11_REVISION_MISMATCH", "No observation for that cutoff")
            if prior and data.context.as_of.isoformat() < max(r["output"]["as_of"] for r in prior):
                raise LedgerError(
                    "R11_OUT_OF_ORDER", "Replay a separate history, do not rewrite later states"
                )
            prior = [r for r in prior if r not in same_week]
            state_fields = {
                "definition_id",
                "method",
                "evidence_class",
                "dataset_kind",
                "computation_version",
                "as_of",
                "last_stable",
                "stable_since",
                "pending_season",
                "pending_weeks",
                "window_end",
                "leader",
                "leading_weeks",
                "leading_score_history",
            }
            history = [{k: v for k, v in r["output"].items() if k in state_fields} for r in prior]
            output = (
                calculate_macro(data, history)
                if isinstance(data, MacroInput)
                else calculate_candidates(data, history)
            )
            output = self._apply_extraction(output, evidence["extraction"], history)
            from investor_core.r11_governance import engine_hash

            saved = self.shadow._append(
                c,
                model["id"],
                "R11_OBSERVATION",
                request.idempotency_key,
                dict(
                    request_hash=digest(body),
                    request_key=request.idempotency_key,
                    request=body,
                    frozen_definition=DEFINITION,
                    definition_hash=digest(DEFINITION),
                    engine_hash=engine_hash(),
                    input=data.model_dump(mode="json"),
                    evidence=evidence,
                    evidence_hash=digest(evidence),
                    previous_outputs=history,
                    previous_run_hashes={r["id"]: r["output_hash"] for r in prior},
                    output=output,
                    output_hash=digest(output),
                    created_at=stamp(self.research._now()),
                    supersedes=request.supersedes,
                    definition_id=VERSION,
                ),
            )

        if data.context.dataset_kind == "REAL" and data.context.evidence_class == "F":
            from investor_core.r11_governance import R11Governance

            R11Governance(self).assess(request.method, "observation:" + saved["id"])
        return saved

    @staticmethod
    def _apply_extraction(
        output: dict[str, Any], extraction: dict[str, Any], history: list[dict[str, Any]]
    ) -> dict[str, Any]:
        output["extraction"] = extraction
        if extraction["status"] != "MATCHED":
            output["gaps"] = sorted(set(output["gaps"] + ["SOURCE_VALUE_BINDING_UNVERIFIED"]))
            output["status"] = "INSUFFICIENT_DATA"
            if output["method"] == "C":
                output["candidate_season"] = "UNKNOWN"
                output.update(stable_state(output, history, rules("C")))
            else:
                output.update(
                    leader=None, leading_weeks=0, leading_score_history=[], replacement="BLOCKED"
                )
                output["replacement_blockers"] = sorted(
                    set(output["replacement_blockers"] + ["SOURCE_VALUE_BINDING_UNVERIFIED"])
                )
                for row in output["rows"]:
                    row["rank"] = None
        return output

    def replay(self, method: str, run_id: str) -> dict[str, Any]:
        with self.research._connect() as c:
            row = c.execute(
                "SELECT r.payload_json FROM shadow_records r "
                "JOIN shadow_models m ON m.id=r.model_id WHERE r.id=? AND r.kind=? "
                "AND m.model_key IN (?,?)",
                (
                    run_id,
                    "R11_OBSERVATION",
                    "r11-" + method.lower(),
                    "r11-simulation-" + method.lower(),
                ),
            ).fetchone()
        if row is None:
            raise LedgerError("R11_RUN_MISSING", "Unknown research run")
        return self.replay_record(json.loads(row[0]))

    @staticmethod
    def replay_record(run: dict[str, Any]) -> dict[str, Any]:
        method = run["output"]["method"]
        run_id = run["id"]
        if run["output"].get("computation_version") == "r11-rules-v1":
            from investor_core.r11_legacy.candidates import (
                CandidateInput as OldCandidateInput,
            )
            from investor_core.r11_legacy.candidates import (
                calculate_candidates as old_candidates,
            )
            from investor_core.r11_legacy.macro import MacroInput as OldMacroInput
            from investor_core.r11_legacy.macro import calculate_macro as old_macro

            replay = (
                old_macro(OldMacroInput.model_validate(run["input"]), run["previous_outputs"])
                if method == "C"
                else old_candidates(
                    OldCandidateInput.model_validate(run["input"]), run["previous_outputs"]
                )
            )
        elif run["output"].get("computation_version") == "r11-rules-v2":
            from investor_core.r11_legacy.v2.candidates import CandidateInput as V2Candidate
            from investor_core.r11_legacy.v2.candidates import calculate_candidates as v2_candidates
            from investor_core.r11_legacy.v2.macro import MacroInput as V2Macro
            from investor_core.r11_legacy.v2.macro import calculate_macro as v2_macro

            replay = (
                v2_macro(V2Macro.model_validate(run["input"]), run["previous_outputs"])
                if method == "C"
                else v2_candidates(
                    V2Candidate.model_validate(run["input"]), run["previous_outputs"]
                )
            )
        elif method == "C":
            replay = calculate_macro(
                MacroInput.model_validate(run["input"]), run["previous_outputs"]
            )
        else:
            replay = calculate_candidates(
                CandidateInput.model_validate(run["input"]), run["previous_outputs"]
            )
        extraction = reconcile(
            run["input"],
            run["evidence"]["sources"],
            json.loads(run["evidence"]["bundle"]["facts_json"])
            .get("facts", {})
            .get("r11_bindings", {}),
        )
        version = run["output"].get("computation_version")
        if version == "r11-rules-v2":
            from investor_core.r11_legacy.v2.provenance import reconcile as v2_reconcile

            extraction = v2_reconcile(
                run["input"],
                run["evidence"]["sources"],
                json.loads(run["evidence"]["bundle"]["facts_json"])
                .get("facts", {})
                .get("r11_bindings", {}),
            )
            replay["extraction"] = extraction
            if replay["dataset_kind"] == "REAL" and extraction["status"] != "MATCHED":
                replay["gaps"] = sorted(set(replay["gaps"] + ["SOURCE_VALUE_BINDING_UNVERIFIED"]))
                replay["status"] = "INSUFFICIENT_DATA"
                if method == "C":
                    replay.update(dominant_season="UNKNOWN", pending_season=None, pending_weeks=0)
                else:
                    replay.update(leader=None, leading_weeks=0, replacement="BLOCKED")
        elif version != "r11-rules-v1":
            replay = R11Service._apply_extraction(replay, extraction, run["previous_outputs"])
        checks = dict(
            definition=run["definition_hash"] == digest(DEFINITION),
            evidence=run["evidence_hash"] == digest(run["evidence"]),
            output=run["output_hash"] == digest(run["output"]),
            deterministic=replay == run["output"],
        )
        return dict(
            run_id=run_id,
            checks=checks,
            result="PASS" if all(checks.values()) else "FAIL",
            money_action=False,
            historical_evaluator=version != "r11-rules-v3",
            qualifies_current_version=version == "r11-rules-v3",
            actual_promotion_authorized=False,
        )
