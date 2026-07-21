"""Local Core availability and guarded Windows on-demand recovery."""

from __future__ import annotations

import asyncio
import logging
import sys
from time import monotonic

import httpx

from investor_core.config import Settings

logger = logging.getLogger(__name__)


async def probe_core_ready(settings: Settings) -> bool:
    """Return whether the configured Core reports full readiness."""
    try:
        async with httpx.AsyncClient(base_url=settings.core_base_url, timeout=2.0) as client:
            response = await client.get("/ready")
            if not response.is_success:
                return False
            payload = response.json()
            return isinstance(payload, dict) and payload.get("status") == "PASS"
    except (httpx.HTTPError, ValueError):
        return False


async def start_windows_task(task_name: str) -> bool:
    """Ask Windows Task Scheduler to start the registered Core supervisor."""
    try:
        process = await asyncio.create_subprocess_exec(
            "schtasks.exe",
            "/Run",
            "/TN",
            task_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except OSError as exc:
        logger.warning("Could not invoke the Core scheduled task: %s", type(exc).__name__)
        return False
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        logger.warning("Core scheduled task start failed: %s", detail or process.returncode)
        return False
    return True


async def repair_windows_entrypoints(settings: Settings) -> bool:
    """Reinstall only the local package when a partial update removed its scripts."""
    project_root = settings.project_root.resolve()
    core_executable = project_root / ".venv" / "Scripts" / "investor-core.exe"
    if core_executable.exists():
        return True
    logger.warning("Investor Core entry point is missing; repairing the local environment")
    try:
        process = await asyncio.create_subprocess_exec(
            "uv",
            "sync",
            "--python",
            "3.11",
            "--reinstall-package",
            "value-dca-agent",
            cwd=str(project_root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
    except (OSError, TimeoutError) as exc:
        logger.warning("Could not repair the Core entry point: %s", type(exc).__name__)
        return False
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        logger.warning("Core entry-point repair failed: %s", detail or process.returncode)
        return False
    return core_executable.exists()


async def ensure_core_ready(settings: Settings) -> bool:
    """Start the managed Windows Core when enabled, then wait for readiness."""
    if await probe_core_ready(settings):
        return True
    if (
        not settings.core_autostart
        or sys.platform != "win32"
        or not settings.core_windows_task_name
    ):
        return False
    if not await repair_windows_entrypoints(settings):
        return False
    if not await start_windows_task(settings.core_windows_task_name):
        return False

    deadline = monotonic() + settings.core_start_timeout_seconds
    while monotonic() < deadline:
        await asyncio.sleep(0.5)
        if await probe_core_ready(settings):
            return True
    logger.warning("Investor Core did not become ready after the scheduled task was started")
    return False
