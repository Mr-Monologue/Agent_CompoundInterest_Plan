"""Add confirmation time precision and audited draft revision state.

Revision ID: 0032_confirmation_time_precision_revision
Revises: 0031_external_subscription_draft_renewal
Create Date: 2026-09-01
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_confirmation_time_precision_revision"
down_revision: str | None = "0031_external_subscription_draft_renewal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _payload_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rewrite_confirmation_draft_payloads(*, add_precision: bool) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT id, payload_json
            FROM external_subscription_drafts
            WHERE action='CONFIRM'
            """
        )
    ).mappings().all()
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        if add_precision:
            payload.setdefault("confirmed_at_precision", "EXACT")
        else:
            payload.pop("confirmed_at_precision", None)
        connection.execute(
            sa.text(
                """
                UPDATE external_subscription_drafts
                SET payload_json=:payload_json, payload_hash=:payload_hash
                WHERE id=:draft_id
                """
            ),
            {
                "payload_json": json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "payload_hash": _payload_hash(payload),
                "draft_id": row["id"],
            },
        )


def upgrade() -> None:
    with op.batch_alter_table("external_subscription_confirmations") as batch:
        batch.add_column(
            sa.Column(
                "confirmed_at_precision",
                sa.Text(),
                nullable=False,
                server_default="EXACT",
            )
        )
        batch.create_check_constraint(
            "ck_external_subscription_confirmations_time_precision",
            "confirmed_at_precision IN ('EXACT','DATE_ONLY')",
        )

    with op.batch_alter_table("external_subscription_drafts") as batch:
        batch.add_column(sa.Column("confirmed_at_precision", sa.Text()))
        batch.add_column(sa.Column("revised_at", sa.Text()))
        batch.add_column(
            sa.Column(
                "revision_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_check_constraint(
            "ck_external_subscription_drafts_time_precision",
            "confirmed_at_precision IS NULL OR "
            "confirmed_at_precision IN ('EXACT','DATE_ONLY')",
        )
        batch.create_check_constraint(
            "ck_external_subscription_drafts_revision_count",
            "revision_count >= 0",
        )

    _rewrite_confirmation_draft_payloads(add_precision=True)
    op.execute(
        """
        UPDATE external_subscription_drafts
        SET confirmed_at_precision='EXACT'
        WHERE action='CONFIRM'
        """
    )


def downgrade() -> None:
    _rewrite_confirmation_draft_payloads(add_precision=False)

    with op.batch_alter_table("external_subscription_drafts") as batch:
        batch.drop_constraint(
            "ck_external_subscription_drafts_revision_count",
            type_="check",
        )
        batch.drop_constraint(
            "ck_external_subscription_drafts_time_precision",
            type_="check",
        )
        batch.drop_column("revision_count")
        batch.drop_column("revised_at")
        batch.drop_column("confirmed_at_precision")

    with op.batch_alter_table("external_subscription_confirmations") as batch:
        batch.drop_constraint(
            "ck_external_subscription_confirmations_time_precision",
            type_="check",
        )
        batch.drop_column("confirmed_at_precision")
