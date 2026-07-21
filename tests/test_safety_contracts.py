from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_skill_has_valid_minimal_frontmatter() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", skill, re.DOTALL)

    assert match is not None
    frontmatter = match.group(1)
    assert "name: value-dca-investor" in frontmatter
    assert "description:" in frontmatter


def test_sell_approval_is_not_execution() -> None:
    safety = (PROJECT_ROOT / "skills/value-dca-investor/references/safety-policy.md").read_text(
        encoding="utf-8"
    )
    soul = (PROJECT_ROOT / "SOUL.md").read_text(encoding="utf-8")

    assert "Only step 4 changes holdings" in safety
    assert "真实\n卖出成交是三个不同状态" in soul


def test_skill_never_offers_an_unavailable_investor_capability() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(encoding="utf-8")

    assert "Check the tools actually available" in skill
    assert "Never\n   name, offer, or imply an Investor capability" in skill
    assert "Attribute rules precisely" in skill


def test_opening_positions_are_not_fabricated_buy_transactions() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(encoding="utf-8")
    safety = (PROJECT_ROOT / "skills/value-dca-investor/references/safety-policy.md").read_text(
        encoding="utf-8"
    )

    assert "never invent missing values or represent the import as a" in skill
    assert "historical `BUY`" in skill
    assert "An opening position is a historical balance import, not a purchase" in safety
    assert "opening_position_draft_commit" in safety


def test_skill_uses_saved_context_instead_of_asking_users_for_uuids() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(encoding="utf-8")

    assert "Use `investment_context_get` before asking for or exposing" in skill
    assert "Never ask the user to memorize or repeatedly paste UUIDs" in skill
    assert "investment_context_set" in skill


def test_windows_installer_keeps_external_actions_disabled() -> None:
    installer = (PROJECT_ROOT / "install-windows.ps1").read_text(encoding="utf-8")

    assert "Cron, Weixin and broker connections remain disabled" in installer
    assert "investor db migrate" in installer
    assert "hermes mcp test investor_core" in installer


def test_cron_examples_are_disabled() -> None:
    for path in (PROJECT_ROOT / "cron").rglob("*.json"):
        assert '"enabled": false' in path.read_text(encoding="utf-8")
