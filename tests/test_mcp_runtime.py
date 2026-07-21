from __future__ import annotations

import asyncio
from pathlib import Path

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

    async def repaired(_settings: Settings) -> bool:
        return True

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runtime, "probe_core_ready", readiness)
    monkeypatch.setattr(runtime, "repair_windows_entrypoints", repaired)
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


def test_windows_core_autostart_stops_when_entrypoint_repair_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def unavailable(_settings: Settings) -> bool:
        return False

    async def failed_repair(_settings: Settings) -> bool:
        return False

    async def unexpected_start(_task_name: str) -> bool:
        raise AssertionError("scheduled task must not start after repair failure")

    monkeypatch.setattr(runtime, "probe_core_ready", unavailable)
    monkeypatch.setattr(runtime, "repair_windows_entrypoints", failed_repair)
    monkeypatch.setattr(runtime, "start_windows_task", unexpected_start)
    monkeypatch.setattr(runtime.sys, "platform", "win32")

    settings = Settings(
        core_autostart=True,
        core_windows_task_name="ValueDCAInvestorCore",
    )
    assert asyncio.run(runtime.ensure_core_ready(settings)) is False


def test_windows_entrypoint_repair_reinstalls_only_local_package(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}
    executable = tmp_path / ".venv" / "Scripts" / "investor-core.exe"

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            executable.parent.mkdir(parents=True)
            executable.touch()
            return b"", b""

    async def create_process(*args: str, **kwargs: object) -> FakeProcess:
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        return FakeProcess()

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", create_process)

    settings = Settings(project_root=tmp_path)
    assert asyncio.run(runtime.repair_windows_entrypoints(settings)) is True
    assert captured["args"] == (
        "uv",
        "sync",
        "--python",
        "3.11",
        "--reinstall-package",
        "value-dca-agent",
    )
    assert captured["cwd"] == str(tmp_path.resolve())
