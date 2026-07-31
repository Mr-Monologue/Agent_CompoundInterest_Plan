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
RULE_VERSION = "risk-rules-v2"
RULE_EVALUATED_STATUSES = {"HIT", "EVALUATED_NOT_HIT", "EXEMPT"}
LIFECYCLE_OBSERVATION_TYPES = {
    "RELATIVE_PERFORMANCE",
    "REPLACEMENT_CANDIDATE",
    "OBJECTIVE_STATUS",
    "TOOL_QUALITY",
    "REDEMPTION_TERMS",
    "EXPOSURE_PROFILE",
}


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


def _rule_status(
    *,
    configured: bool,
    data_available: bool,
    triggered: bool,
    applicable: bool = True,
    exempt: bool = False,
) -> str:
    """Classify rule execution without treating missing configuration as a safe result."""
    if not applicable:
        return "NOT_APPLICABLE"
    if not configured:
        return "NOT_CONFIGURED"
    if not data_available:
        return "DATA_UNAVAILABLE"
    if exempt:
        return "EXEMPT"
    return "HIT" if triggered else "EVALUATED_NOT_HIT"


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

    def record_lifecycle_observation(
        self,
        *,
        instrument_code: str,
        observation_type: str,
        observation_date: str,
        facts: JsonDict,
        source_type: str,
        source_name: str,
        observed_at: str,
        verification_status: str = "UNVERIFIED",
        source_ref: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        normalized_type = observation_type.strip().upper()
        normalized_source = source_type.strip().upper()
        normalized_verification = verification_status.strip().upper()
        if normalized_type not in LIFECYCLE_OBSERVATION_TYPES:
            raise LedgerError("INVALID_OBSERVATION_TYPE", "unsupported lifecycle observation")
        if normalized_source not in {
            "OFFICIAL",
            "PROFESSIONAL",
            "AGGREGATOR",
            "PLATFORM",
            "USER",
        }:
            raise LedgerError("INVALID_SOURCE_TYPE", "unsupported lifecycle source")
        if normalized_verification not in {"VERIFIED", "UNVERIFIED"}:
            raise LedgerError("INVALID_VERIFICATION_STATUS", "unsupported verification status")
        try:
            normalized_date = date.fromisoformat(observation_date).isoformat()
            normalized_observed_at = _iso(_parse_iso(observed_at))
        except ValueError as exc:
            raise LedgerError("INVALID_DATE", "invalid lifecycle evidence date") from exc
        self._validate_lifecycle_facts(normalized_type, facts)
        code = instrument_code.strip().upper()
        payload = {
            "instrument_code": code,
            "observation_type": normalized_type,
            "observation_date": normalized_date,
            "facts": facts,
            "source_type": normalized_source,
            "source_name": source_name.strip(),
            "source_ref": source_ref,
            "verification_status": normalized_verification,
            "observed_at": normalized_observed_at,
        }
        record_hash = _hash(payload)
        with self._connect() as connection:
            instrument = connection.execute(
                "SELECT id FROM instruments WHERE code = ? AND status = 'ACTIVE'",
                (code,),
            ).fetchone()
            if instrument is None:
                raise LedgerError(
                    "INSTRUMENT_NOT_FOUND",
                    "active instrument was not found",
                    http_status=404,
                )
            existing = connection.execute(
                "SELECT id FROM instrument_lifecycle_observations WHERE record_hash = ?",
                (record_hash,),
            ).fetchone()
            if existing is not None:
                return {
                    "id": str(existing["id"]),
                    **payload,
                    "record_hash": record_hash,
                    "idempotent_replay": True,
                }
            observation_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO instrument_lifecycle_observations (
                    id, instrument_id, observation_type, observation_date, facts_json,
                    source_type, source_name, source_ref, verification_status,
                    observed_at, record_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    instrument["id"],
                    normalized_type,
                    normalized_date,
                    _json(facts),
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
                action="LIFECYCLE_OBSERVATION_RECORDED",
                entity_type="instrument_lifecycle_observation",
                entity_id=observation_id,
                actor_ref=actor_ref,
                details=payload,
            )
        return {
            "id": observation_id,
            **payload,
            "record_hash": record_hash,
            "idempotent_replay": False,
        }

    @staticmethod
    def _validate_lifecycle_facts(observation_type: str, facts: JsonDict) -> None:
        required = {
            "RELATIVE_PERFORMANCE": ("relative_return_bps", "window_days"),
            "REPLACEMENT_CANDIDATE": (
                "candidate_code",
                "score_delta_bps",
                "consecutive_periods",
            ),
            "OBJECTIVE_STATUS": ("status",),
            "TOOL_QUALITY": (),
            "REDEMPTION_TERMS": ("fee_bps",),
            "EXPOSURE_PROFILE": ("profile",),
        }[observation_type]
        missing = [key for key in required if key not in facts]
        if missing:
            raise LedgerError(
                "INVALID_OBSERVATION_FACTS",
                f"missing lifecycle facts: {', '.join(missing)}",
            )
        numeric_keys = {
            "RELATIVE_PERFORMANCE": ("relative_return_bps", "window_days"),
            "REPLACEMENT_CANDIDATE": ("score_delta_bps", "consecutive_periods"),
            "OBJECTIVE_STATUS": (),
            "TOOL_QUALITY": ("tracking_error_bps", "expense_ratio_bps"),
            "REDEMPTION_TERMS": ("fee_bps",),
            "EXPOSURE_PROFILE": (),
        }[observation_type]
        try:
            for key in numeric_keys:
                if key in facts:
                    int(facts[key])
        except (TypeError, ValueError) as exc:
            raise LedgerError(
                "INVALID_OBSERVATION_FACTS",
                "lifecycle numeric facts must be integers",
            ) from exc
        if observation_type == "OBJECTIVE_STATUS" and str(facts["status"]).upper() not in {
            "ACTIVE",
            "ACHIEVED",
            "FAILED",
        }:
            raise LedgerError("INVALID_OBSERVATION_FACTS", "unsupported objective status")
        if observation_type == "TOOL_QUALITY" and not {
            "tracking_error_bps",
            "expense_ratio_bps",
        }.intersection(facts):
            raise LedgerError(
                "INVALID_OBSERVATION_FACTS",
                "tool quality requires tracking error or expense ratio",
            )

    def list_lifecycle_observations(
        self,
        *,
        instrument_code: str | None = None,
        observation_type: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = """
            SELECT o.*, i.code AS instrument_code
            FROM instrument_lifecycle_observations o
            JOIN instruments i ON i.id = o.instrument_id
            WHERE 1 = 1
        """
        params: list[object] = []
        if instrument_code:
            query += " AND i.code = ?"
            params.append(instrument_code.strip().upper())
        if observation_type:
            query += " AND o.observation_type = ?"
            params.append(observation_type.strip().upper())
        query += " ORDER BY o.observation_date DESC, o.observed_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "id": str(row["id"]),
                "instrument_code": str(row["instrument_code"]),
                "observation_type": str(row["observation_type"]),
                "observation_date": str(row["observation_date"]),
                "facts": json.loads(str(row["facts_json"])),
                "source_type": str(row["source_type"]),
                "source_name": str(row["source_name"]),
                "source_ref": row["source_ref"],
                "verification_status": str(row["verification_status"]),
                "observed_at": str(row["observed_at"]),
                "record_hash": str(row["record_hash"]),
            }
            for row in rows
        ]

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
        liquidity_amount: str | None = None,
        liquidity_destination: str | None = None,
        include_rule_hits: bool = False,
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
        instrument_summaries: list[JsonDict] = []
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
                "evaluation_summary": {
                    "candidate_rule_count": 1,
                    "configured_rule_count": 1,
                    "evaluable_rule_count": 0,
                    "evaluated_rule_count": 0,
                    "not_configured_count": 0,
                    "data_unavailable_count": 1,
                    "not_applicable_count": 0,
                    "exempt_count": 0,
                    "triggered_rule_count": 0,
                    "sell_proposal_count": 0,
                },
                "instrument_summaries": [],
                "rule_hits_included": include_rule_hits,
                "rule_hits": [hit] if include_rule_hits else [],
                "sell_proposals": [],
                "data_quality": "SOURCE_ERROR",
                "execution_status": "NOT_EXECUTED",
            }
        allocation = self._market.portfolio_brief(
            portfolio_id=portfolio_id,
            account_id=account_id,
            as_of_date_value=as_of_date,
        )["allocation_assessment"]
        liquidity_allocations: dict[str, int] = {}
        if liquidity_amount is not None:
            requested_minor = int(
                (Decimal(liquidity_amount) * Decimal(100)).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            )
            if requested_minor <= 0 or not liquidity_destination:
                raise LedgerError(
                    "INVALID_LIQUIDITY_REQUEST",
                    "liquidity amount must be positive and include a destination",
                )
            candidates = sorted(
                valuation["positions"],
                key=lambda item: int(
                    configs.get(
                        str(item["holding"]["instrument_code"]), {}
                    ).get("lifecycle_rules", {}).get("liquidity_priority", 2_147_483_647)
                ),
            )
            remaining = requested_minor
            for item in candidates:
                code = str(item["holding"]["instrument_code"])
                config = configs.get(code)
                if config is None or "liquidity_priority" not in config["lifecycle_rules"]:
                    continue
                available = int(
                    (Decimal(str(item["market_value"])) * Decimal(100)).to_integral_value(
                        rounding=ROUND_HALF_UP
                    )
                )
                amount = min(remaining, available)
                if amount > 0:
                    liquidity_allocations[code] = amount
                    remaining -= amount
                if remaining == 0:
                    break
        for position in valuation["positions"]:
            holding = position["holding"]
            code = str(holding["instrument_code"])
            config = configs.get(code)
            if config is None:
                continue
            position_hits: list[JsonDict] = []
            evaluations: list[tuple[str, bool, str, str, JsonDict]] = []
            thesis = str(config["thesis_status"])
            thesis_review_hit = thesis == "REVIEW_REQUIRED"
            evaluations.append(
                (
                    "THESIS_REVIEW_REQUIRED",
                    thesis_review_hit,
                    "WARNING",
                    _rule_status(
                        configured=True,
                        data_available=True,
                        triggered=thesis_review_hit,
                    ),
                    {"thesis_status": thesis},
                )
            )
            return_bps = (
                int(
                    (Decimal(str(position["return_pct"])) * Decimal(100)).to_integral_value(
                        rounding=ROUND_HALF_UP
                    )
                )
                if position["return_pct"] is not None
                else None
            )
            rules = config["lifecycle_rules"]
            replacement = self._latest_lifecycle_observation(
                str(config["instrument_id"]), "REPLACEMENT_CANDIDATE"
            )
            replacement_facts = replacement["facts"] if replacement else {}
            replacement_configured = (
                "replacement_min_score_delta_bps" in rules
                and "replacement_min_consecutive_periods" in rules
            )
            replacement_hit = (
                replacement is not None
                and replacement_configured
                and int(replacement_facts["score_delta_bps"])
                >= int(rules["replacement_min_score_delta_bps"])
                and int(replacement_facts["consecutive_periods"])
                >= int(rules["replacement_min_consecutive_periods"])
            )
            evaluations.append(
                (
                    "SELL_04_REPLACE",
                    replacement_hit,
                    "HIGH",
                    _rule_status(
                        configured=replacement_configured,
                        data_available=replacement is not None,
                        triggered=replacement_hit,
                    ),
                    {
                        "configured_rules": {
                            key: rules.get(key)
                            for key in (
                                "replacement_min_score_delta_bps",
                                "replacement_min_consecutive_periods",
                            )
                        },
                        "observation": replacement,
                    },
                )
            )
            relative = self._latest_lifecycle_observation(
                str(config["instrument_id"]), "RELATIVE_PERFORMANCE"
            )
            relative_facts = relative["facts"] if relative else {}
            relative_configured = (
                "underperformance_threshold_bps" in rules
                and "underperformance_min_days" in rules
            )
            underperformance_hit = (
                relative is not None
                and relative_configured
                and int(relative_facts["relative_return_bps"])
                <= int(rules["underperformance_threshold_bps"])
                and int(relative_facts["window_days"])
                >= int(rules["underperformance_min_days"])
            )
            evaluations.append(
                (
                    "SELL_05_UNDERPERFORMANCE",
                    underperformance_hit,
                    "HIGH",
                    _rule_status(
                        configured=relative_configured,
                        data_available=relative is not None,
                        triggered=underperformance_hit,
                    ),
                    {
                        "configured_rules": {
                            key: rules.get(key)
                            for key in (
                                "underperformance_threshold_bps",
                                "underperformance_min_days",
                            )
                        },
                        "observation": relative,
                    },
                )
            )
            holding_days = (
                date.fromisoformat(valuation["as_of_date"])
                - date.fromisoformat(str(holding["as_of"]))
            ).days
            take_profit_configured = (
                "take_profit_return_bps" in rules
                and "take_profit_min_holding_days" in rules
            )
            take_profit_hit = (
                return_bps is not None
                and take_profit_configured
                and return_bps >= int(rules["take_profit_return_bps"])
                and holding_days >= int(rules["take_profit_min_holding_days"])
            )
            evaluations.append(
                (
                    "SELL_06_TAKE_PROFIT",
                    take_profit_hit,
                    "WARNING",
                    _rule_status(
                        configured=take_profit_configured,
                        data_available=return_bps is not None,
                        triggered=take_profit_hit,
                    ),
                    {
                        "configured_return_bps": rules.get("take_profit_return_bps"),
                        "configured_min_holding_days": rules.get(
                            "take_profit_min_holding_days"
                        ),
                        "configured_fraction_bps": rules.get("take_profit_fraction_bps"),
                        "actual_return_bps": return_bps,
                        "holding_days": holding_days,
                    },
                )
            )
            objective = self._latest_lifecycle_observation(
                str(config["instrument_id"]), "OBJECTIVE_STATUS"
            )
            objective_configured = "objective_sell_fraction_bps" in rules
            objective_hit = (
                objective is not None
                and str(objective["facts"]["status"]).upper() == "ACHIEVED"
                and objective_configured
            )
            evaluations.append(
                (
                    "SELL_07_OBJECTIVE_COMPLETE",
                    objective_hit,
                    "WARNING",
                    _rule_status(
                        configured=objective_configured,
                        data_available=objective is not None,
                        triggered=objective_hit,
                    ),
                    {
                        "configured_fraction_bps": rules.get(
                            "objective_sell_fraction_bps"
                        ),
                        "observation": objective,
                    },
                )
            )
            tool_quality = self._latest_lifecycle_observation(
                str(config["instrument_id"]), "TOOL_QUALITY"
            )
            tool_facts = tool_quality["facts"] if tool_quality else {}
            tool_quality_configured = any(
                key in rules
                for key in ("max_tracking_error_bps", "max_expense_ratio_bps")
            )
            tool_quality_hit = tool_quality is not None and (
                (
                    "max_tracking_error_bps" in rules
                    and "tracking_error_bps" in tool_facts
                    and int(tool_facts["tracking_error_bps"])
                    > int(rules["max_tracking_error_bps"])
                )
                or (
                    "max_expense_ratio_bps" in rules
                    and "expense_ratio_bps" in tool_facts
                    and int(tool_facts["expense_ratio_bps"])
                    > int(rules["max_expense_ratio_bps"])
                )
            )
            evaluations.append(
                (
                    "CORE_TOOL_QUALITY",
                    tool_quality_hit,
                    "HIGH",
                    _rule_status(
                        configured=tool_quality_configured,
                        data_available=tool_quality is not None,
                        triggered=tool_quality_hit,
                    ),
                    {"configured_rules": rules, "observation": tool_quality},
                )
            )
            liquidity_minor = liquidity_allocations.get(code)
            liquidity_applicable = liquidity_amount is not None
            liquidity_configured = "liquidity_priority" in rules
            liquidity_hit = liquidity_minor is not None
            evaluations.append(
                (
                    "SELL_08_LIQUIDITY",
                    liquidity_hit,
                    "WARNING",
                    _rule_status(
                        configured=liquidity_configured,
                        data_available=True,
                        triggered=liquidity_hit,
                        applicable=liquidity_applicable,
                    ),
                    {
                        "requested_amount_minor": liquidity_minor,
                        "fund_destination": liquidity_destination,
                        "liquidity_priority": rules.get("liquidity_priority"),
                    },
                )
            )
            evaluations.append(
                (
                    "SELL_02_THESIS_INVALID",
                    thesis == "INVALID",
                    "CRITICAL",
                    _rule_status(
                        configured=True,
                        data_available=True,
                        triggered=thesis == "INVALID",
                    ),
                    {"thesis_status": thesis},
                )
            )
            hard_stop = config["hard_stop_return_bps"]
            hard_stop_configured = hard_stop is not None
            hard_stop_hit = (
                hard_stop_configured
                and return_bps is not None
                and return_bps <= int(hard_stop)
            )
            evaluations.append(
                (
                    "SELL_01_HARD_STOP",
                    hard_stop_hit,
                    "HIGH",
                    _rule_status(
                        configured=hard_stop_configured,
                        data_available=return_bps is not None,
                        triggered=hard_stop_hit,
                    ),
                    {
                        "configured_threshold_bps": hard_stop,
                        "actual_return_bps": return_bps,
                    },
                )
            )
            cap = config["maximum_position_weight_bps"]
            cap_configured = cap is not None
            weight_bps = (
                int(
                    (Decimal(str(position["weight_pct"])) * Decimal(100)).to_integral_value(
                        rounding=ROUND_HALF_UP
                    )
                )
                if position["weight_pct"] is not None
                else None
            )
            cap_hit = cap_configured and weight_bps is not None and weight_bps > int(cap)
            cap_exempt = cap_hit and allocation["state"] == "TRANSITION_REQUIRED"
            rebalance_status = _rule_status(
                configured=cap_configured,
                data_available=weight_bps is not None,
                triggered=cap_hit and not cap_exempt,
                exempt=cap_exempt,
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
                position_hits.append(hit)
                if triggered and (
                    rule_code.startswith("SELL_") or rule_code == "CORE_TOOL_QUALITY"
                ):
                    proposals.append(
                        self._proposal_for_hit(
                            portfolio_id=portfolio_id,
                            assignment=assignment,
                            config=config,
                            position=position,
                            hit=hit,
                        )
                    )
            instrument_status_counts: dict[str, int] = {}
            for hit in position_hits:
                status = str(hit["status"])
                instrument_status_counts[status] = (
                    instrument_status_counts.get(status, 0) + 1
                )
            evaluated_count = sum(
                count
                for status, count in instrument_status_counts.items()
                if status in RULE_EVALUATED_STATUSES
            )
            triggered_for_instrument = sum(
                1 for hit in position_hits if bool(hit["output"].get("triggered"))
            )
            if triggered_for_instrument:
                assessment = "REVIEW_REQUIRED"
                assessment_reason = "RULE_HIT"
            elif (
                instrument_status_counts.get("DATA_UNAVAILABLE", 0)
                and evaluated_count == 0
            ):
                assessment = "DATA_BLOCKED"
                assessment_reason = "NO_EVALUABLE_RULES"
            elif instrument_status_counts.get(
                "NOT_CONFIGURED", 0
            ) or instrument_status_counts.get("DATA_UNAVAILABLE", 0):
                assessment = "PARTIAL"
                assessment_reason = "RULE_CONFIGURATION_OR_DATA_INCOMPLETE"
            else:
                assessment = "PASS"
                assessment_reason = "NO_SELL_RULE_HIT"
            instrument_summaries.append(
                {
                    "instrument_code": code,
                    "instrument_name": holding["instrument_name"],
                    "assessment": assessment,
                    "reason_code": assessment_reason,
                    "candidate_rule_count": len(position_hits),
                    "configured_rule_count": sum(
                        count
                        for status, count in instrument_status_counts.items()
                        if status not in {"NOT_CONFIGURED", "NOT_APPLICABLE"}
                    ),
                    "evaluable_rule_count": evaluated_count,
                    "evaluated_rule_count": evaluated_count,
                    "triggered_rule_count": triggered_for_instrument,
                    "status_counts": instrument_status_counts,
                }
            )
        status_counts: dict[str, int] = {}
        for hit in hits:
            status = str(hit["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        evaluated_count = sum(
            count
            for status, count in status_counts.items()
            if status in RULE_EVALUATED_STATUSES
        )
        configured_count = sum(
            count
            for status, count in status_counts.items()
            if status not in {"NOT_CONFIGURED", "NOT_APPLICABLE"}
        )
        triggered_count = sum(1 for hit in hits if bool(hit["output"].get("triggered")))
        if proposals:
            state = "REVIEW_REQUIRED"
            reason_code = "SELL_PROPOSALS_CREATED"
        elif evaluated_count == 0 and status_counts.get("DATA_UNAVAILABLE", 0):
            state = "DATA_BLOCKED"
            reason_code = "RISK_SCAN_DATA_BLOCKED"
        elif status_counts.get("NOT_CONFIGURED", 0) or status_counts.get(
            "DATA_UNAVAILABLE", 0
        ):
            state = "PARTIAL"
            reason_code = "RISK_SCAN_PARTIAL"
        elif evaluated_count == 0:
            state = "NOT_CONFIGURED"
            reason_code = "NO_CONFIGURED_RULES"
        else:
            state = "PASS"
            reason_code = "NO_SELL_RULE_HIT"
        return {
            "state": state,
            "reason_code": reason_code,
            "rule_contract_version": RULE_VERSION,
            "rule_source": "STRATEGY_INSTANCE_AND_CORE_LIFECYCLE",
            "evaluation_summary": {
                "candidate_rule_count": len(hits),
                "configured_rule_count": configured_count,
                "evaluable_rule_count": evaluated_count,
                "evaluated_rule_count": evaluated_count,
                "not_configured_count": status_counts.get("NOT_CONFIGURED", 0),
                "data_unavailable_count": status_counts.get("DATA_UNAVAILABLE", 0),
                "not_applicable_count": status_counts.get("NOT_APPLICABLE", 0),
                "exempt_count": status_counts.get("EXEMPT", 0),
                "triggered_rule_count": triggered_count,
                "sell_proposal_count": len(proposals),
            },
            "instrument_summaries": instrument_summaries,
            "rule_hits_included": include_rule_hits,
            "rule_hits": hits if include_rule_hits else [],
            "sell_proposals": proposals,
            "data_quality": valuation["data_quality"],
            "execution_status": "SUCCESS",
            "trade_execution_status": "NOT_EXECUTED",
        }

    def _latest_lifecycle_observation(
        self, instrument_id: str, observation_type: str
    ) -> JsonDict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM instrument_lifecycle_observations
                WHERE instrument_id = ? AND observation_type = ?
                  AND verification_status = 'VERIFIED'
                ORDER BY observation_date DESC, observed_at DESC LIMIT 1
                """,
                (instrument_id, observation_type),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "observation_date": str(row["observation_date"]),
            "facts": json.loads(str(row["facts_json"])),
            "source_type": str(row["source_type"]),
            "source_name": str(row["source_name"]),
            "source_ref": row["source_ref"],
            "verification_status": str(row["verification_status"]),
            "observed_at": str(row["observed_at"]),
            "record_hash": str(row["record_hash"]),
        }

    def list_rule_hits(
        self,
        *,
        portfolio_id: str,
        instrument_code: str | None = None,
        rule_code: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_details: bool = False,
    ) -> JsonDict:
        """Return paginated rule facts without forcing large scan responses."""
        clauses = ["rh.portfolio_id = ?"]
        params: list[object] = [portfolio_id]
        if instrument_code:
            clauses.append("i.code = ?")
            params.append(instrument_code)
        if rule_code:
            clauses.append("rh.rule_code = ?")
            params.append(rule_code)
        if status:
            clauses.append("rh.status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM rule_hits rh
                    LEFT JOIN instruments i ON i.id = rh.instrument_id
                    WHERE {where}
                    """,
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT rh.*, i.code AS instrument_code, i.name AS instrument_name
                FROM rule_hits rh
                LEFT JOIN instruments i ON i.id = rh.instrument_id
                WHERE {where}
                ORDER BY rh.evaluated_at DESC, rh.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        items: list[JsonDict] = []
        for row in rows:
            output = json.loads(str(row["output_json"]))
            item: JsonDict = {
                "id": str(row["id"]),
                "instrument_id": row["instrument_id"],
                "instrument_code": row["instrument_code"],
                "instrument_name": row["instrument_name"],
                "rule_code": str(row["rule_code"]),
                "rule_version": str(row["rule_version"]),
                "severity": str(row["severity"]),
                "status": str(row["status"]),
                "triggered": bool(output.get("triggered")),
                "requires_human_review": bool(output.get("requires_human_review")),
                "evaluated_at": str(row["evaluated_at"]),
            }
            if include_details:
                item.update(
                    {
                        "inputs": json.loads(str(row["input_json"])),
                        "output": output,
                        "input_hash": str(row["input_hash"]),
                    }
                )
            items.append(item)
        return {
            "items": items,
            "page": {
                "limit": limit,
                "offset": offset,
                "returned_count": len(items),
                "total_count": total,
                "has_more": offset + len(items) < total,
            },
            "include_details": include_details,
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
        input_hash = _hash({"rule_version": RULE_VERSION, "inputs": inputs})
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
        rules = config["lifecycle_rules"]
        fraction_bps: int | None = None
        target_weight_bps: int | None = None
        recommended_amount_minor: int | None = None
        action = "MANUAL_REVIEW"
        if trigger in {"SELL_02_THESIS_INVALID", "SELL_04_REPLACE"}:
            action, fraction_bps = "FULL_SELL", 10000
        elif trigger == "SELL_03_REBALANCE":
            action = "REDUCE_TO_WEIGHT"
            target_weight_bps = config["maximum_position_weight_bps"]
        elif trigger == "SELL_06_TAKE_PROFIT":
            action = "PARTIAL_SELL"
            fraction_bps = int(rules["take_profit_fraction_bps"])
        elif trigger == "SELL_07_OBJECTIVE_COMPLETE":
            fraction_bps = int(rules["objective_sell_fraction_bps"])
            action = "FULL_SELL" if fraction_bps == 10000 else "PARTIAL_SELL"
        elif trigger == "SELL_08_LIQUIDITY":
            action = "PARTIAL_SELL"
            recommended_amount_minor = int(
                hit["inputs"]["requested_amount_minor"]
            )
            market_minor = int(
                (Decimal(str(position["market_value"])) * Decimal(100)).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            )
            fraction_bps = min(
                10000,
                int(
                    (
                        Decimal(recommended_amount_minor)
                        / Decimal(market_minor)
                        * Decimal(10000)
                    ).to_integral_value(rounding=ROUND_HALF_UP)
                ),
            )
        if recommended_amount_minor is None and fraction_bps is not None:
            market_minor = int(
                (Decimal(str(position["market_value"])) * Decimal(100)).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            )
            recommended_amount_minor = max(
                1,
                int(
                    (
                        Decimal(market_minor)
                        * Decimal(fraction_bps)
                        / Decimal(10000)
                    ).to_integral_value(rounding=ROUND_HALF_UP)
                ),
            )
        engine = "REALIZATION" if trigger in {
            "SELL_06_TAKE_PROFIT",
            "SELL_07_OBJECTIVE_COMPLETE",
            "SELL_08_LIQUIDITY",
        } else "RISK"
        facts = {
            "instrument_code": config["instrument_code"],
            "instrument_name": config["instrument_name"],
            "role": config["role"],
            "rule_hit": hit,
            "position": position,
            "proxy_suitability": config["proxy_suitability"],
            "automatic_selling_allowed": False,
            "execution_status": "NOT_EXECUTED",
            "fund_destination": (
                hit["inputs"].get("fund_destination") or config["fund_destination"]
            ),
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
                    recommended_fraction_bps, target_weight_bps,
                    recommended_amount_minor, status,
                    trigger_facts_json, trigger_input_hash, proposed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'REVIEW_REQUIRED',
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
                    engine,
                    action,
                    fraction_bps,
                    target_weight_bps,
                    recommended_amount_minor,
                    _json(facts),
                    hit["input_hash"],
                    _iso(self._now()),
                ),
            )
            before = {
                "market_value": position["market_value"],
                "weight_pct": position["weight_pct"],
                "role": config["role"],
                "exposure_profile": config["exposure_profile"],
            }
            policy = config["redemption_policy"]
            holding_days_value = hit["inputs"].get("holding_days")
            if holding_days_value is None:
                holding_days_value = (
                    date.fromisoformat(str(position["nav_snapshot"]["nav_date"]))
                    - date.fromisoformat(str(position["holding"]["as_of"]))
                ).days
            holding_days = int(holding_days_value)
            fee_bps = int(policy.get("fee_bps", 0))
            if holding_days >= int(policy.get("fee_waiver_holding_days", 10**9)):
                fee_bps = 0
            elif "short_term_penalty_bps" in policy:
                fee_bps += int(policy["short_term_penalty_bps"])
            estimated_fee_minor = (
                int(
                    (
                        Decimal(recommended_amount_minor)
                        * Decimal(fee_bps)
                        / Decimal(10000)
                    ).to_integral_value(rounding=ROUND_HALF_UP)
                )
                if recommended_amount_minor is not None
                else None
            )
            destination = facts["fund_destination"]
            after = {
                "status": (
                    "DETERMINISTIC_PREVIEW"
                    if recommended_amount_minor is not None
                    else "REQUIRES_EXECUTION_AMOUNT"
                ),
                "estimated_sale_amount_minor": recommended_amount_minor,
                "estimated_fee_minor": estimated_fee_minor,
                "fund_destination": destination,
                "instrument_exposure_after_bps": (
                    max(0, 10000 - fraction_bps) if fraction_bps is not None else None
                ),
                "automatic_trade": False,
            }
            diagnostic_result = (
                "PASS"
                if recommended_amount_minor is not None and destination and policy
                else "WARNING"
            )
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
                            "fees_estimated": estimated_fee_minor is not None,
                            "redemption_policy": policy,
                            "estimated_fee_bps": fee_bps,
                            "fund_destination_configured": bool(destination),
                            "automatic_trade": False,
                        }
                    ),
                    _json(before),
                    _json(after),
                    _json({"review_after_months": 6}),
                    diagnostic_result,
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
        execution = connection.execute(
            """
            SELECT l.*, t.trade_date, t.amount_minor, t.nav_micros, t.shares_micros
            FROM sell_execution_links l
            JOIN transactions t ON t.id = l.transaction_id
            WHERE l.sell_proposal_id = ?
            """,
            (row["id"],),
        ).fetchone()
        followup = connection.execute(
            "SELECT * FROM sell_followups WHERE sell_proposal_id = ?",
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
            "recommended_amount": (
                f"{Decimal(int(row['recommended_amount_minor'])) / 100:.2f}"
                if row["recommended_amount_minor"] is not None
                else None
            ),
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
            "execution_status": "EXECUTED" if execution is not None else "NOT_EXECUTED",
            "execution_link": (
                {
                    "id": str(execution["id"]),
                    "transaction_id": str(execution["transaction_id"]),
                    "trade_date": str(execution["trade_date"]),
                    "amount": f"{Decimal(int(execution['amount_minor'])) / 100:.2f}",
                    "nav": f"{Decimal(int(execution['nav_micros'])) / VALUE_SCALE:.6f}",
                    "shares": f"{Decimal(int(execution['shares_micros'])) / VALUE_SCALE:.6f}",
                    "linked_at": str(execution["linked_at"]),
                    "linked_by": str(execution["linked_by"]),
                }
                if execution is not None
                else None
            ),
            "followup": RiskService._followup_data(followup) if followup else None,
            "holdings_changed": execution is not None,
        }

    @staticmethod
    def _followup_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "sell_proposal_id": str(row["sell_proposal_id"]),
            "transaction_id": str(row["transaction_id"]),
            "sold_at": str(row["sold_at"]),
            "due_at": str(row["due_at"]),
            "status": str(row["status"]),
            "expected_metric": json.loads(str(row["expected_metric_json"])),
            "result": (
                json.loads(str(row["result_json"])) if row["result_json"] is not None else None
            ),
            "evaluated_at": row["evaluated_at"],
        }

    def list_followups(
        self,
        *,
        portfolio_id: str,
        status: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = """
            SELECT f.*
            FROM sell_followups f
            JOIN sell_proposals p ON p.id = f.sell_proposal_id
            WHERE p.portfolio_id = ?
        """
        params: list[object] = [portfolio_id]
        if status:
            query += " AND f.status = ?"
            params.append(status.strip().upper())
        query += " ORDER BY f.due_at, f.created_at LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._followup_data(row) for row in rows]

    def evaluate_followup(
        self,
        *,
        followup_id: str,
        as_of_date: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        end = date.fromisoformat(as_of_date) if as_of_date else self._now().date()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sell_followups WHERE id = ?",
                (followup_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "SELL_FOLLOWUP_NOT_FOUND",
                    "sell follow-up was not found",
                    http_status=404,
                )
            if end < date.fromisoformat(str(row["due_at"])):
                return {
                    **self._followup_data(row),
                    "reason_code": "FOLLOWUP_NOT_DUE",
                }
            expected = json.loads(str(row["expected_metric_json"]))
            nav = connection.execute(
                """
                SELECT nav_micros, nav_date, source_type, source_name,
                       verification_status, record_hash
                FROM market_nav_snapshots
                WHERE instrument_id = ? AND nav_date <= ?
                ORDER BY nav_date DESC, observed_at DESC LIMIT 1
                """,
                (row["instrument_id"], end.isoformat()),
            ).fetchone()
            timestamp = _iso(self._now())
            if nav is None:
                result: JsonDict = {
                    "reason_code": "NAV_MISSING",
                    "as_of_date": end.isoformat(),
                    "data_quality": "SOURCE_ERROR",
                }
                status = "DATA_BLOCKED"
            else:
                execution_nav = Decimal(str(expected["execution_nav"]))
                current_nav = Decimal(int(nav["nav_micros"])) / VALUE_SCALE
                return_bps = int(
                    (
                        (current_nav / execution_nav - Decimal(1)) * Decimal(10000)
                    ).to_integral_value(rounding=ROUND_HALF_UP)
                )
                result = {
                    "reason_code": "FOLLOWUP_EVALUATED",
                    "as_of_date": str(nav["nav_date"]),
                    "execution_nav": f"{execution_nav:.6f}",
                    "current_nav": f"{current_nav:.6f}",
                    "post_sell_return_bps": return_bps,
                    "assessment": (
                        "SELL_AVOIDED_DECLINE" if return_bps < 0 else "ASSET_ROSE_AFTER_SELL"
                    ),
                    "source": {
                        "type": str(nav["source_type"]),
                        "name": str(nav["source_name"]),
                        "verification_status": str(nav["verification_status"]),
                        "record_hash": str(nav["record_hash"]),
                    },
                    "strategy_parameters_changed": False,
                }
                status = "COMPLETED"
            connection.execute(
                """
                UPDATE sell_followups
                SET status = ?, result_json = ?, evaluated_at = ?
                WHERE id = ?
                """,
                (status, _json(result), timestamp, followup_id),
            )
            self._audit(
                connection,
                action="SELL_FOLLOWUP_EVALUATED",
                entity_type="sell_followup",
                entity_id=followup_id,
                actor_ref=actor_ref,
                details=result,
            )
            updated = connection.execute(
                "SELECT * FROM sell_followups WHERE id = ?",
                (followup_id,),
            ).fetchone()
            assert updated is not None
            return self._followup_data(updated)

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
