from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_planning import configured_services

from investor_core.ledger import LedgerError
from investor_core.market_data import MarketDataService
from investor_core.strategy import StrategyService
from investor_core.subscriptions import SubscriptionService
from investor_core.workspace import WorkspaceService


def frozen_plan(database_path: Path, *, amount: str = "100.00"):
    ledger, planning, portfolio_id, account_id = configured_services(database_path)
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount=amount,
        plan_date_value="2026-07-21",
        idempotency_key=f"subscription-plan-{amount}",
        as_of_date_value="2026-07-21",
    )
    plan = planning.freeze(
        plan_id=str(created["plan"]["id"]),
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    return (
        ledger,
        planning,
        SubscriptionService(planning.settings, now=lambda: datetime(2026, 8, 7, tzinfo=UTC)),
        portfolio_id,
        account_id,
        plan,
    )


def submit(
    service: SubscriptionService,
    *,
    portfolio_id: str,
    account_id: str,
    plan_id: str,
    amount: str = "100.00",
    key: str = "subscription-submit",
    expected_date: str | None = None,
):
    draft = service.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=plan_id,
        instrument_code="CORE01",
        requested_amount=amount,
        submitted_at="2026-07-21T10:00:00+08:00",
        submitted_business_date="2026-07-21",
        external_platform="测试平台",
        expected_confirmation_date=expected_date,
        idempotency_key=key,
    )
    return service.commit_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )["subscription"]


def confirm(
    service: SubscriptionService,
    *,
    subscription_id: str,
    amount: str,
    shares: str,
    fee: str = "0.00",
    refund: str = "0.00",
    key: str,
    day: str = "2026-07-22",
):
    draft = service.create_confirmation_draft(
        subscription_id=subscription_id,
        confirmed_at=f"{day}T18:00:00+08:00",
        confirmation_business_date=day,
        nav_date=day,
        nav="1.000000",
        confirmed_shares=shares,
        confirmed_amount=amount,
        fee=fee,
        refunded_amount=refund,
        idempotency_key=key,
    )
    result = service.commit_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )
    return result["subscription"]


def test_external_subscription_is_reserved_until_explicit_ledger_posting(
    tmp_path: Path,
) -> None:
    ledger, planning, service, portfolio_id, account_id, plan = frozen_plan(
        tmp_path / "investor.db"
    )
    baseline = len(
        ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id)
    )
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    assert subscription["status"] == "SUBMITTED"
    assert subscription["in_flight_amount"] == "100.00"
    assert (
        len(ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id))
        == baseline
    )
    progress = planning.get(plan_id=str(plan["id"]))["execution_progress"]
    assert progress["executed_amount"] == "0.00"
    assert progress["in_flight_amount"] == "100.00"
    assert progress["unsubmitted_amount"] == "0.00"

    pending_draft = service.create_status_draft(
        subscription_id=str(subscription["id"]),
        target_status="PENDING_CONFIRMATION",
        reason="平台处理中",
        idempotency_key="subscription-pending",
    )
    pending = service.commit_draft(
        draft_id=str(pending_draft["draft"]["id"]),
        confirmation_token=str(pending_draft["confirmation_token"]),
        confirmed_by="test-user",
    )["subscription"]
    assert pending["status"] == "PENDING_CONFIRMATION"

    partial = confirm(
        service,
        subscription_id=str(subscription["id"]),
        amount="40.00",
        shares="40.000000",
        fee="1.00",
        key="subscription-confirm-1",
    )
    assert partial["status"] == "PARTIALLY_CONFIRMED"
    assert partial["pending_external_amount"] == "59.00"
    assert partial["confirmed_unbooked_amount"] == "41.00"
    assert planning.get(plan_id=str(plan["id"]))["status"] == "FROZEN"

    confirmation_id = str(partial["confirmations"][0]["id"])
    transaction_draft = service.create_transaction_draft(
        confirmation_id=confirmation_id,
        idempotency_key="subscription-ledger-1",
    )
    assert transaction_draft["business_effect"] == "DRAFT_ONLY_NO_HOLDING_CHANGE"
    assert planning.get(plan_id=str(plan["id"]))["execution_progress"]["executed_amount"] == "0.00"
    posted = service.commit_transaction_draft(
        confirmation_id=confirmation_id,
        draft_id=str(transaction_draft["draft"]["id"]),
        confirmation_token=str(transaction_draft["confirmation_token"]),
        confirmed_by="test-user",
    )
    assert posted["trade_executed_by_system"] is False
    assert posted["weekly_plan"]["status"] == "PARTIALLY_EXECUTED"
    assert posted["weekly_plan"]["execution_progress"]["executed_amount"] == "41.00"
    assert posted["weekly_plan"]["execution_progress"]["in_flight_amount"] == "59.00"


