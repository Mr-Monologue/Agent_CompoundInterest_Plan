from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from test_planning import configured_services

from investor_core.ledger import JsonDict, LedgerError
from investor_core.market_data import MarketDataService
from investor_core.strategy import StrategyService
from investor_core.subscriptions import SubscriptionService
from investor_core.workspace import WorkspaceService


def _business_fact_counts(database_path: Path) -> dict[str, int]:
    tables = (
        "external_subscriptions",
        "external_subscription_confirmations",
        "transactions",
        "holding_snapshots",
        "cash_ledger_events",
        "plan_execution_links",
    )
    with sqlite3.connect(database_path) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


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


def test_expired_submission_draft_renews_in_place_without_business_side_effects(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    _, planning, _, portfolio_id, account_id, plan = frozen_plan(database_path)
    clock = [datetime(2026, 8, 7, tzinfo=UTC)]
    service = SubscriptionService(planning.settings, now=lambda: clock[0])
    created = service.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=str(plan["id"]),
        instrument_code="CORE01",
        requested_amount="100.00",
        submitted_at="2026-08-07T10:00:00+08:00",
        submitted_business_date="2026-08-07",
        external_platform="测试平台",
        idempotency_key="renew-submit",
    )
    original = created["draft"]
    original_token = str(created["confirmation_token"])
    assert datetime.fromisoformat(str(original["expires_at"]).replace("Z", "+00:00")) == (
        clock[0] + timedelta(hours=24)
    )
    clock[0] += timedelta(hours=24, seconds=1)
    assert service.get_draft(draft_id=str(original["id"]))["status"] == "EXPIRED"
    reused = service.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=str(plan["id"]),
        instrument_code="CORE01",
        requested_amount="100.00",
        submitted_at="2026-08-07T10:00:00+08:00",
        submitted_business_date="2026-08-07",
        external_platform="测试平台",
        idempotency_key="renew-submit",
    )
    assert reused["draft"]["id"] == original["id"]
    assert reused["confirmation_token"] is None
    assert "external_subscription_draft_renew" in reused["warnings"][0]
    with pytest.raises(LedgerError) as expired:
        service.commit_draft(
            draft_id=str(original["id"]),
            confirmation_token=original_token,
            confirmed_by="test-user",
        )
    assert expired.value.code == "CONFIRMATION_TOKEN_EXPIRED"
    assert "external_subscription_draft_renew" in expired.value.message
    before = _business_fact_counts(database_path)

    renewed = service.renew_draft(draft_id=str(original["id"]), actor_ref="test-user")

    assert renewed["draft"]["id"] == original["id"]
    assert renewed["draft"]["idempotency_key"] == original["idempotency_key"]
    assert renewed["draft"]["payload"] == original["payload"]
    assert renewed["draft"]["payload_hash"] == original["payload_hash"]
    assert renewed["draft"]["status"] == "PENDING"
    assert renewed["draft"]["renewal_count"] == 1
    assert renewed["draft"]["renewed_at"] is not None
    assert renewed["business_facts_created"] is False
    assert _business_fact_counts(database_path) == before
    with pytest.raises(LedgerError) as stale:
        service.commit_draft(
            draft_id=str(original["id"]),
            confirmation_token=original_token,
            confirmed_by="test-user",
        )
    assert stale.value.code == "INVALID_CONFIRMATION_TOKEN"

    committed = service.commit_draft(
        draft_id=str(original["id"]),
        confirmation_token=str(renewed["confirmation_token"]),
        confirmed_by="test-user",
    )
    assert committed["subscription"]["status"] == "SUBMITTED"


