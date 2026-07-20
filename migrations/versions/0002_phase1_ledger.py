"""Create the Phase 1 portfolio and transaction ledger.

Revision ID: 0002_phase1
Revises: 0001_phase0
Create Date: 2026-07-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_phase1"
down_revision: str | None = "0001_phase0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("base_currency", sa.Text(), nullable=False, server_default="CNY"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE','ARCHIVED')", name="ck_portfolios_status"),
    )

    op.create_table(
        "accounts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="CNY"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE','ARCHIVED')", name="ck_accounts_status"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint("portfolio_id", "name", name="uq_accounts_portfolio_name"),
    )
    op.create_index("idx_accounts_portfolio", "accounts", ["portfolio_id", "status"])

    op.create_table(
        "instruments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("asset_type", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default="CNY"),
        sa.Column("role", sa.Text(), nullable=False, server_default="UNASSIGNED"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "asset_type IN ('FUND','ETF','STOCK','INDEX','CASH')",
            name="ck_instruments_asset_type",
        ),
        sa.CheckConstraint(
            "role IN ('CORE','SATELLITE','UNASSIGNED')", name="ck_instruments_role"
        ),
        sa.CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_instruments_status"),
    )

    op.create_table(
        "transaction_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("nav_micros", sa.Integer(), nullable=False),
        sa.Column("shares_micros", sa.Integer(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("reversal_of_transaction_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_transaction_id", sa.Text()),
        sa.Column("actor_ref", sa.Text(), nullable=False),
        sa.CheckConstraint("action IN ('TRADE','REVERSAL')", name="ck_drafts_action"),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="ck_drafts_side"),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED','CANCELLED')",
            name="ck_drafts_status",
        ),
        sa.CheckConstraint("amount_minor > 0", name="ck_drafts_amount_positive"),
        sa.CheckConstraint("nav_micros > 0", name="ck_drafts_nav_positive"),
        sa.CheckConstraint("shares_micros > 0", name="ck_drafts_shares_positive"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )
    op.create_index(
        "idx_drafts_status_expiry", "transaction_drafts", ["status", "expires_at"]
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("draft_id", sa.Text(), nullable=False, unique=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, server_default="TRADE"),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("nav_micros", sa.Integer(), nullable=False),
        sa.Column("shares_micros", sa.Integer(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("reversal_of_transaction_id", sa.Text()),
        sa.Column("reversed_by_transaction_id", sa.Text()),
        sa.Column("confirmed_by", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.Text(), nullable=False, unique=True),
        sa.CheckConstraint("kind IN ('TRADE','REVERSAL')", name="ck_transactions_kind"),
        sa.CheckConstraint("side IN ('BUY','SELL')", name="ck_transactions_side"),
        sa.CheckConstraint("amount_minor > 0", name="ck_transactions_amount_positive"),
        sa.CheckConstraint("nav_micros > 0", name="ck_transactions_nav_positive"),
        sa.CheckConstraint("shares_micros > 0", name="ck_transactions_shares_positive"),
        sa.ForeignKeyConstraint(["draft_id"], ["transaction_drafts.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )
    op.create_index(
        "idx_transactions_holding",
        "transactions",
        ["portfolio_id", "account_id", "instrument_id", "trade_date", "committed_at"],
    )

    op.create_table(
        "holding_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("transaction_id", sa.Text(), nullable=False),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("total_shares_micros", sa.Integer(), nullable=False),
        sa.Column("cost_amount_minor", sa.Integer(), nullable=False),
        sa.Column("average_cost_nav_micros", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("total_shares_micros >= 0", name="ck_holdings_shares_nonnegative"),
        sa.CheckConstraint("cost_amount_minor >= 0", name="ck_holdings_cost_nonnegative"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.UniqueConstraint(
            "transaction_id", "account_id", "instrument_id", name="uq_holding_snapshot_event"
        ),
    )
    op.create_index(
        "idx_holdings_latest",
        "holding_snapshots",
        ["portfolio_id", "account_id", "instrument_id", "created_at"],
    )

    op.execute(
        "UPDATE schema_meta SET value='1', updated_at='2026-07-20T00:00:00Z' "
        "WHERE key='phase'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE schema_meta SET value='0', updated_at='2026-07-17T00:00:00Z' "
        "WHERE key='phase'"
    )
    op.drop_index("idx_holdings_latest", table_name="holding_snapshots")
    op.drop_table("holding_snapshots")
    op.drop_index("idx_transactions_holding", table_name="transactions")
    op.drop_table("transactions")
    op.drop_index("idx_drafts_status_expiry", table_name="transaction_drafts")
    op.drop_table("transaction_drafts")
    op.drop_table("instruments")
    op.drop_index("idx_accounts_portfolio", table_name="accounts")
    op.drop_table("accounts")
    op.drop_table("portfolios")
