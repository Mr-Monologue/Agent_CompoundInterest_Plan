"""Shared read-only calculations for unfinished weekly-plan commitments."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from investor_core.config import Settings
from investor_core.ledger import JsonDict

MONEY_SCALE = 100


def _money(value: int) -> str:
    return f"{value / MONEY_SCALE:.2f}"


def unfinished_plan_commitments(
    settings: Settings,
    *,
    portfolio_id: str,
    account_id: str,
    before_date: str,
) -> JsonDict:
    """Return prior frozen-plan amounts that must not be allocated again."""
    database_path = (
        ":memory:"
        if str(settings.db_path) == ":memory:"
        else str(Path(settings.db_path).resolve())
    )
    connection = sqlite3.connect(database_path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT p.id AS plan_id, p.plan_date,
                   COALESCE(SUM(pi.candidate_amount_minor), 0) AS planned_minor,
                   COALESCE((
                       SELECT SUM(
                           CASE WHEN t.reversed_by_transaction_id IS NULL
                                THEN l.linked_amount_minor ELSE 0 END
                       )
                       FROM plan_execution_links l
                       JOIN transactions t ON t.id=l.transaction_id
                       WHERE l.plan_id=p.id
                   ), 0) AS executed_minor
            FROM investment_plans p
            JOIN plan_revisions pr
              ON pr.plan_id=p.id AND pr.revision=p.current_revision
            JOIN plan_items pi ON pi.plan_revision_id=pr.id
            WHERE p.portfolio_id=? AND p.account_id=?
              AND p.status IN ('FROZEN','PARTIALLY_EXECUTED')
              AND p.plan_date < ?
              AND pi.action='CONTRIBUTE' AND pi.candidate_amount_minor > 0
            GROUP BY p.id, p.plan_date
            ORDER BY p.plan_date, p.id
            """,
            (portfolio_id, account_id, before_date),
        ).fetchall()
    finally:
        connection.close()
    items: list[JsonDict] = []
    outstanding_total = 0
    for row in rows:
        planned = int(row["planned_minor"])
        executed = int(row["executed_minor"])
        outstanding = max(planned - executed, 0)
        outstanding_total += outstanding
        items.append(
            {
                "plan_id": str(row["plan_id"]),
                "plan_date": str(row["plan_date"]),
                "planned_amount": _money(planned),
                "executed_amount": _money(executed),
                "outstanding_amount": _money(outstanding),
            }
        )
    return {
        "plan_count": len(items),
        "outstanding_amount_minor": outstanding_total,
        "outstanding_amount": _money(outstanding_total),
        "items": items,
    }
