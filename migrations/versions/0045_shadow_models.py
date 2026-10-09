"""Append-only isolated shadow model definitions, observations and governance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_shadow_models"
down_revision: str | None = "0044_research_updates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shadow_models",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("model_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("model_key", "version", name="uq_shadow_model_version"),
    )
    op.create_table(
        "shadow_records",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("model_id", sa.Text(), sa.ForeignKey("shadow_models.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False, unique=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("shadow_records")
    op.drop_table("shadow_models")
