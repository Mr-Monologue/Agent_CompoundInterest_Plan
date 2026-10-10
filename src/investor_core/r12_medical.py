"""Opt-in R1.2 medical candidate, pure archived-input evaluation; no database writes.

Existing R1.1 services, registrations, approvals and calculators remain R1.1.
This review candidate cannot register F or confer E eligibility.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime
from decimal import localcontext
from importlib.resources import files
from typing import Any, Literal

from pydantic import Field

from investor_core.execution import StrictModel
from investor_core.r11_candidates import CandidateInput, Product
from investor_core.r11_inputs import DEFINITION as R11_DEFINITION
from investor_core.r11_provenance import publication_matches, reconcile
from investor_core.r11_quality import apply_extraction
from investor_core.r11_rules import rules
from investor_core.r12_candidates import _calculate
from investor_core.scheduler import digest

COMPUTATION_VERSION = "r12-medical-v1"
DEFINITION = {
    **R11_DEFINITION,
    "definition_id": "r12-medical-research-1.0.0",
    "parent_definition_id": R11_DEFINITION["definition_id"],
    "approved_at": None,  # No invented instant; explicit implementation approval below.
    "implementation_approval_date": "2026-10-10",
    "source_commit": "405504eb9ab52cab353590be2fbaf810ad5f37a7",
    "source_path": "docs/SHADOW_MODEL_MINIMUM_PLAN.md#r12-medical-proposal",
    "authorization": "ISOLATED_CANDIDATE_ONLY_NO_FORWARD_OR_PROMOTION",
    "change": "MEDICAL_REDEMPTION_ARRIVAL_EXECUTION_ONLY",
}
# A new proposal is not the original definition blob.
DEFINITION.pop("source_git_blob", None)
DEFINITION.pop("source_sha256", None)


class ArrivalRule(StrictModel):
    source: str
    channel: str = Field(min_length=1)
    effective_from: date
    effective_to: date | None = None
    object: Literal["INVESTOR_ACCOUNT_ARRIVAL", "PAYMENT"]
    day_basis: Literal["TRADING_DAYS", "WORKDAYS_UNRESOLVED"]


class MedicalInput(CandidateInput):
    cohort: Literal["MEDICAL"] = "MEDICAL"
    arrival_rules: dict[str, ArrivalRule] = Field(default_factory=dict)


def arrival_status(product: Product, data: CandidateInput) -> dict[str, Any]:
    rule = getattr(data, "arrival_rules", {}).get(product.code)
    bound = product.redemption_arrival_max_trading_days
    gaps = []
    if rule is None:
        gaps.append("APPLICABLE_ARRIVAL_RULE_MISSING")
    else:
        gaps += data.context.source_gaps(rule.source)
        if rule.object != "INVESTOR_ACCOUNT_ARRIVAL":
            gaps.append("PAYMENT_IS_NOT_ARRIVAL")
        if rule.day_basis != "TRADING_DAYS":
            gaps.append("TRADING_DAY_BASIS_UNKNOWN")
        if rule.effective_from > data.context.day or (
            rule.effective_to is not None and rule.effective_to < data.context.day
        ):
            gaps.append("ARRIVAL_RULE_NOT_EFFECTIVE")
    if bound is None:
        gaps.append("ARRIVAL_BOUND_UNKNOWN")
    return dict(
        state="UNKNOWN"
        if gaps
        else "NOT_SATISFIED"
        if bound is not None and bound > 5
        else "SATISFIED",
        max_trading_days=bound,
        rule=rule.model_dump(mode="json") if rule else None,
        reasons=sorted(set(gaps)),
        overall_execution_pass=False,
    )


def select_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in history
        if (
            row.get("definition_id") == DEFINITION["definition_id"]
            and row.get("computation_version") == COMPUTATION_VERSION
            and row.get("method") == "MEDICAL"
        )
    ]


def _arrival_path(path: str) -> bool:
    return path.startswith("/arrival_rules") or path in {
        "/products/0/redemption_arrival_max_trading_days",
        "/products/1/redemption_arrival_max_trading_days",
    }


def evaluate(
    data: MedicalInput,
    history: list[dict[str, Any]] | None = None,
    *,
    evidence: dict[str, Any] | None = None,
    bindings: dict[str, Any] | None = None,
    core_bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One pure candidate evaluation. Real inputs require the existing raw-byte checks.

    It does not accept approval flags as registration or promotion receipts.
    Execution-only provenance failures cannot erase valid research components.
    """
    prior = select_history(history or [])
    with localcontext() as ctx:
        ctx.prec = 34
        output = _calculate(data, prior, rules("MEDICAL"), core_bindings, medical_r12=True)
    extraction = None
    if evidence is not None or data.context.dataset_kind == "REAL":
        body = data.model_dump(mode="json")
        extraction = reconcile(body, evidence or {}, bindings or {})
        arrival_body = deepcopy(body)
        for product in arrival_body["products"]:
            rule = data.arrival_rules.get(product["code"])
            if rule:
                product["delay_source"] = rule.source
        arrival_check = reconcile(arrival_body, evidence or {}, bindings or {})
        extraction["failures"] = [
            f for f in extraction["failures"] if not _arrival_path(f["path"])
        ] + [f for f in arrival_check["failures"] if _arrival_path(f["path"])]
        bad_sources = set()
        for key, source in data.context.sources.items():
            try:
                saved = (evidence or {})[key]
                archived = json.loads(saved["facts_json"])
                facts = archived["facts"]
                if not (
                    saved["id"] == source.archive_id
                    and saved["source_ref"] == source.url
                    and saved["source_lineage"] == source.lineage
                    and archived["original_sha256"] == source.document_hash
                    and archived["quality"] == source.quality
                    and datetime.fromisoformat(archived["retrieved_at"])
                    == source.first_retrieved_at
                    and facts["publication_timezone"] == source.publication_timezone
                    and facts["publication_precision"] == source.publication_precision
                    and publication_matches(source, archived)
                ):
                    bad_sources.add(key)
            except (KeyError, TypeError, ValueError):
                bad_sources.add(key)
        for match in extraction["matched"] + arrival_check["matched"]:
            if match.get("source") in bad_sources:
                extraction["failures"].append(
                    dict(path=match["path"], reason="ARCHIVED_SOURCE_IDENTITY_MISMATCH")
                )
        extraction["status"] = "UNVERIFIED" if extraction["failures"] else "MATCHED"
        execution_failures = [f for f in extraction["failures"] if _arrival_path(f["path"])]
        research = deepcopy(extraction)
        research["failures"] = [f for f in extraction["failures"] if not _arrival_path(f["path"])]
        research["status"] = "UNVERIFIED" if research["failures"] else "MATCHED"
        output = apply_extraction(output, research, prior)
        if research["failures"]:
            for row in output["rows"]:
                row["total_score"] = None
        for row in output["rows"]:
            index = next(i for i, p in enumerate(data.products) if p.code == row["code"])
            paths = (f"/products/{index}/", f"/arrival_rules/{row['code']}/")
            if any(f["path"].startswith(paths) for f in execution_failures):
                row["redemption_arrival"]["state"] = "UNKNOWN"
                row["redemption_arrival"]["reasons"].append("ARRIVAL_SOURCE_VALUE_UNVERIFIED")
                row["warnings"] = sorted(set(row["warnings"] + ["REDEMPTION_ARRIVAL_UNKNOWN"]))
        output["execution_extraction_failures"] = execution_failures
        output["full_extraction"] = extraction
    # Never rely solely on the research filter to prevent an execution conclusion.
    blocked = [r["code"] for r in output["rows"] if r["redemption_arrival"]["state"] != "SATISFIED"]
    if blocked:
        output.update(replacement="BLOCKED", leading_weeks=0, leading_score_history=[])
        output["replacement_blockers"] = sorted(
            set(
                output["replacement_blockers"]
                + [code + ":ARRIVAL_NOT_VERIFIED_WITHIN_FIVE_TRADING_DAYS" for code in blocked]
            )
        )
    output.update(
        display_text="医疗研究评分与到账资格分列;研究分数不代表可执行替换。",
        formal_forward_record=False,
        actual_promotion_authorized=False,
        e_eligibility="NOT_EVALUATED_NO_R12_REGISTRATION_OR_RECEIPTS",
        delay_stress_status="INCOMPLETE"
        if any(
            p.subscription_confirmation_max_trading_days is None
            or p.redemption_arrival_max_trading_days is None
            for p in data.products
        )
        or blocked
        else "NOT_RUN",
    )
    return output


