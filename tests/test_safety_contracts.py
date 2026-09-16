from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_versioned_operations_skill_governs_gross_cost_and_partial_closure() -> None:
    skill = (PROJECT_ROOT / "skills/investor-core-operations/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "actual gross cash paid" in skill
    assert "Never add or deduct the fee twice" in skill
    assert "weekly_plan_partial_close_draft_create" in skill
    assert "weekly_plan_partial_close_draft_commit" in skill
    assert "PARTIALLY_EXECUTED_CLOSED" in skill
    assert "Never fabricate execution" in skill
    assert "A formal weekly report must bind to one exact `weekly_plan_id`" in skill
    assert "never silently use an adjacent date" in skill
    assert "does not send WeChat" in skill
    assert "## Recording an explicit no-investment week" in skill
    assert "decision_kind=NO_INVESTMENT" in skill
    assert "Do not create an allocation plan merely" in " ".join(skill.split())


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
    assert "report\nthe tool mismatch and stop" in skill
    assert "calculate a per-fund split" in skill
    assert "model-derived substitute" in skill


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

    assert "scheduler reconciliation through the investor Agent" in installer
    assert "Broker connections and automatic trading remain disabled" in installer
    assert "investor db migrate" in installer
    assert "hermes mcp test investor_core" in installer


def test_skill_requires_core_market_calculations_and_source_evidence() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(encoding="utf-8")
    policy_path = PROJECT_ROOT / "skills/value-dca-investor/references/data-quality-policy.md"
    policy = policy_path.read_text(encoding="utf-8")

    assert "market_nav_snapshot_record" in skill
    assert "market-data synchronization capability" in skill
    assert "portfolio_valuation_get" in skill
    assert "never derive those values in prose" in skill
    assert "market_nav_verification_record" in skill
    assert "never copy a primary-provider value into the verification call" in skill
    assert "same upstream publisher" in skill
    assert "missing or stale NAV" in policy
    assert "same-date, same-value `MATCH`" in policy
    assert "must then remain absent" in policy


def test_skill_does_not_invent_allocation_or_sell_triggers() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(encoding="utf-8")
    policy_path = PROJECT_ROOT / "skills/value-dca-investor/references/data-quality-policy.md"
    policy = policy_path.read_text(encoding="utf-8")

    assert "Never describe an allocation as too high, too low" in skill
    assert "require the exact Core rule result and reason code" in skill
    assert "Never claim a scheduled report will run or fail" in skill
    assert "prefer `portfolio_brief_get`" in skill
    assert "return\n    `display_text` verbatim as the entire answer" in skill
    assert "AKShare, 东方财富 and 天天基金" in skill
    assert "`ROLE_UNASSIGNED`" in skill
    assert "`strategy_instrument_role_draft_create` only after" in skill
    assert "`registration_role` is registration\nmetadata" in skill
    assert "deprecated\ncompatibility alias with the same draft-only behavior" in skill
    assert "observations, not allocation or\nsell rules" in policy


def test_skill_separates_public_strategy_instance_and_plan_execution() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(encoding="utf-8")
    safety = (PROJECT_ROOT / "skills/value-dca-investor/references/safety-policy.md").read_text(
        encoding="utf-8"
    )

    assert "Keep public strategy rules separate" in skill
    assert "does not assign a portfolio role or make the instrument eligible" in skill
    assert "Portfolio-local instrument configuration is available only" in skill
    assert "`NO_ELIGIBLE_INSTRUMENT` item reserves the role amount" in skill
    assert "Never infer contribution eligibility" in skill
    assert "Reserved funds do not change executable projected allocation" in skill
    assert "Treat `FROZEN` as an\napproved plan, not a brokerage execution" in skill
    assert "Never treat a preview, DRAFT, or FROZEN plan as a purchase" in safety


def test_market_discovery_and_review_actions_preserve_governance_boundaries() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(encoding="utf-8")
    safety = (PROJECT_ROOT / "skills/value-dca-investor/references/safety-policy.md").read_text(
        encoding="utf-8"
    )
    templates = (
        PROJECT_ROOT / "skills/value-dca-investor/references/output-templates.md"
    ).read_text(encoding="utf-8")

    assert "never turn model opinion into source evidence" in skill
    assert "observations, not\nrankings or recommendations" in skill
    assert "never change an instrument's\nrole, thesis, contribution eligibility" in skill
    assert "only after confirmation with the\nmatching token" in skill
    assert "explicit registered instrument codes stored in its\nconfirmed local policy" in safety
    assert "must not register an instrument, change contribution eligibility" in safety
    assert "A state transition, added or removed flag" in skill
    assert "Never invent a review score, causal explanation" in skill
    assert "must never\ntranslate that change into a rotation" in safety
    assert "research watchlist is portfolio-local and empty by default" in skill
    assert "`ADOPTED` means accepted for continued research" in skill
    assert "Record a review-action outcome only after" in skill
    assert "An `ADOPTED` watchlist state is not strategy membership" in safety
    assert "`COMPLETED` review outcome is not proof that a strategy works" in safety
    assert "Use `research_collection_run_record` only after" in skill
    assert "temporal association across strategy instances is never causal evidence" in skill
    assert "External research collection runs are source-attributed ingestion receipts" in safety
    assert "Review-quality snapshots and `REVIEW_QUALITY_SNAPSHOT` automation" in safety
    assert "Research-source capability is portfolio-local and empty by default" in skill
    assert "never place a secret, token or credential value in" in skill
    assert "Research-source configurations are local capability declarations" in safety
    assert "证据覆盖与待采集任务" in templates
    assert "Never expose its `claim_token`" in skill
    assert "Research task claims are short-lived connector leases" in safety
    assert "连接器健康不是来源独立性" in templates
    assert "call `investment_workspace_get` first" in skill
    assert "workspace is a read-only projection" in safety
    assert "Hermes investment workspace" in templates
    assert "not permission to call that tool" in templates


def test_cron_examples_are_disabled() -> None:
    for path in (PROJECT_ROOT / "cron").rglob("*.json"):
        assert '"enabled": false' in path.read_text(encoding="utf-8")


def test_external_subscription_date_only_and_revision_skill_contract() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert 'confirmed_at_precision="DATE_ONLY"' in skill
    assert "never invent midnight and describe\nit as a real confirmation time" in skill
    assert "never the normalized internal timestamp" in skill
    assert "Draft revision is separate from creation, renewal, and commit" in skill
    assert "Never use a new idempotency\nkey" in skill
    assert "require a fresh explicit confirmation before commit" in skill


def test_external_subscription_gross_transaction_skill_contract() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "submitted gross\ncash, confirmed net amount" in skill
    assert "must not be added or deducted again" in skill
    assert "keep trade date\nequal to NAV date" in skill
    assert "external_subscription_transaction_draft_revise" in skill
    assert "Never use the\ngeneric transaction commit tool" in skill
    assert "create no transaction, holding, cash or plan-execution fact" in skill


def test_automation_skill_keeps_cron_read_only_and_silent() -> None:
    skill = (PROJECT_ROOT / "skills/value-dca-investor/SKILL.md").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "cron/agent-jobs/automation-report-delivery.example.json").read_text(
        encoding="utf-8"
    )

    assert "Never claim a job is scheduled merely" in skill
    assert "require an active Core policy with `enabled=true`" in skill
    assert "return exactly `[SILENT]`" in skill
    assert "Never let a scheduled Agent call a mutation tool" in skill
    assert "Never call a write tool" in template
