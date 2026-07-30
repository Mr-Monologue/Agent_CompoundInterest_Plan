"""Sourced market discovery and governed periodic-review action lifecycle."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from statistics import pstdev
from uuid import uuid4

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError, utc_now

EVIDENCE_TYPES = {
    "FUND_PROFILE",
    "HOLDINGS",
    "MANAGER",
    "FEES",
    "BENCHMARK",
    "MARKET_REGIME",
    "OTHER",
}
DECISIONS = {"ACKNOWLEDGE": "ACKNOWLEDGED", "RESOLVE": "RESOLVED"}
DISCOVERY_VERSION = "market-discovery-v1"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class ResearchService:
    """Build reviewable fact packages without selecting investments or trading."""

    def __init__(self, settings: Settings, *, now: Callable[[], datetime] = utc_now) -> None:
        self.settings = settings
        self._now = now

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

    def record_evidence(
        self,
        *,
        instrument_code: str,
        evidence_date: date,
        evidence_type: str,
        source_name: str,
        source_ref: str,
        source_lineage: str,
        facts: JsonDict,
        actor_ref: str,
    ) -> JsonDict:
        normalized_type = evidence_type.strip().upper()
        if normalized_type not in EVIDENCE_TYPES:
            raise LedgerError(
                "RESEARCH_EVIDENCE_TYPE_INVALID",
                "unsupported research evidence type",
                details={"supported_types": sorted(EVIDENCE_TYPES)},
            )
        if not source_name.strip() or not source_ref.strip() or not source_lineage.strip():
            raise LedgerError(
                "RESEARCH_SOURCE_REQUIRED",
                "source_name, source_ref and source_lineage are required",
            )
        if not facts:
            raise LedgerError("RESEARCH_FACTS_REQUIRED", "research evidence facts are required")
        with self._connect() as connection:
            instrument = connection.execute(
                "SELECT * FROM instruments WHERE code=? AND status='ACTIVE'",
                (instrument_code.strip().upper(),),
            ).fetchone()
            if instrument is None:
                raise LedgerError(
                    "INSTRUMENT_NOT_FOUND",
                    "instrument was not found",
                    http_status=404,
                )
            payload: JsonDict = {
                "instrument_code": str(instrument["code"]),
                "evidence_date": evidence_date.isoformat(),
                "evidence_type": normalized_type,
                "source_name": source_name.strip(),
                "source_ref": source_ref.strip(),
                "source_lineage": source_lineage.strip().upper(),
                "facts": facts,
            }
            facts_hash = _hash(payload)
            existing = connection.execute(
                "SELECT * FROM market_research_evidence WHERE facts_hash=?",
                (facts_hash,),
            ).fetchone()
            if existing is not None:
                return self._evidence_data(connection, existing, idempotent_replay=True)
            evidence_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO market_research_evidence (
                    id, instrument_id, evidence_date, evidence_type, source_name,
                    source_ref, source_lineage, facts_json, facts_hash,
                    recorded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    instrument["id"],
                    evidence_date.isoformat(),
                    normalized_type,
                    source_name.strip(),
                    source_ref.strip(),
                    source_lineage.strip().upper(),
                    _json(facts),
                    facts_hash,
                    actor_ref,
                    _iso(self._now()),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM market_research_evidence WHERE id=?",
                (evidence_id,),
            ).fetchone()
            assert row is not None
            return self._evidence_data(connection, row, idempotent_replay=False)

    @staticmethod
    def _evidence_data(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        idempotent_replay: bool = False,
    ) -> JsonDict:
        instrument = connection.execute(
            "SELECT code, name FROM instruments WHERE id=?",
            (row["instrument_id"],),
        ).fetchone()
        assert instrument is not None
        return {
            "id": str(row["id"]),
            "instrument_code": str(instrument["code"]),
            "instrument_name": str(instrument["name"]),
            "evidence_date": str(row["evidence_date"]),
            "evidence_type": str(row["evidence_type"]),
            "source_name": str(row["source_name"]),
            "source_ref": str(row["source_ref"]),
            "source_lineage": str(row["source_lineage"]),
            "facts": json.loads(str(row["facts_json"])),
            "facts_hash": str(row["facts_hash"]),
            "recorded_by": str(row["recorded_by"]),
            "created_at": str(row["created_at"]),
            "idempotent_replay": idempotent_replay,
            "automatic_trade": False,
        }

    def list_evidence(
        self,
        *,
        instrument_code: str | None = None,
        evidence_type: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = """
            SELECT e.* FROM market_research_evidence e
            JOIN instruments i ON i.id=e.instrument_id
            WHERE 1=1
        """
        params: list[object] = []
        if instrument_code:
            query += " AND i.code=?"
            params.append(instrument_code.strip().upper())
        if evidence_type:
            query += " AND e.evidence_type=?"
            params.append(evidence_type.strip().upper())
        query += " ORDER BY e.evidence_date DESC, e.created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            return [self._evidence_data(connection, row) for row in rows]

    @staticmethod
    def _return_bps(values: list[int], periods: int) -> int | None:
        if len(values) <= periods or values[-periods - 1] <= 0:
            return None
        return round((values[-1] / values[-periods - 1] - 1) * 10000)

    @staticmethod
    def _max_drawdown_bps(values: list[int]) -> int | None:
        if not values:
            return None
        peak = values[0]
        drawdown = 0.0
        for value in values:
            peak = max(peak, value)
            drawdown = min(drawdown, value / peak - 1)
        return round(drawdown * 10000)

    @staticmethod
    def _volatility_bps(values: list[int]) -> int | None:
        if len(values) < 3:
            return None
        returns = [
            math.log(current / previous)
            for previous, current in pairwise(values)
            if previous > 0 and current > 0
        ]
        if len(returns) < 2:
            return None
        return round(pstdev(returns) * math.sqrt(252) * 10000)

    def scan(
        self,
        *,
        portfolio_id: str,
        instrument_codes: list[str],
        as_of_date: date,
        lookback_days: int = 180,
    ) -> JsonDict:
        if not 30 <= lookback_days <= 730:
            raise LedgerError(
                "DISCOVERY_LOOKBACK_INVALID",
                "lookback_days must be between 30 and 730",
            )
        codes = sorted({code.strip().upper() for code in instrument_codes if code.strip()})
        if not codes:
            raise LedgerError(
                "DISCOVERY_UNIVERSE_REQUIRED",
                "an explicit registered instrument universe is required",
            )
        start = as_of_date - timedelta(days=lookback_days)
        items: list[JsonDict] = []
        with self._connect() as connection:
            portfolio = connection.execute(
                "SELECT id FROM portfolios WHERE id=?",
                (portfolio_id,),
            ).fetchone()
            if portfolio is None:
                raise LedgerError(
                    "PORTFOLIO_NOT_FOUND",
                    "portfolio was not found",
                    http_status=404,
                )
            for code in codes:
                instrument = connection.execute(
                    "SELECT * FROM instruments WHERE code=? AND status='ACTIVE'",
                    (code,),
                ).fetchone()
                if instrument is None:
                    raise LedgerError(
                        "DISCOVERY_INSTRUMENT_NOT_FOUND",
                        "discovery universe contains an unregistered instrument",
                        details={"instrument_code": code},
                        http_status=404,
                    )
                nav_rows = connection.execute(
                    """
                    SELECT * FROM market_nav_snapshots
                    WHERE instrument_id=? AND nav_date BETWEEN ? AND ?
                    ORDER BY nav_date, observed_at
                    """,
                    (instrument["id"], start.isoformat(), as_of_date.isoformat()),
                ).fetchall()
                by_date: dict[str, sqlite3.Row] = {}
                for row in nav_rows:
                    current = by_date.get(str(row["nav_date"]))
                    if current is None or (
                        str(row["verification_status"]) == "VERIFIED"
                        and str(current["verification_status"]) != "VERIFIED"
                    ):
                        by_date[str(row["nav_date"])] = row
                ordered = [by_date[key] for key in sorted(by_date)]
                values = [int(row["nav_micros"]) for row in ordered]
                evidence_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM market_research_evidence
                        WHERE instrument_id=? AND evidence_date<=?
                        """,
                        (instrument["id"], as_of_date.isoformat()),
                    ).fetchone()[0]
                )
                flags: list[str] = []
                latest_date = str(ordered[-1]["nav_date"]) if ordered else None
                freshness_days = (
                    (as_of_date - date.fromisoformat(latest_date)).days
                    if latest_date is not None
                    else None
                )
                return_20 = self._return_bps(values, 20)
                return_60 = self._return_bps(values, 60)
                return_120 = self._return_bps(values, 120)
                drawdown = self._max_drawdown_bps(values)
                volatility = self._volatility_bps(values)
                verified_count = sum(
                    str(row["verification_status"]) == "VERIFIED" for row in ordered
                )
                if len(values) < 2:
                    flags.append("NAV_HISTORY_INSUFFICIENT")
                    state = "DATA_BLOCKED"
                else:
                    if freshness_days is not None and freshness_days > 7:
                        flags.append("NAV_STALE")
                    if verified_count != len(values):
                        flags.append("NAV_SINGLE_SOURCE_OR_UNVERIFIED")
                    if return_20 is None or return_60 is None or return_120 is None:
                        flags.append("LONG_WINDOW_HISTORY_INCOMPLETE")
                    if drawdown is not None and drawdown <= -2000:
                        flags.append("DRAWDOWN_REVIEW")
                    if (
                        return_20 is not None
                        and return_60 is not None
                        and return_20 > 0
                        and return_60 > 0
                    ):
                        flags.append("POSITIVE_20D_AND_60D_OBSERVATION")
                    if evidence_count == 0:
                        flags.append("RESEARCH_EVIDENCE_MISSING")
                    state = "REVIEW" if flags else "OBSERVE"
                items.append(
                    {
                        "instrument_id": str(instrument["id"]),
                        "instrument_code": code,
                        "instrument_name": str(instrument["name"]),
                        "state": state,
                        "latest_nav_date": latest_date,
                        "latest_nav": (
                            f"{values[-1] / 1_000_000:.6f}" if values else None
                        ),
                        "observation_count": len(values),
                        "verified_observation_count": verified_count,
                        "research_evidence_count": evidence_count,
                        "return_20d_bps": return_20,
                        "return_60d_bps": return_60,
                        "return_120d_bps": return_120,
                        "max_drawdown_bps": drawdown,
                        "annualized_volatility_bps": volatility,
                        "freshness_days": freshness_days,
                        "review_flags": sorted(flags),
                        "selection_boundary": "FACTS_ONLY_NOT_A_RECOMMENDATION",
                    }
                )
            blocked = sum(item["state"] == "DATA_BLOCKED" for item in items)
            review = sum(item["state"] == "REVIEW" for item in items)
            unverified = any(
                "NAV_SINGLE_SOURCE_OR_UNVERIFIED" in item["review_flags"] for item in items
            )
            quality = "SOURCE_ERROR" if blocked == len(items) else (
                "WARNING" if blocked or unverified else "PASS"
            )
            status = "DATA_BLOCKED" if blocked == len(items) else (
                "DEGRADED" if quality != "PASS" else "COMPLETED"
            )
            reason = (
                "DISCOVERY_DATA_BLOCKED"
                if status == "DATA_BLOCKED"
                else (
                    "DISCOVERY_COMPLETED_WITH_LIMITS"
                    if status == "DEGRADED"
                    else "DISCOVERY_COMPLETED"
                )
            )
            facts: JsonDict = {
                "portfolio_id": portfolio_id,
                "as_of_date": as_of_date.isoformat(),
                "lookback_days": lookback_days,
                "instrument_codes": codes,
                "items": items,
                "summary": {
                    "requested_count": len(codes),
                    "observe_count": sum(item["state"] == "OBSERVE" for item in items),
                    "review_count": review,
                    "blocked_count": blocked,
                },
                "data_quality": quality,
                "reason_code": reason,
                "calculation_version": DISCOVERY_VERSION,
                "automatic_trade": False,
                "strategy_changed": False,
                "contribution_eligibility_changed": False,
            }
            facts_hash = _hash(facts)
            existing = connection.execute(
                "SELECT * FROM market_discovery_runs WHERE facts_hash=?",
                (facts_hash,),
            ).fetchone()
            if existing is not None:
                return self._run_data(connection, existing, idempotent_replay=True)
            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO market_discovery_runs (
                    id, portfolio_id, as_of_date, lookback_days,
                    instrument_codes_json, status, data_quality, reason_code,
                    facts_json, facts_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    portfolio_id,
                    as_of_date.isoformat(),
                    lookback_days,
                    _json(codes),
                    status,
                    quality,
                    reason,
                    _json(facts),
                    facts_hash,
                    _iso(self._now()),
                ),
            )
            for item in items:
                connection.execute(
                    """
                    INSERT INTO market_discovery_items (
                        id, run_id, instrument_id, state, latest_nav_date,
                        observation_count, return_20d_bps, return_60d_bps,
                        return_120d_bps, max_drawdown_bps,
                        annualized_volatility_bps, facts_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        run_id,
                        item["instrument_id"],
                        item["state"],
                        item["latest_nav_date"],
                        item["observation_count"],
                        item["return_20d_bps"],
                        item["return_60d_bps"],
                        item["return_120d_bps"],
                        item["max_drawdown_bps"],
                        item["annualized_volatility_bps"],
                        _json(item),
                    ),
                )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM market_discovery_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            assert row is not None
            return self._run_data(connection, row, idempotent_replay=False)

    @staticmethod
    def _run_data(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        idempotent_replay: bool = False,
    ) -> JsonDict:
        return {
            "id": str(row["id"]),
            **json.loads(str(row["facts_json"])),
            "status": str(row["status"]),
            "facts_hash": str(row["facts_hash"]),
            "created_at": str(row["created_at"]),
            "idempotent_replay": idempotent_replay,
        }

    def list_runs(self, *, portfolio_id: str, limit: int = 100) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM market_discovery_runs
                WHERE portfolio_id=?
                ORDER BY as_of_date DESC, created_at DESC LIMIT ?
                """,
                (portfolio_id, limit),
            ).fetchall()
            return [self._run_data(connection, row) for row in rows]

    def create_action_decision_draft(
        self,
        *,
        action_item_id: str,
        decision: str,
        reason: str,
        actor_ref: str,
    ) -> JsonDict:
        normalized = decision.strip().upper()
        if normalized not in DECISIONS:
            raise LedgerError(
                "REVIEW_ACTION_DECISION_INVALID",
                "decision must be ACKNOWLEDGE or RESOLVE",
            )
        if not reason.strip():
            raise LedgerError("REVIEW_ACTION_REASON_REQUIRED", "decision reason is required")
        with self._connect() as connection:
            action = connection.execute(
                """
                SELECT a.*, r.portfolio_id, r.review_type, r.period_end
                FROM review_action_items a
                JOIN periodic_reviews r ON r.id=a.review_id
                WHERE a.id=?
                """,
                (action_item_id,),
            ).fetchone()
            if action is None:
                raise LedgerError(
                    "REVIEW_ACTION_NOT_FOUND",
                    "review action item was not found",
                    http_status=404,
                )
            target = DECISIONS[normalized]
            if str(action["status"]) == target:
                raise LedgerError(
                    "REVIEW_ACTION_ALREADY_IN_STATE",
                    "review action already has the requested state",
                    http_status=409,
                )
            facts = {
                "action_item_id": action_item_id,
                "review_id": str(action["review_id"]),
                "portfolio_id": str(action["portfolio_id"]),
                "review_type": str(action["review_type"]),
                "period_end": str(action["period_end"]),
                "code": str(action["code"]),
                "previous_status": str(action["status"]),
                "decision": normalized,
                "new_status": target,
                "reason": reason.strip(),
            }
            facts_hash = _hash(facts)
            token = secrets.token_urlsafe(24)
            draft_id = str(uuid4())
            created = self._now()
            expires = created + timedelta(minutes=15)
            connection.execute(
                """
                INSERT INTO review_action_decision_drafts (
                    id, action_item_id, decision, reason, status,
                    confirmation_token_digest, facts_hash, created_by,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    action_item_id,
                    normalized,
                    reason.strip(),
                    _token_digest(token),
                    facts_hash,
                    actor_ref,
                    _iso(created),
                    _iso(expires),
                ),
            )
            connection.commit()
            return {
                "draft": {
                    "id": draft_id,
                    **facts,
                    "status": "PENDING",
                    "facts_hash": facts_hash,
                    "expires_at": _iso(expires),
                },
                "confirmation_token": token,
                "holdings_changed": False,
                "transactions_created": False,
                "automatic_trade": False,
            }

    def commit_action_decision(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            draft = connection.execute(
                "SELECT * FROM review_action_decision_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            if draft is None:
                raise LedgerError(
                    "REVIEW_ACTION_DRAFT_NOT_FOUND",
                    "review action decision draft was not found",
                    http_status=404,
                )
            existing = connection.execute(
                "SELECT * FROM review_action_decisions WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return self._decision_data(existing, idempotent_replay=True)
            if str(draft["status"]) != "PENDING":
                raise LedgerError(
                    "REVIEW_ACTION_DRAFT_NOT_PENDING",
                    "review action decision draft is not pending",
                    http_status=409,
                )
            if self._now() > datetime.fromisoformat(
                str(draft["expires_at"]).replace("Z", "+00:00")
            ):
                connection.execute(
                    "UPDATE review_action_decision_drafts SET status='EXPIRED' WHERE id=?",
                    (draft_id,),
                )
                connection.commit()
                raise LedgerError(
                    "REVIEW_ACTION_DRAFT_EXPIRED",
                    "review action decision draft has expired",
                    http_status=409,
                )
            if not secrets.compare_digest(
                str(draft["confirmation_token_digest"]),
                _token_digest(confirmation_token),
            ):
                raise LedgerError(
                    "CONFIRMATION_TOKEN_MISMATCH",
                    "confirmation token does not match",
                    http_status=409,
                )
            action = connection.execute(
                "SELECT * FROM review_action_items WHERE id=?",
                (draft["action_item_id"],),
            ).fetchone()
            assert action is not None
            target = DECISIONS[str(draft["decision"])]
            decision_id = str(uuid4())
            now = _iso(self._now())
            connection.execute(
                "UPDATE review_action_items SET status=? WHERE id=?",
                (target, action["id"]),
            )
            connection.execute(
                """
                INSERT INTO review_action_decisions (
                    id, draft_id, action_item_id, decision, reason,
                    previous_status, new_status, facts_hash,
                    confirmed_by, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    draft_id,
                    action["id"],
                    draft["decision"],
                    draft["reason"],
                    action["status"],
                    target,
                    draft["facts_hash"],
                    confirmed_by,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE review_action_decision_drafts
                SET status='COMMITTED', committed_at=? WHERE id=?
                """,
                (now, draft_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM review_action_decisions WHERE id=?",
                (decision_id,),
            ).fetchone()
            assert row is not None
            return self._decision_data(row, idempotent_replay=False)

    @staticmethod
    def _decision_data(row: sqlite3.Row, *, idempotent_replay: bool) -> JsonDict:
        return {
            "decision": {
                "id": str(row["id"]),
                "draft_id": str(row["draft_id"]),
                "action_item_id": str(row["action_item_id"]),
                "decision": str(row["decision"]),
                "reason": str(row["reason"]),
                "previous_status": str(row["previous_status"]),
                "new_status": str(row["new_status"]),
                "facts_hash": str(row["facts_hash"]),
                "confirmed_by": str(row["confirmed_by"]),
                "confirmed_at": str(row["confirmed_at"]),
            },
            "idempotent_replay": idempotent_replay,
            "holdings_changed": False,
            "transactions_created": False,
            "automatic_trade": False,
        }
