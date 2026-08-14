"""Disambiguate registration and portfolio strategy roles.

Revision ID: 0030_instrument_role_contract
Revises: 0029_weekly_plan_skip_reconfirmation
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_instrument_role_contract"
down_revision: str | None = "0029_weekly_plan_skip_reconfirmation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("instruments") as batch:
        batch.drop_constraint("ck_instruments_role", type_="check")
        batch.alter_column(
            "role",
            new_column_name="registration_role",
            existing_type=sa.Text(),
            existing_nullable=False,
            existing_server_default="UNASSIGNED",
        )
        batch.create_check_constraint(
            "ck_instruments_registration_role",
            "registration_role IN ('CORE','SATELLITE','UNASSIGNED')",
        )


def downgrade() -> None:
    with op.batch_alter_table("instruments") as batch:
        batch.drop_constraint("ck_instruments_registration_role", type_="check")
        batch.alter_column(
            "registration_role",
            new_column_name="role",
            existing_type=sa.Text(),
            existing_nullable=False,
            existing_server_default="UNASSIGNED",
        )
        batch.create_check_constraint(
            "ck_instruments_role",
            "role IN ('CORE','SATELLITE','UNASSIGNED')",
        )
