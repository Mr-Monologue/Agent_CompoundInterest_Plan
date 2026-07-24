"""Add confirmed strategy governance, valuation evidence and sell proposals.

Revision ID: 0010_strategy_governance_risk
Revises: 0009_strategy_instance_plan
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_strategy_governance_risk"
down_revision: str | None = "0009_strategy_instance_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("strategy_instrument_configs") as batch:
        batch.add_column(
            sa.Column(
                "proxy_suitability",
                sa.Text(),
                nullable=False,
                server_default="NOT_APPLICABLE",
            )
        )
        batch.add_column(sa.Column("hard_stop_return_bps", sa.Integer()))
        batch.add_column(sa.Column("maximum_position_weight_bps", sa.Integer()))
        batch.create_check_constraint(
            "ck_strategy_instrument_configs_proxy_suitability",
            "proxy_suitability IN ('STRONG','WEAK','NOT_APPLICABLE')",
        )
        batch.create_check_constraint(
            "ck_strategy_instrument_configs_hard_stop",
            "hard_stop_return_bps IS NULL OR "
            "(hard_stop_return_bps >= -10000 AND hard_stop_return_bps < 0)",
        )
        batch.create_check_constraint(
            "ck_strategy_instrument_configs_position_cap",
            "maximum_position_weight_bps IS NULL OR "
            "(maximum_position_weight_bps > 0 AND maximum_position_weight_bps <= 10000)",
        )

    op.create_table(
        "strategy_config_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("strategy_assignment_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_by", sa.Text()),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED','CANCELLED')",
            name="ck_strategy_config_drafts_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(
            ["strategy_assignment_id"],
            ["strategy_assignments.id"],
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )

    op.create_table(
        "valuation_observations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("observation_date", sa.Text(), nullable=False),
        sa.Column("value_micros", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text()),
        sa.Column("verification_status", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "metric IN ('PE','PB')",
            name="ck_valuation_observations_metric",
        ),
        sa.CheckConstraint(
            "value_micros > 0",
            name="ck_valuation_observations_value",
        ),
        sa.CheckConstraint(
            "source_type IN ('OFFICIAL','PROFESSIONAL','AGGREGATOR','USER')",
            name="ck_valuation_observations_source_type",
        ),
        sa.CheckConstraint(
            "verification_status IN ('VERIFIED','UNVERIFIED')",
            name="ck_valuation_observations_verification",
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )
    op.create_index(
        "idx_valuation_observations_lookup",
        "valuation_observations",
        ["instrument_id", "metric", "observation_date"],
    )

    op.create_table(
        "valuation_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("benchmark_instrument_id", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("as_of_date", sa.Text(), nullable=False),
        sa.Column("current_value_micros", sa.Integer(), nullable=False),
        sa.Column("percentile_bps", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("lookback_start", sa.Text(), nullable=False),
        sa.Column("valuation_state", sa.Text(), nullable=False),
        sa.Column("proxy_suitability", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("calculation_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "metric IN ('PE','PB')",
            name="ck_valuation_snapshots_metric",
        ),
        sa.CheckConstraint(
            "percentile_bps BETWEEN 0 AND 10000",
            name="ck_valuation_snapshots_percentile",
        ),
        sa.CheckConstraint(
            "sample_count > 0",
            name="ck_valuation_snapshots_sample_count",
        ),
        sa.CheckConstraint(
            "valuation_state IN ('UNDERVALUED','FAIR','OVERPRICED','UNKNOWN')",
            name="ck_valuation_snapshots_state",
        ),
        sa.CheckConstraint(
            "proxy_suitability IN ('STRONG','WEAK')",
            name="ck_valuation_snapshots_suitability",
        ),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_valuation_snapshots_quality",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.ForeignKeyConstraint(
            ["benchmark_instrument_id"],
            ["instruments.id"],
        ),
        sa.UniqueConstraint(
            "portfolio_id",
            "instrument_id",
            "metric",
            "as_of_date",
            "input_hash",
            name="uq_valuation_snapshots_input",
        ),
    )

    op.create_table(
        "rule_hits",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text()),
        sa.Column("rule_code", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('INFO','WARNING','HIGH','CRITICAL')",
            name="ck_rule_hits_severity",
        ),
        sa.CheckConstraint(
            "status IN ('HIT','NOT_HIT','DATA_BLOCKED','EXEMPT')",
            name="ck_rule_hits_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.UniqueConstraint(
            "portfolio_id",
            "instrument_id",
            "rule_code",
            "input_hash",
            name="uq_rule_hits_input",
        ),
    )

    op.create_table(
        "sell_proposals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("strategy_version_id", sa.Text(), nullable=False),
        sa.Column("rule_hit_id", sa.Text(), nullable=False),
        sa.Column("trigger_code", sa.Text(), nullable=False),
        sa.Column("engine", sa.Text(), nullable=False),
        sa.Column("recommended_action", sa.Text(), nullable=False),
        sa.Column("recommended_fraction_bps", sa.Integer()),
        sa.Column("target_weight_bps", sa.Integer()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("trigger_facts_json", sa.Text(), nullable=False),
        sa.Column("trigger_input_hash", sa.Text(), nullable=False),
        sa.Column("proposed_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text()),
        sa.Column("closed_at", sa.Text()),
        sa.CheckConstraint(
            "trigger_code IN ('SELL_01_HARD_STOP','SELL_02_THESIS_INVALID','SELL_03_REBALANCE')",
            name="ck_sell_proposals_trigger",
        ),
        sa.CheckConstraint(
            "engine IN ('RISK','REALIZATION')",
            name="ck_sell_proposals_engine",
        ),
        sa.CheckConstraint(
            "recommended_action IN ('FULL_SELL','PARTIAL_SELL','REDUCE_TO_WEIGHT','MANUAL_REVIEW')",
            name="ck_sell_proposals_action",
        ),
        sa.CheckConstraint(
            "recommended_fraction_bps IS NULL OR recommended_fraction_bps BETWEEN 0 AND 10000",
            name="ck_sell_proposals_fraction",
        ),
        sa.CheckConstraint(
            "target_weight_bps IS NULL OR target_weight_bps BETWEEN 0 AND 10000",
            name="ck_sell_proposals_target",
        ),
        sa.CheckConstraint(
            "status IN ('REVIEW_REQUIRED','APPROVED','DEFERRED','REJECTED','EXPIRED')",
            name="ck_sell_proposals_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
        ),
        sa.ForeignKeyConstraint(["rule_hit_id"], ["rule_hits.id"]),
        sa.UniqueConstraint(
            "portfolio_id",
            "instrument_id",
            "trigger_code",
            "trigger_input_hash",
            name="uq_sell_proposals_input",
        ),
    )

    op.create_table(
        "sell_diagnostics",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("sell_proposal_id", sa.Text(), nullable=False, unique=True),
        sa.Column("diagnosis_version", sa.Text(), nullable=False),
        sa.Column("checklist_json", sa.Text(), nullable=False),
        sa.Column("portfolio_before_json", sa.Text(), nullable=False),
        sa.Column("portfolio_after_json", sa.Text(), nullable=False),
        sa.Column("followup_metric_json", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "result IN ('PASS','WARNING','BLOCK','DATA_BLOCKED')",
            name="ck_sell_diagnostics_result",
        ),
        sa.ForeignKeyConstraint(
            ["sell_proposal_id"],
            ["sell_proposals.id"],
        ),
    )

    op.create_table(
        "sell_decision_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("sell_proposal_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("user_reason", sa.Text()),
        sa.Column("proposal_hash", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.Column("committed_by", sa.Text()),
        sa.CheckConstraint(
            "decision IN ('APPROVE','DEFER','REJECT')",
            name="ck_sell_decision_drafts_decision",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED','CANCELLED')",
            name="ck_sell_decision_drafts_status",
        ),
        sa.ForeignKeyConstraint(
            ["sell_proposal_id"],
            ["sell_proposals.id"],
        ),
    )

    op.create_table(
        "sell_decisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("sell_proposal_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("user_reason", sa.Text()),
        sa.Column("decision_draft_id", sa.Text(), nullable=False, unique=True),
        sa.Column("decided_at", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('APPROVE','DEFER','REJECT')",
            name="ck_sell_decisions_decision",
        ),
        sa.ForeignKeyConstraint(
            ["sell_proposal_id"],
            ["sell_proposals.id"],
        ),
        sa.ForeignKeyConstraint(
            ["decision_draft_id"],
            ["sell_decision_drafts.id"],
        ),
    )


def downgrade() -> None:
    op.drop_table("sell_decisions")
    op.drop_table("sell_decision_drafts")
    op.drop_table("sell_diagnostics")
    op.drop_table("sell_proposals")
    op.drop_table("rule_hits")
    op.drop_table("valuation_snapshots")
    op.drop_index(
        "idx_valuation_observations_lookup",
        table_name="valuation_observations",
    )
    op.drop_table("valuation_observations")
    op.drop_table("strategy_config_drafts")
    with op.batch_alter_table("strategy_instrument_configs") as batch:
        batch.drop_constraint(
            "ck_strategy_instrument_configs_position_cap",
            type_="check",
        )
        batch.drop_constraint(
            "ck_strategy_instrument_configs_hard_stop",
            type_="check",
        )
        batch.drop_constraint(
            "ck_strategy_instrument_configs_proxy_suitability",
            type_="check",
        )
        batch.drop_column("maximum_position_weight_bps")
        batch.drop_column("hard_stop_return_bps")
        batch.drop_column("proxy_suitability")