def test_multi_day_confirmations_complete_one_plan_with_fee_semantics(tmp_path: Path) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    first = confirm(
        service,
        subscription_id=str(subscription["id"]),
        amount="40.00",
        shares="40.000000",
        fee="1.00",
        key="multi-confirm-1",
    )
    second = confirm(
        service,
        subscription_id=str(subscription["id"]),
        amount="58.00",
        shares="58.000000",
        fee="1.00",
        key="multi-confirm-2",
        day="2026-07-29",
    )
    assert second["status"] == "CONFIRMED"
    assert second["pending_external_amount"] == "0.00"
    for index, confirmation in enumerate(first["confirmations"] + second["confirmations"][1:], 1):
        drafted = service.create_transaction_draft(
            confirmation_id=str(confirmation["id"]),
            idempotency_key=f"multi-ledger-{index}",
        )
        result = service.commit_transaction_draft(
            confirmation_id=str(confirmation["id"]),
            draft_id=str(drafted["draft"]["id"]),
            confirmation_token=str(drafted["confirmation_token"]),
            confirmed_by="test-user",
        )
    assert result["weekly_plan"]["status"] == "EXECUTED"
    assert result["weekly_plan"]["execution_progress"]["executed_amount"] == "100.00"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"requested_amount": "0"}, "INVALID_AMOUNT"),
        ({"submitted_at": "2026-07-21T10:00:00"}, "TIMEZONE_REQUIRED"),
        ({"expected_confirmation_date": "2026-07-20"}, "INVALID_EXPECTED_CONFIRMATION_DATE"),
        ({"external_platform": ""}, "PLATFORM_REQUIRED"),
    ],
)
def test_submission_draft_rejects_invalid_external_facts(
    tmp_path: Path, mutation: dict[str, str], expected_code: str
) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    kwargs = {
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "weekly_plan_id": str(plan["id"]),
        "instrument_code": "CORE01",
        "requested_amount": "100.00",
        "submitted_at": "2026-07-21T10:00:00+08:00",
        "submitted_business_date": "2026-07-21",
        "external_platform": "测试平台",
        "idempotency_key": f"invalid-{expected_code}",
    }
    kwargs.update(mutation)
    with pytest.raises(LedgerError) as error:
        service.create_submission_draft(**kwargs)
    assert error.value.code == expected_code


def test_cancelled_remainder_is_audited_and_never_becomes_a_holding(tmp_path: Path) -> None:
    ledger, planning, service, portfolio_id, account_id, plan = frozen_plan(
        tmp_path / "investor.db"
    )
    baseline = ledger.list_holdings(portfolio_id=portfolio_id, account_id=account_id)
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    partial = confirm(
        service,
        subscription_id=str(subscription["id"]),
        amount="40.00",
        shares="40.000000",
        key="cancel-confirm",
    )
    cancelled_draft = service.create_status_draft(
        subscription_id=str(subscription["id"]),
        target_status="CANCELLED",
        reason="平台退回未确认部分",
        idempotency_key="cancel-remainder",
    )
    cancelled = service.commit_draft(
        draft_id=str(cancelled_draft["draft"]["id"]),
        confirmation_token=str(cancelled_draft["confirmation_token"]),
        confirmed_by="test-user",
    )["subscription"]
    assert partial["confirmed_unbooked_amount"] == "40.00"
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["cancelled_amount"] == "60.00"
    assert cancelled["in_flight_amount"] == "40.00"
    assert ledger.list_holdings(portfolio_id=portfolio_id, account_id=account_id) == baseline
    progress = planning.get(plan_id=str(plan["id"]))["execution_progress"]
    assert progress["cancelled_or_refunded_amount"] == "60.00"


