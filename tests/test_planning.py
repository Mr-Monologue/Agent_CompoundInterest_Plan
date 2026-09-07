from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import JsonDict, LedgerError, LedgerService
from investor_core.market_data import MarketDataService
from investor_core.planning import PlanningService
from investor_core.strategy import StrategyService
from investor_core.weekly_reports import WeeklyReportService


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
    strategy.configure_instrument(
        portfolio_id=str(portfolio["id"]),
        instrument_code="SAT01",
        role="SATELLITE",
        contribution_eligible=False,
        target_weight_bps=None,
        priority=2,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="explicit satellite strategy role for the fixture",
    )
    return (
        ledger,
        PlanningService(settings),
        str(portfolio["id"]),
        str(account["id"]),
    )


def commit_buy(
    ledger: LedgerService,
    *,
    portfolio_id: str,
    account_id: str,
    instrument_code: str,
    trade_date: str,
    amount: str,
    key: str,
) -> JsonDict:
    draft = ledger.create_transaction_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code=instrument_code,
        side="BUY",
        trade_date_value=trade_date,
        amount=amount,
        nav="1.000000",
        shares=amount,
        platform="测试平台",
        idempotency_key=key,
    )
    return ledger.commit_transaction_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
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
    assert executed["execution_progress"]["executed_amount"] == "100.00"
    assert executed["execution_progress"]["remaining_amount"] == "0.00"


def test_frozen_plan_can_be_skipped_with_fresh_short_lived_confirmation(
    tmp_path: Path,
) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-21",
        idempotency_key="weekly-lost-original-token",
        as_of_date_value="2026-07-21",
    )
    plan_id = str(created["plan"]["id"])

    with pytest.raises(LedgerError) as not_frozen:
        planning.create_skip_draft(
            plan_id=plan_id,
            reason="尚未冻结的计划不能走重新确认关闭",
        )
    assert not_frozen.value.code == "PLAN_SKIP_RECONFIRMATION_NOT_ALLOWED"

    planning.freeze(
        plan_id=plan_id,
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    transaction_count = len(
        ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id)
    )
    original_expiry = datetime.fromisoformat(
        str(created["plan"]["expires_at"]).replace("Z", "+00:00")
    )
    later = original_expiry + timedelta(days=1)
    reconfirming = PlanningService(planning.settings, now=lambda: later)
    frozen = reconfirming.get(plan_id=plan_id)
    assert frozen["status"] == "FROZEN"
    assert frozen["state_contract"] == {
        "status": "FROZEN",
        "terminal": False,
        "action_required": True,
        "blocks_future_plans": True,
        "plan_fact_expired": False,
        "original_confirmation_credential": "EXPIRED",
        "skip_requires_new_confirmation": True,
        "reason_code": "FROZEN_PLAN_REMAINS_ACTIVE_UNTIL_EXECUTED_OR_SKIPPED",
    }

    close = reconfirming.create_skip_draft(
        plan_id=plan_id,
        reason="用户明确关闭旧冻结计划",
    )
    assert close["draft"]["status"] == "PENDING"
    assert close["plan"]["status"] == "FROZEN"
    assert reconfirming.get_skip_draft(draft_id=str(close["draft"]["id"]))["status"] == "PENDING"

    with pytest.raises(LedgerError) as mismatch:
        reconfirming.commit_skip_draft(
            draft_id=str(close["draft"]["id"]),
            confirmation_token="wrong-token",
            confirmed_by="test-user",
        )
    assert mismatch.value.code == "CONFIRMATION_MISMATCH"
    assert reconfirming.get(plan_id=plan_id)["status"] == "FROZEN"

    committed = reconfirming.commit_skip_draft(
        draft_id=str(close["draft"]["id"]),
        confirmation_token=str(close["confirmation_token"]),
        confirmed_by="test-user",
    )
    assert committed["draft"]["status"] == "COMMITTED"
    assert committed["plan"]["status"] == "SKIPPED"
    assert committed["plan"]["state_contract"]["terminal"] is True
    assert len(ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id)) == (
        transaction_count
    )


