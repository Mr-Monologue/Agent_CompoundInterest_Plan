"""Add governed, versioned weekly-plan reports.

Revision ID: 0035_weekly_plan_reports
Revises: 0034_partial_plan_closure
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_weekly_plan_reports"
down_revision: str | None = "0034_partial_plan_closure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("investment_plans") as batch:
        batch.add_column(sa.Column("period_start", sa.Text()))
        batch.add_column(sa.Column("period_end", sa.Text()))
    op.execute("UPDATE investment_plans SET period_start=plan_date")
    op.execute("UPDATE investment_plans SET period_end=date(plan_date, '+6 days')")
    with op.batch_alter_table("investment_plans") as batch:
        batch.alter_column("period_start", existing_type=sa.Text(), nullable=False)
        batch.alter_column("period_end", existing_type=sa.Text(), nullable=False)
        batch.create_check_constraint(
            "ck_investment_plans_period",
            "period_start=plan_date AND period_end>=period_start",
        )

    op.create_table(
        "weekly_report_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("report_version", sa.Integer(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("valuation_status", sa.Text(), nullable=False),
        sa.Column("regeneration_reason", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("renewed_at", sa.Text()),
        sa.Column("renewal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_report_id", sa.Text()),
        sa.CheckConstraint("report_version > 0", name="ck_weekly_report_draft_version"),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_weekly_report_draft_quality",
        ),
        sa.CheckConstraint(
            "valuation_status IN ('AVAILABLE','LIMITED')",
            name="ck_weekly_report_draft_valuation",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_weekly_report_draft_status",
        ),
        sa.CheckConstraint(
            "renewal_count >= 0", name="ck_weekly_report_draft_renewal_count"
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["investment_plans.id"]),
        sa.UniqueConstraint("plan_id", "report_version", name="uq_weekly_report_draft_version"),
    )
    op.create_index(
        "idx_weekly_report_drafts_plan",
        "weekly_report_drafts",
        ["plan_id", "status", "created_at"],
    )

    op.create_table(
        "weekly_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("report_bundle_id", sa.Text(), nullable=False, unique=True),
        sa.Column("report_version", sa.Integer(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("valuation_status", sa.Text(), nullable=False),
        sa.Column("regeneration_reason", sa.Text()),
        sa.Column("supersedes_report_id", sa.Text()),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("report_version > 0", name="ck_weekly_report_version"),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_weekly_report_quality",
        ),
        sa.CheckConstraint(
            "valuation_status IN ('AVAILABLE','LIMITED')",
            name="ck_weekly_report_valuation",
        ),
        sa.CheckConstraint("is_current IN (0,1)", name="ck_weekly_report_current"),
        sa.ForeignKeyConstraint(["plan_id"], ["investment_plans.id"]),
        sa.ForeignKeyConstraint(["report_bundle_id"], ["report_bundles.id"]),
        sa.ForeignKeyConstraint(["supersedes_report_id"], ["weekly_reports.id"]),
        sa.UniqueConstraint("plan_id", "report_version", name="uq_weekly_report_version"),
    )
    op.create_index(
        "idx_weekly_reports_plan",
        "weekly_reports",
        ["plan_id", "is_current", "report_version"],
    )

    # SQLite cannot add the forward foreign key while creating the draft table.
    # Integrity is enforced by service transactions and this index keeps lookups deterministic.
    op.create_index(
        "idx_weekly_report_drafts_committed",
        "weekly_report_drafts",
        ["committed_report_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_weekly_report_drafts_committed", table_name="weekly_report_drafts")
    op.drop_index("idx_weekly_reports_plan", table_name="weekly_reports")
    op.drop_table("weekly_reports")
    op.drop_index("idx_weekly_report_drafts_plan", table_name="weekly_report_drafts")
    op.drop_table("weekly_report_drafts")
    with op.batch_alter_table("investment_plans") as batch:
        batch.drop_constraint("ck_investment_plans_period", type_="check")
        batch.drop_column("period_end")
        batch.drop_column("period_start")