def test_unposted_confirmation_can_be_reversed_but_posted_one_cannot(tmp_path: Path) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    confirmed = confirm(
        service,
        subscription_id=str(subscription["id"]),
        amount="40.00",
        shares="40.000000",
        key="reverse-confirm",
    )
    confirmation_id = str(confirmed["confirmations"][0]["id"])
    reversal = service.create_confirmation_reversal_draft(
        subscription_id=str(subscription["id"]),
        confirmation_id=confirmation_id,
        reason="平台更正确认结果",
        idempotency_key="reverse-confirmation",
    )
    reversed_result = service.commit_draft(
        draft_id=str(reversal["draft"]["id"]),
        confirmation_token=str(reversal["confirmation_token"]),
        confirmed_by="test-user",
    )["subscription"]
    assert reversed_result["confirmed_amount"] == "0.00"
    assert reversed_result["pending_external_amount"] == "100.00"


def test_idempotency_and_exact_confirmation_are_enforced(tmp_path: Path) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    draft = service.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=str(plan["id"]),
        instrument_code="CORE01",
        requested_amount="100.00",
        submitted_at="2026-07-21T10:00:00+08:00",
        submitted_business_date="2026-07-21",
        external_platform="测试平台",
        idempotency_key="idempotent-submit",
    )
    reused = service.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=str(plan["id"]),
        instrument_code="CORE01",
        requested_amount="100.00",
        submitted_at="2026-07-21T10:00:00+08:00",
        submitted_business_date="2026-07-21",
        external_platform="测试平台",
        idempotency_key="idempotent-submit",
    )
    assert reused["reused"] is True
    with pytest.raises(LedgerError) as wrong:
        service.commit_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token="wrong-token",
            confirmed_by="test-user",
        )
    assert wrong.value.code == "INVALID_CONFIRMATION_TOKEN"


def test_expected_date_only_marks_review_and_never_infers_failure(tmp_path: Path) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
        expected_date="2026-07-23",
    )
    assert subscription["status"] == "SUBMITTED"
    assert subscription["confirmation_overdue"] is True
    summary = service.summary(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=datetime(2026, 8, 7, tzinfo=UTC).date(),
    )
    assert summary["overdue_review_count"] == 1
    assert summary["automatic_failure_inference"] is False
    assert summary["cross_week_count"] == 1


def test_full_confirmation_with_distinct_nav_and_confirmation_dates(tmp_path: Path) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    draft = service.create_confirmation_draft(
        subscription_id=str(subscription["id"]),
        confirmed_at="2026-07-25T18:00:00+08:00",
        confirmation_business_date="2026-07-25",
        nav_date="2026-07-24",
        nav="2.000000",
        confirmed_shares="49.500000",
        confirmed_amount="99.00",
        fee="1.00",
        refunded_amount="0",
        idempotency_key="full-distinct-dates",
    )
    confirmed = service.commit_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )["subscription"]
    assert confirmed["status"] == "CONFIRMED"
    assert confirmed["confirmations"][0]["nav_date"] == "2026-07-24"
    assert confirmed["confirmations"][0]["confirmation_business_date"] == "2026-07-25"


def test_over_confirmation_and_illegal_status_rollback_are_rejected(tmp_path: Path) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    with pytest.raises(LedgerError) as excessive:
        confirm(
            service,
            subscription_id=str(subscription["id"]),
            amount="101.00",
            shares="101.000000",
            key="over-confirm",
        )
    assert excessive.value.code == "SUBSCRIPTION_CONFIRMATION_EXCEEDS_PENDING"

    pending = service.create_status_draft(
        subscription_id=str(subscription["id"]),
        target_status="PENDING_CONFIRMATION",
        reason="平台受理",
        idempotency_key="pending-once",
    )
    service.commit_draft(
        draft_id=str(pending["draft"]["id"]),
        confirmation_token=str(pending["confirmation_token"]),
        confirmed_by="test-user",
    )
    duplicate_pending = service.create_status_draft(
        subscription_id=str(subscription["id"]),
        target_status="PENDING_CONFIRMATION",
        reason="重复回退",
        idempotency_key="pending-twice",
    )
    with pytest.raises(LedgerError) as invalid:
        service.commit_draft(
            draft_id=str(duplicate_pending["draft"]["id"]),
            confirmation_token=str(duplicate_pending["confirmation_token"]),
            confirmed_by="test-user",
        )
    assert invalid.value.code == "INVALID_SUBSCRIPTION_TRANSITION"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("portfolio_id", "wrong-portfolio", "SUBSCRIPTION_CONTEXT_MISMATCH"),
        ("account_id", "wrong-account", "SUBSCRIPTION_CONTEXT_MISMATCH"),
        ("weekly_plan_id", "wrong-plan", "PLAN_NOT_OPEN_FOR_SUBMISSION"),
        ("instrument_code", "SAT01", "PLAN_INSTRUMENT_MISMATCH"),
    ],
)
def test_submission_rejects_cross_context_or_plan_instrument_links(
    tmp_path: Path, field: str, value: str, expected_code: str
) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    values = {
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "weekly_plan_id": str(plan["id"]),
        "instrument_code": "CORE01",
    }
    values[field] = value
    draft = service.create_submission_draft(
        **values,
        requested_amount="10.00",
        submitted_at="2026-07-21T10:00:00+08:00",
        submitted_business_date="2026-07-21",
        external_platform="测试平台",
        idempotency_key=f"context-{field}",
    )
    with pytest.raises(LedgerError) as error:
        service.commit_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token=str(draft["confirmation_token"]),
            confirmed_by="test-user",
        )
    assert error.value.code == expected_code


