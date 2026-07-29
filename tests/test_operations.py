from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from conftest import PROJECT_ROOT, migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerError, LedgerService
from investor_core.operations import OperationsService


def fixed_now() -> datetime:
    return datetime(2026, 7, 27, 2, 0, tzinfo=UTC)


def settings_for(path: Path, *, production: bool = False) -> Settings:
    return Settings(
        environment=Environment.PRODUCTION if production else Environment.TEST,
        db_path=path,
        expected_python_minor="0.0" if production else "3.11",
    )


def commit_policy(
    service: OperationsService,
    *,
    job_name: str,
    enabled: bool = True,
    config: dict[str, Any] | None = None,
    portfolio_id: str | None = None,
) -> dict[str, Any]:
    draft = service.create_policy_draft(
        portfolio_id=portfolio_id,
        job_name=job_name,
        enabled=enabled,
        schedule="0 8 * * *",
        timezone="Asia/Shanghai",
        config=config or {},
        reason="用户明确批准自动化测试策略",
        actor_ref="test-user",
    )
    return service.commit_policy_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )


def create_context(path: Path) -> tuple[Settings, str, str]:
    settings = settings_for(path)
    ledger = LedgerService(settings, now=fixed_now)
    portfolio = ledger.create_portfolio(name="测试组合")
    account = ledger.create_account(
        portfolio_id=str(portfolio["id"]),
        name="测试账户",
        platform="测试平台",
    )
    return settings, str(portfolio["id"]), str(account["id"])


