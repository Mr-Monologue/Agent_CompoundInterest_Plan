"""Add audited renewal state for external subscription drafts.

Revision ID: 0031_external_subscription_draft_renewal
Revises: 0030_instrument_role_contract
Create Date: 2026-08-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_external_subscription_draft_renewal"
down_revision: str | None = "0030_instrument_role_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("external_subscription_drafts") as batch:
        batch.add_column(sa.Column("renewed_at", sa.Text()))
        batch.add_column(
            sa.Column(
                "renewal_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_check_constraint(
            "ck_external_subscription_drafts_renewal_count",
            "renewal_count >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("external_subscription_drafts") as batch:
        batch.drop_constraint(
            "ck_external_subscription_drafts_renewal_count",
            type_="check",
        )
        batch.drop_column("renewal_count")
        batch.drop_column("renewed_at")
