"""Readiness covers the complete response inside one startup deadline."""

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from http_process_support import wait_for_ready


@pytest.mark.parametrize("late", [False, True])
def test_split_response_deadline_and_transport_cleanup(tmp_path, late):
    events = []
    log = tmp_path / "process.log"
    log.write_text("SYNTHETIC isolated server", encoding="utf-8")

    class SplitBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(3):
                await asyncio.sleep(0.06 if late else 0)
                events.append("chunk")
                yield b"x"

        async def aclose(self):
            events.append("stream_closed")

    async def respond(request):
        assert request.url.path == "/ready"
        return httpx.Response(200, stream=SplitBody())

    async def run():
        async with httpx.AsyncClient(
            base_url="http://synthetic.invalid", transport=httpx.MockTransport(respond)
        ) as client:
            await wait_for_ready(
                client, SimpleNamespace(poll=lambda: None), log,
                startup_seconds=0.1 if late else 5,
            )

    if late:
        with pytest.raises(AssertionError, match="did not become ready"):
            asyncio.run(run())
        assert events.count("chunk") < 3
    else:
        asyncio.run(run())
        assert events.count("chunk") == 3
    assert events[-1] == "stream_closed"


def test_late_200_cannot_win_race_against_deadline_callback(tmp_path):
    log = tmp_path / "process.log"
    log.write_text("SYNTHETIC", encoding="utf-8")

    class BlockingResponse:
        async def get(self, path, **kwargs):
            # A deliberately non-cooperative dependency: no yield to timer.
            time.sleep(0.03)
            return httpx.Response(200)

    with pytest.raises(AssertionError, match="did not become ready"):
        asyncio.run(wait_for_ready(
            BlockingResponse(), SimpleNamespace(poll=lambda: None), log, startup_seconds=0.01,
        ))


def test_exited_process_fails_immediately_with_original_log(tmp_path):
    log = tmp_path / "process.log"
    log.write_text("SYNTHETIC crashed", encoding="utf-8")
    with pytest.raises(AssertionError, match="SYNTHETIC crashed"):
        asyncio.run(wait_for_ready(None, SimpleNamespace(poll=lambda: 1), log))
