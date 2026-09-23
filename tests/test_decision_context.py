from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
from test_daily_client import dump
from test_weekly_reports import terminal_plan

from investor_core.api.app import create_app
from investor_core.decision_context import execution_context, research_context
from investor_core.market_data import _instrument_plan_items


def test_strategy_maximum_is_not_a_daily_limit_and_money_is_unchanged():
    preview = {
        "data_quality": "WARNING",
        "display_text": "可执行金额: 40.00",
        "plan": {
            "instrument_items": [
                {"instrument_code": "TEST", "instrument_name": "测试", "candidate_amount": "40.00"}
            ]
        },
    }
    assignment = {
        "strategy": {"version": "1.6"},
        "instruments": [
            {
                "instrument_code": "TEST",
                "minimum_amount_minor": 1,
                "maximum_amount_minor": 1000,
                "approved_at": "2026-01-01",
            }
        ],
    }
    before = deepcopy(preview)
    result = execution_context(preview, assignment)
    assert preview == before and result["plan"] == before["plan"]
    a = result["execution_assessment"]
    assert a["status"] == "EXECUTION_CONSTRAINT_UNKNOWN"
    assert a["items"][0]["maximum_daily_amount_minor"] is None
    assert a["items"][0]["minimum_trading_days"] is None
    assert a["schedule"] == [] and not a["executable_schedule_available"]
    assert not result["version_axes"]["candidate_strategy_activated"]
    assert "可执行金额" not in result["display_text"]
    assert "WARNING" in result["display_text"]


def test_thesis_flag_and_evidence_count_do_not_fabricate_research():
    brief = {"as_of_date": "2026-01-01", "valuation": {"positions": []}}
    assignment = {
        "strategy": {"version": "1.6"},
        "instruments": [
            {
                "instrument_code": "MED",
                "instrument_name": "医疗",
                "strategy_role": "SATELLITE",
                "contribution_eligible": False,
                "thesis_status": "ACTIVE",
            }
        ],
    }
    result = research_context("医疗", brief, assignment, {"MED": [{"claim": "rally"}] * 100})
    d = result["evidence"][0]
    assert d["why_hold"] is None and d["return_driver"] == "UNKNOWN"
    assert d["evidence_maturity"] == "UNVERIFIED" and d["history_may_be_truncated"]
    assert d["relative_diagnosis"] == "DATA_BLOCKED"
    assert not d["decision_frame"]["persisted_decision_journal"]
    assert not result["money_action"] and result["probabilities"] is None
    assert result["causal_claim"] == "NOT_ESTABLISHED"


def test_existing_no_eligible_satellite_reserves_instead_of_rerouting():
    result = _instrument_plan_items(
        assignment={"instruments": []},
        role_allocations={"CORE": "0.00", "SATELLITE": "70.00"},
        data_quality="WARNING",
    )
    assert result[0]["strategy_role"] == "SATELLITE"
    assert result[0]["reserved_amount"] == "70.00"
    assert result[0]["candidate_amount"] == "0.00"


def test_http_research_and_execution_context_are_read_only(tmp_path: Path):
    settings, pid, aid, _ = terminal_plan(tmp_path / "isolated.db", outcome="SKIPPED")
    client = TestClient(create_app(settings))
    before = dump(settings.db_path)
    result = client.get(
        "/v1/weekly-plan-preview",
        params={
            "portfolio_id": pid,
            "account_id": aid,
            "contribution_amount": "200",
            "as_of_date": "2026-07-21",
        },
    )
    assert result.status_code == 200
    assert result.json()["data"]["execution_assessment"]["allocation_only"]
    r = client.get("/v1/research-diagnosis", params={"portfolio_id": pid, "account_id": aid})
    assert r.status_code == 200 and not r.json()["data"]["money_action"]
    assert (
        client.get(
            "/v1/research-diagnosis", params={"portfolio_id": pid, "account_id": aid, "topic": ""}
        ).status_code
        == 422
    )
    assert dump(settings.db_path) == before


def test_saved_official_mandate_is_reused_without_strategy_promotion():
    assignment = {
        "strategy": {"version": "1.6"},
        "instruments": [
            {
                "instrument_code": "MED",
                "instrument_name": "医疗",
                "strategy_role": "SATELLITE",
                "contribution_eligible": False,
                "thesis_status": "ACTIVE",
            }
        ],
    }
    record = {
        "source_name": "Annual report",
        "source_ref": "https://example.com/report",
        "facts": {
            "kind": "EXECUTION_SOURCE_V1",
            "quality": "OFFICIAL",
            "data_date": "2025-12-31",
            "published_date": "2026-03-31",
            "retrieved_at": "2026-09-23T00:00:00Z",
            "facts": {"mandate_summary": "医疗健康股票为主"},
        },
    }
    result = research_context(
        "医疗",
        {"as_of_date": "2026-09-23", "valuation": {"positions": []}},
        assignment,
        {"MED": [record]},
    )
    dossier = result["evidence"][0]
    assert "FUND_MANDATE" not in dossier["missing"]
    assert dossier["documented_mandates"][0]["data_date"] == "2025-12-31"
    assert dossier["why_hold"] is None and not result["money_action"]
    assert "医疗健康股票为主" in result["display_text"]
