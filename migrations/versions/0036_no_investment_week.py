"""Add governed no-investment weekly decisions.

Revision ID: 0036_no_investment_week
Revises: 0035_weekly_plan_reports
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_no_investment_week"
down_revision: str | None = "0035_weekly_plan_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("investment_plans") as batch:
        batch.add_column(
            sa.Column(
                "decision_kind",
                sa.Text(),
                nullable=False,
                server_default="ALLOCATED_PLAN",
            )
        )
        batch.create_check_constraint(
            "ck_investment_plans_decision_kind",
            "decision_kind IN ('ALLOCATED_PLAN','NO_INVESTMENT')",
        )

    op.create_table(
        "weekly_no_investment_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("strategy_assignment_id", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Text(), nullable=False),
        sa.Column("period_end", sa.Text(), nullable=False),
        sa.Column("weekly_budget_minor", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("renewed_at", sa.Text()),
        sa.Column("renewal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_by", sa.Text()),
        sa.Column("committed_plan_id", sa.Text()),
        sa.CheckConstraint(
            "period_end=date(period_start, '+6 days')",
            name="ck_weekly_no_investment_period",
        ),
        sa.CheckConstraint(
            "weekly_budget_minor > 0",
            name="ck_weekly_no_investment_budget",
        ),
        sa.CheckConstraint(
            "reason_code IN ('USER_CHOSE_NO_INVESTMENT','BUDGET_PAUSED_FOR_WEEK',"
            "'OTHER_EXPLICIT_SKIP')",
            name="ck_weekly_no_investment_reason",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_weekly_no_investment_status",
        ),
        sa.CheckConstraint(
            "renewal_count >= 0",
            name="ck_weekly_no_investment_renewals",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(
            ["strategy_assignment_id"], ["strategy_assignments.id"]
        ),
        sa.ForeignKeyConstraint(["committed_plan_id"], ["investment_plans.id"]),
    )
    op.create_index(
        "idx_weekly_no_investment_drafts_period",
        "weekly_no_investment_drafts",
        ["portfolio_id", "account_id", "period_start", "period_end", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_weekly_no_investment_drafts_period",
        table_name="weekly_no_investment_drafts",
    )
    op.drop_table("weekly_no_investment_drafts")
    with op.batch_alter_table("investment_plans") as batch:
        batch.drop_constraint("ck_investment_plans_decision_kind", type_="check")
        batch.drop_column("decision_kind")
