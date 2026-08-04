"""Resolve stale failure alerts after their exact job run recovers.

Revision ID: 0025_alert_recovery_resolution
Revises: 0024_research_collection_orchestration
Create Date: 2026-08-04
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0025_alert_recovery_resolution"
down_revision: str | None = "0024_research_collection_orchestration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def upgrade() -> None:
    with op.batch_alter_table("alerts") as batch:
        batch.add_column(sa.Column("resolved_at", sa.Text()))
        batch.add_column(sa.Column("resolved_by", sa.Text()))
        batch.add_column(sa.Column("resolution_code", sa.Text()))
        batch.add_column(sa.Column("resolution_context_json", sa.Text()))

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT alerts.id AS alert_id, alerts.job_run_id,
                   job_runs.job_name, job_runs.scheduled_for,
                   job_runs.status, job_runs.finished_at,
                   job_runs.attempt_count, job_runs.output_json
            FROM alerts
            JOIN job_runs ON job_runs.id=alerts.job_run_id
            WHERE alerts.status IN ('OPEN','ACKNOWLEDGED')
              AND job_runs.status IN ('SUCCESS','DEGRADED')
              AND job_runs.error_code IS NULL
              AND job_runs.finished_at IS NOT NULL
            ORDER BY alerts.created_at, alerts.id
            """
        )
    ).mappings()
    for row in rows:
        output = json.loads(str(row["output_json"]))
        context = {
            "job_run_id": str(row["job_run_id"]),
            "job_name": str(row["job_name"]),
            "scheduled_for": str(row["scheduled_for"]),
            "final_status": str(row["status"]),
            "attempt_count": int(row["attempt_count"]),
            "reason_code": output.get("reason_code"),
            "data_quality": output.get("data_quality"),
        }
        context_json = _json(context)
        connection.execute(
            sa.text(
                """
                UPDATE alerts
                SET status='RESOLVED', resolved_at=:resolved_at,
                    resolved_by='system:migration-0025',
                    resolution_code='JOB_RUN_RECOVERED',
                    resolution_context_json=:resolution_context
                WHERE id=:alert_id AND status IN ('OPEN','ACKNOWLEDGED')
                """
            ),
            {
                "resolved_at": str(row["finished_at"]),
                "resolution_context": context_json,
                "alert_id": str(row["alert_id"]),
            },
        )
        details_hash = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        connection.execute(
            sa.text(
                """
                INSERT INTO audit_events (
                    id, occurred_at, actor_type, actor_ref, action, entity_type,
                    entity_id, before_hash, after_hash, details_json, trace_id
                ) VALUES (
                    :id, :occurred_at, 'SYSTEM', 'migration:0025_alert_recovery_resolution',
                    'AUTOMATION_ALERT_AUTO_RESOLVED', 'alert', :entity_id,
                    NULL, :after_hash, :details_json, :trace_id
                )
                """
            ),
            {
                "id": str(uuid4()),
                "occurred_at": str(row["finished_at"]),
                "entity_id": str(row["alert_id"]),
                "after_hash": details_hash,
                "details_json": context_json,
                "trace_id": str(uuid4()),
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE alerts
            SET status='OPEN'
            WHERE status='RESOLVED' AND resolved_by='system:migration-0025'
              AND resolution_code='JOB_RUN_RECOVERED'
            """
        )
    )
    with op.batch_alter_table("alerts") as batch:
        batch.drop_column("resolution_context_json")
        batch.drop_column("resolution_code")
        batch.drop_column("resolved_by")
        batch.drop_column("resolved_at")
