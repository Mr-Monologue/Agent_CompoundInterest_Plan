"""Frozen research registrations, evidence reviews and explicit advisory lifecycle.

These transitions affect shadow metadata only. ACTIVE is never a legal target.
External review/CI evidence is archived and bound, never synthesized by this service.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, time, timedelta
from importlib.resources import files
from typing import Any, Literal

from pydantic import Field

from investor_core.execution import StrictModel
from investor_core.ledger import LedgerError
from investor_core.r11_inputs import DEFINITION, TZ
from investor_core.r11_receipts import verify_artifacts
from investor_core.r11_rules import registry
from investor_core.r11_sensitivity import baseline_sets, sensitivity, stress_diagnostics
from investor_core.r11_service import R11Service
from investor_core.r11_validation import ValidationWindow, evidence_summary
from investor_core.scheduler import digest, instant, stamp


def engine_hash() -> str:
    root = files("investor_core")
    return digest(
        {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.iterdir(), key=lambda x: x.name)
            if p.name.startswith("r11_") and p.name.endswith(".py")
        }
    )


class RegistrationRequest(StrictModel):
    window: ValidationWindow
    idempotency_key: str = Field(min_length=1, max_length=200)


class ValidationRequest(StrictModel):
    registration_id: str
    idempotency_key: str = Field(min_length=1, max_length=200)


class ReceiptRequest(StrictModel):
    validation_id: str
    evidence_id: str
    idempotency_key: str = Field(min_length=1, max_length=200)


class Receipt(StrictModel):
    receipt_type: Literal["ENGINEERING", "INDEPENDENT_REVIEW"]
    report_hash: str
    engine_hash: str
    definition_hash: str
    reviewer: str = Field(min_length=1)
    implementer: str = Field(min_length=1)
    reviewed_forward_ids: list[str] = Field(default_factory=list)
    reviewed_history_ids: list[str] = Field(default_factory=list)
    # Underlying immutable artifacts are mandatory, not a bare caller-supplied PASS.
    artifacts: dict[str, str]
    unresolved_high: int = Field(ge=0)
    unresolved_medium: int = Field(ge=0)


class GovernanceDraft(StrictModel):
    method: Literal["C", "MEDICAL", "A500"]
    target: Literal["OFF", "SHADOW", "ADVISORY"]
    validation_id: str | None = None
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)


class GovernanceConfirmation(StrictModel):
    confirmation_token: str
    confirmed_by: str = Field(min_length=1)


class R11Governance:
    def __init__(self, service: R11Service):
        self.service = service
        self.research = service.research
        self.shadow = service.shadow

    def _model(self, method: str) -> dict[str, Any]:
        return self.shadow.register(self.service.definition(method))

    def _find(self, connection: Any, record_id: str, kind: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT payload_json FROM shadow_records WHERE id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise LedgerError("R11_RECORD_MISSING", "Unknown research record")
        value: dict[str, Any] = json.loads(row[0])
        if value["kind"] != kind:
            raise LedgerError("R11_RECORD_KIND", "Wrong research record type")
        return value

    def register(self, request: RegistrationRequest) -> dict[str, Any]:
        model = self._model(request.window.method)
        body = request.model_dump(mode="json")
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            old = c.execute(
                "SELECT payload_json FROM shadow_records WHERE request_key=?",
                ("R11_PREREGISTRATION:" + request.idempotency_key,),
            ).fetchone()
            if old:
                value = json.loads(old[0])
                if value["request_hash"] != digest(body):
                    raise LedgerError("SHADOW_KEY_CONFLICT", "Registration key conflict")
                return value  # type: ignore[no-any-return]
            cutoff = datetime.combine(request.window.forward_start, time(12), TZ)
            if self.research._now() >= cutoff:
                raise LedgerError(
                    "R11_REGISTRATION_LATE", "Register before the first forward cutoff"
                )
            return self.shadow._append(
                c,
                model["id"],
                "R11_PREREGISTRATION",
                request.idempotency_key,
                dict(
                    request_hash=digest(body),
                    window=request.window.model_dump(mode="json"),
                    method=request.window.method,
                    engine_hash=engine_hash(),
                    definition_hash=digest(DEFINITION),
                    registry=registry(request.window.method),
                    created_at=stamp(self.research._now()),
                ),
            )

    def validate(self, request: ValidationRequest) -> dict[str, Any]:
        with self.research._connect() as c:
            registration = self._find(c, request.registration_id, "R11_PREREGISTRATION")
        window = ValidationWindow.model_validate(registration["window"])
        if self.research._now() < datetime.combine(window.forward_end, time(12), TZ):
            raise LedgerError("R11_FORWARD_PERIOD_IN_PROGRESS", "No accelerated future validation")
        runs = self.service.runs(window.method)
        replays = {
            r["id"]: self.service.replay(window.method, r["id"])["result"] == "PASS" for r in runs
        }
        summary = evidence_summary(window, runs, replays)
        summary["not_assessed_by_this_preview"] = []
        summary["pending_external_evidence"] = ["INDEPENDENT_REVIEW", "ENGINEERING"]
        summary.pop("incomplete_adapters", None)
        fixed = baseline_sets(window, runs)
        diagnostics = sensitivity(window, runs, fixed)
        stress = stress_diagnostics(window, runs)
        forward = [
            r
            for r in runs
            if r["output"]["evidence_class"] == "F"
            and r["output"]["dataset_kind"] == "REAL"
            and str(window.forward_start) <= r["output"]["as_of"][:10] <= str(window.forward_end)
        ]
        extra = dict(
            preregistered_engine=registration["engine_hash"] == engine_hash(),
            preregistered_parameters=registration["registry"] == registry(window.method),
            preregistered_definition=registration["definition_hash"] == digest(DEFINITION),
            exact_observation_engine=all(
                r.get("engine_hash") == engine_hash()
                for r in runs
                if r["output"]["dataset_kind"] == "REAL"
            ),
            forward_capture_after_registration=all(
                instant(r["evidence"]["bundle"]["created_at"])
                >= instant(registration["created_at"])
                for r in forward
            ),
            all_forward_values_bound=bool(forward)
            and all(r["output"].get("extraction", {}).get("status") == "MATCHED" for r in forward),
            per_scenario_sensitivity=diagnostics["result"] == "PASS",
            cost_delay_outage_stress=stress["result"] == "PASS",
        )
        report = dict(
            summary=summary,
            additional_checks=extra,
            sensitivity=diagnostics,
            stress=stress,
            method=window.method,
            registration_id=registration["id"],
            engine_hash=engine_hash(),
            definition_hash=digest(DEFINITION),
            run_hashes={r["id"]: digest(r) for r in runs},
            forward_ids=[r["id"] for r in forward],
            history_ids=[
                r["id"]
                for r in runs
                if r["output"]["evidence_class"] == "H"
                and r["output"]["dataset_kind"] == "REAL"
                and str(window.history_start)
                <= r["output"]["as_of"][:10]
                <= str(window.history_end)
            ],
        )
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            return self.shadow._append(
                c,
                registration["model_id"],
                "R11_VALIDATION",
                request.idempotency_key,
                dict(
                    request_hash=digest(request.model_dump(mode="json")),
                    report=report,
                    report_hash=digest(report),
                    created_at=stamp(self.research._now()),
                ),
            )

    def _receipt(self, c: Any, evidence_id: str) -> tuple[Receipt, dict[str, Any]]:
        archived = self.service._evidence(c, evidence_id)
        body = json.loads(archived["facts_json"])
        original = body.get("excerpt", "")
        if (
            not original
            or hashlib.sha256(original.encode()).hexdigest() != body.get("original_sha256")
            or body.get("quality") not in {"OFFICIAL", "ACCOUNT_OBSERVATION"}
        ):
            raise LedgerError(
                "R11_RECEIPT_ORIGINAL_REQUIRED",
                "Full hash-bound accountable review original required",
            )
        try:
            receipt = Receipt.model_validate_json(original)
        except ValueError as exc:
            raise LedgerError("R11_RECEIPT_INVALID", "Typed original receipt required") from exc
        return receipt, archived

    def attach(self, request: ReceiptRequest) -> dict[str, Any]:
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            validation = self._find(c, request.validation_id, "R11_VALIDATION")
            receipt, original = self._receipt(c, request.evidence_id)
            report = validation["report"]
            if (
                receipt.report_hash != validation["report_hash"]
                or receipt.engine_hash != report["engine_hash"]
                or receipt.definition_hash != report["definition_hash"]
                or receipt.reviewer.strip().casefold() == receipt.implementer.strip().casefold()
                or receipt.unresolved_high
                or receipt.unresolved_medium
            ):
                raise LedgerError(
                    "R11_RECEIPT_BINDING_FAILED", "Unresolved or mismatched independent review"
                )
            required = (
                {
                    "full_regression",
                    "ubuntu_ci",
                    "windows_ci",
                    "migration",
                    "idempotency",
                    "six_step_isolation",
                }
                if receipt.receipt_type == "ENGINEERING"
                else {
                    "source_independence",
                    "source_value_reconciliation",
                    "recomputed_observations",
                }
            )
            if not required <= receipt.artifacts.keys():
                raise LedgerError(
                    "R11_RECEIPT_ARTIFACTS_MISSING", "Every required original artifact is needed"
                )
            artifacts = {
                name: self.service._evidence(c, receipt.artifacts[name]) for name in required
            }
            if receipt.receipt_type == "INDEPENDENT_REVIEW" and (
                set(receipt.reviewed_forward_ids) != set(report["forward_ids"])
                or len(set(receipt.reviewed_history_ids)) < min(10, len(set(report["history_ids"])))
                or not set(receipt.reviewed_history_ids) <= set(report["history_ids"])
            ):
                raise LedgerError(
                    "R11_REVIEW_COVERAGE_INCOMPLETE",
                    "Review all forward and at least ten history observations",
                )
            artifacts = verify_artifacts(
                receipt.model_dump(mode="json"),
                report,
                artifacts,
                self.service.runs(report["method"]),
                lambda eid: self.service._evidence(c, eid),
            )
            return self.shadow._append(
                c,
                validation["model_id"],
                "R11_RECEIPT",
                request.idempotency_key,
                dict(
                    request_hash=digest(request.model_dump(mode="json")),
                    validation_id=validation["id"],
                    receipt=receipt.model_dump(mode="json"),
                    original=original,
                    artifacts=artifacts,
                    created_at=stamp(self.research._now()),
                ),
            )

    def _gate(
        self, c: Any, model_id: str, target: str, validation_id: str | None
    ) -> dict[str, Any]:
        rows = self.shadow._rows(c, model_id)
        events = [r for r in rows if r["kind"] == "REVIEW_CONFIRMATION"]
        mode = events[-1]["target"] if events else "SHADOW"
        blockers = []
        if target == "ACTIVE":
            blockers.append("ACTIVE_UNREACHABLE")
        if target == "ADVISORY":
            if mode == "OFF":
                blockers.append("RESUME_SHADOW_WITH_FRESH_CONFIRMATION_FIRST")
            if validation_id is None:
                blockers.append("VALIDATION_REQUIRED")
            else:
                validation = self._find(c, validation_id, "R11_VALIDATION")
                report = validation["report"]
                if validation["model_id"] != model_id:
                    blockers.append("MODEL_SCOPE_MISMATCH")
                if report["engine_hash"] != engine_hash() or validation["report_hash"] != digest(
                    report
                ):
                    blockers.append("VALIDATION_OR_ENGINE_DRIFT")
                blockers += [k for k, v in report["summary"]["computed_checks"].items() if not v]
                blockers += [k for k, v in report["additional_checks"].items() if not v]
                observations = [r for r in rows if r["kind"] == "R11_OBSERVATION"]
                if {r["id"]: digest(r) for r in observations} != report["run_hashes"]:
                    blockers.append("OBSERVATION_SET_CHANGED")
                receipts = [
                    r
                    for r in rows
                    if r["kind"] == "R11_RECEIPT" and r["validation_id"] == validation_id
                ]
                for required in ("ENGINEERING", "INDEPENDENT_REVIEW"):
                    selected = [r for r in receipts if r["receipt"]["receipt_type"] == required]
                    if not selected:
                        blockers.append(required + "_RECEIPT_MISSING")
                    else:
                        latest = selected[-1]
                        for archive in [latest["original"], *latest["artifacts"].values()]:
                            if self.service._evidence(c, archive["id"]) != archive:
                                blockers.append("REVIEW_ARTIFACT_CHANGED")
        return dict(
            current=mode,
            target=target,
            eligible=not blockers,
            blockers=sorted(set(blockers)),
            state_hash=digest(
                [r for r in rows if r["kind"] not in {"REVIEW_DRAFT", "R11_REVIEW_DRAFT"}]
            ),
            money_action=False,
            active_reachable=False,
        )

    def gate(self, method: str, target: str, validation_id: str | None = None) -> dict[str, Any]:
        with self.research._connect() as c:
            row = c.execute(
                "SELECT payload_json FROM shadow_models WHERE model_key=? AND version=?",
                ("r11-" + method.lower(), "1.0.0"),
            ).fetchone()
            if row is None:
                return dict(
                    current="UNREGISTERED",
                    target=target,
                    eligible=False,
                    blockers=["MODEL_NOT_REGISTERED"],
                    money_action=False,
                    active_reachable=False,
                )
            return self._gate(c, json.loads(row[0])["id"], target, validation_id)

    def draft(self, request: GovernanceDraft) -> dict[str, Any]:
        model = self._model(request.method)
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            gate = self._gate(c, model["id"], request.target, request.validation_id)
            if not gate["eligible"]:
                raise LedgerError(
                    "R11_PROMOTION_BLOCKED", "Concrete evidence gate failed", details=gate
                )
            token = secrets.token_urlsafe(32)
            record = self.shadow._append(
                c,
                model["id"],
                "R11_REVIEW_DRAFT",
                request.idempotency_key,
                dict(
                    request_hash=digest(request.model_dump(mode="json")),
                    request=request.model_dump(mode="json"),
                    state_hash=gate["state_hash"],
                    confirmation_digest=digest(token),
                    expires_at=stamp(
                        self.research._now()
                        + timedelta(minutes=self.research.settings.confirmation_ttl_minutes)
                    ),
                ),
            )
            public = self.shadow._public(record)
            if record["confirmation_digest"] == digest(token):
                public["confirmation_token"] = token
            return public

    def confirm(self, draft_id: str, request: GovernanceConfirmation) -> dict[str, Any]:
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            draft = self._find(c, draft_id, "R11_REVIEW_DRAFT")
            if not hmac.compare_digest(
                draft["confirmation_digest"], digest(request.confirmation_token)
            ):
                raise LedgerError(
                    "R11_CONFIRMATION_MISMATCH", "Exact current confirmation required"
                )
            rows = self.shadow._rows(c, draft["model_id"])
            previous = next(
                (
                    r
                    for r in rows
                    if r["kind"] == "REVIEW_CONFIRMATION" and r.get("draft_id") == draft_id
                ),
                None,
            )
            if previous:
                return previous
            if instant(draft["expires_at"]) <= self.research._now():
                raise LedgerError("R11_CONFIRMATION_EXPIRED", "Create a new concrete draft")
            body = draft["request"]
            gate = self._gate(c, draft["model_id"], body["target"], body["validation_id"])
            if not gate["eligible"] or gate["state_hash"] != draft["state_hash"]:
                raise LedgerError(
                    "R11_REVIEW_DRIFT", "Evidence or state changed; review a fresh draft"
                )
            return self.shadow._append(
                c,
                draft["model_id"],
                "REVIEW_CONFIRMATION",
                draft_id,
                dict(
                    request_hash=digest(dict(draft_id=draft_id, target=body["target"])),
                    draft_id=draft_id,
                    target=body["target"],
                    confirmed_by=request.confirmed_by,
                    created_at=stamp(self.research._now()),
                    financial_mutation=False,
                ),
            )

    def assess(self, method: str, idempotency_key: str) -> dict[str, Any]:
        """Discover stale/damaged evidence and downgrade research display only."""
        from investor_core.r11_validation import _switches

        model = self._model(method)
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = self.shadow._rows(c, model["id"])
            events = [r for r in rows if r["kind"] == "REVIEW_CONFIRMATION"]
            mode = events[-1]["target"] if events else "SHADOW"
            real = [
                r
                for r in rows
                if r["kind"] == "R11_OBSERVATION"
                and r["output"]["dataset_kind"] == "REAL"
                and r["output"]["evidence_class"] == "F"
            ]
            latest = real[-1] if real else None
            critical = []
            if latest is None:
                critical.append("NO_REAL_FORWARD_OBSERVATION")
            else:
                output = latest["output"]
                if output["status"] != "CALCULATED_SHADOW" or output["gaps"]:
                    critical.append("INPUT_QUALITY_OR_MAPPING_FAILED")
                if instant(output["as_of"]) + timedelta(days=7) <= self.research._now():
                    critical.append("OBSERVATION_STALE")
                if output.get("extraction", {}).get("status") != "MATCHED":
                    critical.append("INPUT_VALUE_BINDING_FAILED")
                for archive in [
                    latest["evidence"]["bundle"],
                    *latest["evidence"]["sources"].values(),
                ]:
                    if self.service._evidence(c, archive["id"]) != archive:
                        critical.append("SOURCE_ARCHIVE_CHANGED")
                # Replay is read-only on a separate connection, with no lock writes.
                if self.service.replay(method, latest["id"])["result"] != "PASS":
                    critical.append("REPLAY_FAILED")
            validations = [r for r in rows if r["kind"] == "R11_VALIDATION"]
            if validations and validations[-1]["report"]["engine_hash"] != engine_hash():
                critical.append("ENGINE_CHANGED")
            for receipt in [r for r in rows if r["kind"] == "R11_RECEIPT"]:
                for original in [receipt["original"], *receipt["artifacts"].values()]:
                    if self.service._evidence(c, original["id"]) != original:
                        critical.append("REVIEW_EVIDENCE_INVALIDATED")
            by_day = {r["output"]["as_of"][:10]: r["output"] for r in real}
            observations = [by_day[d] for d in sorted(by_day)[-13:]]
            if method == "C":
                seasons = [r.get("dominant_season") for r in observations]
                stable = [s if s not in {None, "UNKNOWN", "TRANSITION"} else None for s in seasons]
                stability = _switches(stable) <= 2 and (
                    not seasons or sum(s is None for s in stable) / len(seasons) <= 0.25
                )
                for i, row in enumerate(observations):
                    baseline = row.get("dominant_season")
                    within = [
                        r.get("dominant_season")
                        for r in observations[i + 1 :]
                        if (instant(r["as_of"]) - instant(row["as_of"])).days <= 28
                    ]
                    other = False
                    for value in within:
                        if value not in {None, "UNKNOWN", "TRANSITION"}:
                            other |= value != baseline
                            if other and value == baseline:
                                stability = False
            else:
                stability = _switches([r.get("leader") for r in observations]) <= 3
            as_of = latest["output"]["as_of"] if latest else stamp(self.research._now())
            earlier = [
                r for r in rows if r["kind"] == "R11_HEALTH_ASSESSMENT" and r["as_of"] < as_of
            ]
            previous = earlier[-1] if earlier else None
            streak = 0 if stability else 1
            if (
                not stability
                and previous
                and not previous["stability_ok"]
                and instant(as_of) - instant(previous["as_of"]) == timedelta(days=7)
            ):
                streak += previous["stability_failure_weeks"]
            request_hash = digest(dict(method=method, idempotency_key=idempotency_key))
            result = self.shadow._append(
                c,
                model["id"],
                "R11_HEALTH_ASSESSMENT",
                idempotency_key,
                dict(
                    request_hash=request_hash,
                    as_of=as_of,
                    critical=sorted(set(critical)),
                    stability_ok=stability,
                    stability_failure_weeks=streak,
                    observed_ids=[r["id"] for r in real],
                    mode_at_assessment=mode,
                    created_at=stamp(self.research._now()),
                    money_action=False,
                ),
            )
            if mode == "ADVISORY" and (
                result["critical"] or result["stability_failure_weeks"] >= 2
            ):
                self.shadow._append(
                    c,
                    model["id"],
                    "REVIEW_CONFIRMATION",
                    "downgrade:" + result["id"],
                    dict(
                        request_hash=digest(result),
                        draft_id="automatic:" + result["id"],
                        target="SHADOW",
                        confirmed_by="R11_RESEARCH_SAFETY",
                        reasons=result["critical"] or ["TWO_CONSECUTIVE_WEEKLY_STABILITY_FAILURES"],
                        created_at=stamp(self.research._now()),
                        financial_mutation=False,
                    ),
                )
            return result
