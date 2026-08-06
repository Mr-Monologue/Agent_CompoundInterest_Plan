from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerService
from investor_core.market_data import MarketDataService
from investor_core.planning import PlanningService
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


def test_weekly_workspace_uses_fixed_window_and_does_not_write(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    settings, portfolio_id, account_id = create_context(database_path)
    ledger = LedgerService(settings, now=fixed_now)
    ledger.create_instrument(code="FUND001", name="周报测试基金")
    for trade_date, key in (
        ("2026-07-28", "outside-week"),
        ("2026-08-02", "inside-week"),
    ):
        draft = ledger.create_transaction_draft(
            portfolio_id=portfolio_id,
            account_id=account_id,
            instrument_code="FUND001",
            side="BUY",
            trade_date_value=trade_date,
            amount="10.00",
            nav="1.000000",
            shares="10.000000",
            platform="测试平台",
            idempotency_key=key,
        )
        ledger.commit_transaction_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token=str(draft["confirmation_token"]),
            confirmed_by="test-user",
        )
    with sqlite3.connect(database_path) as connection:
        before = {
            "audits": int(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]),
            "runtime": int(
                connection.execute("SELECT COUNT(*) FROM runtime_mode_snapshots").fetchone()[0]
            ),
            "transactions": int(
                connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            ),
        }

    result = WorkspaceService(settings, now=fixed_now).get(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=date(2026, 8, 4),
        view="WEEKLY",
    )

    assert result["view"] == "WEEKLY"
    assert result["weekly_summary"]["period_start"] == "2026-07-29"
    assert result["weekly_summary"]["period_end"] == "2026-08-04"
    assert result["weekly_summary"]["counts"] == {
        "transaction_record_count": 1,
        "cash_event_count": 0,
        "plan_count": 0,
        "periodic_review_count": 0,
        "report_bundle_count": 0,
    }
    assert result["weekly_summary"]["transactions"] == [
        {"kind": "TRADE", "side": "BUY", "count": 1, "amount": "10.00"}
    ]
    assert result["display_text"].startswith("Hermes 投资周报")
    assert "统计区间: 2026-07-29 至 2026-08-04" in result["display_text"]
    assert "不生成基金排名、投资建议、计划、交易或持仓变更" in result["display_text"]
    assert result["automatic_trade"] is False
    assert result["financial_state_changed"] is False
    with sqlite3.connect(database_path) as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
            == before["audits"]
        )
        assert (
            int(connection.execute("SELECT COUNT(*) FROM runtime_mode_snapshots").fetchone()[0])
            == before["runtime"]
        )
        assert (
            int(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])
            == before["transactions"]
        )


def test_daily_and_weekly_reports_show_partial_plan_progress(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    settings, portfolio_id, account_id = create_context(database_path)
    ledger = LedgerService(settings, now=fixed_now)
    for code, name, role in (
        ("CORE01", "核心基金", "CORE"),
        ("SAT01", "卫星基金", "SATELLITE"),
    ):
        ledger.create_instrument(code=code, name=name, role=role)
        opening = ledger.create_opening_position_draft(
            portfolio_id=portfolio_id,
            account_id=account_id,
            instrument_code=code,
            as_of_date_value="2026-08-03",
            total_shares="100.000000",
            average_cost_nav="1.000000",
            platform="测试平台",
            idempotency_key=f"workspace-opening-{code}",
        )
        ledger.commit_opening_position_draft(
            draft_id=str(opening["draft"]["id"]),
            confirmation_token=str(opening["confirmation_token"]),
            confirmed_by="test-user",
        )
    market = MarketDataService(settings, now=fixed_now)
    for code, nav in (("CORE01", "0.100000"), ("SAT01", "0.900000")):
        market.record_nav_snapshot(
            instrument_code=code,
            nav_date_value="2026-08-04",
            nav=nav,
            currency="CNY",
            source_type="PLATFORM",
            source_name="测试平台",
            source_ref=f"test://{code}",
            source_lineage="ALIPAY",
            verification_status="VERIFIED",
            observed_at_value="2026-08-04T10:00:00+08:00",
            actor_ref="test-user",
        )
    strategy = StrategyService(settings, now=fixed_now)
    strategy.assign(
        portfolio_id=portfolio_id,
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="测试周计划进度",
    )
    strategy.configure_instrument(
        portfolio_id=portfolio_id,
        instrument_code="CORE01",
        role="CORE",
        contribution_eligible=True,
        target_weight_bps=10000,
        priority=1,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="测试周计划进度",
    )
    planning = PlanningService(settings, now=fixed_now)
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-08-04",
        idempotency_key="workspace-partial-plan",
        as_of_date_value="2026-08-04",
    )
    plan_id = str(created["plan"]["id"])
    planning.freeze(
        plan_id=plan_id,
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    trade = ledger.create_transaction_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        side="BUY",
        trade_date_value="2026-08-04",
        amount="40.00",
        nav="1.000000",
        shares="40.000000",
        platform="测试平台",
        idempotency_key="workspace-partial-buy",
    )
    committed = ledger.commit_transaction_draft(
        draft_id=str(trade["draft"]["id"]),
        confirmation_token=str(trade["confirmation_token"]),
        confirmed_by="test-user",
    )
    planning.link_transaction(
        plan_id=plan_id,
        transaction_id=str(committed["transaction"]["id"]),
        confirmed_by="test-user",
    )

    service = WorkspaceService(settings, now=fixed_now)
    daily = service.get(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=date(2026, 8, 4),
        view="DAILY",
    )
    progress = daily["workflows"]["plan_execution_progress"]
    assert progress["planned_amount"] == "100.00"
    assert progress["executed_amount"] == "40.00"
    assert progress["remaining_amount"] == "60.00"
    assert daily["workflows"]["plan_counts"]["PARTIALLY_EXECUTED"] == 1
    lifecycle = next(
        item
        for item in daily["v1_readiness"]["checks"]
        if item["code"] == "WEEKLY_PLAN_LIFECYCLE"
    )
    assert lifecycle["status"] == "NOT_TESTED"
    assert "已成交 ¥40.00 / 计划 ¥100.00 / 剩余 ¥60.00" in daily["display_text"]

    weekly = service.get(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=date(2026, 8, 4),
        view="WEEKLY",
    )
    assert weekly["weekly_summary"]["plan_execution_progress"] == progress
    assert "已成交 ¥40.00 / 计划 ¥100.00 / 剩余 ¥60.00" in weekly["display_text"]


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


def test_workspace_api_exposes_weekly_view(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    settings, portfolio_id, account_id = create_context(database_path)
    client = TestClient(create_app(settings))

    response = client.get(
        "/v1/investment-workspace",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "view": "WEEKLY",
            "as_of_date": "2026-08-04",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["view"] == "WEEKLY"
    assert payload["data"]["weekly_summary"]["period_start"] == "2026-07-29"
    assert payload["data"]["weekly_summary"]["period_end"] == "2026-08-04"
    assert payload["data"]["financial_state_changed"] is False
