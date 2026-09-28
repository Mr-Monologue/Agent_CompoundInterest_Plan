"""Versioned, deterministic longitudinal comparison. No storage or investment actions."""

from __future__ import annotations

from datetime import date
from typing import Any, cast

from investor_core.scheduler import digest

Json = dict[str, Any]
COMPARISON_VERSION = "baseline-delta-v1"
STATES = ("UNCHANGED", "NEW", "RESOLVED", "CHANGED", "REGRESSED", "STALE")
CATEGORIES = {
    "APPROVAL_GAP": "CONFIG_PENDING",
    "OBSERVED_REVIEW": "OBSERVATION",
    "EVIDENCE_RECONCILIATION": "EVIDENCE_DIFFERENCE",
}
PROBLEMS = {"DATA_GAP", "CONFIG_PENDING", "EVIDENCE_DIFFERENCE"}
# Presentation and transport fields are never identity or evidence of resolution.
IGNORED = {
    "text",
    "description",
    "display_text",
    "next_step",
    "reason_key",
    "evidence_version",
    "run_id",
    "created_at",
    "expires_at",
    "facts_hash",
    "retrieved_at",
    "published_date",
    "idempotency_key",
    "request_hash",
    "actor_ref",
    "source_ref",
    "source_name",
    "url",
    "excerpt",
    "explanation",
    "rationale",
    "id",
    "evidence_ids",
    "limitation",
    "limitations",
}


def normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in sorted(value.items()) if k not in IGNORED}
    if isinstance(value, list):
        unique = {digest(normalized(v)): normalized(v) for v in value}
        return [unique[k] for k in sorted(unique)]
    return value


def observation(
    code: str,
    subject: str,
    category: str,
    value: Json,
    *,
    window: str = "",
    evidence: list[Json] | None = None,
    description: str = "",
    dates: list[str] | None = None,
    approval: Any = None,
    sufficient: bool = False,
    quality: str = "UNKNOWN",
    valid_until: str | None = None,
    freshness_basis: str | None = None,
    unknowns: list[str] | None = None,
) -> Json:
    if quality == "UNKNOWN":
        reported = {s.get("facts", {}).get("quality") for s in evidence or []}
        quality = next(
            (q for q in ("CONFLICT", "INVALID", "UNAVAILABLE") if q in reported), quality
        )
        if quality == "UNKNOWN" and reported:
            if reported <= {"OFFICIAL", "VERIFIED", "PASS"}:
                quality = "SUPPORTED"
            elif reported & {"UNVERIFIED", "REPOST"}:
                quality = "UNVERIFIED"
    # Category deliberately omitted: the same subject can migrate classifications.
    return dict(
        observation_key=digest([code, subject, window]),
        holding_id=code,
        subject=subject,
        window=window,
        category=CATEGORIES.get(category, category),
        value=normalized(value),
        evidence=evidence or [],
        description=description,
        data_dates=sorted(set(dates or [])),
        approval_state=approval,
        resolution_supported=sufficient,
        quality=quality,
        valid_until=valid_until,
        freshness_basis=freshness_basis,
        unknowns=unknowns or [],
    )


def evidence_values(refs: list[Json]) -> list[Json]:
    # Archive IDs and collection times are not source identities. Content stays traceable.
    return cast(
        list[Json],
        normalized(
            [
                dict(
                    lineage=s.get("source_lineage")
                    or s.get("source_name")
                    or "UNKNOWN_SOURCE_SCOPE",
                    content=(
                        s.get("facts", {}).get("original_sha256")
                        or s.get("facts", {}).get("excerpt_sha256")
                        or (
                            digest(
                                dict(
                                    normalized(s["facts"]), quoted_source=s["facts"].get("excerpt")
                                )
                            )
                            if s.get("facts")
                            else s.get("facts_hash")
                        )
                    ),
                    quality=s.get("facts", {}).get("quality"),
                    data_date=s.get("facts", {}).get("data_date") or s.get("evidence_date"),
                )
                for s in refs
            ]
        ),
    )


