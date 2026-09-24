"""Run the existing isolated journey over real loopback HTTP, never production."""

from __future__ import annotations

import gc
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import test_stage_business_flow as journey
import uvicorn


@contextmanager
def loopback_client(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    assert sock.getsockname()[1] != 8710
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Isolated HTTP server failed to start")
            time.sleep(0.02)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False
        ) as client:
            yield client
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Isolated server did not stop")


if __name__ == "__main__":
    journey.TestClient = loopback_client
    with tempfile.TemporaryDirectory(prefix="value-dca-flow-") as name:
        for outcome in ("executed", "partial", "skip"):
            root = Path(name) / outcome
            root.mkdir()
            journey.test_complete_http_business_journey_and_lost_commit_response(root, outcome)
            print(
                f"PASS: loopback HTTP isolated {outcome}; "
                "lost-response retries did not duplicate facts"
            )

        gc.collect()  # Release fixture SQLite cycles before Windows temporary cleanup.
