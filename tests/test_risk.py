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
    assert scan["evaluation_summary"]["evaluated_rule_count"] > 0
    assert scan["evaluation_summary"]["triggered_rule_count"] > 0
    assert scan["evaluation_summary"]["sell_proposal_count"] > 0
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


def test_scan_distinguishes_unconfigured_rules_and_defaults_to_compact_output(
    tmp_path: Path,
) -> None:
    _ledger, risk, portfolio_id, account_id = configured_risk_services(
        tmp_path / "investor.db"
    )
    strategy = StrategyService(
        Settings(environment=Environment.TEST, db_path=tmp_path / "investor.db")
    )
    strategy.configure_instrument(
        portfolio_id=portfolio_id,
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
        hard_stop_return_bps=None,
        maximum_position_weight_bps=None,
        lifecycle_rules={},
        redemption_policy={},
        exposure_profile={},
        fund_destination=None,
        approved_by="test-user",
        reason="remove optional rules to verify honest scan semantics",
    )

    compact = risk.scan(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date="2026-07-21",
    )

    assert compact["state"] == "PARTIAL"
    assert compact["reason_code"] == "RISK_SCAN_PARTIAL"
    assert compact["rule_contract_version"] == "risk-rules-v2"
    assert compact["evaluation_summary"] == {
        "candidate_rule_count": 10,
        "configured_rule_count": 2,
        "evaluable_rule_count": 2,
        "evaluated_rule_count": 2,
        "not_configured_count": 7,
        "data_unavailable_count": 0,
        "not_applicable_count": 1,
        "exempt_count": 0,
        "triggered_rule_count": 0,
        "sell_proposal_count": 0,
    }
    assert compact["rule_hits_included"] is False
    assert compact["rule_hits"] == []
    assert compact["instrument_summaries"][0]["assessment"] == "PARTIAL"
    assert compact["execution_status"] == "SUCCESS"
    assert compact["trade_execution_status"] == "NOT_EXECUTED"

    detailed = risk.scan(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date="2026-07-21",
        include_rule_hits=True,
    )
    assert len(detailed["rule_hits"]) == 10
    assert {item["status"] for item in detailed["rule_hits"]} == {
        "EVALUATED_NOT_HIT",
        "NOT_CONFIGURED",
        "NOT_APPLICABLE",
    }
    page = risk.list_rule_hits(
        portfolio_id=portfolio_id,
        status="NOT_CONFIGURED",
        limit=3,
        offset=0,
    )
    assert page["page"]["total_count"] == 7
    assert page["page"]["returned_count"] == 3
    assert page["page"]["has_more"] is True
    assert all(item["status"] == "NOT_CONFIGURED" for item in page["items"])
    assert all("inputs" not in item for item in page["items"])


def test_portfolio_brief_reports_strategy_rule_configuration(
    tmp_path: Path,
) -> None:
    _ledger, _risk, portfolio_id, account_id = configured_risk_services(
        tmp_path / "investor.db"
    )
    market = MarketDataService(
        Settings(environment=Environment.TEST, db_path=tmp_path / "investor.db")
    )

    brief = market.portfolio_brief(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date_value="2026-07-21",
    )

    assessment = brief["valuation"]["positions"][0]["policy_assessment"]
    assert assessment["risk"] == "PARTIAL"
    assert assessment["sell_rule"] == "PARTIALLY_CONFIGURED"
    assert assessment["configured_rule_count"] == 3
    assert assessment["configured_rule_codes"] == [
        "THESIS_REVIEW_REQUIRED",
        "SELL_02_THESIS_INVALID",
        "SELL_01_HARD_STOP",
    ]


def test_verified_lifecycle_evidence_and_linked_sell_complete_the_lifecycle(
    tmp_path: Path,
) -> None:
    ledger, risk, portfolio_id, account_id = configured_risk_services(tmp_path / "investor.db")
    settings = Settings(environment=Environment.TEST, db_path=tmp_path / "investor.db")
    strategy = StrategyService(settings)
    strategy.configure_instrument(
        portfolio_id=portfolio_id,
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
        lifecycle_rules={
            "replacement_min_score_delta_bps": 500,
            "replacement_min_consecutive_periods": 2,
        },
        redemption_policy={"fee_bps": 50},
        exposure_profile={"industry": {"TEST": 10000}},
        fund_destination="CASH_BUFFER",
        approved_by="test-user",
        reason="approve deterministic sell lifecycle test",
    )
    risk.record_lifecycle_observation(
        instrument_code="FUND001",
        observation_type="REPLACEMENT_CANDIDATE",
        observation_date="2026-07-21",
        facts={
            "candidate_code": "FUND002",
            "score_delta_bps": 800,
            "consecutive_periods": 3,
        },
        source_type="PROFESSIONAL",
        source_name="test research",
        source_ref="test://replacement",
        verification_status="VERIFIED",
        observed_at="2026-07-21T16:00:00+08:00",
    )

    scan = risk.scan(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date="2026-07-21",
    )
    proposal = next(
        item for item in scan["sell_proposals"] if item["trigger_code"] == "SELL_04_REPLACE"
    )
    assert proposal["recommended_action"] == "FULL_SELL"
    assert proposal["recommended_amount"] == "100.00"
    assert proposal["diagnostic"]["checklist"]["fees_estimated"] is True
    assert proposal["diagnostic"]["checklist"]["fund_destination_configured"] is True

    decision = risk.create_decision_draft(
        proposal_id=str(proposal["id"]),
        decision="APPROVE",
        user_reason="approved for external execution",
    )
    risk.commit_decision(
        draft_id=str(decision["draft"]["id"]),
        confirmation_token=str(decision["confirmation_token"]),
        confirmed_by="test-user",
    )
    trade = ledger.create_transaction_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="FUND001",
        side="SELL",
        trade_date_value="2026-07-21",
        amount="100.00",
        nav="1.000000",
        shares="100.000000",
        platform="测试平台",
        idempotency_key="linked-sell",
        sell_proposal_id=str(proposal["id"]),
    )
    committed = ledger.commit_transaction_draft(
        draft_id=str(trade["draft"]["id"]),
        confirmation_token=str(trade["confirmation_token"]),
        confirmed_by="test-user",
    )

    assert committed["sell_execution_link"]["sell_proposal_id"] == proposal["id"]
    assert committed["sell_followup"]["status"] == "PENDING"
    holdings = ledger.list_holdings(portfolio_id=portfolio_id, account_id=account_id)
    assert holdings[0]["total_shares"] == "0.000000"
    executed = risk.get_proposal(proposal_id=str(proposal["id"]))
    assert executed["status"] == "EXECUTED"
    assert executed["execution_status"] == "EXECUTED"
