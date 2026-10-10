"""Small, revision-pinned 003096 H adapter. No collector, database or F writer.

PDF facts are reviewed transcriptions tied to whole original SHA256 and page
locators, not free-form caller assertions. A changed original needs a new review.
"""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from importlib.resources import files
from typing import Any

from investor_core.r11_rules import rules
from investor_core.r12_candidates import _calculate
from investor_core.scheduler import digest


def manifest() -> dict[str, Any]:
    return json.loads(files("investor_core").joinpath("r12_003096_archive.json").read_text("utf-8"))  # type: ignore[no-any-return]


def checked_originals(originals: dict[str, str]) -> tuple[dict[str, Any], dict[str, bytes]]:
    spec = manifest()
    raw = {}
    for key, row in spec["documents"].items():
        try:
            content = base64.b64decode(originals[key], validate=True)
        except (KeyError, ValueError) as exc:
            raise ValueError("ORIGINAL_MISSING_OR_ENCODING:" + key) from exc
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("ORIGINAL_REVISION_NOT_REVIEWED:" + key)
        raw[key] = content
    return spec, raw


def calendar_days(spec: dict[str, Any]) -> list[str]:
    calendar = spec["calendar"]
    closed = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in calendar["closed_ranges"]]
    day, end = date.fromisoformat(calendar["from"]), date.fromisoformat(calendar["to"])
    result = []
    while day <= end:
        if day.weekday() < 5 and not any(a <= day <= b for a, b in closed):
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def prepare(base: dict[str, Any], originals: dict[str, str]) -> dict[str, Any]:
    """Return detached typed inputs. Unknown operating/benchmark facts stay null."""
    spec, raw = checked_originals(originals)
    if base["context"]["evidence_class"] != "H" or base["context"]["as_of"] != spec["as_of"]:
        raise ValueError("REVIEWED_H_SCOPE_ONLY")
    if base["context"]["dataset_kind"] != "REAL":
        raise ValueError("ARCHIVED_REAL_INPUT_REQUIRED")
    result = deepcopy(base)
    source = dict(
        archive_id=spec["version"],
        document_hash=digest(spec),
        url="https://www.zofund.com/fundDetail/003096/index.html",
        lineage="REVIEWED_COMPOSITE_MANAGER_AND_EXCHANGE_ORIGINALS",
        published_at=None,
        publication_precision="UNKNOWN",
        publication_timezone=None,
        first_retrieved_at=None,
        quality="OFFICIAL",
    )
    # Composite time is not substituted for individual original capture times.
    result["context"]["sources"]["zo-archive"] = source
    points = []
    payload = json.loads(raw["zo-nav"])
    seen = set()
    for row in payload["data"]:
        if row["productCode"] != "003096":
            raise ValueError("NAV_SHARE_IDENTITY_MISMATCH")
        day = row["navDate"][:10]
        if day in seen:
            raise ValueError("DUPLICATE_NAV_DATE")
        seen.add(day)
        if day <= spec["calendar"]["to"]:
            points.append(
                dict(day=day, value=format(Decimal(row["relatePrice"]), "f"), source="zo-archive")
            )
    product = dict(code="003096", **{k: deepcopy(v["value"]) for k, v in spec["claims"].items()})
    for field in (
        "facts_source",
        "manager_source",
        "delay_source",
        "dividend_coverage_source",
        "benchmark_mapping_source",
    ):
        product[field] = "zo-archive"
    product["calendar"] = dict(
        source="zo-archive",
        covered_from=spec["calendar"]["from"],
        covered_to=spec["calendar"]["to"],
        dates=calendar_days(spec),
    )
    product["nav"] = dict(
        identity="003096",
        unit="NAV_CNY_NET_INTERNAL_FEES",
        points=sorted(points, key=lambda row: row["day"]),
    )
    # No implied approval, no arbitrary public identity, no inferred operational status.
    result["products"] = [product if p["code"] == "003096" else p for p in result["products"]]
    result["arrival_rules"] = {}
    result["replacement_evidence"] = None
    return result


def evaluate_originals(data: Any, originals: dict[str, str]) -> dict[str, Any]:
    from investor_core.r12_medical import MedicalInput

    spec, _ = checked_originals(originals)
    expected = MedicalInput.model_validate(prepare(data.model_dump(mode="json"), originals))
    actual_product = next(p for p in data.products if p.code == "003096")
    expected_product = next(p for p in expected.products if p.code == "003096")
    if (
        actual_product != expected_product
        or data.context.sources.get("zo-archive") != expected.context.sources["zo-archive"]
    ):
        raise ValueError("EXTRACTED_INPUT_DIFFERS_FROM_REVIEWED_ORIGINALS")
    with localcontext() as ctx:
        ctx.prec = 34
        output = _calculate(expected, [], rules("MEDICAL"), None, medical_r12=True)
    for row in output["rows"]:
        row["rank"] = None
        if row["code"] != "003096":
            row["total_score"] = None
            row["dimension_scores"] = {}
            row["metrics"] = {}
            row["gaps"] = sorted(set(row["gaps"] + ["OUTSIDE_REVIEWED_ORIGINAL_ADAPTER"]))
        if row["code"] == "003096":
            row["warnings"] = sorted(set(row["warnings"] + spec["limitations"]))
    output.update(
        status="INSUFFICIENT_DATA",
        leader=None,
        spread=None,
        tied=False,
        comparable=False,
        replacement="BLOCKED",
        leading_weeks=0,
        leading_score_history=[],
        formal_forward_record=False,
        actual_promotion_authorized=False,
        e_eligibility="NOT_EVALUATED_NO_R12_REGISTRATION_OR_RECEIPTS",
        delay_stress_status="INCOMPLETE",
        adapter_version=spec["version"],
        source_review=spec,
        later_vintage_originals=[
            k
            for k, v in spec["documents"].items()
            if v.get("retrieved_at")
            and datetime.fromisoformat(v["retrieved_at"]) > data.context.as_of
        ],
        unknown_publication_originals=[
            k for k, v in spec["documents"].items() if v.get("published_date") is None
        ],
        unknown_retrieval_originals=[
            k for k, v in spec["documents"].items() if v.get("retrieved_at") is None
        ],
        evidence_class="H",
        display_text="事后原件研究;未取得当时可知证明;未批准替换或前瞻。",
    )
    return output