def test_weekly_plan_skip_reconfirmation_expires_without_closing_plan(
    tmp_path: Path,
) -> None:
    _ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    clock = datetime(2026, 8, 11, 1, 0, tzinfo=UTC)
    service = PlanningService(planning.settings, now=lambda: clock)
    created = service.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-08-11",
        idempotency_key="weekly-close-expiry",
        as_of_date_value="2026-07-21",
    )
    plan_id = str(created["plan"]["id"])
    service.freeze(
        plan_id=plan_id,
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    close = service.create_skip_draft(plan_id=plan_id, reason="关闭旧计划")
    clock += timedelta(hours=1)

    with pytest.raises(LedgerError) as expired:
        service.commit_skip_draft(
            draft_id=str(close["draft"]["id"]),
            confirmation_token=str(close["confirmation_token"]),
            confirmed_by="test-user",
        )
    assert expired.value.code == "CONFIRMATION_EXPIRED"
    assert service.get_skip_draft(draft_id=str(close["draft"]["id"]))["status"] == "EXPIRED"
    assert service.get(plan_id=plan_id)["status"] == "FROZEN"


def test_no_investment_week_records_truth_without_allocated_plan(
    tmp_path: Path,
) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    clock = datetime(2026, 9, 4, 2, 0, tzinfo=UTC)
    service = PlanningService(planning.settings, now=lambda: clock)
    expired_overlap = service.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="200.00",
        plan_date_value="2026-09-04",
        idempotency_key="unconfirmed-2026-09-04",
        as_of_date_value="2026-07-21",
    )
    clock = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)

    preview = service.preview_no_investment_week(
        portfolio_id=portfolio_id,
        account_id=account_id,
        period_start_value="2026-08-31",
        weekly_budget="200.00",
        reason_code="USER_CHOSE_NO_INVESTMENT",
        note="本周主动暂停投入，预算不结转。",
    )
    assert preview["eligible"] is True
    assert preview["executed_amount"] == "0.00"
    assert preview["abandoned_amount"] == "200.00"
    assert service.get(plan_id=str(expired_overlap["plan"]["id"]))["status"] == "EXPIRED"

    draft = service.create_no_investment_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        period_start_value="2026-08-31",
        weekly_budget="200.00",
        reason_code="USER_CHOSE_NO_INVESTMENT",
        note="本周主动暂停投入，预算不结转。",
        idempotency_key="no-investment:2026-08-31",
    )
    assert draft["draft"]["status"] == "PENDING"
    assert draft["draft"]["financial_facts_created"] is False
    committed = service.commit_no_investment_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )
    plan = committed["plan"]
    assert plan["status"] == "SKIPPED"
    assert plan["decision_kind"] == "NO_INVESTMENT"
    assert plan["period_start"] == "2026-08-31"
    assert plan["period_end"] == "2026-09-06"
    assert plan["items"] == []
    assert plan["execution_progress"]["planned_amount"] == "200.00"
    assert plan["execution_progress"]["executed_amount"] == "0.00"
    assert plan["execution_progress"]["abandoned_amount"] == "200.00"
    assert plan["execution_progress"]["unsubmitted_amount"] == "0.00"
    assert plan["execution_progress"]["carry_forward"] is False
    assert committed["financial_facts_created"] is False
    assert all(
        item["kind"] == "OPENING"
        for item in ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id)
    )

    report = WeeklyReportService(planning.settings, now=lambda: clock).preview(
        plan_id=str(plan["id"])
    )
    assert report["final_eligible"] is True
    assert report["decision_kind"] == "NO_INVESTMENT"
    assert report["planned_amount"] == "200.00"
    assert report["executed_amount"] == "0.00"
    assert report["abandoned_amount"] == "200.00"
    assert report["items"] == []
    assert report["no_investment_decision"] == {
        "weekly_budget": "200.00",
        "executed_amount": "0.00",
        "abandoned_amount": "200.00",
        "carry_forward": False,
        "reason_code": "USER_CHOSE_NO_INVESTMENT",
        "note": "本周主动暂停投入，预算不结转。",
    }


def test_no_investment_week_rejects_actual_buy_and_renews_expired_draft(
    tmp_path: Path,
) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    clock = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)
    service = PlanningService(planning.settings, now=lambda: clock)
    draft = service.create_no_investment_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        period_start_value="2026-08-31",
        weekly_budget="200.00",
        reason_code="USER_CHOSE_NO_INVESTMENT",
        note="本周不投入。",
        idempotency_key="no-investment-renew",
    )
    old_token = str(draft["confirmation_token"])
    clock += timedelta(hours=1)
    renewed = service.renew_no_investment_draft(
        draft_id=str(draft["draft"]["id"]), actor_ref="test"
    )
    assert renewed["draft"]["status"] == "PENDING"
    assert renewed["draft"]["renewal_count"] == 1
    with pytest.raises(LedgerError) as old_credential:
        service.commit_no_investment_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token=old_token,
            confirmed_by="test-user",
        )
    assert old_credential.value.code == "CONFIRMATION_TOKEN_INVALID"

    commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-09-02",
        amount="10.00",
        key="actual-buy-in-no-investment-week",
    )
    with pytest.raises(LedgerError) as changed:
        service.commit_no_investment_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token=str(renewed["confirmation_token"]),
            confirmed_by="test-user",
        )
    assert changed.value.code == "NO_INVESTMENT_WEEK_FACTS_CHANGED"


