"""Add short-lived reconfirmation drafts for skipping frozen weekly plans.

Revision ID: 0029_weekly_plan_skip_reconfirmation
Revises: 0028_external_subscription_lifecycle
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_weekly_plan_skip_reconfirmation"
down_revision: str | None = "0028_external_subscription_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "weekly_plan_skip_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("plan_facts_hash", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_by", sa.Text()),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_weekly_plan_skip_drafts_status",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["investment_plans.id"]),
    )
    op.create_index(
        "idx_weekly_plan_skip_drafts_plan_status",
        "weekly_plan_skip_drafts",
        ["plan_id", "status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_weekly_plan_skip_drafts_plan_status",
        table_name="weekly_plan_skip_drafts",
    )
    op.drop_table("weekly_plan_skip_drafts")
