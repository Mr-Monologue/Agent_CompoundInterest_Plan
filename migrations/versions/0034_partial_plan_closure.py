"""Add governed closure for partially executed weekly plans.

Revision ID: 0034_partial_plan_closure
Revises: 0033_external_subscription_gross_transaction
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_partial_plan_closure"
down_revision: str | None = "0033_external_subscription_gross_transaction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REASON_CODES = (
    "PLATFORM_LIMIT_REMAINDER_ABANDONED",
    "PERIOD_ENDED_REMAINDER_ABANDONED",
    "USER_DECLINED_REMAINDER",
)


def upgrade() -> None:
    with op.batch_alter_table("investment_plans") as batch:
        batch.drop_constraint("ck_investment_plans_status", type_="check")
        batch.add_column(sa.Column("closed_at", sa.Text()))
        batch.add_column(sa.Column("closure_business_date", sa.Text()))
        batch.add_column(sa.Column("closure_reason_code", sa.Text()))
        batch.add_column(sa.Column("closure_note", sa.Text()))
        batch.add_column(sa.Column("abandoned_amount_minor", sa.Integer()))
        batch.add_column(sa.Column("carry_forward", sa.Boolean()))
        batch.create_check_constraint(
            "ck_investment_plans_status",
            "status IN ('DRAFT','FROZEN','PARTIALLY_EXECUTED',"
            "'PARTIALLY_EXECUTED_CLOSED','EXECUTED','EXPIRED','SKIPPED')",
        )
        batch.create_check_constraint(
            "ck_investment_plans_partial_closure",
            "(status='PARTIALLY_EXECUTED_CLOSED' AND closed_at IS NOT NULL "
            "AND closure_business_date IS NOT NULL AND closure_reason_code IS NOT NULL "
            "AND closure_note IS NOT NULL AND abandoned_amount_minor > 0 "
            "AND carry_forward=0) OR "
            "(status!='PARTIALLY_EXECUTED_CLOSED' AND closed_at IS NULL "
            "AND closure_business_date IS NULL AND closure_reason_code IS NULL "
            "AND closure_note IS NULL AND abandoned_amount_minor IS NULL "
            "AND carry_forward IS NULL)",
        )

    reason_values = ",".join(f"'{value}'" for value in REASON_CODES)
    op.create_table(
        "weekly_plan_partial_close_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("closure_business_date", sa.Text(), nullable=False),
        sa.Column("closure_reason_code", sa.Text(), nullable=False),
        sa.Column("closure_note", sa.Text(), nullable=False),
        sa.Column("carry_forward", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("planned_amount_minor", sa.Integer(), nullable=False),
        sa.Column("executed_amount_minor", sa.Integer(), nullable=False),
        sa.Column("abandoned_amount_minor", sa.Integer(), nullable=False),
        sa.Column("plan_facts_hash", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("renewed_at", sa.Text()),
        sa.Column("renewal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_by", sa.Text()),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_weekly_plan_partial_close_drafts_status",
        ),
        sa.CheckConstraint(
            f"closure_reason_code IN ({reason_values})",
            name="ck_weekly_plan_partial_close_drafts_reason",
        ),
        sa.CheckConstraint(
            "carry_forward=0",
            name="ck_weekly_plan_partial_close_drafts_no_carry",
        ),
        sa.CheckConstraint(
            "planned_amount_minor > 0 AND executed_amount_minor > 0 "
            "AND abandoned_amount_minor > 0 "
            "AND executed_amount_minor + abandoned_amount_minor = planned_amount_minor",
            name="ck_weekly_plan_partial_close_drafts_amounts",
        ),
        sa.CheckConstraint(
            "renewal_count >= 0",
            name="ck_weekly_plan_partial_close_drafts_renewals",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["investment_plans.id"]),
    )
    op.create_index(
        "idx_weekly_plan_partial_close_drafts_plan_status",
        "weekly_plan_partial_close_drafts",
        ["plan_id", "status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_weekly_plan_partial_close_drafts_plan_status",
        table_name="weekly_plan_partial_close_drafts",
    )
    op.drop_table("weekly_plan_partial_close_drafts")
    op.execute(
        "UPDATE investment_plans SET status='PARTIALLY_EXECUTED', closed_at=NULL, "
        "closure_business_date=NULL, closure_reason_code=NULL, closure_note=NULL, "
        "abandoned_amount_minor=NULL, carry_forward=NULL "
        "WHERE status='PARTIALLY_EXECUTED_CLOSED'"
    )
    with op.batch_alter_table("investment_plans") as batch:
        batch.drop_constraint("ck_investment_plans_partial_closure", type_="check")
        batch.drop_constraint("ck_investment_plans_status", type_="check")
        batch.drop_column("carry_forward")
        batch.drop_column("abandoned_amount_minor")
        batch.drop_column("closure_note")
        batch.drop_column("closure_reason_code")
        batch.drop_column("closure_business_date")
        batch.drop_column("closed_at")
        batch.create_check_constraint(
            "ck_investment_plans_status",
            "status IN ('DRAFT','FROZEN','PARTIALLY_EXECUTED','EXECUTED','EXPIRED','SKIPPED')",
        )
