from __future__ import annotations

import asyncio

from investor_core.config import Settings
from investor_mcp import runtime


def test_core_autostart_is_disabled_by_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def unavailable(_settings: Settings) -> bool:
        return False

    async def unexpected_start(_task_name: str) -> bool:
        raise AssertionError("scheduled task must not start when autostart is disabled")

    monkeypatch.setattr(runtime, "probe_core_ready", unavailable)
    monkeypatch.setattr(runtime, "start_windows_task", unexpected_start)

    assert asyncio.run(runtime.ensure_core_ready(Settings())) is False


def test_windows_core_autostart_waits_for_readiness(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    probes = iter([False, False, True])
    started: list[str] = []

    async def readiness(_settings: Settings) -> bool:
        return next(probes)

    async def start(task_name: str) -> bool:
        started.append(task_name)
        return True

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runtime, "probe_core_ready", readiness)
    monkeypatch.setattr(runtime, "start_windows_task", start)
    monkeypatch.setattr(runtime.asyncio, "sleep", no_wait)
    monkeypatch.setattr(runtime.sys, "platform", "win32")

    settings = Settings(
        core_autostart=True,
        core_windows_task_name="ValueDCAInvestorCore",
        core_start_timeout_seconds=2,
    )
    assert asyncio.run(runtime.ensure_core_ready(settings)) is True
    assert started == ["ValueDCAInvestorCore"]
