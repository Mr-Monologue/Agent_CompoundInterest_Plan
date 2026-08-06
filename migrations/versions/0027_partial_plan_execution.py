"""Add incremental weekly-plan execution links and partial state.

Revision ID: 0027_partial_plan_execution
Revises: 0026_satellite_signal_gating
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_partial_plan_execution"
down_revision: str | None = "0026_satellite_signal_gating"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("investment_plans") as batch:
        batch.drop_constraint("ck_investment_plans_status", type_="check")
        batch.create_check_constraint(
            "ck_investment_plans_status",
            "status IN "
            "('DRAFT','FROZEN','PARTIALLY_EXECUTED','EXECUTED','EXPIRED','SKIPPED')",
        )

    op.create_table(
        "plan_execution_links",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("transaction_id", sa.Text(), nullable=False),
        sa.Column("linked_amount_minor", sa.Integer(), nullable=False),
        sa.Column("linked_at", sa.Text(), nullable=False),
        sa.Column("linked_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "linked_amount_minor > 0",
            name="ck_plan_execution_links_amount",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["investment_plans.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.UniqueConstraint("transaction_id", name="uq_plan_execution_links_transaction"),
    )
    op.create_index(
        "idx_plan_execution_links_plan",
        "plan_execution_links",
        ["plan_id", "linked_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_plan_execution_links_plan", table_name="plan_execution_links")
    op.drop_table("plan_execution_links")
    op.execute(
        "UPDATE investment_plans SET status='FROZEN', executed_at=NULL "
        "WHERE status='PARTIALLY_EXECUTED'"
    )
    with op.batch_alter_table("investment_plans") as batch:
        batch.drop_constraint("ck_investment_plans_status", type_="check")
        batch.create_check_constraint(
            "ck_investment_plans_status",
            "status IN ('DRAFT','FROZEN','EXECUTED','EXPIRED','SKIPPED')",
        )
