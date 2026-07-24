from __future__ import annotations

from pathlib import Path

import pytest
from conftest import migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerError, LedgerService
from investor_core.market_data import MarketDataService
from investor_core.planning import PlanningService
from investor_core.strategy import StrategyService


def configured_services(
    database_path: Path,
) -> tuple[LedgerService, PlanningService, str, str]:
    migrate_database(database_path)
    settings = Settings(
        environment=Environment.TEST,
        db_path=database_path,
        market_nav_max_age_days=7,
    )
    ledger = LedgerService(settings)
    portfolio = ledger.create_portfolio(name="测试组合")
    account = ledger.create_account(
        portfolio_id=str(portfolio["id"]),
        name="测试账户",
        platform="测试平台",
    )
    for code, name, role, shares in (
        ("CORE01", "核心候选", "CORE", "100.000000"),
        ("SAT01", "卫星持仓", "SATELLITE", "100.000000"),
    ):
        ledger.create_instrument(code=code, name=name, role=role)
        opening = ledger.create_opening_position_draft(
            portfolio_id=str(portfolio["id"]),
            account_id=str(account["id"]),
            instrument_code=code,
            as_of_date_value="2026-07-20",
            total_shares=shares,
            average_cost_nav="1.000000",
            platform="测试平台",
            idempotency_key=f"opening-{code}",
        )
        ledger.commit_opening_position_draft(
            draft_id=str(opening["draft"]["id"]),
            confirmation_token=str(opening["confirmation_token"]),
            confirmed_by="test-user",
        )
    market = MarketDataService(settings)
    for code, nav in (("CORE01", "0.100000"), ("SAT01", "0.900000")):
        market.record_nav_snapshot(
            instrument_code=code,
            nav_date_value="2026-07-21",
            nav=nav,
            currency="CNY",
            source_type="PLATFORM",
            source_name="测试平台",
            source_ref=f"test://{code}",
            source_lineage="ALIPAY",
            verification_status="VERIFIED",
            observed_at_value="2026-07-21T22:00:00+08:00",
            actor_ref="test-user",
        )
    strategy = StrategyService(settings)
    strategy.assign(
        portfolio_id=str(portfolio["id"]),
        strategy_key="value-dca",
        strategy_version="1.6",
        instance_config={},
        approved_by="test-user",
        reason="测试计划",
    )
    strategy.configure_instrument(
        portfolio_id=str(portfolio["id"]),
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
        reason="明确批准核心候选",
    )
    return (
        ledger,
        PlanningService(settings),
        str(portfolio["id"]),
        str(account["id"]),
    )


def test_plan_lifecycle_is_separate_from_transactions(tmp_path: Path) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")

    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-21",
        idempotency_key="weekly-2026-07-21",
        as_of_date_value="2026-07-21",
    )

    assert created["plan"]["status"] == "DRAFT"
    assert created["plan"]["items"][0]["instrument_code"] == "CORE01"
    assert created["plan"]["items"][0]["candidate_amount"] == "100.00"
    assert (
        ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id)[0]["kind"]
        == "OPENING"
    )
    assert all(
        item["kind"] == "OPENING"
        for item in ledger.list_transactions(
            portfolio_id=portfolio_id,
            account_id=account_id,
        )
    )

    with pytest.raises(LedgerError) as mismatch:
        planning.freeze(
            plan_id=str(created["plan"]["id"]),
            confirmation_token="wrong-token",
            confirmed_by="test-user",
        )
    assert mismatch.value.code == "CONFIRMATION_MISMATCH"

    frozen = planning.freeze(
        plan_id=str(created["plan"]["id"]),
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    assert frozen["status"] == "FROZEN"

    trade = ledger.create_transaction_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        side="BUY",
        trade_date_value="2026-07-21",
        amount="100.00",
        nav="0.100000",
        shares="1000.000000",
        platform="测试平台",
        idempotency_key="buy-core01-2026-07-21",
    )
    committed = ledger.commit_transaction_draft(
        draft_id=str(trade["draft"]["id"]),
        confirmation_token=str(trade["confirmation_token"]),
        confirmed_by="test-user",
    )
    executed = planning.mark_executed(
        plan_id=str(created["plan"]["id"]),
        transaction_ids=[str(committed["transaction"]["id"])],
        confirmed_by="test-user",
    )
    assert executed["status"] == "EXECUTED"


def test_plan_reserves_role_amount_when_no_instrument_is_approved(
    tmp_path: Path,
) -> None:
    _ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    settings = planning.settings
    StrategyService(settings).configure_instrument(
        portfolio_id=portfolio_id,
        instrument_code="CORE01",
        role="CORE",
        contribution_eligible=False,
        target_weight_bps=None,
        priority=1,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="撤销新增资金资格",
    )

    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-21",
        idempotency_key="weekly-reserved",
        as_of_date_value="2026-07-21",
    )

    item = created["plan"]["items"][0]
    assert item["instrument_code"] is None
    assert item["action"] == "REVIEW_REQUIRED"
    assert item["reason_code"] == "NO_ELIGIBLE_INSTRUMENT"
    assert item["reserved_amount"] == "100.00"
    assert item["candidate_amount"] == "0.00"
    summary = created["plan"]["revision"]["summary"]
    assert summary["state"] == "REVIEW_REQUIRED"
    assert summary["reason_code"] == "NO_EXECUTABLE_INSTRUMENT"
    assert summary["plan"]["projected"]["CORE"]["actual_pct"] == "10.00"
    assert summary["plan"]["projected"]["SATELLITE"]["actual_pct"] == "90.00"

    with pytest.raises(LedgerError) as blocked:
        planning.freeze(
            plan_id=str(created["plan"]["id"]),
            confirmation_token=str(created["confirmation_token"]),
            confirmed_by="test-user",
        )
    assert blocked.value.code == "PLAN_NOT_EXECUTABLE"


def test_plan_idempotency_does_not_reissue_confirmation_token(tmp_path: Path) -> None:
    _ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    arguments = {
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "contribution_amount": "100.00",
        "plan_date_value": "2026-07-21",
        "idempotency_key": "weekly-idempotent",
        "as_of_date_value": "2026-07-21",
    }

    first = planning.create_draft(**arguments)
    second = planning.create_draft(**arguments)

    assert first["reused"] is False
    assert first["confirmation_token"]
    assert second["reused"] is True
    assert second["confirmation_token"] is None
    assert second["plan"]["id"] == first["plan"]["id"]
