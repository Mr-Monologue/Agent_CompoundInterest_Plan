"""Separate reusable strategies from portfolio instances and add plan storage.

Revision ID: 0009_strategy_instance_plan
Revises: 0008_allocation_policy
Create Date: 2026-07-24
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "0009_strategy_instance_plan"
down_revision: str | None = "0008_allocation_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PUBLIC_STRATEGY_KEY = "value-dca"
PUBLIC_STRATEGY_VERSION = "1.6"
PUBLIC_STRATEGY_ID = str(uuid5(NAMESPACE_URL, "value-dca:strategy:value-dca"))
PUBLIC_VERSION_ID = str(uuid5(NAMESPACE_URL, "value-dca:strategy:value-dca:1.6"))
PUBLIC_PARAMETERS = {
    "core_target_pct": "65.00",
    "satellite_target_pct": "35.00",
    "tolerance_pct": "10.00",
    "transition_trigger_pct": "15.00",
    "transition_exit_core_min_pct": "55.00",
    "transition_exit_satellite_max_pct": "45.00",
    "transition_principle": "INCREMENTAL_FUNDS_FIRST",
    "automatic_selling_allowed": False,
    "instrument_selection": "INSTANCE_ALLOWLIST_ONLY",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.create_table(
        "strategy_definitions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("strategy_key", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE','RETIRED')",
            name="ck_strategy_definitions_status",
        ),
    )

    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("strategy_definition_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("parameters_json", sa.Text(), nullable=False),
        sa.Column("parameters_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('PUBLISHED','RETIRED')",
            name="ck_strategy_versions_status",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_definition_id"],
            ["strategy_definitions.id"],
        ),
        sa.UniqueConstraint(
            "strategy_definition_id",
            "version",
            name="uq_strategy_versions_definition_version",
        ),
    )

    op.create_table(
        "strategy_assignments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("strategy_version_id", sa.Text(), nullable=False),
        sa.Column("instance_config_json", sa.Text(), nullable=False),
        sa.Column("instance_config_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("retired_at", sa.Text()),
        sa.CheckConstraint(
            "status IN ('ACTIVE','RETIRED')",
            name="ck_strategy_assignments_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"]),
    )
    op.create_index(
        "uq_strategy_assignments_active_portfolio",
        "strategy_assignments",
        ["portfolio_id"],
        unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "strategy_instrument_configs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("strategy_assignment_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("contribution_eligible", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("target_weight_bps", sa.Integer()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("minimum_amount_minor", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("maximum_amount_minor", sa.Integer()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("benchmark_instrument_id", sa.Text()),
        sa.Column("thesis_status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('CORE','SATELLITE','CASH','WATCH','UNASSIGNED')",
            name="ck_strategy_instrument_configs_role",
        ),
        sa.CheckConstraint(
            "contribution_eligible IN (0,1)",
            name="ck_strategy_instrument_configs_eligible",
        ),
        sa.CheckConstraint(
            "target_weight_bps IS NULL OR "
            "(target_weight_bps >= 0 AND target_weight_bps <= 10000)",
            name="ck_strategy_instrument_configs_target",
        ),
        sa.CheckConstraint(
            "priority >= 0",
            name="ck_strategy_instrument_configs_priority",
        ),
        sa.CheckConstraint(
            "minimum_amount_minor >= 0",
            name="ck_strategy_instrument_configs_minimum",
        ),
        sa.CheckConstraint(
            "maximum_amount_minor IS NULL OR maximum_amount_minor > 0",
            name="ck_strategy_instrument_configs_maximum",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','PAUSED','RETIRED')",
            name="ck_strategy_instrument_configs_status",
        ),
        sa.CheckConstraint(
            "thesis_status IN ('ACTIVE','REVIEW_REQUIRED','INVALID')",
            name="ck_strategy_instrument_configs_thesis",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_assignment_id"],
            ["strategy_assignments.id"],
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.ForeignKeyConstraint(["benchmark_instrument_id"], ["instruments.id"]),
        sa.UniqueConstraint(
            "strategy_assignment_id",
            "instrument_id",
            name="uq_strategy_instrument_configs_assignment_instrument",
        ),
    )
    op.create_index(
        "idx_strategy_instrument_configs_assignment",
        "strategy_instrument_configs",
        ["strategy_assignment_id", "status", "role"],
    )

    op.create_table(
        "investment_plans",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column("strategy_assignment_id", sa.Text(), nullable=False),
        sa.Column("plan_date", sa.Text(), nullable=False),
        sa.Column("contribution_amount_minor", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("confirmation_digest", sa.Text(), nullable=False),
        sa.Column("confirmation_expires_at", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("frozen_at", sa.Text()),
        sa.Column("executed_at", sa.Text()),
        sa.Column("expires_at", sa.Text()),
        sa.CheckConstraint(
            "contribution_amount_minor > 0",
            name="ck_investment_plans_contribution",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','FROZEN','EXECUTED','EXPIRED','SKIPPED')",
            name="ck_investment_plans_status",
        ),
        sa.CheckConstraint(
            "current_revision > 0",
            name="ck_investment_plans_revision",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(
            ["strategy_assignment_id"],
            ["strategy_assignments.id"],
        ),
    )
    op.create_index(
        "idx_investment_plans_portfolio_date",
        "investment_plans",
        ["portfolio_id", "plan_date", "status"],
    )

    op.create_table(
        "plan_revisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("plan_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_plan_revisions_quality",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["investment_plans.id"]),
        sa.UniqueConstraint("plan_id", "revision", name="uq_plan_revisions_plan_revision"),
    )

    op.create_table(
        "plan_items",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("plan_revision_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text()),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("valuation_state", sa.Text(), nullable=False),
        sa.Column("base_amount_minor", sa.Integer(), nullable=False),
        sa.Column("multiplier_bps", sa.Integer(), nullable=False),
        sa.Column("candidate_amount_minor", sa.Integer(), nullable=False),
        sa.Column("reserved_amount_minor", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("data_quality", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("explanation_facts_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role IN ('CORE','SATELLITE','CASH','UNASSIGNED')",
            name="ck_plan_items_role",
        ),
        sa.CheckConstraint(
            "base_amount_minor >= 0 AND candidate_amount_minor >= 0 "
            "AND reserved_amount_minor >= 0",
            name="ck_plan_items_amounts",
        ),
        sa.CheckConstraint(
            "multiplier_bps >= 0",
            name="ck_plan_items_multiplier",
        ),
        sa.CheckConstraint(
            "action IN ('CONTRIBUTE','RESERVE','REVIEW_REQUIRED','SKIP')",
            name="ck_plan_items_action",
        ),
        sa.CheckConstraint(
            "data_quality IN ('PASS','WARNING','SOURCE_ERROR')",
            name="ck_plan_items_quality",
        ),
        sa.ForeignKeyConstraint(["plan_revision_id"], ["plan_revisions.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )
    op.create_index(
        "idx_plan_items_revision",
        "plan_items",
        ["plan_revision_id", "role", "action"],
    )

    connection = op.get_bind()
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    connection.execute(
        sa.text(
            """
            INSERT INTO strategy_definitions (
                id, strategy_key, name, description, status, created_at
            ) VALUES (
                :id, :strategy_key, :name, :description, 'ACTIVE', :created_at
            )
            """
        ),
        {
            "id": PUBLIC_STRATEGY_ID,
            "strategy_key": PUBLIC_STRATEGY_KEY,
            "name": "Value DCA",
            "description": (
                "Reusable value-DCA rules. Contains no user holdings, fund codes, "
                "benchmark mappings, or portfolio decisions."
            ),
            "created_at": timestamp,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO strategy_versions (
                id, strategy_definition_id, version, parameters_json,
                parameters_hash, status, published_at, created_at
            ) VALUES (
                :id, :definition_id, :version, :parameters_json,
                :parameters_hash, 'PUBLISHED', :published_at, :created_at
            )
            """
        ),
        {
            "id": PUBLIC_VERSION_ID,
            "definition_id": PUBLIC_STRATEGY_ID,
            "version": PUBLIC_STRATEGY_VERSION,
            "parameters_json": _json(PUBLIC_PARAMETERS),
            "parameters_hash": _hash(PUBLIC_PARAMETERS),
            "published_at": timestamp,
            "created_at": timestamp,
        },
    )

    legacy_rows = connection.execute(
        sa.text(
            """
            SELECT p.id AS portfolio_id, s.value_json, s.value_hash,
                   s.approved_by, s.approved_at, s.created_at
            FROM portfolios p
            JOIN settings s
              ON s.key = ('allocation_policy:' || p.id)
             AND s.status = 'ACTIVE'
            WHERE p.status = 'ACTIVE'
            ORDER BY p.id
            """
        )
    ).mappings()
    for row in legacy_rows:
        portfolio_id = str(row["portfolio_id"])
        assignment_id = str(
            uuid5(NAMESPACE_URL, f"value-dca:assignment:{portfolio_id}:legacy")
        )
        legacy_policy = json.loads(str(row["value_json"]))
        instance_config = {
            "allocation_policy": legacy_policy,
            "migration": {
                "source": "legacy_allocation_policy",
                "source_hash": str(row["value_hash"]),
            },
        }
        instance_hash = _hash(instance_config)
        connection.execute(
            sa.text(
                """
                INSERT INTO strategy_assignments (
                    id, portfolio_id, strategy_version_id, instance_config_json,
                    instance_config_hash, status, approved_by, approved_at,
                    created_at, retired_at
                ) VALUES (
                    :id, :portfolio_id, :strategy_version_id, :config_json,
                    :config_hash, 'ACTIVE', :approved_by, :approved_at,
                    :created_at, NULL
                )
                """
            ),
            {
                "id": assignment_id,
                "portfolio_id": portfolio_id,
                "strategy_version_id": PUBLIC_VERSION_ID,
                "config_json": _json(instance_config),
                "config_hash": instance_hash,
                "approved_by": str(row["approved_by"]),
                "approved_at": str(row["approved_at"]),
                "created_at": str(row["created_at"]),
            },
        )
        instruments = connection.execute(
            sa.text(
                """
                SELECT id, role
                FROM instruments
                WHERE status = 'ACTIVE' AND role != 'UNASSIGNED'
                ORDER BY id
                """
            )
        ).mappings()
        for instrument in instruments:
            instrument_id = str(instrument["id"])
            config_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"value-dca:assignment:{assignment_id}:instrument:{instrument_id}",
                )
            )
            connection.execute(
                sa.text(
                    """
                    INSERT INTO strategy_instrument_configs (
                        id, strategy_assignment_id, instrument_id, role,
                        contribution_eligible, target_weight_bps, priority,
                        minimum_amount_minor, maximum_amount_minor, status,
                        benchmark_instrument_id, thesis_status, approved_by,
                        approved_at, created_at, updated_at
                    ) VALUES (
                        :id, :assignment_id, :instrument_id, :role,
                        0, NULL, 100, 1, NULL, 'ACTIVE',
                        NULL, 'ACTIVE', :approved_by,
                        :approved_at, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "id": config_id,
                    "assignment_id": assignment_id,
                    "instrument_id": instrument_id,
                    "role": str(instrument["role"]),
                    "approved_by": str(row["approved_by"]),
                    "approved_at": str(row["approved_at"]),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
        connection.execute(
            sa.text(
                """
                INSERT INTO audit_events (
                    id, occurred_at, actor_type, actor_ref, action, entity_type,
                    entity_id, before_hash, after_hash, details_json, trace_id
                ) VALUES (
                    :id, :occurred_at, 'SYSTEM', 'migration:0009',
                    'STRATEGY_ASSIGNMENT_MIGRATED', 'strategy_assignment',
                    :entity_id, :before_hash, :after_hash, :details_json, :trace_id
                )
                """
            ),
            {
                "id": str(uuid4()),
                "occurred_at": timestamp,
                "entity_id": assignment_id,
                "before_hash": str(row["value_hash"]),
                "after_hash": instance_hash,
                "details_json": _json(
                    {
                        "portfolio_id": portfolio_id,
                        "contribution_eligibility_inferred": False,
                    }
                ),
                "trace_id": str(uuid4()),
            },
        )


def downgrade() -> None:
    op.drop_index("idx_plan_items_revision", table_name="plan_items")
    op.drop_table("plan_items")
    op.drop_table("plan_revisions")
    op.drop_index("idx_investment_plans_portfolio_date", table_name="investment_plans")
    op.drop_table("investment_plans")
    op.drop_index(
        "idx_strategy_instrument_configs_assignment",
        table_name="strategy_instrument_configs",
    )
    op.drop_table("strategy_instrument_configs")
    op.drop_index(
        "uq_strategy_assignments_active_portfolio",
        table_name="strategy_assignments",
    )
    op.drop_table("strategy_assignments")
    op.drop_table("strategy_versions")
    op.drop_table("strategy_definitions")