def test_policy_requires_confirmation_and_supports_explicit_pause(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings, portfolio_id, _account_id = create_context(database_path)
    service = OperationsService(settings, now=fixed_now)

    draft = service.create_policy_draft(
        portfolio_id=portfolio_id,
        job_name="DAILY_RISK_SCAN",
        enabled=True,
        schedule="50 22 * * 1-5",
        timezone="Asia/Shanghai",
        config={"delivery_target": "origin"},
        reason="用户明确启用每日风险扫描",
    )
    assert service.list_policies(portfolio_id=portfolio_id) == []
    with pytest.raises(LedgerError, match="confirmation token"):
        service.commit_policy_draft(
            draft_id=str(draft["draft"]["id"]),
            confirmation_token="wrong",
            confirmed_by="test-user",
        )

    committed = service.commit_policy_draft(
        draft_id=str(draft["draft"]["id"]),
        confirmation_token=str(draft["confirmation_token"]),
        confirmed_by="test-user",
    )
    assert committed["policy"]["enabled"] is True
    assert committed["transactions_created"] is False

    paused = commit_policy(
        service,
        job_name="DAILY_RISK_SCAN",
        enabled=False,
        portfolio_id=portfolio_id,
    )
    assert paused["policy"]["version"] == 2
    assert paused["policy"]["enabled"] is False
    assert len(service.list_policies(portfolio_id=portfolio_id)) == 1


def test_unconfigured_and_paused_jobs_are_silent_and_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings, portfolio_id, _account_id = create_context(database_path)
    service = OperationsService(settings, now=fixed_now)

    unconfigured = service.run_job(
        job_name="DAILY_RISK_SCAN",
        scheduled_for="2026-07-27",
        portfolio_id=portfolio_id,
    )
    assert unconfigured["job_run"]["status"] == "SKIPPED"
    assert unconfigured["display_text"] == "[SILENT]"

    commit_policy(
        service,
        job_name="DAILY_RISK_SCAN",
        enabled=False,
        portfolio_id=portfolio_id,
    )
    paused = service.run_job(
        job_name="DAILY_RISK_SCAN",
        scheduled_for="2026-07-28",
        portfolio_id=portfolio_id,
    )
    replay = service.run_job(
        job_name="DAILY_RISK_SCAN",
        scheduled_for="2026-07-28",
        portfolio_id=portfolio_id,
    )
    assert paused["job_run"]["status"] == "SKIPPED"
    assert replay["job_run"]["id"] == paused["job_run"]["id"]
    assert replay["idempotent_replay"] is True
    assert service.list_outbox(status=None) == []


def test_healthy_system_doctor_creates_silent_fact_bundle_once(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = settings_for(database_path)
    service = OperationsService(settings, now=fixed_now)
    commit_policy(service, job_name="SYSTEM_DOCTOR")

    first = service.run_job(
        job_name="SYSTEM_DOCTOR",
        scheduled_for="2026-07-27T10:00:00+08:00",
    )
    second = service.run_job(
        job_name="SYSTEM_DOCTOR",
        scheduled_for="2026-07-27T10:00:00+08:00",
    )

    assert first["job_run"]["status"] == "SUCCESS"
    assert first["report_bundle"]["delivery_action"] == "SILENT"
    assert first["display_text"] == "[SILENT]"
    assert second["idempotent_replay"] is True
    assert second["job_run"]["id"] == first["job_run"]["id"]
    assert len(service.list_report_bundles(bundle_type="SYSTEM_DOCTOR")) == 1
    assert service.list_outbox(status=None) == []
    summary = service.status_summary()
    assert summary["run_counts"]["SUCCESS"] == 1
    assert summary["open_alert_count"] == 0
    assert summary["automatic_trade"] is False
    assert service.retry_due()["display_text"] == "[SILENT]"


def test_failed_system_doctor_creates_notify_bundle_and_outbox(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = settings_for(database_path, production=True)
    service = OperationsService(settings, now=fixed_now)
    commit_policy(
        service,
        job_name="SYSTEM_DOCTOR",
        config={"delivery_target": "origin"},
    )

    result = service.run_job(
        job_name="SYSTEM_DOCTOR",
        scheduled_for="2026-07-27T10:15:00+08:00",
    )

    assert result["job_run"]["status"] == "DEGRADED"
    assert result["report_bundle"]["delivery_action"] == "NOTIFY"
    assert result["display_text"] != "[SILENT]"
    outbox = service.list_outbox()
    assert len(outbox) == 1
    assert outbox[0]["report_bundle_id"] == result["report_bundle"]["id"]


def test_weekly_plan_policy_requires_explicit_contribution_amount(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings, portfolio_id, _account_id = create_context(database_path)
    service = OperationsService(settings, now=fixed_now)

    with pytest.raises(LedgerError, match="contribution_amount"):
        service.create_policy_draft(
            portfolio_id=portfolio_id,
            job_name="WEEKLY_PLAN_PREPARE",
            enabled=True,
            schedule="30 9 * * 6",
            timezone="Asia/Shanghai",
            config={},
            reason="缺少金额的无效策略",
        )


def test_migration_preserves_existing_job_runs_and_adds_retry_state(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")
    command.upgrade(config, "0011_sell_lifecycle")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO job_runs (
                id, job_name, scheduled_for, idempotency_key, status,
                started_at, finished_at, input_json, output_json,
                error_code, error_summary, trace_id
            ) VALUES (
                'legacy-run', 'legacy', '2026-07-26', 'legacy-key', 'SUCCESS',
                '2026-07-26T00:00:00Z', '2026-07-26T00:00:01Z', '{}', '{}',
                NULL, NULL, 'legacy-trace'
            )
            """
        )
        connection.commit()

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT attempt_count, max_attempts FROM job_runs WHERE id='legacy-run'"
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (1, 3)
    assert revision == ("0013_hermes_scheduler_bridge",)


def test_scheduler_manifest_and_snapshot_detect_drift(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings, portfolio_id, _account_id = create_context(database_path)
    service = OperationsService(settings, now=fixed_now)
    commit_policy(
        service,
        job_name="DAILY_RISK_SCAN",
        config={"delivery_target": "origin"},
        portfolio_id=portfolio_id,
    )

    manifest = service.scheduler_manifest(profile="investor")
    assert manifest["automatic_trade"] is False
    desired = {item["managed_name"]: item for item in manifest["jobs"]}
    assert set(desired) == {"value-dca-daily-risk-scan", "value-dca-retry-due"}
    assert desired["value-dca-daily-risk-scan"]["script"] == "value_dca_daily_risk_scan.py"

    drifted = service.record_scheduler_snapshot(
        profile="investor",
        gateway_status="RUNNING",
        jobs=[],
    )
    assert drifted["reconciliation_status"] == "DRIFT"
    assert drifted["drift"]["missing"] == [
        "value-dca-daily-risk-scan",
        "value-dca-retry-due",
    ]

    actual = [
        {
            "managed_name": item["managed_name"],
            "schedule": item["schedule"],
            "enabled": True,
            "no_agent": item["no_agent"],
            "script": item["script"],
            "delivery_target": item["delivery_target"],
            "last_status": None,
            "last_run_at": None,
            "next_run_at": None,
        }
        for item in manifest["jobs"]
    ]
    reconciled = service.record_scheduler_snapshot(
        profile="investor",
        gateway_status="RUNNING",
        jobs=actual,
    )
    assert reconciled["reconciliation_status"] == "IN_SYNC"
    assert service.status_summary()["scheduler_status"] == "IN_SYNC"


def test_scheduler_manifest_has_no_public_default_jobs(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    service = OperationsService(settings_for(database_path), now=fixed_now)

    manifest = service.scheduler_manifest(profile="investor")

    assert manifest["jobs"] == []
    assert manifest["automatic_trade"] is False


def test_job_uses_policy_timezone_date_when_schedule_value_is_omitted(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = settings_for(database_path)
    current = [fixed_now()]

    def mutable_now() -> datetime:
        return current[0]

    service = OperationsService(settings, now=mutable_now)
    commit_policy(service, job_name="SYSTEM_DOCTOR")
    current[0] = datetime(2026, 7, 28, 0, 1, tzinfo=UTC)

    result = service.run_job(job_name="SYSTEM_DOCTOR")

    assert result["job_run"]["scheduled_for"] == "2026-07-28T00:00:00Z"


def test_missed_run_waits_for_grace_then_recovers_once(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    current = [datetime(2026, 7, 27, 2, 0, tzinfo=UTC)]

    def mutable_now() -> datetime:
        return current[0]

    service = OperationsService(settings_for(database_path), now=mutable_now)
    commit_policy(service, job_name="SYSTEM_DOCTOR")

    current[0] = datetime(2026, 7, 28, 0, 5, tzinfo=UTC)
    assert service.list_missed_runs() == []

    current[0] = datetime(2026, 7, 28, 0, 11, tzinfo=UTC)
    missed = service.list_missed_runs()
    assert len(missed) == 1
    assert missed[0]["job_name"] == "SYSTEM_DOCTOR"
    assert missed[0]["scheduled_for"] == "2026-07-28T00:00:00Z"
    assert missed[0]["recovery_state"] == "DUE"

    recovered = service.catch_up_due()
    assert recovered["recovered_count"] == 1
    assert recovered["items"][0]["job_run"]["input"]["actor_ref"] == "operations-catch-up"
    assert recovered["automatic_trade"] is False
    assert service.list_missed_runs() == []
    assert service.catch_up_due()["recovered_count"] == 0


def test_legacy_business_date_run_prevents_duplicate_catch_up(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    current = [datetime(2026, 7, 27, 2, 0, tzinfo=UTC)]

    def mutable_now() -> datetime:
        return current[0]

    service = OperationsService(settings_for(database_path), now=mutable_now)
    commit_policy(service, job_name="SYSTEM_DOCTOR")
    current[0] = datetime(2026, 7, 28, 0, 1, tzinfo=UTC)
    legacy = service.run_job(
        job_name="SYSTEM_DOCTOR",
        scheduled_for="2026-07-28",
    )
    current[0] = datetime(2026, 7, 28, 0, 20, tzinfo=UTC)

    assert legacy["job_run"]["status"] == "SUCCESS"
    assert service.list_missed_runs() == []


def test_recover_due_catches_up_before_processing_retries(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    current = [datetime(2026, 7, 27, 2, 0, tzinfo=UTC)]

    def mutable_now() -> datetime:
        return current[0]

    service = OperationsService(settings_for(database_path), now=mutable_now)
    commit_policy(service, job_name="SYSTEM_DOCTOR")
    current[0] = datetime(2026, 7, 28, 0, 20, tzinfo=UTC)

    result = service.recover_due()

    assert result["catch_up"]["recovered_count"] == 1
    assert result["retries"]["retried_count"] == 0
    assert result["automatic_trade"] is False


def test_recovered_timestamp_uses_policy_timezone_for_business_date() -> None:
    assert (
        OperationsService._business_date(
            "2026-07-27T17:00:00Z",
            timezone="Asia/Shanghai",
        )
        == "2026-07-28"
    )
