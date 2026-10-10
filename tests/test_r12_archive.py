"""Narrow provenance adapter tests; all originals below are explicit synthetic fixtures."""

import base64
import hashlib
import json
from copy import deepcopy
from datetime import date

import pytest
from test_r11_calculators import candidates

from investor_core import r12_archive as adapter
from investor_core.r12_medical import MedicalInput, evaluate
from investor_core.r12_time import ResearchContext


@pytest.fixture
def archived(monkeypatch):
    spec = adapter.manifest()
    base = candidates(cutoff=date(2026, 10, 3))
    base["context"].update(evidence_class="H", dataset_kind="REAL")
    base["context"]["as_of"] = spec["as_of"]
    nav = {
        "data": [
            dict(productCode="003096", navDate=p["day"], relatePrice=str(p["value"]))
            for p in base["products"][0]["nav"]["points"]
        ]
    }
    # Synthetic source bytes are NEVER substituted into the real manifest on disk.
    raw = {key: b"synthetic test original" for key in spec["documents"]}
    raw["zo-nav"] = json.dumps(nav).encode()
    for key, value in raw.items():
        spec["documents"][key]["sha256"] = hashlib.sha256(value).hexdigest()
    monkeypatch.setattr(adapter, "manifest", lambda: deepcopy(spec))
    originals = {k: base64.b64encode(v).decode() for k, v in raw.items()}
    return base, originals, spec


@pytest.mark.parametrize("evidence_class", ["H", "K", "F"])
def test_unknown_publication_is_not_retrieval(evidence_class):
    base = candidates()["context"]
    src = base["sources"]["official"]
    src.update(published_at=None, publication_precision="UNKNOWN", publication_timezone=None)
    base["evidence_class"] = evidence_class
    context = ResearchContext.model_validate(base)
    assert context.sources["official"].published_at is None
    assert context.source_gaps("official") == (
        [] if evidence_class == "H" else ["PUBLICATION_OR_RETRIEVAL_TIME_UNKNOWN"]
    )


def test_repeat_read_and_missing_mandatory_fields(archived):
    base, raw, _ = archived
    before = deepcopy(base)
    data = MedicalInput.model_validate(adapter.prepare(base, raw))
    a = evaluate(data, originals=raw)
    assert a == evaluate(data, originals=raw)
    assert base == before
    assert a["rows"][0]["total_score"] is None
    assert "BENCHMARK_MAPPING_INCOMPLETE" in a["rows"][0]["gaps"]
    assert a["leader"] is None and a["replacement"] == "BLOCKED"
    assert not a["formal_forward_record"] and not a["money_action"]
    assert a["unknown_knowledge_times"] == ["zo-archive"]


@pytest.mark.parametrize("mode", ["missing", "changed"])
def test_original_missing_or_revision_change_blocks(archived, mode):
    base, raw, _ = archived
    if mode == "missing":
        raw.pop("zo-legal")
    else:
        raw["zo-legal"] = base64.b64encode(b"changed fee clause").decode()
    with pytest.raises(ValueError, match="ORIGINAL_"):
        adapter.prepare(base, raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("manager_team_since", "2020-01-01"),
        ("benchmark_research_approved", True),
        ("subscription_open", False),
    ],
)
def test_caller_cannot_fill_unknown_or_override_extraction(archived, field, value):
    base, raw, _ = archived
    prepared = adapter.prepare(base, raw)
    next(p for p in prepared["products"] if p["code"] == "003096")[field] = value
    with pytest.raises(ValueError, match="EXTRACTED_INPUT_DIFFERS"):
        evaluate(MedicalInput.model_validate(prepared), originals=raw)


def test_calendar_closes_official_holidays_and_adjusted_work_weekend():
    days = adapter.calendar_days(adapter.manifest())
    assert "2025-10-07" not in days and "2026-09-27" not in days
    assert "2025-10-09" in days and "2026-09-30" in days
    assert len([d for d in days if "2025-09-15" <= d <= "2026-09-30"]) == 253


@pytest.mark.parametrize("evidence_class", ["K", "F"])
def test_original_adapter_cannot_register_other_time_class(archived, evidence_class):
    base, raw, _ = archived
    base["context"]["evidence_class"] = evidence_class
    with pytest.raises(ValueError, match="H_SCOPE"):
        adapter.prepare(base, raw)


def test_other_product_calendar_source_does_not_erase_qualified_components():
    base = candidates()
    base["products"][1]["calendar"] = {
        **base["products"][1]["calendar"],
        "source": "missing-gf-source",
    }
    result = evaluate(MedicalInput.model_validate(base))
    assert result["rows"][0]["total_score"] is not None
    assert result["rows"][1]["total_score"] is None
    assert result["leader"] is None


def test_weekend_and_large_order_cap_do_not_mean_product_suspension():
    spec = adapter.manifest()
    assert spec["operating_scope"]["calendar_closed"] is True
    assert spec["claims"]["subscription_open"]["value"] is True
    assert spec["claims"]["redemption_open"]["value"] is True
    assert spec["operating_scope"]["agency_channel_daily_cap_cny"] == "100000"
    assert spec["operating_scope"]["account_available_amount"] is None
    assert spec["benchmark_method_review"]["historical_coverage_status"] == "UNVERIFIED"
    assert "benchmark_return_basis" not in spec["claims"]