def test_plan_accumulates_multiple_buy_records_across_trade_dates(tmp_path: Path) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-21",
        idempotency_key="weekly-partial",
        as_of_date_value="2026-07-21",
    )
    plan_id = str(created["plan"]["id"])
    planning.freeze(
        plan_id=plan_id,
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    first = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-07-21",
        amount="40.00",
        key="partial-first",
    )
    first_id = str(first["transaction"]["id"])
    partial = planning.link_transaction(
        plan_id=plan_id,
        transaction_id=first_id,
        confirmed_by="test-user",
    )
    assert partial["status"] == "PARTIALLY_EXECUTED"
    assert partial["execution_progress"]["executed_amount"] == "40.00"
    assert partial["execution_progress"]["remaining_amount"] == "60.00"
    assert partial["execution_progress"]["fee_treatment"] == (
        "CONFIRMED_PRINCIPAL_PLUS_FEE_COUNTS_TOWARD_PLAN"
    )
    assert partial["execution_progress"]["items"][0] == {
        "instrument_id": partial["items"][0]["instrument_id"],
        "instrument_code": "CORE01",
        "instrument_name": partial["items"][0]["instrument_name"],
        "planned_amount": "100.00",
        "executed_amount": "40.00",
        "remaining_amount": "60.00",
        "in_flight_amount": "0.00",
        "unsubmitted_amount": "60.00",
        "cancelled_or_refunded_amount": "0.00",
        "excess_amount": "0.00",
        "complete": False,
    }

    with pytest.raises(LedgerError) as duplicate:
        planning.link_transaction(
            plan_id=plan_id,
            transaction_id=first_id,
            confirmed_by="test-user",
        )
    assert duplicate.value.code == "PLAN_TRANSACTION_ALREADY_LINKED"

    second = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-07-22",
        amount="60.00",
        key="partial-second",
    )
    completed = planning.link_transaction(
        plan_id=plan_id,
        transaction_id=str(second["transaction"]["id"]),
        confirmed_by="test-user",
    )
    assert completed["status"] == "EXECUTED"
    assert completed["execution_progress"]["linked_transaction_count"] == 2
    assert completed["execution_progress"]["executed_amount"] == "100.00"
    assert completed["execution_progress"]["remaining_amount"] == "0.00"


