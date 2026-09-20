from __future__ import annotations

import json
import sqlite3
from datetime import date
from hashlib import sha256
from pathlib import Path

from alembic import command
from alembic.config import Config
from conftest import PROJECT_ROOT, migrate_database
from test_planning import commit_buy, configured_services
from test_subscriptions import confirm, frozen_plan, legacy_net_transaction_draft, submit

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerService
from investor_core.market_data import MarketDataService
from investor_core.research import ResearchService


def migrate_to(database_path: Path, revision: str) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")
    command.upgrade(config, revision)


def downgrade_to(database_path: Path, revision: str) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")
    command.downgrade(config, revision)


def test_phase1_migration_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        phase = connection.execute("SELECT value FROM schema_meta WHERE key='phase'").fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert tables >= {
        "accounts",
        "alembic_version",
        "alerts",
        "audit_events",
        "automation_policies",
        "automation_policy_drafts",
        "automation_scheduler_snapshots",
        "backups",
        "cash_event_drafts",
        "cash_ledger_events",
        "market_research_evidence",
        "research_evidence_changes",
        "research_watchlist_entries",
        "research_watchlist_transition_drafts",
        "research_watchlist_transitions",
        "research_watchlist_review_snapshots",
        "research_collection_runs",
        "research_collection_items",
        "review_quality_snapshots",
        "research_source_config_drafts",
        "research_source_configs",
        "research_coverage_snapshots",
        "research_coverage_changes",
        "research_collection_tasks",
        "research_collection_claims",
        "research_collection_task_receipts",
        "research_connector_health_receipts",
        "market_discovery_runs",
        "market_discovery_items",
        "market_discovery_changes",
        "holding_snapshots",
        "instruments",
        "job_runs",
        "market_nav_snapshots",
        "market_data_source_health",
        "market_sync_runs",
        "market_nav_verifications",
        "notification_delivery_attempts",
        "notification_outbox",
        "notification_test_requests",
        "official_nav_backfill_batches",
        "review_action_decision_drafts",
        "review_action_decisions",
        "review_action_outcome_drafts",
        "review_action_outcomes",
        "review_trend_snapshots",
        "portfolios",
        "report_bundles",
        "runtime_mode_snapshots",
        "schema_meta",
        "settings",
        "strategy_assignments",
        "strategy_definitions",
        "strategy_instrument_configs",
        "strategy_versions",
        "investment_plans",
        "plan_execution_links",
        "weekly_plan_skip_drafts",
        "weekly_plan_partial_close_drafts",
        "weekly_report_drafts",
        "weekly_reports",
        "weekly_no_investment_drafts",
        "external_subscriptions",
        "external_subscription_confirmations",
        "external_subscription_drafts",
        "subscription_confirmation_transaction_links",
        "plan_items",
        "plan_revisions",
        "transaction_drafts",
        "transactions",
    }
    assert phase == ("3",)
    assert revision == ("0037_background_scheduler",)


