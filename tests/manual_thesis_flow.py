"""Reuse HTTP governance acceptance against a real isolated Core loopback server."""

import gc
import tempfile
from pathlib import Path

import test_thesis_governance as journey
from manual_stage_flow import loopback_client

if __name__ == "__main__":
    journey.TestClient = loopback_client
    with tempfile.TemporaryDirectory(prefix="value-dca-thesis-") as name:
        journey.test_scope_conditions_missing_reason_and_http_confirmation(Path(name))
        gc.collect()
    print(
        "PASS: isolated HTTP draft, confirmation, readback; original reason unknown"
    )
