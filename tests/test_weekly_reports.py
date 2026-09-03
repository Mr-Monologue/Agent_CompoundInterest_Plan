from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_planning import commit_buy, configured_services

from investor_core.ledger import LedgerError
from investor_core.market_data import MarketDataService
from investor_core.weekly_reports import WeeklyReportService
from investor_core.workspace import WorkspaceService


def terminal_plan(database_path: Path, *, outcome: str = "EXECUTED"):
    ledger, planning, portfolio_id, account_id = configured_services(database_path)
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-21",
        idempotency_key=f"weekly-report-{outcome}",
        as_of_date_value="2026-07-21",
    )
    plan_id = str(created["plan"]["id"])
    if outcome == "SKIPPED":
        planning.skip(
            plan_id=plan_id,
            confirmation_token=str(created["confirmation_token"]),
            confirmed_by="test-user",
            reason="本周明确跳过",
        )
    else:
        planning.freeze(
            plan_id=plan_id,
            confirmation_token=str(created["confirmation_token"]),
            confirmed_by="test-user",
        )
        amount = "100.00" if outcome == "EXECUTED" else "85.00"
        transaction = commit_buy(
            ledger,
            portfolio_id=portfolio_id,
            account_id=account_id,
            instrument_code="CORE01",
            trade_date="2026-07-22",
            amount=amount,
            key=f"weekly-report-trade-{outcome}",
        )
        planning.link_transaction(
            plan_id=plan_id,
            transaction_id=str(transaction["transaction"]["id"]),
            confirmed_by="test-user",
        )
        if outcome == "PARTIALLY_EXECUTED_CLOSED":
            draft = planning.create_partial_close_draft(
                plan_id=plan_id,
                closure_business_date="2026-07-27",
                closure_reason_code="PLATFORM_LIMIT_REMAINDER_ABANDONED",
                closure_note="周期结束; 剩余金额不执行且不结转。",
                carry_forward=False,
                idempotency_key="weekly-report-partial-close",
            )
            planning.commit_partial_close_draft(
                draft_id=str(draft["draft"]["id"]),
                confirmation_token=str(draft["confirmation_token"]),
                confirmed_by="test-user",
            )
    return planning.settings, portfolio_id, account_id, plan_id


def commit_report(service: WeeklyReportService, plan_id: str, *, key: str, reason=None):
    draft = service.create_draft(
        plan_id=plan_id,
        idempotency_key=key,
        regeneration_reason=reason,
    )
    return service.commit_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )


@pytest.mark.parametrize(
    ("outcome", "label", "executed", "abandoned", "rate"),
    [
        ("EXECUTED", "全部执行", "100.00", "0.00", "100.00"),
        ("SKIPPED", "已跳过", "0.00", "100.00", "0.00"),
        (
            "PARTIALLY_EXECUTED_CLOSED",
            "部分执行后结束",
            "85.00",
            "15.00",
            "85.00",
        ),
    ],
)
def test_terminal_plan_weekly_report_uses_plan_period_and_outcome(
    tmp_path: Path,
    outcome: str,
    label: str,
    executed: str,
    abandoned: str,
    rate: str,
) -> None:
    settings, _portfolio_id, _account_id, plan_id = terminal_plan(
        tmp_path / f"{outcome}.db", outcome=outcome
    )
    report = commit_report(WeeklyReportService(settings), plan_id, key=f"report-{outcome}")
    content = report["report"]["content"]
    assert content["weekly_plan_id"] == plan_id
    assert content["period_start"] == "2026-07-21"
    assert content["period_end"] == "2026-07-27"
    assert content["plan_outcome"] == label
    assert content["executed_amount"] == executed
    assert content["abandoned_amount"] == abandoned
    assert content["execution_rate_pct"] == rate
    assert report["financial_facts_created"] is False
    assert report["notification_sent"] is False


def test_open_plan_is_preview_only_and_cannot_be_finalized(tmp_path: Path) -> None:
    ledger, planning, portfolio_id, account_id = configured_services(tmp_path / "open.db")
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-08-18",
        idempotency_key="open-weekly-report",
        as_of_date_value="2026-07-21",
    )
    service = WeeklyReportService(planning.settings)
    transaction_count = len(
        ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id)
    )
    preview = service.preview(plan_id=str(created["plan"]["id"]))
    assert preview["period_start"] == "2026-08-18"
    assert preview["period_end"] == "2026-08-24"
    assert preview["preview_only"] is True
    with pytest.raises(LedgerError) as error:
        service.create_draft(
            plan_id=str(created["plan"]["id"]), idempotency_key="open-final"
        )
    assert error.value.code == "WEEKLY_REPORT_PLAN_NOT_FINAL"
    assert (
        len(ledger.list_transactions(portfolio_id=portfolio_id, account_id=account_id))
        == transaction_count
    )