def test_external_subscription_draft_renewal_migration_preserves_existing_drafts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0030_instrument_role_contract")
    payload_json = "{}"
    payload_hash = sha256(payload_json.encode("utf-8")).hexdigest()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO external_subscription_drafts (
                id, action, subscription_id, payload_json, payload_hash, status,
                idempotency_key, confirmation_digest, expires_at, created_at,
                committed_at, committed_entity_id, actor_ref
            ) VALUES (
                'legacy-expired-draft', 'SUBMIT', NULL, ?, ?, 'PENDING',
                'legacy-idempotency-key', 'legacy-digest',
                '2026-08-18T00:15:00Z', '2026-08-18T00:00:00Z',
                NULL, NULL, 'hermes'
            )
            """,
            (payload_json, payload_hash),
        )
        connection.commit()

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(external_subscription_drafts)")
        }
        row = connection.execute(
            """
            SELECT id, idempotency_key, payload_json, payload_hash, status,
                   renewed_at, renewal_count
            FROM external_subscription_drafts WHERE id='legacy-expired-draft'
            """
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"renewed_at", "renewal_count"} <= columns
    assert row == (
        "legacy-expired-draft",
        "legacy-idempotency-key",
        payload_json,
        payload_hash,
        "PENDING",
        None,
        0,
    )
    assert revision == ("0037_background_scheduler",)

    downgrade_to(database_path, "0030_instrument_role_contract")
    with sqlite3.connect(database_path) as connection:
        downgraded_columns = {
            str(column[1])
            for column in connection.execute("PRAGMA table_info(external_subscription_drafts)")
        }
        downgraded_row = connection.execute(
            """
            SELECT id, idempotency_key, payload_json, payload_hash, status
            FROM external_subscription_drafts WHERE id='legacy-expired-draft'
            """
        ).fetchone()
        downgraded_revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    assert "renewed_at" not in downgraded_columns
    assert "renewal_count" not in downgraded_columns
    assert downgraded_row == (
        "legacy-expired-draft",
        "legacy-idempotency-key",
        payload_json,
        payload_hash,
        "PENDING",
    )
    assert downgraded_revision == ("0030_instrument_role_contract",)


def test_weekly_report_migration_preserves_plans_and_downgrades(tmp_path: Path) -> None:
    database_path = tmp_path / "weekly-report-migration.db"
    migrate_to(database_path, "0034_partial_plan_closure")
    with sqlite3.connect(database_path) as connection:
        before_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    migrate_database(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(investment_plans)")
        }
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"period_start", "period_end"} <= columns
        assert {"weekly_report_drafts", "weekly_reports"} <= tables
    downgrade_to(database_path, "0034_partial_plan_closure")
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(investment_plans)")
        }
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert "period_start" not in columns and "period_end" not in columns
    assert "weekly_report_drafts" not in tables and "weekly_reports" not in tables
    assert before_tables <= tables
    assert revision == ("0034_partial_plan_closure",)


def test_no_investment_week_migration_preserves_existing_plans_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "no-investment-week-migration.db"
    migrate_to(database_path, "0035_weekly_plan_reports")
    with sqlite3.connect(database_path) as connection:
        before_count = connection.execute("SELECT COUNT(*) FROM investment_plans").fetchone()[0]
    migrate_database(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(investment_plans)")
        }
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        kinds = {
            row[0] for row in connection.execute("SELECT decision_kind FROM investment_plans")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert "decision_kind" in columns
    assert "weekly_no_investment_drafts" in tables
    assert kinds <= {"ALLOCATED_PLAN"}
    assert revision == ("0037_background_scheduler",)
    downgrade_to(database_path, "0035_weekly_plan_reports")
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(investment_plans)")
        }
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        after_count = connection.execute("SELECT COUNT(*) FROM investment_plans").fetchone()[0]
    assert "decision_kind" not in columns
    assert "weekly_no_investment_drafts" not in tables
    assert after_count == before_count


def test_confirmation_time_precision_revision_migration_upgrades_and_downgrades(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    _, _, service, portfolio_id, account_id, plan = frozen_plan(database_path)
    first_subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
        amount="50.00",
        key="migration-submit-one",
    )
    committed_draft = service.create_confirmation_draft(
        subscription_id=str(first_subscription["id"]),
        confirmed_at="2026-07-22T18:00:00+08:00",
        confirmed_at_precision="EXACT",
        confirmation_business_date="2026-07-22",
        nav_date="2026-07-22",
        nav="1",
        confirmed_shares="50",
        confirmed_amount="50",
        fee="0",
        refunded_amount="0",
        idempotency_key="migration-confirm-committed",
    )
    committed = service.commit_draft(
        draft_id=str(committed_draft["draft"]["id"]),
        confirmation_token=str(committed_draft["confirmation_token"]),
        confirmed_by="test-user",
    )
    confirmation_id = committed["subscription"]["confirmations"][0]["id"]

    _, _, second_service, second_portfolio, second_account, second_plan = frozen_plan(
        tmp_path / "draft.db"
    )
    second_subscription = submit(
        second_service,
        portfolio_id=second_portfolio,
        account_id=second_account,
        plan_id=str(second_plan["id"]),
        key="migration-submit-two",
    )
    pending = second_service.create_confirmation_draft(
        subscription_id=str(second_subscription["id"]),
        confirmed_at="2026-07-22T00:00:00+08:00",
        confirmed_at_precision="EXACT",
        confirmation_business_date="2026-07-22",
        nav_date="2026-07-22",
        nav="1",
        confirmed_shares="100",
        confirmed_amount="100",
        fee="0",
        refunded_amount="0",
        idempotency_key="migration-confirm-pending",
    )

    downgrade_to(database_path, "0031_external_subscription_draft_renewal")
    downgrade_to(tmp_path / "draft.db", "0031_external_subscription_draft_renewal")
    with sqlite3.connect(tmp_path / "draft.db") as connection:
        legacy_payload_json, legacy_payload_hash = connection.execute(
            "SELECT payload_json, payload_hash FROM external_subscription_drafts WHERE id=?",
            (pending["draft"]["id"],),
        ).fetchone()
    assert "confirmed_at_precision" not in legacy_payload_json

    migrate_database(database_path)
    migrate_database(tmp_path / "draft.db")

    with sqlite3.connect(database_path) as connection:
        confirmation_precision = connection.execute(
            """
            SELECT confirmed_at_precision
            FROM external_subscription_confirmations WHERE id=?
            """,
            (confirmation_id,),
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    with sqlite3.connect(tmp_path / "draft.db") as connection:
        draft_row = connection.execute(
            """
            SELECT id, idempotency_key, payload_json, payload_hash,
                   confirmed_at_precision, revised_at, revision_count
            FROM external_subscription_drafts WHERE id=?
            """,
            (pending["draft"]["id"],),
        ).fetchone()
    migrated_payload = json.loads(draft_row[2])
    assert confirmation_precision == ("EXACT",)
    assert revision == ("0037_background_scheduler",)
    assert draft_row[0] == pending["draft"]["id"]
    assert draft_row[1] == pending["draft"]["idempotency_key"]
    assert migrated_payload["confirmed_at_precision"] == "EXACT"
    assert draft_row[3] != legacy_payload_hash
    assert draft_row[4:] == ("EXACT", None, 0)

    downgrade_to(tmp_path / "draft.db", "0031_external_subscription_draft_renewal")
    with sqlite3.connect(tmp_path / "draft.db") as connection:
        downgraded_columns = {
            str(column[1])
            for column in connection.execute("PRAGMA table_info(external_subscription_drafts)")
        }
        restored_payload_json, restored_payload_hash = connection.execute(
            "SELECT payload_json, payload_hash FROM external_subscription_drafts WHERE id=?",
            (pending["draft"]["id"],),
        ).fetchone()
    assert {
        "confirmed_at_precision",
        "revised_at",
        "revision_count",
    }.isdisjoint(downgraded_columns)
    assert "confirmed_at_precision" not in restored_payload_json
    assert restored_payload_hash == legacy_payload_hash


def test_gross_transaction_migration_marks_and_preserves_legacy_net_draft(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    _, _, service, portfolio_id, account_id, plan = frozen_plan(
        database_path,
        amount="40.00",
    )
    subscription = submit(
        service,
        portfolio_id=portfolio_id,
        account_id=account_id,
        plan_id=str(plan["id"]),
        amount="40.00",
        key="migration-gross-submit",
    )
    confirmed = confirm(
        service,
        subscription_id=str(subscription["id"]),
        amount="39.97",
        shares="39.97",
        fee="0.03",
        key="migration-gross-confirm",
    )
    confirmation_id = str(confirmed["confirmations"][0]["id"])
    legacy = legacy_net_transaction_draft(
        database_path,
        service,
        confirmation_id=confirmation_id,
        idempotency_key="migration-gross-ledger",
    )
    draft_id = str(legacy["draft"]["id"])

    downgrade_to(database_path, "0032_confirmation_time_precision_revision")
    with sqlite3.connect(database_path) as connection:
        legacy_draft = connection.execute(
            """
            SELECT amount_minor, request_hash, confirmation_digest, idempotency_key
            FROM transaction_drafts WHERE id=?
            """,
            (draft_id,),
        ).fetchone()
        legacy_link = connection.execute(
            """
            SELECT confirmation_id, transaction_draft_id, plan_linked_amount_minor
            FROM subscription_confirmation_transaction_links WHERE confirmation_id=?
            """,
            (confirmation_id,),
        ).fetchone()
    assert legacy_draft[0] == 3997
    assert legacy_link == (confirmation_id, draft_id, 4000)

    migrate_database(database_path)
    with sqlite3.connect(database_path) as connection:
        draft_columns = {
            str(column[1]) for column in connection.execute("PRAGMA table_info(transaction_drafts)")
        }
        link_columns = {
            str(column[1])
            for column in connection.execute(
                "PRAGMA table_info(subscription_confirmation_transaction_links)"
            )
        }
        migrated_draft = connection.execute(
            """
            SELECT amount_minor, request_hash, confirmation_digest, idempotency_key,
                   origin, origin_reference_id, revised_at, revision_count
            FROM transaction_drafts WHERE id=?
            """,
            (draft_id,),
        ).fetchone()
        migrated_link = connection.execute(
            """
            SELECT gross_amount_minor, confirmed_amount_minor, fee_minor,
                   revised_at, revision_count
            FROM subscription_confirmation_transaction_links WHERE confirmation_id=?
            """,
            (confirmation_id,),
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"origin", "origin_reference_id", "revised_at", "revision_count"} <= draft_columns
    assert {
        "gross_amount_minor",
        "confirmed_amount_minor",
        "fee_minor",
        "revised_at",
        "revision_count",
    } <= link_columns
    assert migrated_draft[:4] == legacy_draft
    assert migrated_draft[4:] == (
        "EXTERNAL_SUBSCRIPTION",
        confirmation_id,
        None,
        0,
    )
    assert migrated_link == (4000, 3997, 3, None, 0)
    assert revision == ("0037_background_scheduler",)

    downgrade_to(database_path, "0032_confirmation_time_precision_revision")
    with sqlite3.connect(database_path) as connection:
        downgraded_draft_columns = {
            str(column[1]) for column in connection.execute("PRAGMA table_info(transaction_drafts)")
        }
        downgraded_link_columns = {
            str(column[1])
            for column in connection.execute(
                "PRAGMA table_info(subscription_confirmation_transaction_links)"
            )
        }
        restored_draft = connection.execute(
            """
            SELECT amount_minor, request_hash, confirmation_digest, idempotency_key
            FROM transaction_drafts WHERE id=?
            """,
            (draft_id,),
        ).fetchone()
    assert {"origin", "origin_reference_id", "revised_at", "revision_count"}.isdisjoint(
        downgraded_draft_columns
    )
    assert {
        "gross_amount_minor",
        "confirmed_amount_minor",
        "fee_minor",
        "revised_at",
        "revision_count",
    }.isdisjoint(downgraded_link_columns)
    assert restored_draft == legacy_draft


def test_instrument_role_contract_migration_renames_and_preserves_registration_role(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0029_weekly_plan_skip_reconfirmation")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO instruments (
                id, code, name, asset_type, currency, role, status, created_at
            ) VALUES (
                'instrument-role-contract', '040046', '华安纳斯达克100ETF联接A',
                'FUND', 'CNY', 'UNASSIGNED', 'ACTIVE', '2026-08-14T00:00:00Z'
            )
            """
        )
        connection.commit()

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(instruments)")}
        saved_role = connection.execute(
            "SELECT registration_role FROM instruments WHERE code = '040046'"
        ).fetchone()

    assert "registration_role" in columns
    assert "role" not in columns
    assert saved_role == ("UNASSIGNED",)

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")
    command.downgrade(config, "0029_weekly_plan_skip_reconfirmation")
    with sqlite3.connect(database_path) as connection:
        downgraded_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(instruments)")
        }
        downgraded_role = connection.execute(
            "SELECT role FROM instruments WHERE code = '040046'"
        ).fetchone()

    assert "role" in downgraded_columns
    assert "registration_role" not in downgraded_columns
    assert downgraded_role == ("UNASSIGNED",)


