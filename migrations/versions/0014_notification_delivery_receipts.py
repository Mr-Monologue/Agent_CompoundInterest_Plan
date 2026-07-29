"""Add auditable notification dispatch attempts and verified delivery receipts.

Revision ID: 0014_notification_delivery_receipts
Revises: 0013_hermes_scheduler_bridge
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_notification_delivery_receipts"
down_revision: str | None = "0013_hermes_scheduler_bridge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Legacy SENT had no channel receipt contract. Preserve its timestamp as dispatch
    # evidence, but never upgrade it into verified delivery.
    op.execute(
        """
        UPDATE notification_outbox
        SET status='PENDING'
        WHERE status='SENT'
        """
    )
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("ck_outbox_status", type_="check")
        batch.add_column(sa.Column("dispatched_at", sa.Text()))
        batch.add_column(sa.Column("delivered_at", sa.Text()))
        batch.add_column(sa.Column("provider_message_id", sa.Text()))
        batch.create_check_constraint(
            "ck_outbox_status",
            "status IN ('PENDING','DISPATCHED','DELIVERED','FAILED','SUPPRESSED')",
        )
    op.execute(
        """
        UPDATE notification_outbox
        SET status='DISPATCHED',
            dispatched_at=COALESCE(sent_at, created_at),
            next_attempt_at=COALESCE(sent_at, created_at)
        WHERE status='PENDING' AND sent_at IS NOT NULL
        """
    )

    op.create_table(
        "notification_delivery_attempts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("outbox_id", sa.Text(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("receipt_digest", sa.Text(), nullable=False),
        sa.Column("delivery_target", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text()),
        sa.Column("provider_message_id", sa.Text()),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("claimed_at", sa.Text(), nullable=False),
        sa.Column("receipt_at", sa.Text()),
        sa.CheckConstraint(
            "status IN ('DISPATCHED','DELIVERED','FAILED','TIMED_OUT')",
            name="ck_delivery_attempt_status",
        ),
        sa.ForeignKeyConstraint(["outbox_id"], ["notification_outbox.id"]),
        sa.UniqueConstraint(
            "outbox_id",
            "attempt_number",
            name="uq_delivery_attempt_number",
        ),
        sa.UniqueConstraint("receipt_digest", name="uq_delivery_receipt_digest"),
    )
    op.create_index(
        "idx_delivery_attempts_outbox",
        "notification_delivery_attempts",
        ["outbox_id", "attempt_number"],
    )
    op.create_index(
        "idx_delivery_attempts_status",
        "notification_delivery_attempts",
        ["status", "claimed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_delivery_attempts_status",
        table_name="notification_delivery_attempts",
    )
    op.drop_index(
        "idx_delivery_attempts_outbox",
        table_name="notification_delivery_attempts",
    )
    op.drop_table("notification_delivery_attempts")
    op.execute(
        """
        UPDATE notification_outbox
        SET status=CASE
            WHEN status='DELIVERED' THEN 'SENT'
            WHEN status='DISPATCHED' THEN 'PENDING'
            ELSE status
        END
        """
    )
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("ck_outbox_status", type_="check")
        batch.drop_column("provider_message_id")
        batch.drop_column("delivered_at")
        batch.drop_column("dispatched_at")
        batch.create_check_constraint(
            "ck_outbox_status",
            "status IN ('PENDING','SENT','FAILED','SUPPRESSED')",
        )
