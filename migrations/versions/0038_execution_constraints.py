"""Governed execution constraints referencing reusable research evidence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_execution_constraints"
down_revision: str | None = "0037_background_scheduler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_constraints",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("account_id", sa.Text(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "execution_constraint_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("execution_constraint_drafts")
    op.drop_table("execution_constraints")