def test_report_idempotency_versions_and_bundle_storage(tmp_path: Path) -> None:
    settings, _portfolio_id, _account_id, plan_id = terminal_plan(tmp_path / "versions.db")
    service = WeeklyReportService(settings)
    draft = service.create_draft(plan_id=plan_id, idempotency_key="same-request")
    replay = service.create_draft(plan_id=plan_id, idempotency_key="same-request")
    assert replay["reused"] is True
    assert replay["draft"]["id"] == draft["draft"]["id"]
    first = service.commit_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )["report"]
    committed_replay = service.create_draft(plan_id=plan_id, idempotency_key="same-request")
    assert committed_replay["reused"] is True
    assert committed_replay["draft"]["status"] == "COMMITTED"
    with pytest.raises(LedgerError) as missing_reason:
        service.create_draft(plan_id=plan_id, idempotency_key="second-no-reason")
    assert missing_reason.value.code == "WEEKLY_REPORT_REGENERATION_REASON_REQUIRED"
    second = commit_report(
        service,
        plan_id,
        key="second-with-reason",
        reason="补充来源核验结果",
    )["report"]
    assert first["report_version"] == 1
    assert first["is_current"] is True
    assert service.get_report(report_id=str(first["id"]))["is_current"] is False
    assert second["report_version"] == 2
    assert second["supersedes_report_id"] == first["id"]
    assert len(service.list_reports(plan_id=plan_id)) == 2
    with sqlite3.connect(settings.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM report_bundles WHERE bundle_type='WEEKLY_PLAN_REPORT'"
        ).fetchone() == (2,)


def test_missing_or_unverified_exact_end_nav_only_limits_valuation(tmp_path: Path) -> None:
    settings, _portfolio_id, _account_id, plan_id = terminal_plan(tmp_path / "quality.db")
    service = WeeklyReportService(settings)
    missing = service.preview(plan_id=plan_id)
    assert missing["valuation"]["status"] == "LIMITED"
    assert missing["valuation"]["substitution_used"] is False
    assert missing["valuation"]["missing"][0]["required_nav_date"] == "2026-07-27"

    market = MarketDataService(settings)
    market.record_nav_snapshot(
        instrument_code="CORE01",
        nav_date_value="2026-07-27",
        nav="1.100000",
        currency="CNY",
        source_type="AGGREGATOR",
        source_name="单一来源",
        source_ref="test://unverified",
        source_lineage="EASTMONEY",
        verification_status="UNVERIFIED",
        observed_at_value="2026-07-27T18:00:00+08:00",
        actor_ref="test-user",
    )
    limited = service.preview(plan_id=plan_id)
    assert limited["valuation"]["status"] == "LIMITED"
    assert limited["valuation"]["limited"][0]["verification_status"] == "UNVERIFIED"
    report = commit_report(service, plan_id, key="limited-report")["report"]
    assert report["valuation_status"] == "LIMITED"
    assert report["content"]["valuation"]["business_state"] == "交易事实完整、估值部分不可用"


def test_exact_verified_end_nav_allows_valuation_state(tmp_path: Path) -> None:
    settings, _portfolio_id, _account_id, plan_id = terminal_plan(tmp_path / "verified.db")
    market = MarketDataService(settings)
    for code, nav in (("CORE01", "1.100000"), ("SAT01", "1.000000")):
        market.record_nav_snapshot(
            instrument_code=code,
            nav_date_value="2026-07-27",
            nav=nav,
            currency="CNY",
            source_type="PLATFORM",
            source_name="已验证平台",
            source_ref=f"test://verified/end/{code}",
            source_lineage="ALIPAY",
            verification_status="VERIFIED",
            observed_at_value="2026-07-27T18:00:00+08:00",
            actor_ref="test-user",
        )
    preview = WeeklyReportService(settings).preview(plan_id=plan_id)
    assert preview["valuation"]["status"] == "AVAILABLE", preview["valuation"]
    assert preview["data_quality"] in {"PASS", "WARNING"}
    assert preview["valuation"]["end_market_value"] is not None
    assert preview["valuation"]["period_return"] is not None
    assert preview["valuation"]["period_return"]["twr_bps"] is None


