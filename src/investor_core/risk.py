"""Deterministic valuation-percentile, risk-rule and sell-proposal lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError
from investor_core.market_data import MarketDataService
from investor_core.strategy import StrategyService

VALUE_SCALE = 1_000_000
CALCULATION_VERSION = "valuation-percentile-v1"
RULE_VERSION = "risk-rules-v1"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _scaled(value: str) -> int:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise LedgerError("INVALID_DECIMAL", "valuation value must be decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise LedgerError("INVALID_DECIMAL", "valuation value must be positive")
    scaled = parsed * VALUE_SCALE
    integral = scaled.to_integral_value(rounding=ROUND_HALF_UP)
    if scaled != integral:
        raise LedgerError(
            "DECIMAL_PRECISION_EXCEEDED",
            "valuation value supports at most 6 decimal places",
        )
    return int(integral)


def _value(value: int) -> str:
    return f"{Decimal(value) / VALUE_SCALE:.6f}"


class RiskService:
    """Keep evidence, rule hits, proposals, decisions and trades strictly separate."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.settings = settings
        self._now = now
        self._market = MarketDataService(settings, now=now)
        self._strategy = StrategyService(settings, now=now)

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

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        actor_ref: str,
        details: JsonDict,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                id, occurred_at, actor_type, actor_ref, action, entity_type,
                entity_id, before_hash, after_hash, details_json, trace_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                str(uuid4()),
                _iso(self._now()),
                "SYSTEM" if actor_ref == "risk-scan" else "USER",
                actor_ref,
                action,
                entity_type,
                entity_id,
                _hash(details),
                _json(details),
                str(uuid4()),
            ),
        )

    def record_valuation_observation(
        self,
        *,
        instrument_code: str,
        metric: str,
        observation_date: str,
        value: str,
        source_type: str,
        source_name: str,
        observed_at: str,
        verification_status: str = "UNVERIFIED",
        source_ref: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        normalized_metric = metric.strip().upper()
        normalized_source = source_type.strip().upper()
        normalized_verification = verification_status.strip().upper()
        if normalized_metric not in {"PE", "PB"}:
            raise LedgerError("INVALID_VALUATION_METRIC", "metric must be PE or PB")
        if normalized_source not in {"OFFICIAL", "PROFESSIONAL", "AGGREGATOR", "USER"}:
            raise LedgerError("INVALID_SOURCE_TYPE", "unsupported valuation source")
        if normalized_verification not in {"VERIFIED", "UNVERIFIED"}:
            raise LedgerError("INVALID_VERIFICATION_STATUS", "unsupported verification status")
        try:
            normalized_date = date.fromisoformat(observation_date).isoformat()
            normalized_observed_at = _iso(_parse_iso(observed_at))
        except ValueError as exc:
            raise LedgerError("INVALID_DATE", "invalid valuation evidence date") from exc
        value_micros = _scaled(value)
        code = instrument_code.strip().upper()
        payload = {
            "instrument_code": code,
            "metric": normalized_metric,
            "observation_date": normalized_date,
            "value_micros": value_micros,
            "source_type": normalized_source,
            "source_name": source_name.strip(),
            "source_ref": source_ref,
            "verification_status": normalized_verification,
            "observed_at": normalized_observed_at,
        }
        record_hash = _hash(payload)
        with self._connect() as connection:
            instrument = connection.execute(
                "SELECT id, asset_type FROM instruments WHERE code = ? AND status = 'ACTIVE'",
                (code,),
            ).fetchone()
            if instrument is None:
                raise LedgerError(
                    "INSTRUMENT_NOT_FOUND",
                    "active valuation instrument was not found",
                    http_status=404,
                )
            if str(instrument["asset_type"]) != "INDEX":
                raise LedgerError(
                    "VALUATION_BENCHMARK_REQUIRED",
                    "PE/PB observations must be recorded against an index benchmark",
                    http_status=409,
                )
            existing = connection.execute(
                "SELECT id FROM valuation_observations WHERE record_hash = ?",
                (record_hash,),
            ).fetchone()
            if existing is not None:
                return {
                    "id": str(existing["id"]),
                    **payload,
                    "value": _value(value_micros),
                    "record_hash": record_hash,
                    "idempotent_replay": True,
                }
            record_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO valuation_observations (
                    id, instrument_id, metric, observation_date, value_micros,
                    source_type, source_name, source_ref, verification_status,
                    observed_at, record_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    instrument["id"],
                    normalized_metric,
                    normalized_date,
                    value_micros,
                    normalized_source,
                    source_name.strip(),
                    source_ref,
                    normalized_verification,
                    normalized_observed_at,
                    record_hash,
                    _iso(self._now()),
                ),
            )
            self._audit(
                connection,
                action="VALUATION_OBSERVATION_RECORDED",
                entity_type="valuation_observation",
                entity_id=record_id,
                actor_ref=actor_ref,
                details=payload,
            )
        return {
            "id": record_id,
            **payload,
            "value": _value(value_micros),
            "record_hash": record_hash,
            "idempotent_replay": False,
        }

    def valuation_snapshot(
        self,
        *,
        portfolio_id: str,
        instrument_code: str,
        metric: str = "PE",
        as_of_date: str | None = None,
        lookback_days: int = 1826,
        lower_percentile_bps: int = 3000,
        upper_percentile_bps: int = 7000,
    ) -> JsonDict:
        if lookback_days < 30:
            raise LedgerError("INVALID_LOOKBACK", "lookback must be at least 30 days")
        normalized_metric = metric.strip().upper()
        if normalized_metric not in {"PE", "PB"}:
            raise LedgerError("INVALID_VALUATION_METRIC", "metric must be PE or PB")
        end = date.fromisoformat(as_of_date) if as_of_date else self._now().date()
        start = end - timedelta(days=lookback_days)
        assignment = self._strategy.get_assignment(portfolio_id=portfolio_id)
        config = next(
            (
                item
                for item in assignment["instruments"]
                if item["instrument_code"] == instrument_code.strip().upper()
            ),
            None,
        )
        if config is None:
            raise LedgerError(
                "STRATEGY_INSTRUMENT_NOT_CONFIGURED",
                "instrument has no strategy-instance configuration",
                http_status=409,
            )
        suitability = str(config["proxy_suitability"])
        if suitability == "NOT_APPLICABLE":
            return {
                "available": False,
                "reason_code": "PROXY_NOT_APPLICABLE",
                "instrument_code": config["instrument_code"],
                "proxy_suitability": suitability,
                "percentile": None,
            }
        benchmark_code = config["benchmark_code"]
        if not benchmark_code:
            raise LedgerError(
                "BENCHMARK_REQUIRED",
                "valuation proxy benchmark is not configured",
                http_status=409,
            )
        with self._connect() as connection:
            benchmark = connection.execute(
                "SELECT id FROM instruments WHERE code = ? AND asset_type = 'INDEX'",
                (benchmark_code,),
            ).fetchone()
            assert benchmark is not None
            rows = connection.execute(
                """
                SELECT * FROM valuation_observations
                WHERE instrument_id = ? AND metric = ?
                  AND observation_date BETWEEN ? AND ?
                ORDER BY observation_date, created_at
                """,
                (benchmark["id"], normalized_metric, start.isoformat(), end.isoformat()),
            ).fetchall()
            if not rows:
                return {
                    "available": False,
                    "reason_code": "VALUATION_HISTORY_MISSING",
                    "instrument_code": config["instrument_code"],
                    "benchmark_code": benchmark_code,
                    "proxy_suitability": suitability,
                    "percentile": None,
                }
            latest = rows[-1]
            current = int(latest["value_micros"])
            below_or_equal = sum(int(row["value_micros"]) <= current for row in rows)
            percentile_bps = int(
                (Decimal(below_or_equal) / Decimal(len(rows)) * Decimal(10000)).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            )
            if percentile_bps <= lower_percentile_bps:
                state = "UNDERVALUED"
            elif percentile_bps >= upper_percentile_bps:
                state = "OVERPRICED"
            else:
                state = "FAIR"
            quality = (
                "PASS"
                if all(
                    str(row["verification_status"]) == "VERIFIED"
                    and str(row["source_type"]) in {"OFFICIAL", "PROFESSIONAL"}
                    for row in rows
                )
                else "WARNING"
            )
            inputs = {
                "observation_hashes": [str(row["record_hash"]) for row in rows],
                "lower_percentile_bps": lower_percentile_bps,
                "upper_percentile_bps": upper_percentile_bps,
                "calculation_version": CALCULATION_VERSION,
            }
            input_hash = _hash(inputs)
            existing = connection.execute(
                """
                SELECT * FROM valuation_snapshots
                WHERE portfolio_id = ? AND instrument_id = ? AND metric = ?
                  AND as_of_date = ? AND input_hash = ?
                """,
                (
                    portfolio_id,
                    config["instrument_id"],
                    normalized_metric,
                    end.isoformat(),
                    input_hash,
                ),
            ).fetchone()
            snapshot_id = str(existing["id"]) if existing is not None else str(uuid4())
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO valuation_snapshots (
                        id, portfolio_id, instrument_id, benchmark_instrument_id,
                        metric, as_of_date, current_value_micros, percentile_bps,
                        sample_count, lookback_start, valuation_state,
                        proxy_suitability, data_quality, input_hash,
                        calculation_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        portfolio_id,
                        config["instrument_id"],
                        benchmark["id"],
                        normalized_metric,
                        end.isoformat(),
                        current,
                        percentile_bps,
                        len(rows),
                        start.isoformat(),
                        state,
                        suitability,
                        quality,
                        input_hash,
                        CALCULATION_VERSION,
                        _iso(self._now()),
                    ),
                )
        return {
            "available": True,
            "reason_code": "VALUATION_PERCENTILE_CALCULATED",
            "id": snapshot_id,
            "instrument_code": config["instrument_code"],
            "benchmark_code": benchmark_code,
            "metric": normalized_metric,
            "as_of_date": end.isoformat(),
            "current_value": _value(current),
            "percentile": f"{Decimal(percentile_bps) / 100:.2f}",
            "percentile_bps": percentile_bps,
            "sample_count": len(rows),
            "valuation_state": state,
            "proxy_suitability": suitability,
            "data_quality": quality,
            "input_hash": input_hash,
            "calculation_version": CALCULATION_VERSION,
            "sell_trigger_allowed": suitability == "STRONG" and quality == "PASS",
        }

    def scan(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        as_of_date: str | None = None,
    ) -> JsonDict:
        """Evaluate only explicitly configured deterministic rules."""
        valuation = self._market.portfolio_valuation(
            portfolio_id=portfolio_id,
            account_id=account_id,
            as_of_date_value=as_of_date,
        )
        assignment = self._strategy.get_assignment(portfolio_id=portfolio_id)
        configs = {str(item["instrument_code"]): item for item in assignment["instruments"]}
        hits: list[JsonDict] = []
        proposals: list[JsonDict] = []
        if valuation["totals"] is None:
            hit = self._record_hit(
                portfolio_id=portfolio_id,
                instrument_id=None,
                rule_code="DATA_QUALITY_BLOCK",
                severity="CRITICAL",
                status="DATA_BLOCKED",
                inputs={
                    "as_of_date": valuation["as_of_date"],
                    "data_quality": valuation["data_quality"],
                    "warnings": valuation["warnings"],
                },
                output={"amount_conclusions_allowed": False},
            )
            return {
                "state": "DATA_BLOCKED",
                "reason_code": "DATA_QUALITY_BLOCK",
                "rule_hits": [hit],
                "sell_proposals": [],
                "data_quality": "SOURCE_ERROR",
            }
        allocation = self._market.portfolio_brief(
            portfolio_id=portfolio_id,
            account_id=account_id,
            as_of_date_value=as_of_date,
        )["allocation_assessment"]
        for position in valuation["positions"]:
            holding = position["holding"]
            code = str(holding["instrument_code"])
            config = configs.get(code)
            if config is None:
                continue
            evaluations: list[tuple[str, bool, str, str, JsonDict]] = []
            thesis = str(config["thesis_status"])
            evaluations.append(
                (
                    "THESIS_REVIEW_REQUIRED",
                    thesis == "REVIEW_REQUIRED",
                    "WARNING",
                    "HIT" if thesis == "REVIEW_REQUIRED" else "NOT_HIT",
                    {"thesis_status": thesis},
                )
            )
            evaluations.append(
                (
                    "SELL_02_THESIS_INVALID",
                    thesis == "INVALID",
                    "CRITICAL",
                    "HIT" if thesis == "INVALID" else "NOT_HIT",
                    {"thesis_status": thesis},
                )
            )
            hard_stop = config["hard_stop_return_bps"]
            return_bps = (
                int(
                    (Decimal(str(position["return_pct"])) * Decimal(100)).to_integral_value(
                        rounding=ROUND_HALF_UP
                    )
                )
                if position["return_pct"] is not None
                else None
            )
            evaluations.append(
                (
                    "SELL_01_HARD_STOP",
                    hard_stop is not None
                    and return_bps is not None
                    and return_bps <= int(hard_stop),
                    "HIGH",
                    (
                        "HIT"
                        if hard_stop is not None
                        and return_bps is not None
                        and return_bps <= int(hard_stop)
                        else "NOT_HIT"
                    ),
                    {
                        "configured_threshold_bps": hard_stop,
                        "actual_return_bps": return_bps,
                    },
                )
            )
            cap = config["maximum_position_weight_bps"]
            weight_bps = (
                int(
                    (Decimal(str(position["weight_pct"])) * Decimal(100)).to_integral_value(
                        rounding=ROUND_HALF_UP
                    )
                )
                if position["weight_pct"] is not None
                else None
            )
            cap_hit = cap is not None and weight_bps is not None and weight_bps > int(cap)
            rebalance_status = (
                "EXEMPT"
                if cap_hit and allocation["state"] == "TRANSITION_REQUIRED"
                else ("HIT" if cap_hit else "NOT_HIT")
            )
            evaluations.append(
                (
                    "SELL_03_REBALANCE",
                    cap_hit and rebalance_status == "HIT",
                    "HIGH",
                    rebalance_status,
                    {
                        "configured_cap_bps": cap,
                        "actual_weight_bps": weight_bps,
                        "allocation_state": allocation["state"],
                    },
                )
            )
            for rule_code, triggered, severity, status, facts in evaluations:
                hit = self._record_hit(
                    portfolio_id=portfolio_id,
                    instrument_id=str(config["instrument_id"]),
                    rule_code=rule_code,
                    severity=severity,
                    status=status,
                    inputs={
                        "instrument_code": code,
                        "holding": holding,
                        "valuation": {
                            "return_pct": position["return_pct"],
                            "weight_pct": position["weight_pct"],
                            "data_quality": position["data_quality"],
                        },
                        **facts,
                    },
                    output={
                        "triggered": triggered,
                        "automatic_trade": False,
                        "requires_human_review": triggered,
                    },
                )
                hits.append(hit)
                if triggered and rule_code.startswith("SELL_"):
                    proposals.append(
                        self._proposal_for_hit(
                            portfolio_id=portfolio_id,
                            assignment=assignment,
                            config=config,
                            position=position,
                            hit=hit,
                        )
                    )
        return {
            "state": "REVIEW_REQUIRED" if proposals else "PASS",
            "reason_code": (
                "SELL_PROPOSALS_CREATED" if proposals else "NO_CONFIGURED_SELL_RULE_HIT"
            ),
            "rule_hits": hits,
            "sell_proposals": proposals,
            "data_quality": valuation["data_quality"],
            "execution_status": "NOT_EXECUTED",
        }

    def _record_hit(
        self,
        *,
        portfolio_id: str,
        instrument_id: str | None,
        rule_code: str,
        severity: str,
        status: str,
        inputs: JsonDict,
        output: JsonDict,
    ) -> JsonDict:
        input_hash = _hash(inputs)
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM rule_hits
                WHERE portfolio_id = ? AND instrument_id IS ?
                  AND rule_code = ? AND input_hash = ?
                """,
                (portfolio_id, instrument_id, rule_code, input_hash),
            ).fetchone()
            if existing is not None:
                return self._hit_data(existing)
            hit_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO rule_hits (
                    id, portfolio_id, instrument_id, rule_code, rule_version,
                    severity, status, input_json, output_json, input_hash,
                    evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hit_id,
                    portfolio_id,
                    instrument_id,
                    rule_code,
                    RULE_VERSION,
                    severity,
                    status,
                    _json(inputs),
                    _json(output),
                    input_hash,
                    _iso(self._now()),
                ),
            )
            row = connection.execute(
                "SELECT * FROM rule_hits WHERE id = ?",
                (hit_id,),
            ).fetchone()
            assert row is not None
            return self._hit_data(row)

    @staticmethod
    def _hit_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "instrument_id": row["instrument_id"],
            "rule_code": str(row["rule_code"]),
            "rule_version": str(row["rule_version"]),
            "severity": str(row["severity"]),
            "status": str(row["status"]),
            "inputs": json.loads(str(row["input_json"])),
            "output": json.loads(str(row["output_json"])),
            "input_hash": str(row["input_hash"]),
            "evaluated_at": str(row["evaluated_at"]),
        }

    def _proposal_for_hit(
        self,
        *,
        portfolio_id: str,
        assignment: JsonDict,
        config: JsonDict,
        position: JsonDict,
        hit: JsonDict,
    ) -> JsonDict:
        trigger = str(hit["rule_code"])
        action = "FULL_SELL" if trigger == "SELL_02_THESIS_INVALID" else "MANUAL_REVIEW"
        facts = {
            "instrument_code": config["instrument_code"],
            "instrument_name": config["instrument_name"],
            "role": config["role"],
            "rule_hit": hit,
            "position": position,
            "proxy_suitability": config["proxy_suitability"],
            "automatic_selling_allowed": False,
            "execution_status": "NOT_EXECUTED",
        }
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT p.*, i.code AS instrument_code, i.name AS instrument_name
                FROM sell_proposals p
                JOIN instruments i ON i.id = p.instrument_id
                WHERE p.portfolio_id = ? AND p.instrument_id = ?
                  AND p.trigger_code = ? AND p.trigger_input_hash = ?
                """,
                (
                    portfolio_id,
                    config["instrument_id"],
                    trigger,
                    hit["input_hash"],
                ),
            ).fetchone()
            if existing is not None:
                return self._proposal_data(connection, existing)
            proposal_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO sell_proposals (
                    id, portfolio_id, instrument_id, strategy_version_id,
                    rule_hit_id, trigger_code, engine, recommended_action,
                    recommended_fraction_bps, target_weight_bps, status,
                    trigger_facts_json, trigger_input_hash, proposed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'RISK', ?, ?, ?, 'REVIEW_REQUIRED',
                          ?, ?, ?)
                """,
                (
                    proposal_id,
                    portfolio_id,
                    config["instrument_id"],
                    assignment["strategy"]["id"]
                    if "id" in assignment["strategy"]
                    else self._strategy_version_id(connection, assignment["id"]),
                    hit["id"],
                    trigger,
                    action,
                    10000 if action == "FULL_SELL" else None,
                    config["target_weight_bps"] if trigger == "SELL_03_REBALANCE" else None,
                    _json(facts),
                    hit["input_hash"],
                    _iso(self._now()),
                ),
            )
            before = {
                "market_value": position["market_value"],
                "weight_pct": position["weight_pct"],
                "role": config["role"],
            }
            connection.execute(
                """
                INSERT INTO sell_diagnostics (
                    id, sell_proposal_id, diagnosis_version, checklist_json,
                    portfolio_before_json, portfolio_after_json,
                    followup_metric_json, result, created_at
                ) VALUES (?, ?, 'sell-diagnosis-v1', ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    proposal_id,
                    _json(
                        {
                            "rule_evidence_present": True,
                            "fees_estimated": False,
                            "fund_destination_configured": False,
                            "automatic_trade": False,
                        }
                    ),
                    _json(before),
                    _json(
                        {
                            "status": "NOT_CALCULATED",
                            "reason_code": "EXECUTION_AMOUNT_NOT_APPROVED",
                        }
                    ),
                    _json({"review_after_months": 6}),
                    "WARNING",
                    _iso(self._now()),
                ),
            )
            self._audit(
                connection,
                action="SELL_PROPOSAL_CREATED",
                entity_type="sell_proposal",
                entity_id=proposal_id,
                actor_ref="risk-scan",
                details=facts,
            )
            row = connection.execute(
                """
                SELECT p.*, i.code AS instrument_code, i.name AS instrument_name
                FROM sell_proposals p
                JOIN instruments i ON i.id = p.instrument_id
                WHERE p.id = ?
                """,
                (proposal_id,),
            ).fetchone()
            assert row is not None
            return self._proposal_data(connection, row)

    @staticmethod
    def _strategy_version_id(connection: sqlite3.Connection, assignment_id: str) -> str:
        row = connection.execute(
            "SELECT strategy_version_id FROM strategy_assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
        assert row is not None
        return str(row["strategy_version_id"])

    def list_proposals(
        self,
        *,
        portfolio_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = """
            SELECT p.*, i.code AS instrument_code, i.name AS instrument_name
            FROM sell_proposals p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE p.portfolio_id = ?
        """
        params: list[object] = [portfolio_id]
        if status:
            query += " AND p.status = ?"
            params.append(status.strip().upper())
        query += " ORDER BY p.proposed_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            return [self._proposal_data(connection, row) for row in rows]

    def get_proposal(self, *, proposal_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT p.*, i.code AS instrument_code, i.name AS instrument_name
                FROM sell_proposals p
                JOIN instruments i ON i.id = p.instrument_id
                WHERE p.id = ?
                """,
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "SELL_PROPOSAL_NOT_FOUND",
                    "sell proposal was not found",
                    http_status=404,
                )
            return self._proposal_data(connection, row)

    @staticmethod
    def _proposal_data(connection: sqlite3.Connection, row: sqlite3.Row) -> JsonDict:
        diagnostic = connection.execute(
            "SELECT * FROM sell_diagnostics WHERE sell_proposal_id = ?",
            (row["id"],),
        ).fetchone()
        return {
            "id": str(row["id"]),
            "portfolio_id": str(row["portfolio_id"]),
            "instrument_code": str(row["instrument_code"]),
            "instrument_name": str(row["instrument_name"]),
            "trigger_code": str(row["trigger_code"]),
            "engine": str(row["engine"]),
            "recommended_action": str(row["recommended_action"]),
            "recommended_fraction_bps": row["recommended_fraction_bps"],
            "target_weight_bps": row["target_weight_bps"],
            "status": str(row["status"]),
            "trigger_facts": json.loads(str(row["trigger_facts_json"])),
            "trigger_input_hash": str(row["trigger_input_hash"]),
            "proposed_at": str(row["proposed_at"]),
            "diagnostic": (
                {
                    "diagnosis_version": str(diagnostic["diagnosis_version"]),
                    "checklist": json.loads(str(diagnostic["checklist_json"])),
                    "portfolio_before": json.loads(str(diagnostic["portfolio_before_json"])),
                    "portfolio_after": json.loads(str(diagnostic["portfolio_after_json"])),
                    "followup_metric": json.loads(str(diagnostic["followup_metric_json"])),
                    "result": str(diagnostic["result"]),
                }
                if diagnostic is not None
                else None
            ),
            "execution_status": "NOT_EXECUTED",
            "holdings_changed": False,
        }

    def create_decision_draft(
        self,
        *,
        proposal_id: str,
        decision: str,
        user_reason: str | None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        proposal = self.get_proposal(proposal_id=proposal_id)
        normalized = decision.strip().upper()
        if normalized not in {"APPROVE", "DEFER", "REJECT"}:
            raise LedgerError("INVALID_SELL_DECISION", "unsupported sell decision")
        if proposal["status"] != "REVIEW_REQUIRED":
            raise LedgerError(
                "INVALID_SELL_PROPOSAL_STATUS",
                "sell proposal is not awaiting review",
                http_status=409,
            )
        token = secrets.token_urlsafe(24)
        draft_id = str(uuid4())
        now = self._now()
        expires_at = now + timedelta(minutes=self.settings.confirmation_ttl_minutes)
        proposal_hash = _hash(proposal)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sell_decision_drafts (
                    id, sell_proposal_id, decision, user_reason, proposal_hash,
                    confirmation_digest, status, created_by, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    draft_id,
                    proposal_id,
                    normalized,
                    user_reason,
                    proposal_hash,
                    _token_digest(token),
                    actor_ref,
                    _iso(now),
                    _iso(expires_at),
                ),
            )
        return {
            "draft": {
                "id": draft_id,
                "status": "PENDING",
                "decision": normalized,
                "user_reason": user_reason,
                "proposal": proposal,
                "proposal_hash": proposal_hash,
                "expires_at": _iso(expires_at),
                "execution_status": "NOT_EXECUTED",
            },
            "confirmation_token": token,
        }

    def commit_decision(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sell_decision_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "SELL_DECISION_DRAFT_NOT_FOUND",
                    "sell decision draft was not found",
                    http_status=404,
                )
            if str(row["status"]) == "COMMITTED":
                decision = connection.execute(
                    "SELECT * FROM sell_decisions WHERE decision_draft_id = ?",
                    (draft_id,),
                ).fetchone()
                assert decision is not None
                connection.commit()
                return self._decision_result(connection, row, decision, True)
            if str(row["status"]) != "PENDING":
                raise LedgerError(
                    "INVALID_SELL_DECISION_DRAFT_STATUS",
                    "sell decision draft is not pending",
                    http_status=409,
                )
            if _parse_iso(str(row["expires_at"])) <= self._now():
                connection.execute(
                    "UPDATE sell_decision_drafts SET status = 'EXPIRED' WHERE id = ?",
                    (draft_id,),
                )
                raise LedgerError(
                    "CONFIRMATION_EXPIRED",
                    "sell decision confirmation has expired",
                    http_status=409,
                )
            if not hmac.compare_digest(
                str(row["confirmation_digest"]),
                _token_digest(confirmation_token),
            ):
                raise LedgerError(
                    "CONFIRMATION_MISMATCH",
                    "confirmation token does not match this decision draft",
                    http_status=409,
                )
            proposal = self.get_proposal(proposal_id=str(row["sell_proposal_id"]))
            if _hash(proposal) != str(row["proposal_hash"]):
                raise LedgerError(
                    "SELL_PROPOSAL_CHANGED",
                    "sell proposal changed after the decision preview",
                    http_status=409,
                )
            timestamp = _iso(self._now())
            decision_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO sell_decisions (
                    id, sell_proposal_id, decision, user_reason,
                    decision_draft_id, decided_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    row["sell_proposal_id"],
                    row["decision"],
                    row["user_reason"],
                    draft_id,
                    timestamp,
                    confirmed_by.strip(),
                ),
            )
            status = {
                "APPROVE": "APPROVED",
                "DEFER": "DEFERRED",
                "REJECT": "REJECTED",
            }[str(row["decision"])]
            connection.execute(
                "UPDATE sell_proposals SET status = ?, closed_at = ? WHERE id = ?",
                (
                    status,
                    timestamp if status in {"DEFERRED", "REJECTED"} else None,
                    row["sell_proposal_id"],
                ),
            )
            connection.execute(
                """
                UPDATE sell_decision_drafts
                SET status = 'COMMITTED', committed_at = ?, committed_by = ?
                WHERE id = ?
                """,
                (timestamp, confirmed_by.strip(), draft_id),
            )
            decision = connection.execute(
                "SELECT * FROM sell_decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
            assert decision is not None
            self._audit(
                connection,
                action="SELL_DECISION_COMMITTED",
                entity_type="sell_decision",
                entity_id=decision_id,
                actor_ref=confirmed_by.strip(),
                details={
                    "sell_proposal_id": row["sell_proposal_id"],
                    "decision": row["decision"],
                    "execution_status": "NOT_EXECUTED",
                },
            )
            connection.commit()
            return self._decision_result(connection, row, decision, False)
        finally:
            connection.close()

    def _decision_result(
        self,
        connection: sqlite3.Connection,
        draft: sqlite3.Row,
        decision: sqlite3.Row,
        replay: bool,
    ) -> JsonDict:
        proposal = self.get_proposal(proposal_id=str(draft["sell_proposal_id"]))
        return {
            "decision": {
                "id": str(decision["id"]),
                "sell_proposal_id": str(decision["sell_proposal_id"]),
                "decision": str(decision["decision"]),
                "user_reason": decision["user_reason"],
                "decided_at": str(decision["decided_at"]),
                "created_by": str(decision["created_by"]),
            },
            "proposal": proposal,
            "execution_status": "NOT_EXECUTED",
            "holdings_changed": False,
            "transaction_created": False,
            "idempotent_replay": replay,
        }
