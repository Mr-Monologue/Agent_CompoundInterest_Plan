"""Add governed watchlists, research changes and review outcomes.

Revision ID: 0020_watchlist_research_outcomes
Revises: 0019_review_trends_discovery_changes
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_watchlist_research_outcomes"
down_revision: str | None = "0019_review_trends_discovery_changes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("rule_hits") as batch_op:
        batch_op.drop_constraint("ck_rule_hits_status", type_="check")
        batch_op.create_check_constraint(
            "ck_rule_hits_status",
            "status IN ('HIT','EVALUATED_NOT_HIT','NOT_CONFIGURED',"
            "'DATA_UNAVAILABLE','NOT_APPLICABLE','DATA_BLOCKED','EXEMPT','NOT_HIT')",
        )

    op.create_table(
        "research_evidence_changes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("evidence_id", sa.Text(), nullable=False),
        sa.Column("previous_evidence_id", sa.Text()),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("change_type", sa.Text(), nullable=False),
        sa.Column("added_keys_json", sa.Text(), nullable=False),
        sa.Column("removed_keys_json", sa.Text(), nullable=False),
        sa.Column("changed_keys_json", sa.Text(), nullable=False),
        sa.Column("facts_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "change_type IN ('INITIAL','UNCHANGED','CHANGED')",
            name="ck_research_evidence_change_type",
        ),
        sa.ForeignKeyConstraint(["evidence_id"], ["market_research_evidence.id"]),
        sa.ForeignKeyConstraint(["previous_evidence_id"], ["market_research_evidence.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.UniqueConstraint("evidence_id", name="uq_research_evidence_change"),
    )
    op.create_index(
        "idx_research_evidence_changes_instrument",
        "research_evidence_changes",
        ["instrument_id", "change_type", "created_at"],
    )

    op.create_table(
        "research_watchlist_entries",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("review_due_date", sa.Text()),
        sa.Column("latest_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "state IN ('CANDIDATE','OBSERVING','REVIEW_DUE','ADOPTED',"
            "'REJECTED','ARCHIVED')",
            name="ck_research_watchlist_state",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.UniqueConstraint(
            "portfolio_id",
            "instrument_id",
            name="uq_research_watchlist_instrument",
        ),
    )
    op.create_index(
        "idx_research_watchlist_state",
        "research_watchlist_entries",
        ["portfolio_id", "state", "review_due_date"],
    )

    op.create_table(
        "research_watchlist_transition_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("instrument_id", sa.Text(), nullable=False),
        sa.Column("previous_state", sa.Text()),
        sa.Column("new_state", sa.Text(), nullable=False),
        sa.Column("review_due_date", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("confirmation_token_digest", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.CheckConstraint(
            "new_state IN ('CANDIDATE','OBSERVING','REVIEW_DUE','ADOPTED',"
            "'REJECTED','ARCHIVED')",
            name="ck_research_watchlist_draft_state",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_research_watchlist_draft_status",
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )

    op.create_table(
        "research_watchlist_transitions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("draft_id", sa.Text(), nullable=False),
        sa.Column("entry_id", sa.Text(), nullable=False),
        sa.Column("previous_state", sa.Text()),
        sa.Column("new_state", sa.Text(), nullable=False),
        sa.Column("review_due_date", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("confirmed_by", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["draft_id"], ["research_watchlist_transition_drafts.id"]
        ),
        sa.ForeignKeyConstraint(["entry_id"], ["research_watchlist_entries.id"]),
        sa.UniqueConstraint("draft_id", name="uq_research_watchlist_transition_draft"),
    )

    op.create_table(
        "review_action_outcome_drafts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("action_item_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("evidence_quality", sa.Text(), nullable=False),
        sa.Column("evidence_ref", sa.Text()),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("confirmation_token_digest", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.Text()),
        sa.CheckConstraint(
            "outcome IN ('COMPLETED','PARTIAL','NOT_COMPLETED','NOT_APPLICABLE')",
            name="ck_review_action_outcome",
        ),
        sa.CheckConstraint(
            "evidence_quality IN ('VERIFIED','USER_REPORTED','UNVERIFIED')",
            name="ck_review_action_outcome_quality",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','COMMITTED','EXPIRED')",
            name="ck_review_action_outcome_draft_status",
        ),
        sa.ForeignKeyConstraint(["action_item_id"], ["review_action_items.id"]),
    )

    op.create_table(
        "review_action_outcomes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("draft_id", sa.Text(), nullable=False),
        sa.Column("action_item_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("evidence_quality", sa.Text(), nullable=False),
        sa.Column("evidence_ref", sa.Text()),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("facts_hash", sa.Text(), nullable=False),
        sa.Column("confirmed_by", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["review_action_outcome_drafts.id"]),
        sa.ForeignKeyConstraint(["action_item_id"], ["review_action_items.id"]),
        sa.UniqueConstraint("draft_id", name="uq_review_action_outcome_draft"),
        sa.UniqueConstraint("action_item_id", name="uq_review_action_outcome_action"),
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE rule_hits
        SET status = CASE
            WHEN status = 'EVALUATED_NOT_HIT' THEN 'NOT_HIT'
            WHEN status IN ('NOT_CONFIGURED','DATA_UNAVAILABLE','NOT_APPLICABLE')
                THEN 'NOT_HIT'
            ELSE status
        END
        """
    )
    with op.batch_alter_table("rule_hits") as batch_op:
        batch_op.drop_constraint("ck_rule_hits_status", type_="check")
        batch_op.create_check_constraint(
            "ck_rule_hits_status",
            "status IN ('HIT','NOT_HIT','DATA_BLOCKED','EXEMPT')",
        )
    op.drop_table("review_action_outcomes")
    op.drop_table("review_action_outcome_drafts")
    op.drop_table("research_watchlist_transitions")
    op.drop_table("research_watchlist_transition_drafts")
    op.drop_index("idx_research_watchlist_state", table_name="research_watchlist_entries")
    op.drop_table("research_watchlist_entries")
    op.drop_index(
        "idx_research_evidence_changes_instrument",
        table_name="research_evidence_changes",
    )
    op.drop_table("research_evidence_changes")
