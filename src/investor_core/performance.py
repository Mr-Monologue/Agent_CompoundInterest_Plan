"""Deterministic portfolio performance, benchmark attribution and periodic reviews."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from calendar import monthrange
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError, utc_now

CALCULATION_VERSION = "performance-v2"
REVIEW_TREND_VERSION = "review-trend-v1"
REVIEW_QUALITY_VERSION = "review-quality-v1"


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
    def _cash_events(
        connection: sqlite3.Connection,
        portfolio_id: str,
        end: date,
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                SELECT * FROM cash_ledger_events
                WHERE portfolio_id=? AND event_date<=?
                ORDER BY event_date, committed_at, id
                """,
                (portfolio_id, end.isoformat()),
            ).fetchall()
        )

    @staticmethod
    def _cash_balance_at(
        transactions: list[sqlite3.Row],
        cash_events: list[sqlite3.Row],
        at: date,
    ) -> int:
        balance = sum(
            int(row["signed_amount_minor"])
            for row in cash_events
            if str(row["event_date"]) <= at.isoformat()
        )
        for row in transactions:
            if str(row["trade_date"]) > at.isoformat() or str(row["kind"]) != "TRADE":
                continue
            balance += (
                -int(row["amount_minor"])
                if str(row["side"]) == "BUY"
                else int(row["amount_minor"])
            )
        return balance

    def _total_value_at(
        self,
        connection: sqlite3.Connection,
        transactions: list[sqlite3.Row],
        cash_events: list[sqlite3.Row],
        at: date,
    ) -> tuple[int, list[JsonDict], list[str], int]:
        invested, positions, warnings = self._value_at(connection, transactions, at)
        cash = self._cash_balance_at(transactions, cash_events, at)
        if cash < 0:
            warnings.append(f"CASH_LEDGER_NEGATIVE_BALANCE:{at.isoformat()}:{cash}")
        return invested + cash, positions, sorted(set(warnings)), cash

    def _daily_linked_twr(
        self,
        connection: sqlite3.Connection,
        transactions: list[sqlite3.Row],
        cash_events: list[sqlite3.Row],
        start: date,
        end: date,
    ) -> tuple[float | None, list[JsonDict], list[str]]:
        previous_value, _positions, warnings, previous_cash = self._total_value_at(
            connection, transactions, cash_events, start
        )
        if previous_value <= 0 or previous_cash < 0:
            return None, [], sorted(set([*warnings, "TWR_START_VALUE_UNAVAILABLE"]))
        linked = 1.0
        checkpoints: list[JsonDict] = []
        current = start + timedelta(days=1)
        while current <= end:
            current_value, _positions, day_warnings, current_cash = self._total_value_at(
                connection, transactions, cash_events, current
            )
            warnings.extend(day_warnings)
            external_flow = sum(
                int(row["signed_amount_minor"])
                for row in cash_events
                if bool(row["is_external_flow"]) and str(row["event_date"]) == current.isoformat()
            )
            if (
                previous_value <= 0
                or current_value < 0
                or current_cash < 0
                or any(item.startswith("NAV_MISSING") for item in day_warnings)
            ):
                return None, checkpoints, sorted(
                    set([*warnings, "TWR_DAILY_CHAIN_INCOMPLETE"])
                )
            daily_return = (current_value - external_flow) / previous_value - 1
            linked *= 1 + daily_return
            checkpoints.append(
                {
                    "date": current.isoformat(),
                    "opening_value": _money(previous_value),
                    "closing_value": _money(current_value),
                    "external_flow": _money(external_flow),
                    "return_bps": round(daily_return * 10000),
                }
            )
            previous_value = current_value
            current += timedelta(days=1)
        return linked - 1, checkpoints, sorted(set(warnings))

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
            cash_events = self._cash_events(connection, portfolio_id, period_end)
            cash_ledger_active = bool(cash_events)
            if cash_ledger_active:
                start_value, start_positions, start_warnings, start_cash = self._total_value_at(
                    connection, transactions, cash_events, period_start
                )
                end_value, end_positions, end_warnings, end_cash = self._total_value_at(
                    connection, transactions, cash_events, period_end
                )
            else:
                start_value, start_positions, start_warnings = self._value_at(
                    connection, transactions, period_start
                )
                end_value, end_positions, end_warnings = self._value_at(
                    connection, transactions, period_end
                )
                start_cash = end_cash = 0
            flows: list[JsonDict] = []
            net_flow = 0
            weighted_flow = 0.0
            total_days = max((period_end - period_start).days, 1)
            if cash_ledger_active:
                for row in cash_events:
                    if not bool(row["is_external_flow"]):
                        continue
                    flow_date = date.fromisoformat(str(row["event_date"]))
                    if not period_start < flow_date <= period_end:
                        continue
                    amount = int(row["signed_amount_minor"])
                    net_flow += amount
                    weight = (period_end - flow_date).days / total_days
                    weighted_flow += amount * weight
                    flows.append(
                        {
                            "date": flow_date.isoformat(),
                            "event_type": str(row["event_type"]),
                            "amount": _money(amount),
                            "amount_minor": amount,
                            "cash_convention": "CONFIRMED_EXTERNAL_CASH_FLOW",
                        }
                    )
            else:
                for row in transactions:
                    flow_date = date.fromisoformat(str(row["trade_date"]))
                    if not period_start < flow_date <= period_end:
                        continue
                    amount = int(row["amount_minor"]) * (
                        1 if str(row["side"]) == "BUY" else -1
                    )
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
                            "cash_convention": "LEGACY_EXTERNAL_FLOW",
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
            twr_checkpoints: list[JsonDict] = []
            twr_warnings: list[str] = []
            if cash_ledger_active:
                twr, twr_checkpoints, twr_warnings = self._daily_linked_twr(
                    connection,
                    transactions,
                    cash_events,
                    period_start,
                    period_end,
                )
            else:
                twr = modified_dietz if not flows else None
            benchmark_return, attribution, benchmark_warnings = self._benchmark_attribution(
                connection,
                portfolio_id,
                period_start,
                period_end,
                start_positions,
            )
            modified_dietz_bps = _bps(modified_dietz)
            warnings = sorted(
                set(start_warnings + end_warnings + benchmark_warnings + twr_warnings)
            )
            if not cash_ledger_active and flows:
                warnings.append("CASH_LEDGER_ABSENT:BUY_SELL_TREATED_AS_EXTERNAL_FLOWS")
            if not cash_ledger_active and twr is None:
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
                "start_cash": _money(start_cash),
                "end_cash": _money(end_cash),
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
                "twr_checkpoints": twr_checkpoints,
                "benchmark_attribution": attribution,
                "methodology": {
                    "calculation_version": CALCULATION_VERSION,
                    "cash_ledger": cash_ledger_active,
                    "buy_sell_cash_convention": (
                        "INTERNAL_CASH_MOVEMENT"
                        if cash_ledger_active
                        else "LEGACY_EXTERNAL_FLOW"
                    ),
                    "opening_position_treatment": "INITIAL_CAPITAL_OR_EXTERNAL_FLOW",
                    "twr_method": (
                        "DAILY_LINKED_EXTERNAL_FLOW_ADJUSTED"
                        if cash_ledger_active
                        else "NO_INTRAPERIOD_FLOW_ONLY"
                    ),
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
        with self._connect() as connection:
            assignment = connection.execute(
                """
                SELECT * FROM strategy_assignments
                WHERE portfolio_id=? AND status='ACTIVE'
                ORDER BY approved_at DESC LIMIT 1
                """,
                (portfolio_id,),
            ).fetchone()
            configs: list[sqlite3.Row] = []
            if assignment is not None:
                configs = list(
                    connection.execute(
                        """
                        SELECT c.*, i.code
                        FROM strategy_instrument_configs c
                        JOIN instruments i ON i.id=c.instrument_id
                        WHERE c.strategy_assignment_id=? AND c.status='ACTIVE'
                        ORDER BY i.code
                        """,
                        (assignment["id"],),
                    ).fetchall()
                )
            latest_discovery = connection.execute(
                """
                SELECT * FROM market_discovery_runs
                WHERE portfolio_id=? AND as_of_date BETWEEN ? AND ?
                ORDER BY as_of_date DESC, created_at DESC LIMIT 1
                """,
                (portfolio_id, start.isoformat(), end.isoformat()),
            ).fetchone()
        governance: JsonDict = {
            "strategy_assignment_present": assignment is not None,
            "configured_instrument_count": len(configs),
            "contribution_eligible_count": sum(
                bool(item["contribution_eligible"]) for item in configs
            ),
            "benchmark_mapped_count": sum(
                item["benchmark_instrument_id"] is not None for item in configs
            ),
            "benchmark_missing_codes": [
                str(item["code"])
                for item in configs
                if str(item["role"]) in {"CORE", "SATELLITE"}
                and item["benchmark_instrument_id"] is None
            ],
            "thesis_review_codes": [
                str(item["code"])
                for item in configs
                if str(item["thesis_status"]) != "ACTIVE"
            ],
            "latest_discovery": (
                None
                if latest_discovery is None
                else {
                    "id": str(latest_discovery["id"]),
                    "as_of_date": str(latest_discovery["as_of_date"]),
                    "status": str(latest_discovery["status"]),
                    "data_quality": str(latest_discovery["data_quality"]),
                    "reason_code": str(latest_discovery["reason_code"]),
                    "summary": json.loads(str(latest_discovery["facts_json"]))["summary"],
                    "change_summary": json.loads(
                        str(latest_discovery["facts_json"])
                    ).get("change_summary"),
                }
            ),
        }
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
        if assignment is None:
            action_items.append(
                {
                    "code": "STRATEGY_INSTANCE_REVIEW",
                    "severity": "HIGH",
                    "facts": {"reason": "ACTIVE_STRATEGY_INSTANCE_MISSING"},
                }
            )
        elif not governance["contribution_eligible_count"]:
            action_items.append(
                {
                    "code": "CONTRIBUTION_UNIVERSE_REVIEW",
                    "severity": "WARNING",
                    "facts": {"reason": "NO_CONTRIBUTION_ELIGIBLE_INSTRUMENT"},
                }
            )
        if governance["benchmark_missing_codes"]:
            action_items.append(
                {
                    "code": "INSTRUMENT_BENCHMARK_REVIEW",
                    "severity": "WARNING",
                    "facts": {"instrument_codes": governance["benchmark_missing_codes"]},
                }
            )
        if governance["thesis_review_codes"]:
            action_items.append(
                {
                    "code": "INVESTMENT_THESIS_REVIEW",
                    "severity": "HIGH",
                    "facts": {"instrument_codes": governance["thesis_review_codes"]},
                }
            )
        if not bool(performance["methodology"]["cash_ledger"]):
            action_items.append(
                {
                    "code": "CASH_LEDGER_REVIEW",
                    "severity": "WARNING",
                    "facts": {"reason": "LEGACY_EXTERNAL_FLOW_CONVENTION"},
                }
            )
        if governance["latest_discovery"] is None:
            action_items.append(
                {
                    "code": "MARKET_DISCOVERY_REVIEW",
                    "severity": "INFO",
                    "facts": {"reason": "NO_DISCOVERY_FACT_PACKAGE_FOR_PERIOD"},
                }
            )
        elif str(governance["latest_discovery"]["status"]) == "DATA_BLOCKED":
            action_items.append(
                {
                    "code": "MARKET_DISCOVERY_DATA_REVIEW",
                    "severity": "WARNING",
                    "facts": governance["latest_discovery"],
                }
            )
        elif (
            governance["latest_discovery"]["change_summary"] is not None
            and int(
                governance["latest_discovery"]["change_summary"]["attention_count"]
            )
            > 0
        ):
            action_items.append(
                {
                    "code": "MARKET_DISCOVERY_CHANGE_REVIEW",
                    "severity": "INFO",
                    "facts": governance["latest_discovery"],
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
            "position_facts": {
                "end_positions": performance["end_positions"],
                "benchmark_attribution": performance["benchmark_attribution"],
            },
            "governance": governance,
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

    def build_review_trend(
        self,
        *,
        portfolio_id: str,
        as_of_date: date,
        review_type: str = "ALL",
        lookback_reviews: int = 12,
    ) -> JsonDict:
        normalized = review_type.strip().upper()
        if normalized not in {"ALL", "MONTHLY", "QUARTERLY", "ANNUAL"}:
            raise LedgerError(
                "REVIEW_TREND_TYPE_INVALID",
                "review_type must be ALL, MONTHLY, QUARTERLY or ANNUAL",
            )
        if not 1 <= lookback_reviews <= 120:
            raise LedgerError(
                "REVIEW_TREND_LOOKBACK_INVALID",
                "lookback_reviews must be between 1 and 120",
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
            query = """
                SELECT * FROM (
                    SELECT r.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY review_type, period_start, period_end
                               ORDER BY revision DESC
                           ) AS latest_revision
                    FROM periodic_reviews r
                    WHERE portfolio_id=? AND period_end<=?
            """
            params: list[object] = [portfolio_id, as_of_date.isoformat()]
            if normalized != "ALL":
                query += " AND review_type=?"
                params.append(normalized)
            query += """
                ) ranked
                WHERE latest_revision=1
                ORDER BY period_end DESC
                LIMIT ?
            """
            params.append(lookback_reviews)
            review_rows = list(connection.execute(query, params).fetchall())
            review_rows.reverse()
            review_ids = [str(row["id"]) for row in review_rows]
            action_rows: list[sqlite3.Row] = []
            outcome_rows: list[sqlite3.Row] = []
            if review_ids:
                placeholders = ",".join("?" for _ in review_ids)
                action_rows = list(
                    connection.execute(
                        f"""
                        SELECT a.*, r.period_end, r.review_type
                        FROM review_action_items a
                        JOIN periodic_reviews r ON r.id=a.review_id
                        WHERE a.review_id IN ({placeholders})
                        ORDER BY r.period_end, a.code
                        """,
                        tuple(review_ids),
                    ).fetchall()
                )
                outcome_rows = list(
                    connection.execute(
                        f"""
                        SELECT o.*, a.code, a.review_id, a.created_at AS action_created_at,
                               d.confirmed_at AS resolved_at
                        FROM review_action_outcomes o
                        JOIN review_action_items a ON a.id=o.action_item_id
                        LEFT JOIN review_action_decisions d
                          ON d.action_item_id=a.id AND d.new_status='RESOLVED'
                        WHERE a.review_id IN ({placeholders})
                        ORDER BY o.confirmed_at, a.code
                        """,
                        tuple(review_ids),
                    ).fetchall()
                )

            quality_counts = {"PASS": 0, "WARNING": 0, "SOURCE_ERROR": 0}
            review_series: list[JsonDict] = []
            governance_series: list[JsonDict] = []
            for row in review_rows:
                quality = str(row["data_quality"])
                quality_counts[quality] += 1
                review_facts = json.loads(str(row["facts_json"]))
                performance = review_facts["performance"]
                governance = review_facts.get("governance", {})
                review_series.append(
                    {
                        "review_id": str(row["id"]),
                        "review_type": str(row["review_type"]),
                        "period_start": str(row["period_start"]),
                        "period_end": str(row["period_end"]),
                        "revision": int(row["revision"]),
                        "status": str(row["status"]),
                        "data_quality": quality,
                        "modified_dietz_bps": performance.get("modified_dietz_bps"),
                        "xirr_bps": performance.get("xirr_bps"),
                        "twr_bps": performance.get("twr_bps"),
                        "benchmark_return_bps": performance.get(
                            "benchmark_return_bps"
                        ),
                        "excess_return_bps": performance.get("excess_return_bps"),
                    }
                )
                governance_series.append(
                    {
                        "review_id": str(row["id"]),
                        "period_end": str(row["period_end"]),
                        "configured_instrument_count": governance.get(
                            "configured_instrument_count"
                        ),
                        "contribution_eligible_count": governance.get(
                            "contribution_eligible_count"
                        ),
                        "benchmark_mapped_count": governance.get(
                            "benchmark_mapped_count"
                        ),
                        "benchmark_missing_count": len(
                            governance.get("benchmark_missing_codes", [])
                        ),
                        "thesis_review_count": len(
                            governance.get("thesis_review_codes", [])
                        ),
                        "discovery_status": (
                            None
                            if governance.get("latest_discovery") is None
                            else governance["latest_discovery"].get("status")
                        ),
                    }
                )

            status_counts = {"OPEN": 0, "ACKNOWLEDGED": 0, "RESOLVED": 0}
            severity_counts = {"INFO": 0, "WARNING": 0, "HIGH": 0}
            code_counts: dict[str, int] = {}
            unresolved: list[JsonDict] = []
            for row in action_rows:
                status = str(row["status"])
                severity = str(row["severity"])
                code = str(row["code"])
                status_counts[status] += 1
                severity_counts[severity] += 1
                code_counts[code] = code_counts.get(code, 0) + 1
                if status != "RESOLVED":
                    created = datetime.fromisoformat(
                        str(row["created_at"]).replace("Z", "+00:00")
                    ).date()
                    unresolved.append(
                        {
                            "action_item_id": str(row["id"]),
                            "review_id": str(row["review_id"]),
                            "review_type": str(row["review_type"]),
                            "period_end": str(row["period_end"]),
                            "code": code,
                            "severity": severity,
                            "status": status,
                            "age_days": max(0, (as_of_date - created).days),
                        }
                    )
            recurring_codes = [
                {"code": code, "review_count": count}
                for code, count in sorted(code_counts.items())
                if count >= 2
            ]
            unresolved.sort(
                key=lambda item: (
                    -int(item["age_days"]),
                    str(item["severity"]),
                    str(item["code"]),
                )
            )
            outcome_counts = {
                "COMPLETED": 0,
                "PARTIAL": 0,
                "NOT_COMPLETED": 0,
                "NOT_APPLICABLE": 0,
            }
            outcome_quality_counts = {
                "VERIFIED": 0,
                "USER_REPORTED": 0,
                "UNVERIFIED": 0,
            }
            outcome_items: list[JsonDict] = []
            resolution_days: list[int] = []
            for row in outcome_rows:
                outcome_counts[str(row["outcome"])] += 1
                outcome_quality_counts[str(row["evidence_quality"])] += 1
                resolved_at = row["resolved_at"]
                days_to_resolution = None
                if resolved_at is not None:
                    created = datetime.fromisoformat(
                        str(row["action_created_at"]).replace("Z", "+00:00")
                    )
                    resolved = datetime.fromisoformat(
                        str(resolved_at).replace("Z", "+00:00")
                    )
                    days_to_resolution = max(0, (resolved - created).days)
                    resolution_days.append(days_to_resolution)
                outcome_items.append(
                    {
                        "outcome_id": str(row["id"]),
                        "action_item_id": str(row["action_item_id"]),
                        "review_id": str(row["review_id"]),
                        "code": str(row["code"]),
                        "outcome": str(row["outcome"]),
                        "evidence_quality": str(row["evidence_quality"]),
                        "evidence_ref": row["evidence_ref"],
                        "days_to_resolution": days_to_resolution,
                        "confirmed_at": str(row["confirmed_at"]),
                    }
                )
            resolved_count = status_counts["RESOLVED"]
            if not review_rows:
                quality = "SOURCE_ERROR"
                status = "DATA_BLOCKED"
                reason = "REVIEW_TREND_NO_PERIODIC_REVIEWS"
            elif quality_counts["SOURCE_ERROR"]:
                quality = "SOURCE_ERROR"
                status = "COMPLETED"
                reason = "REVIEW_TREND_COMPLETED_WITH_SOURCE_ERRORS"
            elif quality_counts["WARNING"]:
                quality = "WARNING"
                status = "COMPLETED"
                reason = "REVIEW_TREND_COMPLETED_WITH_LIMITS"
            else:
                quality = "PASS"
                status = "COMPLETED"
                reason = "REVIEW_TREND_COMPLETED"
            facts: JsonDict = {
                "portfolio_id": portfolio_id,
                "as_of_date": as_of_date.isoformat(),
                "review_type": normalized,
                "lookback_reviews": lookback_reviews,
                "review_count": len(review_rows),
                "review_series": review_series,
                "quality_summary": quality_counts,
                "governance_series": governance_series,
                "action_summary": {
                    "total_count": len(action_rows),
                    "status_counts": status_counts,
                    "severity_counts": severity_counts,
                    "recurring_codes": recurring_codes,
                    "unresolved_count": len(unresolved),
                    "oldest_unresolved_age_days": (
                        None if not unresolved else unresolved[0]["age_days"]
                    ),
                    "unresolved_items": unresolved,
                    "outcome_summary": {
                        "resolved_count": resolved_count,
                        "recorded_outcome_count": len(outcome_rows),
                        "missing_outcome_count": max(
                            0, resolved_count - len(outcome_rows)
                        ),
                        "outcome_coverage_bps": (
                            None
                            if resolved_count == 0
                            else round(len(outcome_rows) / resolved_count * 10000)
                        ),
                        "outcome_counts": outcome_counts,
                        "evidence_quality_counts": outcome_quality_counts,
                        "average_resolution_days": (
                            None
                            if not resolution_days
                            else round(sum(resolution_days) / len(resolution_days), 2)
                        ),
                        "items": outcome_items,
                        "evaluation_boundary": (
                            "DESCRIPTIVE_OUTCOME_FACTS_NOT_A_STRATEGY_SCORE"
                        ),
                    },
                },
                "data_quality": quality,
                "status": status,
                "reason_code": reason,
                "calculation_version": REVIEW_TREND_VERSION,
                "trend_boundary": "DESCRIPTIVE_FACTS_NOT_INVESTMENT_ADVICE",
                "automatic_trade": False,
                "strategy_changed": False,
            }
            facts_hash = _hash(facts)
            existing = connection.execute(
                "SELECT * FROM review_trend_snapshots WHERE facts_hash=?",
                (facts_hash,),
            ).fetchone()
            if existing is not None:
                return self._review_trend_data(existing, idempotent_replay=True)
            snapshot_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO review_trend_snapshots (
                    id, portfolio_id, as_of_date, review_type,
                    lookback_reviews, review_count, status, data_quality,
                    reason_code, facts_json, facts_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    portfolio_id,
                    as_of_date.isoformat(),
                    normalized,
                    lookback_reviews,
                    len(review_rows),
                    status,
                    quality,
                    reason,
                    _json(facts),
                    facts_hash,
                    _iso(self._now()),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM review_trend_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            assert row is not None
            return self._review_trend_data(row, idempotent_replay=False)

    def list_review_trends(
        self,
        *,
        portfolio_id: str,
        limit: int = 100,
    ) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_trend_snapshots
                WHERE portfolio_id=?
                ORDER BY as_of_date DESC, created_at DESC LIMIT ?
                """,
                (portfolio_id, limit),
            ).fetchall()
            return [self._review_trend_data(row) for row in rows]

    def build_review_quality_snapshot(
        self,
        *,
        portfolio_id: str,
        as_of_date: date,
        lookback_reviews: int = 12,
    ) -> JsonDict:
        """Describe review-process quality without scoring strategy effectiveness."""
        trend = self.build_review_trend(
            portfolio_id=portfolio_id,
            as_of_date=as_of_date,
            review_type="ALL",
            lookback_reviews=lookback_reviews,
        )
        quality_summary = dict(trend["quality_summary"])
        action_summary = dict(trend["action_summary"])
        outcome_summary = dict(action_summary["outcome_summary"])
        review_series = list(trend["review_series"])
        with self._connect() as connection:
            collection_rows = connection.execute(
                """
                SELECT connector_key, source_lineage, execution_status,
                       item_count, recorded_count, replayed_count, rejected_count,
                       finished_at
                FROM research_collection_runs
                WHERE portfolio_id=? AND finished_at<=?
                ORDER BY finished_at
                """,
                (portfolio_id, f"{as_of_date.isoformat()}T23:59:59Z"),
            ).fetchall()
            task_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM research_collection_tasks
                WHERE portfolio_id=? AND created_at<=?
                GROUP BY status
                """,
                (portfolio_id, f"{as_of_date.isoformat()}T23:59:59Z"),
            ).fetchall()
            claim_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM research_collection_claims
                WHERE portfolio_id=? AND claimed_at<=?
                GROUP BY status
                """,
                (portfolio_id, f"{as_of_date.isoformat()}T23:59:59Z"),
            ).fetchall()
            receipt_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM research_collection_task_receipts r
                    JOIN research_collection_claims c ON c.id=r.claim_id
                    WHERE c.portfolio_id=? AND r.completed_at<=?
                    """,
                    (portfolio_id, f"{as_of_date.isoformat()}T23:59:59Z"),
                ).fetchone()[0]
            )
            health_rows = connection.execute(
                """
                SELECT h.* FROM research_connector_health_receipts h
                WHERE h.portfolio_id=? AND h.observed_at<=?
                  AND h.id=(
                    SELECT h2.id FROM research_connector_health_receipts h2
                    WHERE h2.portfolio_id=h.portfolio_id
                      AND h2.connector_key=h.connector_key
                      AND h2.observed_at<=?
                    ORDER BY h2.observed_at DESC, h2.created_at DESC LIMIT 1
                  )
                ORDER BY h.connector_key
                """,
                (
                    portfolio_id,
                    f"{as_of_date.isoformat()}T23:59:59Z",
                    f"{as_of_date.isoformat()}T23:59:59Z",
                ),
            ).fetchall()
            coverage_change_rows = connection.execute(
                """
                SELECT change_kind, COUNT(*) AS count
                FROM research_coverage_changes
                WHERE portfolio_id=? AND created_at<=?
                GROUP BY change_kind
                """,
                (portfolio_id, f"{as_of_date.isoformat()}T23:59:59Z"),
            ).fetchall()
            assignment_rows = connection.execute(
                """
                SELECT a.id, a.instance_config_hash, a.status, a.approved_at,
                       a.retired_at, d.strategy_key, v.version
                FROM strategy_assignments a
                JOIN strategy_versions v ON v.id=a.strategy_version_id
                JOIN strategy_definitions d ON d.id=v.strategy_definition_id
                WHERE a.portfolio_id=? AND a.approved_at<=?
                ORDER BY a.approved_at
                """,
                (portfolio_id, f"{as_of_date.isoformat()}T23:59:59Z"),
            ).fetchall()

            assignment_contexts: list[JsonDict] = []
            for assignment in assignment_rows:
                approved_date = datetime.fromisoformat(
                    str(assignment["approved_at"]).replace("Z", "+00:00")
                ).date()
                retired_date = (
                    None
                    if assignment["retired_at"] is None
                    else datetime.fromisoformat(
                        str(assignment["retired_at"]).replace("Z", "+00:00")
                    ).date()
                )
                associated_reviews = [
                    item
                    for item in review_series
                    if date.fromisoformat(str(item["period_start"])) >= approved_date
                    and (
                        retired_date is None
                        or date.fromisoformat(str(item["period_end"])) <= retired_date
                    )
                ]
                assignment_contexts.append(
                    {
                        "strategy_assignment_id": str(assignment["id"]),
                        "strategy_key": str(assignment["strategy_key"]),
                        "strategy_version": str(assignment["version"]),
                        "instance_config_hash": str(assignment["instance_config_hash"]),
                        "status": str(assignment["status"]),
                        "approved_at": str(assignment["approved_at"]),
                        "retired_at": assignment["retired_at"],
                        "calendar_contained_review_count": len(associated_reviews),
                        "review_observations": associated_reviews,
                    }
                )

        resolved_count = int(outcome_summary["resolved_count"])
        missing_outcome_count = int(outcome_summary["missing_outcome_count"])
        task_status_counts = {str(row["status"]): int(row["count"]) for row in task_rows}
        claim_status_counts = {
            str(row["status"]): int(row["count"]) for row in claim_rows
        }
        connector_health_counts: dict[str, int] = {}
        for row in health_rows:
            state = str(row["state"])
            connector_health_counts[state] = connector_health_counts.get(state, 0) + 1
        exhausted_task_count = task_status_counts.get("EXHAUSTED", 0)
        unhealthy_connector_count = sum(
            connector_health_counts.get(state, 0)
            for state in ("DEGRADED", "UNAVAILABLE")
        )
        if not review_series:
            status = "DATA_BLOCKED"
            data_quality = "SOURCE_ERROR"
            reason_code = "REVIEW_QUALITY_NO_PERIODIC_REVIEWS"
        elif quality_summary["SOURCE_ERROR"]:
            status = "DATA_BLOCKED"
            data_quality = "SOURCE_ERROR"
            reason_code = "REVIEW_QUALITY_SOURCE_BLOCKED"
        elif (
            quality_summary["WARNING"]
            or missing_outcome_count
            or exhausted_task_count
            or unhealthy_connector_count
        ):
            status = "PARTIAL"
            data_quality = "WARNING"
            reason_code = "REVIEW_QUALITY_PARTIAL"
        else:
            status = "COMPLETE"
            data_quality = "PASS"
            reason_code = "REVIEW_QUALITY_COMPLETE"

        connector_keys = sorted({str(row["connector_key"]) for row in collection_rows})
        source_lineages = sorted({str(row["source_lineage"]) for row in collection_rows})
        successful_collection_count = sum(
            str(row["execution_status"]) == "SUCCESS" for row in collection_rows
        )
        rejected_item_count = sum(int(row["rejected_count"]) for row in collection_rows)
        comparable_assignment_count = sum(
            int(item["calendar_contained_review_count"]) > 0
            for item in assignment_contexts
        )
        facts: JsonDict = {
            "portfolio_id": portfolio_id,
            "as_of_date": as_of_date.isoformat(),
            "lookback_reviews": lookback_reviews,
            "status": status,
            "data_quality": data_quality,
            "reason_code": reason_code,
            "review_history": {
                "review_count": len(review_series),
                "review_types": sorted(
                    {str(item["review_type"]) for item in review_series}
                ),
                "period_start": (
                    None if not review_series else review_series[0]["period_start"]
                ),
                "period_end": (
                    None if not review_series else review_series[-1]["period_end"]
                ),
                "quality_counts": quality_summary,
                "continuity_status": (
                    "MISSING"
                    if not review_series
                    else (
                        "SOURCE_BLOCKED"
                        if quality_summary["SOURCE_ERROR"]
                        else (
                            "LIMITED"
                            if quality_summary["WARNING"]
                            else "COMPLETE"
                        )
                    )
                ),
            },
            "action_closure": {
                "total_count": int(action_summary["total_count"]),
                "status_counts": action_summary["status_counts"],
                "unresolved_count": int(action_summary["unresolved_count"]),
                "oldest_unresolved_age_days": action_summary[
                    "oldest_unresolved_age_days"
                ],
                "resolved_count": resolved_count,
                "recorded_outcome_count": int(
                    outcome_summary["recorded_outcome_count"]
                ),
                "missing_outcome_count": missing_outcome_count,
                "outcome_coverage_bps": outcome_summary["outcome_coverage_bps"],
                "outcome_evidence_quality_counts": outcome_summary[
                    "evidence_quality_counts"
                ],
                "closure_quality_status": (
                    "NOT_APPLICABLE"
                    if resolved_count == 0
                    else (
                        "COMPLETE"
                        if missing_outcome_count == 0
                        else (
                            "PARTIAL"
                            if int(outcome_summary["recorded_outcome_count"]) > 0
                            else "MISSING"
                        )
                    )
                ),
            },
            "research_traceability": {
                "collection_run_count": len(collection_rows),
                "successful_run_count": successful_collection_count,
                "partial_or_failed_run_count": (
                    len(collection_rows) - successful_collection_count
                ),
                "rejected_item_count": rejected_item_count,
                "connector_keys": connector_keys,
                "source_lineages": source_lineages,
                "latest_finished_at": (
                    None
                    if not collection_rows
                    else str(collection_rows[-1]["finished_at"])
                ),
                "traceability_status": (
                    "NOT_AVAILABLE"
                    if not collection_rows
                    else ("PARTIAL" if rejected_item_count else "RECORDED")
                ),
            },
            "research_collection_orchestration": {
                "task_status_counts": task_status_counts,
                "claim_status_counts": claim_status_counts,
                "completed_task_count": task_status_counts.get("COMPLETED", 0),
                "exhausted_task_count": exhausted_task_count,
                "receipt_count": receipt_count,
                "orchestration_status": (
                    "DATA_BLOCKED"
                    if exhausted_task_count
                    else (
                        "IN_PROGRESS"
                        if task_status_counts.get("PENDING", 0)
                        or task_status_counts.get("CLAIMED", 0)
                        else "SETTLED"
                    )
                ),
                "boundary": "TASK_EXECUTION_FACTS_NOT_RESEARCH_QUALITY_OR_INVESTMENT_SIGNAL",
            },
            "research_connector_runtime": {
                "latest_state_counts": connector_health_counts,
                "unhealthy_connector_count": unhealthy_connector_count,
                "connectors": [
                    {
                        "connector_key": str(row["connector_key"]),
                        "adapter_version": str(row["adapter_version"]),
                        "observed_at": str(row["observed_at"]),
                        "state": str(row["state"]),
                        "reason_code": str(row["reason_code"]),
                    }
                    for row in health_rows
                ],
                "boundary": "RUNTIME_HEALTH_NOT_SOURCE_INDEPENDENCE_OR_FACT_CORRECTNESS",
            },
            "research_coverage_changes": {
                "counts": {
                    str(row["change_kind"]): int(row["count"])
                    for row in coverage_change_rows
                },
                "change_count": sum(int(row["count"]) for row in coverage_change_rows),
                "boundary": "COVERAGE_DELTA_NOT_CAUSAL_EFFECT_OR_INVESTMENT_SIGNAL",
            },
            "strategy_contexts": assignment_contexts,
            "strategy_parameter_observation": {
                "assignment_count": len(assignment_contexts),
                "comparable_assignment_count": comparable_assignment_count,
                "status": (
                    "OBSERVATIONAL_ONLY"
                    if comparable_assignment_count >= 2
                    else "INSUFFICIENT_HISTORY"
                ),
                "boundary": (
                    "TEMPORAL_ASSOCIATION_ONLY_NOT_CAUSAL_EFFECT_OR_PARAMETER_ADVICE"
                ),
            },
            "calculation_version": REVIEW_QUALITY_VERSION,
            "quality_boundary": (
                "REVIEW_PROCESS_FACTS_NOT_STRATEGY_SCORE_INVESTMENT_ADVICE_OR_AUTO_TUNING"
            ),
            "strategy_changed": False,
            "transactions_created": False,
            "automatic_trade": False,
        }
        facts_hash = _hash(facts)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM review_quality_snapshots WHERE facts_hash=?",
                (facts_hash,),
            ).fetchone()
            if existing is not None:
                return self._review_quality_data(existing, idempotent_replay=True)
            snapshot_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO review_quality_snapshots (
                    id, portfolio_id, as_of_date, lookback_reviews, status,
                    data_quality, reason_code, facts_json, facts_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    portfolio_id,
                    as_of_date.isoformat(),
                    lookback_reviews,
                    status,
                    data_quality,
                    reason_code,
                    _json(facts),
                    facts_hash,
                    _iso(self._now()),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM review_quality_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            assert row is not None
            return self._review_quality_data(row)

    def list_review_quality_snapshots(
        self,
        *,
        portfolio_id: str,
        limit: int = 100,
    ) -> list[JsonDict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_quality_snapshots
                WHERE portfolio_id=?
                ORDER BY as_of_date DESC, created_at DESC LIMIT ?
                """,
                (portfolio_id, limit),
            ).fetchall()
            return [self._review_quality_data(row) for row in rows]

    @staticmethod
    def _review_quality_data(
        row: sqlite3.Row,
        *,
        idempotent_replay: bool = False,
    ) -> JsonDict:
        return {
            "id": str(row["id"]),
            **json.loads(str(row["facts_json"])),
            "facts_hash": str(row["facts_hash"]),
            "created_at": str(row["created_at"]),
            "idempotent_replay": idempotent_replay,
        }

    @staticmethod
    def _review_trend_data(
        row: sqlite3.Row,
        *,
        idempotent_replay: bool = False,
    ) -> JsonDict:
        return {
            "id": str(row["id"]),
            **json.loads(str(row["facts_json"])),
            "facts_hash": str(row["facts_hash"]),
            "created_at": str(row["created_at"]),
            "idempotent_replay": idempotent_replay,
        }

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
