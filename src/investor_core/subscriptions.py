"""Governed lifecycle for fund subscriptions performed on external platforms."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4
from zoneinfo import ZoneInfo

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError, LedgerService
from investor_core.planning import PlanningService

MONEY_SCALE = 100
NAV_SCALE = 1_000_000
SHARE_SCALE = 1_000_000


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _scaled(value: str, scale: int, field: str) -> int:
    try:
        result = int((Decimal(value) * scale).to_integral_exact())
    except Exception as exc:
        raise LedgerError("INVALID_DECIMAL", f"{field} must use an exact decimal") from exc
    if result < 0:
        raise LedgerError("INVALID_AMOUNT", f"{field} must not be negative")
    return result


def _money(value: int) -> str:
    return f"{Decimal(value) / MONEY_SCALE:.2f}"


def _decimal(value: int, scale: int, places: int) -> str:
    return f"{Decimal(value) / scale:.{places}f}"


class SubscriptionService:
    """Store external facts without executing orders or inferring platform state."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.settings = settings
        self._now = now
        self._ledger = LedgerService(settings, now=now)
        self._planning = PlanningService(settings, now=now)

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
        connection: sqlite3.Connection, error: LedgerError
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
        entity_type: str,
        entity_id: str,
        details: JsonDict,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                id, occurred_at, actor_type, actor_ref, action,
                entity_type, entity_id, details_json, before_hash, after_hash, trace_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                str(uuid4()),
                _iso(self._now()),
                actor_type,
                actor_ref,
                action,
                entity_type,
                entity_id,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                str(uuid4()),
            ),
        )

    @staticmethod
    def _subscription_query() -> str:
        return """
            SELECT s.*, i.code AS instrument_code, i.name AS instrument_name,
                   p.plan_date, p.status AS plan_status
            FROM external_subscriptions s
            JOIN instruments i ON i.id=s.instrument_id
            JOIN investment_plans p ON p.id=s.weekly_plan_id
        """

    def _subscription_row(
        self, connection: sqlite3.Connection, subscription_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            self._subscription_query() + " WHERE s.id=?", (subscription_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(
                "EXTERNAL_SUBSCRIPTION_NOT_FOUND",
                "没有找到该场外申购记录。",
                http_status=404,
            )
        assert isinstance(row, sqlite3.Row)
        return row

    @staticmethod
    def _active_confirmation_rows(
        connection: sqlite3.Connection, subscription_id: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT c.*, l.transaction_draft_id, l.transaction_id,
                   l.plan_linked_amount_minor, t.reversed_by_transaction_id
            FROM external_subscription_confirmations c
            LEFT JOIN subscription_confirmation_transaction_links l
              ON l.confirmation_id=c.id
            LEFT JOIN transactions t ON t.id=l.transaction_id
            WHERE c.subscription_id=? AND c.kind='CONFIRMATION'
              AND c.reversed_by_confirmation_id IS NULL
            ORDER BY c.confirmation_business_date, c.created_at, c.id
            """,
            (subscription_id,),
        ).fetchall()

    def _refresh_subscription(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> sqlite3.Row:
        confirmations = self._active_confirmation_rows(connection, str(row["id"]))
        confirmed = sum(int(item["confirmed_amount_minor"]) for item in confirmations)
        fees = sum(int(item["fee_minor"]) for item in confirmations)
        refunded = sum(int(item["refunded_amount_minor"]) for item in confirmations)
        cancelled = int(row["cancelled_amount_minor"])
        requested = int(row["requested_amount_minor"])
        consumed = confirmed + fees + refunded + cancelled
        if consumed > requested:
            self._rollback_and_raise(
                connection,
                LedgerError(
                    "SUBSCRIPTION_AMOUNT_EXCEEDED",
                    "确认金额、费用、退款和取消金额合计超过原申购金额。",
                    http_status=409,
                ),
            )
        pending = requested - consumed
        current = str(row["status"])
        if current in {"CANCELLED", "REJECTED"}:
            status = current
        elif pending == 0 and confirmed > 0:
            status = "CONFIRMED"
        elif confirmed > 0:
            status = "PARTIALLY_CONFIRMED"
        elif current == "PENDING_CONFIRMATION":
            status = "PENDING_CONFIRMATION"
        else:
            status = "SUBMITTED"
        connection.execute(
            """
            UPDATE external_subscriptions
            SET status=?, pending_amount_minor=?, confirmed_amount_minor=?,
                fee_minor=?, refunded_amount_minor=?, updated_at=?
            WHERE id=?
            """,
            (
                status,
                pending,
                confirmed,
                fees,
                refunded,
                _iso(self._now()),
                row["id"],
            ),
        )
        return self._subscription_row(connection, str(row["id"]))

    def _subscription_data(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> JsonDict:
        confirmations = self._active_confirmation_rows(connection, str(row["id"]))
        confirmation_items: list[JsonDict] = []
        confirmed_unbooked = 0
        booked_plan_amount = 0
        for item in confirmations:
            active_transaction = (
                item["transaction_id"] is not None
                and item["reversed_by_transaction_id"] is None
            )
            cash_use = int(item["confirmed_amount_minor"]) + int(item["fee_minor"])
            if active_transaction:
                booked_plan_amount += int(item["plan_linked_amount_minor"] or cash_use)
            else:
                confirmed_unbooked += cash_use
            confirmation_items.append(
                {
                    "id": str(item["id"]),
                    "confirmation_business_date": str(item["confirmation_business_date"]),
                    "confirmed_at": str(item["confirmed_at"]),
                    "nav_date": str(item["nav_date"]),
                    "nav": _decimal(int(item["nav_micros"]), NAV_SCALE, 6),
                    "confirmed_shares": _decimal(
                        int(item["confirmed_shares_micros"]), SHARE_SCALE, 6
                    ),
                    "confirmed_amount": _money(int(item["confirmed_amount_minor"])),
                    "fee": _money(int(item["fee_minor"])),
                    "refunded_amount": _money(int(item["refunded_amount_minor"])),
                    "external_reference": item["external_reference"],
                    "ledger_status": "BOOKED" if active_transaction else "AWAITING_USER_POSTING",
                    "transaction_id": item["transaction_id"],
                }
            )
        expected = row["expected_confirmation_date"]
        today = self._now().astimezone(ZoneInfo(self.settings.timezone)).date()
        overdue = (
            expected is not None
            and date.fromisoformat(str(expected)) < today
            and int(row["pending_amount_minor"]) > 0
        )
        pending_external = int(row["pending_amount_minor"])
        return {
            "id": str(row["id"]),
            "portfolio_id": str(row["portfolio_id"]),
            "account_id": str(row["account_id"]),
            "weekly_plan_id": str(row["weekly_plan_id"]),
            "plan_date": str(row["plan_date"]),
            "plan_status": str(row["plan_status"]),
            "instrument_id": str(row["instrument_id"]),
            "instrument_code": str(row["instrument_code"]),
            "instrument_name": str(row["instrument_name"]),
            "requested_amount": _money(int(row["requested_amount_minor"])),
            "currency": str(row["currency"]),
            "submitted_at": str(row["submitted_at"]),
            "submitted_business_date": str(row["submitted_business_date"]),
            "expected_confirmation_date": expected,
            "external_platform": str(row["external_platform"]),
            "external_reference": row["external_reference"],
            "status": str(row["status"]),
            "pending_external_amount": _money(pending_external),
            "confirmed_amount": _money(int(row["confirmed_amount_minor"])),
            "confirmed_unbooked_amount": _money(confirmed_unbooked),
            "booked_plan_amount": _money(booked_plan_amount),
            "in_flight_amount": _money(pending_external + confirmed_unbooked),
            "fee": _money(int(row["fee_minor"])),
            "refunded_amount": _money(int(row["refunded_amount_minor"])),
            "cancelled_amount": _money(int(row["cancelled_amount_minor"])),
            "confirmation_overdue": overdue,
            "source": str(row["source"]),
            "recorded_by": str(row["recorded_by"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "confirmations": confirmation_items,
            "holding_changed": False,
            "trade_executed": False,
        }

    def _draft_data(self, row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "action": str(row["action"]),
            "subscription_id": row["subscription_id"],
            "payload": json.loads(str(row["payload_json"])),
            "status": str(row["status"]),
            "expires_at": str(row["expires_at"]),
            "created_at": str(row["created_at"]),
            "committed_at": row["committed_at"],
            "committed_entity_id": row["committed_entity_id"],
        }

    def _create_draft(
        self,
        *,
        action: str,
        payload: JsonDict,
        idempotency_key: str,
        actor_ref: str,
        subscription_id: str | None = None,
    ) -> JsonDict:
        key = idempotency_key.strip()
        actor = actor_ref.strip()
        if not key or not actor:
            raise LedgerError("MISSING_REQUIRED_FIELD", "幂等键和记录人不能为空。")
        payload_hash = _hash(payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM external_subscription_drafts WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash or str(
                    existing["action"]
                ) != action:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "IDEMPOTENCY_CONFLICT",
                            "同一幂等键已经用于不同的申购草稿。",
                            http_status=409,
                        ),
                    )
                connection.commit()
                return {
                    "draft": self._draft_data(existing),
                    "confirmation_token": None,
                    "reused": True,
                    "warnings": ["已复用相同草稿; 请使用原确认信息。"],
                }
            if subscription_id is not None:
                self._subscription_row(connection, subscription_id)
            now = self._now()
            draft_id = str(uuid4())
            token = secrets.token_urlsafe(24)
            expires_at = now + timedelta(minutes=self.settings.confirmation_ttl_minutes)
            connection.execute(
                """
                INSERT INTO external_subscription_drafts (
                    id, action, subscription_id, payload_json, payload_hash, status,
                    idempotency_key, confirmation_digest, expires_at, created_at,
                    committed_at, committed_entity_id, actor_ref
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    draft_id,
                    action,
                    subscription_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    payload_hash,
                    key,
                    _token_digest(token),
                    _iso(expires_at),
                    _iso(now),
                    actor,
                ),
            )
            self._audit(
                connection,
                actor_type="AGENT",
                actor_ref=actor,
                action=f"EXTERNAL_SUBSCRIPTION_{action}_DRAFT_CREATED",
                entity_type="external_subscription_draft",
                entity_id=draft_id,
                details={"subscription_id": subscription_id, "expires_at": _iso(expires_at)},
            )
            row = connection.execute(
                "SELECT * FROM external_subscription_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            assert row is not None
            connection.commit()
            return {
                "draft": self._draft_data(row),
                "confirmation_token": token,
                "reused": False,
                "warnings": [],
            }
        finally:
            connection.close()

    def create_submission_draft(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        weekly_plan_id: str,
        instrument_code: str,
        requested_amount: str,
        submitted_at: str,
        submitted_business_date: str,
        external_platform: str,
        idempotency_key: str,
        external_reference: str | None = None,
        expected_confirmation_date: str | None = None,
        source: str = "USER_REPORTED",
        actor_ref: str = "hermes",
    ) -> JsonDict:
        amount_minor = _scaled(requested_amount, MONEY_SCALE, "requested_amount")
        if amount_minor <= 0:
            raise LedgerError("INVALID_AMOUNT", "申购金额必须大于零。")
        timestamp = _parse_iso(submitted_at)
        if timestamp.utcoffset() is None:
            raise LedgerError("TIMEZONE_REQUIRED", "申购提交时间必须包含时区。")
        business_date = date.fromisoformat(submitted_business_date)
        expected = (
            date.fromisoformat(expected_confirmation_date)
            if expected_confirmation_date
            else None
        )
        if expected is not None and expected < business_date:
            raise LedgerError(
                "INVALID_EXPECTED_CONFIRMATION_DATE", "预计确认日期不能早于申购日期。"
            )
        payload: JsonDict = {
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "weekly_plan_id": weekly_plan_id,
            "instrument_code": instrument_code.strip().upper(),
            "requested_amount_minor": amount_minor,
            "submitted_at": _iso(timestamp),
            "submitted_business_date": business_date.isoformat(),
            "expected_confirmation_date": expected.isoformat() if expected else None,
            "external_platform": external_platform.strip(),
            "external_reference": external_reference.strip() if external_reference else None,
            "source": source.strip().upper(),
        }
        if not payload["external_platform"]:
            raise LedgerError("PLATFORM_REQUIRED", "必须记录外部申购平台。")
        return self._create_draft(
            action="SUBMIT",
            payload=payload,
            idempotency_key=idempotency_key,
            actor_ref=actor_ref,
        )

    def create_status_draft(
        self,
        *,
        subscription_id: str,
        target_status: str,
        reason: str,
        idempotency_key: str,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        normalized = target_status.strip().upper()
        actions = {
            "PENDING_CONFIRMATION": "MARK_PENDING",
            "CANCELLED": "CANCEL",
            "REJECTED": "REJECT",
        }
        if normalized not in actions:
            raise LedgerError(
                "INVALID_SUBSCRIPTION_STATUS",
                "状态只能是待确认、已取消或已拒绝。",
            )
        if not reason.strip():
            raise LedgerError("REASON_REQUIRED", "必须记录状态变化原因。")
        return self._create_draft(
            action=actions[normalized],
            subscription_id=subscription_id,
            payload={"target_status": normalized, "reason": reason.strip()},
            idempotency_key=idempotency_key,
            actor_ref=actor_ref,
        )

    def create_confirmation_draft(
        self,
        *,
        subscription_id: str,
        confirmed_at: str,
        confirmation_business_date: str,
        nav_date: str,
        nav: str,
        confirmed_shares: str,
        confirmed_amount: str,
        fee: str,
        refunded_amount: str,
        idempotency_key: str,
        external_reference: str | None = None,
        reversal_of_confirmation_id: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        if reversal_of_confirmation_id:
            reversal_payload: JsonDict = {
                "reversal_of_confirmation_id": reversal_of_confirmation_id,
                "reason": external_reference or "用户确认冲销错误的份额确认事实",
            }
            return self._create_draft(
                action="REVERSE_CONFIRMATION",
                subscription_id=subscription_id,
                payload=reversal_payload,
                idempotency_key=idempotency_key,
                actor_ref=actor_ref,
            )
        timestamp = _parse_iso(confirmed_at)
        if timestamp.utcoffset() is None:
            raise LedgerError("TIMEZONE_REQUIRED", "确认时间必须包含时区。")
        confirmation_date = date.fromisoformat(confirmation_business_date)
        nav_day = date.fromisoformat(nav_date)
        amount_minor = _scaled(confirmed_amount, MONEY_SCALE, "confirmed_amount")
        nav_micros = _scaled(nav, NAV_SCALE, "nav")
        shares_micros = _scaled(confirmed_shares, SHARE_SCALE, "confirmed_shares")
        fee_minor = _scaled(fee, MONEY_SCALE, "fee")
        refunded_minor = _scaled(refunded_amount, MONEY_SCALE, "refunded_amount")
        if min(amount_minor, nav_micros, shares_micros) <= 0:
            raise LedgerError(
                "INVALID_CONFIRMATION_FACT", "确认金额、净值和份额必须大于零。"
            )
        expected_minor = (nav_micros * shares_micros + 5_000_000_000) // 10_000_000_000
        proportional = (
            amount_minor * self.settings.transaction_amount_tolerance_bps + 9_999
        ) // 10_000
        allowed = max(self.settings.transaction_amount_tolerance_minor, proportional)
        if abs(amount_minor - expected_minor) > allowed:
            raise LedgerError(
                "AMOUNT_SHARE_MISMATCH",
                "确认金额、净值和份额超出账本允许误差。",
                details={
                    "confirmed_amount": _money(amount_minor),
                    "expected_amount": _money(expected_minor),
                    "allowed_difference": _money(allowed),
                },
            )
        payload: JsonDict = {
            "confirmed_at": _iso(timestamp),
            "confirmation_business_date": confirmation_date.isoformat(),
            "nav_date": nav_day.isoformat(),
            "nav_micros": nav_micros,
            "confirmed_shares_micros": shares_micros,
            "confirmed_amount_minor": amount_minor,
            "fee_minor": fee_minor,
            "refunded_amount_minor": refunded_minor,
            "external_reference": external_reference.strip() if external_reference else None,
        }
        return self._create_draft(
            action="CONFIRM",
            subscription_id=subscription_id,
            payload=payload,
            idempotency_key=idempotency_key,
            actor_ref=actor_ref,
        )

    def create_confirmation_reversal_draft(
        self,
        *,
        subscription_id: str,
        confirmation_id: str,
        reason: str,
        idempotency_key: str,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        """Draft a correction to one unposted confirmation fact."""
        if not reason.strip():
            raise LedgerError("REASON_REQUIRED", "A reversal reason is required.")
        return self._create_draft(
            action="REVERSE_CONFIRMATION",
            subscription_id=subscription_id,
            payload={
                "reversal_of_confirmation_id": confirmation_id,
                "reason": reason.strip(),
            },
            idempotency_key=idempotency_key,
            actor_ref=actor_ref,
        )

    def get_draft(self, *, draft_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM external_subscription_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "EXTERNAL_SUBSCRIPTION_DRAFT_NOT_FOUND",
                    "没有找到该申购草稿。",
                    http_status=404,
                )
            return self._draft_data(row)

    def _plan_capacity(
        self,
        connection: sqlite3.Connection,
        *,
        plan_id: str,
        instrument_id: str,
    ) -> tuple[int, int, int]:
        planned = connection.execute(
            """
            SELECT pi.candidate_amount_minor, p.status AS plan_status
            FROM investment_plans p
            JOIN plan_revisions pr ON pr.plan_id=p.id AND pr.revision=p.current_revision
            JOIN plan_items pi ON pi.plan_revision_id=pr.id
            WHERE p.id=? AND pi.instrument_id=? AND pi.action='CONTRIBUTE'
              AND pi.candidate_amount_minor > 0
            """,
            (plan_id, instrument_id),
        ).fetchone()
        if planned is None:
            raise LedgerError(
                "PLAN_INSTRUMENT_MISMATCH", "该基金不在当前冻结周计划中。", http_status=409
            )
        if str(planned["plan_status"]) not in {"FROZEN", "PARTIALLY_EXECUTED"}:
            raise LedgerError(
                "PLAN_NOT_OPEN_FOR_SUBSCRIPTION_POSTING",
                "The weekly plan is no longer open for subscription posting.",
                http_status=409,
            )
        executed = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(l.linked_amount_minor), 0)
                FROM plan_execution_links l
                JOIN transactions t ON t.id=l.transaction_id
                WHERE l.plan_id=? AND t.instrument_id=?
                  AND t.reversed_by_transaction_id IS NULL
                """,
                (plan_id, instrument_id),
            ).fetchone()[0]
        )
        subscriptions = connection.execute(
            """
            SELECT s.id, s.requested_amount_minor, s.cancelled_amount_minor,
                   s.refunded_amount_minor,
                   COALESCE(SUM(
                       CASE WHEN t.id IS NOT NULL AND t.reversed_by_transaction_id IS NULL
                            THEN l.plan_linked_amount_minor ELSE 0 END
                   ), 0) AS booked_minor
            FROM external_subscriptions s
            LEFT JOIN external_subscription_confirmations c
              ON c.subscription_id=s.id AND c.kind='CONFIRMATION'
             AND c.reversed_by_confirmation_id IS NULL
            LEFT JOIN subscription_confirmation_transaction_links l
              ON l.confirmation_id=c.id
            LEFT JOIN transactions t ON t.id=l.transaction_id
            WHERE s.weekly_plan_id=? AND s.instrument_id=?
            GROUP BY s.id
            """,
            (plan_id, instrument_id),
        ).fetchall()
        reserved = sum(
            max(
                int(item["requested_amount_minor"])
                - int(item["cancelled_amount_minor"])
                - int(item["refunded_amount_minor"])
                - int(item["booked_minor"]),
                0,
            )
            for item in subscriptions
        )
        capacity = max(int(planned["candidate_amount_minor"]) - executed - reserved, 0)
        return int(planned["candidate_amount_minor"]), executed, capacity

    def commit_draft(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
        allowed_actions: set[str] | None = None,
    ) -> JsonDict:
        actor = confirmed_by.strip()
        if not actor or not confirmation_token:
            raise LedgerError("CONFIRMATION_REQUIRED", "必须明确确认并记录确认人。")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            draft = connection.execute(
                "SELECT * FROM external_subscription_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if draft is None:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "EXTERNAL_SUBSCRIPTION_DRAFT_NOT_FOUND",
                        "没有找到该申购草稿。",
                        http_status=404,
                    ),
                )
            action = str(draft["action"])
            if allowed_actions is not None and action not in allowed_actions:
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "DRAFT_TYPE_MISMATCH",
                        "该草稿必须使用对应的精确提交操作。",
                        http_status=409,
                    ),
                )
            if not hmac.compare_digest(
                str(draft["confirmation_digest"]), _token_digest(confirmation_token)
            ):
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "INVALID_CONFIRMATION_TOKEN", "确认信息不匹配。", http_status=403
                    ),
                )
            if str(draft["status"]) == "COMMITTED":
                entity_id = str(draft["committed_entity_id"])
                if action == "CONFIRM" or action == "REVERSE_CONFIRMATION":
                    subscription_id = str(draft["subscription_id"])
                else:
                    subscription_id = entity_id
                result = self._subscription_data(
                    connection, self._subscription_row(connection, subscription_id)
                )
                connection.commit()
                return {"subscription": result, "idempotent_replay": True}
            if str(draft["status"]) != "PENDING":
                self._rollback_and_raise(
                    connection,
                    LedgerError(
                        "DRAFT_NOT_PENDING",
                        f"草稿当前状态为 {draft['status']}; 不能提交。",
                        http_status=409,
                    ),
                )
            if self._now() > _parse_iso(str(draft["expires_at"])):
                connection.execute(
                    "UPDATE external_subscription_drafts SET status='EXPIRED' WHERE id=?",
                    (draft_id,),
                )
                connection.commit()
                raise LedgerError(
                    "CONFIRMATION_TOKEN_EXPIRED",
                    "草稿已经过期; 请重新创建相同草稿后再次确认。",
                    http_status=410,
                )
            payload = json.loads(str(draft["payload_json"]))
            now = _iso(self._now())
            committed_entity_id: str
            if action == "SUBMIT":
                plan = connection.execute(
                    "SELECT * FROM investment_plans WHERE id=?",
                    (payload["weekly_plan_id"],),
                ).fetchone()
                if plan is None or str(plan["status"]) not in {
                    "FROZEN",
                    "PARTIALLY_EXECUTED",
                }:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "PLAN_NOT_OPEN_FOR_SUBMISSION",
                            "只有已冻结或部分执行的周计划可以记录外部申购。",
                            http_status=409,
                        ),
                    )
                if str(plan["portfolio_id"]) != payload["portfolio_id"] or str(
                    plan["account_id"]
                ) != payload["account_id"]:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "SUBSCRIPTION_CONTEXT_MISMATCH",
                            "申购与周计划的组合或账户不一致。",
                            http_status=409,
                        ),
                    )
                instrument = connection.execute(
                    "SELECT * FROM instruments WHERE code=? AND status='ACTIVE'",
                    (payload["instrument_code"],),
                ).fetchone()
                if instrument is None or str(instrument["asset_type"]) != "FUND":
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "SUBSCRIPTION_INSTRUMENT_INVALID",
                            "外部申购只能关联已登记的可交易基金。",
                            http_status=409,
                        ),
                    )
                account = connection.execute(
                    "SELECT 1 FROM accounts WHERE id=? AND portfolio_id=? AND status='ACTIVE'",
                    (payload["account_id"], payload["portfolio_id"]),
                ).fetchone()
                if account is None:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "ACCOUNT_NOT_FOUND", "没有找到活动账户。", http_status=409
                        ),
                    )
                _, _, capacity = self._plan_capacity(
                    connection,
                    plan_id=payload["weekly_plan_id"],
                    instrument_id=str(instrument["id"]),
                )
                requested = int(payload["requested_amount_minor"])
                if requested > capacity:
                    self._rollback_and_raise(
                        connection,
                        LedgerError(
                            "SUBSCRIPTION_EXCEEDS_PLAN_REMAINING",
                            "申购金额超过该基金尚未安排的计划金额。",
                            http_status=409,
                            details={
                                "requested_amount": _money(requested),
                                "available_plan_amount": _money(capacity),
                            },
                        ),
                    )
                subscription_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO external_subscriptions (
                        id, portfolio_id, account_id, weekly_plan_id, instrument_id,
                        requested_amount_minor, currency, submitted_at,
                        submitted_business_date, expected_confirmation_date,
                        external_platform, external_reference, status,
                        pending_amount_minor, confirmed_amount_minor, fee_minor,
                        refunded_amount_minor, cancelled_amount_minor, source,
                        recorded_by, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'CNY', ?, ?, ?, ?, ?, 'SUBMITTED',
                              ?, 0, 0, 0, 0, ?, ?, ?, ?, ?)
                    """,
                    (
                        subscription_id,
                        payload["portfolio_id"],
                        payload["account_id"],
                        payload["weekly_plan_id"],
                        instrument["id"],
                        requested,
                        payload["submitted_at"],
                        payload["submitted_business_date"],
                        payload["expected_confirmation_date"],
                        payload["external_platform"],
                        payload["external_reference"],
                        requested,
                        payload["source"],
                        actor,
                        str(draft["idempotency_key"]),
                        now,
                        now,
                    ),
                )
                committed_entity_id = subscription_id
            else:
                subscription_id = str(draft["subscription_id"])
                row = self._subscription_row(connection, subscription_id)
                current = str(row["status"])
                if action == "MARK_PENDING":
                    if current != "SUBMITTED":
                        self._rollback_and_raise(
                            connection,
                            LedgerError(
                                "INVALID_SUBSCRIPTION_TRANSITION",
                                "只有已提交申购可以标记为等待平台确认。",
                                http_status=409,
                            ),
                        )
                    connection.execute(
                        "UPDATE external_subscriptions SET status='PENDING_CONFIRMATION', "
                        "updated_at=? WHERE id=?",
                        (now, subscription_id),
                    )
                    committed_entity_id = subscription_id
                elif action in {"CANCEL", "REJECT"}:
                    if current not in {
                        "SUBMITTED",
                        "PENDING_CONFIRMATION",
                        "PARTIALLY_CONFIRMED",
                    } or int(row["pending_amount_minor"]) <= 0:
                        self._rollback_and_raise(
                            connection,
                            LedgerError(
                                "INVALID_SUBSCRIPTION_TRANSITION",
                                "只有仍有在途余额的申购可以取消或拒绝。",
                                http_status=409,
                            ),
                        )
                    cancelled = int(row["cancelled_amount_minor"]) + int(
                        row["pending_amount_minor"]
                    )
                    terminal = "CANCELLED" if action == "CANCEL" else "REJECTED"
                    connection.execute(
                        """
                        UPDATE external_subscriptions
                        SET status=?, pending_amount_minor=0,
                            cancelled_amount_minor=?, updated_at=? WHERE id=?
                        """,
                        (terminal, cancelled, now, subscription_id),
                    )
                    committed_entity_id = subscription_id
                elif action == "CONFIRM":
                    if current not in {
                        "SUBMITTED",
                        "PENDING_CONFIRMATION",
                        "PARTIALLY_CONFIRMED",
                    }:
                        self._rollback_and_raise(
                            connection,
                            LedgerError(
                                "INVALID_SUBSCRIPTION_TRANSITION",
                                "当前申购状态不能新增份额确认。",
                                http_status=409,
                            ),
                        )
                    submitted_day = date.fromisoformat(str(row["submitted_business_date"]))
                    confirmation_day = date.fromisoformat(
                        payload["confirmation_business_date"]
                    )
                    nav_day = date.fromisoformat(payload["nav_date"])
                    if confirmation_day < submitted_day or not (
                        submitted_day <= nav_day <= confirmation_day
                    ):
                        self._rollback_and_raise(
                            connection,
                            LedgerError(
                                "INVALID_CONFIRMATION_DATES",
                                "净值日期和确认日期必须晚于或等于申购日期; "
                                "且净值日期不能晚于确认日期。",
                            ),
                        )
                    consumed = (
                        int(payload["confirmed_amount_minor"])
                        + int(payload["fee_minor"])
                        + int(payload["refunded_amount_minor"])
                    )
                    if consumed > int(row["pending_amount_minor"]):
                        self._rollback_and_raise(
                            connection,
                            LedgerError(
                                "SUBSCRIPTION_CONFIRMATION_EXCEEDS_PENDING",
                                "本次确认、费用和退款超过剩余在途金额。",
                                http_status=409,
                            ),
                        )
                    confirmation_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO external_subscription_confirmations (
                            id, subscription_id, kind, confirmed_at,
                            confirmation_business_date, nav_date, nav_micros,
                            confirmed_shares_micros, confirmed_amount_minor,
                            fee_minor, refunded_amount_minor, external_reference,
                            reversal_of_confirmation_id, reversed_by_confirmation_id,
                            recorded_by, idempotency_key, created_at
                        ) VALUES (?, ?, 'CONFIRMATION', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  NULL, NULL, ?, ?, ?)
                        """,
                        (
                            confirmation_id,
                            subscription_id,
                            payload["confirmed_at"],
                            payload["confirmation_business_date"],
                            payload["nav_date"],
                            payload["nav_micros"],
                            payload["confirmed_shares_micros"],
                            payload["confirmed_amount_minor"],
                            payload["fee_minor"],
                            payload["refunded_amount_minor"],
                            payload["external_reference"],
                            actor,
                            str(draft["idempotency_key"]),
                            now,
                        ),
                    )
                    committed_entity_id = confirmation_id
                    self._refresh_subscription(connection, row)
                elif action == "REVERSE_CONFIRMATION":
                    original_id = str(payload["reversal_of_confirmation_id"])
                    original = connection.execute(
                        """
                        SELECT c.*, l.transaction_id, t.reversed_by_transaction_id
                        FROM external_subscription_confirmations c
                        LEFT JOIN subscription_confirmation_transaction_links l
                          ON l.confirmation_id=c.id
                        LEFT JOIN transactions t ON t.id=l.transaction_id
                        WHERE c.id=? AND c.subscription_id=?
                        """,
                        (original_id, subscription_id),
                    ).fetchone()
                    if (
                        original is None
                        or str(original["kind"]) != "CONFIRMATION"
                        or original["reversed_by_confirmation_id"] is not None
                    ):
                        self._rollback_and_raise(
                            connection,
                            LedgerError(
                                "CONFIRMATION_NOT_REVERSIBLE",
                                "原确认事实不存在或已经冲销。",
                                http_status=409,
                            ),
                        )
                    if (
                        original["transaction_id"] is not None
                        and original["reversed_by_transaction_id"] is None
                    ):
                        self._rollback_and_raise(
                            connection,
                            LedgerError(
                                "CONFIRMATION_TRANSACTION_ACTIVE",
                                "该确认已经生成正式交易; 必须先冲销交易事实。",
                                http_status=409,
                            ),
                        )
                    reversal_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO external_subscription_confirmations (
                            id, subscription_id, kind, confirmed_at,
                            confirmation_business_date, nav_date, nav_micros,
                            confirmed_shares_micros, confirmed_amount_minor,
                            fee_minor, refunded_amount_minor, external_reference,
                            reversal_of_confirmation_id, reversed_by_confirmation_id,
                            recorded_by, idempotency_key, created_at
                        ) VALUES (?, ?, 'REVERSAL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                        """,
                        (
                            reversal_id,
                            subscription_id,
                            now,
                            original["confirmation_business_date"],
                            original["nav_date"],
                            original["nav_micros"],
                            original["confirmed_shares_micros"],
                            original["confirmed_amount_minor"],
                            original["fee_minor"],
                            original["refunded_amount_minor"],
                            payload["reason"],
                            original_id,
                            actor,
                            str(draft["idempotency_key"]),
                            now,
                        ),
                    )
                    connection.execute(
                        "UPDATE external_subscription_confirmations "
                        "SET reversed_by_confirmation_id=? WHERE id=?",
                        (reversal_id, original_id),
                    )
                    committed_entity_id = reversal_id
                    self._refresh_subscription(connection, row)
                else:
                    raise AssertionError(f"unsupported subscription draft action: {action}")
            connection.execute(
                """
                UPDATE external_subscription_drafts
                SET status='COMMITTED', committed_at=?, committed_entity_id=? WHERE id=?
                """,
                (now, committed_entity_id, draft_id),
            )
            self._audit(
                connection,
                actor_type="USER",
                actor_ref=actor,
                action=f"EXTERNAL_SUBSCRIPTION_{action}_COMMITTED",
                entity_type=(
                    "external_subscription_confirmation"
                    if action in {"CONFIRM", "REVERSE_CONFIRMATION"}
                    else "external_subscription"
                ),
                entity_id=committed_entity_id,
                details={"draft_id": draft_id, "subscription_id": subscription_id},
            )
            refreshed = self._subscription_row(connection, subscription_id)
            result = self._subscription_data(connection, refreshed)
            connection.commit()
            return {"subscription": result, "idempotent_replay": False}
        finally:
            connection.close()

    def get(self, *, subscription_id: str) -> JsonDict:
        with self._connect() as connection:
            row = self._subscription_row(connection, subscription_id)
            return self._subscription_data(connection, row)

    def list(
        self,
        *,
        portfolio_id: str | None = None,
        account_id: str | None = None,
        weekly_plan_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = self._subscription_query() + " WHERE 1=1"
        parameters: list[Any] = []
        if portfolio_id:
            query += " AND s.portfolio_id=?"
            parameters.append(portfolio_id)
        if account_id:
            query += " AND s.account_id=?"
            parameters.append(account_id)
        if weekly_plan_id:
            query += " AND s.weekly_plan_id=?"
            parameters.append(weekly_plan_id)
        if status:
            query += " AND s.status=?"
            parameters.append(status.strip().upper())
        query += " ORDER BY s.submitted_business_date DESC, s.created_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            return [
                self._subscription_data(connection, row)
                for row in connection.execute(query, parameters).fetchall()
            ]

    def summary(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        as_of_date: date,
    ) -> JsonDict:
        items = self.list(
            portfolio_id=portfolio_id,
            account_id=account_id,
            limit=500,
        )
        active = [
            item
            for item in items
            if Decimal(str(item["in_flight_amount"])) > 0
            or Decimal(str(item["confirmed_unbooked_amount"])) > 0
        ]
        counts: dict[str, int] = {}
        for item in items:
            counts[str(item["status"])] = counts.get(str(item["status"]), 0) + 1
        in_flight = sum(Decimal(str(item["in_flight_amount"])) for item in active)
        confirmed_unbooked = sum(
            Decimal(str(item["confirmed_unbooked_amount"])) for item in active
        )
        cross_week = sum(
            1
            for item in active
            if date.fromisoformat(str(item["submitted_business_date"]))
            < as_of_date - timedelta(days=6)
        )
        overdue = sum(1 for item in active if item["confirmation_overdue"])
        return {
            "counts": counts,
            "active_count": len(active),
            "in_flight_amount": f"{in_flight:.2f}",
            "confirmed_unbooked_amount": f"{confirmed_unbooked:.2f}",
            "cross_week_count": cross_week,
            "overdue_review_count": overdue,
            "items": active,
            "automatic_failure_inference": False,
        }

    def create_transaction_draft(
        self,
        *,
        confirmation_id: str,
        idempotency_key: str,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        with self._connect() as connection:
            confirmation = connection.execute(
                """
                SELECT c.*, s.portfolio_id, s.account_id, s.weekly_plan_id,
                       s.instrument_id, s.external_platform, i.code AS instrument_code,
                       l.transaction_id, t.reversed_by_transaction_id
                FROM external_subscription_confirmations c
                JOIN external_subscriptions s ON s.id=c.subscription_id
                JOIN instruments i ON i.id=s.instrument_id
                LEFT JOIN subscription_confirmation_transaction_links l
                  ON l.confirmation_id=c.id
                LEFT JOIN transactions t ON t.id=l.transaction_id
                WHERE c.id=?
                """,
                (confirmation_id,),
            ).fetchone()
            if (
                confirmation is None
                or str(confirmation["kind"]) != "CONFIRMATION"
                or confirmation["reversed_by_confirmation_id"] is not None
            ):
                raise LedgerError(
                    "CONFIRMATION_NOT_POSTABLE",
                    "只有有效且未冲销的份额确认可以生成交易草稿。",
                    http_status=409,
                )
            if (
                confirmation["transaction_id"] is not None
                and confirmation["reversed_by_transaction_id"] is None
            ):
                raise LedgerError(
                    "CONFIRMATION_ALREADY_POSTED",
                    "该份额确认已经生成正式交易; 不能重复记账。",
                    http_status=409,
                )
            plan_linked_minor = int(confirmation["confirmed_amount_minor"]) + int(
                confirmation["fee_minor"]
            )
            planned, executed, _ = self._plan_capacity(
                connection,
                plan_id=str(confirmation["weekly_plan_id"]),
                instrument_id=str(confirmation["instrument_id"]),
            )
            if plan_linked_minor > planned - executed:
                raise LedgerError(
                    "SUBSCRIPTION_CONFIRMATION_EXCEEDS_PLAN_REMAINING",
                    "The confirmed cash amount exceeds the remaining frozen plan amount.",
                    http_status=409,
                    details={
                        "confirmed_cash_amount": _money(plan_linked_minor),
                        "remaining_plan_amount": _money(max(planned - executed, 0)),
                    },
                )
            draft_result = self._ledger.create_transaction_draft(
                portfolio_id=str(confirmation["portfolio_id"]),
                account_id=str(confirmation["account_id"]),
                instrument_code=str(confirmation["instrument_code"]),
                side="BUY",
                trade_date_value=str(confirmation["nav_date"]),
                amount=_money(int(confirmation["confirmed_amount_minor"])),
                nav=_decimal(int(confirmation["nav_micros"]), NAV_SCALE, 6),
                shares=_decimal(
                    int(confirmation["confirmed_shares_micros"]), SHARE_SCALE, 6
                ),
                platform=str(confirmation["external_platform"]),
                idempotency_key=idempotency_key,
                note=f"External subscription confirmation {confirmation_id}",
                actor_ref=actor_ref,
            )
        draft_id = str(draft_result["draft"]["id"])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO subscription_confirmation_transaction_links (
                    confirmation_id, transaction_draft_id, transaction_id,
                    plan_linked_amount_minor, created_at, committed_at
                ) VALUES (?, ?, NULL, ?, ?, NULL)
                ON CONFLICT(confirmation_id) DO UPDATE SET
                    transaction_draft_id=excluded.transaction_draft_id,
                    plan_linked_amount_minor=excluded.plan_linked_amount_minor
                """,
                (
                    confirmation_id,
                    draft_id,
                    plan_linked_minor,
                    _iso(self._now()),
                ),
            )
            connection.commit()
        return {
            **draft_result,
            "confirmation_id": confirmation_id,
            "plan_linked_amount": _money(plan_linked_minor),
            "business_effect": "DRAFT_ONLY_NO_HOLDING_CHANGE",
        }

    def commit_transaction_draft(
        self,
        *,
        confirmation_id: str,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        with self._connect() as connection:
            link = connection.execute(
                """
                SELECT l.*, c.subscription_id, s.weekly_plan_id
                FROM subscription_confirmation_transaction_links l
                JOIN external_subscription_confirmations c ON c.id=l.confirmation_id
                JOIN external_subscriptions s ON s.id=c.subscription_id
                WHERE l.confirmation_id=?
                """,
                (confirmation_id,),
            ).fetchone()
            if link is None or str(link["transaction_draft_id"]) != draft_id:
                raise LedgerError(
                    "SUBSCRIPTION_TRANSACTION_DRAFT_MISMATCH",
                    "交易草稿与该份额确认不匹配。",
                    http_status=409,
                )
            plan_id = str(link["weekly_plan_id"])
            linked_amount = _money(int(link["plan_linked_amount_minor"]))
            confirmation = connection.execute(
                """
                SELECT c.reversed_by_confirmation_id, s.instrument_id
                FROM external_subscription_confirmations c
                JOIN external_subscriptions s ON s.id=c.subscription_id
                WHERE c.id=? AND c.kind='CONFIRMATION'
                """,
                (confirmation_id,),
            ).fetchone()
            if confirmation is None or confirmation["reversed_by_confirmation_id"] is not None:
                raise LedgerError(
                    "CONFIRMATION_NOT_POSTABLE",
                    "The share confirmation is missing or has been reversed.",
                    http_status=409,
                )
            planned, executed, _ = self._plan_capacity(
                connection,
                plan_id=plan_id,
                instrument_id=str(confirmation["instrument_id"]),
            )
            if int(link["plan_linked_amount_minor"]) > planned - executed:
                raise LedgerError(
                    "SUBSCRIPTION_CONFIRMATION_EXCEEDS_PLAN_REMAINING",
                    "The frozen plan no longer has enough remaining cash capacity.",
                    http_status=409,
                )
        transaction_result = self._ledger.commit_transaction_draft(
            draft_id=draft_id,
            confirmation_token=confirmation_token,
            confirmed_by=confirmed_by,
        )
        transaction_id = str(transaction_result["transaction"]["id"])
        try:
            plan = self._planning.link_transaction(
                plan_id=plan_id,
                transaction_id=transaction_id,
                confirmed_by=confirmed_by,
                linked_amount=linked_amount,
            )
        except LedgerError as exc:
            if exc.code != "PLAN_TRANSACTION_ALREADY_LINKED":
                raise
            plan = self._planning.get(plan_id=plan_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE subscription_confirmation_transaction_links
                SET transaction_id=?, committed_at=? WHERE confirmation_id=?
                """,
                (transaction_id, _iso(self._now()), confirmation_id),
            )
            row = self._subscription_row(connection, str(link["subscription_id"]))
            subscription = self._subscription_data(connection, row)
            self._audit(
                connection,
                actor_type="USER",
                actor_ref=confirmed_by.strip(),
                action="EXTERNAL_SUBSCRIPTION_CONFIRMATION_POSTED",
                entity_type="external_subscription_confirmation",
                entity_id=confirmation_id,
                details={"transaction_id": transaction_id, "weekly_plan_id": plan_id},
            )
            connection.commit()
        return {
            "subscription": subscription,
            "transaction": transaction_result["transaction"],
            "holding": transaction_result["holding"],
            "weekly_plan": plan,
            "trade_executed_by_system": False,
        }