def test_unexpired_or_committed_external_subscription_draft_cannot_renew(
    tmp_path: Path,
) -> None:
    _, planning, _, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    clock = [datetime(2026, 8, 7, tzinfo=UTC)]
    service = SubscriptionService(planning.settings, now=lambda: clock[0])
    created = service.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=str(plan["id"]),
        instrument_code="CORE01",
        requested_amount="100.00",
        submitted_at="2026-08-07T10:00:00+08:00",
        submitted_business_date="2026-08-07",
        external_platform="测试平台",
        idempotency_key="renew-state-guards",
    )
    with pytest.raises(LedgerError) as active:
        service.renew_draft(draft_id=str(created["draft"]["id"]))
    assert active.value.code == "DRAFT_NOT_EXPIRED"
    service.commit_draft(
        draft_id=str(created["draft"]["id"]),
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    clock[0] += timedelta(days=2)
    with pytest.raises(LedgerError) as committed:
        service.renew_draft(draft_id=str(created["draft"]["id"]))
    assert committed.value.code == "DRAFT_ALREADY_COMMITTED"


def test_external_subscription_draft_renew_rejects_changed_payload(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    _, planning, _, portfolio_id, account_id, plan = frozen_plan(database_path)
    clock = [datetime(2026, 8, 7, tzinfo=UTC)]
    service = SubscriptionService(planning.settings, now=lambda: clock[0])
    created = service.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=str(plan["id"]),
        instrument_code="CORE01",
        requested_amount="100.00",
        submitted_at="2026-08-07T10:00:00+08:00",
        submitted_business_date="2026-08-07",
        external_platform="测试平台",
        idempotency_key="renew-payload-guard",
    )
    clock[0] += timedelta(days=2)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE external_subscription_drafts SET payload_json=? WHERE id=?",
            ('{"requested_amount_minor":1}', created["draft"]["id"]),
        )
        connection.commit()

    with pytest.raises(LedgerError) as changed:
        service.renew_draft(draft_id=str(created["draft"]["id"]))
    assert changed.value.code == "DRAFT_PAYLOAD_CHANGED"


def test_concurrent_external_subscription_renew_issues_only_one_token(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    _, planning, _, portfolio_id, account_id, plan = frozen_plan(database_path)
    clock = [datetime(2026, 8, 7, tzinfo=UTC)]
    creator = SubscriptionService(planning.settings, now=lambda: clock[0])
    created = creator.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=str(plan["id"]),
        instrument_code="CORE01",
        requested_amount="100.00",
        submitted_at="2026-08-07T10:00:00+08:00",
        submitted_business_date="2026-08-07",
        external_platform="测试平台",
        idempotency_key="renew-concurrent",
    )
    clock[0] += timedelta(days=2)
    barrier = Barrier(2)

    def attempt() -> JsonDict | str:
        service = SubscriptionService(planning.settings, now=lambda: clock[0])
        barrier.wait()
        try:
            return service.renew_draft(draft_id=str(created["draft"]["id"]))
        except LedgerError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: attempt(), range(2)))

    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, str)]
    assert len(successes) == 1
    assert failures == ["DRAFT_NOT_EXPIRED"]
    with sqlite3.connect(database_path) as connection:
        audit_count = connection.execute(
            """
            SELECT COUNT(*) FROM audit_events
            WHERE entity_id=? AND action='EXTERNAL_SUBSCRIPTION_DRAFT_RENEWED'
            """,
            (created["draft"]["id"],),
        ).fetchone()[0]
    assert audit_count == 1


def test_expired_confirmation_draft_renews_with_same_contract(tmp_path: Path) -> None:
    _, planning, service, portfolio_id, account_id, plan = frozen_plan(
        tmp_path / "investor.db"
    )
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    clock = [datetime(2026, 8, 7, tzinfo=UTC)]
    governed = SubscriptionService(planning.settings, now=lambda: clock[0])
    created = governed.create_confirmation_draft(
        subscription_id=str(subscription["id"]),
        confirmed_at="2026-08-07T18:00:00+08:00",
        confirmation_business_date="2026-08-07",
        nav_date="2026-08-07",
        nav="1.000000",
        confirmed_shares="100.000000",
        confirmed_amount="100.00",
        fee="0",
        refunded_amount="0",
        idempotency_key="renew-confirm",
    )
    original = created["draft"]
    original_token = str(created["confirmation_token"])
    clock[0] += timedelta(hours=24, seconds=1)

    renewed = governed.renew_draft(draft_id=str(original["id"]))

    assert renewed["draft"]["id"] == original["id"]
    assert renewed["draft"]["idempotency_key"] == original["idempotency_key"]
    assert renewed["draft"]["payload"] == original["payload"]
    assert renewed["draft"]["payload_hash"] == original["payload_hash"]
    with pytest.raises(LedgerError) as stale:
        governed.commit_draft(
            draft_id=str(original["id"]),
            confirmation_token=original_token,
            confirmed_by="test-user",
        )
    assert stale.value.code == "INVALID_CONFIRMATION_TOKEN"
    result = governed.commit_draft(
        draft_id=str(original["id"]),
        confirmation_token=str(renewed["confirmation_token"]),
        confirmed_by="test-user",
    )
    assert result["subscription"]["status"] == "CONFIRMED"