def test_opening_position_migration_preserves_phase1_ledger_records(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0002_phase1")
    service = LedgerService(Settings(environment=Environment.TEST, db_path=database_path))
    portfolio = service.create_portfolio(name="测试组合")
    account = service.create_account(
        portfolio_id=str(portfolio["id"]), name="测试账户", platform="模拟平台"
    )
    service.create_instrument(code="OLD001", name="已有基金")
    draft = service.create_transaction_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="OLD001",
        side="BUY",
        trade_date_value="2026-07-19",
        amount="100.00",
        nav="1.000000",
        shares="100.000000",
        platform="模拟平台",
        idempotency_key="existing-trade",
    )
    draft_data = draft["draft"]
    token = draft["confirmation_token"]
    assert isinstance(draft_data, dict)
    assert isinstance(token, str)
    service.commit_transaction_draft(
        draft_id=str(draft_data["id"]),
        confirmation_token=token,
        confirmed_by="test-user",
    )

    migrate_database(database_path)

    upgraded = LedgerService(Settings(environment=Environment.TEST, db_path=database_path))
    assert upgraded.list_transactions()[0]["kind"] == "TRADE"
    assert upgraded.list_holdings()[0]["total_shares"] == "100.000000"
    upgraded.create_instrument(code="NEW001", name="待导入基金")
    opening = upgraded.create_opening_position_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="NEW001",
        as_of_date_value="2026-07-20",
        total_shares="50.000000",
        cost_amount="60.00",
        platform="模拟平台",
        idempotency_key="new-opening",
    )
    assert opening["draft"]["action"] == "OPENING"