def test_partial_plan_closure_preserves_200_170_30_and_releases_commitment(
    tmp_path: Path,
) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-20",
        idempotency_key="weekly-partial-close",
        as_of_date_value="2026-07-21",
    )
    plan_id = str(created["plan"]["id"])
    planning.freeze(
        plan_id=plan_id,
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    with sqlite3.connect(planning.settings.db_path) as connection:
        connection.execute(
            "UPDATE investment_plans SET contribution_amount_minor=20000 WHERE id=?",
            (plan_id,),
        )
        connection.execute(
            """
            UPDATE plan_items SET base_amount_minor=20000, candidate_amount_minor=20000
            WHERE plan_revision_id=(
                SELECT id FROM plan_revisions WHERE plan_id=? AND revision=1
            ) AND action='CONTRIBUTE'
            """,
            (plan_id,),
        )
    trade = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-07-20",
        amount="170.00",
        key="partial-close-buy",
    )
    partial = planning.link_transaction(
        plan_id=plan_id,
        transaction_id=str(trade["transaction"]["id"]),
        confirmed_by="test-user",
    )
    assert partial["status"] == "PARTIALLY_EXECUTED"

    before_transaction_count = len(
        ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id)
    )
    draft = planning.create_partial_close_draft(
        plan_id=plan_id,
        closure_business_date="2026-07-26",
        closure_reason_code="PLATFORM_LIMIT_REMAINDER_ABANDONED",
        closure_note="周期结束; 平台限额导致剩余30元不再执行, 且不结转。",
        carry_forward=False,
        idempotency_key="partial-close-governed",
    )
    assert draft["draft"]["planned_amount"] == "200.00"
    assert draft["draft"]["executed_amount"] == "170.00"
    assert draft["draft"]["abandoned_amount"] == "30.00"
    assert planning.get(plan_id=plan_id)["status"] == "PARTIALLY_EXECUTED"

    def commit_close() -> JsonDict:
        return planning.commit_partial_close_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token=str(draft["confirmation_token"]),
            confirmed_by="test-user",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        close_results = list(pool.map(lambda _index: commit_close(), range(2)))
    assert sorted(result["idempotent_replay"] for result in close_results) == [False, True]
    closed = next(result for result in close_results if not result["idempotent_replay"])
    assert closed["plan"]["status"] == "PARTIALLY_EXECUTED_CLOSED"
    assert closed["plan"]["state_contract"]["blocks_future_plans"] is False
    assert closed["plan"]["execution_progress"]["planned_amount"] == "200.00"
    assert closed["plan"]["execution_progress"]["executed_amount"] == "170.00"
    assert closed["plan"]["execution_progress"]["remaining_amount"] == "30.00"
    assert closed["plan"]["execution_progress"]["abandoned_amount"] == "30.00"
    assert closed["plan"]["execution_progress"]["execution_rate_pct"] == "85.00"
    assert closed["plan"]["closure"]["carry_forward"] is False
    assert closed["financial_facts_created"] is False
    assert len(ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id)) == (
        before_transaction_count
    )

    preview = MarketDataService(planning.settings).weekly_plan_preview(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="200.00",
        as_of_date_value="2026-07-21",
    )
    assert preview["available"] is True
    assert preview["plan"]["requested_contribution_amount"] == "200.00"
    assert preview["plan"]["available_contribution_amount"] == "200.00"
    assert preview["plan"]["prior_commitments"]["outstanding_amount"] == "0.00"


