from copy import deepcopy
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_benchmarks import input_for, setup_mapping
from test_daily_client import dump

from investor_core.api.app import create_app
from investor_core.benchmarks import DiagnosticInput, MappingApproval, MappingDraft
from investor_core.research_summary import (
    DRAWDOWN_LABEL,
    notification_status,
    portfolio_summary,
    summarize_fund,
)


def test_approved_version_overrides_run_snapshot_and_new_candidate(tmp_path):
    settings, pid, aid, _, eid, service, payload = setup_mapping(tmp_path)
    mapping = service.create(MappingDraft.model_validate(payload))
    service.run(DiagnosticInput.model_validate(input_for(mapping, eid)), persist=True)
    service.approve(
        mapping["id"],
        MappingApproval(confirmation_token=mapping["confirmation_token"], confirmed_by="test"),
    )
    revised = deepcopy(payload)
    revised.update(expected_previous_version=1, idempotency_key="new-candidate")
    service.create(MappingDraft.model_validate(revised))
    before = dump(settings.db_path)
    web = TestClient(create_app(settings))
    response = web.get(
        "/v1/portfolio-research-summary", params={"portfolio_id": pid, "account_id": aid}
    )
    assert response.status_code == 200
    item = summarize_fund(service.read(pid, "CORE01"), today=date(2026, 9, 24))
    assert item["mapping_approved"] and item["newer_candidate_pending"]
    assert item["mapping_version"] == 1 and len(item["windows"]) == 1
    assert item["observed_through"] == "2026-09-22"
    assert item["observation_age_days"] == 2 and eid in item["evidence_ids"]
    assert not item["money_action"]
    assert web.get("/v1/notification-status").status_code == 200
    assert dump(settings.db_path) == before


def test_report_only_loss_is_not_drawdown_and_missing_is_not_underperformance():
    record = dict(
        versions=[dict(id="m", status="DRAFT", version=1, limitations=["FX unknown"])], runs=[]
    )
    assert summarize_fund(record, today=date(2026, 9, 24))["classification"] == "DATA_INSUFFICIENT"
    record["runs"] = [
        dict(
            id="r",
            mapping_id="m",
            gaps=["NO_DAILY_SERIES"],
            warnings=[],
            reported_windows=[
                dict(
                    start="2025-12-31",
                    end="2026-06-30",
                    fund_return_pct=-10,
                    benchmark_return_pct=-5,
                    excess_percentage_points=-5,
                )
            ],
        )
    ]
    item = summarize_fund(record, today=date(2026, 9, 24))
    assert item["classification"] == "REVIEW"
    assert item["windows"][0]["fund_max_drawdown_pct"] is None
    assert item["windows"][0]["basis"] == "ISSUER_REPORTED_ONLY"
    assert item["drawdown_label"] == DRAWDOWN_LABEL and item["data_quality"] == "WARNING"


def test_published_composite_path_is_explicit_and_not_component_reconstruction(tmp_path):
    _, pid, _, _, eid, service, payload = setup_mapping(tmp_path)
    period = deepcopy(payload["diagnostic_mapping"][0])
    period.update(effective_to=None, method="PUBLISHED_PATH")
    period["components"] = [
        dict(period["components"][0], weight_bps=10000, return_basis="PUBLISHED_INDEX")
    ]
    payload["diagnostic_mapping"] = [period]
    mapping = service.create(MappingDraft.model_validate(payload))
    request = input_for(mapping, eid)
    request["benchmarks"] = [dict(request["benchmarks"][0], return_basis="PUBLISHED_INDEX")]
    result = service.run(DiagnosticInput.model_validate(request), persist=False)
    assert result["calculated"]["benchmark_return_pct"] == pytest.approx(-1)
    assert "ISSUER_COMPOSITE_NOT_COMPONENT_RECONSTRUCTION" in result["warnings"]
    assert "SINGLE_SOURCE_WARNING" in result["warnings"]
    assert DRAWDOWN_LABEL in result["display_text"]
    bad = deepcopy(payload)
    bad["diagnostic_mapping"][0]["components"][0]["return_basis"] = "PRICE"
    with pytest.raises(ValidationError):
        MappingDraft.model_validate(bad)
    assert service.read(pid, "CORE01")["approved_research_version"] is None


def test_notification_flags_never_claim_recipient_receipt():
    outbox = [dict(status="DELIVERED", last_error_code=None)] * 500
    attempts = [
        dict(claimed_at="2026-09-24T01:00:00Z", evidence=dict(evidence_level="PROVIDER_ACCEPTED"))
    ]
    result = notification_status(outbox, attempts)
    assert result["recipient_delivery_verified"] is None
    assert result["sends_performed"] == 0 and result["history_may_be_truncated"]


def test_closed_holding_is_not_current_review_and_missing_dates_are_explicit(tmp_path):
    result = portfolio_summary(
        [{"holding": {"instrument_code": "CLOSED", "total_shares": "0"}}],
        {},
        today=date(2026, 9, 24),
    )
    assert result["items"] == []
    _, _, _, _, eid, service, payload = setup_mapping(tmp_path)
    mapping = service.create(MappingDraft.model_validate(payload))
    request = input_for(mapping, eid)
    request["benchmarks"][0]["points"].pop(1)
    run = service.run(DiagnosticInput.model_validate(request), persist=False)
    assert run["calculated"] is None
    assert run["missing_observations"] == {"issuer:EQ": ["2026-09-21"]}
