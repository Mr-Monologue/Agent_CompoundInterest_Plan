from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_ledger import build_service
from test_weekly_reports import terminal_plan

from investor_core.api.app import create_app
from investor_core.daily_client import WORKFLOWS, AssistantError, DailyClient
from investor_core.weekly_reports import WeeklyReportService


def dump(path: Path) -> list[str]:
    with sqlite3.connect(path) as db:
        return list(db.iterdump())


def test_context_resolve_is_pure_and_respects_saved_choice(tmp_path: Path) -> None:
    ledger, c = build_service(tmp_path)
    web = TestClient(create_app(ledger.settings))
    before = dump(ledger.settings.db_path)
    resolved = web.get("/v1/investment-context/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["data"]["source"] == "UNAMBIGUOUS"
    assert dump(ledger.settings.db_path) == before
    second = ledger.create_account(
        portfolio_id=c["portfolio"]["id"], name="second", platform="test"
    )
    assert web.get("/v1/investment-context/resolve").status_code == 409
    ledger.set_investment_context(portfolio_id=c["portfolio"]["id"], account_id=second["id"])
    before = dump(ledger.settings.db_path)
    assert web.get("/v1/investment-context/resolve").json()["data"]["account"]["id"] == second["id"]
    assert dump(ledger.settings.db_path) == before


def test_daily_queries_preview_and_expired_report_preserve_all_rows(tmp_path: Path) -> None:
    settings, _pid, _aid, plan_id = terminal_plan(tmp_path / "isolated.db", outcome="SKIPPED")
    reports = WeeklyReportService(settings)
    created = reports.create_draft(plan_id=plan_id, idempotency_key="read-only-report")
    with sqlite3.connect(settings.db_path) as db:
        db.execute(
            "UPDATE weekly_report_drafts SET expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(), created["draft"]["id"]),
        )
    client = DailyClient(
        client=TestClient(create_app(settings)), journal=tmp_path / "unused.sqlite3"
    )
    before = dump(settings.db_path)
    investment = client.investment()
    assert investment["brief"]["valuation"]["data_quality"] != "PASS"
    assert "净值日期" in investment["display_text"]
    assert client.plan("200")["data_quality"] != "PASS"
    assert client.week()["report_drafts"][plan_id][0]["status"] == "EXPIRED"
    assert client.get("/v1/weekly-report-drafts/" + created["draft"]["id"])["status"] == "EXPIRED"
    assert client.research("医疗")["causal_claim"] == "NOT_ESTABLISHED"
    assert dump(settings.db_path) == before
    assert not (tmp_path / "unused.sqlite3").exists()


@pytest.mark.parametrize("budget", ["0", "-1", "NaN", "Infinity", "1.001", ""])
def test_budget_is_explicit_positive_finite(budget: str) -> None:
    with pytest.raises(AssistantError):
        DailyClient().plan(budget)


def test_lost_write_response_cannot_be_replayed_after_restart(tmp_path: Path) -> None:
    calls = []

    def lost(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("response lost")

    journal = tmp_path / "journal.sqlite3"
    for _ in range(2):
        client = DailyClient(
            client=httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(lost)),
            journal=journal,
        )
        with pytest.raises(AssistantError):
            client.workflow("report-draft", {"idempotency_key": "same-intent"}, subject="plan-1")
    assert len(calls) == 1
    assert b"confirmation_token" not in journal.read_bytes()


def test_concurrent_write_intent_is_dispatched_once(tmp_path: Path) -> None:
    calls = []

    def accepted(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "data": {"status": "COMMITTED"}})

    def invoke(_: int) -> None:
        client = DailyClient(
            client=httpx.Client(
                base_url="http://localhost", transport=httpx.MockTransport(accepted)
            ),
            journal=tmp_path / "journal.sqlite3",
        )
        with suppress(AssistantError):
            client.workflow(
                "report-commit",
                {"confirmation_token": "private-secret", "confirmed_by": "test"},
                subject="draft-1",
                confirmation="确认",
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(invoke, range(4)))
    assert len(calls) == 1
    assert b"private-secret" not in (tmp_path / "journal.sqlite3").read_bytes()


def test_financial_confirmation_and_nested_commit_route(tmp_path: Path) -> None:
    calls = []

    def accepted(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = DailyClient(
        client=httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(accepted)),
        journal=tmp_path / "journal.sqlite3",
    )
    payload = {"confirmation_token": "token", "confirmed_by": "test"}
    with pytest.raises(AssistantError):
        client.workflow(
            "transaction-commit", payload, subject="draft", parent="shares", confirmation="确认"
        )
    assert not calls
    client.workflow(
        "transaction-commit", payload, subject="draft", parent="shares", confirmation="确认记录"
    )
    assert (
        calls[0].url.path
        == "/v1/external-subscription-confirmations/shares/transaction-drafts/draft/commit"
    )


