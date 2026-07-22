"""Add immutable cross-source NAV verification evidence.

Revision ID: 0006_market_nav_verification
Revises: 0005_market_data_sync
Create Date: 2026-07-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_market_nav_verification"
down_revision: str | None = "0005_market_data_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_nav_verifications",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("primary_snapshot_id", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("nav_delta_micros", sa.Integer(), nullable=False),
        sa.Column("verified_at", sa.Text(), nullable=False),
        sa.Column("actor_ref", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.Text(), nullable=False, unique=True),
        sa.CheckConstraint(
            "status IN ('MATCH','CONFLICT')",
            name="ck_market_nav_verification_status",
        ),
        sa.CheckConstraint(
            "nav_delta_micros >= 0",
            name="ck_market_nav_verification_delta",
        ),
        sa.CheckConstraint(
            "primary_snapshot_id <> evidence_snapshot_id",
            name="ck_market_nav_verification_distinct_snapshots",
        ),
        sa.ForeignKeyConstraint(
            ["primary_snapshot_id"],
            ["market_nav_snapshots.id"],
        ),
        sa.ForeignKeyConstraint(
            ["evidence_snapshot_id"],
            ["market_nav_snapshots.id"],
        ),
    )
    op.create_index(
        "idx_market_nav_verification_primary",
        "market_nav_verifications",
        ["primary_snapshot_id", "verified_at"],
    )
    op.create_index(
        "idx_market_nav_verification_evidence",
        "market_nav_verifications",
        ["evidence_snapshot_id", "verified_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_market_nav_verification_evidence",
        table_name="market_nav_verifications",
    )
    op.drop_index(
        "idx_market_nav_verification_primary",
        table_name="market_nav_verifications",
    )
    op.drop_table("market_nav_verifications")
