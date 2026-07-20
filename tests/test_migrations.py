from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from conftest import PROJECT_ROOT, migrate_database

from investor_core.config import Environment, Settings
from investor_core.ledger import LedgerService


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
        "portfolios",
        "schema_meta",
        "settings",
        "transaction_drafts",
        "transactions",
    }
    assert phase == ("1",)
    assert revision == ("0003_opening_position",)


def test_opening_position_migration_preserves_phase1_ledger_records(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_to(database_path, "0002_phase1")
    service = LedgerService(
        Settings(environment=Environment.TEST, db_path=database_path)
    )
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

    upgraded = LedgerService(
        Settings(environment=Environment.TEST, db_path=database_path)
    )
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