def test_named_workflows_exist_in_core(tmp_path: Path) -> None:
    import re

    ledger, _ = build_service(tmp_path)
    paths = TestClient(create_app(ledger.settings)).get("/openapi.json").json()["paths"]
    normalized = {re.sub(r"\{[^}]+\}", "{}", p) for p, verbs in paths.items() if "post" in verbs}
    assert all(re.sub(r"\{[^}]+\}", "{}", p) in normalized for p, _, _ in WORKFLOWS.values())


def test_no_remote_core_or_business_refresh() -> None:
    with pytest.raises(AssistantError):
        DailyClient("https://example.com")
    with pytest.raises(AssistantError):
        DailyClient().workflow("market-sync", {})


def test_real_http_entry_queries_and_preview(tmp_path: Path) -> None:
    import os
    import socket
    import subprocess
    import sys
    import time

    settings, _pid, _aid, _plan = terminal_plan(tmp_path / "real-http.db", outcome="SKIPPED")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(
        os.environ,
        INVESTOR_DB_PATH=str(settings.db_path),
        INVESTOR_PORT=str(port),
        INVESTOR_ENVIRONMENT="test",
        INVESTOR_CORE_AUTOSTART="false",
        INVESTOR_CORE_WINDOWS_TASK_NAME="",
        PYTHONUTF8="1",
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "from investor_core.api.app import main; main()"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = DailyClient(f"http://127.0.0.1:{port}")
    try:
        for _ in range(60):
            if process.poll() is not None:
                pytest.fail("isolated server exited before readiness")
            try:
                if client.get("/health")["status"] == "ok":
                    break
            except AssistantError:
                time.sleep(0.2)
        else:
            pytest.fail("isolated HTTP server did not start")
        before = dump(settings.db_path)
        assert client.investment()["brief"]["valuation"]["data_quality"] != "PASS"
        assert client.plan("200")["display_text"]
        assert client.week()["plans"]
        assert client.schema("transaction-commit")["schema"]["required"]
        assert dump(settings.db_path) == before
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=10)


def test_system_preserves_failure_history_dates_and_duplicates(monkeypatch) -> None:
    from datetime import date

    client = DailyClient()
    today = date.today().isoformat()
    run = {
        "scheduled_for": today + "T00:00:00+08:00",
        "status": "DEGRADED",
        "input": {"portfolio_id": None, "scheduler_source": "WINDOWS"},
        "output": {"reason_code": "RISK_SCAN_PARTIAL"},
    }
    responses = {
        "/health": {"version": "0.33.0"},
        "/ready": {"status": "PASS"},
        "/v1/background-scheduler": {
            "source": "WINDOWS",
            "heartbeat": {"received_at": today, "failures": [{"code": "CORE_UNAVAILABLE"}]},
            "anomalies": ["WORKER_FAILURES_RECORDED"],
            "cutover_at": today + "T00:00:00+08:00",
            "policies": [
                {
                    "schedule": "0 0 * * *",
                    "timezone": "Asia/Shanghai",
                    "job_name": "SYSTEM_DOCTOR",
                    "portfolio_id": None,
                    "enabled": True,
                }
            ],
        },
        "/v1/automation-runs": {"items": [run, run]},
        "/v1/market-data/status": {
            "runs": [
                {
                    "status": "PASS",
                    "completed_at": today,
                    "details": {
                        "items": [
                            {
                                "instrument_code": "FUND",
                                "nav_date": "2026-01-01",
                                "snapshot": {"data_quality": "WARNING"},
                            }
                        ]
                    },
                }
            ]
        },
    }
    monkeypatch.setattr(client, "get", lambda path, **kwargs: responses[path])
    result = client.system(today)
    assert result["occurrences"][0]["occurrence_count"] == 2
    assert "WARNING" in result["display_text"] and "2026-01-01" in result["display_text"]
    assert "历史工作进程异常数: 1" in result["display_text"]


def test_research_does_not_convert_name_or_recent_nav_into_buy_signal(monkeypatch) -> None:
    client = DailyClient()
    monkeypatch.setattr(client, "scope", lambda: {"portfolio_id": "p", "account_id": "a"})
    responses = {
        "/v1/portfolio-brief": {
            "as_of_date": "2026-01-02",
            "valuation": {
                "positions": [
                    {
                        "holding": {"instrument_code": "MED"},
                        "market_value": "100",
                        "weight_pct": "20",
                        "data_quality": "WARNING",
                        "nav_snapshot": {
                            "nav_date": "2026-01-01",
                            "source_ref": "https://example.org/nav",
                        },
                    }
                ]
            },
        },
        "/v1/strategy-assignment": {
            "instruments": [
                {
                    "instrument_code": "MED",
                    "instrument_name": "医疗混合",
                    "strategy_role": "SATELLITE",
                    "contribution_eligible": False,
                    "benchmark_code": None,
                    "thesis_status": "ACTIVE",
                }
            ]
        },
    }
    monkeypatch.setattr(client, "get", lambda path, **kwargs: responses[path])
    result = client.research("医疗")
    assert result["lookthrough_exposure"] == "UNKNOWN"
    assert result["causal_claim"] == "NOT_ESTABLISHED"
    assert "当前不具备定投资格" in result["display_text"]
    assert "https://example.org/nav" in result["display_text"]