def test_exact_end_nav_does_not_substitute_an_adjacent_start_nav(tmp_path: Path) -> None:
    settings, _portfolio_id, _account_id, plan_id = terminal_plan(tmp_path / "start.db")
    market = MarketDataService(settings)
    with sqlite3.connect(settings.db_path) as connection:
        connection.execute("DELETE FROM market_nav_snapshots WHERE nav_date='2026-07-21'")
        connection.commit()
    for code, nav in (("CORE01", "1.000000"), ("SAT01", "1.000000")):
        market.record_nav_snapshot(
            instrument_code=code,
            nav_date_value="2026-07-20",
            nav=nav,
            currency="CNY",
            source_type="PLATFORM",
            source_name="相邻日期数据",
            source_ref=f"test://verified/adjacent/{code}",
            source_lineage="ALIPAY",
            verification_status="VERIFIED",
            observed_at_value="2026-07-20T18:00:00+08:00",
            actor_ref="test-user",
        )
    for code, nav in (("CORE01", "1.100000"), ("SAT01", "1.000000")):
        market.record_nav_snapshot(
            instrument_code=code,
            nav_date_value="2026-07-27",
            nav=nav,
            currency="CNY",
            source_type="PLATFORM",
            source_name="已验证平台",
            source_ref=f"test://verified/end/{code}",
            source_lineage="ALIPAY",
            verification_status="VERIFIED",
            observed_at_value="2026-07-27T18:00:00+08:00",
            actor_ref="test-user",
        )
    valuation = WeeklyReportService(settings).preview(plan_id=plan_id)["valuation"]
    assert valuation["status"] == "AVAILABLE"
    assert valuation["end_market_value"] is not None
    assert valuation["period_return"] is None
    assert valuation["substitution_used"] is False
    assert {item["required_nav_date"] for item in valuation["limited"]} == {"2026-07-21"}


def test_commit_is_concurrent_safe_and_does_not_touch_business_facts(tmp_path: Path) -> None:
    settings, _portfolio_id, _account_id, plan_id = terminal_plan(tmp_path / "concurrent.db")
    service = WeeklyReportService(settings)
    draft = service.create_draft(plan_id=plan_id, idempotency_key="concurrent-report")
    with sqlite3.connect(settings.db_path) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "transactions",
                "holding_snapshots",
                "cash_ledger_events",
                "external_subscriptions",
                "investment_plans",
            )
        }

    def commit():
        return service.commit_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token=str(draft["confirmation_token"]),
            confirmed_by="test-user",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: commit(), range(2)))
    assert sorted(result["idempotent_replay"] for result in results) == [False, True]
    with sqlite3.connect(settings.db_path) as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    assert after == before


def test_confirmation_facts_preserve_gross_net_fee_and_hide_date_only_time(
    tmp_path: Path,
) -> None:
    settings, _portfolio_id, _account_id, plan_id = terminal_plan(tmp_path / "dates.db")
    with sqlite3.connect(settings.db_path) as connection:
        context = connection.execute(
            """SELECT portfolio_id, account_id FROM investment_plans WHERE id=?""",
            (plan_id,),
        ).fetchone()
        instrument_id = connection.execute(
            "SELECT id FROM instruments WHERE code='CORE01'"
        ).fetchone()[0]
        for index, (gross, net, fee) in enumerate(
            ((4000, 3997, 3), (3000, 2996, 4), (2000, 1998, 2), (1000, 999, 1)),
            start=1,
        ):
            subscription_id = f"weekly-report-subscription-{index}"
            confirmation_id = f"weekly-report-confirmation-{index}"
            connection.execute(
                """
                INSERT INTO external_subscriptions (
                    id, portfolio_id, account_id, weekly_plan_id, instrument_id,
                    requested_amount_minor, currency, submitted_at,
                    submitted_business_date, external_platform, status,
                    pending_amount_minor, confirmed_amount_minor, fee_minor,
                    refunded_amount_minor, cancelled_amount_minor, source,
                    recorded_by, idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'CNY', '2026-07-22T02:00:00Z',
                          '2026-07-22', '测试平台', 'CONFIRMED', 0, ?, ?, 0, 0,
                          'USER_REPORTED', 'test-user', ?, '2026-07-22T02:00:00Z',
                          '2026-07-22T02:00:00Z')
                """,
                (
                    subscription_id,
                    context[0],
                    context[1],
                    plan_id,
                    instrument_id,
                    gross,
                    net,
                    fee,
                    f"weekly-report-subscription-key-{index}",
                ),
            )
            connection.execute(
                """
                INSERT INTO external_subscription_confirmations (
                    id, subscription_id, kind, confirmed_at, confirmed_at_precision,
                    confirmation_business_date, nav_date, nav_micros,
                    confirmed_shares_micros, confirmed_amount_minor, fee_minor,
                    refunded_amount_minor, recorded_by, idempotency_key, created_at
                ) VALUES (?, ?, 'CONFIRMATION', '2026-07-24T00:00:00+08:00',
                          'DATE_ONLY', '2026-07-24', '2026-07-23', 1000000,
                          ?, ?, ?, 0, 'test-user', ?, '2026-07-24T02:00:00Z')
                """,
                (
                    confirmation_id,
                    subscription_id,
                    net * 10_000,
                    net,
                    fee,
                    f"weekly-report-confirmation-key-{index}",
                ),
            )
        connection.commit()
    content = WeeklyReportService(settings).preview(plan_id=plan_id)
    subscriptions = content["items"][0]["subscriptions"]
    assert [item["gross_amount"] for item in subscriptions] == [
        "40.00",
        "30.00",
        "20.00",
        "10.00",
    ]
    assert sum(Decimal(item["confirmed_amount"]) for item in subscriptions) == Decimal("99.90")
    assert sum(Decimal(item["fee"]) for item in subscriptions) == Decimal("0.10")
    assert all(item["confirmed_at_precision"] == "DATE_ONLY" for item in subscriptions)
    assert all(item["confirmed_at"] is None for item in subscriptions)


