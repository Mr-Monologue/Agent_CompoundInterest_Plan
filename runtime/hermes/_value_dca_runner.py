"""Console-free Hermes Cron bridge for the local Investor Core HTTP API."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _request(path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url = os.environ.get("INVESTOR_CORE_URL", "http://127.0.0.1:8710").rstrip("/")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Investor Core HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Investor Core unavailable: {exc.reason}") from exc
    if not bool(result.get("ok")):
        raise RuntimeError(json.dumps(result.get("error", result), ensure_ascii=False))
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Investor Core returned an invalid response")
    return data


def run_job(job_name: str) -> int:
    try:
        result = _request(
            "/v1/automation-runs",
            {
                "job_name": job_name,
                "scheduled_for": None,
                "actor_ref": "hermes-cron",
            },
        )
        display_text = str(result.get("display_text", "[SILENT]"))
        if display_text != "[SILENT]":
            print(display_text)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def retry_due() -> int:
    try:
        result = _request("/v1/automation-recovery/run")
        display_text = str(result.get("display_text", "[SILENT]"))
        if display_text != "[SILENT]":
            print(display_text)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
