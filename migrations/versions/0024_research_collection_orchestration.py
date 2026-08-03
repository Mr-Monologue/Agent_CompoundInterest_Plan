"""Add governed research collection orchestration and connector health.

Revision ID: 0024_research_collection_orchestration
Revises: 0023_research_source_coverage
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_research_collection_orchestration"
down_revision: str | None = "0023_research_source_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_collection_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("coverage_snapshot_id", sa.Text(), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("eligible_connectors_json", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.Text(), nullable=False),
        sa.Column("active_claim_id", sa.Text()),
        sa.Column("completed_run_id", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','CLAIMED','COMPLETED','EXHAUSTED','SUPERSEDED')",
            name="ck_research_collection_task_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(
            ["coverage_snapshot_id"], ["research_coverage_snapshots.id"]
        ),
        sa.ForeignKeyConstraint(["completed_run_id"], ["research_collection_runs.id"]),
        sa.UniqueConstraint(
            "coverage_snapshot_id",
            "instrument_code",
            "evidence_type",
            name="uq_research_collection_task_scope",
        ),
    )
    op.create_index(
        "idx_research_collection_task_queue",
        "research_collection_tasks",
        ["portfolio_id", "status", "available_at"],
    )

    op.create_table(
        "research_collection_claims",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("connector_key", sa.Text(), nullable=False),
        sa.Column("adapter_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("task_ids_json", sa.Text(), nullable=False),
        sa.Column("claim_token_digest", sa.Text(), nullable=False),
        sa.Column("claimed_at", sa.Text(), nullable=False),
        sa.Column("lease_expires_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text()),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE','COMPLETED','PARTIAL','FAILED','EXPIRED')",
            name="ck_research_collection_claim_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint("facts_hash", name="uq_research_collection_claim_facts"),
    )
    op.create_index(
        "idx_research_collection_claim_connector",
        "research_collection_claims",
        ["portfolio_id", "connector_key", "status", "lease_expires_at"],
    )

    op.create_table(
        "research_collection_task_receipts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("claim_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("collection_run_id", sa.Text(), nullable=False),
        sa.Column("ingestion_status", sa.Text(), nullable=False),
        sa.Column("evidence_id", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("completed_at", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "ingestion_status IN ('RECORDED','REPLAYED','REJECTED','MISSING')",
            name="ck_research_collection_task_receipt_status",
        ),
        sa.ForeignKeyConstraint(["claim_id"], ["research_collection_claims.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["research_collection_tasks.id"]),
        sa.ForeignKeyConstraint(
            ["collection_run_id"], ["research_collection_runs.id"]
        ),
        sa.ForeignKeyConstraint(["evidence_id"], ["market_research_evidence.id"]),
        sa.UniqueConstraint(
            "claim_id", "task_id", name="uq_research_collection_task_receipt"
        ),
        sa.UniqueConstraint("facts_hash", name="uq_research_collection_receipt_facts"),
    )
    op.create_index(
        "idx_research_collection_receipt_task",
        "research_collection_task_receipts",
        ["task_id", "completed_at"],
    )

    op.create_table(
        "research_connector_health_receipts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("connector_key", sa.Text(), nullable=False),
        sa.Column("adapter_version", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "state IN ('HEALTHY','DEGRADED','UNAVAILABLE')",
            name="ck_research_connector_health_state",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.UniqueConstraint("facts_hash", name="uq_research_connector_health_facts"),
    )
    op.create_index(
        "idx_research_connector_health_latest",
        "research_connector_health_receipts",
        ["portfolio_id", "connector_key", "observed_at"],
    )

    op.create_table(
        "research_coverage_changes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("previous_snapshot_id", sa.Text(), nullable=False),
        sa.Column("current_snapshot_id", sa.Text(), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.Text(), nullable=False),
        sa.Column("change_kind", sa.Text(), nullable=False),
        sa.Column("previous_state", sa.Text(), nullable=False),
        sa.Column("current_state", sa.Text(), nullable=False),
        sa.Column("previous_collection_state", sa.Text(), nullable=False),
        sa.Column("current_collection_state", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "change_kind IN ('IMPROVED','REGRESSED','CHANGED')",
            name="ck_research_coverage_change_kind",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(
            ["previous_snapshot_id"], ["research_coverage_snapshots.id"]
        ),
        sa.ForeignKeyConstraint(
            ["current_snapshot_id"], ["research_coverage_snapshots.id"]
        ),
        sa.UniqueConstraint("facts_hash", name="uq_research_coverage_change_facts"),
    )
    op.create_index(
        "idx_research_coverage_change_portfolio",
        "research_coverage_changes",
        ["portfolio_id", "current_snapshot_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_research_coverage_change_portfolio",
        table_name="research_coverage_changes",
    )
    op.drop_table("research_coverage_changes")
    op.drop_index(
        "idx_research_connector_health_latest",
        table_name="research_connector_health_receipts",
    )
    op.drop_table("research_connector_health_receipts")
    op.drop_index(
        "idx_research_collection_receipt_task",
        table_name="research_collection_task_receipts",
    )
    op.drop_table("research_collection_task_receipts")
    op.drop_index(
        "idx_research_collection_claim_connector",
        table_name="research_collection_claims",
    )
    op.drop_table("research_collection_claims")
    op.drop_index(
        "idx_research_collection_task_queue",
        table_name="research_collection_tasks",
    )
    op.drop_table("research_collection_tasks")
