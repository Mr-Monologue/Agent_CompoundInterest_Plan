"""Independent append-only public peer research; no investment instruments added."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_peer_research"
down_revision: str | None = "0042_holding_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "peer_research_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("anchor_code", sa.Text(), nullable=False),
        sa.Column("cohort_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("knowledge_date", sa.Text(), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("anchor_code", "cohort_key", "version"),
        sa.UniqueConstraint("anchor_code", "cohort_key", "idempotency_key"),
        sa.UniqueConstraint("anchor_code", "cohort_key", "content_hash"),
    )


def downgrade() -> None:
    op.drop_table("peer_research_runs")