def test_external_subscription_ttl_is_configurable_without_extending_existing_drafts(
    tmp_path: Path,
) -> None:
    _, planning, _, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    clock = [datetime(2026, 8, 7, tzinfo=UTC)]
    settings = planning.settings.model_copy(
        update={"external_subscription_confirmation_ttl_minutes": 60}
    )
    service = SubscriptionService(settings, now=lambda: clock[0])
    created = service.create_submission_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        weekly_plan_id=str(plan["id"]),
        instrument_code="CORE01",
        requested_amount="10.00",
        submitted_at="2026-08-07T10:00:00+08:00",
        submitted_business_date="2026-08-07",
        external_platform="测试平台",
        idempotency_key="renew-configurable-ttl",
    )
    assert datetime.fromisoformat(
        str(created["draft"]["expires_at"]).replace("Z", "+00:00")
    ) == clock[0] + timedelta(hours=1)


def test_exact_and_date_only_confirmation_precision_contract(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    _, _, service, portfolio_id, account_id, plan = frozen_plan(database_path)
    first_subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
        amount="50.00",
        key="precision-submit-one",
    )
    exact = service.create_confirmation_draft(
        subscription_id=str(first_subscription["id"]),
        confirmed_at="2026-07-22T18:30:00+08:00",
        confirmed_at_precision="EXACT",
        confirmation_business_date="2026-07-22",
        nav_date="2026-07-22",
        nav="1.000000",
        confirmed_shares="50.000000",
        confirmed_amount="50.00",
        fee="0",
        refunded_amount="0",
        idempotency_key="precision-exact",
    )
    exact_result = service.commit_draft(
        draft_id=str(exact["draft"]["id"]),
        confirmation_token=str(exact["confirmation_token"]),
        confirmed_by="test-user",
    )["subscription"]["confirmations"][0]
    assert exact_result["confirmed_at_precision"] == "EXACT"
    assert exact_result["confirmed_at_is_exact"] is True
    assert exact_result["confirmed_at_display"] == "2026-07-22T10:30:00Z"

    _, _, second_service, second_portfolio, second_account, second_plan = frozen_plan(
        tmp_path / "date-only.db"
    )
    second_subscription = submit(
        second_service,
        portfolio_id=second_portfolio,
        account_id=second_account,
        plan_id=str(second_plan["id"]),
        key="precision-submit-two",
    )
    date_only = second_service.create_confirmation_draft(
        subscription_id=str(second_subscription["id"]),
        confirmed_at=None,
        confirmed_at_precision="DATE_ONLY",
        confirmation_business_date="2026-07-22",
        nav_date="2026-07-22",
        nav="1.000000",
        confirmed_shares="100.000000",
        confirmed_amount="100.00",
        fee="0",
        refunded_amount="0",
        idempotency_key="precision-date-only",
    )
    assert date_only["draft"]["payload"]["confirmed_at"] == "2026-07-21T16:00:00Z"
    date_only_result = second_service.commit_draft(
        draft_id=str(date_only["draft"]["id"]),
        confirmation_token=str(date_only["confirmation_token"]),
        confirmed_by="test-user",
    )["subscription"]["confirmations"][0]
    assert date_only_result["confirmed_at_precision"] == "DATE_ONLY"
    assert date_only_result["confirmed_at_is_exact"] is False
    assert date_only_result["confirmed_at_display"] == "2026-07-22"
    assert ":" not in date_only_result["confirmed_at_display"]
    assert date_only_result["confirmed_at"] == "2026-07-21T16:00:00Z"
    report_item = second_service.summary(
        portfolio_id=second_portfolio,
        account_id=second_account,
        as_of_date=datetime(2026, 8, 7, tzinfo=UTC).date(),
    )["items"][0]["confirmations"][0]
    assert report_item["confirmed_at_precision"] == "DATE_ONLY"
    assert report_item["confirmed_at_display"] == "2026-07-22"
    assert ":" not in report_item["confirmed_at_display"]


