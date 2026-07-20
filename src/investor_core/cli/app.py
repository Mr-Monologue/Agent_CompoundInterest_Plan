"""Operational CLI for deterministic jobs and diagnostics."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated, NoReturn
from uuid import uuid4

import typer
from alembic import command
from alembic.config import Config

from investor_core.config import get_settings
from investor_core.database import ensure_database_parent
from investor_core.health import build_doctor_report
from investor_core.ledger import LedgerError, LedgerService
from investor_core.version import __version__

app = typer.Typer(no_args_is_help=True, help="Operate the Value DCA investor core.")
db_app = typer.Typer(no_args_is_help=True, help="Manage the local database schema.")
setup_app = typer.Typer(no_args_is_help=True, help="Create the first portfolio and account.")
instrument_app = typer.Typer(no_args_is_help=True, help="Manage the local instrument registry.")
ledger_app = typer.Typer(no_args_is_help=True, help="Inspect holdings and committed transactions.")
opening_app = typer.Typer(no_args_is_help=True, help="Import confirmed opening positions.")
app.add_typer(db_app, name="db")
app.add_typer(setup_app, name="setup")
app.add_typer(instrument_app, name="instrument")
app.add_typer(ledger_app, name="ledger")
app.add_typer(opening_app, name="opening")


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def alembic_config() -> Config:
    settings = get_settings()
    config = Config(str(project_root() / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


@db_app.command("migrate")
def migrate() -> None:
    """Create or upgrade the database to the latest migration."""
    settings = get_settings()
    ensure_database_parent(settings)
    command.upgrade(alembic_config(), "head")
    typer.echo("Database migration complete.")


@db_app.command("backup")
def backup_database(
    output: Annotated[
        Path,
        typer.Option("--output", help="Destination path for a consistent SQLite backup."),
    ],
) -> None:
    """Create and verify a consistent SQLite backup without changing the ledger."""
    settings = get_settings()
    source = settings.db_path.resolve()
    destination = output.resolve()
    if not source.exists():
        raise typer.BadParameter(f"database does not exist: {source}", param_hint="--output")
    if source == destination:
        raise typer.BadParameter("backup destination must differ from the database")
    if destination.exists():
        raise typer.BadParameter(f"backup destination already exists: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with (
            closing(sqlite3.connect(source)) as source_connection,
            closing(sqlite3.connect(temporary)) as backup_connection,
        ):
            source_connection.backup(backup_connection)
            quick_check = backup_connection.execute("PRAGMA quick_check").fetchone()
            if quick_check != ("ok",):
                raise RuntimeError(f"backup integrity check failed: {quick_check}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    typer.echo(
        json.dumps(
            {
                "ok": True,
                "source": str(source),
                "backup": str(destination),
                "quick_check": "ok",
            },
            ensure_ascii=False,
        )
    )


def emit_ledger_result(operation: object) -> None:
    typer.echo(json.dumps(operation, ensure_ascii=False, indent=2))


def emit_ledger_error(error: LedgerError) -> NoReturn:
    typer.echo(
        json.dumps(
            {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                },
            },
            ensure_ascii=False,
        ),
        err=True,
    )
    raise typer.Exit(code=1)


@setup_app.command("init")
def setup_init(
    portfolio_name: Annotated[
        str, typer.Option(help="Portfolio display name.")
    ] = "个人投资组合",
    account_name: Annotated[str, typer.Option(help="Account display name.")] = "默认账户",
    platform: Annotated[str, typer.Option(help="Broker or fund platform name.")] = "未配置",
    currency: Annotated[str, typer.Option(help="Three-letter currency code.")] = "CNY",
) -> None:
    """Idempotently create one portfolio and one account."""
    service = LedgerService(get_settings())
    try:
        portfolio = service.create_portfolio(name=portfolio_name, base_currency=currency)
        account = service.create_account(
            portfolio_id=str(portfolio["id"]),
            name=account_name,
            platform=platform,
            currency=currency,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, "portfolio": portfolio, "account": account})


@instrument_app.command("add")
def instrument_add(
    code: Annotated[str, typer.Argument(help="Fund, ETF or other instrument code.")],
    name: Annotated[str, typer.Option(help="Instrument display name.")],
    asset_type: Annotated[str, typer.Option(help="FUND, ETF, STOCK, INDEX or CASH.")] = "FUND",
    role: Annotated[str, typer.Option(help="CORE, SATELLITE or UNASSIGNED.")] = "UNASSIGNED",
    currency: Annotated[str, typer.Option(help="Three-letter currency code.")] = "CNY",
) -> None:
    """Idempotently register an instrument for transaction recording."""
    try:
        result = LedgerService(get_settings()).create_instrument(
            code=code,
            name=name,
            asset_type=asset_type,
            role=role,
            currency=currency,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, "instrument": result})


@instrument_app.command("list")
def instrument_list() -> None:
    """List registered instruments."""
    emit_ledger_result({"ok": True, "items": LedgerService(get_settings()).list_instruments()})


@ledger_app.command("holdings")
def ledger_holdings() -> None:
    """List the latest reconstructed holding for each account and instrument."""
    emit_ledger_result({"ok": True, "items": LedgerService(get_settings()).list_holdings()})


@ledger_app.command("transactions")
def ledger_transactions(
    limit: Annotated[int, typer.Option(min=1, max=500, help="Maximum records.")] = 100,
) -> None:
    """List committed trades and reversals."""
    try:
        items = LedgerService(get_settings()).list_transactions(limit=limit)
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, "items": items})


@opening_app.command("draft")
def opening_draft(
    portfolio_id: Annotated[str, typer.Option(help="Existing portfolio ID.")],
    account_id: Annotated[str, typer.Option(help="Existing account ID.")],
    instrument_code: Annotated[str, typer.Option(help="Registered non-index instrument code.")],
    as_of_date: Annotated[str, typer.Option(help="Position date in YYYY-MM-DD format.")],
    total_shares: Annotated[str, typer.Option(help="Exact platform-reported shares.")],
    platform: Annotated[str, typer.Option(help="Source platform name.")],
    idempotency_key: Annotated[str, typer.Option(help="Unique source message or import key.")],
    cost_amount: Annotated[
        str, typer.Option(help="Platform-reported total cost; mutually exclusive with cost NAV.")
    ] = "",
    average_cost_nav: Annotated[
        str,
        typer.Option(
            help="Platform-reported per-share average cost; mutually exclusive with total cost."
        ),
    ] = "",
    note: Annotated[str, typer.Option(help="Optional evidence or import note.")] = "",
) -> None:
    """Create an expiring opening-position import draft without changing holdings."""
    try:
        result = LedgerService(get_settings()).create_opening_position_draft(
            portfolio_id=portfolio_id,
            account_id=account_id,
            instrument_code=instrument_code,
            as_of_date_value=as_of_date,
            total_shares=total_shares,
            platform=platform,
            idempotency_key=idempotency_key,
            cost_amount=cost_amount or None,
            average_cost_nav=average_cost_nav or None,
            note=note or None,
            actor_ref="cli",
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, **result})


@opening_app.command("commit")
def opening_commit(
    draft_id: Annotated[str, typer.Argument(help="Opening-position draft ID.")],
    confirmation_token: Annotated[str, typer.Option(help="One-time draft token.")],
    confirmed_by: Annotated[str, typer.Option(help="Explicitly confirming user reference.")],
) -> None:
    """Commit one matching opening-position draft after explicit confirmation."""
    try:
        result = LedgerService(get_settings()).commit_opening_position_draft(
            draft_id=draft_id,
            confirmation_token=confirmation_token,
            confirmed_by=confirmed_by,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, **result})


@app.command()
def doctor(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable report."),
    ] = False,
) -> None:
    """Check Python, SQLite, WAL and the required schema."""
    report = build_doctor_report(get_settings())
    if json_output:
        typer.echo(json.dumps(report.model_dump(mode="json"), ensure_ascii=False))
    else:
        typer.echo(f"Value DCA doctor: {report.status} (v{report.version})")
        for check in report.checks:
            typer.echo(f"[{check.status}] {check.name}: {check.message}")
    if report.status == "FAIL":
        raise typer.Exit(code=1)


def main() -> None:
    app()