def test_multiple_submissions_reserve_one_fund_across_days(tmp_path: Path) -> None:
    _, planning, service, portfolio_id, account_id, plan = frozen_plan(
        tmp_path / "investor.db"
    )
    first = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
        amount="40.00",
        key="submission-day-1",
    )
    second_draft = service.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=str(plan["id"]),
        instrument_code="CORE01",
        requested_amount="60.00",
        submitted_at="2026-07-23T10:00:00+08:00",
        submitted_business_date="2026-07-23",
        external_platform="测试平台",
        idempotency_key="submission-day-3",
    )
    second = service.commit_draft(
        draft_id=str(second_draft["draft"]["id"]),
        confirmation_token=str(second_draft["confirmation_token"]),
        confirmed_by="test-user",
    )["subscription"]
    assert first["submitted_business_date"] == "2026-07-21"
    assert second["submitted_business_date"] == "2026-07-23"
    progress = planning.get(plan_id=str(plan["id"]))["execution_progress"]
    assert progress["in_flight_amount"] == "100.00"


def test_next_week_preview_suppresses_unfinished_frozen_plan(tmp_path: Path) -> None:
    _, planning, service, portfolio_id, account_id, plan = frozen_plan(
        tmp_path / "investor.db"
    )
    submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    market = MarketDataService(planning.settings)
    market.record_nav_snapshot(
        instrument_code="CORE01",
        nav_date_value="2026-07-28",
        nav="0.100000",
        currency="CNY",
        source_type="PLATFORM",
        source_name="测试平台",
        source_ref="test://CORE01-next-week",
        source_lineage="ALIPAY",
        verification_status="VERIFIED",
        observed_at_value="2026-07-28T22:00:00+08:00",
        actor_ref="test-user",
    )
    market.record_nav_snapshot(
        instrument_code="SAT01",
        nav_date_value="2026-07-28",
        nav="0.900000",
        currency="CNY",
        source_type="PLATFORM",
        source_name="测试平台",
        source_ref="test://SAT01-next-week",
        source_lineage="ALIPAY",
        verification_status="VERIFIED",
        observed_at_value="2026-07-28T22:00:00+08:00",
        actor_ref="test-user",
    )
    preview = market.weekly_plan_preview(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        as_of_date_value="2026-07-28",
    )
    assert preview["available"] is False
    assert preview["reason_code"] == "OUTSTANDING_PLAN_COMMITMENT"
    assert preview["prior_outstanding_amount"] == "100.00"
    assert preview["suppressed_amount"] == "100.00"


def test_workspace_daily_and_weekly_views_expose_subscription_progress(tmp_path: Path) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
        expected_date="2026-07-23",
    )
    confirm(
        service,
        subscription_id=str(subscription["id"]),
        amount="40.00",
        shares="40.000000",
        key="report-confirm",
    )
    workspace = WorkspaceService(
        service.settings, now=lambda: datetime(2026, 8, 7, tzinfo=UTC)
    ).get(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=datetime(2026, 8, 7, tzinfo=UTC).date(),
        view="FULL",
    )
    weekly_workspace = WorkspaceService(
        service.settings, now=lambda: datetime(2026, 8, 7, tzinfo=UTC)
    ).get(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=datetime(2026, 8, 7, tzinfo=UTC).date(),
        view="WEEKLY",
    )
    daily = workspace["workflows"]["external_subscription_progress"]
    weekly = weekly_workspace["weekly_summary"]["external_subscription_progress"]
    assert daily["in_flight_amount"] == "100.00"
    assert daily["confirmed_unbooked_amount"] == "40.00"
    assert daily["cross_week_count"] == 1
    assert weekly["in_flight_amount"] == "100.00"
    assert "场外申购进度" in workspace["display_text"]
    assert "场外申购进度" in weekly_workspace["display_text"]
    lifecycle = next(
        item
        for item in workspace["v1_readiness"]["checks"]
        if item["code"] == "WEEKLY_PLAN_LIFECYCLE"
    )
    assert lifecycle["status"] == "IN_PROGRESS"


