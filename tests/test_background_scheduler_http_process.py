"""Real loopback HTTP + separate worker process, using only a temporary Core DB."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from conftest import PROJECT_ROOT, migrate_database

from investor_core.config import Environment, Settings
from investor_core.operations import OperationsService
from investor_core.scheduler import SchedulerService


def test_separate_worker_http_process_and_offline_restart(tmp_path: Path) -> None:
    database = tmp_path / "isolated.db"
    migrate_database(database)
    settings = Settings(environment=Environment.TEST, db_path=database)
    # Synthetic policy approval/cutover two minutes ago. Real HTTP and doctor execution below.
    before = datetime.now(UTC) - timedelta(minutes=2)
    operations = OperationsService(settings, now=lambda: before)
    draft = operations.create_policy_draft(
        job_name="SYSTEM_DOCTOR",
        enabled=True,
        schedule="* * * * *",
        timezone="Asia/Shanghai",
        config={"max_attempts": 2},
        reason="isolated process test",
    )
    operations.commit_policy_draft(
        draft_id=draft["draft"]["id"],
        confirmation_token=draft["confirmation_token"],
        confirmed_by="test",
    )
    scheduler = SchedulerService(operations)
    change = scheduler.create_draft(target="WINDOWS", actor_ref="test")
    scheduler.commit(
        draft_id=change["draft"]["id"],
        confirmation_token=change["confirmation_token"],
        confirmed_by="test",
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "INVESTOR_ENVIRONMENT": "test",
        "INVESTOR_DB_PATH": str(database),
    }
    command = [
        sys.executable,
        "-c",
        "import sys, uvicorn; uvicorn.run('investor_core.api.app:app', "
        "host='127.0.0.1', port=int(sys.argv[1]), log_level='error')",
        str(port),
    ]
    log_path = tmp_path / "core-process.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=log, stderr=log)
    worker = [
        sys.executable,
        "-m",
        "investor_core.background_worker",
        "--core-url",
        url,
        "--state",
        str(tmp_path / "worker.db"),
    ]
    try:
        with httpx.Client(base_url=url, trust_env=False, timeout=0.3) as client:
            for _ in range(40):
                if process.poll() is not None:
                    raise AssertionError(log_path.read_text(encoding="utf-8"))
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError(
                    "isolated Core did not start: " + log_path.read_text(encoding="utf-8")
                )
            assert client.get("/ready").status_code == 200
            first = subprocess.run(
                worker,
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            assert json.loads(first.stdout)["state"] == "OBSERVED"
            runs = client.get("/v1/automation-runs").json()["data"]["items"]
            assert len(runs) >= 2
            assert all(r["status"] == "SUCCESS" for r in runs)
            prior = {r["scheduled_for"]: r["id"] for r in runs}
            subprocess.run(
                worker, cwd=PROJECT_ROOT, env=env, capture_output=True, timeout=60, check=True
            )
            after = client.get("/v1/automation-runs").json()["data"]["items"]
            assert all(
                {r["scheduled_for"]: r["id"] for r in after}[k] == v for k, v in prior.items()
            )
            assert client.get("/v1/background-scheduler").json()["data"]["heartbeat"]
        process.terminate()
        process.wait(timeout=15)
        offline = subprocess.run(
            worker,
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        assert json.loads(offline.stdout)["state"] == "CORE_UNAVAILABLE"
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        log.close()