def test_partial_close_expiry_renews_same_draft_and_invalidates_old_token(
    tmp_path: Path,
) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    clock = datetime(2026, 8, 24, 1, 0, tzinfo=UTC)
    service = PlanningService(planning.settings, now=lambda: clock)
    created = service.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-08-17",
        idempotency_key="renewable-partial-plan",
        as_of_date_value="2026-07-21",
    )
    plan_id = str(created["plan"]["id"])
    service.freeze(
        plan_id=plan_id,
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    trade = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-08-18",
        amount="40.00",
        key="renewable-partial-buy",
    )
    service.link_transaction(
        plan_id=plan_id,
        transaction_id=str(trade["transaction"]["id"]),
        confirmed_by="test-user",
    )
    with sqlite3.connect(planning.settings.db_path) as connection:
        connection.execute(
            """
            INSERT INTO external_subscription_drafts (
                id, action, subscription_id, payload_json, payload_hash, status,
                idempotency_key, confirmation_digest, expires_at, created_at,
                committed_at, committed_entity_id, actor_ref
            ) VALUES (
                'active-submit-draft', 'SUBMIT', NULL, ?, 'payload-hash', 'PENDING',
                'active-submit-idempotency', 'digest', ?, ?, NULL, NULL, 'hermes'
            )
            """,
            (
                f'{{"weekly_plan_id":"{plan_id}"}}',
                (clock + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                clock.isoformat().replace("+00:00", "Z"),
            ),
        )
    with pytest.raises(LedgerError) as active_draft:
        service.create_partial_close_draft(
            plan_id=plan_id,
            closure_business_date="2026-08-23",
            closure_reason_code="PERIOD_ENDED_REMAINDER_ABANDONED",
            closure_note="周期结束, 剩余金额不再执行。",
            carry_forward=False,
            idempotency_key="blocked-partial-close",
        )
    assert active_draft.value.code == "PLAN_PARTIAL_CLOSE_HAS_ACTIVE_FACTS"
    with sqlite3.connect(planning.settings.db_path) as connection:
        connection.execute(
            "UPDATE external_subscription_drafts SET status='EXPIRED' "
            "WHERE id='active-submit-draft'"
        )
    close = service.create_partial_close_draft(
        plan_id=plan_id,
        closure_business_date="2026-08-23",
        closure_reason_code="PERIOD_ENDED_REMAINDER_ABANDONED",
        closure_note="周期结束, 剩余金额不再执行。",
        carry_forward=False,
        idempotency_key="renewable-partial-close",
    )
    old_token = str(close["confirmation_token"])
    clock += timedelta(hours=1)
    assert (
        service.get_partial_close_draft(draft_id=str(close["draft"]["id"]))["status"] == "EXPIRED"
    )

    renewed = service.renew_partial_close_draft(
        draft_id=str(close["draft"]["id"]), actor_ref="hermes"
    )
    assert renewed["draft"]["id"] == close["draft"]["id"]
    assert renewed["draft"]["renewal_count"] == 1
    with pytest.raises(LedgerError) as old_rejected:
        service.commit_partial_close_draft(
            draft_id=str(close["draft"]["id"]),
            confirmation_token=old_token,
            confirmed_by="test-user",
        )
    assert old_rejected.value.code == "CONFIRMATION_MISMATCH"
    committed = service.commit_partial_close_draft(
        draft_id=str(close["draft"]["id"]),
        confirmation_token=str(renewed["confirmation_token"]),
        confirmed_by="test-user",
    )
    assert committed["plan"]["status"] == "PARTIALLY_EXECUTED_CLOSED"


def test_plan_requires_every_fund_and_exact_amount_before_execution(tmp_path: Path) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    strategy = StrategyService(planning.settings)
    ledger.create_instrument(code="CORE02", name="第二只核心基金", role="CORE")
    strategy.configure_instrument(
        portfolio_id=portfolio_id,
        instrument_code="CORE01",
        role="CORE",
        contribution_eligible=True,
        target_weight_bps=5000,
        priority=1,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="测试多基金计划",
    )
    strategy.configure_instrument(
        portfolio_id=portfolio_id,
        instrument_code="CORE02",
        role="CORE",
        contribution_eligible=True,
        target_weight_bps=5000,
        priority=2,
        minimum_amount_minor=1,
        maximum_amount_minor=None,
        benchmark_code=None,
        thesis_status="ACTIVE",
        approved_by="test-user",
        reason="测试多基金计划",
    )
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-21",
        idempotency_key="multi-fund-plan",
        as_of_date_value="2026-07-21",
    )
    planned = {
        str(item["instrument_code"]): str(item["candidate_amount"])
        for item in created["plan"]["items"]
        if item["instrument_code"] is not None and item["candidate_amount"] != "0.00"
    }
    assert set(planned) == {"CORE01", "CORE02"}
    assert sum(Decimal(amount) for amount in planned.values()) == Decimal("100.00")
    plan_id = str(created["plan"]["id"])
    planning.freeze(
        plan_id=plan_id,
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    first = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-07-21",
        amount=planned["CORE01"],
        key="multi-fund-core",
    )
    partial = planning.link_transaction(
        plan_id=plan_id,
        transaction_id=str(first["transaction"]["id"]),
        confirmed_by="test-user",
    )
    assert partial["status"] == "PARTIALLY_EXECUTED"
    assert {
        item["instrument_code"]: item["remaining_amount"]
        for item in partial["execution_progress"]["items"]
    } == {"CORE01": "0.00", "CORE02": planned["CORE02"]}

    second = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE02",
        trade_date="2026-07-23",
        amount=planned["CORE02"],
        key="multi-fund-satellite",
    )
    complete = planning.link_transaction(
        plan_id=plan_id,
        transaction_id=str(second["transaction"]["id"]),
        confirmed_by="test-user",
    )
    assert complete["status"] == "EXECUTED"
    assert complete["execution_progress"]["complete"] is True


