from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from investor_core.hermes_delivery import resolve_delivery_target, send_with_hermes


def test_origin_resolves_to_profile_home_channel(tmp_path: Path) -> None:
    profile = tmp_path / "hermes" / "profiles" / "investor"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("WEIXIN_HOME_CHANNEL=user@example\n", encoding="utf-8")

    target = resolve_delivery_target(
        "origin",
        profile="investor",
        environment={"LOCALAPPDATA": str(tmp_path)},
    )

    assert target == "weixin:user@example"


def test_hermes_exit_zero_is_provider_acceptance_not_read_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["hermes"], 0, stdout="sent", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    evidence = send_with_hermes(
        profile="investor",
        target="weixin:user@example",
        message="test",
    )

    assert evidence["acknowledgement"] == "CLI_EXIT_ZERO"
    assert evidence["evidence_level"] == "PROVIDER_ACCEPTED_NOT_HUMAN_READ"
    assert evidence["native_provider_message_id"] is False