def computation_fingerprint() -> str:
    from investor_core.r11_governance import engine_hash

    root = files("investor_core")
    return digest(
        dict(
            parent=engine_hash(),
            own={
                name: hashlib.sha256(root.joinpath(name).read_bytes()).hexdigest()
                for name in ("r12_medical.py", "r12_candidates.py")
            },
        )
    )


def snapshot(data: MedicalInput, **kwargs: Any) -> dict[str, Any]:
    """Return a detached review artifact, not a persisted/eligible observation."""
    output = evaluate(data, **kwargs)
    return dict(
        definition=deepcopy(DEFINITION),
        definition_hash=digest(DEFINITION),
        computation_fingerprint=computation_fingerprint(),
        input_hash=digest(data.model_dump(mode="json")),
        input=data.model_dump(mode="json"),
        arguments=deepcopy(kwargs),
        output=output,
        output_hash=digest(output),
    )


def replay(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("definition_hash") != digest(DEFINITION) or digest(
        record.get("definition")
    ) != digest(DEFINITION):
        raise ValueError("R1.2 exact definition required; R1.1 is replayed by its own service")
    if (
        record.get("input_hash") != digest(record["input"])
        or record.get("computation_fingerprint") != computation_fingerprint()
    ):
        raise ValueError("Exact archived input and computation required")
    output = evaluate(MedicalInput.model_validate(record["input"]), **record["arguments"])
    return dict(
        result="PASS"
        if digest(output) == record["output_hash"] == digest(record["output"])
        else "FAIL",
        formal_forward_record=False,
        actual_promotion_authorized=False,
    )
