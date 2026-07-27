"""Add governed automation runs, report bundles, alerts and notification outbox.

Revision ID: 0012_operations_automation
Revises: 0011_sell_lifecycle
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_operations_automation"
down_revision: str | None = "0011_sell_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "automation_policy_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text()),
        sa.Column("job_name", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("schedule", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.CheckConstraint("enabled IN (0,1)", name="ck_automation_drafts_enabled"),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_automation_drafts_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
    )
    op.create_index(
        "idx_automation_drafts_status",
        "automation_policy_drafts",
        ["status", "expires_at"],
    )

    op.create_table(
        "automation_policies",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text()),
        sa.Column("job_name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("schedule", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("enabled IN (0,1)", name="ck_automation_policies_enabled"),
        sa.CheckConstraint("status IN ('ACTIVE','RETIRED')", name="ck_automation_policies_status"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint(
            "portfolio_id",
            "job_name",
            "version",
            name="uq_automation_policy_version",
        ),
    )
    op.create_index(
        "idx_automation_policies_active",
        "automation_policies",
        ["portfolio_id", "job_name", "status"],
    )

    with op.batch_alter_table("job_runs") as batch:
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3")
        )
        batch.add_column(sa.Column("heartbeat_at", sa.Text()))
        batch.add_column(sa.Column("next_retry_at", sa.Text()))

    op.create_table(
        "report_bundles",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text()),
        sa.Column("job_run_id", sa.Text(), nullable=False),
        sa.Column("bundle_type", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("delivery_action", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_report_bundles_quality",
        ),
        sa.CheckConstraint(
            "delivery_action IN ('SILENT','NOTIFY')",
            name="ck_report_bundles_delivery",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["job_run_id"], ["job_runs.id"]),
        sa.UniqueConstraint(
            "portfolio_id",
            "bundle_type",
            "scheduled_for",
            "facts_hash",
            name="uq_report_bundle_content",
        ),
    )
    op.create_index(
        "idx_report_bundles_lookup",
        "report_bundles",
        ["portfolio_id", "bundle_type", "created_at"],
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text()),
        sa.Column("job_run_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False, unique=True),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_seen_at", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.Text()),
        sa.Column("acknowledged_by", sa.Text()),
        sa.CheckConstraint("severity IN ('INFO','WARNING','CRITICAL')", name="ck_alerts_severity"),
        sa.CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED')", name="ck_alerts_status"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["job_run_id"], ["job_runs.id"]),
    )
    op.create_index(
        "idx_alerts_lookup",
        "alerts",
        ["portfolio_id", "status", "severity", "last_seen_at"],
    )

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("report_bundle_id", sa.Text()),
        sa.Column("alert_id", sa.Text()),
        sa.Column("delivery_target", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False, unique=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.Text()),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.Text()),
        sa.CheckConstraint(
            "(report_bundle_id IS NOT NULL) != (alert_id IS NOT NULL)",
            name="ck_outbox_exact_source",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','SENT','FAILED','SUPPRESSED')",
            name="ck_outbox_status",
        ),
        sa.ForeignKeyConstraint(["report_bundle_id"], ["report_bundles.id"]),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"]),
    )
    op.create_index(
        "idx_outbox_delivery",
        "notification_outbox",
        ["status", "next_attempt_at", "created_at"],
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
    op.drop_index("idx_outbox_delivery", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_index("idx_alerts_lookup", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("idx_report_bundles_lookup", table_name="report_bundles")
    op.drop_table("report_bundles")
    with op.batch_alter_table("job_runs") as batch:
        batch.drop_column("next_retry_at")
        batch.drop_column("heartbeat_at")
        batch.drop_column("max_attempts")
        batch.drop_column("attempt_count")
    op.drop_index("idx_automation_policies_active", table_name="automation_policies")
    op.drop_table("automation_policies")
    op.drop_index("idx_automation_drafts_status", table_name="automation_policy_drafts")
    op.drop_table("automation_policy_drafts")
    op.execute(
        """
        UPDATE schema_meta
        SET value = '2', updated_at = '2026-07-27T00:00:00Z'
        WHERE key = 'phase'
        """
    )
