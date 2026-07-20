"""SQLite connection policy and deterministic readiness checks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from investor_core.config import Settings

if TYPE_CHECKING:
    from investor_core.health import CheckResult


REQUIRED_TABLES = {
    "alembic_version",
    "accounts",
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
EXPECTED_ALEMBIC_REVISION = "0003_opening_position"


def ensure_database_parent(settings: Settings) -> None:
    if str(settings.db_path) != ":memory:":
        Path(settings.db_path).resolve().parent.mkdir(parents=True, exist_ok=True)


def create_sqlite_engine(settings: Settings) -> Engine:
    engine = create_engine(
        settings.database_url,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragmas(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def check_database(settings: Settings) -> list[CheckResult]:
    # Local import avoids a runtime cycle between health models and database checks.
    from investor_core.health import CheckResult, CheckStatus

    if str(settings.db_path) != ":memory:" and not Path(settings.db_path).resolve().exists():
        return [
            CheckResult(
                name="database",
                status=CheckStatus.FAIL,
                message="Database file does not exist; run `investor db migrate`",
                details={"path": str(Path(settings.db_path).resolve())},
            )
        ]

    engine = create_sqlite_engine(settings)
    try:
        with engine.connect() as connection:
            quick_check = connection.execute(text("PRAGMA quick_check")).scalar_one()
            journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
            table_names = set(inspect(connection).get_table_names())
            missing = sorted(REQUIRED_TABLES - table_names)
            current_revision = (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                if "alembic_version" in table_names
                else None
            )

        checks: list[CheckResult] = [
            CheckResult(
                name="sqlite-integrity",
                status=(CheckStatus.PASS if quick_check == "ok" else CheckStatus.FAIL),
                message=f"SQLite quick_check: {quick_check}",
            ),
            CheckResult(
                name="sqlite-wal",
                status=(
                    CheckStatus.PASS if str(journal_mode).lower() == "wal" else CheckStatus.FAIL
                ),
                message=f"SQLite journal mode: {journal_mode}",
            ),
        ]
        schema_current = not missing and current_revision == EXPECTED_ALEMBIC_REVISION
        if missing:
            schema_message = f"Missing required tables: {', '.join(missing)}"
        elif current_revision != EXPECTED_ALEMBIC_REVISION:
            schema_message = (
                f"Database revision {current_revision or 'none'} is not current; "
                "run `investor db migrate`"
            )
        else:
            schema_message = "Opening-position schema is current"
        checks.append(
            CheckResult(
                name="database-schema",
                status=CheckStatus.PASS if schema_current else CheckStatus.FAIL,
                message=schema_message,
                details={
                    "table_count": len(table_names),
                    "current_revision": current_revision,
                    "expected_revision": EXPECTED_ALEMBIC_REVISION,
                },
            )
        )
        return checks
    except SQLAlchemyError as exc:
        return [
            CheckResult(
                name="database",
                status=CheckStatus.FAIL,
                message="Database connection or schema check failed",
                details={"error_type": type(exc).__name__},
            )
        ]
    finally:
        engine.dispose()
