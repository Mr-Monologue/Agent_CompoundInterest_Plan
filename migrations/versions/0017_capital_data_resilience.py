"""Add cash ledger, official NAV backfill and runtime degradation facts.

Revision ID: 0017_capital_data_resilience
Revises: 0016_notification_test_delivery
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_capital_data_resilience"
down_revision: str | None = "0016_notification_test_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cash_event_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_date", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("confirmation_token_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_event_id", sa.Text()),
        sa.CheckConstraint(
            "event_type IN ('DEPOSIT','WITHDRAWAL','DIVIDEND','INTEREST','FEE')",
            name="ck_cash_draft_type",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_cash_draft_status",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_cash_draft_amount"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
    )
    op.create_index(
        "idx_cash_drafts_status",
        "cash_event_drafts",
        ["status", "expires_at"],
    )

    op.create_table(
        "cash_ledger_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_date", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("signed_amount_minor", sa.Integer(), nullable=False),
        sa.Column("is_external_flow", sa.Boolean(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("draft_id", sa.Text(), nullable=False, unique=True),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("committed_by", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('DEPOSIT','WITHDRAWAL','DIVIDEND','INTEREST','FEE')",
            name="ck_cash_event_type",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_cash_event_amount"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["draft_id"], ["cash_event_drafts.id"]),
    )
    op.create_index(
        "idx_cash_events_portfolio_date",
        "cash_ledger_events",
        ["portfolio_id", "event_date", "committed_at"],
    )

    with op.batch_alter_table("cash_event_drafts") as batch:
        batch.create_foreign_key(
            "fk_cash_draft_committed_event",
            "cash_ledger_events",
            ["committed_event_id"],
            ["id"],
        )

    op.create_table(
        "official_nav_backfill_batches",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_lineage", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("replayed_count", sa.Integer(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("actor_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('COMPLETED','CONFLICT')",
            name="ck_official_backfill_status",
        ),
        sa.CheckConstraint(
            "requested_count >= 1 AND created_count >= 0 "
            "AND replayed_count >= 0 AND conflict_count >= 0",
            name="ck_official_backfill_counts",
        ),
    )
    op.create_index(
        "idx_official_backfills_created",
        "official_nav_backfill_batches",
        ["created_at"],
    )

    op.create_table(
        "runtime_mode_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text()),
        sa.Column("as_of_date", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("capabilities_json", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("level IN ('L0','L1','L2','L3')", name="ck_runtime_mode_level"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
    )
    op.create_index(
        "idx_runtime_modes_portfolio_date",
        "runtime_mode_snapshots",
        ["portfolio_id", "as_of_date", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_runtime_modes_portfolio_date", table_name="runtime_mode_snapshots")
    op.drop_table("runtime_mode_snapshots")
    op.drop_index("idx_official_backfills_created", table_name="official_nav_backfill_batches")
    op.drop_table("official_nav_backfill_batches")
    with op.batch_alter_table("cash_event_drafts") as batch:
        batch.drop_constraint("fk_cash_draft_committed_event", type_="foreignkey")
    op.drop_index("idx_cash_events_portfolio_date", table_name="cash_ledger_events")
    op.drop_table("cash_ledger_events")
    op.drop_index("idx_cash_drafts_status", table_name="cash_event_drafts")
    op.drop_table("cash_event_drafts")