def project(raw: Json) -> Json:
    """Adapt legacy immutable snapshots without rewriting their identities or content."""
    records: list[Json] = []
    for item in raw["items"]:
        code = item["instrument_code"]
        reasons = item["reasons"]
        refs = list({s["id"]: s for r in reasons for s in r["sources"]}.values())
        comparison = item["comparison"]
        approved = comparison["mapping_approved"]
        common = dict(evidence=refs, approval=approved)
        reliable = any(
            s.get("facts", {}).get("quality") in {"OFFICIAL", "VERIFIED", "PASS"} for s in refs
        )
        records.append(
            observation(
                code,
                "benchmark_mapping",
                "OBSERVATION" if approved else "CONFIG_PENDING",
                dict(
                    periods=comparison["diagnostic_periods"],
                    version=comparison["mapping_version"],
                    approved=approved,
                ),
                sufficient=approved,
                description="研究映射批准状态与历史口径",
                **common,
            )
        )
        gaps = comparison["gaps"]
        missing = next(
            (r["basis"].get("missing", {}) for r in reasons if r["kind"] == "COVERAGE_GAP"), {}
        )
        records.append(
            observation(
                code,
                "comparison_coverage",
                "DATA_GAP" if gaps else "OBSERVATION",
                dict(gaps=gaps, missing=missing, eligibility=comparison["eligibility"]),
                unknowns=gaps,
                sufficient=not gaps and bool(item["windows"]) and reliable,
                description="比较资格与数据覆盖",
                **common,
            )
        )
        for w in item["windows"]:
            # Fixed windows retain exact dates. Rolling windows require an explicit window_key.
            key = w.get("window_key") or w["start"] + "/" + w["end"]
            values = {k: v for k, v in w.items() if k not in {"start", "end", "run_id"}}
            values["conclusion"] = (
                "LAG"
                if w["excess_percentage_points"] < 0
                else "LEAD"
                if w["excess_percentage_points"] > 0
                else "EQUAL"
            )
            records.append(
                observation(
                    code,
                    "relative_performance",
                    "OBSERVATION",
                    values,
                    window=key,
                    dates=[w["start"], w["end"]],
                    description="对应基准相对表现;不是选股Alpha或买卖信号",
                    **common,
                )
            )
        for topic in ("fees", "concentration"):
            selected = [
                r for r in reasons if r["kind"] in {topic.upper(), topic.upper() + "_UNKNOWN"}
            ]
            known = [r for r in selected if r["category"] == "CONTEXT_OBSERVATION"]
            sources = [s for r in known for s in r["sources"]]
            records.append(
                observation(
                    code,
                    topic,
                    "CONTEXT_OBSERVATION" if known else "DATA_GAP",
                    dict(
                        claims=[normalized(r["basis"]) for r in known],
                        evidence=evidence_values(sources),
                    ),
                    evidence=sources,
                    dates=[d for r in known for d in r["data_dates"]],
                    description="费用披露" if topic == "fees" else "集中度披露",
                    sufficient=bool(known and sources)
                    and any(
                        s.get("facts", {}).get("quality") in {"OFFICIAL", "VERIFIED", "PASS"}
                        for s in sources
                    ),
                    unknowns=[] if known else ["缺少完整披露"],
                    approval=approved,
                )
            )
        thesis = item["thesis"]
        triggers = [r["basis"].get("triggers", []) for r in reasons if r["kind"] == "THESIS_REVIEW"]
        records.append(
            observation(
                code,
                "thesis",
                "EVIDENCE_DIFFERENCE" if triggers else "OBSERVATION",
                dict(
                    status=thesis["status"],
                    decision=thesis["decision"],
                    candidate_version=thesis["candidate_version"],
                    active_case=thesis["active_case"],
                    candidate_case=thesis.get("candidate_case"),
                    triggers=triggers,
                ),
                description="论点版本、批准及证据复核状态",
                unknowns=thesis["evidence_gaps"],
                **common,
            )
        )
        for r in reasons:
            if r["kind"] in {
                "RELATIVE_LAG",
                "MAPPING_PENDING",
                "COVERAGE_GAP",
                "FEES",
                "FEES_UNKNOWN",
                "CONCENTRATION",
                "CONCENTRATION_UNKNOWN",
                "THESIS_REVIEW",
            }:
                continue
            records.append(
                observation(
                    code,
                    r["kind"].lower(),
                    r["category"],
                    r["basis"],
                    evidence=r["sources"],
                    dates=r["data_dates"],
                    description=r["text"],
                    unknowns=r["unresolved_source_ids"],
                    approval=approved,
                )
            )
        # Watches were already present in the original governed thesis decision.
        decision = thesis.get("decision") or {}
        for watch in decision.get("evidence_watches", []):
            eid = watch["evidence_id"]
            sources = [s for s in refs if s["id"] == eid]
            records.append(
                observation(
                    code,
                    "evidence_watch",
                    "OBSERVATION",
                    dict(evidence_id=eid),
                    window=eid,
                    evidence=sources,
                    valid_until=watch.get("valid_until"),
                    freshness_basis=watch.get("basis"),
                    description="已确认的证据有效期",
                    unknowns=[] if watch.get("valid_until") else ["有效期未知,不推断永久有效"],
                )
            )
        records.append(
            observation(
                code,
                "research_quality",
                "OBSERVATION",
                dict(warnings=item["warnings"], limitations=comparison["limitations"]),
                description="研究质量与适用限制",
                **common,
            )
        )
        for candidate in raw.get("pending_mappings", {}).get(code, []):
            records.append(
                observation(
                    code,
                    "mapping_candidate",
                    "CONFIG_PENDING",
                    candidate,
                    description="新的研究映射候选,尚未批准",
                    approval=False,
                )
            )
    return dict(
        observed_on=raw["observed_on"],
        records=records,
        holdings=[dict(code=i["instrument_code"], name=i["instrument_name"]) for i in raw["items"]],
        window_count=sum(len(i["windows"]) for i in raw["items"]),
        classification_count=sum(len(i["reasons"]) for i in raw["items"]),
    )


