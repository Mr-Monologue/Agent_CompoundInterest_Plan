"""Create the Phase 0 operational schema.

Revision ID: 0001_phase0
Revises: None
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_phase0"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    schema_meta = op.create_table(
        "schema_meta",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
    )
    op.bulk_insert(
        schema_meta,
        [
            {
                "key": "phase",
                "value": "0",
                "updated_at": "2026-07-17T00:00:00Z",
            }
        ],
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("value_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE','RETIRED')", name="ck_settings_status"),
        sa.PrimaryKeyConstraint("key", "version"),
    )

    op.create_table(
        "job_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("job_name", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text()),
        sa.Column("input_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("output_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.Text()),
        sa.Column("error_summary", sa.Text()),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('RUNNING','SUCCESS','DEGRADED','FAILED','SKIPPED')",
            name="ck_job_runs_status",
        ),
    )
    op.create_index("idx_job_name_time", "job_runs", ["job_name", "scheduled_for"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_ref", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("before_hash", sa.Text()),
        sa.Column("after_hash", sa.Text()),
        sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("trace_id", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('USER','AGENT','CRON','CLI','SYSTEM')",
            name="ck_audit_events_actor_type",
        ),
    )
    op.create_index(
        "idx_audit_entity",
        "audit_events",
        ["entity_type", "entity_id", "occurred_at"],
    )

    op.create_table(
        "backups",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("database_schema_version", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("encrypted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.CheckConstraint("encrypted IN (0,1)", name="ck_backups_encrypted"),
        sa.CheckConstraint(
            "verification_status IN ('PENDING','PASS','FAIL')",
            name="ck_backups_verification_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("backups")
    op.drop_index("idx_audit_entity", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("idx_job_name_time", table_name="job_runs")
    op.drop_table("job_runs")
    op.drop_table("settings")
    op.drop_table("schema_meta")