def test_confirmation_precision_rejects_false_or_mismatched_exact_time(
    tmp_path: Path,
) -> None:
    _, _, service, portfolio_id, account_id, plan = frozen_plan(tmp_path / "investor.db")
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    with pytest.raises(LedgerError) as exact_mismatch:
        service.create_confirmation_draft(
            subscription_id=str(subscription["id"]),
            confirmed_at="2026-07-23T00:01:00+08:00",
            confirmed_at_precision="EXACT",
            confirmation_business_date="2026-07-22",
            nav_date="2026-07-22",
            nav="1",
            confirmed_shares="100",
            confirmed_amount="100",
            fee="0",
            refunded_amount="0",
            idempotency_key="precision-exact-mismatch",
        )
    assert exact_mismatch.value.code == "CONFIRMED_AT_DATE_MISMATCH"
    with pytest.raises(LedgerError) as false_time:
        service.create_confirmation_draft(
            subscription_id=str(subscription["id"]),
            confirmed_at="2026-07-22T09:30:00+08:00",
            confirmed_at_precision="DATE_ONLY",
            confirmation_business_date="2026-07-22",
            nav_date="2026-07-22",
            nav="1",
            confirmed_shares="100",
            confirmed_amount="100",
            fee="0",
            refunded_amount="0",
            idempotency_key="precision-date-only-false-time",
        )
    assert false_time.value.code == "DATE_ONLY_TIMESTAMP_NOT_NORMALIZED"


@pytest.mark.parametrize("expired", [False, True])
def test_uncommitted_confirmation_draft_revises_precision_in_place(
    tmp_path: Path,
    expired: bool,
) -> None:
    database_path = tmp_path / "investor.db"
    _, planning, service, portfolio_id, account_id, plan = frozen_plan(database_path)
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    clock = [datetime(2026, 8, 20, tzinfo=UTC)]
    governed = SubscriptionService(planning.settings, now=lambda: clock[0])
    created = governed.create_confirmation_draft(
        subscription_id=str(subscription["id"]),
        confirmed_at="2026-08-20T00:00:00+08:00",
        confirmed_at_precision="EXACT",
        confirmation_business_date="2026-08-20",
        nav_date="2026-08-20",
        nav="1.000000",
        confirmed_shares="100.000000",
        confirmed_amount="100.00",
        fee="0",
        refunded_amount="0",
        external_reference="immutable-order-reference",
        idempotency_key=f"revise-precision-{expired}",
    )
    original = created["draft"]
    original_token = str(created["confirmation_token"])
    if expired:
        clock[0] += timedelta(days=2)
        assert governed.get_draft(draft_id=str(original["id"]))["status"] == "EXPIRED"
    before = _business_fact_counts(database_path)

    revised = governed.revise_confirmation_draft(
        draft_id=str(original["id"]),
        expected_payload_hash=str(original["payload_hash"]),
        confirmed_at_precision="DATE_ONLY",
        actor_ref="test-user",
    )

    assert revised["draft"]["id"] == original["id"]
    assert revised["draft"]["subscription_id"] == original["subscription_id"]
    assert revised["draft"]["idempotency_key"] == original["idempotency_key"]
    assert revised["draft"]["payload_hash"] != original["payload_hash"]
    assert revised["draft"]["revision_count"] == 1
    assert revised["draft"]["revised_at"] is not None
    assert revised["draft"]["status"] == "PENDING"
    assert revised["changed_fields"] == ["confirmed_at_precision"]
    assert revised["draft"]["payload"]["external_reference"] == (
        "immutable-order-reference"
    )
    assert revised["business_facts_created"] is False
    assert _business_fact_counts(database_path) == before
    with sqlite3.connect(database_path) as connection:
        revision_audit = json.loads(
            connection.execute(
                """
                SELECT details_json FROM audit_events
                WHERE entity_id=?
                  AND action='EXTERNAL_SUBSCRIPTION_CONFIRMATION_DRAFT_REVISED'
                """,
                (original["id"],),
            ).fetchone()[0]
        )
    assert revision_audit["old_payload_hash"] == original["payload_hash"]
    assert revision_audit["new_payload_hash"] == revised["draft"]["payload_hash"]
    assert revision_audit["confirmed_at_precision"] == "DATE_ONLY"
    with pytest.raises(LedgerError) as stale:
        governed.commit_draft(
            draft_id=str(original["id"]),
            confirmation_token=original_token,
            confirmed_by="test-user",
        )
    assert stale.value.code == "INVALID_CONFIRMATION_TOKEN"

    committed = governed.commit_draft(
        draft_id=str(original["id"]),
        confirmation_token=str(revised["confirmation_token"]),
        confirmed_by="test-user",
    )["subscription"]["confirmations"][0]
    assert committed["confirmed_at_precision"] == "DATE_ONLY"
    assert committed["confirmed_at_display"] == "2026-08-20"
    with sqlite3.connect(database_path) as connection:
        commit_audit = json.loads(
            connection.execute(
                """
                SELECT details_json FROM audit_events
                WHERE action='EXTERNAL_SUBSCRIPTION_CONFIRM_COMMITTED'
                ORDER BY occurred_at DESC LIMIT 1
                """
            ).fetchone()[0]
        )
    assert commit_audit["confirmed_at_precision"] == "DATE_ONLY"
    assert commit_audit["confirmation_business_date"] == "2026-08-20"
    assert commit_audit["confirmed_at"] is None


