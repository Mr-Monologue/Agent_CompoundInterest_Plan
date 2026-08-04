"""Reusable strategy definitions and explicitly approved portfolio instances."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_lifecycle_rules(value: JsonDict | None) -> JsonDict:
    rules = dict(value or {})
    supported = {
        "replacement_min_score_delta_bps",
        "replacement_min_consecutive_periods",
        "underperformance_threshold_bps",
        "underperformance_min_days",
        "take_profit_return_bps",
        "take_profit_min_holding_days",
        "take_profit_fraction_bps",
        "objective_sell_fraction_bps",
        "max_tracking_error_bps",
        "max_expense_ratio_bps",
        "liquidity_priority",
    }
    unknown = sorted(set(rules) - supported)
    if unknown:
        raise LedgerError(
            "UNSUPPORTED_LIFECYCLE_RULE",
            "unsupported lifecycle rule keys",
            details={"unknown_keys": unknown},
        )
    integer_rules = {key: int(item) for key, item in rules.items()}
    required_groups = (
        {
            "replacement_min_score_delta_bps",
            "replacement_min_consecutive_periods",
        },
        {"underperformance_threshold_bps", "underperformance_min_days"},
        {
            "take_profit_return_bps",
            "take_profit_min_holding_days",
            "take_profit_fraction_bps",
        },
    )
    for group in required_groups:
        supplied = group.intersection(integer_rules)
        if supplied and supplied != group:
            raise LedgerError(
                "INCOMPLETE_LIFECYCLE_RULE",
                "related lifecycle rule parameters must be configured together",
                details={"required_keys": sorted(group), "supplied_keys": sorted(supplied)},
            )
    if (
        "underperformance_threshold_bps" in integer_rules
        and not -10000 <= integer_rules["underperformance_threshold_bps"] < 0
    ):
        raise LedgerError(
            "INVALID_UNDERPERFORMANCE_RULE",
            "underperformance threshold must be -10000..<0 bps",
        )
    for key in {
        "replacement_min_score_delta_bps",
        "take_profit_return_bps",
        "max_tracking_error_bps",
        "max_expense_ratio_bps",
        "liquidity_priority",
    }:
        if key in integer_rules and integer_rules[key] < 0:
            raise LedgerError("INVALID_LIFECYCLE_RULE", f"{key} must be nonnegative")
    for key in {
        "replacement_min_consecutive_periods",
        "underperformance_min_days",
    }:
        if key in integer_rules and integer_rules[key] < 1:
            raise LedgerError("INVALID_LIFECYCLE_RULE", f"{key} must be positive")
    for key in {"take_profit_fraction_bps", "objective_sell_fraction_bps"}:
        if key in integer_rules and not 1 <= integer_rules[key] <= 10000:
            raise LedgerError("INVALID_LIFECYCLE_RULE", f"{key} must be 1..10000 bps")
    if (
        "take_profit_return_bps" in integer_rules
        and integer_rules["take_profit_return_bps"] <= 0
    ):
        raise LedgerError(
            "INVALID_TAKE_PROFIT_RULE",
            "take-profit return must be positive",
        )
    if (
        "take_profit_min_holding_days" in integer_rules
        and integer_rules["take_profit_min_holding_days"] < 0
    ):
        raise LedgerError(
            "INVALID_TAKE_PROFIT_RULE",
            "take-profit holding period must be nonnegative",
        )
    return integer_rules


def _validate_redemption_policy(value: JsonDict | None) -> JsonDict:
    policy = dict(value or {})
    supported = {"fee_bps", "fee_waiver_holding_days", "short_term_penalty_bps"}
    unknown = sorted(set(policy) - supported)
    if unknown:
        raise LedgerError(
            "UNSUPPORTED_REDEMPTION_POLICY",
            "unsupported redemption policy keys",
            details={"unknown_keys": unknown},
        )
    normalized = {key: int(item) for key, item in policy.items()}
    for key in {"fee_bps", "short_term_penalty_bps"}:
        if key in normalized and not 0 <= normalized[key] <= 10000:
            raise LedgerError("INVALID_REDEMPTION_POLICY", f"{key} must be 0..10000 bps")
    if (
        "fee_waiver_holding_days" in normalized
        and normalized["fee_waiver_holding_days"] < 0
    ):
        raise LedgerError(
            "INVALID_REDEMPTION_POLICY",
            "fee waiver holding days must be nonnegative",
        )
    return normalized


def _validate_exposure_profile(value: JsonDict | None) -> JsonDict:
    profile = dict(value or {})
    supported = {"industry", "market", "geography", "style"}
    unknown = sorted(set(profile) - supported)
    if unknown:
        raise LedgerError(
            "UNSUPPORTED_EXPOSURE_DIMENSION",
            "unsupported exposure profile dimensions",
            details={"unknown_keys": unknown},
        )
    normalized: JsonDict = {}
    for dimension, raw_weights in profile.items():
        if not isinstance(raw_weights, dict):
            raise LedgerError(
                "INVALID_EXPOSURE_PROFILE",
                f"{dimension} exposure must be an object of label to bps",
            )
        weights = {str(label): int(weight) for label, weight in raw_weights.items()}
        if any(weight < 0 or weight > 10000 for weight in weights.values()):
            raise LedgerError(
                "INVALID_EXPOSURE_PROFILE",
                f"{dimension} exposure weights must be 0..10000 bps",
            )
        if sum(weights.values()) > 10000:
            raise LedgerError(
                "INVALID_EXPOSURE_PROFILE",
                f"{dimension} exposure weights cannot exceed 10000 bps",
            )
        normalized[dimension] = weights
    return normalized


class StrategyService:
    """Keep public rules separate from a user's portfolio configuration."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.settings = settings
        self._now = now

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
        before_hash: str | None = None,
        after_hash: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                id, occurred_at, actor_type, actor_ref, action, entity_type,
                entity_id, before_hash, after_hash, details_json, trace_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                _iso(self._now()),
                actor_type,
                actor_ref,
                action,
                entity_type,
                entity_id,
                before_hash,
                after_hash,
                _canonical_json(details),
                str(uuid4()),
            ),
        )

    @staticmethod
    def _decode_json(value: object, *, code: str, message: str) -> JsonDict:
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LedgerError(code, message, http_status=409) from exc
        if not isinstance(decoded, dict):
            raise LedgerError(code, message, http_status=409)
        return decoded

    def list_definitions(self) -> list[JsonDict]:
        """List reusable public strategy versions without portfolio data."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.id, d.strategy_key, d.name, d.description, d.status,
                       v.id AS version_id, v.version, v.parameters_json,
                       v.parameters_hash, v.status AS version_status,
                       v.published_at
                FROM strategy_definitions d
                JOIN strategy_versions v ON v.strategy_definition_id = d.id
                ORDER BY d.strategy_key, v.published_at DESC, v.version DESC
                """
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "strategy_key": str(row["strategy_key"]),
                "name": str(row["name"]),
                "description": str(row["description"]),
                "status": str(row["status"]),
                "version": {
                    "id": str(row["version_id"]),
                    "version": str(row["version"]),
                    "parameters": self._decode_json(
                        row["parameters_json"],
                        code="INVALID_STRATEGY_VERSION",
                        message="saved strategy version is invalid",
                    ),
                    "parameters_hash": str(row["parameters_hash"]),
                    "status": str(row["version_status"]),
                    "published_at": str(row["published_at"]),
                },
            }
            for row in rows
        ]

    def get_assignment(self, *, portfolio_id: str) -> JsonDict:
        """Return one active portfolio strategy instance and its approved instruments."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT a.*, d.strategy_key, d.name AS strategy_name,
                       v.version AS strategy_version, v.parameters_json,
                       v.parameters_hash
                FROM strategy_assignments a
                JOIN strategy_versions v ON v.id = a.strategy_version_id
                JOIN strategy_definitions d ON d.id = v.strategy_definition_id
                WHERE a.portfolio_id = ? AND a.status = 'ACTIVE'
                LIMIT 1
                """,
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "STRATEGY_NOT_ASSIGNED",
                    "no approved strategy is assigned to this portfolio",
                    http_status=409,
                    details={"portfolio_id": portfolio_id},
                )
            configs = connection.execute(
                """
                SELECT c.*, i.code AS instrument_code, i.name AS instrument_name,
                       i.asset_type, b.code AS benchmark_code
                FROM strategy_instrument_configs c
                JOIN instruments i ON i.id = c.instrument_id
                LEFT JOIN instruments b ON b.id = c.benchmark_instrument_id
                WHERE c.strategy_assignment_id = ?
                ORDER BY c.priority, i.code
                """,
                (row["id"],),
            ).fetchall()
        return {
            "id": str(row["id"]),
            "portfolio_id": str(row["portfolio_id"]),
            "status": str(row["status"]),
            "strategy": {
                "key": str(row["strategy_key"]),
                "name": str(row["strategy_name"]),
                "version": str(row["strategy_version"]),
                "parameters": self._decode_json(
                    row["parameters_json"],
                    code="INVALID_STRATEGY_VERSION",
                    message="saved strategy version is invalid",
                ),
                "parameters_hash": str(row["parameters_hash"]),
            },
            "instance_config": self._decode_json(
                row["instance_config_json"],
                code="INVALID_STRATEGY_ASSIGNMENT",
                message="saved strategy assignment is invalid",
            ),
            "instance_config_hash": str(row["instance_config_hash"]),
            "approved_by": str(row["approved_by"]),
            "approved_at": str(row["approved_at"]),
            "instruments": [
                {
                    "id": str(config["id"]),
                    "instrument_id": str(config["instrument_id"]),
                    "instrument_code": str(config["instrument_code"]),
                    "instrument_name": str(config["instrument_name"]),
                    "asset_type": str(config["asset_type"]),
                    "role": str(config["role"]),
                    "contribution_eligible": bool(config["contribution_eligible"]),
                    "target_weight_bps": (
                        int(config["target_weight_bps"])
                        if config["target_weight_bps"] is not None
                        else None
                    ),
                    "priority": int(config["priority"]),
                    "minimum_amount_minor": int(config["minimum_amount_minor"]),
                    "maximum_amount_minor": (
                        int(config["maximum_amount_minor"])
                        if config["maximum_amount_minor"] is not None
                        else None
                    ),
                    "status": str(config["status"]),
                    "benchmark_code": (
                        str(config["benchmark_code"])
                        if config["benchmark_code"] is not None
                        else None
                    ),
                    "thesis_status": str(config["thesis_status"]),
                    "proxy_suitability": str(config["proxy_suitability"]),
                    "hard_stop_return_bps": (
                        int(config["hard_stop_return_bps"])
                        if config["hard_stop_return_bps"] is not None
                        else None
                    ),
                    "maximum_position_weight_bps": (
                        int(config["maximum_position_weight_bps"])
                        if config["maximum_position_weight_bps"] is not None
                        else None
                    ),
                    "lifecycle_rules": self._decode_json(
                        config["lifecycle_rules_json"],
                        code="INVALID_LIFECYCLE_RULES",
                        message="saved lifecycle rules are invalid",
                    ),
                    "redemption_policy": self._decode_json(
                        config["redemption_policy_json"],
                        code="INVALID_REDEMPTION_POLICY",
                        message="saved redemption policy is invalid",
                    ),
                    "exposure_profile": self._decode_json(
                        config["exposure_profile_json"],
                        code="INVALID_EXPOSURE_PROFILE",
                        message="saved exposure profile is invalid",
                    ),
                    "fund_destination": config["fund_destination"],
                    "approved_by": str(config["approved_by"]),
                    "approved_at": str(config["approved_at"]),
                    "updated_at": str(config["updated_at"]),
                }
                for config in configs
            ],
        }

    def assign(
        self,
        *,
        portfolio_id: str,
        strategy_key: str,
        strategy_version: str,
        instance_config: JsonDict | None,
        approved_by: str,
        reason: str,
    ) -> JsonDict:
        """Protected operation: bind an approved strategy version to one portfolio."""
        normalized_approver = approved_by.strip()
        normalized_reason = reason.strip()
        if not normalized_approver or not normalized_reason:
            raise LedgerError(
                "APPROVAL_REQUIRED",
                "approved_by and reason are required for strategy assignment",
            )
        config = instance_config or {}
        config_hash = _canonical_hash(config)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            portfolio = connection.execute(
                "SELECT id FROM portfolios WHERE id = ? AND status = 'ACTIVE'",
                (portfolio_id,),
            ).fetchone()
            if portfolio is None:
                raise LedgerError(
                    "PORTFOLIO_NOT_FOUND",
                    "active portfolio was not found",
                    http_status=404,
                )
            version = connection.execute(
                """
                SELECT v.id
                FROM strategy_versions v
                JOIN strategy_definitions d ON d.id = v.strategy_definition_id
                WHERE d.strategy_key = ? AND d.status = 'ACTIVE'
                  AND v.version = ? AND v.status = 'PUBLISHED'
                """,
                (strategy_key.strip(), strategy_version.strip()),
            ).fetchone()
            if version is None:
                raise LedgerError(
                    "STRATEGY_VERSION_NOT_FOUND",
                    "published strategy version was not found",
                    http_status=404,
                )
            current = connection.execute(
                """
                SELECT id, strategy_version_id, instance_config_hash
                FROM strategy_assignments
                WHERE portfolio_id = ? AND status = 'ACTIVE'
                """,
                (portfolio_id,),
            ).fetchone()
            timestamp = _iso(self._now())
            if (
                current is not None
                and str(current["strategy_version_id"]) == str(version["id"])
                and str(current["instance_config_hash"]) == config_hash
            ):
                connection.commit()
                return self.get_assignment(portfolio_id=portfolio_id)
            if current is not None:
                connection.execute(
                    """
                    UPDATE strategy_assignments
                    SET status = 'RETIRED', retired_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, current["id"]),
                )
            assignment_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO strategy_assignments (
                    id, portfolio_id, strategy_version_id, instance_config_json,
                    instance_config_hash, status, approved_by, approved_at,
                    created_at, retired_at
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, NULL)
                """,
                (
                    assignment_id,
                    portfolio_id,
                    version["id"],
                    _canonical_json(config),
                    config_hash,
                    normalized_approver,
                    timestamp,
                    timestamp,
                ),
            )
            self._audit(
                connection,
                actor_type="CLI",
                actor_ref=normalized_approver,
                action="STRATEGY_ASSIGNED",
                entity_type="strategy_assignment",
                entity_id=assignment_id,
                details={
                    "portfolio_id": portfolio_id,
                    "strategy_key": strategy_key,
                    "strategy_version": strategy_version,
                    "reason": normalized_reason,
                },
                before_hash=(str(current["instance_config_hash"]) if current is not None else None),
                after_hash=config_hash,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_assignment(portfolio_id=portfolio_id)

    def configure_instrument(
        self,
        *,
        portfolio_id: str,
        instrument_code: str,
        role: str,
        contribution_eligible: bool,
        target_weight_bps: int | None,
        priority: int,
        minimum_amount_minor: int,
        maximum_amount_minor: int | None,
        benchmark_code: str | None,
        thesis_status: str,
        approved_by: str,
        reason: str,
        proxy_suitability: str = "NOT_APPLICABLE",
        hard_stop_return_bps: int | None = None,
        maximum_position_weight_bps: int | None = None,
        lifecycle_rules: JsonDict | None = None,
        redemption_policy: JsonDict | None = None,
        exposure_profile: JsonDict | None = None,
        fund_destination: str | None = None,
    ) -> JsonDict:
        """Protected operation: approve one instrument for a strategy instance."""
        normalized_role = role.strip().upper()
        normalized_thesis = thesis_status.strip().upper()
        normalized_suitability = proxy_suitability.strip().upper()
        if normalized_role not in {"CORE", "SATELLITE", "CASH", "WATCH", "UNASSIGNED"}:
            raise LedgerError("INVALID_ROLE", "unsupported strategy instrument role")
        if normalized_thesis not in {"ACTIVE", "REVIEW_REQUIRED", "INVALID"}:
            raise LedgerError("INVALID_THESIS_STATUS", "unsupported thesis status")
        if normalized_suitability not in {"STRONG", "WEAK", "NOT_APPLICABLE"}:
            raise LedgerError("INVALID_PROXY_SUITABILITY", "unsupported proxy suitability")
        if normalized_suitability == "NOT_APPLICABLE" and benchmark_code:
            raise LedgerError(
                "INVALID_BENCHMARK_MAPPING",
                "NOT_APPLICABLE instruments cannot have a valuation benchmark",
            )
        if normalized_suitability != "NOT_APPLICABLE" and not benchmark_code:
            raise LedgerError(
                "BENCHMARK_REQUIRED",
                "STRONG or WEAK proxy suitability requires a benchmark index",
            )
        if hard_stop_return_bps is not None and not -10000 <= hard_stop_return_bps < 0:
            raise LedgerError("INVALID_HARD_STOP", "hard stop must be -10000..<0 bps")
        if maximum_position_weight_bps is not None and not 0 < maximum_position_weight_bps <= 10000:
            raise LedgerError(
                "INVALID_POSITION_CAP",
                "maximum position weight must be 1..10000 bps",
            )
        if target_weight_bps is not None and not 0 <= target_weight_bps <= 10000:
            raise LedgerError("INVALID_TARGET_WEIGHT", "target weight must be 0..10000 bps")
        if priority < 0 or minimum_amount_minor < 0:
            raise LedgerError("INVALID_PLAN_CONSTRAINT", "priority and minimum must be nonnegative")
        if maximum_amount_minor is not None and maximum_amount_minor <= 0:
            raise LedgerError("INVALID_PLAN_CONSTRAINT", "maximum amount must be positive")
        if not approved_by.strip() or not reason.strip():
            raise LedgerError(
                "APPROVAL_REQUIRED",
                "approved_by and reason are required for instrument configuration",
            )
        normalized_lifecycle_rules = _validate_lifecycle_rules(lifecycle_rules)
        normalized_redemption_policy = _validate_redemption_policy(redemption_policy)
        normalized_exposure_profile = _validate_exposure_profile(exposure_profile)
        normalized_destination = fund_destination.strip() if fund_destination else None

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            assignment = connection.execute(
                """
                SELECT id FROM strategy_assignments
                WHERE portfolio_id = ? AND status = 'ACTIVE'
                """,
                (portfolio_id,),
            ).fetchone()
            if assignment is None:
                raise LedgerError(
                    "STRATEGY_NOT_ASSIGNED",
                    "no approved strategy is assigned to this portfolio",
                    http_status=409,
                )
            instrument = connection.execute(
                """
                SELECT id, asset_type
                FROM instruments
                WHERE code = ? AND status = 'ACTIVE'
                """,
                (instrument_code.strip().upper(),),
            ).fetchone()
            if instrument is None:
                raise LedgerError(
                    "INSTRUMENT_NOT_FOUND",
                    "active instrument was not found",
                    http_status=404,
                )
            if contribution_eligible and str(instrument["asset_type"]) == "INDEX":
                raise LedgerError(
                    "INDEX_NOT_TRADABLE",
                    "an index can be a benchmark but cannot receive contributions",
                    http_status=409,
                )
            benchmark_id: str | None = None
            if benchmark_code:
                benchmark = connection.execute(
                    """
                    SELECT id FROM instruments
                    WHERE code = ? AND asset_type = 'INDEX' AND status = 'ACTIVE'
                    """,
                    (benchmark_code.strip().upper(),),
                ).fetchone()
                if benchmark is None:
                    raise LedgerError(
                        "BENCHMARK_NOT_FOUND",
                        "active benchmark index was not found",
                        http_status=404,
                    )
                benchmark_id = str(benchmark["id"])

            timestamp = _iso(self._now())
            current = connection.execute(
                """
                SELECT * FROM strategy_instrument_configs
                WHERE strategy_assignment_id = ? AND instrument_id = ?
                """,
                (assignment["id"], instrument["id"]),
            ).fetchone()
            payload = {
                "role": normalized_role,
                "contribution_eligible": contribution_eligible,
                "target_weight_bps": target_weight_bps,
                "priority": priority,
                "minimum_amount_minor": minimum_amount_minor,
                "maximum_amount_minor": maximum_amount_minor,
                "benchmark_instrument_id": benchmark_id,
                "thesis_status": normalized_thesis,
                "proxy_suitability": normalized_suitability,
                "hard_stop_return_bps": hard_stop_return_bps,
                "maximum_position_weight_bps": maximum_position_weight_bps,
                "lifecycle_rules": normalized_lifecycle_rules,
                "redemption_policy": normalized_redemption_policy,
                "exposure_profile": normalized_exposure_profile,
                "fund_destination": normalized_destination,
            }
            before_hash = _canonical_hash(dict(current)) if current is not None else None
            config_id = str(current["id"]) if current is not None else str(uuid4())
            if current is None:
                connection.execute(
                    """
                    INSERT INTO strategy_instrument_configs (
                        id, strategy_assignment_id, instrument_id, role,
                        contribution_eligible, target_weight_bps, priority,
                        minimum_amount_minor, maximum_amount_minor, status,
                        benchmark_instrument_id, thesis_status, approved_by,
                        approved_at, created_at, updated_at, proxy_suitability,
                        hard_stop_return_bps, maximum_position_weight_bps,
                        lifecycle_rules_json, redemption_policy_json,
                        exposure_profile_json, fund_destination
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?)
                    """,
                    (
                        config_id,
                        assignment["id"],
                        instrument["id"],
                        normalized_role,
                        int(contribution_eligible),
                        target_weight_bps,
                        priority,
                        minimum_amount_minor,
                        maximum_amount_minor,
                        benchmark_id,
                        normalized_thesis,
                        approved_by.strip(),
                        timestamp,
                        timestamp,
                        timestamp,
                        normalized_suitability,
                        hard_stop_return_bps,
                        maximum_position_weight_bps,
                        _canonical_json(normalized_lifecycle_rules),
                        _canonical_json(normalized_redemption_policy),
                        _canonical_json(normalized_exposure_profile),
                        normalized_destination,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE strategy_instrument_configs
                    SET role = ?, contribution_eligible = ?, target_weight_bps = ?,
                        priority = ?, minimum_amount_minor = ?,
                        maximum_amount_minor = ?, status = 'ACTIVE',
                        benchmark_instrument_id = ?, thesis_status = ?,
                        approved_by = ?, approved_at = ?, updated_at = ?,
                        proxy_suitability = ?, hard_stop_return_bps = ?,
                        maximum_position_weight_bps = ?, lifecycle_rules_json = ?,
                        redemption_policy_json = ?, exposure_profile_json = ?,
                        fund_destination = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_role,
                        int(contribution_eligible),
                        target_weight_bps,
                        priority,
                        minimum_amount_minor,
                        maximum_amount_minor,
                        benchmark_id,
                        normalized_thesis,
                        approved_by.strip(),
                        timestamp,
                        timestamp,
                        normalized_suitability,
                        hard_stop_return_bps,
                        maximum_position_weight_bps,
                        _canonical_json(normalized_lifecycle_rules),
                        _canonical_json(normalized_redemption_policy),
                        _canonical_json(normalized_exposure_profile),
                        normalized_destination,
                        config_id,
                    ),
                )
            self._audit(
                connection,
                actor_type="CLI",
                actor_ref=approved_by.strip(),
                action="STRATEGY_INSTRUMENT_CONFIGURED",
                entity_type="strategy_instrument_config",
                entity_id=config_id,
                details={
                    "portfolio_id": portfolio_id,
                    "instrument_code": instrument_code.strip().upper(),
                    "reason": reason.strip(),
                    **payload,
                },
                before_hash=before_hash,
                after_hash=_canonical_hash(payload),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_assignment(portfolio_id=portfolio_id)

    def update_instrument_role(
        self,
        *,
        portfolio_id: str,
        instrument_code: str,
        role: str,
        expected_current_role: str,
        reason: str,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        """Update a portfolio-local role without changing contribution eligibility."""
        assignment = self.get_assignment(portfolio_id=portfolio_id)
        normalized_code = instrument_code.strip().upper()
        config = next(
            (
                item
                for item in assignment["instruments"]
                if item["instrument_code"] == normalized_code
            ),
            None,
        )
        current_role = str(config["role"]) if config is not None else "UNASSIGNED"
        normalized_expected = expected_current_role.strip().upper()
        if current_role != normalized_expected:
            raise LedgerError(
                "ROLE_CONFLICT",
                "strategy instrument role changed since it was last read",
                http_status=409,
                details={
                    "portfolio_id": portfolio_id,
                    "instrument_code": normalized_code,
                    "expected_current_role": normalized_expected,
                    "actual_current_role": current_role,
                },
            )
        updated = self.configure_instrument(
            portfolio_id=portfolio_id,
            instrument_code=normalized_code,
            role=role,
            contribution_eligible=(
                bool(config["contribution_eligible"]) if config is not None else False
            ),
            target_weight_bps=(config["target_weight_bps"] if config is not None else None),
            priority=int(config["priority"]) if config is not None else 100,
            minimum_amount_minor=(int(config["minimum_amount_minor"]) if config is not None else 1),
            maximum_amount_minor=(config["maximum_amount_minor"] if config is not None else None),
            benchmark_code=(config["benchmark_code"] if config is not None else None),
            thesis_status=(str(config["thesis_status"]) if config is not None else "ACTIVE"),
            approved_by=actor_ref,
            reason=reason,
            proxy_suitability=(
                str(config["proxy_suitability"]) if config is not None else "NOT_APPLICABLE"
            ),
            hard_stop_return_bps=(config["hard_stop_return_bps"] if config is not None else None),
            maximum_position_weight_bps=(
                config["maximum_position_weight_bps"] if config is not None else None
            ),
            lifecycle_rules=(config["lifecycle_rules"] if config is not None else None),
            redemption_policy=(config["redemption_policy"] if config is not None else None),
            exposure_profile=(config["exposure_profile"] if config is not None else None),
            fund_destination=(config["fund_destination"] if config is not None else None),
        )
        return {
            "portfolio_id": portfolio_id,
            "instrument_code": normalized_code,
            "previous_role": current_role,
            "changed": current_role != role.strip().upper(),
            "assignment": updated,
        }

    def create_config_draft(
        self,
        *,
        portfolio_id: str,
        instrument_code: str,
        contribution_eligible: bool,
        reason: str,
        role: str | None = None,
        target_weight_bps: int | None = None,
        priority: int | None = None,
        minimum_amount_minor: int | None = None,
        maximum_amount_minor: int | None = None,
        benchmark_code: str | None = None,
        proxy_suitability: str | None = None,
        thesis_status: str | None = None,
        hard_stop_return_bps: int | None = None,
        maximum_position_weight_bps: int | None = None,
        lifecycle_rules: JsonDict | None = None,
        redemption_policy: JsonDict | None = None,
        exposure_profile: JsonDict | None = None,
        fund_destination: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        """Create an expiring preview; NAV never determines contribution eligibility."""
        if not reason.strip():
            raise LedgerError("INVALID_REASON", "configuration reason is required")
        assignment = self.get_assignment(portfolio_id=portfolio_id)
        normalized_code = instrument_code.strip().upper()
        current = next(
            (
                item
                for item in assignment["instruments"]
                if item["instrument_code"] == normalized_code
            ),
            None,
        )
        if current is None:
            with self._connect() as connection:
                instrument = connection.execute(
                    "SELECT id, code, name, asset_type FROM instruments "
                    "WHERE code = ? AND status = 'ACTIVE'",
                    (normalized_code,),
                ).fetchone()
            if instrument is None:
                raise LedgerError(
                    "INSTRUMENT_NOT_FOUND",
                    "active instrument was not found",
                    http_status=404,
                )
            current = {
                "instrument_id": str(instrument["id"]),
                "instrument_code": str(instrument["code"]),
                "instrument_name": str(instrument["name"]),
                "asset_type": str(instrument["asset_type"]),
                "role": "UNASSIGNED",
                "contribution_eligible": False,
                "target_weight_bps": None,
                "priority": 100,
                "minimum_amount_minor": 1,
                "maximum_amount_minor": None,
                "benchmark_code": None,
                "thesis_status": "ACTIVE",
                "proxy_suitability": "NOT_APPLICABLE",
                "hard_stop_return_bps": None,
                "maximum_position_weight_bps": None,
                "lifecycle_rules": {},
                "redemption_policy": {},
                "exposure_profile": {},
                "fund_destination": None,
            }
        request: JsonDict = {
            "portfolio_id": portfolio_id,
            "instrument_code": normalized_code,
            "role": (role or str(current["role"])).strip().upper(),
            "contribution_eligible": contribution_eligible,
            "target_weight_bps": (
                target_weight_bps if target_weight_bps is not None else current["target_weight_bps"]
            ),
            "priority": priority if priority is not None else int(current["priority"]),
            "minimum_amount_minor": (
                minimum_amount_minor
                if minimum_amount_minor is not None
                else int(current["minimum_amount_minor"])
            ),
            "maximum_amount_minor": (
                maximum_amount_minor
                if maximum_amount_minor is not None
                else current["maximum_amount_minor"]
            ),
            "benchmark_code": (
                benchmark_code if benchmark_code is not None else current["benchmark_code"]
            ),
            "proxy_suitability": (
                proxy_suitability if proxy_suitability is not None else current["proxy_suitability"]
            ),
            "thesis_status": (
                thesis_status if thesis_status is not None else current["thesis_status"]
            ),
            "hard_stop_return_bps": (
                hard_stop_return_bps
                if hard_stop_return_bps is not None
                else current["hard_stop_return_bps"]
            ),
            "maximum_position_weight_bps": (
                maximum_position_weight_bps
                if maximum_position_weight_bps is not None
                else current["maximum_position_weight_bps"]
            ),
            "lifecycle_rules": (
                lifecycle_rules if lifecycle_rules is not None else current["lifecycle_rules"]
            ),
            "redemption_policy": (
                redemption_policy
                if redemption_policy is not None
                else current["redemption_policy"]
            ),
            "exposure_profile": (
                exposure_profile if exposure_profile is not None else current["exposure_profile"]
            ),
            "fund_destination": (
                fund_destination if fund_destination is not None else current["fund_destination"]
            ),
            "reason": reason.strip(),
        }
        request["lifecycle_rules"] = _validate_lifecycle_rules(request["lifecycle_rules"])
        request["redemption_policy"] = _validate_redemption_policy(request["redemption_policy"])
        request["exposure_profile"] = _validate_exposure_profile(request["exposure_profile"])
        # Reuse the protected validator without mutating by checking the key invariants here.
        if request["contribution_eligible"] and current["asset_type"] == "INDEX":
            raise LedgerError(
                "INDEX_NOT_TRADABLE",
                "an index can be a benchmark but cannot receive contributions",
                http_status=409,
            )
        now = self._now()
        expires_at = now + timedelta(minutes=self.settings.confirmation_ttl_minutes)
        token = secrets.token_urlsafe(24)
        draft_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO strategy_config_drafts (
                    id, portfolio_id, strategy_assignment_id, instrument_id,
                    request_json, request_hash, before_json, confirmation_digest,
                    status, created_by, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    draft_id,
                    portfolio_id,
                    assignment["id"],
                    current["instrument_id"],
                    _canonical_json(request),
                    _canonical_hash(request),
                    _canonical_json(current),
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
                "instrument_code": normalized_code,
                "before": current,
                "proposed": request,
                "expires_at": _iso(expires_at),
                "execution_status": "NOT_APPLIED",
            },
            "confirmation_token": token,
            "warnings": [
                "Contribution eligibility is an explicit long-term strategy decision; "
                "it was not inferred from NAV, valuation, holdings, or model opinion"
            ],
        }

    def get_config_draft(self, *, draft_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_config_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "STRATEGY_CONFIG_DRAFT_NOT_FOUND",
                    "strategy configuration draft was not found",
                    http_status=404,
                )
            status = str(row["status"])
            if status == "PENDING" and _parse_iso(str(row["expires_at"])) <= self._now():
                connection.execute(
                    "UPDATE strategy_config_drafts SET status = 'EXPIRED' WHERE id = ?",
                    (draft_id,),
                )
                status = "EXPIRED"
            return {
                "id": str(row["id"]),
                "status": status,
                "portfolio_id": str(row["portfolio_id"]),
                "before": self._decode_json(
                    row["before_json"],
                    code="INVALID_STRATEGY_CONFIG_DRAFT",
                    message="saved strategy configuration draft is invalid",
                ),
                "proposed": self._decode_json(
                    row["request_json"],
                    code="INVALID_STRATEGY_CONFIG_DRAFT",
                    message="saved strategy configuration draft is invalid",
                ),
                "created_at": str(row["created_at"]),
                "expires_at": str(row["expires_at"]),
                "committed_at": row["committed_at"],
                "committed_by": row["committed_by"],
            }

    def commit_config_draft(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        """Apply one exact strategy config after explicit confirmation."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM strategy_config_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "STRATEGY_CONFIG_DRAFT_NOT_FOUND",
                    "strategy configuration draft was not found",
                    http_status=404,
                )
            if str(row["status"]) == "COMMITTED":
                connection.commit()
                return {
                    "draft": self.get_config_draft(draft_id=draft_id),
                    "idempotent_replay": True,
                }
            if str(row["status"]) != "PENDING":
                raise LedgerError(
                    "INVALID_STRATEGY_CONFIG_DRAFT_STATUS",
                    "strategy configuration draft is not pending",
                    http_status=409,
                )
            if _parse_iso(str(row["expires_at"])) <= self._now():
                connection.execute(
                    "UPDATE strategy_config_drafts SET status = 'EXPIRED' WHERE id = ?",
                    (draft_id,),
                )
                raise LedgerError(
                    "CONFIRMATION_EXPIRED",
                    "strategy configuration confirmation has expired",
                    http_status=409,
                )
            if not hmac.compare_digest(
                str(row["confirmation_digest"]),
                _token_digest(confirmation_token),
            ):
                raise LedgerError(
                    "CONFIRMATION_MISMATCH",
                    "confirmation token does not match this draft",
                    http_status=409,
                )
            request = self._decode_json(
                row["request_json"],
                code="INVALID_STRATEGY_CONFIG_DRAFT",
                message="saved strategy configuration draft is invalid",
            )
            connection.commit()
        finally:
            connection.close()
        assignment = self.configure_instrument(
            portfolio_id=str(request["portfolio_id"]),
            instrument_code=str(request["instrument_code"]),
            role=str(request["role"]),
            contribution_eligible=bool(request["contribution_eligible"]),
            target_weight_bps=request["target_weight_bps"],
            priority=int(request["priority"]),
            minimum_amount_minor=int(request["minimum_amount_minor"]),
            maximum_amount_minor=request["maximum_amount_minor"],
            benchmark_code=request["benchmark_code"],
            thesis_status=str(request["thesis_status"]),
            approved_by=confirmed_by,
            reason=str(request["reason"]),
            proxy_suitability=str(request["proxy_suitability"]),
            hard_stop_return_bps=request["hard_stop_return_bps"],
            maximum_position_weight_bps=request["maximum_position_weight_bps"],
            lifecycle_rules=request["lifecycle_rules"],
            redemption_policy=request["redemption_policy"],
            exposure_profile=request["exposure_profile"],
            fund_destination=request["fund_destination"],
        )
        with self._connect() as update:
            timestamp = _iso(self._now())
            update.execute(
                """
                UPDATE strategy_config_drafts
                SET status = 'COMMITTED', committed_at = ?, committed_by = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (timestamp, confirmed_by.strip(), draft_id),
            )
            self._audit(
                update,
                actor_type="USER",
                actor_ref=confirmed_by.strip(),
                action="STRATEGY_CONFIG_DRAFT_COMMITTED",
                entity_type="strategy_config_draft",
                entity_id=draft_id,
                details={"request_hash": str(row["request_hash"])},
                before_hash=_canonical_hash(
                    self._decode_json(
                        row["before_json"],
                        code="INVALID_STRATEGY_CONFIG_DRAFT",
                        message="saved strategy configuration draft is invalid",
                    )
                ),
                after_hash=str(row["request_hash"]),
            )
        return {
            "draft": self.get_config_draft(draft_id=draft_id),
            "assignment": assignment,
            "idempotent_replay": False,
        }
