from __future__ import annotations

import runpy
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from investor_core.hermes_delivery import resolve_delivery_target, send_with_hermes
from investor_core.ledger import LedgerError


def test_origin_resolves_to_profile_home_channel(tmp_path: Path) -> None:
    profile = tmp_path / "hermes" / "profiles" / "investor"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("WEIXIN_HOME_CHANNEL=user@example\n", encoding="utf-8")

    target = resolve_delivery_target(
        "origin",
        profile="investor",
        environment={"LOCALAPPDATA": str(tmp_path)},
    )

    assert target == "weixin"


def test_hermes_exit_zero_is_provider_acceptance_not_read_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["hermes"], 0, stdout="sent", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    evidence = send_with_hermes(
        profile="investor",
        target="weixin",
        message="test",
    )

    assert evidence["acknowledgement"] == "CLI_EXIT_ZERO"
    assert evidence["evidence_level"] == "PROVIDER_ACCEPTED_NOT_HUMAN_READ"
    assert evidence["native_provider_message_id"] is False


@pytest.mark.parametrize(
    ("stderr", "expected_code"),
    [
        (
            "Weixin send failed: iLink sendmessage rate limited: ret=-2 errmsg=prepare failed",
            "HERMES_WEIXIN_SESSION_STALE",
        ),
        ("cooldown active: rate limited", "HERMES_CHANNEL_RATE_LIMITED"),
        ("unknown provider failure", "HERMES_SEND_FAILED"),
    ],
)
def test_hermes_failures_are_classified_for_durable_retry(
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    expected_code: str,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["hermes"], 1, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(LedgerError) as exc:
        send_with_hermes(
            profile="investor",
            target="weixin",
            message="test",
        )
    assert exc.value.code == expected_code


def test_copied_notification_worker_has_no_project_package_dependency() -> None:
    project_root = Path(__file__).resolve().parents[1]
    worker = (
        project_root / "runtime" / "hermes" / "value_dca_notification_delivery.py"
    ).read_text(encoding="utf-8")
    helper = (
        project_root / "runtime" / "hermes" / "_value_dca_delivery.py"
    ).read_text(encoding="utf-8")

    assert "from investor_core" not in worker
    assert "import investor_core" not in worker
    assert "from investor_core" not in helper
    assert "import investor_core" not in helper
    assert "return platform" in helper


def test_copied_runtime_helper_resolves_weixin_as_home_platform(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    namespace: dict[str, Any] = runpy.run_path(
        str(project_root / "runtime" / "hermes" / "_value_dca_delivery.py"),
        run_name="value_dca_delivery_test",
    )
    resolve = cast(Callable[..., str], namespace["resolve_delivery_target"])
    profile = tmp_path / "hermes" / "profiles" / "investor"
    profile.mkdir(parents=True)
    (profile / ".env").write_text(
        "WEIXIN_HOME_CHANNEL=opaque-ilink-chat-id@im.wechat\n",
        encoding="utf-8",
    )

    assert resolve(
        "origin",
        profile="investor",
        environment={"LOCALAPPDATA": str(tmp_path)},
    ) == "weixin"
