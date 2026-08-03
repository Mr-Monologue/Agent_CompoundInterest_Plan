from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerService
from investor_core.strategy import StrategyService
from investor_core.workspace import WorkspaceService


def fixed_now() -> datetime:
    return datetime(2026, 8, 4, 2, 0, tzinfo=UTC)


def create_context(database_path: Path) -> tuple[Settings, str, str]:
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    ledger = LedgerService(settings, now=fixed_now)
    portfolio = ledger.create_portfolio(name="V1 测试组合")
    account = ledger.create_account(
        portfolio_id=str(portfolio["id"]),
        name="默认账户",
        platform="测试平台",
    )
    return settings, str(portfolio["id"]), str(account["id"])


def test_daily_workspace_is_read_only_and_exposes_exact_narrative(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    settings, portfolio_id, account_id = create_context(database_path)
    with sqlite3.connect(database_path) as connection:
        before_audits = int(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
        before_runtime = int(
            connection.execute("SELECT COUNT(*) FROM runtime_mode_snapshots").fetchone()[0]
        )

    result = WorkspaceService(settings, now=fixed_now).get(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=date(2026, 8, 4),
        view="DAILY",
    )

    assert result["contract_version"] == "investment-workspace-v1"
    assert result["view"] == "DAILY"
    assert result["state"] == "SETUP_REQUIRED"
    assert result["v1_readiness"]["status"] == "BLOCKED"
    assert result["next_actions"][0]["code"] == "STRATEGY_INSTANCE_REQUIRED"
    assert result["next_actions"][0]["suggested_read_tool"] == "strategy_current_get"
    assert result["narrative_contract"] == {
        "mode": "EXACT_TEXT",
        "response_field": "display_text",
        "additions_allowed": False,
    }
    assert result["display_text"].startswith("Hermes 投资工作台")
    assert "不是基金排名、投资建议或交易执行" in result["display_text"]
    assert result["automatic_trade"] is False
    assert result["financial_state_changed"] is False
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (
            before_audits,
        )
        assert connection.execute("SELECT COUNT(*) FROM runtime_mode_snapshots").fetchone() == (
            before_runtime,
        )


def test_readiness_reports_strategy_and_v1_operations_literally(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    settings, portfolio_id, account_id = create_context(database_path)
    ledger = LedgerService(settings, now=fixed_now)
    ledger.create_instrument(code="FUND001", name="测试基金")
    strategy = StrategyService(settings, now=fixed_now)
    strategy.assign(
        portfolio_id=portfolio_id,
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="V1 工作台测试",
    )
    strategy.configure_instrument(
        portfolio_id=portfolio_id,
        instrument_code="FUND001",
        role="CORE",
        contribution_eligible=True,
        target_weight_bps=10000,
        priority=1,
        minimum_amount_minor=100,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="显式批准测试标的",
    )
    ledger.create_instrument(code="FUND002", name="测试卫星基金")
    strategy.configure_instrument(
        portfolio_id=portfolio_id,
        instrument_code="FUND002",
        role="SATELLITE",
        contribution_eligible=True,
        target_weight_bps=10000,
        priority=1,
        minimum_amount_minor=100,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="显式批准卫星测试标的",
    )

    result = WorkspaceService(settings, now=fixed_now).get(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=date(2026, 8, 4),
        view="READINESS",
    )
    checks = {item["code"]: item for item in result["v1_readiness"]["checks"]}

    assert checks["INVESTMENT_CONTEXT"]["status"] == "PASS"
    assert checks["STRATEGY_INSTANCE"]["status"] == "PASS"
    assert checks["STRATEGY_INSTANCE"]["facts"]["eligible_instrument_count"] == 2
    assert checks["STRATEGY_INSTANCE"]["facts"]["eligible_by_role"] == {
        "CORE": 1,
        "SATELLITE": 1,
    }
    assert checks["STRATEGY_INSTANCE"]["facts"]["missing_roles"] == []
    assert checks["AUTOMATION_SCHEDULER"]["status"] == "NOT_CONFIGURED"
    assert checks["NOTIFICATION_DELIVERY"]["status"] == "NOT_TESTED"
    assert checks["VERIFIED_BACKUP"]["status"] == "NOT_TESTED"
    assert checks["FOURTEEN_DAY_OPERATION"]["status"] == "NOT_ESTABLISHED"
    assert checks["RESEARCH_CONNECTOR"]["required_for_v1"] is False
    assert result["display_text"].startswith("Value DCA V1 就绪度")
    assert "只评价产品运行条件" in result["display_text"]


def test_readiness_requires_allowlist_coverage_for_each_target_role(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    settings, portfolio_id, account_id = create_context(database_path)
    ledger = LedgerService(settings, now=fixed_now)
    ledger.create_instrument(code="CORE001", name="核心基金")
    ledger.create_instrument(code="SAT001", name="卫星候选基金")
    strategy = StrategyService(settings, now=fixed_now)
    strategy.assign(
        portfolio_id=portfolio_id,
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="测试按目标舱位检查准入覆盖",
    )
    strategy.configure_instrument(
        portfolio_id=portfolio_id,
        instrument_code="CORE001",
        role="CORE",
        contribution_eligible=True,
        target_weight_bps=10000,
        priority=1,
        minimum_amount_minor=100,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="核心舱允许定投",
    )
    strategy.configure_instrument(
        portfolio_id=portfolio_id,
        instrument_code="SAT001",
        role="SATELLITE",
        contribution_eligible=False,
        target_weight_bps=None,
        priority=1,
        minimum_amount_minor=100,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="卫星舱等待信号门控",
    )

    result = WorkspaceService(settings, now=fixed_now).get(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=date(2026, 8, 4),
        view="READINESS",
    )
    check = next(
        item for item in result["v1_readiness"]["checks"] if item["code"] == "STRATEGY_INSTANCE"
    )

    assert check["status"] == "NOT_CONFIGURED"
    assert check["reason_code"] == "CONTRIBUTION_ROLE_ALLOWLIST_INCOMPLETE"
    assert check["facts"] == {
        "active": True,
        "eligible_instrument_count": 1,
        "eligible_by_role": {"CORE": 1, "SATELLITE": 0},
        "required_roles": ["CORE", "SATELLITE"],
        "missing_roles": ["SATELLITE"],
        "target_pct_by_role": {"CORE": "65.00", "SATELLITE": "35.00"},
    }
    assert result["next_actions"][0]["code"] == "CONTRIBUTION_ROLE_ALLOWLIST_INCOMPLETE"
    assert result["next_actions"][0]["facts"]["missing_roles"] == ["SATELLITE"]


def test_workspace_api_preserves_valuation_quality_and_boundaries(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    settings, portfolio_id, account_id = create_context(database_path)
    client = TestClient(create_app(settings))

    response = client.get(
        "/v1/investment-workspace",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "view": "FULL",
            "as_of_date": "2026-08-04",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"]["view"] == "FULL"
    assert payload["data"]["automatic_trade"] is False
    assert payload["meta"]["data_quality"] == payload["data"]["valuation_summary"][
        "data_quality"
    ]