def test_market_nav_migration_preserves_committed_opening_position(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0003_opening_position")
    service = LedgerService(Settings(environment=Environment.TEST, db_path=database_path))
    portfolio = service.create_portfolio(name="个人投资组合")
    account = service.create_account(
        portfolio_id=str(portfolio["id"]),
        name="测试账户",
        platform="测试平台",
    )
    service.create_instrument(code="FUND001", name="测试基金A")
    opening = service.create_opening_position_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="FUND001",
        as_of_date_value="2026-07-17",
        total_shares="100.000000",
        average_cost_nav="1.250000",
        platform="测试平台",
        idempotency_key="opening-before-market-migration",
    )
    service.commit_opening_position_draft(
        draft_id=str(opening["draft"]["id"]),
        confirmation_token=str(opening["confirmation_token"]),
        confirmed_by="test-user",
    )
    before = service.list_holdings()

    migrate_database(database_path)

    after = LedgerService(
        Settings(environment=Environment.TEST, db_path=database_path)
    ).list_holdings()
    assert after == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_nav_snapshots").fetchone() == (0,)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0037_background_scheduler",
        )


def test_source_lineage_migration_backfills_eastmoney_aliases(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0006_market_nav_verification")
    service = LedgerService(Settings(environment=Environment.TEST, db_path=database_path))
    service.create_instrument(code="FUND001", name="测试基金")
    with sqlite3.connect(database_path) as connection:
        instrument_id = connection.execute(
            "SELECT id FROM instruments WHERE code='FUND001'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO market_nav_snapshots (
                id, instrument_id, nav_date, nav_micros, currency, source_type,
                source_name, source_ref, verification_status, observed_at,
                ingested_at, record_hash
            ) VALUES (
                'snapshot-1', ?, '2026-07-21', 1500000, 'CNY', 'AGGREGATOR',
                '天天基金', 'https://fund.eastmoney.com/FUND001', 'UNVERIFIED',
                '2026-07-21T22:00:00Z', '2026-07-21T22:00:00Z', 'hash-1'
            )
            """,
            (instrument_id,),
        )
        connection.commit()

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT source_lineage FROM market_nav_snapshots WHERE id='snapshot-1'"
        ).fetchone() == ("EASTMONEY",)


def test_watchlist_review_cycle_migration_preserves_and_backfills_entries(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0020_watchlist_research_outcomes")
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    ledger = LedgerService(settings)
    portfolio = ledger.create_portfolio(name="迁移研究组合")
    instrument = ledger.create_instrument(code="FUND001", name="迁移观察基金")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO research_watchlist_entries (
                id, portfolio_id, instrument_id, state, review_due_date,
                latest_reason, created_at, updated_at
            ) VALUES (
                'entry-1', ?, ?, 'OBSERVING', '2026-08-31',
                '已有观察状态', '2026-07-01T00:00:00Z',
                '2026-07-02T00:00:00Z'
            )
            """,
            (portfolio["id"], instrument["id"]),
        )
        connection.execute(
            """
            INSERT INTO research_watchlist_transition_drafts (
                id, portfolio_id, instrument_id, previous_state, new_state,
                review_due_date, reason, status, confirmation_token_digest,
                facts_hash, created_by, created_at, expires_at, committed_at
            ) VALUES (
                'draft-1', ?, ?, 'CANDIDATE', 'OBSERVING', '2026-08-31',
                '已有确认观察', 'COMMITTED', 'digest', 'facts',
                'user', '2026-07-02T00:00:00Z',
                '2026-07-02T00:15:00Z', '2026-07-02T00:01:00Z'
            )
            """,
            (portfolio["id"], instrument["id"]),
        )
        connection.execute(
            """
            INSERT INTO research_watchlist_transitions (
                id, draft_id, entry_id, previous_state, new_state,
                review_due_date, reason, facts_hash, confirmed_by, confirmed_at
            ) VALUES (
                'transition-1', 'draft-1', 'entry-1', 'CANDIDATE',
                'OBSERVING', '2026-08-31', '已有确认观察', 'facts',
                'user', '2026-07-02T00:01:00Z'
            )
            """
        )
        connection.commit()

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT state, review_due_date, observation_started_at, last_reviewed_at
            FROM research_watchlist_entries WHERE id='entry-1'
            """
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert row == (
        "OBSERVING",
        "2026-08-31",
        "2026-07-02T00:01:00Z",
        None,
    )
    assert revision == ("0037_background_scheduler",)
    snapshot = ResearchService(settings).build_watchlist_review_snapshot(
        portfolio_id=str(portfolio["id"]),
        as_of_date=date(2026, 9, 1),
    )
    assert snapshot["summary"]["due_count"] == 1
    assert snapshot["holdings_changed"] is False


def test_delivery_receipt_migration_upgrades_existing_operations_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0013_hermes_scheduler_bridge")

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        outbox_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(notification_outbox)")
        }
        attempt_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='notification_delivery_attempts'
            """
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"dispatched_at", "delivered_at", "provider_message_id"} <= outbox_columns
    assert attempt_table == ("notification_delivery_attempts",)
    assert revision == ("0037_background_scheduler",)


