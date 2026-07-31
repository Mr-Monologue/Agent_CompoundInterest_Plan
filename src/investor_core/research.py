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
WATCHLIST_STATES = {
    "CANDIDATE",
    "OBSERVING",
    "REVIEW_DUE",
    "ADOPTED",
    "REJECTED",
    "ARCHIVED",
}
WATCHLIST_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"CANDIDATE"},
    "CANDIDATE": {"OBSERVING", "REJECTED", "ARCHIVED"},
    "OBSERVING": {"REVIEW_DUE", "ADOPTED", "REJECTED", "ARCHIVED"},
    "REVIEW_DUE": {"OBSERVING", "ADOPTED", "REJECTED", "ARCHIVED"},
    "ADOPTED": {"OBSERVING", "ARCHIVED"},
    "REJECTED": {"CANDIDATE", "ARCHIVED"},
    "ARCHIVED": {"CANDIDATE"},
}
ACTION_OUTCOMES = {"COMPLETED", "PARTIAL", "NOT_COMPLETED", "NOT_APPLICABLE"}
OUTCOME_QUALITY = {"VERIFIED", "USER_REPORTED", "UNVERIFIED"}
DISCOVERY_VERSION = "market-discovery-v2"
DISCOVERY_METRICS = (
    "observation_count",
    "verified_observation_count",
    "research_evidence_count",
    "return_20d_bps",
    "return_60d_bps",
    "return_120d_bps",
    "max_drawdown_bps",
    "annualized_volatility_bps",
    "freshness_days",
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _flatten(value: object, prefix: str = "") -> dict[str, object]:
    if isinstance(value, dict):
        flattened: dict[str, object] = {}
        for key in sorted(value):
            path = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten(value[key], path))
        return flattened
    if isinstance(value, list):
        flattened = {}
        for index, item in enumerate(value):
            path = f"{prefix}[{index}]"
            flattened.update(_flatten(item, path))
        return flattened or {prefix: []}
    return {prefix: value}


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

    @staticmethod
    def source_contract() -> JsonDict:
        """Describe the public sourced-research ingestion boundary."""
        return {
            "contract_version": "research-source-v1",
            "ingestion_tool": "market_research_evidence_record",
            "supported_evidence_types": sorted(EVIDENCE_TYPES),
            "required_fields": [
                "instrument_code",
                "evidence_date",
                "evidence_type",
                "source_name",
                "source_ref",
                "source_lineage",
                "facts",
            ],
            "configured_connectors": [],
            "automatic_sync": False,
            "idempotency": "CONTENT_HASH",
            "lineage_rule": (
                "SOURCE_LINEAGE_IDENTIFIES_THE_UPSTREAM_PUBLISHER_NOT_THE_FETCH_TOOL"
            ),
            "verification_rule": (
                "SOURCE_ATTRIBUTION_DOES_NOT_IMPLY_INDEPENDENT_VERIFICATION"
            ),
            "connector_boundary": (
                "PUBLIC_ADAPTER_CONTRACT_NO_DEFAULT_SOURCE_UNIVERSE_RANKING_OR_TRADE"
            ),
            "model_may_fill_missing_facts": False,
            "strategy_changed": False,
            "automatic_trade": False,
        }

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
            previous = connection.execute(
                """
                SELECT * FROM market_research_evidence
                WHERE instrument_id=? AND evidence_type=? AND source_lineage=?
                  AND evidence_date<=?
                ORDER BY evidence_date DESC, created_at DESC LIMIT 1
                """,
                (
                    instrument["id"],
                    normalized_type,
                    source_lineage.strip().upper(),
                    evidence_date.isoformat(),
                ),
            ).fetchone()
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
            previous_facts = (
                {} if previous is None else json.loads(str(previous["facts_json"]))
            )
            before = _flatten(previous_facts)
            after = _flatten(facts)
            added_keys = sorted(after.keys() - before.keys())
            removed_keys = sorted(before.keys() - after.keys())
            changed_keys = sorted(
                key for key in before.keys() & after.keys() if before[key] != after[key]
            )
            change_type = (
                "INITIAL"
                if previous is None
                else (
                    "CHANGED"
                    if added_keys or removed_keys or changed_keys
                    else "UNCHANGED"
                )
            )
            change_facts: JsonDict = {
                "evidence_id": evidence_id,
                "previous_evidence_id": (
                    None if previous is None else str(previous["id"])
                ),
                "instrument_code": str(instrument["code"]),
                "evidence_type": normalized_type,
                "source_lineage": source_lineage.strip().upper(),
                "change_type": change_type,
                "added_keys": added_keys,
                "removed_keys": removed_keys,
                "changed_keys": changed_keys,
                "change_boundary": "SOURCE_FACT_CHANGE_NOT_INVESTMENT_ADVICE",
            }
            connection.execute(
                """
                INSERT INTO research_evidence_changes (
                    id, evidence_id, previous_evidence_id, instrument_id,
                    change_type, added_keys_json, removed_keys_json,
                    changed_keys_json, facts_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    evidence_id,
                    None if previous is None else previous["id"],
                    instrument["id"],
                    change_type,
                    _json(added_keys),
                    _json(removed_keys),
                    _json(changed_keys),
                    _json(change_facts),
                    _iso(self._now()),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM market_research_evidence WHERE id=?",
                (evidence_id,),
            ).fetchone()
            assert row is not None
            result = self._evidence_data(connection, row, idempotent_replay=False)
            result["change"] = change_facts
            return result

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

    def list_evidence_changes(
        self,
        *,
        instrument_code: str | None = None,
        change_type: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = """
            SELECT c.*, i.code, i.name
            FROM research_evidence_changes c
            JOIN instruments i ON i.id=c.instrument_id
            WHERE 1=1
        """
        params: list[object] = []
        if instrument_code:
            query += " AND i.code=?"
            params.append(instrument_code.strip().upper())
        if change_type:
            query += " AND c.change_type=?"
            params.append(change_type.strip().upper())
        query += " ORDER BY c.created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            return [
                {
                    "id": str(row["id"]),
                    "instrument_code": str(row["code"]),
                    "instrument_name": str(row["name"]),
                    **json.loads(str(row["facts_json"])),
                    "created_at": str(row["created_at"]),
                    "automatic_trade": False,
                }
                for row in rows
            ]

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
            previous_run = connection.execute(
                """
                SELECT * FROM market_discovery_runs
                WHERE portfolio_id=? AND instrument_codes_json=?
                  AND lookback_days=? AND as_of_date<?
                ORDER BY as_of_date DESC, created_at DESC LIMIT 1
                """,
                (
                    portfolio_id,
                    _json(codes),
                    lookback_days,
                    as_of_date.isoformat(),
                ),
            ).fetchone()
            previous_items = (
                {}
                if previous_run is None
                else {
                    str(item["instrument_code"]): item
                    for item in json.loads(str(previous_run["facts_json"]))["items"]
                }
            )
            changes: list[JsonDict] = []
            for item in items:
                previous = previous_items.get(str(item["instrument_code"]))
                current_flags = set(item["review_flags"])
                previous_flags = (
                    set() if previous is None else set(previous["review_flags"])
                )
                added_flags = sorted(current_flags - previous_flags)
                removed_flags = sorted(previous_flags - current_flags)
                metric_deltas: JsonDict = {}
                if previous is not None:
                    for metric in DISCOVERY_METRICS:
                        before = previous.get(metric)
                        after = item.get(metric)
                        metric_deltas[metric] = (
                            None
                            if before is None or after is None
                            else int(after) - int(before)
                        )
                state_changed = (
                    previous is not None and previous["state"] != item["state"]
                )
                evidence_changed = (
                    previous is not None
                    and previous["research_evidence_count"]
                    != item["research_evidence_count"]
                )
                verification_changed = (
                    previous is not None
                    and previous["verified_observation_count"]
                    != item["verified_observation_count"]
                )
                if previous is None:
                    change_type = "INITIAL"
                    attention = bool(current_flags or item["state"] != "OBSERVE")
                else:
                    changed = bool(
                        state_changed
                        or added_flags
                        or removed_flags
                        or evidence_changed
                        or verification_changed
                    )
                    change_type = "CHANGED" if changed else "UNCHANGED"
                    attention = changed
                changes.append(
                    {
                        "instrument_id": item["instrument_id"],
                        "instrument_code": item["instrument_code"],
                        "instrument_name": item["instrument_name"],
                        "change_type": change_type,
                        "previous_state": (
                            None if previous is None else previous["state"]
                        ),
                        "current_state": item["state"],
                        "attention_required": attention,
                        "added_flags": added_flags,
                        "removed_flags": removed_flags,
                        "metric_deltas": metric_deltas,
                        "previous_latest_nav_date": (
                            None if previous is None else previous["latest_nav_date"]
                        ),
                        "current_latest_nav_date": item["latest_nav_date"],
                        "change_boundary": "FACTUAL_CHANGE_NOT_A_RECOMMENDATION",
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
                "change_summary": {
                    "previous_run_id": (
                        None if previous_run is None else str(previous_run["id"])
                    ),
                    "initial_count": sum(
                        item["change_type"] == "INITIAL" for item in changes
                    ),
                    "changed_count": sum(
                        item["change_type"] == "CHANGED" for item in changes
                    ),
                    "unchanged_count": sum(
                        item["change_type"] == "UNCHANGED" for item in changes
                    ),
                    "attention_count": sum(
                        bool(item["attention_required"]) for item in changes
                    ),
                },
                "changes": changes,
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
            for change in changes:
                connection.execute(
                    """
                    INSERT INTO market_discovery_changes (
                        id, run_id, previous_run_id, instrument_id, change_type,
                        previous_state, current_state, attention_required,
                        added_flags_json, removed_flags_json, metric_deltas_json,
                        facts_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        run_id,
                        None if previous_run is None else previous_run["id"],
                        change["instrument_id"],
                        change["change_type"],
                        change["previous_state"],
                        change["current_state"],
                        bool(change["attention_required"]),
                        _json(change["added_flags"]),
                        _json(change["removed_flags"]),
                        _json(change["metric_deltas"]),
                        _json(change),
                        _iso(self._now()),
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

    def list_changes(
        self,
        *,
        portfolio_id: str,
        run_id: str | None = None,
        attention_only: bool = False,
        limit: int = 200,
    ) -> list[JsonDict]:
        query = """
            SELECT c.*, r.portfolio_id, r.as_of_date
            FROM market_discovery_changes c
            JOIN market_discovery_runs r ON r.id=c.run_id
            WHERE r.portfolio_id=?
        """
        params: list[object] = [portfolio_id]
        if run_id:
            query += " AND c.run_id=?"
            params.append(run_id)
        if attention_only:
            query += " AND c.attention_required=1"
        query += " ORDER BY r.as_of_date DESC, c.created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            return [
                {
                    "id": str(row["id"]),
                    "run_id": str(row["run_id"]),
                    "previous_run_id": (
                        None
                        if row["previous_run_id"] is None
                        else str(row["previous_run_id"])
                    ),
                    "portfolio_id": str(row["portfolio_id"]),
                    "as_of_date": str(row["as_of_date"]),
                    **json.loads(str(row["facts_json"])),
                    "created_at": str(row["created_at"]),
                    "automatic_trade": False,
                }
                for row in rows
            ]

    def create_watchlist_transition_draft(
        self,
        *,
        portfolio_id: str,
        instrument_code: str,
        new_state: str,
        reason: str,
        review_due_date: date | None,
        actor_ref: str,
    ) -> JsonDict:
        normalized_state = new_state.strip().upper()
        if normalized_state not in WATCHLIST_STATES:
            raise LedgerError(
                "WATCHLIST_STATE_INVALID",
                "unsupported research watchlist state",
                details={"supported_states": sorted(WATCHLIST_STATES)},
            )
        if not reason.strip():
            raise LedgerError("WATCHLIST_REASON_REQUIRED", "transition reason is required")
        if normalized_state == "REVIEW_DUE" and review_due_date is None:
            raise LedgerError(
                "WATCHLIST_REVIEW_DATE_REQUIRED",
                "REVIEW_DUE requires review_due_date",
            )
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
            instrument = connection.execute(
                "SELECT id, code, name FROM instruments WHERE code=? AND status='ACTIVE'",
                (instrument_code.strip().upper(),),
            ).fetchone()
            if instrument is None:
                raise LedgerError(
                    "INSTRUMENT_NOT_FOUND",
                    "instrument was not found",
                    http_status=404,
                )
            entry = connection.execute(
                """
                SELECT * FROM research_watchlist_entries
                WHERE portfolio_id=? AND instrument_id=?
                """,
                (portfolio_id, instrument["id"]),
            ).fetchone()
            previous_state = None if entry is None else str(entry["state"])
            if normalized_state not in WATCHLIST_TRANSITIONS[previous_state]:
                raise LedgerError(
                    "WATCHLIST_TRANSITION_INVALID",
                    "requested watchlist state transition is not allowed",
                    details={
                        "previous_state": previous_state,
                        "new_state": normalized_state,
                        "allowed_states": sorted(WATCHLIST_TRANSITIONS[previous_state]),
                    },
                    http_status=409,
                )
            facts: JsonDict = {
                "portfolio_id": portfolio_id,
                "instrument_code": str(instrument["code"]),
                "instrument_name": str(instrument["name"]),
                "previous_state": previous_state,
                "new_state": normalized_state,
                "review_due_date": (
                    None if review_due_date is None else review_due_date.isoformat()
                ),
                "reason": reason.strip(),
                "watchlist_boundary": (
                    "RESEARCH_CLASSIFICATION_ONLY_NO_STRATEGY_OR_TRADE_CHANGE"
                ),
            }
            token = secrets.token_urlsafe(24)
            draft_id = str(uuid4())
            created = self._now()
            expires = created + timedelta(minutes=15)
            connection.execute(
                """
                INSERT INTO research_watchlist_transition_drafts (
                    id, portfolio_id, instrument_id, previous_state, new_state,
                    review_due_date, reason, status, confirmation_token_digest,
                    facts_hash, created_by, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    portfolio_id,
                    instrument["id"],
                    previous_state,
                    normalized_state,
                    facts["review_due_date"],
                    reason.strip(),
                    _token_digest(token),
                    _hash(facts),
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
                    "expires_at": _iso(expires),
                },
                "confirmation_token": token,
                "strategy_changed": False,
                "contribution_eligibility_changed": False,
                "holdings_changed": False,
                "transactions_created": False,
                "automatic_trade": False,
            }

    def commit_watchlist_transition(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            draft = connection.execute(
                "SELECT * FROM research_watchlist_transition_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            if draft is None:
                raise LedgerError(
                    "WATCHLIST_DRAFT_NOT_FOUND",
                    "watchlist transition draft was not found",
                    http_status=404,
                )
            existing = connection.execute(
                "SELECT * FROM research_watchlist_transitions WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return self._watchlist_transition_data(
                    connection, existing, idempotent_replay=True
                )
            if str(draft["status"]) != "PENDING":
                raise LedgerError(
                    "WATCHLIST_DRAFT_NOT_PENDING",
                    "watchlist transition draft is not pending",
                    http_status=409,
                )
            if self._now() > datetime.fromisoformat(
                str(draft["expires_at"]).replace("Z", "+00:00")
            ):
                connection.execute(
                    """
                    UPDATE research_watchlist_transition_drafts
                    SET status='EXPIRED' WHERE id=?
                    """,
                    (draft_id,),
                )
                connection.commit()
                raise LedgerError(
                    "WATCHLIST_DRAFT_EXPIRED",
                    "watchlist transition draft has expired",
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
            entry = connection.execute(
                """
                SELECT * FROM research_watchlist_entries
                WHERE portfolio_id=? AND instrument_id=?
                """,
                (draft["portfolio_id"], draft["instrument_id"]),
            ).fetchone()
            current_state = None if entry is None else str(entry["state"])
            if current_state != draft["previous_state"]:
                raise LedgerError(
                    "WATCHLIST_STATE_CONFLICT",
                    "watchlist state changed after the draft was created",
                    details={
                        "expected_state": draft["previous_state"],
                        "current_state": current_state,
                    },
                    http_status=409,
                )
            now = _iso(self._now())
            if entry is None:
                entry_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO research_watchlist_entries (
                        id, portfolio_id, instrument_id, state, review_due_date,
                        latest_reason, observation_started_at, last_reviewed_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        entry_id,
                        draft["portfolio_id"],
                        draft["instrument_id"],
                        draft["new_state"],
                        draft["review_due_date"],
                        draft["reason"],
                        now if str(draft["new_state"]) == "OBSERVING" else None,
                        now,
                        now,
                    ),
                )
            else:
                entry_id = str(entry["id"])
                connection.execute(
                    """
                    UPDATE research_watchlist_entries
                    SET state=?, review_due_date=?, latest_reason=?,
                        observation_started_at=CASE
                            WHEN ?='OBSERVING' THEN ?
                            ELSE observation_started_at
                        END,
                        last_reviewed_at=CASE
                            WHEN state='REVIEW_DUE' AND ?<>'REVIEW_DUE' THEN ?
                            ELSE last_reviewed_at
                        END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        draft["new_state"],
                        draft["review_due_date"],
                        draft["reason"],
                        draft["new_state"],
                        now,
                        draft["new_state"],
                        now,
                        now,
                        entry_id,
                    ),
                )
            transition_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO research_watchlist_transitions (
                    id, draft_id, entry_id, previous_state, new_state,
                    review_due_date, reason, facts_hash, confirmed_by, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    draft_id,
                    entry_id,
                    draft["previous_state"],
                    draft["new_state"],
                    draft["review_due_date"],
                    draft["reason"],
                    draft["facts_hash"],
                    confirmed_by,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE research_watchlist_transition_drafts
                SET status='COMMITTED', committed_at=? WHERE id=?
                """,
                (now, draft_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM research_watchlist_transitions WHERE id=?",
                (transition_id,),
            ).fetchone()
            assert row is not None
            return self._watchlist_transition_data(
                connection, row, idempotent_replay=False
            )

    @staticmethod
    def _watchlist_transition_data(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        idempotent_replay: bool,
    ) -> JsonDict:
        entry = connection.execute(
            """
            SELECT e.*, i.code, i.name
            FROM research_watchlist_entries e
            JOIN instruments i ON i.id=e.instrument_id
            WHERE e.id=?
            """,
            (row["entry_id"],),
        ).fetchone()
        assert entry is not None
        return {
            "transition": {
                "id": str(row["id"]),
                "draft_id": str(row["draft_id"]),
                "entry_id": str(row["entry_id"]),
                "portfolio_id": str(entry["portfolio_id"]),
                "instrument_code": str(entry["code"]),
                "instrument_name": str(entry["name"]),
                "previous_state": row["previous_state"],
                "new_state": str(row["new_state"]),
                "review_due_date": row["review_due_date"],
                "reason": str(row["reason"]),
                "facts_hash": str(row["facts_hash"]),
                "confirmed_by": str(row["confirmed_by"]),
                "confirmed_at": str(row["confirmed_at"]),
            },
            "idempotent_replay": idempotent_replay,
            "strategy_changed": False,
            "contribution_eligibility_changed": False,
            "holdings_changed": False,
            "transactions_created": False,
            "automatic_trade": False,
        }

    def list_watchlist(
        self,
        *,
        portfolio_id: str,
        state: str | None = None,
        limit: int = 200,
    ) -> list[JsonDict]:
        query = """
            SELECT e.*, i.code, i.name, i.asset_type
            FROM research_watchlist_entries e
            JOIN instruments i ON i.id=e.instrument_id
            WHERE e.portfolio_id=?
        """
        params: list[object] = [portfolio_id]
        if state:
            query += " AND e.state=?"
            params.append(state.strip().upper())
        query += " ORDER BY e.updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            return [
                {
                    "id": str(row["id"]),
                    "portfolio_id": str(row["portfolio_id"]),
                    "instrument_code": str(row["code"]),
                    "instrument_name": str(row["name"]),
                    "asset_type": str(row["asset_type"]),
                    "state": str(row["state"]),
                    "review_due_date": row["review_due_date"],
                    "observation_started_at": row["observation_started_at"],
                    "last_reviewed_at": row["last_reviewed_at"],
                    "latest_reason": str(row["latest_reason"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                    "watchlist_boundary": (
                        "RESEARCH_CLASSIFICATION_ONLY_NO_STRATEGY_OR_TRADE_CHANGE"
                    ),
                    "automatic_trade": False,
                }
                for row in rows
            ]

    def build_watchlist_review_snapshot(
        self,
        *,
        portfolio_id: str,
        as_of_date: date,
    ) -> JsonDict:
        """Build one immutable due-review fact package without changing watchlist state."""
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
            rows = connection.execute(
                """
                SELECT e.*, i.code, i.name, i.asset_type
                FROM research_watchlist_entries e
                JOIN instruments i ON i.id=e.instrument_id
                WHERE e.portfolio_id=?
                ORDER BY i.code
                """,
                (portfolio_id,),
            ).fetchall()
            items: list[JsonDict] = []
            for row in rows:
                state = str(row["state"])
                due_date = (
                    None
                    if row["review_due_date"] is None
                    else date.fromisoformat(str(row["review_due_date"]))
                )
                closed = state in {"REJECTED", "ARCHIVED"}
                if closed:
                    due_status = "CLOSED"
                elif state == "REVIEW_DUE":
                    due_status = "DUE"
                elif due_date is None:
                    due_status = "NOT_SCHEDULED"
                elif due_date <= as_of_date:
                    due_status = "DUE"
                else:
                    due_status = "UPCOMING"

                evidence_rows = connection.execute(
                    """
                    SELECT evidence_type, COUNT(*) AS evidence_count,
                           MAX(evidence_date) AS latest_evidence_date
                    FROM market_research_evidence
                    WHERE instrument_id=? AND evidence_date<=?
                    GROUP BY evidence_type
                    ORDER BY evidence_type
                    """,
                    (row["instrument_id"], as_of_date.isoformat()),
                ).fetchall()
                evidence_by_type = {
                    str(item["evidence_type"]): {
                        "count": int(item["evidence_count"]),
                        "latest_evidence_date": str(item["latest_evidence_date"]),
                    }
                    for item in evidence_rows
                }
                evidence_count = sum(
                    int(item["evidence_count"]) for item in evidence_rows
                )
                latest_discovery = connection.execute(
                    """
                    SELECT di.facts_json, dr.id AS run_id, dr.as_of_date,
                           dr.data_quality
                    FROM market_discovery_items di
                    JOIN market_discovery_runs dr ON dr.id=di.run_id
                    WHERE dr.portfolio_id=? AND di.instrument_id=?
                      AND dr.as_of_date<=?
                    ORDER BY dr.as_of_date DESC, dr.created_at DESC LIMIT 1
                    """,
                    (portfolio_id, row["instrument_id"], as_of_date.isoformat()),
                ).fetchone()
                discovery_facts: JsonDict = (
                    {}
                    if latest_discovery is None
                    else json.loads(str(latest_discovery["facts_json"]))
                )
                observation_started_at = (
                    None
                    if row["observation_started_at"] is None
                    else str(row["observation_started_at"])
                )
                observation_days = (
                    None
                    if observation_started_at is None
                    else max(
                        0,
                        (
                            as_of_date
                            - datetime.fromisoformat(
                                observation_started_at.replace("Z", "+00:00")
                            ).date()
                        ).days,
                    )
                )
                quality_flags: list[str] = []
                if not closed and due_date is None:
                    quality_flags.append("REVIEW_DATE_NOT_SCHEDULED")
                if not evidence_by_type:
                    quality_flags.append("RESEARCH_EVIDENCE_MISSING")
                if latest_discovery is None:
                    quality_flags.append("DISCOVERY_SNAPSHOT_MISSING")
                elif str(latest_discovery["data_quality"]) != "PASS":
                    quality_flags.append("DISCOVERY_DATA_QUALITY_LIMITED")
                if due_status == "DUE":
                    quality_flags.append("WATCHLIST_REVIEW_DUE")
                items.append(
                    {
                        "entry_id": str(row["id"]),
                        "instrument_code": str(row["code"]),
                        "instrument_name": str(row["name"]),
                        "asset_type": str(row["asset_type"]),
                        "watchlist_state": state,
                        "review_due_date": (
                            None if due_date is None else due_date.isoformat()
                        ),
                        "due_status": due_status,
                        "days_until_review": (
                            None
                            if due_date is None
                            else (due_date - as_of_date).days
                        ),
                        "observation_started_at": observation_started_at,
                        "observation_days": observation_days,
                        "last_reviewed_at": row["last_reviewed_at"],
                        "research_evidence_count": evidence_count,
                        "research_evidence_by_type": evidence_by_type,
                        "latest_discovery": (
                            None
                            if latest_discovery is None
                            else {
                                "run_id": str(latest_discovery["run_id"]),
                                "as_of_date": str(latest_discovery["as_of_date"]),
                                "data_quality": str(
                                    latest_discovery["data_quality"]
                                ),
                                "state": discovery_facts["state"],
                                "review_flags": discovery_facts["review_flags"],
                                "latest_nav_date": discovery_facts[
                                    "latest_nav_date"
                                ],
                                "return_20d_bps": discovery_facts[
                                    "return_20d_bps"
                                ],
                                "return_60d_bps": discovery_facts[
                                    "return_60d_bps"
                                ],
                                "return_120d_bps": discovery_facts[
                                    "return_120d_bps"
                                ],
                                "max_drawdown_bps": discovery_facts[
                                    "max_drawdown_bps"
                                ],
                            }
                        ),
                        "quality_flags": sorted(quality_flags),
                        "review_boundary": (
                            "FACTS_ONLY_REQUIRES_CONFIRMED_WATCHLIST_TRANSITION"
                        ),
                    }
                )

            due_count = sum(item["due_status"] == "DUE" for item in items)
            scheduled_count = sum(
                item["due_status"] in {"DUE", "UPCOMING"} for item in items
            )
            limited_count = sum(bool(item["quality_flags"]) for item in items)
            status = "REVIEW_REQUIRED" if due_count else "COMPLETED"
            quality = "WARNING" if limited_count else "PASS"
            reason_code = (
                "WATCHLIST_EMPTY"
                if not items
                else (
                    "WATCHLIST_REVIEW_DUE"
                    if due_count
                    else (
                        "WATCHLIST_REVIEW_CURRENT_WITH_LIMITS"
                        if quality == "WARNING"
                        else "WATCHLIST_REVIEW_CURRENT"
                    )
                )
            )
            facts: JsonDict = {
                "portfolio_id": portfolio_id,
                "as_of_date": as_of_date.isoformat(),
                "status": status,
                "reason_code": reason_code,
                "data_quality": quality,
                "summary": {
                    "entry_count": len(items),
                    "active_count": sum(
                        item["due_status"] != "CLOSED" for item in items
                    ),
                    "scheduled_count": scheduled_count,
                    "due_count": due_count,
                    "upcoming_count": sum(
                        item["due_status"] == "UPCOMING" for item in items
                    ),
                    "not_scheduled_count": sum(
                        item["due_status"] == "NOT_SCHEDULED" for item in items
                    ),
                    "closed_count": sum(
                        item["due_status"] == "CLOSED" for item in items
                    ),
                    "limited_count": limited_count,
                },
                "items": items,
                "snapshot_boundary": (
                    "REVIEW_FACTS_ONLY_NO_AUTOMATIC_STATE_STRATEGY_PLAN_OR_TRADE_CHANGE"
                ),
                "strategy_changed": False,
                "contribution_eligibility_changed": False,
                "holdings_changed": False,
                "transactions_created": False,
                "automatic_trade": False,
            }
            facts_hash = _hash(facts)
            existing = connection.execute(
                """
                SELECT * FROM research_watchlist_review_snapshots
                WHERE facts_hash=?
                """,
                (facts_hash,),
            ).fetchone()
            if existing is not None:
                return self._watchlist_review_snapshot_data(
                    existing,
                    idempotent_replay=True,
                )
            snapshot_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO research_watchlist_review_snapshots (
                    id, portfolio_id, as_of_date, status, data_quality,
                    reason_code, due_count, facts_json, facts_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    portfolio_id,
                    as_of_date.isoformat(),
                    status,
                    quality,
                    reason_code,
                    due_count,
                    _json(facts),
                    facts_hash,
                    _iso(self._now()),
                ),
            )
            connection.commit()
            created = connection.execute(
                """
                SELECT * FROM research_watchlist_review_snapshots WHERE id=?
                """,
                (snapshot_id,),
            ).fetchone()
            assert created is not None
            return self._watchlist_review_snapshot_data(
                created,
                idempotent_replay=False,
            )

    @staticmethod
    def _watchlist_review_snapshot_data(
        row: sqlite3.Row,
        *,
        idempotent_replay: bool,
    ) -> JsonDict:
        return {
            "id": str(row["id"]),
            **json.loads(str(row["facts_json"])),
            "facts_hash": str(row["facts_hash"]),
            "created_at": str(row["created_at"]),
            "idempotent_replay": idempotent_replay,
        }

    def list_watchlist_review_snapshots(
        self,
        *,
        portfolio_id: str,
        limit: int = 100,
    ) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM research_watchlist_review_snapshots
                WHERE portfolio_id=?
                ORDER BY as_of_date DESC, created_at DESC LIMIT ?
                """,
                (portfolio_id, limit),
            ).fetchall()
            return [
                self._watchlist_review_snapshot_data(
                    row,
                    idempotent_replay=False,
                )
                for row in rows
            ]

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

    def create_action_outcome_draft(
        self,
        *,
        action_item_id: str,
        outcome: str,
        evidence_quality: str,
        evidence_ref: str | None,
        note: str,
        actor_ref: str,
    ) -> JsonDict:
        normalized_outcome = outcome.strip().upper()
        normalized_quality = evidence_quality.strip().upper()
        if normalized_outcome not in ACTION_OUTCOMES:
            raise LedgerError(
                "REVIEW_ACTION_OUTCOME_INVALID",
                "unsupported review action outcome",
                details={"supported_outcomes": sorted(ACTION_OUTCOMES)},
            )
        if normalized_quality not in OUTCOME_QUALITY:
            raise LedgerError(
                "REVIEW_ACTION_OUTCOME_QUALITY_INVALID",
                "unsupported outcome evidence quality",
                details={"supported_quality": sorted(OUTCOME_QUALITY)},
            )
        if not note.strip():
            raise LedgerError(
                "REVIEW_ACTION_OUTCOME_NOTE_REQUIRED",
                "outcome note is required",
            )
        if normalized_quality == "VERIFIED" and not (evidence_ref or "").strip():
            raise LedgerError(
                "REVIEW_ACTION_OUTCOME_EVIDENCE_REQUIRED",
                "VERIFIED outcome quality requires evidence_ref",
            )
        normalized_evidence_ref = (evidence_ref or "").strip() or None
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
            if str(action["status"]) != "RESOLVED":
                raise LedgerError(
                    "REVIEW_ACTION_OUTCOME_REQUIRES_RESOLVED",
                    "an outcome can only be recorded for a resolved review action",
                    http_status=409,
                )
            existing = connection.execute(
                "SELECT id FROM review_action_outcomes WHERE action_item_id=?",
                (action_item_id,),
            ).fetchone()
            if existing is not None:
                raise LedgerError(
                    "REVIEW_ACTION_OUTCOME_ALREADY_RECORDED",
                    "review action outcome already exists",
                    http_status=409,
                )
            facts: JsonDict = {
                "action_item_id": action_item_id,
                "review_id": str(action["review_id"]),
                "portfolio_id": str(action["portfolio_id"]),
                "review_type": str(action["review_type"]),
                "period_end": str(action["period_end"]),
                "code": str(action["code"]),
                "outcome": normalized_outcome,
                "evidence_quality": normalized_quality,
                "evidence_ref": normalized_evidence_ref,
                "note": note.strip(),
                "outcome_boundary": "REVIEW_FACT_NOT_INVESTMENT_OR_TRADE_ACTION",
            }
            token = secrets.token_urlsafe(24)
            draft_id = str(uuid4())
            created = self._now()
            expires = created + timedelta(minutes=15)
            connection.execute(
                """
                INSERT INTO review_action_outcome_drafts (
                    id, action_item_id, outcome, evidence_quality, evidence_ref,
                    note, status, confirmation_token_digest, facts_hash,
                    created_by, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    action_item_id,
                    normalized_outcome,
                    normalized_quality,
                    facts["evidence_ref"],
                    note.strip(),
                    _token_digest(token),
                    _hash(facts),
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
                    "expires_at": _iso(expires),
                },
                "confirmation_token": token,
                "holdings_changed": False,
                "transactions_created": False,
                "strategy_changed": False,
                "automatic_trade": False,
            }

    def commit_action_outcome(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            draft = connection.execute(
                "SELECT * FROM review_action_outcome_drafts WHERE id=?",
                (draft_id,),
            ).fetchone()
            if draft is None:
                raise LedgerError(
                    "REVIEW_ACTION_OUTCOME_DRAFT_NOT_FOUND",
                    "review action outcome draft was not found",
                    http_status=404,
                )
            existing = connection.execute(
                "SELECT * FROM review_action_outcomes WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return self._outcome_data(existing, idempotent_replay=True)
            if str(draft["status"]) != "PENDING":
                raise LedgerError(
                    "REVIEW_ACTION_OUTCOME_DRAFT_NOT_PENDING",
                    "review action outcome draft is not pending",
                    http_status=409,
                )
            if self._now() > datetime.fromisoformat(
                str(draft["expires_at"]).replace("Z", "+00:00")
            ):
                connection.execute(
                    """
                    UPDATE review_action_outcome_drafts
                    SET status='EXPIRED' WHERE id=?
                    """,
                    (draft_id,),
                )
                connection.commit()
                raise LedgerError(
                    "REVIEW_ACTION_OUTCOME_DRAFT_EXPIRED",
                    "review action outcome draft has expired",
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
                "SELECT status FROM review_action_items WHERE id=?",
                (draft["action_item_id"],),
            ).fetchone()
            assert action is not None
            if str(action["status"]) != "RESOLVED":
                raise LedgerError(
                    "REVIEW_ACTION_OUTCOME_REQUIRES_RESOLVED",
                    "review action is no longer resolved",
                    http_status=409,
                )
            duplicate = connection.execute(
                "SELECT id FROM review_action_outcomes WHERE action_item_id=?",
                (draft["action_item_id"],),
            ).fetchone()
            if duplicate is not None:
                raise LedgerError(
                    "REVIEW_ACTION_OUTCOME_ALREADY_RECORDED",
                    "review action outcome already exists",
                    http_status=409,
                )
            outcome_id = str(uuid4())
            now = _iso(self._now())
            connection.execute(
                """
                INSERT INTO review_action_outcomes (
                    id, draft_id, action_item_id, outcome, evidence_quality,
                    evidence_ref, note, facts_hash, confirmed_by, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome_id,
                    draft_id,
                    draft["action_item_id"],
                    draft["outcome"],
                    draft["evidence_quality"],
                    draft["evidence_ref"],
                    draft["note"],
                    draft["facts_hash"],
                    confirmed_by,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE review_action_outcome_drafts
                SET status='COMMITTED', committed_at=? WHERE id=?
                """,
                (now, draft_id),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM review_action_outcomes WHERE id=?",
                (outcome_id,),
            ).fetchone()
            assert row is not None
            return self._outcome_data(row, idempotent_replay=False)

    @staticmethod
    def _outcome_data(row: sqlite3.Row, *, idempotent_replay: bool) -> JsonDict:
        return {
            "outcome": {
                "id": str(row["id"]),
                "draft_id": str(row["draft_id"]),
                "action_item_id": str(row["action_item_id"]),
                "outcome": str(row["outcome"]),
                "evidence_quality": str(row["evidence_quality"]),
                "evidence_ref": row["evidence_ref"],
                "note": str(row["note"]),
                "facts_hash": str(row["facts_hash"]),
                "confirmed_by": str(row["confirmed_by"]),
                "confirmed_at": str(row["confirmed_at"]),
                "outcome_boundary": "REVIEW_FACT_NOT_INVESTMENT_OR_TRADE_ACTION",
            },
            "idempotent_replay": idempotent_replay,
            "holdings_changed": False,
            "transactions_created": False,
            "strategy_changed": False,
            "automatic_trade": False,
        }

    def list_action_outcomes(
        self,
        *,
        portfolio_id: str,
        limit: int = 200,
    ) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.*, a.code, a.review_id, r.review_type, r.period_end
                FROM review_action_outcomes o
                JOIN review_action_items a ON a.id=o.action_item_id
                JOIN periodic_reviews r ON r.id=a.review_id
                WHERE r.portfolio_id=?
                ORDER BY o.confirmed_at DESC LIMIT ?
                """,
                (portfolio_id, limit),
            ).fetchall()
            return [
                {
                    **self._outcome_data(row, idempotent_replay=False)["outcome"],
                    "portfolio_id": portfolio_id,
                    "review_id": str(row["review_id"]),
                    "review_type": str(row["review_type"]),
                    "period_end": str(row["period_end"]),
                    "code": str(row["code"]),
                }
                for row in rows
            ]
