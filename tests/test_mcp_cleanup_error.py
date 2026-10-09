"""Transport shutdown must not replace the operation error or fake success."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import anyio
import pytest

from investor_core.version import __version__
from investor_mcp import check


@pytest.mark.parametrize("primary_kind", ["timeout", "group", "cancel", "readiness", "none"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_connection_preserves_primary_and_reports_cleanup_only_failure(
    tmp_path, monkeypatch, primary_kind, cleanup_fails
):
    timeout = TimeoutError("SYNTHETIC initialization timeout")
    primary = {
        "timeout": timeout,
        "group": ExceptionGroup("SYNTHETIC session", [timeout]),
        "cancel": asyncio.CancelledError("SYNTHETIC cancellation"),
    }.get(primary_kind)
    secondary = ExceptionGroup("SYNTHETIC shutdown", [anyio.BrokenResourceError()])
    events = []

    @asynccontextmanager
    async def transport(parameters):
        assert parameters.env["INVESTOR_CORE_AUTOSTART"] == "false"
        try:
            yield None, None
        finally:
            events.append("transport_closed")
            if cleanup_fails:
                raise secondary

    class Session:
        def __init__(self, reader, writer, *, read_timeout_seconds):
            assert read_timeout_seconds == timedelta(seconds=20)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            events.append("session_closed")

        async def initialize(self):
            if primary is not None:
                raise primary
            return SimpleNamespace(instructions="SYNTHETIC")

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in check.REQUIRED_TOOLS])

        async def call_tool(self, name, arguments):
            assert name == "system_health_get" and arguments == {"detail_level": "full"}
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    "ok": True,
                    "data": {
                        "adapter": {"actor_ref": "codex", "version": __version__},
                        "health": {"version": __version__},
                        "ready": {"status": "FAIL" if primary_kind == "readiness" else "PASS"},
                    },
                },
            )

    monkeypatch.setattr(check, "stdio_client", transport)
    monkeypatch.setattr(check, "ClientSession", Session)
    operation = check.check_connection("SYNTHETIC", project_root=tmp_path, core_url="unused")
    if primary is not None:
        with pytest.raises(type(primary)) as raised:
            asyncio.run(operation)
        assert raised.value is primary
        if cleanup_fails:
            assert raised.value.__cause__ is secondary
    elif primary_kind == "readiness":
        with pytest.raises(RuntimeError, match="Core readiness failed") as raised:
            asyncio.run(operation)
        if cleanup_fails:
            assert raised.value.__cause__ is secondary
    elif cleanup_fails:
        with pytest.raises(ExceptionGroup) as raised:
            asyncio.run(operation)
        assert raised.value is secondary
    else:
        assert asyncio.run(operation)["status"] == "PASS"
    assert events == ["session_closed", "transport_closed"]


def test_shutdown_group_that_already_contains_original_keeps_both_errors(monkeypatch):
    original = TimeoutError("SYNTHETIC timeout")
    secondary = anyio.BrokenResourceError()
    combined = ExceptionGroup("SYNTHETIC shutdown", [ExceptionGroup("body", [original]), secondary])

    @asynccontextmanager
    async def transport(_):
        try:
            yield None, None
        finally:
            raise combined

    monkeypatch.setattr(check, "stdio_client", transport)

    async def run():
        async with check._stdio_connection(None):
            raise original

    with pytest.raises(ExceptionGroup) as raised:
        asyncio.run(run())
    assert raised.value is combined
    assert raised.value.exceptions[1] is secondary


@pytest.mark.parametrize("grouped", [False, True])
def test_new_cancellation_during_shutdown_is_never_replaced(monkeypatch, grouped):
    cancellation = asyncio.CancelledError("SYNTHETIC outer cancellation")
    closing = (
        BaseExceptionGroup("SYNTHETIC cancellation group", [cancellation])
        if grouped else cancellation
    )

    @asynccontextmanager
    async def transport(_):
        try:
            yield None, None
        finally:
            raise closing

    monkeypatch.setattr(check, "stdio_client", transport)

    async def run():
        async with check._stdio_connection(None):
            raise TimeoutError("SYNTHETIC original timeout")

    with pytest.raises(type(closing)) as raised:
        asyncio.run(run())
    assert raised.value is closing
