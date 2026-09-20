from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from conftest import migrate_database
from fastapi.testclient import TestClient
from test_operations import commit_policy, create_context

from investor_core.api.app import create_app
from investor_core.background_worker import BackgroundWorker, CoreClient
from investor_core.ledger import LedgerError
from investor_core.operations import OperationsService
from investor_core.scheduler import SchedulerService


class Rig:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = datetime(2026, 7, 27, 2, tzinfo=UTC)
        self.db = tmp / "core.db"
        migrate_database(self.db)
        settings, self.portfolio, _ = create_context(self.db)
        self.ops = OperationsService(settings, now=lambda: self.clock)
        self.scheduler = SchedulerService(self.ops)
        self.executions: list[dict[str, Any]] = []
        self.offline = False
        self.fail = False
        monkeypatch.setattr(self.ops, "_execute", self.execute)
        commit_policy(self.ops, job_name="SYSTEM_DOCTOR", config={"max_attempts": 2})
        self.switch("WINDOWS")
        self.worker = BackgroundWorker(tmp / "journal.db", self.request, now=lambda: self.clock)

    def execute(self, **kwargs: Any) -> tuple[dict[str, Any], str, bool, str]:
        self.executions.append(kwargs)
        if self.fail:
            raise LedgerError("TEST_FAILURE", "Injected task failure")
        return {"status": "PASS"}, "PASS", False, "DOCTOR_PASS"

    def switch(self, target: str) -> dict[str, Any]:
        draft = self.scheduler.create_draft(target=target, actor_ref="test")
        return self.scheduler.commit(
            draft_id=draft["draft"]["id"],
            confirmation_token=draft["confirmation_token"],
            confirmed_by="test",
        )

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.offline:
            raise httpx.ConnectError("isolated offline simulation")
        if method == "GET":
            return self.scheduler.status()
        assert body is not None
        if path.endswith("heartbeat"):
            return self.scheduler.heartbeat(**body)
        return self.ops.run_job(**body)

    def due(self) -> None:
        self.clock = datetime(2026, 7, 28, 0, 1, tzinfo=UTC)

    def queue(self) -> list[dict[str, Any]]:
        with self.worker.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM queue ORDER BY scheduled_for")]

    def run_args(self) -> dict[str, Any]:
        status = self.scheduler.status()
        return {
            "job_name": "SYSTEM_DOCTOR",
            "scheduled_for": "2026-07-28T00:00:00Z",
            "scheduler_generation": status["generation"],
            "scheduler_policy_hash": status["policies"][0]["content_hash"],
        }


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def test_normal_trigger_restart_and_exact_occurrence_dedup(rig: Rig) -> None:
    rig.due()
    rig.worker.tick()
    restarted = BackgroundWorker(rig.worker.path, rig.request, now=lambda: rig.clock)
    restarted.tick()
    assert len(rig.executions) == 1
    row = rig.queue()[0]
    assert row["state"] == "SUCCESS"
    assert row["scheduled_for"] == "2026-07-28T00:00:00Z"
    assert row["actual_at"] == "2026-07-28T00:01:00Z"
    assert rig.scheduler.status()["heartbeat"]["pending_count"] == 0


