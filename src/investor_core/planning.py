"""Audited weekly investment-plan lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError
from investor_core.market_data import MONEY_SCALE, MarketDataService


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _minor(value: str) -> int:
    return int((Decimal(value) * MONEY_SCALE).to_integral_exact())


class PlanningService:
    """Persist plans separately from transaction records and brokerage execution."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.settings = settings
        self._now = now
        self._market_data = MarketDataService(settings, now=now)

    def _connect(self) -> sqlite3.Connection:
        database_path = (
            ":memory:"
            if str(self.settings.db_path) == ":memory:"
            else str(Path(self.settings.db_path).resolve())
        )
        connection = sqlite3.connect(database_path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @staticmethod
    def _rollback_and_raise(
        connection: sqlite3.Connection,
        error: LedgerError,
    ) -> NoReturn:
        connection.rollback()
        raise error

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        actor_type: str,
        actor_ref: str,
        action: str,
        entity_id: str,
        details: JsonDict,
        before_hash: str | None = None,
        after_hash: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                id, occurred_at, actor_type, actor_ref, action, entity_type,
                entity_id, before_hash, after_hash, details_json, trace_id
            ) VALUES (?, ?, ?, ?, ?, 'investment_plan', ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                _iso(self._now()),
                actor_type,
                actor_ref,
                action,
                entity_id,
                before_hash,
                after_hash,
                _json(details),
                str(uuid4()),
            ),
        )

    @staticmethod
    def _plan_query() -> str:
        return """
            SELECT p.*, a.approved_by AS strategy_approved_by,
                   d.strategy_key, v.version AS strategy_version
            FROM investment_plans p
            JOIN strategy_assignments a ON a.id = p.strategy_assignment_id
            JOIN strategy_versions v ON v.id = a.strategy_version_id
            JOIN strategy_definitions d ON d.id = v.strategy_definition_id
        """

    def _expire_if_needed(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> sqlite3.Row:
        if str(row["status"]) == "DRAFT" and _parse_iso(str(row["expires_at"])) <= self._now():
            connection.execute(
                """
                UPDATE investment_plans
                SET status = 'EXPIRED', updated_at = ?
                WHERE id = ? AND status = 'DRAFT'
                """,
                (_iso(self._now()), row["id"]),
            )
            self._audit(
                connection,
                actor_type="SYSTEM",
                actor_ref="plan-expiry",
                action="INVESTMENT_PLAN_EXPIRED",
                entity_id=str(row["id"]),
                details={"expires_at": str(row["expires_at"])},
            )
            row = connection.execute(
                self._plan_query() + " WHERE p.id = ?",
                (row["id"],),
            ).fetchone()
            assert row is not None
        return row

    def _plan_data(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> JsonDict:
        revision = connection.execute(
            """
            SELECT * FROM plan_revisions
            WHERE plan_id = ? AND revision = ?
            """,
            (row["id"], row["current_revision"]),
        ).fetchone()
        if revision is None:
            raise LedgerError(
                "INVALID_INVESTMENT_PLAN",
                "plan revision was not found",
                http_status=409,
            )
        items = connection.execute(
            """
            SELECT pi.*, i.code AS instrument_code, i.name AS instrument_name
            FROM plan_items pi
            LEFT JOIN instruments i ON i.id = pi.instrument_id
            WHERE pi.plan_revision_id = ?
            ORDER BY pi.role, i.code, pi.id
            """,
            (revision["id"],),
        ).fetchall()
        execution_progress = self._execution_progress(connection, row)
        return {
            "id": str(row["id"]),
            "portfolio_id": str(row["portfolio_id"]),
            "account_id": str(row["account_id"]),
            "strategy_assignment_id": str(row["strategy_assignment_id"]),
            "strategy": {
                "key": str(row["strategy_key"]),
                "version": str(row["strategy_version"]),
            },
            "plan_date": str(row["plan_date"]),
            "contribution_amount": (
                f"{Decimal(int(row['contribution_amount_minor'])) / MONEY_SCALE:.2f}"
            ),
            "status": str(row["status"]),
            "current_revision": int(row["current_revision"]),
            "idempotency_key": str(row["idempotency_key"]),
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "frozen_at": row["frozen_at"],
            "executed_at": row["executed_at"],
            "expires_at": row["expires_at"],
            "revision": {
                "id": str(revision["id"]),
                "revision": int(revision["revision"]),
                "input_hash": str(revision["input_hash"]),
                "summary": json.loads(str(revision["summary_json"])),
                "data_quality": str(revision["data_quality"]),
                "reason_code": str(revision["reason_code"]),
                "created_at": str(revision["created_at"]),
            },
            "items": [
                {
                    "id": str(item["id"]),
                    "instrument_id": (
                        str(item["instrument_id"]) if item["instrument_id"] is not None else None
                    ),
                    "instrument_code": (
                        str(item["instrument_code"])
                        if item["instrument_code"] is not None
                        else None
                    ),
                    "instrument_name": (
                        str(item["instrument_name"])
                        if item["instrument_name"] is not None
                        else None
                    ),
                    "role": str(item["role"]),
                    "valuation_state": str(item["valuation_state"]),
                    "base_amount": (f"{Decimal(int(item['base_amount_minor'])) / MONEY_SCALE:.2f}"),
                    "multiplier": f"{Decimal(int(item['multiplier_bps'])) / 10000:.4f}",
                    "candidate_amount": (
                        f"{Decimal(int(item['candidate_amount_minor'])) / MONEY_SCALE:.2f}"
                    ),
                    "reserved_amount": (
                        f"{Decimal(int(item['reserved_amount_minor'])) / MONEY_SCALE:.2f}"
                    ),
                    "action": str(item["action"]),
                    "data_quality": str(item["data_quality"]),
                    "reason_code": str(item["reason_code"]),
                    "explanation_facts": json.loads(str(item["explanation_facts_json"])),
                }
                for item in items
            ],
            "execution_progress": execution_progress,
        }

    def _execution_progress(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> JsonDict:
        planned_rows = connection.execute(
            """
            SELECT pi.instrument_id, i.code AS instrument_code,
                   i.name AS instrument_name, pi.candidate_amount_minor
            FROM plan_items pi
            JOIN plan_revisions pr ON pr.id = pi.plan_revision_id
            JOIN instruments i ON i.id = pi.instrument_id
            WHERE pr.plan_id = ? AND pr.revision = ?
              AND pi.action = 'CONTRIBUTE' AND pi.candidate_amount_minor > 0
            ORDER BY i.code
            """,
            (row["id"], row["current_revision"]),
        ).fetchall()
        linked_rows = connection.execute(
            """
            SELECT l.id, l.transaction_id, l.linked_amount_minor, l.linked_at,
                   l.linked_by, t.instrument_id, t.trade_date, t.amount_minor,
                   t.reversed_by_transaction_id
            FROM plan_execution_links l
            JOIN transactions t ON t.id = l.transaction_id
            WHERE l.plan_id = ?
            ORDER BY t.trade_date, t.committed_at, t.id
            """,
            (row["id"],),
        ).fetchall()
        valid_by_instrument: dict[str, int] = {}
        valid_count = 0
        reversed_count = 0
        links: list[JsonDict] = []
        for link in linked_rows:
            reversed_transaction = link["reversed_by_transaction_id"] is not None
            if reversed_transaction:
                reversed_count += 1
            else:
                valid_count += 1
                instrument_id = str(link["instrument_id"])
                valid_by_instrument[instrument_id] = (
                    valid_by_instrument.get(instrument_id, 0)
                    + int(link["linked_amount_minor"])
                )
            links.append(
                {
                    "transaction_id": str(link["transaction_id"]),
                    "instrument_id": str(link["instrument_id"]),
                    "trade_date": str(link["trade_date"]),
                    "transaction_amount": (
                        f"{Decimal(int(link['amount_minor'])) / MONEY_SCALE:.2f}"
                    ),
                    "linked_amount": (
                        f"{Decimal(int(link['linked_amount_minor'])) / MONEY_SCALE:.2f}"
                    ),
                    "reversed": reversed_transaction,
                    "linked_at": str(link["linked_at"]),
                    "linked_by": str(link["linked_by"]),
                }
            )

        subscription_rows = connection.execute(
            """
            WITH booked AS (
                SELECT c.subscription_id,
                       COALESCE(SUM(
                           CASE WHEN t.reversed_by_transaction_id IS NULL
                                THEN l.plan_linked_amount_minor ELSE 0 END
                       ), 0) AS booked_minor
                FROM external_subscription_confirmations c
                JOIN subscription_confirmation_transaction_links l
                  ON l.confirmation_id=c.id
                JOIN transactions t ON t.id=l.transaction_id
                WHERE c.kind='CONFIRMATION'
                  AND c.reversed_by_confirmation_id IS NULL
                GROUP BY c.subscription_id
            )
            SELECT s.instrument_id,
                   COALESCE(SUM(
                       s.requested_amount_minor - s.cancelled_amount_minor
                       - s.refunded_amount_minor
                   ), 0) AS active_requested_minor,
                   COALESCE(SUM(s.cancelled_amount_minor + s.refunded_amount_minor), 0)
                       AS cancelled_or_refunded_minor,
                   COALESCE(SUM(b.booked_minor), 0) AS booked_minor
            FROM external_subscriptions s
            LEFT JOIN booked b ON b.subscription_id=s.id
            WHERE s.weekly_plan_id=?
            GROUP BY s.instrument_id
            """,
            (row["id"],),
        ).fetchall()
        in_flight_by_instrument = {
            str(item["instrument_id"]): max(
                int(item["active_requested_minor"]) - int(item["booked_minor"]), 0
            )
            for item in subscription_rows
        }
        cancelled_by_instrument = {
            str(item["instrument_id"]): int(item["cancelled_or_refunded_minor"])
            for item in subscription_rows
        }

        items: list[JsonDict] = []
        planned_total = 0
        executed_total = 0
        in_flight_total = 0
        cancelled_total = 0
        complete = bool(planned_rows)
        for planned in planned_rows:
            instrument_id = str(planned["instrument_id"])
            planned_minor = int(planned["candidate_amount_minor"])
            executed_minor = valid_by_instrument.get(instrument_id, 0)
            remaining_minor = max(planned_minor - executed_minor, 0)
            excess_minor = max(executed_minor - planned_minor, 0)
            in_flight_minor = min(
                in_flight_by_instrument.get(instrument_id, 0), remaining_minor
            )
            unsubmitted_minor = max(remaining_minor - in_flight_minor, 0)
            cancelled_minor = cancelled_by_instrument.get(instrument_id, 0)
            planned_total += planned_minor
            executed_total += executed_minor
            in_flight_total += in_flight_minor
            cancelled_total += cancelled_minor
            if executed_minor != planned_minor:
                complete = False
            items.append(
                {
                    "instrument_id": instrument_id,
                    "instrument_code": str(planned["instrument_code"]),
                    "instrument_name": str(planned["instrument_name"]),
                    "planned_amount": f"{Decimal(planned_minor) / MONEY_SCALE:.2f}",
                    "executed_amount": f"{Decimal(executed_minor) / MONEY_SCALE:.2f}",
                    "remaining_amount": f"{Decimal(remaining_minor) / MONEY_SCALE:.2f}",
                    "in_flight_amount": f"{Decimal(in_flight_minor) / MONEY_SCALE:.2f}",
                    "unsubmitted_amount": (
                        f"{Decimal(unsubmitted_minor) / MONEY_SCALE:.2f}"
                    ),
                    "cancelled_or_refunded_amount": (
                        f"{Decimal(cancelled_minor) / MONEY_SCALE:.2f}"
                    ),
                    "excess_amount": f"{Decimal(excess_minor) / MONEY_SCALE:.2f}",
                    "complete": executed_minor == planned_minor,
                }
            )
        remaining_total = sum(_minor(str(item["remaining_amount"])) for item in items)
        return {
            "status": str(row["status"]),
            "amount_semantics": "PLANNED_CASH_OUTFLOW",
            "fee_treatment": "CONFIRMED_PRINCIPAL_PLUS_FEE_COUNTS_TOWARD_PLAN",
            "planned_amount": f"{Decimal(planned_total) / MONEY_SCALE:.2f}",
            "executed_amount": f"{Decimal(executed_total) / MONEY_SCALE:.2f}",
            "remaining_amount": f"{Decimal(remaining_total) / MONEY_SCALE:.2f}",
            "in_flight_amount": f"{Decimal(in_flight_total) / MONEY_SCALE:.2f}",
            "unsubmitted_amount": (
                f"{Decimal(max(remaining_total - in_flight_total, 0)) / MONEY_SCALE:.2f}"
            ),
            "cancelled_or_refunded_amount": (
                f"{Decimal(cancelled_total) / MONEY_SCALE:.2f}"
            ),
            "linked_transaction_count": len(linked_rows),
            "valid_transaction_count": valid_count,
            "reversed_transaction_count": reversed_count,
            "complete": complete,
            "items": items,
            "links": links,
        }

    def create_draft(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        contribution_amount: str,
        plan_date_value: str,
        idempotency_key: str,
        as_of_date_value: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        """Create a DRAFT from the exact deterministic preview; never create trades."""
        try:
            plan_date = date.fromisoformat(plan_date_value)
        except ValueError as exc:
            raise LedgerError("INVALID_DATE", "plan_date must be an ISO date") from exc
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise LedgerError("MISSING_REQUIRED_FIELD", "idempotency_key is required")
        preview = self._market_data.weekly_plan_preview(
            portfolio_id=portfolio_id,
            account_id=account_id,
            contribution_amount=contribution_amount,
            as_of_date_value=as_of_date_value,
        )
        if not preview["available"]:
            raise LedgerError(
                "INVESTMENT_PLAN_BLOCKED",
                "the deterministic weekly plan preview is blocked",
                http_status=409,
                details={"reason_code": preview["reason_code"]},
            )
        assignment_id = str(preview["policy"]["strategy_assignment_id"])
        request = {
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "strategy_assignment_id": assignment_id,
            "plan_date": plan_date.isoformat(),
            "contribution_amount": contribution_amount,
            "as_of_date": as_of_date_value,
            "preview_input_hash": _hash(preview["plan"]),
        }
        request_hash = _hash(request)
        connection = self._connect()
        token: str | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                self._plan_query() + " WHERE p.idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_hash"]) != request_hash:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "IDEMPOTENCY_CONFLICT",
                            "idempotency key was already used for different plan content",
                            http_status=409,
                        ),
                    )
                existing = self._expire_if_needed(connection, existing)
                data = self._plan_data(connection, existing)
                connection.commit()
                return {
                    "plan": data,
                    "confirmation_token": None,
                    "reused": True,
                    "display_text": preview["display_text"],
                    "warnings": preview["warnings"],
                }

            now = self._now()
            expires_at = now + timedelta(minutes=self.settings.confirmation_ttl_minutes)
            token = secrets.token_urlsafe(24)
            plan_id = str(uuid4())
            revision_id = str(uuid4())
            contribution_minor = _minor(str(preview["plan"]["contribution_amount"]))
            connection.execute(
                """
                INSERT INTO investment_plans (
                    id, portfolio_id, account_id, strategy_assignment_id,
                    plan_date, contribution_amount_minor, idempotency_key,
                    request_hash, status, current_revision, created_by,
                    confirmation_digest, confirmation_expires_at, created_at,
                    updated_at, frozen_at, executed_at, expires_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', 1, ?, ?, ?, ?, ?,
                    NULL, NULL, ?
                )
                """,
                (
                    plan_id,
                    portfolio_id,
                    account_id,
                    assignment_id,
                    plan_date.isoformat(),
                    contribution_minor,
                    normalized_key,
                    request_hash,
                    actor_ref,
                    _token_digest(token),
                    _iso(expires_at),
                    _iso(now),
                    _iso(now),
                    _iso(expires_at),
                ),
            )
            connection.execute(
                """
                INSERT INTO plan_revisions (
                    id, plan_id, revision, input_json, input_hash, summary_json,
                    data_quality, reason_code, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    plan_id,
                    _json(request),
                    request_hash,
                    _json(preview),
                    preview["data_quality"],
                    preview["reason_code"],
                    _iso(now),
                ),
            )
            for item in preview["plan"]["instrument_items"]:
                connection.execute(
                    """
                    INSERT INTO plan_items (
                        id, plan_revision_id, instrument_id, role,
                        valuation_state, base_amount_minor, multiplier_bps,
                        candidate_amount_minor, reserved_amount_minor, action,
                        data_quality, reason_code, explanation_facts_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        revision_id,
                        item["instrument_id"],
                        item["role"],
                        item["valuation_state"],
                        _minor(item["base_amount"]),
                        int(Decimal(item["multiplier"]) * 10000),
                        _minor(item["candidate_amount"]),
                        _minor(item["reserved_amount"]),
                        item["action"],
                        item["data_quality"],
                        item["reason_code"],
                        _json(item["explanation_facts"]),
                    ),
                )
            self._audit(
                connection,
                actor_type="AGENT",
                actor_ref=actor_ref,
                action="INVESTMENT_PLAN_DRAFT_CREATED",
                entity_id=plan_id,
                details={
                    "plan_date": plan_date.isoformat(),
                    "contribution_amount": contribution_amount,
                    "idempotency_key": normalized_key,
                    "transaction_draft_created": False,
                },
                after_hash=request_hash,
            )
            row = connection.execute(
                self._plan_query() + " WHERE p.id = ?",
                (plan_id,),
            ).fetchone()
            assert row is not None
            data = self._plan_data(connection, row)
            connection.commit()
            return {
                "plan": data,
                "confirmation_token": token,
                "reused": False,
                "display_text": preview["display_text"],
                "warnings": preview["warnings"],
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, *, plan_id: str) -> JsonDict:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                self._plan_query() + " WHERE p.id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVESTMENT_PLAN_NOT_FOUND",
                        "investment plan was not found",
                        http_status=404,
                    ),
                )
            row = self._expire_if_needed(connection, row)
            result = self._plan_data(connection, row)
            connection.commit()
            return result
        finally:
            connection.close()

    def list(
        self,
        *,
        portfolio_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        if limit < 1 or limit > 500:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 500")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            query = self._plan_query() + " WHERE 1 = 1"
            parameters: list[Any] = []
            if portfolio_id:
                query += " AND p.portfolio_id = ?"
                parameters.append(portfolio_id)
            if status:
                normalized_status = status.strip().upper()
                if normalized_status not in {
                    "DRAFT",
                    "FROZEN",
                    "PARTIALLY_EXECUTED",
                    "EXECUTED",
                    "EXPIRED",
                    "SKIPPED",
                }:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "INVALID_PLAN_STATUS",
                            "unsupported plan status",
                        ),
                    )
                query += " AND p.status = ?"
                parameters.append(normalized_status)
            query += " ORDER BY p.plan_date DESC, p.created_at DESC LIMIT ?"
            parameters.append(limit)
            rows = connection.execute(query, parameters).fetchall()
            result: list[JsonDict] = []
            for row in rows:
                row = self._expire_if_needed(connection, row)
                result.append(self._plan_data(connection, row))
            connection.commit()
            return result
        finally:
            connection.close()

    def _confirmed_transition(
        self,
        *,
        plan_id: str,
        confirmation_token: str,
        target_status: str,
        confirmed_by: str,
        reason: str,
    ) -> JsonDict:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                self._plan_query() + " WHERE p.id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVESTMENT_PLAN_NOT_FOUND",
                        "investment plan was not found",
                        http_status=404,
                    ),
                )
            row = self._expire_if_needed(connection, row)
            current_status = str(row["status"])
            allowed = {"DRAFT"} if target_status == "FROZEN" else {"DRAFT", "FROZEN"}
            if current_status == target_status:
                result = self._plan_data(connection, row)
                connection.commit()
                return result
            if current_status not in allowed:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVALID_PLAN_TRANSITION",
                        f"cannot transition {current_status} to {target_status}",
                        http_status=409,
                    ),
                )
            if target_status == "FROZEN":
                unresolved = connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM plan_items pi
                    JOIN plan_revisions pr ON pr.id = pi.plan_revision_id
                    WHERE pr.plan_id = ? AND pr.revision = ?
                      AND (pi.reserved_amount_minor > 0
                           OR pi.action = 'REVIEW_REQUIRED')
                    """,
                    (plan_id, row["current_revision"]),
                ).fetchone()
                if unresolved is not None and int(unresolved["count"]) > 0:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "PLAN_NOT_EXECUTABLE",
                            "a plan with reserved or review-required items cannot be frozen",
                            http_status=409,
                        ),
                    )
            if _parse_iso(str(row["confirmation_expires_at"])) <= self._now():
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "CONFIRMATION_EXPIRED",
                        "plan confirmation token has expired",
                        http_status=409,
                    ),
                )
            if not hmac.compare_digest(
                str(row["confirmation_digest"]),
                _token_digest(confirmation_token),
            ):
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "CONFIRMATION_MISMATCH",
                        "confirmation token does not match this plan",
                        http_status=409,
                    ),
                )
            timestamp = _iso(self._now())
            frozen_at = timestamp if target_status == "FROZEN" else row["frozen_at"]
            connection.execute(
                """
                UPDATE investment_plans
                SET status = ?, updated_at = ?, frozen_at = ?
                WHERE id = ?
                """,
                (target_status, timestamp, frozen_at, plan_id),
            )
            self._audit(
                connection,
                actor_type="USER",
                actor_ref=confirmed_by.strip(),
                action=f"INVESTMENT_PLAN_{target_status}",
                entity_id=plan_id,
                details={"reason": reason.strip()},
                before_hash=str(row["request_hash"]),
                after_hash=str(row["request_hash"]),
            )
            updated = connection.execute(
                self._plan_query() + " WHERE p.id = ?",
                (plan_id,),
            ).fetchone()
            assert updated is not None
            result = self._plan_data(connection, updated)
            connection.commit()
            return result
        finally:
            connection.close()

    def freeze(
        self,
        *,
        plan_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        """Freeze an exact DRAFT after explicit user confirmation."""
        return self._confirmed_transition(
            plan_id=plan_id,
            confirmation_token=confirmation_token,
            target_status="FROZEN",
            confirmed_by=confirmed_by,
            reason="User confirmed exact plan revision",
        )

    def skip(
        self,
        *,
        plan_id: str,
        confirmation_token: str,
        confirmed_by: str,
        reason: str,
    ) -> JsonDict:
        """Skip a DRAFT or FROZEN plan without creating a transaction."""
        if not reason.strip():
            raise LedgerError("INVALID_REASON", "skip reason is required")
        return self._confirmed_transition(
            plan_id=plan_id,
            confirmation_token=confirmation_token,
            target_status="SKIPPED",
            confirmed_by=confirmed_by,
            reason=reason,
        )

    def _insert_execution_link(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        transaction_id: str,
        confirmed_by: str,
        linked_amount_minor: int | None = None,
    ) -> None:
        existing = connection.execute(
            "SELECT plan_id FROM plan_execution_links WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if existing is not None:
            code = (
                "PLAN_TRANSACTION_ALREADY_LINKED"
                if str(existing["plan_id"]) == str(row["id"])
                else "TRANSACTION_USED_BY_ANOTHER_PLAN"
            )
            message = (
                "该成交已经关联到当前周计划, 不能重复关联。"
                if code == "PLAN_TRANSACTION_ALREADY_LINKED"
                else "该成交已经被其他周计划使用, 不能重复关联。"
            )
            self._rollback_and_raise(
                connection,
                LedgerError(code, message, http_status=409),
            )
        transaction = connection.execute(
            """
            SELECT t.*, i.code AS instrument_code, i.name AS instrument_name
            FROM transactions t
            JOIN instruments i ON i.id = t.instrument_id
            WHERE t.id = ?
            """,
            (transaction_id,),
        ).fetchone()
        if transaction is None:
            self._rollback_and_raise(
                connection,
                LedgerError(
                    "TRANSACTION_NOT_COMMITTED",
                    "没有找到已确认的真实成交记录, 不能关联到周计划。",
                    http_status=409,
                ),
            )
        if transaction["reversed_by_transaction_id"] is not None:
            self._rollback_and_raise(
                connection,
                LedgerError(
                    "TRANSACTION_ALREADY_REVERSED",
                    "该成交已经冲销, 不能用于完成周计划。",
                    http_status=409,
                ),
            )
        if str(transaction["portfolio_id"]) != str(row["portfolio_id"]):
            self._rollback_and_raise(
                connection,
                LedgerError(
                    "PLAN_TRANSACTION_PORTFOLIO_MISMATCH",
                    "该成交不属于当前周计划的投资组合。",
                    http_status=409,
                ),
            )
        if str(transaction["account_id"]) != str(row["account_id"]):
            self._rollback_and_raise(
                connection,
                LedgerError(
                    "PLAN_TRANSACTION_ACCOUNT_MISMATCH",
                    "该成交不属于当前周计划的账户。",
                    http_status=409,
                ),
            )
        if str(transaction["kind"]) != "TRADE" or str(transaction["side"]) != "BUY":
            self._rollback_and_raise(
                connection,
                LedgerError(
                    "PLAN_TRANSACTION_NOT_BUY",
                    "周计划只能关联已经确认且未冲销的真实买入记录。",
                    http_status=409,
                ),
            )
        planned = connection.execute(
            """
            SELECT pi.candidate_amount_minor
            FROM plan_items pi
            JOIN plan_revisions pr ON pr.id = pi.plan_revision_id
            WHERE pr.plan_id = ? AND pr.revision = ?
              AND pi.instrument_id = ? AND pi.action = 'CONTRIBUTE'
              AND pi.candidate_amount_minor > 0
            """,
            (row["id"], row["current_revision"], transaction["instrument_id"]),
        ).fetchone()
        if planned is None:
            self._rollback_and_raise(
                connection,
                LedgerError(
                    "PLAN_INSTRUMENT_MISMATCH",
                    f"{transaction['instrument_name']}({transaction['instrument_code']})"
                    "不在该冻结周计划中。",
                    http_status=409,
                ),
            )
        accumulated = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(l.linked_amount_minor), 0)
                FROM plan_execution_links l
                JOIN transactions t ON t.id = l.transaction_id
                WHERE l.plan_id = ? AND t.instrument_id = ?
                  AND t.reversed_by_transaction_id IS NULL
                """,
                (row["id"], transaction["instrument_id"]),
            ).fetchone()[0]
        )
        planned_minor = int(planned["candidate_amount_minor"])
        transaction_minor = int(transaction["amount_minor"])
        effective_linked_minor = (
            transaction_minor if linked_amount_minor is None else linked_amount_minor
        )
        if effective_linked_minor <= 0:
            self._rollback_and_raise(
                connection,
                LedgerError(
                    "INVALID_LINKED_AMOUNT",
                    "The plan-linked cash amount must be positive.",
                ),
            )
        if accumulated + effective_linked_minor > planned_minor:
            remaining_minor = max(planned_minor - accumulated, 0)
            self._rollback_and_raise(
                connection,
                LedgerError(
                    "PLAN_EXECUTION_AMOUNT_EXCEEDED",
                    f"{transaction['instrument_name']}({transaction['instrument_code']})"
                    "的累计成交金额将超过计划金额, 不能关联。",
                    http_status=409,
                    details={
                        "planned_amount": f"{Decimal(planned_minor) / MONEY_SCALE:.2f}",
                        "accumulated_amount": (
                            f"{Decimal(accumulated) / MONEY_SCALE:.2f}"
                        ),
                        "transaction_amount": (
                            f"{Decimal(effective_linked_minor) / MONEY_SCALE:.2f}"
                        ),
                        "remaining_amount": (
                            f"{Decimal(remaining_minor) / MONEY_SCALE:.2f}"
                        ),
                    },
                ),
            )
        connection.execute(
            """
            INSERT INTO plan_execution_links (
                id, plan_id, transaction_id, linked_amount_minor, linked_at, linked_by
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                row["id"],
                transaction_id,
                effective_linked_minor,
                _iso(self._now()),
                confirmed_by,
            ),
        )

    def _refresh_execution_status(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
    ) -> tuple[sqlite3.Row, JsonDict]:
        progress = self._execution_progress(connection, row)
        target_status = "EXECUTED" if progress["complete"] else "PARTIALLY_EXECUTED"
        timestamp = _iso(self._now())
        connection.execute(
            """
            UPDATE investment_plans
            SET status = ?, updated_at = ?, executed_at = ?
            WHERE id = ?
            """,
            (
                target_status,
                timestamp,
                timestamp if target_status == "EXECUTED" else None,
                row["id"],
            ),
        )
        updated = connection.execute(
            self._plan_query() + " WHERE p.id = ?",
            (row["id"],),
        ).fetchone()
        assert updated is not None
        return updated, self._execution_progress(connection, updated)

    def link_transaction(
        self,
        *,
        plan_id: str,
        transaction_id: str,
        confirmed_by: str,
        linked_amount: str | None = None,
    ) -> JsonDict:
        """Attach one committed BUY fact and deterministically refresh plan progress."""
        actor = confirmed_by.strip()
        if not actor:
            raise LedgerError("CONFIRMED_BY_REQUIRED", "必须记录本次关联的确认人。")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                self._plan_query() + " WHERE p.id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVESTMENT_PLAN_NOT_FOUND",
                        "没有找到该周计划。",
                        http_status=404,
                    ),
                )
            if str(row["status"]) not in {"FROZEN", "PARTIALLY_EXECUTED"}:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVALID_PLAN_TRANSITION",
                        "只有已冻结或部分执行的周计划可以继续关联真实买入记录。",
                        http_status=409,
                    ),
                )
            self._insert_execution_link(
                connection,
                row=row,
                transaction_id=transaction_id,
                confirmed_by=actor,
                linked_amount_minor=(
                    _minor(linked_amount) if linked_amount is not None else None
                ),
            )
            updated, progress = self._refresh_execution_status(connection, row=row)
            self._audit(
                connection,
                actor_type="USER",
                actor_ref=actor,
                action=(
                    "INVESTMENT_PLAN_EXECUTED"
                    if str(updated["status"]) == "EXECUTED"
                    else "INVESTMENT_PLAN_PARTIALLY_EXECUTED"
                ),
                entity_id=plan_id,
                details={
                    "transaction_id": transaction_id,
                    "executed_amount": progress["executed_amount"],
                    "remaining_amount": progress["remaining_amount"],
                    "fee_treatment": progress["fee_treatment"],
                },
            )
            result = self._plan_data(connection, updated)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def mark_executed(
        self,
        *,
        plan_id: str,
        transaction_ids: Sequence[str],
        confirmed_by: str,
    ) -> JsonDict:
        """Atomically link committed BUY records only when they complete a plan."""
        if not transaction_ids:
            raise LedgerError(
                "TRANSACTION_EVIDENCE_REQUIRED",
                "至少需要一条已确认的真实买入记录。",
            )
        if len(transaction_ids) != len(set(transaction_ids)):
            raise LedgerError(
                "DUPLICATE_TRANSACTION_EVIDENCE",
                "同一成交不能在一次请求中重复关联。",
            )
        actor = confirmed_by.strip()
        if not actor:
            raise LedgerError("CONFIRMED_BY_REQUIRED", "必须记录本次关联的确认人。")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                self._plan_query() + " WHERE p.id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVESTMENT_PLAN_NOT_FOUND",
                        "没有找到该周计划。",
                        http_status=404,
                    ),
                )
            if str(row["status"]) == "EXECUTED":
                result = self._plan_data(connection, row)
                connection.commit()
                return result
            if str(row["status"]) not in {"FROZEN", "PARTIALLY_EXECUTED"}:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVALID_PLAN_TRANSITION",
                        "只有已冻结或部分执行的周计划可以标记为已执行。",
                        http_status=409,
                    ),
                )
            for transaction_id in transaction_ids:
                self._insert_execution_link(
                    connection,
                    row=row,
                    transaction_id=transaction_id,
                    confirmed_by=actor,
                )
            updated, progress = self._refresh_execution_status(connection, row=row)
            if str(updated["status"]) != "EXECUTED":
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "PLAN_EXECUTION_INCOMPLETE",
                        "仍有基金或金额未完成, 周计划只能保持部分执行。",
                        http_status=409,
                        details={"execution_progress": progress},
                    ),
                )
            self._audit(
                connection,
                actor_type="USER",
                actor_ref=actor,
                action="INVESTMENT_PLAN_EXECUTED",
                entity_id=plan_id,
                details={
                    "transaction_ids": sorted(transaction_ids),
                    "executed_amount": progress["executed_amount"],
                    "remaining_amount": progress["remaining_amount"],
                    "fee_treatment": progress["fee_treatment"],
                },
            )
            result = self._plan_data(connection, updated)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
