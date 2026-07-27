"""Complete the deterministic sell lifecycle and execution follow-up.

Revision ID: 0011_sell_lifecycle
Revises: 0010_strategy_governance_risk
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_sell_lifecycle"
down_revision: str | None = "0010_strategy_governance_risk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("strategy_instrument_configs") as batch:
        batch.add_column(
            sa.Column("lifecycle_rules_json", sa.Text(), nullable=False, server_default="{}")
        )
        batch.add_column(
            sa.Column("redemption_policy_json", sa.Text(), nullable=False, server_default="{}")
        )
        batch.add_column(
            sa.Column("exposure_profile_json", sa.Text(), nullable=False, server_default="{}")
        )
        batch.add_column(sa.Column("fund_destination", sa.Text()))

    op.create_table(
        "instrument_lifecycle_observations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("observation_type", sa.Text(), nullable=False),
        sa.Column("observation_date", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text()),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "observation_type IN "
            "('RELATIVE_PERFORMANCE','REPLACEMENT_CANDIDATE','OBJECTIVE_STATUS',"
            "'TOOL_QUALITY','REDEMPTION_TERMS','EXPOSURE_PROFILE')",
            name="ck_lifecycle_observations_type",
        ),
        sa.CheckConstraint(
            "source_type IN ('OFFICIAL','PROFESSIONAL','AGGREGATOR','PLATFORM','USER')",
            name="ck_lifecycle_observations_source",
        ),
        sa.CheckConstraint(
            "verification_status IN ('VERIFIED','UNVERIFIED')",
            name="ck_lifecycle_observations_verification",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )
    op.create_index(
        "idx_lifecycle_observations_lookup",
        "instrument_lifecycle_observations",
        ["instrument_id", "observation_type", "observation_date"],
    )

    with op.batch_alter_table("sell_proposals") as batch:
        batch.drop_constraint("ck_sell_proposals_trigger", type_="check")
        batch.drop_constraint("ck_sell_proposals_status", type_="check")
        batch.create_check_constraint(
            "ck_sell_proposals_trigger",
            "trigger_code IN "
            "('SELL_01_HARD_STOP','SELL_02_THESIS_INVALID','SELL_03_REBALANCE',"
            "'SELL_04_REPLACE','SELL_05_UNDERPERFORMANCE','SELL_06_TAKE_PROFIT',"
            "'SELL_07_OBJECTIVE_COMPLETE','SELL_08_LIQUIDITY','CORE_TOOL_QUALITY')",
        )
        batch.add_column(sa.Column("recommended_amount_minor", sa.Integer()))
        batch.create_check_constraint(
            "ck_sell_proposals_amount",
            "recommended_amount_minor IS NULL OR recommended_amount_minor > 0",
        )
        batch.create_check_constraint(
            "ck_sell_proposals_status",
            "status IN "
            "('REVIEW_REQUIRED','APPROVED','DEFERRED','REJECTED','EXPIRED','EXECUTED')",
        )

    with op.batch_alter_table("transaction_drafts") as batch:
        batch.add_column(sa.Column("sell_proposal_id", sa.Text()))
        batch.create_foreign_key(
            "fk_transaction_drafts_sell_proposal",
            "sell_proposals",
            ["sell_proposal_id"],
            ["id"],
        )

    op.create_table(
        "sell_execution_links",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("sell_proposal_id", sa.Text(), nullable=False),
        sa.Column("transaction_id", sa.Text(), nullable=False),
        sa.Column("linked_at", sa.Text(), nullable=False),
        sa.Column("linked_by", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["sell_proposal_id"], ["sell_proposals.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.UniqueConstraint("sell_proposal_id", name="uq_sell_execution_links_proposal"),
        sa.UniqueConstraint("transaction_id", name="uq_sell_execution_links_transaction"),
    )
    op.create_index(
        "idx_sell_execution_links_proposal",
        "sell_execution_links",
        ["sell_proposal_id", "linked_at"],
    )

    op.create_table(
        "sell_followups",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("sell_proposal_id", sa.Text(), nullable=False),
        sa.Column("sell_execution_link_id", sa.Text(), nullable=False, unique=True),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("transaction_id", sa.Text(), nullable=False),
        sa.Column("sold_at", sa.Text(), nullable=False),
        sa.Column("due_at", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expected_metric_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("evaluated_at", sa.Text()),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','DUE','COMPLETED','DATA_BLOCKED')",
            name="ck_sell_followups_status",
        ),
        sa.ForeignKeyConstraint(["sell_proposal_id"], ["sell_proposals.id"]),
        sa.ForeignKeyConstraint(["sell_execution_link_id"], ["sell_execution_links.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
    )
    op.create_index(
        "idx_sell_followups_due",
        "sell_followups",
        ["status", "due_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_sell_followups_due", table_name="sell_followups")
    op.drop_table("sell_followups")
    op.drop_index("idx_sell_execution_links_proposal", table_name="sell_execution_links")
    op.drop_table("sell_execution_links")
    with op.batch_alter_table("transaction_drafts") as batch:
        batch.drop_constraint("fk_transaction_drafts_sell_proposal", type_="foreignkey")
        batch.drop_column("sell_proposal_id")
    with op.batch_alter_table("sell_proposals") as batch:
        batch.drop_constraint("ck_sell_proposals_status", type_="check")
        batch.create_check_constraint(
            "ck_sell_proposals_status",
            "status IN "
            "('REVIEW_REQUIRED','APPROVED','DEFERRED','REJECTED','EXPIRED')",
        )
        batch.drop_constraint("ck_sell_proposals_amount", type_="check")
        batch.drop_column("recommended_amount_minor")
        batch.drop_constraint("ck_sell_proposals_trigger", type_="check")
        batch.create_check_constraint(
            "ck_sell_proposals_trigger",
            "trigger_code IN "
            "('SELL_01_HARD_STOP','SELL_02_THESIS_INVALID','SELL_03_REBALANCE')",
        )
    op.drop_index(
        "idx_lifecycle_observations_lookup",
        table_name="instrument_lifecycle_observations",
    )
    op.drop_table("instrument_lifecycle_observations")
    with op.batch_alter_table("strategy_instrument_configs") as batch:
        batch.drop_column("fund_destination")
        batch.drop_column("exposure_profile_json")
        batch.drop_column("redemption_policy_json")
        batch.drop_column("lifecycle_rules_json")
