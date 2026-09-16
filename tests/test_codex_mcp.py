from __future__ import annotations

import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from investor_core.config import Settings, get_settings
from investor_core.version import __version__
from investor_mcp import server
from investor_mcp.check import check_connection

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("actor", ["hermes", "codex"])
def test_client_identity_preserves_draft_payload_and_confirmation(
    monkeypatch: pytest.MonkeyPatch, actor: Literal["hermes", "codex"]
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    monkeypatch.setattr(server, "get_settings", lambda: Settings(mcp_actor_ref=actor))

    async def capture(
        method: str, path: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        calls.append((method, path, payload))
        return {"ok": True}

    monkeypatch.setattr(server, "core_request", capture)
    asyncio.run(
        server.weekly_plan_draft_create(
            "200", "2026-09-14", "test-idempotency", portfolio_id="p", account_id="a"
        )
    )
    asyncio.run(server.external_subscription_draft_renew("existing-draft"))
    asyncio.run(server.transaction_draft_commit("draft", "test-token", "test-user"))
    assert calls == [
        (
            "POST",
            "/v1/weekly-plans",
            {
                "portfolio_id": "p",
                "account_id": "a",
                "contribution_amount": "200",
                "plan_date": "2026-09-14",
                "as_of_date": None,
                "idempotency_key": "test-idempotency",
                "actor_ref": actor,
            },
        ),
        ("POST", "/v1/external-subscription-drafts/existing-draft/renew", {"actor_ref": actor}),
        (
            "POST",
            "/v1/transaction-drafts/draft/commit",
            {"confirmation_token": "test-token", "confirmed_by": "test-user"},
        ),
    ]


def test_client_identity_is_configured_and_not_an_arbitrary_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVESTOR_MCP_ACTOR_REF", "codex")
    get_settings.cache_clear()
    try:
        assert get_settings().mcp_actor_ref == "codex"
        monkeypatch.setenv("INVESTOR_MCP_ACTOR_REF", "administrator")
        get_settings.cache_clear()
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        get_settings.cache_clear()


def exception_messages(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return " ".join(exception_messages(child) for child in error.exceptions)
    return str(error)


@pytest.mark.parametrize(
    ("ready", "version", "failure"),
    [("PASS", __version__, ""), ("FAIL", __version__, "readiness failed"),
     ("PASS", "0.0.0", "versions differ")],
)
def test_real_stdio_handshake_checks_health_without_business_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready: str, version: str, failure: str
) -> None:
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(("GET", self.path))
            if self.path == "/health":
                response = {"status": "ok", "version": version}
            elif self.path == "/ready":
                response = {"status": ready}
            else:
                self.send_error(404)
                return
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            requests.append(("POST", self.path))
            self.send_error(405)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    # The spawned MCP process sees only an isolated root and a loopback fake Core.
    monkeypatch.setenv("PYTHONPATH", str(PROJECT_ROOT / "src"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        async def run() -> dict[str, Any]:
            return await asyncio.wait_for(
                check_connection(
                    sys.executable,
                    args=["-m", "investor_mcp.server"],
                    project_root=tmp_path,
                    core_url=f"http://127.0.0.1:{httpd.server_port}",
                ),
                timeout=35,
            )

        if failure:
            with pytest.raises(Exception) as exc:
                asyncio.run(run())
            assert failure in exception_messages(exc.value)
        else:
            result = asyncio.run(run())
            assert result["status"] == "PASS"
            assert result["actor_ref"] == "codex"
            assert result["business_writes"] is False
            assert result["version"] == __version__
        assert requests == [("GET", "/health"), ("GET", "/ready")]
        assert not list(tmp_path.rglob("*.db"))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
