"""Hermes CLI delivery adapter with conservative provider-acceptance receipts."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from investor_core.ledger import JsonDict, LedgerError

HOME_CHANNELS = (
    ("WEIXIN_HOME_CHANNEL", "weixin"),
    ("TELEGRAM_HOME_CHANNEL", "telegram"),
    ("WHATSAPP_HOME_CHANNEL", "whatsapp"),
    ("SLACK_HOME_CHANNEL", "slack"),
    ("TEAMS_HOME_CHANNEL", "teams"),
    ("GOOGLE_CHAT_HOME_CHANNEL", "google_chat"),
    ("EMAIL_HOME_ADDRESS", "email"),
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def resolve_delivery_target(
    requested_target: str,
    *,
    profile: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Resolve Core's portable `origin` target to one configured Hermes home channel."""
    target = requested_target.strip()
    if target and target.lower() != "origin":
        return target
    env = dict(environment or os.environ)
    override = env.get("INVESTOR_HERMES_DELIVERY_TARGET", "").strip()
    if override:
        return override
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise LedgerError(
            "DELIVERY_TARGET_UNRESOLVED",
            "LOCALAPPDATA is unavailable and no Hermes delivery target override is configured",
        )
    profile_env = _read_env(Path(local_app_data) / "hermes" / "profiles" / profile / ".env")
    for variable, platform in HOME_CHANNELS:
        value = (env.get(variable) or profile_env.get(variable) or "").strip()
        if value:
            return f"{platform}:{value}"
    raise LedgerError(
        "DELIVERY_TARGET_UNRESOLVED",
        "origin cannot be resolved because the Hermes profile has no configured home channel",
        details={"profile": profile},
    )


def send_with_hermes(
    *,
    profile: str,
    target: str,
    message: str,
    command: Sequence[str] | None = None,
    timeout_seconds: int = 90,
) -> JsonDict:
    """Send once and return receipt evidence without claiming a human read receipt."""
    if not message.strip():
        raise LedgerError("DELIVERY_PAYLOAD_EMPTY", "notification message is empty")
    executable = list(command or ("hermes",))
    args = [*executable, "-p", profile, "send", "--to", target, message]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise LedgerError(
            "HERMES_CLI_NOT_FOUND",
            "Hermes CLI executable was not found",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LedgerError(
            "HERMES_SEND_TIMEOUT",
            "Hermes CLI delivery timed out",
        ) from exc
    evidence: dict[str, Any] = {
        "adapter": "value-dca-hermes-send",
        "adapter_version": "1",
        "acknowledgement": "CLI_EXIT_ZERO" if result.returncode == 0 else "CLI_EXIT_NONZERO",
        "evidence_level": "PROVIDER_ACCEPTED_NOT_HUMAN_READ",
        "native_provider_message_id": False,
        "target": target,
        "exit_code": result.returncode,
        "stdout_sha256": _digest(result.stdout),
        "stderr_sha256": _digest(result.stderr),
    }
    if result.returncode != 0:
        raise LedgerError(
            "HERMES_SEND_FAILED",
            "Hermes CLI did not accept the outbound message",
            details=evidence,
        )
    return evidence