def test_alert_recovery_migration_resolves_only_recovered_job_runs(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0024_research_collection_orchestration")
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO job_runs (
                id, job_name, scheduled_for, idempotency_key, status,
                started_at, finished_at, input_json, output_json,
                error_code, error_summary, trace_id, attempt_count,
                max_attempts, heartbeat_at, next_retry_at
            ) VALUES (?, 'DAILY_MARKET_SYNC', ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, 3, ?, ?)
            """,
            [
                (
                    "recovered-run",
                    "2026-08-03T13:00:00Z",
                    "recovered-key",
                    "DEGRADED",
                    "2026-08-03T13:10:06Z",
                    "2026-08-03T13:10:08Z",
                    '{"data_quality":"WARNING","execution_status":"SUCCESS",'
                    '"reason_code":"MARKET_SYNC_COMPLETED"}',
                    None,
                    None,
                    "recovered-trace",
                    2,
                    "2026-08-03T13:10:08Z",
                    None,
                ),
                (
                    "failed-run",
                    "2026-08-04T13:00:00Z",
                    "failed-key",
                    "FAILED",
                    "2026-08-04T13:00:00Z",
                    "2026-08-04T13:00:02Z",
                    '{"error_code":"PROVIDER_CANARY_FAILED"}',
                    "PROVIDER_CANARY_FAILED",
                    "canary failed",
                    "failed-trace",
                    1,
                    "2026-08-04T13:00:02Z",
                    "2026-08-04T13:05:02Z",
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO alerts (
                id, portfolio_id, job_run_id, code, severity, status,
                fingerprint, context_json, occurrence_count, created_at,
                last_seen_at, acknowledged_at, acknowledged_by
            ) VALUES (?, NULL, ?, 'PROVIDER_CANARY_FAILED', 'CRITICAL', 'OPEN',
                      ?, '{}', 1, ?, ?, NULL, NULL)
            """,
            [
                (
                    "stale-alert",
                    "recovered-run",
                    "stale-fingerprint",
                    "2026-08-03T13:00:02Z",
                    "2026-08-03T13:00:02Z",
                ),
                (
                    "live-alert",
                    "failed-run",
                    "live-fingerprint",
                    "2026-08-04T13:00:02Z",
                    "2026-08-04T13:00:02Z",
                ),
            ],
        )
        connection.commit()

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, status, resolved_at, resolved_by, resolution_code,
                   resolution_context_json
            FROM alerts ORDER BY id
            """
        ).fetchall()
        audits = connection.execute(
            """
            SELECT entity_id FROM audit_events
            WHERE action='AUTOMATION_ALERT_AUTO_RESOLVED'
            """
        ).fetchall()
    assert rows[0] == ("live-alert", "OPEN", None, None, None, None)
    assert rows[1][:5] == (
        "stale-alert",
        "RESOLVED",
        "2026-08-03T13:10:08Z",
        "system:migration-0025",
        "JOB_RUN_RECOVERED",
    )
    assert '"attempt_count":2' in rows[1][5]
    assert audits == [("stale-alert",)]


def test_satellite_signal_migration_preserves_alert_resolution_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0025_alert_recovery_resolution")

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        alert_columns = {row[1] for row in connection.execute("PRAGMA table_info(alerts)")}
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {
        "satellite_signal_policy_drafts",
        "satellite_signal_policies",
        "satellite_signal_snapshots",
    } <= tables
    assert {
        "resolved_at",
        "resolved_by",
        "resolution_code",
        "resolution_context_json",
    } <= alert_columns
    assert revision == ("0037_background_scheduler",)


def test_external_subscription_migration_preserves_v030_facts_and_starts_empty(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0027_partial_plan_execution")
    with sqlite3.connect(database_path) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "investment_plans",
                "plan_execution_links",
                "transactions",
                "holding_snapshots",
            )
        }

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        new_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "external_subscriptions",
                "external_subscription_confirmations",
                "external_subscription_drafts",
                "subscription_confirmation_transaction_links",
                "weekly_plan_skip_drafts",
                "weekly_plan_partial_close_drafts",
            )
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert after == before
    assert set(new_counts.values()) == {0}
    assert revision == ("0037_background_scheduler",)


def test_partial_plan_closure_migration_never_auto_closes_historical_partial_plan(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    ledger, planning, portfolio_id, account_id = configured_services(database_path)
    created = planning.create_draft(
        portfolio_id=portfolio_id,
        account_id=account_id,
        contribution_amount="100.00",
        plan_date_value="2026-07-21",
        idempotency_key="historical-partial-migration",
        as_of_date_value="2026-07-21",
    )
    plan_id = str(created["plan"]["id"])
    planning.freeze(
        plan_id=plan_id,
        confirmation_token=str(created["confirmation_token"]),
        confirmed_by="test-user",
    )
    trade = commit_buy(
        ledger,
        portfolio_id=portfolio_id,
        account_id=account_id,
        instrument_code="CORE01",
        trade_date="2026-07-21",
        amount="40.00",
        key="historical-partial-buy",
    )
    planning.link_transaction(
        plan_id=plan_id,
        transaction_id=str(trade["transaction"]["id"]),
        confirmed_by="test-user",
    )

    downgrade_to(database_path, "0033_external_subscription_gross_transaction")
    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        plan = connection.execute(
            """
            SELECT status, closed_at, closure_reason_code, abandoned_amount_minor
            FROM investment_plans WHERE id=?
            """,
            (plan_id,),
        ).fetchone()
        close_draft_count = connection.execute(
            "SELECT COUNT(*) FROM weekly_plan_partial_close_drafts"
        ).fetchone()[0]
    assert plan == ("PARTIALLY_EXECUTED", None, None, None)
    assert close_draft_count == 0


def test_allocation_policy_migration_seeds_existing_portfolios_with_audit(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0007_source_lineage")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO portfolios (id, name, base_currency, status, created_at)
            VALUES (
                'portfolio-existing', '个人投资组合', 'CNY', 'ACTIVE',
                '2026-07-20T00:00:00Z'
            )
            """
        )
        connection.commit()

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        policy = connection.execute(
            """
            SELECT version, value_json, approved_by
            FROM settings
            WHERE key = 'allocation_policy:portfolio-existing'
              AND status = 'ACTIVE'
            """
        ).fetchone()
        audit = connection.execute(
            """
            SELECT action
            FROM audit_events
            WHERE entity_id = 'allocation_policy:portfolio-existing'
            """
        ).fetchone()

    assert policy is not None
    assert policy[0] == 1
    assert '"core_target_pct": "65.00"' in policy[1]
    assert policy[2] == "system:approved-strategy-v1.6"
    assert audit == ("ALLOCATION_POLICY_INITIALIZED",)