def test_concurrent_core_claims_execute_once(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.due()
    entered, release = Event(), Event()

    def execute(**kwargs: Any) -> tuple[dict[str, Any], str, bool, str]:
        entered.set()
        assert release.wait(10)
        return rig.execute(**kwargs)

    monkeypatch.setattr(rig.ops, "_execute", execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(rig.ops.run_job, **rig.run_args())
        assert entered.wait(10)
        second = pool.submit(rig.ops.run_job, **rig.run_args()).result(timeout=10)
        assert second["job_run"]["status"] == "RUNNING"
        assert second["idempotent_replay"]
        with pytest.raises(LedgerError, match="running jobs"):
            rig.switch("HERMES")
        release.set()
        assert first.result()["job_run"]["status"] == "SUCCESS"
    assert len(rig.executions) == 1


def test_bounded_task_failure_retry_respects_core_backoff(rig: Rig) -> None:
    rig.due()
    rig.fail = True
    rig.worker.tick()
    assert len(rig.executions) == 1
    early = rig.ops.run_job(**rig.run_args())
    assert early["reason_code"] == "RETRY_NOT_DUE"
    rig.worker.tick()
    assert len(rig.executions) == 1
    rig.clock += timedelta(minutes=5)
    rig.worker.tick()
    assert len(rig.executions) == 2
    assert rig.queue()[0]["state"] == "FAILED"
    rig.clock += timedelta(hours=1)
    rig.worker.tick()
    assert len(rig.executions) == 2


def test_offline_journal_survives_restart_and_transport_retry_is_bounded(rig: Rig) -> None:
    rig.worker.tick()  # cache approved policies
    rig.due()
    rig.offline = True
    rig.worker.tick()
    assert rig.queue()[0]["attempts"] == 1
    assert rig.queue()[0]["detail"] == "CORE_UNAVAILABLE"
    rig.clock += timedelta(minutes=5)
    rig.worker = BackgroundWorker(rig.worker.path, rig.request, now=lambda: rig.clock)
    rig.worker.tick()
    assert rig.queue()[0]["state"] == "FAILED_TRANSPORT"
    rig.offline = False
    rig.clock += timedelta(hours=1)
    rig.worker.tick()
    assert not rig.executions
    assert "WORKER_FAILURES_RECORDED" in rig.scheduler.status()["anomalies"]


def test_temporary_offline_then_recovery(rig: Rig) -> None:
    rig.worker.tick()
    rig.due()
    rig.offline = True
    rig.worker.tick()
    rig.offline = False
    rig.clock += timedelta(minutes=5)
    rig.worker.tick()
    assert len(rig.executions) == 1
    assert rig.queue()[0]["attempts"] == 1


def test_lost_response_reuses_core_claim(rig: Rig) -> None:
    rig.due()
    real_request = rig.request
    lost = False

    def request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal lost
        result = real_request(method, path, body)
        if path.endswith("/run") and not lost:
            lost = True
            raise httpx.ReadTimeout("response lost after commit")
        return result

    rig.worker.request = request
    rig.worker.tick()
    rig.clock += timedelta(minutes=5)
    rig.worker.tick()
    assert len(rig.executions) == 1
    assert rig.queue()[0]["state"] == "SUCCESS"


def test_sleep_catchup_expiry_and_shutdown_range_are_honest(rig: Rig) -> None:
    rig.clock += timedelta(days=3)
    rig.worker.tick()
    assert len(rig.executions) == 1
    assert [r["state"] for r in rig.queue()] == ["EXPIRED", "EXPIRED", "SUCCESS"]
    rig.clock += timedelta(days=10)
    rig.worker.tick()
    with rig.worker.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM events WHERE code LIKE 'MISSED_RANGE:%'").fetchone()[0]
            == 1
        )
    assert len(rig.executions) == 2


def test_switch_rollback_and_pause_fence_old_workers_and_protect_misses(rig: Rig) -> None:
    rig.due()
    old_args = rig.run_args()
    with pytest.raises(LedgerError, match="active owner"):
        rig.ops.run_job(job_name="SYSTEM_DOCTOR", scheduled_for=old_args["scheduled_for"])
    assert rig.ops.catch_up_due()["recovered_count"] == 0
    rig.clock += timedelta(minutes=11)
    rig.switch("HERMES")
    with pytest.raises(LedgerError, match="active owner"):
        rig.ops.run_job(**old_args)
    with pytest.raises(LedgerError, match="cutover boundary"):
        rig.ops.run_job(job_name="SYSTEM_DOCTOR", scheduled_for=old_args["scheduled_for"])
    assert len(rig.scheduler.status()["protected_misses"]) == 1
    assert rig.ops.list_runs() == []
    rig.clock += timedelta(days=1)
    result = rig.ops.run_job(job_name="SYSTEM_DOCTOR", scheduled_for="2026-07-29T00:00:00Z")
    assert result["job_run"]["status"] == "SUCCESS"
    rig.switch("PAUSED")
    rig.clock += timedelta(days=1)
    with pytest.raises(LedgerError, match="active owner"):
        rig.ops.run_job(job_name="SYSTEM_DOCTOR", scheduled_for="2026-07-30T00:00:00Z")


def test_policy_drift_and_disabled_policy_do_not_run(rig: Rig) -> None:
    draft = rig.scheduler.create_draft(target="HERMES", actor_ref="test")
    commit_policy(rig.ops, job_name="SYSTEM_DOCTOR", enabled=False)
    with pytest.raises(LedgerError, match="policy changed"):
        rig.scheduler.commit(
            draft_id=draft["draft"]["id"],
            confirmation_token=draft["confirmation_token"],
            confirmed_by="test",
        )
    rig.due()
    rig.worker.tick()
    assert not rig.executions


def test_stale_heartbeat_and_indeterminate_run_remain_visible(rig: Rig) -> None:
    rig.due()
    args = rig.run_args()
    rig.ops.run_job(**args)
    with sqlite3.connect(rig.db) as db:
        db.execute("UPDATE job_runs SET status='RUNNING',finished_at=NULL")
    rig.worker.tick()
    assert rig.queue()[0]["state"] == "UNKNOWN"
    assert len(rig.executions) == 1
    rig.clock += timedelta(minutes=4)
    assert "WINDOWS_HEARTBEAT_MISSING_OR_STALE" in rig.scheduler.status()["anomalies"]
    rig.worker.tick()
    assert len(rig.executions) == 1


def test_worker_process_mutex(rig: Rig) -> None:
    lock = sqlite3.connect(str(rig.worker.path) + ".lock")
    try:
        lock.execute("BEGIN IMMEDIATE")
        assert rig.worker.tick() == {"state": "BUSY"}
    finally:
        lock.close()


def test_http_preview_governance_and_scope(tmp_path: Path) -> None:
    db = tmp_path / "http.db"
    migrate_database(db)
    settings, _, _ = create_context(db)
    client = TestClient(create_app(settings))
    before = client.get("/v1/background-scheduler").json()["data"]
    assert client.get("/v1/background-scheduler/preview").status_code == 200
    assert client.get("/v1/background-scheduler").json()["data"] == before
    draft = client.post("/v1/background-scheduler/drafts", json={"target": "WINDOWS"}).json()[
        "data"
    ]
    route = f"/v1/background-scheduler/drafts/{draft['draft']['id']}/commit"
    assert (
        client.post(route, json={"confirmation_token": "wrong", "confirmed_by": "test"}).status_code
        == 409
    )
    assert (
        client.post(
            route, json={"confirmation_token": draft["confirmation_token"], "confirmed_by": "test"}
        ).status_code
        == 200
    )
    assert client.post(
        "/v1/background-scheduler/run",
        json={
            "job_name": "WEEKLY_REPORT",
            "portfolio_id": "other",
            "scheduler_generation": "stale",
            "scheduler_policy_hash": "wrong",
        },
    ).status_code in {400, 409}
    with pytest.raises(ValueError, match="loopback"):
        CoreClient("https://example.com")


def test_expired_change_does_not_switch_source(rig: Rig) -> None:
    draft = rig.scheduler.create_draft(target="HERMES", actor_ref="test")
    rig.clock += timedelta(minutes=16)
    with pytest.raises(LedgerError, match="Preview again"):
        rig.scheduler.commit(
            draft_id=draft["draft"]["id"],
            confirmation_token=draft["confirmation_token"],
            confirmed_by="test",
        )
    assert rig.scheduler.status()["source"] == "WINDOWS"


def test_scheduler_migration_roundtrip_preserves_business_tables(rig: Rig) -> None:
    from alembic import command
    from alembic.config import Config
    from conftest import PROJECT_ROOT

    with sqlite3.connect(rig.db) as db:
        before = {
            table: db.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("portfolios", "accounts", "automation_policies", "job_runs")
        }
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{rig.db}")
    command.downgrade(config, "0036_no_investment_week")
    command.upgrade(config, "head")
    with sqlite3.connect(rig.db) as db:
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
    assert rig.scheduler.status()["source"] == "HERMES"
