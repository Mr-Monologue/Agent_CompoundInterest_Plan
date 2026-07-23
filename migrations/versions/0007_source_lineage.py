"""Add normalized upstream publisher lineage to NAV evidence.

Revision ID: 0007_source_lineage
Revises: 0006_market_nav_verification
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_source_lineage"
down_revision: str | None = "0006_market_nav_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("market_nav_snapshots") as batch:
        batch.add_column(
            sa.Column(
                "source_lineage",
                sa.Text(),
                nullable=False,
                server_default="UNKNOWN",
            )
        )
        batch.create_check_constraint(
            "ck_market_nav_source_lineage",
            "source_lineage IN "
            "('EASTMONEY','WIND','FUND_MANAGER_OFFICIAL','ALIPAY','UNKNOWN')",
        )

    op.execute(
        """
        UPDATE market_nav_snapshots
        SET source_lineage = 'EASTMONEY'
        WHERE lower(source_name) LIKE '%eastmoney%'
           OR source_name LIKE '%东方财富%'
           OR source_name LIKE '%天天基金%'
           OR lower(source_name) LIKE '%akshare%'
           OR lower(COALESCE(source_ref, '')) LIKE '%fund.eastmoney.com%'
        """
    )
    op.execute(
        """
        UPDATE market_nav_snapshots
        SET source_lineage = 'WIND'
        WHERE lower(source_name) LIKE '%wind%' OR source_name LIKE '%万得%'
        """
    )
    op.execute(
        """
        UPDATE market_nav_snapshots
        SET source_lineage = 'ALIPAY'
        WHERE lower(source_name) LIKE '%alipay%' OR source_name LIKE '%支付宝%'
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("market_nav_snapshots") as batch:
        batch.drop_constraint("ck_market_nav_source_lineage", type_="check")
        batch.drop_column("source_lineage")
