from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from conftest import PROJECT_ROOT, migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerService
from investor_core.market_data import MarketDataService


def migrate_to(database_path: Path, revision: str) -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")
    command.upgrade(config, revision)


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
        "audit_events",
        "backups",
        "holding_snapshots",
        "instruments",
        "job_runs",
        "market_nav_snapshots",
        "market_data_source_health",
        "market_sync_runs",
        "market_nav_verifications",
        "portfolios",
        "schema_meta",
        "settings",
        "transaction_drafts",
        "transactions",
    }
    assert phase == ("2",)
    assert revision == ("0008_allocation_policy",)


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
            "0008_allocation_policy",
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