def test_strategy_instance_migration_preserves_policy_without_inventing_buy_targets(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0008_allocation_policy")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO portfolios (id, name, base_currency, status, created_at)
            VALUES ('portfolio-existing', '已有组合', 'CNY', 'ACTIVE', '2026-07-20T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO instruments (
                id, code, name, asset_type, currency, role, status, created_at
            ) VALUES (
                'instrument-existing', 'HISTORY01', '历史持仓基金', 'FUND',
                'CNY', 'CORE', 'ACTIVE', '2026-07-20T00:00:00Z'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO settings (
                key, version, value_json, value_hash, status, approved_by,
                approved_at, created_at
            ) VALUES (
                'allocation_policy:portfolio-existing', 1,
                ?,
                'legacy-hash', 'ACTIVE', 'test-user',
                '2026-07-20T00:00:00Z', '2026-07-20T00:00:00Z'
            )
            """,
            (
                '{"policy_id":"value-dca-v1.6","core_target_pct":"65.00",'
                '"satellite_target_pct":"35.00","tolerance_pct":"10.00",'
                '"transition_trigger_pct":"15.00",'
                '"transition_exit_core_min_pct":"55.00",'
                '"transition_exit_satellite_max_pct":"45.00",'
                '"transition_principle":"INCREMENTAL_FUNDS_FIRST",'
                '"automatic_selling_allowed":false}',
            ),
        )
        connection.commit()

    migrate_database(database_path)

    with sqlite3.connect(database_path) as connection:
        assignment = connection.execute(
            """
            SELECT a.portfolio_id, d.strategy_key, v.version, a.approved_by
            FROM strategy_assignments a
            JOIN strategy_versions v ON v.id = a.strategy_version_id
            JOIN strategy_definitions d ON d.id = v.strategy_definition_id
            WHERE a.status = 'ACTIVE'
            """
        ).fetchone()
        config = connection.execute(
            """
            SELECT role, contribution_eligible
            FROM strategy_instrument_configs
            WHERE instrument_id = 'instrument-existing'
            """
        ).fetchone()

    assert assignment == ("portfolio-existing", "value-dca", "1.6", "test-user")
    assert config == ("CORE", 0)


def test_new_portfolio_does_not_receive_an_implicit_strategy(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    service = LedgerService(Settings(environment=Environment.TEST, db_path=database_path))
    portfolio = service.create_portfolio(name="新用户组合")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM strategy_assignments WHERE portfolio_id = ?",
            (portfolio["id"],),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM settings WHERE key = ?",
            (f"allocation_policy:{portfolio['id']}",),
        ).fetchone() == (0,)


def test_market_sync_migration_preserves_existing_holding_and_nav(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0004_market_nav")
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    ledger = LedgerService(settings)
    portfolio = ledger.create_portfolio(name="个人投资组合")
    account = ledger.create_account(
        portfolio_id=str(portfolio["id"]),
        name="测试账户",
        platform="测试平台",
    )
    ledger.create_instrument(code="FUND001", name="测试基金A")
    opening = ledger.create_opening_position_draft(
        portfolio_id=str(portfolio["id"]),
        account_id=str(account["id"]),
        instrument_code="FUND001",
        as_of_date_value="2026-07-17",
        total_shares="100.000000",
        average_cost_nav="1.250000",
        platform="测试平台",
        idempotency_key="opening-before-sync-migration",
    )
    ledger.commit_opening_position_draft(
        draft_id=str(opening["draft"]["id"]),
        confirmation_token=str(opening["confirmation_token"]),
        confirmed_by="test-user",
    )
    with sqlite3.connect(database_path) as connection:
        instrument_id = connection.execute(
            "SELECT id FROM instruments WHERE code='FUND001'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO market_nav_snapshots (
                id, instrument_id, nav_date, nav_micros, currency, source_type,
                source_name, verification_status, observed_at, ingested_at, record_hash
            ) VALUES (
                'existing-snapshot', ?, '2026-07-21', 1534500, 'CNY', 'AGGREGATOR',
                'existing-source', 'UNVERIFIED', '2026-07-21T22:00:00Z',
                '2026-07-21T22:00:00Z', 'existing-hash'
            )
            """,
            (instrument_id,),
        )
        connection.commit()
    holdings_before = ledger.list_holdings()

    migrate_database(database_path)

    assert LedgerService(settings).list_holdings() == holdings_before
    snapshots = MarketDataService(settings).list_nav_snapshots(instrument_code="FUND001")
    assert len(snapshots) == 1
    assert snapshots[0]["nav"] == "1.534500"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_sync_runs").fetchone() == (0,)