def test_duplicate_official_transaction_posting_is_rejected(tmp_path: Path) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    confirmed = confirm(
        service,
        subscription_id=str(subscription["id"]),
        amount="100.00",
        shares="100.000000",
        key="duplicate-post-confirm",
    )
    confirmation_id = str(confirmed["confirmations"][0]["id"])
    drafted = service.create_transaction_draft(
        confirmation_id=confirmation_id,
        idempotency_key="duplicate-post-ledger",
    )
    service.commit_transaction_draft(
        confirmation_id=confirmation_id,
        draft_id=str(drafted["draft"]["id"]),
        confirmation_token=str(drafted["confirmation_token"]),
        confirmed_by="test-user",
    )
    with pytest.raises(LedgerError) as duplicate:
        service.create_transaction_draft(
            confirmation_id=confirmation_id,
            idempotency_key="duplicate-post-ledger-2",
        )
    assert duplicate.value.code == "CONFIRMATION_ALREADY_POSTED"


def test_two_funds_in_one_frozen_plan_track_independent_submissions(tmp_path: Path) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "investor.db")
    ledger.create_instrument(code="CORE02", name="核心基金二", role="CORE")
    opening = ledger.create_opening_position_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE02",
        as_of_date_value="2026-07-20",
        total_shares="100.000000",
        average_cost_nav="1.000000",
        platform="测试平台",
        idempotency_key="opening-CORE02",
    )
    ledger.commit_opening_position_draft(
        draft_id=str(opening["draft"]["id"]),
        confirmation_token=str(opening["confirmation_token"]),
        confirmed_by="test-user",
    )
    MarketDataService(planning.settings).record_nav_snapshot(
        instrument_code="CORE02",
        nav_date_value="2026-07-21",
        nav="0.100000",
        currency="CNY",
        source_type="PLATFORM",
        source_name="测试平台",
        source_ref="test://CORE02",
        source_lineage="ALIPAY",
        verification_status="VERIFIED",
        observed_at_value="2026-07-21T22:00:00+08:00",
        actor_ref="test-user",
    )
    strategy = StrategyService(planning.settings)
    for code, priority in (("CORE01", 1), ("CORE02", 2)):
        strategy.configure_instrument(
            portfolio_id=portfolio_id,
            instrument_code=code,
            role="CORE",
            contribution_eligible=True,
            target_weight_bps=5000,
            priority=priority,
            minimum_amount_minor=1,
            maximum_amount_minor=None,
            benchmark_code=None,
            thesis_status="ACTIVE",
            approved_by="test-user",
            reason="测试同一计划多基金申购",
        )
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-21",
        idempotency_key="two-fund-plan",
        as_of_date_value="2026-07-21",
    )
    plan = planning.freeze(
        plan_id=str(created["plan"]["id"]),
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    amounts = {
        str(item["instrument_code"]): str(item["candidate_amount"])
        for item in plan["items"]
        if item["action"] == "CONTRIBUTE"
    }
    assert amounts == {"CORE01": "50.00", "CORE02": "50.00"}
    service = SubscriptionService(planning.settings)
    for code in ("CORE01", "CORE02"):
        draft = service.create_submission_draft(
            portfolio_id=portfolio_id,
            account_id=account_id,
            weekly_plan_id=str(plan["id"]),
            instrument_code=code,
            requested_amount="50.00",
            submitted_at="2026-07-21T10:00:00+08:00",
            submitted_business_date="2026-07-21",
            external_platform="测试平台",
            idempotency_key=f"two-fund-{code}",
        )
        service.commit_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token=str(draft["confirmation_token"]),
            confirmed_by="test-user",
        )
    progress = planning.get(plan_id=str(plan["id"]))["execution_progress"]
    assert {item["instrument_code"]: item["in_flight_amount"] for item in progress["items"]} == {
        "CORE01": "50.00",
        "CORE02": "50.00",
    }
