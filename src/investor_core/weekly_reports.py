"""Governed weekly reports bound to immutable weekly-plan facts."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError, utc_now
from investor_core.performance import PerformanceService

FINAL_PLAN_STATUSES = {"EXECUTED", "SKIPPED", "PARTIALLY_EXECUTED_CLOSED"}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _money(value: int) -> str:
    return f"{Decimal(value) / 100:.2f}"


class WeeklyReportService:
    """Build and persist one versioned formal report per weekly plan."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self._now = now
        self._performance = PerformanceService(settings, now=now)

    def _connect(self) -> sqlite3.Connection:
        path = (
            ":memory:"
            if str(self.settings.db_path) == ":memory:"
            else str(Path(self.settings.db_path).resolve())
        )
        connection = sqlite3.connect(path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _rollback(connection: sqlite3.Connection, error: LedgerError) -> NoReturn:
        connection.rollback()
        raise error

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        entity_id: str,
        actor_ref: str,
        details: JsonDict,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                id, occurred_at, actor_type, actor_ref, action, entity_type,
                entity_id, before_hash, after_hash, details_json, trace_id
            ) VALUES (?, ?, ?, ?, ?, 'weekly_report', ?, NULL, ?, ?, ?)
            """,
            (
                str(uuid4()),
                _iso(self._now()),
                "CRON" if actor_ref == "cron" else "USER",
                actor_ref,
                action,
                entity_id,
                _hash(details),
                _json(details),
                str(uuid4()),
            ),
        )

    @staticmethod
    def _draft_data(row: sqlite3.Row, *, now: datetime) -> JsonDict:
        status = str(row["status"])
        if status == "PENDING" and _parse_iso(str(row["expires_at"])) <= now:
            status = "EXPIRED"
        return {
            "id": str(row["id"]),
            "weekly_plan_id": str(row["plan_id"]),
            "report_version": int(row["report_version"]),
            "facts_hash": str(row["facts_hash"]),
            "content": json.loads(str(row["content_json"])),
            "data_quality": str(row["data_quality"]),
            "valuation_status": str(row["valuation_status"]),
            "regeneration_reason": row["regeneration_reason"],
            "status": status,
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "expires_at": str(row["expires_at"]),
            "renewed_at": row["renewed_at"],
            "renewal_count": int(row["renewal_count"]),
            "committed_at": row["committed_at"],
            "committed_report_id": row["committed_report_id"],
        }

    @staticmethod
    def _report_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "weekly_plan_id": str(row["plan_id"]),
            "report_bundle_id": str(row["report_bundle_id"]),
            "report_version": int(row["report_version"]),
            "facts_hash": str(row["facts_hash"]),
            "content": json.loads(str(row["content_json"])),
            "data_quality": str(row["data_quality"]),
            "valuation_status": str(row["valuation_status"]),
            "regeneration_reason": row["regeneration_reason"],
            "supersedes_report_id": row["supersedes_report_id"],
            "is_current": bool(row["is_current"]),
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _plan_row(connection: sqlite3.Connection, plan_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM investment_plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise LedgerError("INVESTMENT_PLAN_NOT_FOUND", "没有找到该周计划。", http_status=404)
        assert isinstance(row, sqlite3.Row)
        return row

    def _valuation(
        self,
        connection: sqlite3.Connection,
        *,
        plan: sqlite3.Row,
    ) -> JsonDict:
        end = str(plan["period_end"])
        holdings = connection.execute(
            """
            WITH ranked AS (
                SELECT h.*, i.code AS instrument_code,
                       ROW_NUMBER() OVER (
                           PARTITION BY h.instrument_id
                           ORDER BY h.as_of DESC, h.created_at DESC, h.id DESC
                       ) AS position_rank
                FROM holding_snapshots h
                JOIN instruments i ON i.id=h.instrument_id
                WHERE h.portfolio_id=? AND h.account_id=? AND h.as_of<=?
            )
            SELECT * FROM ranked
            WHERE position_rank=1 AND total_shares_micros>0
            ORDER BY instrument_code
            """,
            (plan["portfolio_id"], plan["account_id"], end),
        ).fetchall()
        missing: list[JsonDict] = []
        limited: list[JsonDict] = []
        snapshots: list[JsonDict] = []
        end_market_minor = 0
        for item in holdings:
            rows = connection.execute(
                """
                SELECT * FROM market_nav_snapshots
                WHERE instrument_id=? AND nav_date=?
                ORDER BY observed_at DESC, rowid DESC
                """,
                (item["instrument_id"], end),
            ).fetchall()
            if not rows:
                missing.append(
                    {
                        "instrument_code": str(item["instrument_code"]),
                        "required_nav_date": end,
                        "source": None,
                        "verification_status": "MISSING",
                    }
                )
                continue
            values = {int(row["nav_micros"]) for row in rows}
            selected = rows[0]
            quality = (
                "PASS"
                if len(values) == 1
                and str(selected["verification_status"]) == "VERIFIED"
                and str(selected["source_type"]) in {"OFFICIAL", "PLATFORM"}
                else "WARNING"
            )
            fact = {
                "instrument_code": str(item["instrument_code"]),
                "nav_date": end,
                "nav": f"{Decimal(int(selected['nav_micros'])) / 1_000_000:.6f}",
                "source": str(selected["source_name"]),
                "source_type": str(selected["source_type"]),
                "verification_status": str(selected["verification_status"]),
                "data_quality": quality,
            }
            snapshots.append(fact)
            if quality != "PASS":
                limited.append(fact)
            else:
                end_market_minor += (
                    int(item["total_shares_micros"])
                    * int(selected["nav_micros"])
                    * 100
                    + 1_000_000 * 1_000_000 // 2
                ) // (1_000_000 * 1_000_000)
        if missing or limited:
            return {
                "status": "LIMITED",
                "business_state": "交易事实完整、估值部分不可用",
                "end_market_value": None,
                "period_return": None,
                "missing": missing,
                "limited": limited,
                "snapshots": snapshots,
                "substitution_used": False,
            }
        performance = self._performance.calculate(
            portfolio_id=str(plan["portfolio_id"]),
            period_start=datetime.fromisoformat(str(plan["period_start"])).date(),
            period_end=datetime.fromisoformat(end).date(),
            period_type="CUSTOM",
            persist=False,
        )
        if (
            str(performance["data_quality"]) == "SOURCE_ERROR"
            or performance["modified_dietz_bps"] is None
        ):
            return {
                "status": "LIMITED",
                "business_state": "交易事实完整、估值部分不可用",
                "end_market_value": _money(end_market_minor),
                "period_return": None,
                "missing": [],
                "limited": [
                    {
                        "reason_code": performance["reason_code"],
                        "warnings": performance["warnings"],
                    }
                ],
                "snapshots": snapshots,
                "substitution_used": False,
                "data_quality": str(performance["data_quality"]),
            }
        return {
            "status": "AVAILABLE",
            "business_state": "交易事实与期末估值均可用",
            "end_market_value": _money(end_market_minor),
            "period_return": {
                "modified_dietz_bps": performance["modified_dietz_bps"],
                "xirr_bps": performance["xirr_bps"],
                "twr_bps": performance["twr_bps"],
            },
            "missing": [],
            "limited": [],
            "snapshots": snapshots,
            "substitution_used": False,
            "data_quality": str(performance["data_quality"]),
            "warnings": performance["warnings"],
        }

    def _facts(self, connection: sqlite3.Connection, plan_id: str) -> JsonDict:
        plan = self._plan_row(connection, plan_id)
        item_rows = list(
            connection.execute(
                """
                SELECT pi.*, i.code AS instrument_code, i.name AS instrument_name
                FROM plan_revisions pr
                JOIN plan_items pi ON pi.plan_revision_id=pr.id
                JOIN instruments i ON i.id=pi.instrument_id
                WHERE pr.plan_id=? AND pr.revision=?
                  AND pi.action='CONTRIBUTE' AND pi.candidate_amount_minor>0
                ORDER BY i.code
                """,
                (plan_id, plan["current_revision"]),
            ).fetchall()
        )
        items: list[JsonDict] = []
        executed_total = 0
        for item in item_rows:
            links = connection.execute(
                """
                SELECT l.linked_amount_minor, t.id AS transaction_id, t.trade_date,
                       t.amount_minor, t.nav_micros, t.shares_micros,
                       t.reversed_by_transaction_id
                FROM plan_execution_links l
                JOIN transactions t ON t.id=l.transaction_id
                WHERE l.plan_id=? AND t.instrument_id=?
                ORDER BY t.trade_date, t.id
                """,
                (plan_id, item["instrument_id"]),
            ).fetchall()
            valid_links = [row for row in links if row["reversed_by_transaction_id"] is None]
            executed = sum(int(row["linked_amount_minor"]) for row in valid_links)
            executed_total += executed
            subscriptions = connection.execute(
                """
                SELECT s.id, s.submitted_business_date, s.requested_amount_minor,
                       s.status, s.external_platform,
                       c.id AS confirmation_id, c.confirmation_business_date,
                       c.confirmed_at, c.confirmed_at_precision, c.nav_date,
                       c.nav_micros, c.confirmed_shares_micros,
                       c.confirmed_amount_minor, c.fee_minor,
                       l.transaction_id AS posted_transaction_id
                FROM external_subscriptions s
                LEFT JOIN external_subscription_confirmations c
                  ON c.subscription_id=s.id AND c.kind='CONFIRMATION'
                 AND c.reversed_by_confirmation_id IS NULL
                LEFT JOIN subscription_confirmation_transaction_links l
                  ON l.confirmation_id=c.id
                WHERE s.weekly_plan_id=? AND s.instrument_id=?
                ORDER BY s.submitted_business_date, s.id, c.created_at
                """,
                (plan_id, item["instrument_id"]),
            ).fetchall()
            subscription_facts = []
            for row in subscriptions:
                precision = row["confirmed_at_precision"]
                subscription_facts.append(
                    {
                        "subscription_id": str(row["id"]),
                        "order_date": str(row["submitted_business_date"]),
                        "gross_amount": _money(int(row["requested_amount_minor"])),
                        "subscription_status": str(row["status"]),
                        "confirmation_id": row["confirmation_id"],
                        "confirmation_date": row["confirmation_business_date"],
                        "confirmed_at_precision": precision,
                        "confirmed_at": (
                            str(row["confirmed_at"])
                            if precision == "EXACT" and row["confirmed_at"] is not None
                            else None
                        ),
                        "nav_date": row["nav_date"],
                        "confirmed_amount": (
                            _money(int(row["confirmed_amount_minor"]))
                            if row["confirmed_amount_minor"] is not None
                            else None
                        ),
                        "fee": (
                            _money(int(row["fee_minor"])) if row["fee_minor"] is not None else None
                        ),
                        "shares": (
                            f"{Decimal(int(row['confirmed_shares_micros'])) / 1_000_000:.6f}"
                            if row["confirmed_shares_micros"] is not None
                            else None
                        ),
                        "nav": (
                            f"{Decimal(int(row['nav_micros'])) / 1_000_000:.6f}"
                            if row["nav_micros"] is not None
                            else None
                        ),
                        "ledger_status": (
                            "POSTED" if row["posted_transaction_id"] is not None else "UNPOSTED"
                        ),
                    }
                )
            planned = int(item["candidate_amount_minor"])
            items.append(
                {
                    "instrument_code": str(item["instrument_code"]),
                    "instrument_name": str(item["instrument_name"]),
                    "planned_amount": _money(planned),
                    "executed_amount": _money(executed),
                    "unexecuted_amount": _money(max(planned - executed, 0)),
                    "transactions": [
                        {
                            "transaction_id": str(row["transaction_id"]),
                            "trade_date": str(row["trade_date"]),
                            "gross_amount": _money(int(row["amount_minor"])),
                            "linked_amount": _money(int(row["linked_amount_minor"])),
                            "nav": f"{Decimal(int(row['nav_micros'])) / 1_000_000:.6f}",
                            "shares": f"{Decimal(int(row['shares_micros'])) / 1_000_000:.6f}",
                        }
                        for row in valid_links
                    ],
                    "subscriptions": subscription_facts,
                }
            )
        planned_total = int(plan["contribution_amount_minor"])
        abandoned = (
            int(plan["abandoned_amount_minor"])
            if plan["abandoned_amount_minor"] is not None
            else max(planned_total - executed_total, 0)
        )
        status = str(plan["status"])
        skip_reason = None
        if status == "SKIPPED":
            skip_audit = connection.execute(
                """
                SELECT details_json FROM audit_events
                WHERE entity_type='investment_plan' AND entity_id=?
                  AND action='INVESTMENT_PLAN_SKIPPED'
                ORDER BY occurred_at DESC, id DESC LIMIT 1
                """,
                (plan_id,),
            ).fetchone()
            if skip_audit is not None:
                skip_reason = json.loads(str(skip_audit["details_json"])).get("reason")
        valuation = self._valuation(connection, plan=plan)
        result = {
            "weekly_plan_id": plan_id,
            "portfolio_id": str(plan["portfolio_id"]),
            "account_id": str(plan["account_id"]),
            "period_start": str(plan["period_start"]),
            "period_end": str(plan["period_end"]),
            "plan_status": status,
            "plan_outcome": {
                "EXECUTED": "全部执行",
                "SKIPPED": "已跳过",
                "PARTIALLY_EXECUTED_CLOSED": "部分执行后结束",
            }.get(status, "尚未结束"),
            "final_eligible": status in FINAL_PLAN_STATUSES,
            "preview_only": status not in FINAL_PLAN_STATUSES,
            "planned_amount": _money(planned_total),
            "executed_amount": _money(executed_total),
            "abandoned_amount": _money(abandoned if status != "EXECUTED" else 0),
            "execution_rate_pct": (
                f"{Decimal(executed_total) / Decimal(planned_total) * 100:.2f}"
                if planned_total
                else "0.00"
            ),
            "closure": (
                {
                    "reason_code": plan["closure_reason_code"],
                    "note": plan["closure_note"],
                    "carry_forward": bool(plan["carry_forward"]),
                }
                if status == "PARTIALLY_EXECUTED_CLOSED"
                else None
            ),
            "skip_reason": skip_reason,
            "items": items,
            "workflow_checks": {
                "duplicate_transaction_links": len(
                    [row for item in items for row in item["transactions"]]
                )
                - len({row["transaction_id"] for item in items for row in item["transactions"]}),
                "unposted_confirmation_count": sum(
                    fact["ledger_status"] == "UNPOSTED"
                    for item in items
                    for fact in item["subscriptions"]
                    if fact["confirmation_id"] is not None
                ),
            },
            "valuation": valuation,
            "data_quality": (
                str(valuation.get("data_quality", "PASS"))
                if valuation["status"] == "AVAILABLE"
                else "WARNING"
            ),
            "automatic_trade": False,
            "financial_state_changed": False,
        }
        return result

    def preview(self, *, plan_id: str) -> JsonDict:
        with self._connect() as connection:
            facts = self._facts(connection, plan_id)
        return {**facts, "facts_hash": _hash(facts)}

    def create_draft(
        self,
        *,
        plan_id: str,
        idempotency_key: str,
        regeneration_reason: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        key = idempotency_key.strip()
        actor = actor_ref.strip()
        if not key or not actor:
            raise LedgerError("MISSING_REQUIRED_FIELD", "幂等键和操作人不能为空。")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            facts = self._facts(connection, plan_id)
            if not bool(facts["final_eligible"]):
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_PLAN_NOT_FINAL",
                        "周计划尚未结束; 只能查看非最终预览。",
                        http_status=409,
                    ),
                )
            reason = regeneration_reason.strip() if regeneration_reason else None
            facts_hash = _hash(facts)
            existing = connection.execute(
                "SELECT * FROM weekly_report_drafts WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["plan_id"]) != plan_id
                    or str(existing["facts_hash"]) != facts_hash
                    or existing["regeneration_reason"] != reason
                ):
                    self._rollback(
                        connection,
                        LedgerError(
                            "IDEMPOTENCY_CONFLICT", "幂等键已用于不同的周报请求。", http_status=409
                        ),
                    )
                connection.commit()
                return {
                    "draft": self._draft_data(existing, now=self._now()),
                    "confirmation_token": None,
                    "reused": True,
                }
            latest = connection.execute(
                "SELECT * FROM weekly_reports WHERE plan_id=? ORDER BY report_version DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
            version = 1 if latest is None else int(latest["report_version"]) + 1
            if latest is not None and not reason:
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_REGENERATION_REASON_REQUIRED",
                        "该计划已有正式周报; 生成新版本必须说明原因。",
                        http_status=409,
                    ),
                )
            active_version = connection.execute(
                """SELECT id, status FROM weekly_report_drafts
                   WHERE plan_id=? AND report_version=? LIMIT 1""",
                (plan_id, version),
            ).fetchone()
            if active_version is not None:
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_DRAFT_ALREADY_EXISTS",
                        "该周报版本已有草稿; 请读取或续签原草稿。",
                        http_status=409,
                        details={"draft_id": str(active_version["id"])},
                    ),
                )
            request_hash = _hash(
                {"plan_id": plan_id, "version": version, "facts_hash": facts_hash, "reason": reason}
            )
            token = secrets.token_urlsafe(24)
            draft_id = str(uuid4())
            now = self._now()
            connection.execute(
                """
                INSERT INTO weekly_report_drafts (
                    id, plan_id, report_version, facts_hash, content_json,
                    data_quality, valuation_status, regeneration_reason,
                    idempotency_key, confirmation_digest, status, created_by,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    draft_id,
                    plan_id,
                    version,
                    facts_hash,
                    _json(facts),
                    facts["data_quality"],
                    facts["valuation"]["status"],
                    reason,
                    key,
                    _digest(token),
                    actor,
                    _iso(now),
                    _iso(now + timedelta(minutes=self.settings.confirmation_ttl_minutes)),
                ),
            )
            row = connection.execute(
                "SELECT * FROM weekly_report_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            assert row is not None
            self._audit(
                connection,
                action="WEEKLY_REPORT_DRAFT_CREATED",
                entity_id=draft_id,
                actor_ref=actor,
                details={"request_hash": request_hash, "facts_hash": facts_hash},
            )
            connection.commit()
            return {
                "draft": self._draft_data(row, now=now),
                "confirmation_token": token,
                "reused": False,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_draft(self, *, draft_id: str) -> JsonDict:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM weekly_report_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if row is None:
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_DRAFT_NOT_FOUND", "没有找到周报草稿。", http_status=404
                    ),
                )
            if (
                str(row["status"]) == "PENDING"
                and _parse_iso(str(row["expires_at"])) <= self._now()
            ):
                connection.execute(
                    """UPDATE weekly_report_drafts SET status='EXPIRED'
                       WHERE id=? AND status='PENDING'""",
                    (draft_id,),
                )
                row = connection.execute(
                    "SELECT * FROM weekly_report_drafts WHERE id=?", (draft_id,)
                ).fetchone()
                assert row is not None
            connection.commit()
            return self._draft_data(row, now=self._now())
        finally:
            connection.close()

    def commit_draft(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            draft = connection.execute(
                "SELECT * FROM weekly_report_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if draft is None:
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_DRAFT_NOT_FOUND", "没有找到周报草稿。", http_status=404
                    ),
                )
            if str(draft["status"]) == "COMMITTED":
                report = connection.execute(
                    "SELECT * FROM weekly_reports WHERE id=?", (draft["committed_report_id"],)
                ).fetchone()
                assert report is not None
                connection.commit()
                return {"report": self._report_data(report), "idempotent_replay": True}
            if _parse_iso(str(draft["expires_at"])) <= self._now():
                connection.execute(
                    "UPDATE weekly_report_drafts SET status='EXPIRED' WHERE id=?", (draft_id,)
                )
                self._rollback(
                    connection,
                    LedgerError(
                        "CONFIRMATION_TOKEN_EXPIRED",
                        "周报确认已过期; 请重新创建相同事实的草稿。",
                        http_status=409,
                    ),
                )
            if not hmac.compare_digest(
                str(draft["confirmation_digest"]), _digest(confirmation_token)
            ):
                self._rollback(
                    connection,
                    LedgerError(
                        "CONFIRMATION_TOKEN_INVALID", "周报确认凭据无效。", http_status=403
                    ),
                )
            facts = self._facts(connection, str(draft["plan_id"]))
            if _hash(facts) != str(draft["facts_hash"]):
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_FACTS_CHANGED",
                        "周计划事实已变化; 请重新生成周报草稿。",
                        http_status=409,
                    ),
                )
            report_id = str(uuid4())
            run_id = str(uuid4())
            bundle_id = str(uuid4())
            now = _iso(self._now())
            version = int(draft["report_version"])
            previous = connection.execute(
                "SELECT id FROM weekly_reports WHERE plan_id=? AND is_current=1",
                (draft["plan_id"],),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO job_runs (
                    id, job_name, scheduled_for, idempotency_key, status,
                    started_at, finished_at, input_json, output_json, trace_id,
                    attempt_count, max_attempts
                ) VALUES (?, 'WEEKLY_PLAN_REPORT', ?, ?, 'SUCCESS', ?, ?, ?, ?, ?, 1, 1)
                """,
                (
                    run_id,
                    str(facts["period_end"]),
                    f"weekly-report:{draft['plan_id']}:{version}",
                    now,
                    now,
                    _json({"weekly_plan_id": draft["plan_id"], "report_version": version}),
                    _json({"weekly_report_id": report_id}),
                    str(uuid4()),
                ),
            )
            bundle_facts = {
                "weekly_plan_id": str(draft["plan_id"]),
                "report_version": version,
                "regeneration_reason": draft["regeneration_reason"],
                "report": json.loads(str(draft["content_json"])),
            }
            connection.execute(
                """
                INSERT INTO report_bundles (
                    id, portfolio_id, job_run_id, bundle_type, scheduled_for,
                    facts_json, facts_hash, data_quality, delivery_action,
                    reason_code, created_at
                ) VALUES (?, ?, ?, 'WEEKLY_PLAN_REPORT', ?, ?, ?, ?, 'SILENT', ?, ?)
                """,
                (
                    bundle_id,
                    facts["portfolio_id"],
                    run_id,
                    facts["period_end"],
                    _json(bundle_facts),
                    _hash(bundle_facts),
                    draft["data_quality"],
                    (
                        "WEEKLY_REPORT_FINALIZED"
                        if draft["valuation_status"] == "AVAILABLE"
                        else "WEEKLY_REPORT_FACTS_COMPLETE_VALUATION_LIMITED"
                    ),
                    now,
                ),
            )
            connection.execute(
                "UPDATE weekly_reports SET is_current=0 WHERE plan_id=? AND is_current=1",
                (draft["plan_id"],),
            )
            connection.execute(
                """
                INSERT INTO weekly_reports (
                    id, plan_id, report_bundle_id, report_version, facts_hash,
                    content_json, data_quality, valuation_status,
                    regeneration_reason, supersedes_report_id, is_current,
                    created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    report_id,
                    draft["plan_id"],
                    bundle_id,
                    version,
                    draft["facts_hash"],
                    draft["content_json"],
                    draft["data_quality"],
                    draft["valuation_status"],
                    draft["regeneration_reason"],
                    previous["id"] if previous is not None else None,
                    confirmed_by.strip(),
                    now,
                ),
            )
            updated = connection.execute(
                """
                UPDATE weekly_report_drafts
                SET status='COMMITTED', committed_at=?, committed_report_id=?
                WHERE id=? AND status='PENDING'
                """,
                (now, report_id, draft_id),
            )
            if updated.rowcount != 1:
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_CONCURRENT_COMMIT", "周报草稿已被并发处理。", http_status=409
                    ),
                )
            self._audit(
                connection,
                action="WEEKLY_REPORT_COMMITTED",
                entity_id=report_id,
                actor_ref=confirmed_by.strip(),
                details={
                    "draft_id": draft_id,
                    "report_version": version,
                    "financial_facts_created": False,
                },
            )
            row = connection.execute(
                "SELECT * FROM weekly_reports WHERE id=?", (report_id,)
            ).fetchone()
            assert row is not None
            connection.commit()
            return {
                "report": self._report_data(row),
                "idempotent_replay": False,
                "financial_facts_created": False,
                "notification_sent": False,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_draft(self, *, draft_id: str, actor_ref: str = "hermes") -> JsonDict:
        """Rotate only an expired, unchanged and uncommitted weekly-report draft token."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM weekly_report_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if row is None:
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_DRAFT_NOT_FOUND",
                        "没有找到周报草稿。",
                        http_status=404,
                    ),
                )
            if str(row["status"]) == "COMMITTED" or row["committed_at"] is not None:
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_DRAFT_ALREADY_COMMITTED",
                        "已提交的周报草稿不能续签。",
                        http_status=409,
                    ),
                )
            if _parse_iso(str(row["expires_at"])) > self._now():
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_DRAFT_NOT_EXPIRED",
                        "仍有效的周报草稿不需要续签。",
                        http_status=409,
                    ),
                )
            facts = self._facts(connection, str(row["plan_id"]))
            if _hash(facts) != str(row["facts_hash"]):
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_FACTS_CHANGED",
                        "周计划事实已变化; 不能续签旧周报草稿。",
                        http_status=409,
                    ),
                )
            token = secrets.token_urlsafe(24)
            now = self._now()
            updated = connection.execute(
                """
                UPDATE weekly_report_drafts
                SET confirmation_digest=?, expires_at=?, renewed_at=?,
                    renewal_count=renewal_count+1, status='PENDING'
                WHERE id=? AND committed_at IS NULL AND expires_at<=?
                  AND status IN ('PENDING','EXPIRED') AND facts_hash=?
                """,
                (
                    _digest(token),
                    _iso(now + timedelta(minutes=self.settings.confirmation_ttl_minutes)),
                    _iso(now),
                    draft_id,
                    _iso(now),
                    row["facts_hash"],
                ),
            )
            if updated.rowcount != 1:
                self._rollback(
                    connection,
                    LedgerError(
                        "WEEKLY_REPORT_DRAFT_RENEWAL_CONFLICT",
                        "周报草稿已被并发续签。",
                        http_status=409,
                    ),
                )
            renewed = connection.execute(
                "SELECT * FROM weekly_report_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            assert renewed is not None
            self._audit(
                connection,
                action="WEEKLY_REPORT_DRAFT_RENEWED",
                entity_id=draft_id,
                actor_ref=actor_ref,
                details={"renewal_count": int(renewed["renewal_count"])},
            )
            connection.commit()
            return {
                "draft": self._draft_data(renewed, now=now),
                "confirmation_token": token,
                "business_effect": "TOKEN_ROTATED_NO_REPORT_OR_FINANCIAL_FACT",
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_report(self, *, report_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM weekly_reports WHERE id=?", (report_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("WEEKLY_REPORT_NOT_FOUND", "没有找到正式周报。", http_status=404)
            return self._report_data(row)

    def list_reports(
        self,
        *,
        plan_id: str | None = None,
        portfolio_id: str | None = None,
        current_only: bool = False,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = (
            "SELECT r.* FROM weekly_reports r JOIN investment_plans p ON p.id=r.plan_id WHERE 1=1"
        )
        params: list[object] = []
        if plan_id:
            query += " AND r.plan_id=?"
            params.append(plan_id)
        if portfolio_id:
            query += " AND p.portfolio_id=?"
            params.append(portfolio_id)
        if current_only:
            query += " AND r.is_current=1"
        query += " ORDER BY p.period_end DESC, r.report_version DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            return [self._report_data(row) for row in connection.execute(query, params).fetchall()]

    def report_statuses(self, *, portfolio_id: str) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.id, p.status, p.period_start, p.period_end,
                       r.id AS report_id, r.valuation_status, r.data_quality
                FROM investment_plans p
                LEFT JOIN weekly_reports r ON r.plan_id=p.id AND r.is_current=1
                WHERE p.portfolio_id=?
                  AND p.status IN ('EXECUTED','SKIPPED','PARTIALLY_EXECUTED_CLOSED')
                ORDER BY p.period_end DESC, p.id
                """,
                (portfolio_id,),
            ).fetchall()
        return [
            {
                "weekly_plan_id": str(row["id"]),
                "plan_status": str(row["status"]),
                "period_start": str(row["period_start"]),
                "period_end": str(row["period_end"]),
                "report_status": (
                    "MISSING"
                    if row["report_id"] is None
                    else (
                        "FACTS_COMPLETE_VALUATION_LIMITED"
                        if row["valuation_status"] == "LIMITED"
                        else "GENERATED"
                    )
                ),
                "report_id": row["report_id"],
                "data_quality": row["data_quality"],
            }
            for row in rows
        ]

    def generate_eligible(self, *, portfolio_id: str, actor_ref: str = "cron") -> JsonDict:
        """Finalize missing terminal-plan reports under an approved automation run."""
        with self._connect() as connection:
            plan_ids = [
                str(row["id"])
                for row in connection.execute(
                    """
                    SELECT p.id FROM investment_plans p
                    LEFT JOIN weekly_reports r ON r.plan_id=p.id AND r.is_current=1
                    WHERE p.portfolio_id=?
                      AND p.status IN ('EXECUTED','SKIPPED','PARTIALLY_EXECUTED_CLOSED')
                      AND r.id IS NULL
                    ORDER BY p.period_end, p.id
                    """,
                    (portfolio_id,),
                ).fetchall()
            ]
        reports: list[JsonDict] = []
        for plan_id in plan_ids:
            key = f"automatic-weekly-report:{plan_id}:1"
            draft = self.create_draft(
                plan_id=plan_id,
                idempotency_key=key,
                actor_ref=actor_ref,
            )
            token = draft["confirmation_token"]
            if token is None:
                draft_data = draft["draft"]
                if draft_data["status"] == "COMMITTED":
                    reports.append(
                        self.get_report(report_id=str(draft_data["committed_report_id"]))
                    )
                    continue
                if draft_data["status"] == "EXPIRED":
                    renewed = self.renew_draft(
                        draft_id=str(draft_data["id"]), actor_ref=actor_ref
                    )
                    draft = renewed
                    token = renewed["confirmation_token"]
                else:
                    raise LedgerError(
                        "WEEKLY_REPORT_AUTOMATION_DRAFT_PENDING",
                        "自动周报草稿已存在但尚未完成; 等待正式恢复后重试。",
                        http_status=409,
                        details={"weekly_plan_id": plan_id},
                    )
            committed = self.commit_draft(
                draft_id=str(draft["draft"]["id"]),
                confirmation_token=str(token),
                confirmed_by=actor_ref,
            )
            reports.append(dict(committed["report"]))
        return {
            "items": reports,
            "generated_count": len(reports),
            "eligible_count": len(plan_ids),
            "notification_sent": False,
            "financial_facts_created": False,
            "idempotent": True,
        }
