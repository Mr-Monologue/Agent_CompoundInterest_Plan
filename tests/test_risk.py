from __future__ import annotations

from pathlib import Path

from conftest import migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerService
from investor_core.market_data import MarketDataService
from investor_core.risk import RiskService
from investor_core.strategy import StrategyService


def configured_risk_services(
    database_path: Path,
) -> tuple[LedgerService, RiskService, str, str]:
    migrate_database(database_path)
    settings = Settings(
        environment=Environment.TEST,
        db_path=database_path,
        market_nav_max_age_days=7,
    )
    ledger = LedgerService(settings)
    portfolio = ledger.create_portfolio(name="风险测试组合")
    account = ledger.create_account(
        portfolio_id=str(portfolio["id"]),
        name="测试账户",
        platform="测试平台",
    )
    ledger.create_instrument(code="INDEX001", name="通用测试指数", asset_type="INDEX")
    ledger.create_instrument(code="FUND001", name="通用测试基金")
    opening = ledger.create_opening_position_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="FUND001",
        as_of_date_value="2026-07-20",
        total_shares="100.000000",
        average_cost_nav="2.000000",
        platform="测试平台",
        idempotency_key="opening-risk-fund",
    )
    ledger.commit_opening_position_draft(
        draft_id=str(opening["draft"]["id"]),
        confirmation_token=str(opening["confirmation_token"]),
        confirmed_by="test-user",
    )
    MarketDataService(settings).record_nav_snapshot(
        instrument_code="FUND001",
        nav_date_value="2026-07-21",
        nav="1.000000",
        currency="CNY",
        source_type="PLATFORM",
        source_name="测试平台",
        source_ref="test://fund001",
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
        reason="测试风险规则",
    )
    strategy.configure_instrument(
        portfolio_id=str(portfolio["id"]),
        instrument_code="FUND001",
        role="SATELLITE",
        contribution_eligible=False,
        target_weight_bps=None,
        priority=100,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code="INDEX001",
        proxy_suitability="WEAK",
        thesis_status="ACTIVE",
        hard_stop_return_bps=-2500,
        maximum_position_weight_bps=None,
        approved_by="test-user",
        reason="用户明确批准测试阈值和弱代理",
    )
    return ledger, RiskService(settings), str(portfolio["id"]), str(account["id"])


def test_percentile_direction_and_weak_proxy_boundary(tmp_path: Path) -> None:
    _ledger, risk, portfolio_id, _account_id = configured_risk_services(tmp_path / "investor.db")
    for day, value in (("2026-07-01", "10"), ("2026-07-02", "20"), ("2026-07-03", "30")):
        risk.record_valuation_observation(
            instrument_code="INDEX001",
            metric="PE",
            observation_date=day,
            value=value,
            source_type="OFFICIAL",
            source_name="测试官方源",
            source_ref=f"test://{day}",
            verification_status="VERIFIED",
            observed_at=f"{day}T16:00:00+08:00",
        )

    snapshot = risk.valuation_snapshot(
        portfolio_id=portfolio_id,
        instrument_code="FUND001",
        metric="PE",
        as_of_date="2026-07-03",
    )

    assert snapshot["percentile"] == "100.00"
    assert snapshot["valuation_state"] == "OVERPRICED"
    assert snapshot["proxy_suitability"] == "WEAK"
    assert snapshot["sell_trigger_allowed"] is False


def test_sell_rule_creates_proposal_but_approval_never_changes_holding(
    tmp_path: Path,
) -> None:
    ledger, risk, portfolio_id, account_id = configured_risk_services(tmp_path / "investor.db")
    before = ledger.list_holdings(portfolio_id=portfolio_id, account_id=account_id)

    scan = risk.scan(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date="2026-07-21",
    )

    assert scan["state"] == "REVIEW_REQUIRED"
    proposal = scan["sell_proposals"][0]
    assert proposal["trigger_code"] == "SELL_01_HARD_STOP"
    assert proposal["status"] == "REVIEW_REQUIRED"
    assert proposal["execution_status"] == "NOT_EXECUTED"

    draft = risk.create_decision_draft(
        proposal_id=str(proposal["id"]),
        decision="APPROVE",
        user_reason="同意建议书, 尚未在平台成交",
    )
    committed = risk.commit_decision(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )

    assert committed["proposal"]["status"] == "APPROVED"
    assert committed["execution_status"] == "NOT_EXECUTED"
    assert committed["transaction_created"] is False
    assert ledger.list_holdings(portfolio_id=portfolio_id, account_id=account_id) == before
