"""Add auditable Hermes scheduler reconciliation snapshots.

Revision ID: 0013_hermes_scheduler_bridge
Revises: 0012_operations_automation
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_hermes_scheduler_bridge"
down_revision: str | None = "0012_operations_automation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_scheduler_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("gateway_status", sa.Text(), nullable=False),
        sa.Column("jobs_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("reconciliation_status", sa.Text(), nullable=False),
        sa.Column("drift_json", sa.Text(), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "gateway_status IN ('RUNNING','STOPPED','UNKNOWN')",
            name="ck_scheduler_snapshot_gateway",
        ),
        sa.CheckConstraint(
            "reconciliation_status IN ('IN_SYNC','DRIFT','BLOCKED')",
            name="ck_scheduler_snapshot_reconciliation",
        ),
    )
    op.create_index(
        "idx_scheduler_snapshots_profile",
        "automation_scheduler_snapshots",
        ["profile", "recorded_at"],
    )

    op.execute(
        """
        INSERT INTO schema_meta (key, value, updated_at)
        VALUES ('phase', '3', '2026-07-27T00:00:00Z')
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """
    )


def downgrade() -> None:
    op.drop_index(
        "idx_scheduler_snapshots_profile",
        table_name="automation_scheduler_snapshots",
    )
    op.drop_table("automation_scheduler_snapshots")