def test_expired_draft_can_be_renewed_without_changing_facts(tmp_path: Path) -> None:
    settings, _portfolio_id, _account_id, plan_id = terminal_plan(tmp_path / "renew.db")
    clock = datetime(2026, 8, 1, tzinfo=UTC)
    service = WeeklyReportService(settings, now=lambda: clock)
    created = service.create_draft(plan_id=plan_id, idempotency_key="renew-report")
    old_token = str(created["confirmation_token"])
    clock += timedelta(hours=1)
    assert service.get_draft(draft_id=str(created["draft"]["id"]))["status"] == "EXPIRED"
    renewed = service.renew_draft(draft_id=str(created["draft"]["id"]))
    assert renewed["draft"]["id"] == created["draft"]["id"]
    assert renewed["draft"]["facts_hash"] == created["draft"]["facts_hash"]
    assert renewed["draft"]["renewal_count"] == 1
    with pytest.raises(LedgerError) as old_rejected:
        service.commit_draft(
            draft_id=str(created["draft"]["id"]),
            confirmation_token=old_token,
            confirmed_by="test-user",
        )
    assert old_rejected.value.code == "CONFIRMATION_TOKEN_INVALID"
    committed = service.commit_draft(
        draft_id=str(created["draft"]["id"]),
        confirmation_token=str(renewed["confirmation_token"]),
        confirmed_by="test-user",
    )
    assert committed["report"]["report_version"] == 1


def test_automatic_generation_is_idempotent_and_workspace_tracks_status(tmp_path: Path) -> None:
    settings, portfolio_id, account_id, plan_id = terminal_plan(tmp_path / "automatic.db")
    service = WeeklyReportService(settings)
    first = service.generate_eligible(portfolio_id=portfolio_id)
    second = service.generate_eligible(portfolio_id=portfolio_id)
    assert first["generated_count"] == 1
    assert second["generated_count"] == 0
    with sqlite3.connect(settings.db_path) as connection:
        connection.execute(
            """
            INSERT INTO job_runs (
                id, job_name, scheduled_for, idempotency_key, status,
                started_at, finished_at, input_json, output_json,
                error_code, error_summary, trace_id, attempt_count, max_attempts
            ) VALUES (
                'failed-weekly-report-run', 'WEEKLY_REPORT', '2026-07-28',
                'failed-weekly-report-key', 'FAILED', '2026-07-28T00:00:00Z',
                '2026-07-28T00:00:01Z', '{}', '{}', 'SYNTHETIC_FAILURE',
                'synthetic test failure', 'weekly-report-trace', 1, 3
            )
            """
        )
        connection.commit()
    workspace = WorkspaceService(
        settings, now=lambda: datetime(2026, 7, 28, tzinfo=UTC)
    ).get(
        portfolio_id=portfolio_id,
        account_id=account_id,
        as_of_date=datetime(2026, 7, 28, tzinfo=UTC).date(),
        view="DAILY",
    )
    statuses = workspace["workflows"]["weekly_reports"]
    assert statuses[0]["weekly_plan_id"] == plan_id
    assert statuses[0]["report_status"] == "FACTS_COMPLETE_VALUATION_LIMITED"
    assert any(
        item["code"] == "WEEKLY_REPORTS_VALUATION_LIMITED"
        for item in workspace["next_actions"]
    )
    assert any(
        item["code"] == "WEEKLY_REPORT_GENERATION_FAILED"
        for item in workspace["next_actions"]
    )
