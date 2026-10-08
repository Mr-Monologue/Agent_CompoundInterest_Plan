"""Read-only Core mapping/account snapshots; caller assertions never grant approval."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from investor_core.r11_candidates import CandidateInput
from investor_core.scheduler import instant


def newer_approval(c: Any, mapping: dict[str, Any], as_of: datetime) -> bool:
    rows = c.execute(
        "SELECT payload_json FROM research_benchmark_mappings "
        "WHERE portfolio_id=? AND instrument_code=? AND version>?",
        (mapping["portfolio_id"], mapping["instrument_code"], mapping["version"]),
    ).fetchall()
    return any(
        value.get("status") == "APPROVED_RESEARCH"
        and value.get("confirmed_at")
        and instant(value["confirmed_at"]) <= as_of
        for value in (json.loads(row[0]) for row in rows)
    )


def capture(
    c: Any,
    data: CandidateInput,
    portfolio_id: str | None,
    mapping_ids: dict[str, str],
    window_start: str,
) -> dict[str, Any]:
    common = set.intersection(
        *({d for d in p.calendar.dates if d <= data.context.day} for p in data.products)
    )
    if data.benchmark_calendar:
        common &= set(data.benchmark_calendar.dates)
    window_start = str(sorted(common)[-253]) if len(common) >= 253 else window_start
    mappings = {}
    for product in data.products:
        blockers = []
        if data.cohort == "A500" and (
            data.benchmark is None
            or product.benchmark_identity != data.benchmark.identity
            or product.benchmark_identity != "000510CNY010"
            or product.benchmark_return_basis != "TOTAL_RETURN"
            or product.benchmark_currency != "CNY"
        ):
            blockers.append("MAPPING_DIFFERS_FROM_SCORED_BENCHMARK")
        row = c.execute(
            "SELECT payload_json FROM research_benchmark_mappings WHERE id=?",
            (mapping_ids.get(product.code),),
        ).fetchone()
        value = json.loads(row[0]) if row else None
        if value:
            value.pop("confirmation_digest", None)
            value.pop("confirmation_token", None)
            if value["portfolio_id"] != portfolio_id or value["instrument_code"] != product.code:
                blockers.append("MAPPING_SCOPE_MISMATCH")
            if value["status"] != "APPROVED_RESEARCH" or not value.get("confirmed_at"):
                blockers.append("MAPPING_NOT_APPROVED")
            elif (
                data.context.evidence_class != "H"
                and instant(value["confirmed_at"]) > data.context.as_of
            ):
                blockers.append("MAPPING_APPROVAL_AFTER_CUTOFF")
            if newer_approval(c, value, data.context.as_of):
                blockers.append("MAPPING_VERSION_SUPERSEDED")
            # A composite contract benchmark is not an exact single-index research mapping.
            for point in product.nav.points:
                if not window_start <= str(point.day) <= str(data.context.day):
                    continue
                periods = [
                    p
                    for p in value["diagnostic_mapping"]
                    if p.get("effective_from")
                    and p["effective_from"] <= str(point.day)
                    and (not p.get("effective_to") or str(point.day) <= p["effective_to"])
                ]
                if len(periods) != 1:
                    blockers.append("MAPPING_WINDOW_UNCOVERED")
                    break
                components = periods[0]["components"]
                if len(components) != 1 or components[0]["weight_bps"] != 10000:
                    blockers.append("COMPOSITE_MAPPING_NOT_EXACT_SINGLE_PATH")
                    break
                part = components[0]
                if (
                    part["code"] != product.benchmark_identity
                    or part["currency"] != product.benchmark_currency
                    or part["return_basis"] != product.benchmark_return_basis
                    or periods[0]["method"] == "UNKNOWN"
                ):
                    blockers.append("MAPPING_IDENTITY_CURRENCY_OR_BASIS_MISMATCH")
                    break
        else:
            blockers.append("APPROVED_CORE_MAPPING_REQUIRED")
        source = data.context.sources.get(product.benchmark_mapping_source or "")
        mappings[product.code] = dict(
            source_timezone=source.publication_timezone if source else None,
            expected_path=dict(
                code=product.benchmark_identity,
                currency=product.benchmark_currency,
                return_basis=product.benchmark_return_basis,
            ),
            snapshot=value,
            blockers=sorted(set(blockers)),
            retrospective_h_only=bool(
                value
                and value.get("confirmed_at")
                and data.context.evidence_class == "H"
                and instant(value["confirmed_at"]) > data.context.as_of
            ),
        )
    account = None
    account_blockers = []
    if data.replacement_evidence:
        row = c.execute(
            "SELECT * FROM accounts WHERE id=?", (data.replacement_evidence.account_ref,)
        ).fetchone()
        account = dict(row) if row else None
        if not account or account["portfolio_id"] != portfolio_id:
            account_blockers.append("CORE_ACCOUNT_SCOPE_MISMATCH")
        elif account["status"] != "ACTIVE" or account["currency"] != "CNY":
            account_blockers.append("CORE_ACCOUNT_NOT_ACTIVE_CNY")
    return dict(
        portfolio_id=portfolio_id,
        mappings=mappings,
        account=account,
        account_blockers=account_blockers,
        read_only=True,
    )


def apply(output: dict[str, Any], bindings: dict[str, Any]) -> dict[str, Any]:
    output["core_bindings"] = bindings
    blockers = list(bindings["account_blockers"])
    for row in output["rows"]:
        gaps = bindings["mappings"][row["code"]]["blockers"]
        blockers += gaps
        row["warnings"] = sorted(set([w for w in row["warnings"] if w != "MAPPING_DRAFT"] + gaps))
    if blockers:
        output.update(leading_weeks=0, leading_score_history=[], replacement="BLOCKED")
        output["replacement_blockers"] = sorted(set(output["replacement_blockers"] + blockers))
    output["mapping_qualified"] = all(not m["blockers"] for m in bindings["mappings"].values())
    return output


def currently_effective(mapping: dict[str, Any], binding: dict[str, Any], now: datetime) -> bool:
    """Inclusive calendar dates in the archived source timezone, never host time."""
    try:
        day = now.astimezone(ZoneInfo(binding["source_timezone"])).date().isoformat()
        expected = binding["expected_path"]
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return False
    periods = [
        p
        for p in mapping["diagnostic_mapping"]
        if p.get("effective_from")
        and p["effective_from"] <= day
        and (not p.get("effective_to") or day <= p["effective_to"])
    ]
    if len(periods) != 1:
        return False
    period = periods[0]
    parts = period["components"]
    return (
        period["method"] != "UNKNOWN"
        and len(parts) == 1
        and parts[0]["weight_bps"] == 10000
        and all(parts[0].get(key) == value for key, value in expected.items())
    )


def changed(c: Any, bindings: dict[str, Any], now: datetime) -> bool:
    """Current approval/entity drift blocks new advice without rewriting saved runs."""
    for mapping in bindings["mappings"].values():
        old = mapping["snapshot"]
        if not old:
            return True
        row = c.execute(
            "SELECT payload_json FROM research_benchmark_mappings WHERE id=?", (old["id"],)
        ).fetchone()
        current = json.loads(row[0]) if row else {}
        current.pop("confirmation_digest", None)
        current.pop("confirmation_token", None)
        if (
            current != old
            or current.get("status") != "APPROVED_RESEARCH"
            or newer_approval(c, old, now)
            or not currently_effective(current, mapping, now)
        ):
            return True
    account = bindings.get("account")
    if account:
        row = c.execute("SELECT * FROM accounts WHERE id=?", (account["id"],)).fetchone()
        if not row or dict(row) != account:
            return True
    return False
