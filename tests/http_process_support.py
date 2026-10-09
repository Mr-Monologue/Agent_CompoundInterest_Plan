"""Shared bounded readiness for isolated real HTTP process tests."""

import asyncio

import httpx


async def wait_for_ready(client, process, log_path, startup_seconds=30):
    """One cancellable deadline spans connect, headers and every body chunk."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + startup_seconds
    try:
        async with asyncio.timeout_at(deadline):
            while True:
                if process.poll() is not None:
                    raise AssertionError(log_path.read_text(encoding="utf-8"))
                try:
                    response = await client.get("/ready", timeout=None)
                    # Also reject a late response from an operation that delayed
                    # cancellation or completed without yielding to the timer.
                    if response.status_code == 200 and loop.time() < deadline:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
    except TimeoutError as exc:
        raise AssertionError(
            "isolated Core did not become ready: " + log_path.read_text(encoding="utf-8")
        ) from exc
