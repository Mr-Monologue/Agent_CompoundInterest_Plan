"""Seed the approved versioned allocation policy for existing portfolios.

Revision ID: 0008_allocation_policy
Revises: 0007_source_lineage
Create Date: 2026-07-23
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0008_allocation_policy"
down_revision: str | None = "0007_source_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = {
    "policy_id": "value-dca-v1.6",
    "core_target_pct": "65.00",
    "satellite_target_pct": "35.00",
    "tolerance_pct": "10.00",
    "transition_trigger_pct": "15.00",
    "transition_exit_core_min_pct": "55.00",
    "transition_exit_satellite_max_pct": "45.00",
    "transition_principle": "INCREMENTAL_FUNDS_FIRST",
    "automatic_selling_allowed": False,
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upgrade() -> None:
    connection = op.get_bind()
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    value_json = _canonical_json(POLICY)
    value_hash = _canonical_hash(POLICY)
    portfolio_ids = connection.execute(
        sa.text("SELECT id FROM portfolios WHERE status = 'ACTIVE' ORDER BY id")
    ).scalars()
    for portfolio_id in portfolio_ids:
        key = f"allocation_policy:{portfolio_id}"
        existing = connection.execute(
            sa.text(
                """
                SELECT 1
                FROM settings
                WHERE key = :key AND status = 'ACTIVE'
                LIMIT 1
                """
            ),
            {"key": key},
        ).first()
        if existing is not None:
            continue
        connection.execute(
            sa.text(
                """
                INSERT INTO settings (
                    key, version, value_json, value_hash, status, approved_by,
                    approved_at, created_at
                ) VALUES (
                    :key, 1, :value_json, :value_hash, 'ACTIVE',
                    'system:approved-strategy-v1.6', :timestamp, :timestamp
                )
                """
            ),
            {
                "key": key,
                "value_json": value_json,
                "value_hash": value_hash,
                "timestamp": timestamp,
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO audit_events (
                    id, occurred_at, actor_type, actor_ref, action, entity_type,
                    entity_id, before_hash, after_hash, details_json, trace_id
                ) VALUES (
                    :id, :timestamp, 'SYSTEM', 'system:approved-strategy-v1.6',
                    'ALLOCATION_POLICY_INITIALIZED', 'setting', :key, NULL,
                    :value_hash, :details_json, :trace_id
                )
                """
            ),
            {
                "id": str(uuid4()),
                "timestamp": timestamp,
                "key": key,
                "value_hash": value_hash,
                "details_json": _canonical_json(
                    {
                        "portfolio_id": str(portfolio_id),
                        "version": 1,
                        "policy_id": POLICY["policy_id"],
                        "reason": "Approved Value-DCA architecture v1.6",
                    }
                ),
                "trace_id": str(uuid4()),
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM audit_events
            WHERE action = 'ALLOCATION_POLICY_INITIALIZED'
              AND actor_ref = 'system:approved-strategy-v1.6'
            """
        )
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM settings
            WHERE key LIKE 'allocation_policy:%'
              AND approved_by = 'system:approved-strategy-v1.6'
              AND version = 1
            """
        )
    )
