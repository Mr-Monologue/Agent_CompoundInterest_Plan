"""Add auditable market NAV snapshots.

Revision ID: 0004_market_nav
Revises: 0003_opening_position
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_market_nav"
down_revision: str | None = "0003_opening_position"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_nav_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("nav_date", sa.Text(), nullable=False),
        sa.Column("nav_micros", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="CNY"),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text()),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.Text(), nullable=False, unique=True),
        sa.CheckConstraint("nav_micros > 0", name="ck_market_nav_positive"),
        sa.CheckConstraint(
            "source_type IN ('OFFICIAL','PLATFORM','AGGREGATOR','USER')",
            name="ck_market_nav_source_type",
        ),
        sa.CheckConstraint(
            "verification_status IN ('VERIFIED','UNVERIFIED')",
            name="ck_market_nav_verification",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )
    op.create_index(
        "idx_market_nav_latest",
        "market_nav_snapshots",
        ["instrument_id", "nav_date", "observed_at"],
    )
    op.execute(
        "UPDATE schema_meta SET value='2', updated_at='2026-07-21T00:00:00Z' "
        "WHERE key='phase'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE schema_meta SET value='1', updated_at='2026-07-20T00:00:00Z' "
        "WHERE key='phase'"
    )
    op.drop_index("idx_market_nav_latest", table_name="market_nav_snapshots")
    op.drop_table("market_nav_snapshots")
