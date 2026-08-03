"""Confirmed cash ledger, official NAV backfill and deterministic runtime modes."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import cast
from uuid import uuid4

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError, utc_now
from investor_core.market_data import MarketDataService
from investor_core.source_lineage import resolve_source_lineage

MONEY_SCALE = Decimal("100")
OFFICIAL_LINEAGES = {"FUND_MANAGER_OFFICIAL", "WIND"}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _money_minor(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise LedgerError("INVALID_AMOUNT", "amount must be decimal") from exc
    if not amount.is_finite() or amount <= 0:
        raise LedgerError("INVALID_AMOUNT", "amount must be positive")
    return int((amount * MONEY_SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(value: int) -> str:
    return f"{value / 100:.2f}"


class CapitalService:
    """Persist only explicit cash facts and sourced official observations."""

    def __init__(self, settings: Settings, *, now: Callable[[], datetime] = utc_now) -> None:
        self.settings = settings
        self._now = now
        self._market = MarketDataService(settings, now=now)

    def _connect(self) -> sqlite3.Connection:
        path = (
            ":memory:"
            if str(self.settings.db_path) == ":memory:"
            else str(Path(self.settings.db_path).resolve())
        )
        connection = sqlite3.connect(path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @staticmethod
    def _cash_semantics(event_type: str, amount_minor: int) -> tuple[int, bool]:
        normalized = event_type.upper()
        if normalized in {"DEPOSIT", "DIVIDEND", "INTEREST"}:
            return amount_minor, normalized == "DEPOSIT"
        if normalized in {"WITHDRAWAL", "FEE"}:
            return -amount_minor, normalized == "WITHDRAWAL"
        raise LedgerError("CASH_EVENT_TYPE_INVALID", "unsupported cash event type")

    @staticmethod
    def _draft_data(row: sqlite3.Row, *, include_token: str | None = None) -> JsonDict:
        result: JsonDict = {
            "id": str(row["id"]),
            "portfolio_id": str(row["portfolio_id"]),
            "account_id": str(row["account_id"]),
            "event_type": str(row["event_type"]),
            "event_date": str(row["event_date"]),
            "amount": _money(int(row["amount_minor"])),
            "currency": str(row["currency"]),
            "source": str(row["source"]),
            "note": row["note"],
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "expires_at": str(row["expires_at"]),
            "committed_event_id": row["committed_event_id"],
            "holdings_changed": False,
            "transactions_created": False,
            "automatic_trade": False,
        }
        if include_token is not None:
            result["confirmation_token"] = include_token
        return result

    def create_cash_event_draft(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        event_type: str,
        event_date: date,
        amount: str,
        source: str,
        idempotency_key: str,
        currency: str = "CNY",
        note: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        del actor_ref
        normalized_type = event_type.strip().upper()
        amount_minor = _money_minor(amount)
        self._cash_semantics(normalized_type, amount_minor)
        normalized_source = source.strip()
        if not normalized_source:
            raise LedgerError("CASH_SOURCE_REQUIRED", "source is required")
        normalized_currency = currency.strip().upper()
        payload = {
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "event_type": normalized_type,
            "event_date": event_date.isoformat(),
            "amount_minor": amount_minor,
            "currency": normalized_currency,
            "source": normalized_source,
            "note": note,
        }
        payload_hash = _hash(payload)
        token = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        created_at = self._now()
        expires_at = created_at + timedelta(minutes=15)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM cash_event_drafts WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise LedgerError(
                        "IDEMPOTENCY_CONFLICT",
                        "idempotency_key already belongs to a different cash event",
                        http_status=409,
                    )
                return {
                    "draft": self._draft_data(existing),
                    "idempotent_replay": True,
                    "confirmation_token_available": False,
                }
            portfolio = connection.execute(
                "SELECT id FROM portfolios WHERE id=? AND status='ACTIVE'",
                (portfolio_id,),
            ).fetchone()
            account = connection.execute(
                "SELECT id FROM accounts WHERE id=? AND portfolio_id=? AND status='ACTIVE'",
                (account_id, portfolio_id),
            ).fetchone()
            if portfolio is None or account is None:
                raise LedgerError(
                    "INVESTMENT_CONTEXT_INVALID",
                    "active portfolio/account context was not found",
                    http_status=404,
                )
            draft_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO cash_event_drafts (
                    id, portfolio_id, account_id, event_type, event_date,
                    amount_minor, currency, source, note, idempotency_key,
                    payload_hash, confirmation_token_hash, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    draft_id,
                    portfolio_id,
                    account_id,
                    normalized_type,
                    event_date.isoformat(),
                    amount_minor,
                    normalized_currency,
                    normalized_source,
                    note,
                    idempotency_key,
                    payload_hash,
                    token_hash,
                    _iso(created_at),
                    _iso(expires_at),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM cash_event_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            assert row is not None
            return {
                "draft": self._draft_data(row, include_token=token),
                "idempotent_replay": False,
                "confirmation_required": True,
                "confirmation_scope": "ONE_EXACT_CASH_EVENT",
            }

    def commit_cash_event_draft(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM cash_event_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "CASH_DRAFT_NOT_FOUND",
                    "cash event draft was not found",
                    http_status=404,
                )
            if str(row["status"]) == "COMMITTED":
                event = connection.execute(
                    "SELECT * FROM cash_ledger_events WHERE id=?",
                    (row["committed_event_id"],),
                ).fetchone()
                assert event is not None
                connection.commit()
                return {"event": self._cash_event_data(event), "idempotent_replay": True}
            if str(row["status"]) != "PENDING":
                raise LedgerError(
                    "CASH_DRAFT_NOT_PENDING",
                    "cash event draft is not pending",
                    http_status=409,
                )
            expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            if now > expires_at:
                connection.execute(
                    "UPDATE cash_event_drafts SET status='EXPIRED' WHERE id=?", (draft_id,)
                )
                connection.commit()
                raise LedgerError(
                    "CASH_DRAFT_EXPIRED",
                    "cash event draft has expired",
                    http_status=409,
                )
            actual_hash = hashlib.sha256(confirmation_token.encode()).hexdigest()
            if not secrets.compare_digest(actual_hash, str(row["confirmation_token_hash"])):
                raise LedgerError(
                    "CONFIRMATION_TOKEN_INVALID",
                    "confirmation token is invalid",
                    http_status=409,
                )
            signed_amount, external = self._cash_semantics(
                str(row["event_type"]), int(row["amount_minor"])
            )
            event_id = str(uuid4())
            committed_at = _iso(now)
            connection.execute(
                """
                INSERT INTO cash_ledger_events (
                    id, portfolio_id, account_id, event_type, event_date,
                    amount_minor, signed_amount_minor, is_external_flow,
                    currency, source, note, draft_id, payload_hash,
                    committed_by, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    row["portfolio_id"],
                    row["account_id"],
                    row["event_type"],
                    row["event_date"],
                    row["amount_minor"],
                    signed_amount,
                    external,
                    row["currency"],
                    row["source"],
                    row["note"],
                    draft_id,
                    row["payload_hash"],
                    confirmed_by,
                    committed_at,
                ),
            )
            connection.execute(
                """
                UPDATE cash_event_drafts
                SET status='COMMITTED', committed_at=?, committed_event_id=?
                WHERE id=?
                """,
                (committed_at, event_id, draft_id),
            )
            connection.execute(
                """
                INSERT INTO audit_events (
                    id, occurred_at, actor_type, actor_ref, action, entity_type,
                    entity_id, after_hash, details_json, trace_id
                ) VALUES (?, ?, 'USER', ?, 'CASH_EVENT_COMMITTED',
                          'cash_ledger_event', ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    committed_at,
                    confirmed_by,
                    event_id,
                    row["payload_hash"],
                    _json(
                        {
                            "event_type": row["event_type"],
                            "event_date": row["event_date"],
                            "amount_minor": row["amount_minor"],
                            "is_external_flow": external,
                        }
                    ),
                    str(uuid4()),
                ),
            )
            connection.commit()
            event = connection.execute(
                "SELECT * FROM cash_ledger_events WHERE id=?", (event_id,)
            ).fetchone()
            assert event is not None
            return {"event": self._cash_event_data(event), "idempotent_replay": False}

    @staticmethod
    def _cash_event_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "portfolio_id": str(row["portfolio_id"]),
            "account_id": str(row["account_id"]),
            "event_type": str(row["event_type"]),
            "event_date": str(row["event_date"]),
            "amount": _money(int(row["amount_minor"])),
            "signed_amount": _money(int(row["signed_amount_minor"])),
            "is_external_flow": bool(row["is_external_flow"]),
            "currency": str(row["currency"]),
            "source": str(row["source"]),
            "note": row["note"],
            "committed_by": str(row["committed_by"]),
            "committed_at": str(row["committed_at"]),
            "holdings_changed": False,
            "transactions_created": False,
            "automatic_trade": False,
        }

    def list_cash_events(
        self,
        *,
        portfolio_id: str,
        account_id: str | None = None,
        limit: int = 200,
    ) -> JsonDict:
        query = "SELECT * FROM cash_ledger_events WHERE portfolio_id=?"
        params: list[object] = [portfolio_id]
        if account_id:
            query += " AND account_id=?"
            params.append(account_id)
        query += " ORDER BY event_date, committed_at, id LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            items = [self._cash_event_data(row) for row in rows]
            cash_events = sum(int(row["signed_amount_minor"]) for row in rows)
            trade_cash = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(
                        CASE side WHEN 'BUY' THEN -amount_minor ELSE amount_minor END
                    ), 0)
                    FROM transactions
                    WHERE portfolio_id=? AND kind='TRADE'
                      AND reversed_by_transaction_id IS NULL
                    """,
                    (portfolio_id,),
                ).fetchone()[0]
            )
            return {
                "items": items,
                "cash_event_balance": _money(cash_events),
                "trade_cash_effect": _money(trade_cash),
                "cash_balance": _money(cash_events + trade_cash),
                "cash_balance_minor": cash_events + trade_cash,
                "event_count": len(items),
                "methodology": "CONFIRMED_CASH_EVENTS_PLUS_COMMITTED_TRADES",
            }

    def record_official_nav_backfill(
        self,
        *,
        source_name: str,
        source_ref: str,
        source_lineage: str,
        observations: list[JsonDict],
        actor_ref: str = "hermes",
    ) -> JsonDict:
        normalized_lineage = resolve_source_lineage(
            source_name, source_ref, source_lineage
        )
        if normalized_lineage not in OFFICIAL_LINEAGES:
            raise LedgerError(
                "OFFICIAL_SOURCE_REQUIRED",
                "official backfill requires an independent official/professional lineage",
            )
        if not observations or len(observations) > 1000:
            raise LedgerError("BACKFILL_SIZE_INVALID", "provide between 1 and 1000 observations")
        normalized = sorted(
            [
                {
                    "instrument_code": str(item["instrument_code"]).strip().upper(),
                    "nav_date": str(item["nav_date"]),
                    "nav": str(item["nav"]),
                    "observed_at": str(item["observed_at"]),
                }
                for item in observations
            ],
            key=lambda item: (item["instrument_code"], item["nav_date"]),
        )
        facts_input = {
            "source_name": source_name.strip(),
            "source_ref": source_ref.strip(),
            "source_lineage": normalized_lineage,
            "observations": normalized,
        }
        facts_hash = _hash(facts_input)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM official_nav_backfill_batches WHERE facts_hash=?",
                (facts_hash,),
            ).fetchone()
            if existing is not None:
                return self._backfill_data(existing, idempotent_replay=True)

        created = 0
        replayed = 0
        conflicts: list[JsonDict] = []
        snapshots: list[JsonDict] = []
        for item in normalized:
            item_conflicted = False
            with self._connect() as connection:
                instrument = connection.execute(
                    "SELECT id FROM instruments WHERE code=? AND status='ACTIVE'",
                    (item["instrument_code"],),
                ).fetchone()
                if instrument is not None:
                    rows = connection.execute(
                        """
                        SELECT nav_micros, source_name, source_lineage
                        FROM market_nav_snapshots
                        WHERE instrument_id=? AND nav_date=?
                          AND source_type='OFFICIAL'
                          AND verification_status='VERIFIED'
                        """,
                        (instrument["id"], item["nav_date"]),
                    ).fetchall()
                    expected_nav = int(
                        (Decimal(item["nav"]) * Decimal("1000000")).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    )
                    for row in rows:
                        if int(row["nav_micros"]) != expected_nav:
                            item_conflicted = True
                            conflicts.append(
                                {
                                    "instrument_code": item["instrument_code"],
                                    "nav_date": item["nav_date"],
                                    "existing_source": str(row["source_name"]),
                                    "existing_lineage": str(row["source_lineage"]),
                                }
                            )
            if item_conflicted:
                continue
            result = self._market.record_nav_snapshot(
                instrument_code=item["instrument_code"],
                nav_date_value=item["nav_date"],
                nav=item["nav"],
                source_type="OFFICIAL",
                source_name=source_name,
                source_ref=source_ref,
                source_lineage=normalized_lineage,
                verification_status="VERIFIED",
                observed_at_value=item["observed_at"],
                actor_ref=actor_ref,
            )
            snapshots.append(cast(JsonDict, result["snapshot"]))
            if bool(result["created"]):
                created += 1
            else:
                replayed += 1
        facts = {
            **facts_input,
            "snapshot_ids": [item["id"] for item in snapshots],
            "conflicts": conflicts,
        }
        batch_id = str(uuid4())
        now = _iso(self._now())
        status = "CONFLICT" if conflicts else "COMPLETED"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO official_nav_backfill_batches (
                    id, source_name, source_lineage, source_ref, requested_count,
                    created_count, replayed_count, conflict_count, status,
                    facts_json, facts_hash, actor_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    source_name.strip(),
                    normalized_lineage,
                    source_ref.strip(),
                    len(normalized),
                    created,
                    replayed,
                    len(conflicts),
                    status,
                    _json(facts),
                    facts_hash,
                    actor_ref,
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM official_nav_backfill_batches WHERE id=?", (batch_id,)
            ).fetchone()
            assert row is not None
            return self._backfill_data(row, idempotent_replay=False)

    @staticmethod
    def _backfill_data(row: sqlite3.Row, *, idempotent_replay: bool) -> JsonDict:
        return {
            "id": str(row["id"]),
            "source_name": str(row["source_name"]),
            "source_lineage": str(row["source_lineage"]),
            "source_ref": str(row["source_ref"]),
            "requested_count": int(row["requested_count"]),
            "created_count": int(row["created_count"]),
            "replayed_count": int(row["replayed_count"]),
            "conflict_count": int(row["conflict_count"]),
            "status": str(row["status"]),
            "facts": json.loads(str(row["facts_json"])),
            "created_at": str(row["created_at"]),
            "idempotent_replay": idempotent_replay,
            "holdings_changed": False,
            "transactions_created": False,
            "automatic_trade": False,
        }

    def list_official_nav_backfills(self, *, limit: int = 100) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM official_nav_backfill_batches
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._backfill_data(row, idempotent_replay=False) for row in rows]

    def runtime_mode(
        self,
        *,
        portfolio_id: str | None,
        as_of_date: date,
        persist: bool = True,
    ) -> JsonDict:
        with self._connect() as connection:
            high_alerts = int(
                connection.execute(
                    "SELECT COUNT(*) FROM alerts WHERE status='OPEN' AND severity='HIGH'"
                ).fetchone()[0]
            )
            if high_alerts:
                level, reason = "L3", "OPEN_HIGH_OPERATIONAL_ALERT"
                facts: JsonDict = {"open_high_alert_count": high_alerts}
            elif portfolio_id is None:
                level, reason = "L2", "INVESTMENT_CONTEXT_UNAVAILABLE"
                facts = {"portfolio_id": None}
            else:
                holdings = connection.execute(
                    """
                    SELECT t.instrument_id, i.code,
                           SUM(CASE t.side WHEN 'BUY' THEN t.shares_micros
                                          ELSE -t.shares_micros END) AS shares
                    FROM transactions t
                    JOIN instruments i ON i.id=t.instrument_id
                    WHERE t.portfolio_id=? AND t.trade_date<=?
                      AND t.kind!='REVERSAL' AND t.reversed_by_transaction_id IS NULL
                    GROUP BY t.instrument_id, i.code
                    HAVING shares != 0
                    """,
                    (portfolio_id, as_of_date.isoformat()),
                ).fetchall()
                missing: list[str] = []
                unverified: list[str] = []
                holding_codes = {str(item["code"]) for item in holdings}
                for holding in holdings:
                    nav = connection.execute(
                        """
                        SELECT source_type, verification_status
                        FROM market_nav_snapshots
                        WHERE instrument_id=? AND nav_date<=?
                        ORDER BY nav_date DESC,
                                 CASE verification_status WHEN 'VERIFIED' THEN 0 ELSE 1 END,
                                 observed_at DESC
                        LIMIT 1
                        """,
                        (holding["instrument_id"], as_of_date.isoformat()),
                    ).fetchone()
                    if nav is None:
                        missing.append(str(holding["code"]))
                    elif not (
                        str(nav["verification_status"]) == "VERIFIED"
                        and str(nav["source_type"]) in {"OFFICIAL", "PLATFORM"}
                    ):
                        unverified.append(str(holding["code"]))
                conflicting_codes: set[str] = set()
                conflict_rows = connection.execute(
                    """
                    SELECT facts_json
                    FROM official_nav_backfill_batches
                    WHERE status='CONFLICT'
                    """
                ).fetchall()
                for conflict_row in conflict_rows:
                    conflict_facts = json.loads(str(conflict_row["facts_json"]))
                    for conflict in conflict_facts.get("conflicts", []):
                        code = str(conflict.get("instrument_code", ""))
                        if code in holding_codes:
                            conflicting_codes.add(code)
                cash_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM cash_ledger_events WHERE portfolio_id=?",
                        (portfolio_id,),
                    ).fetchone()[0]
                )
                facts = {
                    "portfolio_id": portfolio_id,
                    "holding_count": len(holdings),
                    "missing_nav_codes": missing,
                    "unverified_nav_codes": unverified,
                    "official_nav_conflict_codes": sorted(conflicting_codes),
                    "cash_ledger_event_count": cash_count,
                    "open_high_alert_count": 0,
                }
                if missing:
                    level, reason = "L2", "VALUATION_FACTS_INCOMPLETE"
                elif conflicting_codes:
                    level, reason = "L2", "OFFICIAL_NAV_CONFLICT"
                elif unverified or cash_count == 0:
                    level, reason = "L1", "LIMITED_DATA_ASSURANCE"
                else:
                    level, reason = "L0", "FULL_DETERMINISTIC_FACTS_AVAILABLE"
            capabilities: JsonDict = {
                "system_health": True,
                "ledger_reads": level != "L3",
                "valuation": level in {"L0", "L1"},
                "planning_preview": level in {"L0", "L1"},
                "risk_scan": level in {"L0", "L1"},
                "periodic_review": level in {"L0", "L1"},
                "review_trend": level in {"L0", "L1"},
                "market_discovery": level in {"L0", "L1"},
                "research_watchlist": level in {"L0", "L1", "L2"},
                "watchlist_review_cycle": level in {"L0", "L1", "L2"},
                "research_content_change": level in {"L0", "L1", "L2"},
                "research_source_contract": level in {"L0", "L1", "L2"},
                "research_collection_runs": level in {"L0", "L1", "L2"},
                "research_source_configuration": level in {"L0", "L1", "L2"},
                "research_coverage_snapshot": level in {"L0", "L1", "L2"},
                "research_collection_orchestration": level in {"L0", "L1", "L2"},
                "research_connector_health": level in {"L0", "L1", "L2"},
                "research_coverage_changes": level in {"L0", "L1", "L2"},
                "review_action_outcome": level in {"L0", "L1", "L2"},
                "review_quality_snapshot": level in {"L0", "L1", "L2"},
                "model_may_fill_missing_facts": False,
                "automatic_trade": False,
            }
            payload: JsonDict = {
                "portfolio_id": portfolio_id,
                "as_of_date": as_of_date.isoformat(),
                "level": level,
                "reason_code": reason,
                "capabilities": capabilities,
                "facts": facts,
                "automatic_trade": False,
            }
            facts_hash = _hash(payload)
            existing = connection.execute(
                "SELECT id, created_at FROM runtime_mode_snapshots WHERE facts_hash=?",
                (facts_hash,),
            ).fetchone()
            snapshot_id = str(existing["id"]) if existing else str(uuid4())
            created_at = str(existing["created_at"]) if existing else _iso(self._now())
            if persist and existing is None:
                connection.execute(
                    """
                    INSERT INTO runtime_mode_snapshots (
                        id, portfolio_id, as_of_date, level, reason_code,
                        capabilities_json, facts_json, facts_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        portfolio_id,
                        as_of_date.isoformat(),
                        level,
                        reason,
                        _json(capabilities),
                        _json(facts),
                        facts_hash,
                        created_at,
                    ),
                )
                connection.commit()
            return {
                "snapshot_id": snapshot_id if persist else None,
                **payload,
                "created_at": created_at,
                "idempotent_replay": existing is not None,
            }
