"""Auditable NAV storage and deterministic portfolio valuation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from investor_core.config import Settings
from investor_core.ledger import LedgerError, LedgerService, utc_now

JsonDict = dict[str, Any]
NAV_SCALE = 1_000_000
SHARE_SCALE = 1_000_000
MONEY_SCALE = 100


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _scaled(value: str, scale: int, field: str) -> int:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise LedgerError("INVALID_DECIMAL", f"{field} must be a decimal value") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise LedgerError("INVALID_DECIMAL", f"{field} must be greater than zero")
    scaled = parsed * scale
    integral = scaled.to_integral_value(rounding=ROUND_HALF_UP)
    if scaled != integral:
        raise LedgerError(
            "DECIMAL_PRECISION_EXCEEDED",
            f"{field} supports at most 6 decimal places",
        )
    return int(integral)


def _money(value_minor: int) -> str:
    return f"{Decimal(value_minor) / MONEY_SCALE:.2f}"


def _nav(value_micros: int) -> str:
    return f"{Decimal(value_micros) / NAV_SCALE:.6f}"


def _canonical_hash(payload: JsonDict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MarketDataService:
    """Store immutable NAV observations and value committed holdings."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings = settings
        self._now = now
        self._ledger = LedgerService(settings, now=now)

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
    def _quality(source_type: str, verification_status: str) -> tuple[str, list[str]]:
        if source_type in {"OFFICIAL", "PLATFORM"} and verification_status == "VERIFIED":
            return "PASS", []
        return "WARNING", [
            "NAV is single-source or unverified; use the deterministic result conservatively"
        ]

    @classmethod
    def _snapshot_data(cls, row: sqlite3.Row) -> JsonDict:
        quality, warnings = cls._quality(str(row["source_type"]), str(row["verification_status"]))
        return {
            "id": row["id"],
            "instrument_id": row["instrument_id"],
            "instrument_code": row["instrument_code"],
            "instrument_name": row["instrument_name"],
            "nav_date": row["nav_date"],
            "nav": _nav(int(row["nav_micros"])),
            "currency": row["currency"],
            "source_type": row["source_type"],
            "source_name": row["source_name"],
            "source_ref": row["source_ref"],
            "verification_status": row["verification_status"],
            "observed_at": row["observed_at"],
            "ingested_at": row["ingested_at"],
            "record_hash": row["record_hash"],
            "data_quality": quality,
            "warnings": warnings,
        }

    def record_nav_snapshot(
        self,
        *,
        instrument_code: str,
        nav_date_value: str,
        nav: str,
        source_type: str,
        source_name: str,
        observed_at_value: str,
        verification_status: str = "UNVERIFIED",
        currency: str = "CNY",
        source_ref: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        try:
            nav_date = date.fromisoformat(nav_date_value)
        except ValueError as exc:
            raise LedgerError("INVALID_DATE", "nav_date must be an ISO date") from exc
        try:
            observed_at = datetime.fromisoformat(observed_at_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LedgerError("INVALID_DATETIME", "observed_at must be an ISO datetime") from exc
        if observed_at.tzinfo is None:
            raise LedgerError("INVALID_DATETIME", "observed_at must include a timezone")

        normalized_code = instrument_code.strip().upper()
        normalized_source_type = source_type.strip().upper()
        normalized_verification = verification_status.strip().upper()
        normalized_source_name = source_name.strip()
        normalized_currency = currency.strip().upper()
        if normalized_source_type not in {"OFFICIAL", "PLATFORM", "AGGREGATOR", "USER"}:
            raise LedgerError("INVALID_SOURCE_TYPE", "unsupported market data source type")
        if normalized_verification not in {"VERIFIED", "UNVERIFIED"}:
            raise LedgerError("INVALID_VERIFICATION", "unsupported verification status")
        if not normalized_source_name:
            raise LedgerError("INVALID_SOURCE", "source_name is required")
        nav_micros = _scaled(nav, NAV_SCALE, "nav")
        payload = {
            "instrument_code": normalized_code,
            "nav_date": nav_date.isoformat(),
            "nav_micros": nav_micros,
            "currency": normalized_currency,
            "source_type": normalized_source_type,
            "source_name": normalized_source_name,
            "source_ref": source_ref.strip() if source_ref else None,
            "verification_status": normalized_verification,
            "observed_at": _iso(observed_at),
        }
        record_hash = _canonical_hash(payload)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            instrument = connection.execute(
                "SELECT * FROM instruments WHERE code = ? AND status = 'ACTIVE'",
                (normalized_code,),
            ).fetchone()
            if instrument is None:
                raise LedgerError(
                    "INSTRUMENT_NOT_FOUND", "active instrument was not found", http_status=404
                )
            semantic_existing = connection.execute(
                """
                SELECT m.*, i.code AS instrument_code, i.name AS instrument_name
                FROM market_nav_snapshots m
                JOIN instruments i ON i.id = m.instrument_id
                WHERE m.instrument_id = ?
                  AND m.nav_date = ?
                  AND m.nav_micros = ?
                  AND m.source_type = ?
                  AND m.source_name = ?
                  AND COALESCE(m.source_ref, '') = COALESCE(?, '')
                ORDER BY m.observed_at DESC, m.rowid DESC
                LIMIT 1
                """,
                (
                    instrument["id"],
                    payload["nav_date"],
                    nav_micros,
                    normalized_source_type,
                    normalized_source_name,
                    payload["source_ref"],
                ),
            ).fetchone()
            if semantic_existing is not None:
                connection.commit()
                data = self._snapshot_data(semantic_existing)
                return {"snapshot": data, "created": False, "warnings": data["warnings"]}
            existing = connection.execute(
                """
                SELECT m.*, i.code AS instrument_code, i.name AS instrument_name
                FROM market_nav_snapshots m
                JOIN instruments i ON i.id = m.instrument_id
                WHERE m.record_hash = ?
                """,
                (record_hash,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                data = self._snapshot_data(existing)
                return {"snapshot": data, "created": False, "warnings": data["warnings"]}

            snapshot_id = str(uuid4())
            ingested_at = _iso(self._now())
            connection.execute(
                """
                INSERT INTO market_nav_snapshots (
                    id, instrument_id, nav_date, nav_micros, currency, source_type,
                    source_name, source_ref, verification_status, observed_at,
                    ingested_at, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    instrument["id"],
                    payload["nav_date"],
                    nav_micros,
                    normalized_currency,
                    normalized_source_type,
                    normalized_source_name,
                    payload["source_ref"],
                    normalized_verification,
                    payload["observed_at"],
                    ingested_at,
                    record_hash,
                ),
            )
            connection.execute(
                """
                INSERT INTO audit_events (
                    id, occurred_at, actor_type, actor_ref, action, entity_type,
                    entity_id, after_hash, details_json, trace_id
                ) VALUES (?, ?, 'AGENT', ?, 'MARKET_NAV_RECORDED',
                          'market_nav_snapshot', ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    ingested_at,
                    actor_ref,
                    snapshot_id,
                    record_hash,
                    json.dumps(
                        {
                            "instrument_code": normalized_code,
                            "nav_date": payload["nav_date"],
                            "source_name": normalized_source_name,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    str(uuid4()),
                ),
            )
            row = connection.execute(
                """
                SELECT m.*, i.code AS instrument_code, i.name AS instrument_name
                FROM market_nav_snapshots m
                JOIN instruments i ON i.id = m.instrument_id
                WHERE m.id = ?
                """,
                (snapshot_id,),
            ).fetchone()
            connection.commit()
            assert row is not None
            data = self._snapshot_data(row)
            return {"snapshot": data, "created": True, "warnings": data["warnings"]}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_nav_snapshots(
        self, *, instrument_code: str | None = None, limit: int = 100
    ) -> list[JsonDict]:
        if limit < 1 or limit > 500:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 500")
        query = """
            SELECT m.*, i.code AS instrument_code, i.name AS instrument_name
            FROM market_nav_snapshots m
            JOIN instruments i ON i.id = m.instrument_id
            WHERE 1 = 1
        """
        parameters: list[Any] = []
        if instrument_code:
            query += " AND i.code = ?"
            parameters.append(instrument_code.strip().upper())
        query += " ORDER BY m.nav_date DESC, m.observed_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            return [self._snapshot_data(row) for row in connection.execute(query, parameters)]

    def portfolio_valuation(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        as_of_date_value: str | None = None,
    ) -> JsonDict:
        try:
            as_of = (
                date.fromisoformat(as_of_date_value)
                if as_of_date_value
                else self._now().astimezone(ZoneInfo(self.settings.timezone)).date()
            )
        except ValueError as exc:
            raise LedgerError("INVALID_DATE", "as_of_date must be an ISO date") from exc
        holdings = self._ledger.list_holdings(portfolio_id=portfolio_id, account_id=account_id)
        positions: list[JsonDict] = []
        warnings: list[str] = []
        total_market_minor = 0
        total_cost_minor = 0
        complete = bool(holdings)
        aggregate_quality = "PASS"

        with self._connect() as connection:
            for holding in holdings:
                nav_rows = connection.execute(
                    """
                    SELECT m.*, i.code AS instrument_code, i.name AS instrument_name
                    FROM market_nav_snapshots m
                    JOIN instruments i ON i.id = m.instrument_id
                    WHERE m.instrument_id = ?
                      AND m.nav_date = (
                          SELECT MAX(nav_date)
                          FROM market_nav_snapshots
                          WHERE instrument_id = ? AND nav_date <= ?
                      )
                    ORDER BY
                        CASE
                            WHEN m.verification_status = 'VERIFIED'
                             AND m.source_type IN ('OFFICIAL','PLATFORM') THEN 0
                            ELSE 1
                        END,
                        m.observed_at DESC,
                        m.rowid DESC
                    """,
                    (
                        holding["instrument_id"],
                        holding["instrument_id"],
                        as_of.isoformat(),
                    ),
                ).fetchall()
                position: JsonDict = {"holding": holding}
                if not nav_rows:
                    complete = False
                    aggregate_quality = "SOURCE_ERROR"
                    position.update(
                        {
                            "data_quality": "SOURCE_ERROR",
                            "market_value": None,
                            "unrealized_pnl": None,
                            "return_pct": None,
                            "weight_pct": None,
                            "error": "NAV_MISSING",
                        }
                    )
                    warnings.append(f"Missing NAV for {holding['instrument_code']}")
                    positions.append(position)
                    continue

                distinct_navs = {int(candidate["nav_micros"]) for candidate in nav_rows}
                if len(distinct_navs) > 1:
                    complete = False
                    aggregate_quality = "SOURCE_ERROR"
                    position.update(
                        {
                            "data_quality": "SOURCE_ERROR",
                            "market_value": None,
                            "unrealized_pnl": None,
                            "return_pct": None,
                            "weight_pct": None,
                            "error": "NAV_CONFLICT",
                            "conflicting_snapshots": [
                                self._snapshot_data(candidate) for candidate in nav_rows
                            ],
                        }
                    )
                    warnings.append(
                        f"Conflicting NAV observations for {holding['instrument_code']}"
                    )
                    positions.append(position)
                    continue

                row = nav_rows[0]
                snapshot = self._snapshot_data(row)
                age_days = (as_of - date.fromisoformat(str(row["nav_date"]))).days
                if age_days > self.settings.market_nav_max_age_days:
                    complete = False
                    aggregate_quality = "SOURCE_ERROR"
                    position.update(
                        {
                            "nav_snapshot": snapshot,
                            "nav_age_days": age_days,
                            "data_quality": "SOURCE_ERROR",
                            "market_value": None,
                            "unrealized_pnl": None,
                            "return_pct": None,
                            "weight_pct": None,
                            "error": "NAV_STALE",
                        }
                    )
                    warnings.append(
                        f"Stale NAV for {holding['instrument_code']}: {age_days} days old"
                    )
                    positions.append(position)
                    continue

                if snapshot["data_quality"] == "WARNING" and aggregate_quality == "PASS":
                    aggregate_quality = "WARNING"
                shares_micros = int(
                    (Decimal(str(holding["total_shares"])) * SHARE_SCALE).to_integral_exact()
                )
                market_minor = (
                    shares_micros * int(row["nav_micros"]) * MONEY_SCALE
                    + SHARE_SCALE * NAV_SCALE // 2
                ) // (SHARE_SCALE * NAV_SCALE)
                cost_minor = int(
                    (Decimal(str(holding["cost_amount"])) * MONEY_SCALE).to_integral_exact()
                )
                pnl_minor = market_minor - cost_minor
                return_pct = (
                    (Decimal(pnl_minor) / Decimal(cost_minor) * Decimal(100))
                    if cost_minor
                    else Decimal(0)
                )
                total_market_minor += market_minor
                total_cost_minor += cost_minor
                position.update(
                    {
                        "nav_snapshot": snapshot,
                        "nav_age_days": age_days,
                        "data_quality": snapshot["data_quality"],
                        "market_value": _money(market_minor),
                        "unrealized_pnl": _money(pnl_minor),
                        "return_pct": f"{return_pct:.2f}",
                        "weight_pct": None,
                    }
                )
                warnings.extend(snapshot["warnings"])
                positions.append(position)

        if not holdings:
            aggregate_quality = "SOURCE_ERROR"
            warnings.append("No committed holdings were found")
        if complete and total_market_minor:
            for position in positions:
                market_value = position["market_value"]
                if market_value is not None:
                    weight = (
                        Decimal(str(market_value))
                        / (Decimal(total_market_minor) / MONEY_SCALE)
                        * Decimal(100)
                    )
                    position["weight_pct"] = f"{weight:.2f}"

        return {
            "as_of_date": as_of.isoformat(),
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "positions": positions,
            "totals": (
                {
                    "market_value": _money(total_market_minor),
                    "cost_amount": _money(total_cost_minor),
                    "unrealized_pnl": _money(total_market_minor - total_cost_minor),
                }
                if complete
                else None
            ),
            "data_quality": aggregate_quality,
            "warnings": list(dict.fromkeys(warnings)),
        }
