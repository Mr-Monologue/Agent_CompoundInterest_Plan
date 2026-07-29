"""Operational CLI for deterministic jobs and diagnostics."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import date
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
from investor_core.operations import OperationsService
from investor_core.performance import PerformanceService
from investor_core.strategy import StrategyService
from investor_core.version import __version__

app = typer.Typer(no_args_is_help=True, help="Operate the Value DCA investor core.")
db_app = typer.Typer(no_args_is_help=True, help="Manage the local database schema.")
setup_app = typer.Typer(no_args_is_help=True, help="Create the first portfolio and account.")
instrument_app = typer.Typer(no_args_is_help=True, help="Manage the local instrument registry.")
ledger_app = typer.Typer(no_args_is_help=True, help="Inspect holdings and committed transactions.")
opening_app = typer.Typer(no_args_is_help=True, help="Import confirmed opening positions.")
strategy_app = typer.Typer(
    no_args_is_help=True,
    help="Manage protected strategy assignments and instrument eligibility.",
)
operations_app = typer.Typer(
    no_args_is_help=True,
    help="Run and inspect explicitly approved deterministic automation.",
)
performance_app = typer.Typer(
    no_args_is_help=True,
    help="Calculate deterministic portfolio performance and inspect periodic reviews.",
)
app.add_typer(db_app, name="db")
app.add_typer(setup_app, name="setup")
app.add_typer(instrument_app, name="instrument")
app.add_typer(ledger_app, name="ledger")
app.add_typer(opening_app, name="opening")
app.add_typer(strategy_app, name="strategy")
app.add_typer(operations_app, name="operations")
app.add_typer(performance_app, name="performance")


@performance_app.command("calculate")
def performance_calculate(
    portfolio_id: Annotated[str, typer.Option(help="Portfolio identifier.")],
    period_start: Annotated[str, typer.Option(help="Inclusive period start (YYYY-MM-DD).")],
    period_end: Annotated[str, typer.Option(help="Inclusive period end (YYYY-MM-DD).")],
    period_type: Annotated[
        str,
        typer.Option(help="CUSTOM, MONTHLY, QUARTERLY, ANNUAL or SINCE_INCEPTION."),
    ] = "CUSTOM",
) -> None:
    """Calculate and persist one auditable performance snapshot."""
    try:
        result = PerformanceService(get_settings()).calculate(
            portfolio_id=portfolio_id,
            period_start=date.fromisoformat(period_start),
            period_end=date.fromisoformat(period_end),
            period_type=period_type,
            persist=True,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, **result})


@performance_app.command("reviews")
def performance_reviews(
    portfolio_id: Annotated[str, typer.Option(help="Portfolio identifier.")],
    review_type: Annotated[
        str,
        typer.Option(help="Optional MONTHLY, QUARTERLY or ANNUAL filter."),
    ] = "",
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
) -> None:
    """List immutable periodic review facts and action items."""
    try:
        items = PerformanceService(get_settings()).list_reviews(
            portfolio_id=portfolio_id,
            review_type=review_type or None,
            limit=limit,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, "items": items})


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
    portfolio_name: Annotated[str, typer.Option(help="Portfolio display name.")] = "个人投资组合",
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
    currency: Annotated[str, typer.Option(help="Three-letter currency code.")] = "CNY",
) -> None:
    """Idempotently register an instrument for transaction recording."""
    try:
        result = LedgerService(get_settings()).create_instrument(
            code=code,
            name=name,
            asset_type=asset_type,
            currency=currency,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, "instrument": result})


@strategy_app.command("list")
def strategy_list() -> None:
    """List reusable strategy definitions without user portfolio data."""
    emit_ledger_result({"ok": True, "items": StrategyService(get_settings()).list_definitions()})


@strategy_app.command("current")
def strategy_current(
    portfolio_id: Annotated[str, typer.Option(help="Existing portfolio ID.")],
) -> None:
    """Read the approved strategy instance for one portfolio."""
    try:
        result = StrategyService(get_settings()).get_assignment(portfolio_id=portfolio_id)
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, "assignment": result})


@strategy_app.command("assign")
def strategy_assign(
    portfolio_id: Annotated[str, typer.Option(help="Existing portfolio ID.")],
    strategy_key: Annotated[str, typer.Option(help="Reusable public strategy key.")] = "value-dca",
    strategy_version: Annotated[str, typer.Option(help="Published strategy version.")] = "1.6",
    approved_by: Annotated[
        str, typer.Option(help="User or operator who explicitly approved the assignment.")
    ] = "local-user",
    reason: Annotated[
        str, typer.Option(help="Audit reason for the assignment.")
    ] = "Explicit local strategy assignment",
) -> None:
    """Explicitly bind a public strategy version to a local portfolio."""
    try:
        result = StrategyService(get_settings()).assign(
            portfolio_id=portfolio_id,
            strategy_key=strategy_key,
            strategy_version=strategy_version,
            instance_config={},
            approved_by=approved_by,
            reason=reason,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, "assignment": result})


@strategy_app.command("instrument-configure")
def strategy_instrument_configure(
    portfolio_id: Annotated[str, typer.Option(help="Existing portfolio ID.")],
    instrument_code: Annotated[str, typer.Option(help="Registered instrument code.")],
    role: Annotated[str, typer.Option(help="CORE, SATELLITE, CASH, WATCH or UNASSIGNED.")],
    contribution_eligible: Annotated[
        bool,
        typer.Option(
            "--contribution-eligible/--not-contribution-eligible",
            help="Explicitly allow or deny new contribution plans.",
        ),
    ] = False,
    target_weight_bps: Annotated[
        int | None,
        typer.Option(min=0, max=10000, help="Optional within-role target in basis points."),
    ] = None,
    priority: Annotated[int, typer.Option(min=0, help="Lower values are allocated first.")] = 100,
    minimum_amount_minor: Annotated[
        int, typer.Option(min=0, help="Smallest contribution in currency minor units.")
    ] = 1,
    maximum_amount_minor: Annotated[
        int | None,
        typer.Option(min=1, help="Optional contribution cap in currency minor units."),
    ] = None,
    benchmark_code: Annotated[str, typer.Option(help="Optional registered INDEX code.")] = "",
    thesis_status: Annotated[
        str, typer.Option(help="ACTIVE, REVIEW_REQUIRED or INVALID.")
    ] = "ACTIVE",
    approved_by: Annotated[
        str, typer.Option(help="User or operator who approved this local mapping.")
    ] = "local-user",
    reason: Annotated[
        str, typer.Option(help="Audit reason for this local mapping.")
    ] = "Explicit local instrument strategy configuration",
) -> None:
    """Configure a local instrument without changing the public strategy."""
    try:
        result = StrategyService(get_settings()).configure_instrument(
            portfolio_id=portfolio_id,
            instrument_code=instrument_code,
            role=role,
            contribution_eligible=contribution_eligible,
            target_weight_bps=target_weight_bps,
            priority=priority,
            minimum_amount_minor=minimum_amount_minor,
            maximum_amount_minor=maximum_amount_minor,
            benchmark_code=benchmark_code or None,
            thesis_status=thesis_status,
            approved_by=approved_by,
            reason=reason,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, "assignment": result})


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


@operations_app.command("run")
def operations_run(
    job_name: Annotated[
        str,
        typer.Argument(
            help=(
                "DAILY_MARKET_SYNC, DAILY_RISK_SCAN, WEEKLY_PLAN_PREPARE, "
                "SELL_FOLLOWUP_DUE, SYSTEM_DOCTOR, MONTHLY_REVIEW, "
                "QUARTERLY_REVIEW or ANNUAL_REVIEW."
            )
        ),
    ],
    scheduled_for: Annotated[
        str,
        typer.Option(help="Stable market date or scheduled timestamp used for idempotency."),
    ] = "",
    portfolio_id: Annotated[
        str,
        typer.Option(help="Optional portfolio; default investment context is used when omitted."),
    ] = "",
) -> None:
    """Run one governed job; unconfigured or paused jobs succeed silently."""
    try:
        result = OperationsService(get_settings()).run_job(
            job_name=job_name,
            scheduled_for=scheduled_for or None,
            portfolio_id=portfolio_id or None,
            actor_ref="operations-runner",
        )
    except LedgerError as error:
        emit_ledger_error(error)
    display_text = str(result["display_text"])
    if display_text != "[SILENT]":
        emit_ledger_result({"ok": True, **result})


@operations_app.command("status")
def operations_status(
    job_name: Annotated[str, typer.Option(help="Optional deterministic job name.")] = "",
    status: Annotated[str, typer.Option(help="Optional run status filter.")] = "",
    limit: Annotated[int, typer.Option(min=1, max=500)] = 100,
) -> None:
    """List automation policies, recent runs, alerts and pending delivery records."""
    service = OperationsService(get_settings())
    try:
        result = {
            "policies": service.list_policies(active_only=True),
            "runs": service.list_runs(
                job_name=job_name or None,
                status=status or None,
                limit=limit,
            ),
            "alerts": service.list_alerts(status="OPEN", limit=limit),
            "outbox": service.list_outbox(status="PENDING", limit=limit),
        }
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, **result})


@operations_app.command("scheduler-manifest")
def operations_scheduler_manifest(
    profile: Annotated[
        str, typer.Option(help="Hermes profile receiving managed jobs.")
    ] = "investor",
) -> None:
    """Print the desired managed Hermes job set without changing the scheduler."""
    try:
        result = OperationsService(get_settings()).scheduler_manifest(profile=profile)
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, **result})


@operations_app.command("retry-due")
def operations_retry_due(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    """Retry failed deterministic jobs whose persisted backoff has elapsed."""
    try:
        result = OperationsService(get_settings()).retry_due(limit=limit)
    except LedgerError as error:
        emit_ledger_error(error)
    if result["display_text"] != "[SILENT]":
        emit_ledger_result({"ok": True, **result})


@operations_app.command("missed-runs")
def operations_missed_runs(
    grace_minutes: Annotated[int, typer.Option(min=1, max=1440)] = 10,
    lookback_days: Annotated[int, typer.Option(min=1, max=31)] = 7,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 100,
) -> None:
    """List approved schedule occurrences that are due but have no run record."""
    try:
        result = OperationsService(get_settings()).list_missed_runs(
            grace_minutes=grace_minutes,
            lookback_days=lookback_days,
            limit=limit,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    emit_ledger_result({"ok": True, "items": result})


@operations_app.command("recover-due")
def operations_recover_due(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    """Catch up missed schedule windows, then retry failed deterministic runs."""
    try:
        result = OperationsService(get_settings()).recover_due(limit=limit)
    except LedgerError as error:
        emit_ledger_error(error)
    if result["display_text"] != "[SILENT]":
        emit_ledger_result({"ok": True, **result})


@operations_app.command("delivery-claim")
def operations_delivery_claim(
    delivery_target: Annotated[
        str,
        typer.Option(help="Optional exact Hermes delivery target."),
    ] = "",
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
) -> None:
    """Claim due notification facts for an external Hermes delivery adapter."""
    try:
        result = OperationsService(get_settings()).claim_delivery_attempts(
            delivery_target=delivery_target or None,
            limit=limit,
        )
    except LedgerError as error:
        emit_ledger_error(error)
    if result["display_text"] != "[SILENT]":
        emit_ledger_result({"ok": True, **result})


@operations_app.command("delivery-receipt")
def operations_delivery_receipt(
    outbox_id: Annotated[str, typer.Option(help="Claimed notification outbox ID.")],
    attempt_id: Annotated[str, typer.Option(help="Claimed delivery attempt ID.")],
    receipt_token: Annotated[str, typer.Option(help="One-time receipt token from claim.")],
    outcome: Annotated[str, typer.Option(help="DELIVERED or FAILED.")],
    provider: Annotated[str, typer.Option(help="Hermes channel provider name.")],
    provider_message_id: Annotated[
        str,
        typer.Option(help="Provider message ID; required for DELIVERED."),
    ] = "",
    error_code: Annotated[
        str,
        typer.Option(help="Failure code; required for FAILED."),
    ] = "",
) -> None:
    """Record provider evidence after the channel reports its actual result."""
    try:
        result = OperationsService(get_settings()).record_delivery_receipt(
            outbox_id=outbox_id,
            attempt_id=attempt_id,
            receipt_token=receipt_token,
            outcome=outcome,
            provider=provider,
            provider_message_id=provider_message_id or None,
            evidence={},
            error_code=error_code or None,
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
