"""Allow explicitly confirmed opening-position ledger events.

Revision ID: 0003_opening_position
Revises: 0002_phase1
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_opening_position"
down_revision: str | None = "0002_phase1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transaction_drafts", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_drafts_action", type_="check")
        batch_op.create_check_constraint(
            "ck_drafts_action", "action IN ('TRADE','REVERSAL','OPENING')"
        )

    with op.batch_alter_table("transactions", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_transactions_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_transactions_kind", "kind IN ('TRADE','REVERSAL','OPENING')"
        )

    op.execute(
        "UPDATE schema_meta SET value='1', updated_at='2026-07-20T00:00:00Z' "
        "WHERE key='phase'"
    )


def downgrade() -> None:
    connection = op.get_bind()
    opening_count = connection.exec_driver_sql(
        "SELECT COUNT(*) FROM transactions WHERE kind = 'OPENING'"
    ).scalar_one()
    opening_draft_count = connection.exec_driver_sql(
        "SELECT COUNT(*) FROM transaction_drafts WHERE action = 'OPENING'"
    ).scalar_one()
    if opening_count or opening_draft_count:
        raise RuntimeError(
            "cannot downgrade while opening-position ledger records exist"
        )

    with op.batch_alter_table("transactions", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_transactions_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_transactions_kind", "kind IN ('TRADE','REVERSAL')"
        )

    with op.batch_alter_table("transaction_drafts", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_drafts_action", type_="check")
        batch_op.create_check_constraint(
            "ck_drafts_action", "action IN ('TRADE','REVERSAL')"
        )
