"""Add controlled end-to-end notification test requests.

Revision ID: 0016_notification_test_delivery
Revises: 0015_performance_reviews
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_notification_test_delivery"
down_revision: str | None = "0015_performance_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_test_requests",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("display_text", sa.Text(), nullable=False),
        sa.Column("delivery_target", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )
    op.create_index(
        "idx_notification_tests_created",
        "notification_test_requests",
        ["created_at"],
    )

    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("ck_outbox_exact_source", type_="check")
        batch.add_column(sa.Column("notification_test_request_id", sa.Text()))
        batch.create_foreign_key(
            "fk_outbox_notification_test",
            "notification_test_requests",
            ["notification_test_request_id"],
            ["id"],
        )
        batch.create_check_constraint(
            "ck_outbox_exact_source",
            """
            (CASE WHEN report_bundle_id IS NOT NULL THEN 1 ELSE 0 END) +
            (CASE WHEN alert_id IS NOT NULL THEN 1 ELSE 0 END) +
            (CASE WHEN notification_test_request_id IS NOT NULL THEN 1 ELSE 0 END) = 1
            """,
        )


def downgrade() -> None:
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("ck_outbox_exact_source", type_="check")
        batch.drop_constraint("fk_outbox_notification_test", type_="foreignkey")
        batch.drop_column("notification_test_request_id")
        batch.create_check_constraint(
            "ck_outbox_exact_source",
            "(report_bundle_id IS NOT NULL) != (alert_id IS NOT NULL)",
        )
    op.drop_index(
        "idx_notification_tests_created",
        table_name="notification_test_requests",
    )
    op.drop_table("notification_test_requests")