def test_confirmation_draft_revision_guards_hash_commit_and_concurrency(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    _, planning, service, portfolio_id, account_id, plan = frozen_plan(database_path)
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
    )
    created = service.create_confirmation_draft(
        subscription_id=str(subscription["id"]),
        confirmed_at="2026-07-22T00:00:00+08:00",
        confirmed_at_precision="EXACT",
        confirmation_business_date="2026-07-22",
        nav_date="2026-07-22",
        nav="1",
        confirmed_shares="100",
        confirmed_amount="100",
        fee="0",
        refunded_amount="0",
        idempotency_key="revise-guards",
    )
    with pytest.raises(LedgerError) as mismatch:
        service.revise_confirmation_draft(
            draft_id=str(created["draft"]["id"]),
            expected_payload_hash="0" * 64,
            confirmed_at_precision="DATE_ONLY",
        )
    assert mismatch.value.code == "DRAFT_PAYLOAD_HASH_MISMATCH"
    with pytest.raises(LedgerError) as invalid_dates:
        service.revise_confirmation_draft(
            draft_id=str(created["draft"]["id"]),
            expected_payload_hash=str(created["draft"]["payload_hash"]),
            confirmed_at_precision="DATE_ONLY",
            confirmation_business_date="2026-07-20",
            nav_date="2026-07-20",
        )
    assert invalid_dates.value.code == "INVALID_CONFIRMATION_DATES"

    barrier = Barrier(2)

    def attempt() -> JsonDict | str:
        concurrent = SubscriptionService(planning.settings)
        barrier.wait()
        try:
            return concurrent.revise_confirmation_draft(
                draft_id=str(created["draft"]["id"]),
                expected_payload_hash=str(created["draft"]["payload_hash"]),
                confirmed_at_precision="DATE_ONLY",
            )
        except LedgerError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: attempt(), range(2)))
    assert len([result for result in results if isinstance(result, dict)]) == 1
    assert [result for result in results if isinstance(result, str)] == [
        "DRAFT_PAYLOAD_HASH_MISMATCH"
    ]

    current = service.get_draft(draft_id=str(created["draft"]["id"]))
    with pytest.raises(LedgerError) as no_changes:
        service.revise_confirmation_draft(
            draft_id=str(current["id"]),
            expected_payload_hash=str(current["payload_hash"]),
            confirmed_at_precision="DATE_ONLY",
        )
    assert no_changes.value.code == "DRAFT_REVISION_NO_CHANGES"
    committed = service.commit_draft(
        draft_id=str(current["id"]),
        confirmation_token=str(
            next(
                result["confirmation_token"]
                for result in results
                if isinstance(result, dict)
            )
        ),
        confirmed_by="test-user",
    )
    assert committed["subscription"]["status"] == "CONFIRMED"
    with pytest.raises(LedgerError) as already_committed:
        service.revise_confirmation_draft(
            draft_id=str(current["id"]),
            expected_payload_hash=str(current["payload_hash"]),
            confirmed_at_precision="EXACT",
        )
    assert already_committed.value.code == "DRAFT_ALREADY_COMMITTED"


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