def structured(record: Json, observed_on: str) -> Json:
    stale = bool(
        record.get("valid_until")
        and record.get("freshness_basis")
        and date.fromisoformat(observed_on) > date.fromisoformat(record["valid_until"])
    )
    return dict(
        category=record["category"],
        value=record["value"],
        evidence=evidence_values(record["evidence"]),
        quality=record["quality"],
        approval=record["approval_state"],
        stale=stale,
        valid_until=record["valid_until"],
        freshness_basis=record["freshness_basis"],
        resolution_supported=record["resolution_supported"],
    )


def state_hash(current: Json) -> str:
    return digest(
        dict(
            records=sorted(
                [
                    (r["observation_key"], structured(r, current["observed_on"]), r["data_dates"])
                    for r in current["records"]
                ]
            ),
            holdings=sorted(h["code"] for h in current["holdings"]),
        )
    )


class MaterialityEvaluator:
    @staticmethod
    def evaluate(before: Json, after: Json) -> str:
        if before == after:
            return "NONE"

        # Small numerical changes below displayed precision do not create new conclusions.
        def rounded(value: Any) -> Any:
            if isinstance(value, float):
                return round(value, 2)
            if isinstance(value, dict):
                return {k: rounded(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rounded(v) for v in value]
            return value

        return "MINOR" if rounded(before) == rounded(after) else "MATERIAL"


class DeltaEngine:
    def evaluate(self, baseline: Json, current: Json) -> Json:
        def index(state: Json) -> dict[str, Json]:
            result = {r["observation_key"]: r for r in state["records"]}
            if len(result) != len(state["records"]):
                raise ValueError("Ambiguous semantic identity; comparison blocked")
            return result

        old, new = index(baseline), index(current)
        deltas = []
        for key in sorted(old.keys() | new.keys()):
            b, a = old.get(key), new.get(key)
            bv = structured(b, baseline["observed_on"]) if b else None
            av = structured(a, current["observed_on"]) if a else None
            transition = None
            if b is None:
                status, materiality, why = "NEW", "MATERIAL", "NEW_STRUCTURED_SUBJECT"
            elif a is None:
                status, materiality, why = (
                    "REGRESSED",
                    "MATERIAL",
                    "SUBJECT_NO_LONGER_OBSERVABLE_NOT_RESOLVED",
                )
            else:
                assert av is not None and bv is not None
                materiality = MaterialityEvaluator.evaluate(bv, av)
                if b["category"] != a["category"]:
                    transition = dict(
                        type="CLASSIFICATION_CHANGED",
                        **{"from": b["category"], "to": a["category"]},
                    )
                lost = (
                    (not bv["stale"] and av["stale"])
                    or (bool(bv["evidence"]) and not av["evidence"])
                    or (
                        b["quality"] in {"VALID", "SUPPORTED"}
                        and a["quality"] in {"UNKNOWN", "UNVERIFIED"}
                    )
                    or (
                        a["quality"] in {"INVALID", "UNAVAILABLE", "CONFLICT"}
                        and b["quality"] != a["quality"]
                    )
                    or (
                        a["category"] in {"DATA_GAP", "EVIDENCE_DIFFERENCE"}
                        and b["category"] not in {"DATA_GAP", "EVIDENCE_DIFFERENCE"}
                    )
                )
                solved = (
                    b["category"] in PROBLEMS
                    and a["category"] not in PROBLEMS
                    and a["resolution_supported"]
                    and bool(a["evidence"])
                    and (b["category"] != "CONFIG_PENDING" or a["approval_state"] is True)
                )
                status = (
                    "REGRESSED"
                    if lost
                    else "RESOLVED"
                    if solved
                    else "CHANGED"
                    if materiality != "NONE"
                    else "STALE"
                    if av["stale"]
                    else "UNCHANGED"
                )
                why = (
                    "QUALITY_DEGRADED"
                    if lost
                    else "SUPPORTED_RESOLUTION"
                    if solved
                    else "CLASSIFICATION_CHANGED"
                    if transition
                    else "STRUCTURED_VALUE_CHANGED"
                    if materiality != "NONE"
                    else "SAME_FACTS"
                )
                if materiality == "NONE" and b["data_dates"] != a["data_dates"]:
                    status, materiality, why = "CHANGED", "MINOR", "WINDOW_DATES_ONLY"
                # URL/publication metadata changes are details, never source-content changes.
                if materiality == "NONE" and normalized_metadata(b) != normalized_metadata(a):
                    status, materiality, why = "CHANGED", "MINOR", "SOURCE_METADATA_ONLY"
            subject = a or b
            assert subject is not None
            deltas.append(
                dict(
                    observation_key=key,
                    holding_id=subject["holding_id"],
                    subject=subject["subject"],
                    window=subject["window"],
                    status=status,
                    materiality=materiality,
                    before=b,
                    after=a,
                    change_reason=why,
                    transition=transition,
                    evidence=subject["evidence"],
                    unresolved_problem=bool(a and a["category"] in PROBLEMS),
                    risk_alert=False,
                    approval_mutation=False,
                    holding_mutation=False,
                )
            )
        counts = {s: sum(d["status"] == s for d in deltas) for s in STATES}
        material_counts = {
            s: sum(d["status"] == s and d["materiality"] == "MATERIAL" for d in deltas)
            for s in STATES
        }
        return dict(
            comparison_version=COMPARISON_VERSION,
            persistence="ephemeral",
            deltas=deltas,
            summary=dict(
                states=counts,
                material_states=material_counts,
                materiality={
                    s: sum(d["materiality"] == s for d in deltas)
                    for s in ("MATERIAL", "MINOR", "NONE")
                },
            ),
            current=current,
            current_state_hash=state_hash(current),
            writes_performed=False,
            approval_mutation=False,
            holding_mutation=False,
            money_action=False,
        )


def normalized_metadata(record: Json) -> Any:
    return sorted(
        {
            digest(
                {k: s.get(k) for k in ("source_ref", "published_date")}
                | {"published": s.get("facts", {}).get("published_date")}
            )
            for s in record["evidence"]
        }
    )
