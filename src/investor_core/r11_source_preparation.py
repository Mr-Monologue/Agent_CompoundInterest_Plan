"""Read-only source preparation and replay, separate from R11Service.observe.

Snapshots are returned to the caller; no registration, observation, approval,
financial record, scheduling or production mutation is performed here.
"""

from __future__ import annotations

import json
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal

from investor_core.r11_inputs import DEFINITION, clip
from investor_core.r11_rules import rules
from investor_core.r11_source_archive import read_envelope
from investor_core.r11_source_json import MAX_COLLECTION_BYTES, fullgoal_nav_collection
from investor_core.r11_source_monthly import monthly_html, pbc_stock_xlsx
from investor_core.research import ResearchService
from investor_core.scheduler import digest

PREPARATION_VERSION = "r11-source-preparation-v2"
LEGACY_VERSION = "r11-source-preparation-v1"
Kind = Literal["MONTHLY", "FULLGOAL_NAV"]


def _extract(
    kind: Kind, sources: list[dict[str, Any]], *, historical: bool = False
) -> dict[str, Any]:
    if not 1 <= len(sources) <= 32:
        raise ValueError("bounded source set required")
    originals = [read_envelope(source) for source in sources]
    if sum(original.byte_length for original in originals) > MAX_COLLECTION_BYTES:
        raise ValueError("preparation byte limit exceeded")
    if kind == "FULLGOAL_NAV":
        return fullgoal_nav_collection(originals)
    if kind != "MONTHLY":
        raise ValueError("unsupported preparation kind")
    html_reader, xlsx_reader = monthly_html, pbc_stock_xlsx
    if historical:
        from investor_core.r11_legacy import source_monthly_v1

        html_reader, xlsx_reader = source_monthly_v1.monthly_html, source_monthly_v1.pbc_stock_xlsx
    results = []
    for original in originals:
        if original.media_type == "text/html":
            results.append(html_reader(original))
        elif original.media_type == "application/xlsx":
            results.append(xlsx_reader(original))
        else:
            raise ValueError("monthly format unsupported; PDF is not adapted")
    return dict(
        extractions=results,
        status="PARTIAL_SOURCE_PREPARATION",
        conditional_arithmetic=_monthly_arithmetic(results),
    )


def _monthly_arithmetic(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Show frozen-formula arithmetic without treating source claims as qualified inputs.

    Only per-release HTML points are selected; a later annual table never silently
    replaces original releases. No cutoff, season or effective week is asserted.
    """
    params = rules("C")
    values: dict[str, str | None] = {"F": None, "L": None}
    periods: dict[str, list[str]] = {}
    for dimension, identity in (
        ("F", "NBS_MANUFACTURING_PMI"),
        ("L", "PBC_TSF_STOCK_YOY_SAME_BASIS"),
    ):
        points = sorted(
            [r for r in results if r["identity"] == identity and "day" in r],
            key=lambda r: r["day"],
        )
        periods[dimension] = [r["day"] for r in points]
        months = [int(r["day"][:4]) * 12 + int(r["day"][5:7]) for r in points]
        if len(points) != 4 or any(b - a != 1 for a, b in pairwise(months)):
            continue
        first, last = Decimal(points[0]["value"]), Decimal(points[-1]["value"])
        if dimension == "F":
            value = params["pmi_weights"][0] * clip(
                (last - params["pmi_neutral"]) / params["pmi_level_scale"]
            ) + params["pmi_weights"][1] * clip((last - first) / params["pmi_change_scale"])
        else:
            value = clip((last - first) / params["liquidity_scale"])
        values[dimension] = str(value)
    return dict(
        formula_definition_hash=digest(DEFINITION),
        values=values,
        periods=periods,
        formal_dimension_scores=None,
        season="UNKNOWN",
        effective_forward_weeks=0,
        limitations=[
            "CONDITIONAL_ARITHMETIC_ONLY; NOT_A_QUALIFIED_MODEL_RUN",
            "SOURCE_TIMEZONE_VINTAGE_SAME_BASIS_AND_CUTOFF_NOT_VALIDATED",
            "C_IDENTITY_CALENDAR_V_B_P_STILL_MISSING",
        ],
    )


def prepare_sources(
    research: ResearchService, kind: Kind, evidence_ids: list[str]
) -> dict[str, Any]:
    if not 1 <= len(evidence_ids) <= 32 or len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("unique bounded evidence set required")
    sources = []
    with research._connect() as connection:
        for evidence_id in evidence_ids:
            row = connection.execute(
                "SELECT facts_json FROM market_research_evidence WHERE id=?", (evidence_id,)
            ).fetchone()
            if row is None:
                raise ValueError("original archive missing")
            sources.append(json.loads(row["facts_json"]))
    report = dict(
        preparation_version=PREPARATION_VERSION,
        kind=kind,
        evidence_ids=evidence_ids,
        sources=sources,
        source_hash=digest(sources),
        output=_extract(kind, sources),
        eligible_forward_observation=False,
        promotion_eligible=False,
        status="RESEARCH_PREPARATION_ONLY",
        limitations=[
            "SOURCE_AUTHENTICITY_AND_INDEPENDENCE_NOT_UPGRADED",
            "NOT_A_COMPLETE_C_OR_D_INPUT_BUNDLE",
            "OLD_V4_AND_PROMOTION_READERS_DO_NOT_CONSUME_THIS_FORMAT",
        ],
    )
    return {**report, "report_hash": digest(report)}


def replay_preparation(report: dict[str, Any]) -> dict[str, Any]:
    if report.get("preparation_version") not in {PREPARATION_VERSION, LEGACY_VERSION}:
        raise ValueError("unsupported preparation version")
    if report.get("kind") not in {"MONTHLY", "FULLGOAL_NAV"}:
        raise ValueError("unsupported preparation kind")
    checks = dict(
        report=report["report_hash"]
        == digest({k: v for k, v in report.items() if k != "report_hash"}),
        source=report["source_hash"] == digest(report["sources"]),
        deterministic=report["output"]
        == _extract(
            report["kind"],
            report["sources"],
            historical=report["preparation_version"] == LEGACY_VERSION,
        ),
    )
    return dict(
        checks=checks,
        result="REPRODUCED" if all(checks.values()) else "MISMATCH",
        historical_parser=report["preparation_version"] == LEGACY_VERSION,
        qualifies_current_preparation=False,
        eligible_forward_observation=False,
        promotion_eligible=False,
    )
