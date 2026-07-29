"""Deterministic portfolio performance, benchmark attribution and periodic reviews."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from calendar import monthrange
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError, utc_now

CALCULATION_VERSION = "performance-v1"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _money(minor: int) -> str:
    return f"{minor / 100:.2f}"


def _bps(value: float | None) -> int | None:
    return None if value is None or not math.isfinite(value) else round(value * 10000)


class PerformanceService:
    """Calculate only ledger- and NAV-backed facts; never generate trade advice."""

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
    def _period_bounds(review_type: str, anchor: date) -> tuple[date, date]:
        normalized = review_type.upper()
        if normalized == "MONTHLY":
            end = date(anchor.year, anchor.month, monthrange(anchor.year, anchor.month)[1])
            return date(anchor.year, anchor.month, 1), end
        if normalized == "QUARTERLY":
            first_month = ((anchor.month - 1) // 3) * 3 + 1
            end_month = first_month + 2
            end = date(anchor.year, end_month, monthrange(anchor.year, end_month)[1])
            return date(anchor.year, first_month, 1), end
        if normalized == "ANNUAL":
            return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
        raise LedgerError("REVIEW_TYPE_INVALID", "review_type must be monthly, quarterly or annual")

    @staticmethod
    def _transactions(
        connection: sqlite3.Connection,
        portfolio_id: str,
        end: date,
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
            SELECT t.*, i.code, i.name
            FROM transactions t
            JOIN instruments i ON i.id=t.instrument_id
            WHERE t.portfolio_id=? AND t.trade_date<=?
              AND t.kind!='REVERSAL' AND t.reversed_by_transaction_id IS NULL
            ORDER BY t.trade_date, t.committed_at, t.id
            """,
                (portfolio_id, end.isoformat()),
            ).fetchall()
        )

    @staticmethod
    def _nav(
        connection: sqlite3.Connection,
        instrument_id: str,
        at: date,
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT * FROM market_nav_snapshots
                WHERE instrument_id=? AND nav_date<=?
                ORDER BY nav_date DESC,
                         CASE verification_status WHEN 'VERIFIED' THEN 0 ELSE 1 END,
                         observed_at DESC
                LIMIT 1
                """,
                (instrument_id, at.isoformat()),
            ).fetchone(),
        )

    @staticmethod
    def _shares_at(transactions: list[sqlite3.Row], at: date) -> dict[str, int]:
        shares: dict[str, int] = {}
        for row in transactions:
            if str(row["trade_date"]) > at.isoformat():
                continue
            sign = 1 if str(row["side"]) == "BUY" else -1
            instrument_id = str(row["instrument_id"])
            shares[instrument_id] = shares.get(instrument_id, 0) + sign * int(row["shares_micros"])
        return shares

    def _value_at(
        self,
        connection: sqlite3.Connection,
        transactions: list[sqlite3.Row],
        at: date,
    ) -> tuple[int, list[JsonDict], list[str]]:
        positions: list[JsonDict] = []
        warnings: list[str] = []
        transaction_names = {
            str(row["instrument_id"]): (str(row["code"]), str(row["name"])) for row in transactions
        }
        total = 0
        for instrument_id, shares in self._shares_at(transactions, at).items():
            if shares == 0:
                continue
            nav = self._nav(connection, instrument_id, at)
            if nav is None:
                warnings.append(f"NAV_MISSING:{transaction_names[instrument_id][0]}:{at}")
                continue
            value_minor = round(shares * int(nav["nav_micros"]) / 10_000_000_000)
            total += value_minor
            code, name = transaction_names[instrument_id]
            positions.append(
                {
                    "instrument_id": instrument_id,
                    "instrument_code": code,
                    "instrument_name": name,
                    "shares": f"{shares / 1_000_000:.6f}",
                    "nav": f"{int(nav['nav_micros']) / 1_000_000:.6f}",
                    "nav_date": str(nav["nav_date"]),
                    "verification_status": str(nav["verification_status"]),
                    "value": _money(value_minor),
                    "value_minor": value_minor,
                }
            )
            if str(nav["verification_status"]) != "VERIFIED":
                warnings.append(f"NAV_UNVERIFIED:{code}:{nav['nav_date']}")
        return total, positions, sorted(set(warnings))

    @staticmethod
    def _xirr(cashflows: list[tuple[date, int]]) -> float | None:
        if (
            not cashflows
            or not any(v < 0 for _, v in cashflows)
            or not any(v > 0 for _, v in cashflows)
        ):
            return None
        base = cashflows[0][0]

        def npv(rate: float) -> float:
            return float(
                sum(
                    amount / ((1 + rate) ** ((flow_date - base).days / 365.0))
                    for flow_date, amount in cashflows
                )
            )

        low, high = -0.9999, 10.0
        low_value, high_value = npv(low), npv(high)
        while low_value * high_value > 0 and high < 1_000_000:
            high *= 10
            high_value = npv(high)
        if low_value * high_value > 0:
            return None
        for _ in range(160):
            middle = (low + high) / 2
            value = npv(middle)
            if abs(value) < 0.000001:
                return middle
            if low_value * value <= 0:
                high = middle
            else:
                low, low_value = middle, value
        return (low + high) / 2

    def _benchmark_attribution(
        self,
        connection: sqlite3.Connection,
        portfolio_id: str,
        start: date,
        end: date,
        start_positions: list[JsonDict],
    ) -> tuple[int | None, list[JsonDict], list[str]]:
        rows = connection.execute(
            """
            SELECT c.instrument_id, c.benchmark_instrument_id,
                   i.code, b.code AS benchmark_code
            FROM strategy_assignments a
            JOIN strategy_instrument_configs c ON c.strategy_assignment_id=a.id
            JOIN instruments i ON i.id=c.instrument_id
            LEFT JOIN instruments b ON b.id=c.benchmark_instrument_id
            WHERE a.portfolio_id=? AND a.status='ACTIVE' AND c.status='ACTIVE'
            """,
            (portfolio_id,),
        ).fetchall()
        mappings = {str(row["instrument_id"]): row for row in rows}
        denominator = sum(int(item["value_minor"]) for item in start_positions)
        warnings: list[str] = []
        items: list[JsonDict] = []
        weighted_return = 0.0
        mapped_weight = 0.0
        for position in start_positions:
            instrument_id = str(position["instrument_id"])
            mapping = mappings.get(instrument_id)
            weight = int(position["value_minor"]) / denominator if denominator else 0.0
            if mapping is None or mapping["benchmark_instrument_id"] is None:
                warnings.append(f"BENCHMARK_MISSING:{position['instrument_code']}")
                continue
            benchmark_id = str(mapping["benchmark_instrument_id"])
            nav_start = self._nav(connection, benchmark_id, start)
            nav_end = self._nav(connection, benchmark_id, end)
            actual_start = self._nav(connection, instrument_id, start)
            actual_end = self._nav(connection, instrument_id, end)
            if nav_start is None or nav_end is None:
                warnings.append(f"BENCHMARK_NAV_MISSING:{mapping['benchmark_code']}")
                continue
            if actual_start is None or actual_end is None:
                warnings.append(f"ATTRIBUTION_NAV_MISSING:{position['instrument_code']}")
                continue
            benchmark_return = int(nav_end["nav_micros"]) / int(nav_start["nav_micros"]) - 1
            actual_return = int(actual_end["nav_micros"]) / int(actual_start["nav_micros"]) - 1
            weighted_return += weight * benchmark_return
            mapped_weight += weight
            items.append(
                {
                    "instrument_code": str(position["instrument_code"]),
                    "benchmark_code": str(mapping["benchmark_code"]),
                    "start_weight_bps": round(weight * 10000),
                    "instrument_return_bps": round(actual_return * 10000),
                    "benchmark_return_bps": round(benchmark_return * 10000),
                    "instrument_contribution_bps": round(weight * actual_return * 10000),
                    "benchmark_contribution_bps": round(weight * benchmark_return * 10000),
                    "active_contribution_bps": round(
                        weight * (actual_return - benchmark_return) * 10000
                    ),
                }
            )
        if not items or mapped_weight < 0.9999:
            return None, items, sorted(set(warnings))
        return round(weighted_return * 10000), items, sorted(set(warnings))

    def calculate(
        self,
        *,
        portfolio_id: str,
        period_start: date,
        period_end: date,
        period_type: str = "CUSTOM",
        persist: bool = True,
    ) -> JsonDict:
        if period_end < period_start:
            raise LedgerError("PERFORMANCE_PERIOD_INVALID", "period_end precedes period_start")
        normalized_type = period_type.upper()
        allowed = {"CUSTOM", "MONTHLY", "QUARTERLY", "ANNUAL", "SINCE_INCEPTION"}
        if normalized_type not in allowed:
            raise LedgerError("PERFORMANCE_PERIOD_INVALID", "unsupported period_type")
        with self._connect() as connection:
            portfolio = connection.execute(
                "SELECT * FROM portfolios WHERE id=?", (portfolio_id,)
            ).fetchone()
            if portfolio is None:
                raise LedgerError("PORTFOLIO_NOT_FOUND", "portfolio was not found", http_status=404)
            transactions = self._transactions(connection, portfolio_id, period_end)
            if not transactions:
                raise LedgerError("PERFORMANCE_DATA_UNAVAILABLE", "portfolio has no transactions")
            if normalized_type == "SINCE_INCEPTION":
                period_start = date.fromisoformat(str(transactions[0]["trade_date"]))
            start_value, start_positions, start_warnings = self._value_at(
                connection, transactions, period_start
            )
            end_value, end_positions, end_warnings = self._value_at(
                connection, transactions, period_end
            )
            flows: list[JsonDict] = []
            net_flow = 0
            weighted_flow = 0.0
            total_days = max((period_end - period_start).days, 1)
            for row in transactions:
                flow_date = date.fromisoformat(str(row["trade_date"]))
                if not period_start < flow_date <= period_end:
                    continue
                amount = int(row["amount_minor"]) * (1 if str(row["side"]) == "BUY" else -1)
                net_flow += amount
                weight = (period_end - flow_date).days / total_days
                weighted_flow += amount * weight
                flows.append(
                    {
                        "date": flow_date.isoformat(),
                        "instrument_code": str(row["code"]),
                        "side": str(row["side"]),
                        "amount": _money(amount),
                        "amount_minor": amount,
                        "cash_convention": "EXTERNAL_FLOW",
                    }
                )
            denominator = start_value + weighted_flow
            modified_dietz = (
                (end_value - start_value - net_flow) / denominator if denominator > 0 else None
            )
            cashflows = [(period_start, -start_value)]
            cashflows.extend(
                (date.fromisoformat(str(item["date"])), -int(item["amount_minor"]))
                for item in flows
            )
            cashflows.append((period_end, end_value))
            xirr = self._xirr(cashflows)
            # With no represented cash balance, TWR is only deterministic when no
            # transaction flow occurs inside the period.
            twr = modified_dietz if not flows else None
            benchmark_return, attribution, benchmark_warnings = self._benchmark_attribution(
                connection,
                portfolio_id,
                period_start,
                period_end,
                start_positions,
            )
            modified_dietz_bps = _bps(modified_dietz)
            warnings = sorted(set(start_warnings + end_warnings + benchmark_warnings))
            if flows:
                warnings.append("CASH_LEDGER_ABSENT:BUY_SELL_TREATED_AS_EXTERNAL_FLOWS")
            if twr is None:
                warnings.append("TWR_UNAVAILABLE_WITH_INTRAPERIOD_FLOWS")
            if benchmark_return is None:
                warnings.append("BENCHMARK_RETURN_INCOMPLETE")
            missing_nav = any(value.startswith("NAV_MISSING") for value in warnings)
            quality = "SOURCE_ERROR" if missing_nav else ("WARNING" if warnings else "PASS")
            reason_code = (
                "PERFORMANCE_DATA_BLOCKED"
                if quality == "SOURCE_ERROR"
                else (
                    "PERFORMANCE_CALCULATED_WITH_LIMITS" if warnings else "PERFORMANCE_CALCULATED"
                )
            )
            facts: JsonDict = {
                "portfolio_id": portfolio_id,
                "portfolio_name": str(portfolio["name"]),
                "period_type": normalized_type,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "start_value": _money(start_value),
                "end_value": _money(end_value),
                "net_external_flow": _money(net_flow),
                "modified_dietz_bps": modified_dietz_bps,
                "xirr_bps": _bps(xirr),
                "twr_bps": _bps(twr),
                "benchmark_return_bps": benchmark_return,
                "excess_return_bps": (
                    None
                    if modified_dietz_bps is None or benchmark_return is None
                    else modified_dietz_bps - benchmark_return
                ),
                "start_positions": start_positions,
                "end_positions": end_positions,
                "external_flows": flows,
                "benchmark_attribution": attribution,
                "methodology": {
                    "calculation_version": CALCULATION_VERSION,
                    "cash_ledger": False,
                    "buy_sell_cash_convention": "EXTERNAL_FLOW",
                    "opening_position_treatment": "INITIAL_CAPITAL_OR_EXTERNAL_FLOW",
                    "twr_requires_no_intraperiod_flow": True,
                },
                "warnings": sorted(set(warnings)),
                "data_quality": quality,
                "reason_code": reason_code,
            }
            facts_hash = _hash(facts)
            existing = connection.execute(
                """
                SELECT * FROM performance_snapshots
                WHERE portfolio_id=? AND period_type=? AND period_start=?
                  AND period_end=? AND facts_hash=?
                """,
                (
                    portfolio_id,
                    normalized_type,
                    period_start.isoformat(),
                    period_end.isoformat(),
                    facts_hash,
                ),
            ).fetchone()
            snapshot_id = str(existing["id"]) if existing else str(uuid4())
            if persist and existing is None:
                connection.execute(
                    """
                    INSERT INTO performance_snapshots (
                        id, portfolio_id, period_type, period_start, period_end,
                        start_value_minor, end_value_minor, net_external_flow_minor,
                        modified_dietz_bps, xirr_bps, twr_bps, benchmark_return_bps,
                        excess_return_bps, attribution_json, facts_json, facts_hash,
                        data_quality, reason_code, calculation_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        portfolio_id,
                        normalized_type,
                        period_start.isoformat(),
                        period_end.isoformat(),
                        start_value,
                        end_value,
                        net_flow,
                        modified_dietz_bps,
                        _bps(xirr),
                        _bps(twr),
                        benchmark_return,
                        facts["excess_return_bps"],
                        _json(attribution),
                        _json(facts),
                        facts_hash,
                        quality,
                        reason_code,
                        CALCULATION_VERSION,
                        _iso(self._now()),
                    ),
                )
                connection.commit()
            return {
                "snapshot_id": snapshot_id if persist else None,
                **facts,
                "facts_hash": facts_hash,
                "persisted": persist,
                "idempotent_replay": bool(existing),
            }

    def prepare_review(
        self,
        *,
        portfolio_id: str,
        review_type: str,
        anchor_date: date,
    ) -> JsonDict:
        normalized = review_type.upper()
        start, end = self._period_bounds(normalized, anchor_date)
        performance = self.calculate(
            portfolio_id=portfolio_id,
            period_start=start,
            period_end=end,
            period_type=normalized,
            persist=True,
        )
        action_items: list[JsonDict] = []
        if str(performance["data_quality"]) != "PASS":
            action_items.append(
                {
                    "code": "DATA_QUALITY_REVIEW",
                    "severity": "WARNING",
                    "facts": {"warnings": performance["warnings"]},
                }
            )
        if performance["benchmark_return_bps"] is None:
            action_items.append(
                {
                    "code": "BENCHMARK_COVERAGE_REVIEW",
                    "severity": "WARNING",
                    "facts": {"reason": "BENCHMARK_RETURN_INCOMPLETE"},
                }
            )
        facts: JsonDict = {
            "portfolio_id": portfolio_id,
            "review_type": normalized,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "performance_snapshot_id": performance["snapshot_id"],
            "performance": {
                key: performance[key]
                for key in (
                    "start_value",
                    "end_value",
                    "net_external_flow",
                    "modified_dietz_bps",
                    "xirr_bps",
                    "twr_bps",
                    "benchmark_return_bps",
                    "excess_return_bps",
                    "data_quality",
                    "reason_code",
                )
            },
            "action_items": action_items,
            "automatic_trade": False,
            "review_boundary": "FACTS_AND_ACTION_ITEMS_ONLY",
        }
        facts_hash = _hash(facts)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM periodic_reviews WHERE facts_hash=?", (facts_hash,)
            ).fetchone()
            if existing is not None:
                return self._review_data(connection, existing, idempotent_replay=True)
            revision = (
                int(
                    connection.execute(
                        """
                    SELECT COALESCE(MAX(revision), 0) FROM periodic_reviews
                    WHERE portfolio_id=? AND review_type=? AND period_start=? AND period_end=?
                    """,
                        (portfolio_id, normalized, start.isoformat(), end.isoformat()),
                    ).fetchone()[0]
                )
                + 1
            )
            review_id = str(uuid4())
            quality = str(performance["data_quality"])
            status = "DATA_BLOCKED" if quality == "SOURCE_ERROR" else "FINALIZED"
            reason = (
                "PERIODIC_REVIEW_DATA_BLOCKED"
                if status == "DATA_BLOCKED"
                else "PERIODIC_REVIEW_FINALIZED"
            )
            connection.execute(
                """
                INSERT INTO periodic_reviews (
                    id, portfolio_id, review_type, period_start, period_end,
                    revision, status, performance_snapshot_id, facts_json,
                    facts_hash, data_quality, reason_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    portfolio_id,
                    normalized,
                    start.isoformat(),
                    end.isoformat(),
                    revision,
                    status,
                    performance["snapshot_id"],
                    _json(facts),
                    facts_hash,
                    quality,
                    reason,
                    _iso(self._now()),
                ),
            )
            for item in action_items:
                connection.execute(
                    """
                    INSERT INTO review_action_items (
                        id, review_id, code, severity, status, facts_json, created_at
                    ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?)
                    """,
                    (
                        str(uuid4()),
                        review_id,
                        item["code"],
                        item["severity"],
                        _json(item["facts"]),
                        _iso(self._now()),
                    ),
                )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM periodic_reviews WHERE id=?", (review_id,)
            ).fetchone()
            assert row is not None
            return self._review_data(connection, row, idempotent_replay=False)

    def list_reviews(
        self,
        *,
        portfolio_id: str,
        review_type: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = "SELECT * FROM periodic_reviews WHERE portfolio_id=?"
        params: list[object] = [portfolio_id]
        if review_type:
            query += " AND review_type=?"
            params.append(review_type.upper())
        query += " ORDER BY period_end DESC, revision DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            return [self._review_data(connection, row) for row in rows]

    @staticmethod
    def _review_data(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        idempotent_replay: bool | None = None,
    ) -> JsonDict:
        actions = connection.execute(
            "SELECT * FROM review_action_items WHERE review_id=? ORDER BY severity, code",
            (row["id"],),
        ).fetchall()
        result: JsonDict = {
            "id": str(row["id"]),
            "portfolio_id": str(row["portfolio_id"]),
            "review_type": str(row["review_type"]),
            "period_start": str(row["period_start"]),
            "period_end": str(row["period_end"]),
            "revision": int(row["revision"]),
            "status": str(row["status"]),
            "performance_snapshot_id": str(row["performance_snapshot_id"]),
            "facts": json.loads(str(row["facts_json"])),
            "facts_hash": str(row["facts_hash"]),
            "data_quality": str(row["data_quality"]),
            "reason_code": str(row["reason_code"]),
            "created_at": str(row["created_at"]),
            "action_items": [
                {
                    "id": str(item["id"]),
                    "code": str(item["code"]),
                    "severity": str(item["severity"]),
                    "status": str(item["status"]),
                    "facts": json.loads(str(item["facts_json"])),
                }
                for item in actions
            ],
            "automatic_trade": False,
        }
        if idempotent_replay is not None:
            result["idempotent_replay"] = idempotent_replay
        return result
