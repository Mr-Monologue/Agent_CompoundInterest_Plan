"""User-triggered research refresh audit and content-addressed public staging."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_research_updates"
down_revision: str | None = "0043_peer_research"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_update_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text()),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("lease_until", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
    )
    op.create_index(
        "uq_research_update_active",
        "research_update_runs",
        ["status"],
        unique=True,
        sqlite_where=sa.text("status = 'RUNNING'"),
    )
    op.create_table(
        "research_update_evidence",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("identity", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.Text(), nullable=False),
        sa.Column("raw_hash", sa.Text(), nullable=False),
        sa.Column("semantic_hash", sa.Text(), nullable=False),
        sa.Column("raw_base64", sa.Text(), nullable=False),
        sa.Column("parsed_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("research_update_evidence")
    op.drop_table("research_update_runs")
