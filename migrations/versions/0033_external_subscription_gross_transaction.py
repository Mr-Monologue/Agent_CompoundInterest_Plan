"""Use gross external-subscription cost and support governed draft correction.

Revision ID: 0033_external_subscription_gross_transaction
Revises: 0032_confirmation_time_precision_revision
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_external_subscription_gross_transaction"
down_revision: str | None = "0032_confirmation_time_precision_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transaction_drafts") as batch:
        batch.add_column(
            sa.Column(
                "origin",
                sa.Text(),
                nullable=False,
                server_default="GENERIC",
            )
        )
        batch.add_column(sa.Column("origin_reference_id", sa.Text()))
        batch.add_column(sa.Column("revised_at", sa.Text()))
        batch.add_column(
            sa.Column(
                "revision_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_check_constraint(
            "ck_transaction_drafts_origin",
            "origin IN ('GENERIC','EXTERNAL_SUBSCRIPTION')",
        )
        batch.create_check_constraint(
            "ck_transaction_drafts_revision_count",
            "revision_count >= 0",
        )

    op.execute(
        """
        UPDATE transaction_drafts
        SET origin='EXTERNAL_SUBSCRIPTION',
            origin_reference_id=(
                SELECT l.confirmation_id
                FROM subscription_confirmation_transaction_links l
                WHERE l.transaction_draft_id=transaction_drafts.id
                LIMIT 1
            )
        WHERE EXISTS (
            SELECT 1
            FROM subscription_confirmation_transaction_links l
            WHERE l.transaction_draft_id=transaction_drafts.id
        )
        """
    )

    with op.batch_alter_table("subscription_confirmation_transaction_links") as batch:
        batch.add_column(sa.Column("gross_amount_minor", sa.Integer()))
        batch.add_column(sa.Column("confirmed_amount_minor", sa.Integer()))
        batch.add_column(sa.Column("fee_minor", sa.Integer()))
        batch.add_column(sa.Column("revised_at", sa.Text()))
        batch.add_column(
            sa.Column(
                "revision_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    op.execute(
        """
        UPDATE subscription_confirmation_transaction_links
        SET gross_amount_minor=plan_linked_amount_minor,
            confirmed_amount_minor=(
                SELECT c.confirmed_amount_minor
                FROM external_subscription_confirmations c
                WHERE c.id=subscription_confirmation_transaction_links.confirmation_id
            ),
            fee_minor=(
                SELECT c.fee_minor
                FROM external_subscription_confirmations c
                WHERE c.id=subscription_confirmation_transaction_links.confirmation_id
            )
        """
    )

    with op.batch_alter_table("subscription_confirmation_transaction_links") as batch:
        batch.alter_column("gross_amount_minor", existing_type=sa.Integer(), nullable=False)
        batch.alter_column(
            "confirmed_amount_minor",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch.alter_column("fee_minor", existing_type=sa.Integer(), nullable=False)
        batch.create_check_constraint(
            "ck_subscription_confirmation_link_gross_amount",
            "gross_amount_minor > 0 AND confirmed_amount_minor > 0 AND fee_minor >= 0",
        )
        batch.create_check_constraint(
            "ck_subscription_confirmation_link_revision_count",
            "revision_count >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("subscription_confirmation_transaction_links") as batch:
        batch.drop_constraint(
            "ck_subscription_confirmation_link_revision_count",
            type_="check",
        )
        batch.drop_constraint(
            "ck_subscription_confirmation_link_gross_amount",
            type_="check",
        )
        batch.drop_column("revision_count")
        batch.drop_column("revised_at")
        batch.drop_column("fee_minor")
        batch.drop_column("confirmed_amount_minor")
        batch.drop_column("gross_amount_minor")

    with op.batch_alter_table("transaction_drafts") as batch:
        batch.drop_constraint("ck_transaction_drafts_revision_count", type_="check")
        batch.drop_constraint("ck_transaction_drafts_origin", type_="check")
        batch.drop_column("revision_count")
        batch.drop_column("revised_at")
        batch.drop_column("origin_reference_id")
        batch.drop_column("origin")
