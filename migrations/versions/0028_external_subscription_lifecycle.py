"""Add governed external subscription and confirmation facts.

Revision ID: 0028_external_subscription_lifecycle
Revises: 0027_partial_plan_execution
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_external_subscription_lifecycle"
down_revision: str | None = "0027_partial_plan_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_subscriptions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("weekly_plan_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("requested_amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.Text(), nullable=False),
        sa.Column("submitted_business_date", sa.Text(), nullable=False),
        sa.Column("expected_confirmation_date", sa.Text()),
        sa.Column("external_platform", sa.Text(), nullable=False),
        sa.Column("external_reference", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("pending_amount_minor", sa.Integer(), nullable=False),
        sa.Column("confirmed_amount_minor", sa.Integer(), nullable=False),
        sa.Column("fee_minor", sa.Integer(), nullable=False),
        sa.Column("refunded_amount_minor", sa.Integer(), nullable=False),
        sa.Column("cancelled_amount_minor", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('SUBMITTED','PENDING_CONFIRMATION','PARTIALLY_CONFIRMED',"
            "'CONFIRMED','CANCELLED','REJECTED')",
            name="ck_external_subscriptions_status",
        ),
        sa.CheckConstraint(
            "requested_amount_minor > 0 AND pending_amount_minor >= 0 "
            "AND confirmed_amount_minor >= 0 AND fee_minor >= 0 "
            "AND refunded_amount_minor >= 0 AND cancelled_amount_minor >= 0",
            name="ck_external_subscriptions_amounts",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["weekly_plan_id"], ["investment_plans.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )
    op.create_index(
        "idx_external_subscriptions_plan_status",
        "external_subscriptions",
        ["weekly_plan_id", "status", "submitted_business_date"],
    )
    op.create_index(
        "idx_external_subscriptions_context",
        "external_subscriptions",
        ["portfolio_id", "account_id", "status"],
    )

    op.create_table(
        "external_subscription_confirmations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("subscription_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.Text(), nullable=False),
        sa.Column("confirmation_business_date", sa.Text(), nullable=False),
        sa.Column("nav_date", sa.Text(), nullable=False),
        sa.Column("nav_micros", sa.Integer(), nullable=False),
        sa.Column("confirmed_shares_micros", sa.Integer(), nullable=False),
        sa.Column("confirmed_amount_minor", sa.Integer(), nullable=False),
        sa.Column("fee_minor", sa.Integer(), nullable=False),
        sa.Column("refunded_amount_minor", sa.Integer(), nullable=False),
        sa.Column("external_reference", sa.Text()),
        sa.Column("reversal_of_confirmation_id", sa.Text()),
        sa.Column("reversed_by_confirmation_id", sa.Text()),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('CONFIRMATION','REVERSAL')",
            name="ck_external_subscription_confirmations_kind",
        ),
        sa.CheckConstraint(
            "nav_micros > 0 AND confirmed_shares_micros > 0 "
            "AND confirmed_amount_minor > 0 AND fee_minor >= 0 "
            "AND refunded_amount_minor >= 0",
            name="ck_external_subscription_confirmations_amounts",
        ),
        sa.ForeignKeyConstraint(["subscription_id"], ["external_subscriptions.id"]),
        sa.ForeignKeyConstraint(
            ["reversal_of_confirmation_id"], ["external_subscription_confirmations.id"]
        ),
        sa.ForeignKeyConstraint(
            ["reversed_by_confirmation_id"], ["external_subscription_confirmations.id"]
        ),
    )
    op.create_index(
        "idx_external_subscription_confirmations_subscription",
        "external_subscription_confirmations",
        ["subscription_id", "confirmation_business_date", "created_at"],
    )

    op.create_table(
        "external_subscription_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("subscription_id", sa.Text()),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_entity_id", sa.Text()),
        sa.Column("actor_ref", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "action IN ('SUBMIT','MARK_PENDING','CONFIRM','REVERSE_CONFIRMATION',"
            "'CANCEL','REJECT')",
            name="ck_external_subscription_drafts_action",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED','CANCELLED')",
            name="ck_external_subscription_drafts_status",
        ),
        sa.ForeignKeyConstraint(["subscription_id"], ["external_subscriptions.id"]),
    )
    op.create_index(
        "idx_external_subscription_drafts_status",
        "external_subscription_drafts",
        ["status", "expires_at"],
    )

    op.create_table(
        "subscription_confirmation_transaction_links",
        sa.Column("confirmation_id", sa.Text(), primary_key=True),
        sa.Column("transaction_draft_id", sa.Text(), nullable=False),
        sa.Column("transaction_id", sa.Text(), unique=True),
        sa.Column("plan_linked_amount_minor", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.CheckConstraint(
            "plan_linked_amount_minor > 0",
            name="ck_subscription_confirmation_link_amount",
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_id"], ["external_subscription_confirmations.id"]
        ),
        sa.ForeignKeyConstraint(["transaction_draft_id"], ["transaction_drafts.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
    )


def downgrade() -> None:
    op.drop_table("subscription_confirmation_transaction_links")
    op.drop_index(
        "idx_external_subscription_drafts_status",
        table_name="external_subscription_drafts",
    )
    op.drop_table("external_subscription_drafts")
    op.drop_index(
        "idx_external_subscription_confirmations_subscription",
        table_name="external_subscription_confirmations",
    )
    op.drop_table("external_subscription_confirmations")
    op.drop_index("idx_external_subscriptions_context", table_name="external_subscriptions")
    op.drop_index(
        "idx_external_subscriptions_plan_status",
        table_name="external_subscriptions",
    )
    op.drop_table("external_subscriptions")