def test_plan_rejects_overage_wrong_instrument_and_cross_plan_reuse(tmp_path: Path) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")

    def frozen_plan(key: str, plan_date: str) -> str:
        created = planning.create_draft(
            portfolio_id=portfolio_id,
            account_id=account_id,
            contribution_amount="100.00",
            plan_date_value=plan_date,
            idempotency_key=key,
            as_of_date_value="2026-07-21",
        )
        plan_id = str(created["plan"]["id"])
        planning.freeze(
            plan_id=plan_id,
            confirmation_token=str(created["confirmation_token"]),
            confirmed_by="test-user",
        )
        return plan_id

    first_plan = frozen_plan("validation-plan-1", "2026-07-21")
    second_plan = frozen_plan("validation-plan-2", "2026-07-22")
    sixty = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-07-21",
        amount="60.00",
        key="validation-sixty",
    )
    sixty_id = str(sixty["transaction"]["id"])
    planning.link_transaction(
        plan_id=first_plan,
        transaction_id=sixty_id,
        confirmed_by="test-user",
    )
    with pytest.raises(LedgerError) as reused:
        planning.link_transaction(
            plan_id=second_plan,
            transaction_id=sixty_id,
            confirmed_by="test-user",
        )
    assert reused.value.code == "TRANSACTION_USED_BY_ANOTHER_PLAN"

    fifty = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-07-22",
        amount="50.00",
        key="validation-fifty",
    )
    with pytest.raises(LedgerError) as overage:
        planning.link_transaction(
            plan_id=first_plan,
            transaction_id=str(fifty["transaction"]["id"]),
            confirmed_by="test-user",
        )
    assert overage.value.code == "PLAN_EXECUTION_AMOUNT_EXCEEDED"
    assert planning.get(plan_id=first_plan)["execution_progress"]["executed_amount"] == "60.00"

    satellite = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="SAT01",
        trade_date="2026-07-22",
        amount="10.00",
        key="validation-satellite",
    )
    with pytest.raises(LedgerError) as wrong_fund:
        planning.link_transaction(
            plan_id=first_plan,
            transaction_id=str(satellite["transaction"]["id"]),
            confirmed_by="test-user",
        )
    assert wrong_fund.value.code == "PLAN_INSTRUMENT_MISMATCH"

    other_account = ledger.create_account(
        portfolio_id=portfolio_id,
        name="其他账户",
        platform="测试平台",
    )
    wrong_account = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=str(other_account["id"]),
        instrument_code="CORE01",
        trade_date="2026-07-22",
        amount="10.00",
        key="validation-wrong-account",
    )
    with pytest.raises(LedgerError) as account_mismatch:
        planning.link_transaction(
            plan_id=first_plan,
            transaction_id=str(wrong_account["transaction"]["id"]),
            confirmed_by="test-user",
        )
    assert account_mismatch.value.code == "PLAN_TRANSACTION_ACCOUNT_MISMATCH"

    sell_draft = ledger.create_transaction_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        side="SELL",
        trade_date_value="2026-07-22",
        amount="10.00",
        nav="1.000000",
        shares="10.00",
        platform="测试平台",
        idempotency_key="validation-sell",
    )
    sell = ledger.commit_transaction_draft(
        draft_id=str(sell_draft["draft"]["id"]),
        confirmation_token=str(sell_draft["confirmation_token"]),
        confirmed_by="test-user",
    )
    with pytest.raises(LedgerError) as not_buy:
        planning.link_transaction(
            plan_id=first_plan,
            transaction_id=str(sell["transaction"]["id"]),
            confirmed_by="test-user",
        )
    assert not_buy.value.code == "PLAN_TRANSACTION_NOT_BUY"


def test_reversing_linked_buy_reopens_execution_progress(tmp_path: Path) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-21",
        idempotency_key="reversal-plan",
        as_of_date_value="2026-07-21",
    )
    plan_id = str(created["plan"]["id"])
    planning.freeze(
        plan_id=plan_id,
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    buy = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-07-21",
        amount="100.00",
        key="reversal-buy",
    )
    buy_id = str(buy["transaction"]["id"])
    assert (
        planning.link_transaction(
            plan_id=plan_id,
            transaction_id=buy_id,
            confirmed_by="test-user",
        )["status"]
        == "EXECUTED"
    )

    reversal = ledger.create_reversal_draft(
        transaction_id=buy_id,
        idempotency_key="reverse-linked-buy",
    )
    ledger.commit_transaction_draft(
        draft_id=str(reversal["draft"]["id"]),
        confirmation_token=str(reversal["confirmation_token"]),
        confirmed_by="test-user",
    )
    reopened = planning.get(plan_id=plan_id)
    assert reopened["status"] == "PARTIALLY_EXECUTED"
    assert reopened["execution_progress"]["executed_amount"] == "0.00"
    assert reopened["execution_progress"]["remaining_amount"] == "100.00"
    assert reopened["execution_progress"]["reversed_transaction_count"] == 1

    replacement = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-07-22",
        amount="100.00",
        key="replacement-buy",
    )
    replaced = planning.link_transaction(
        plan_id=plan_id,
        transaction_id=str(replacement["transaction"]["id"]),
        confirmed_by="test-user",
    )
    assert replaced["status"] == "EXECUTED"
    assert replaced["execution_progress"]["valid_transaction_count"] == 1
    assert replaced["execution_progress"]["reversed_transaction_count"] == 1


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
