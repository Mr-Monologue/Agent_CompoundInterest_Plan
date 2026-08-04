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
from investor_core.source_lineage import resolve_source_lineage
from investor_core.strategy import StrategyService

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


def _display_money(value: str) -> str:
    amount = Decimal(value)
    if amount < 0:
        return f"-¥{abs(amount):.2f}"
    return f"¥{amount:.2f}"


def _display_warning(warning: str) -> str:
    translations = {
        "NAV is single-source or unverified; use the deterministic result conservatively": (
            "净值为单一来源或未经独立验证，请保守使用确定性估值结果。"  # noqa: RUF001
        ),
        "No committed holdings were found": "未找到已提交的持仓记录。",
    }
    if warning in translations:
        return translations[warning]
    if warning.startswith("Missing NAV for "):
        return f"{warning.removeprefix('Missing NAV for ')} 缺少净值。"
    if warning.startswith("Conflicting NAV observations for "):
        return f"{warning.removeprefix('Conflicting NAV observations for ')} 存在冲突净值。"
    if warning.startswith("Stale NAV for "):
        return warning.replace("Stale NAV for ", "净值已过期: ").replace(" days old", " 天。")
    return warning


def _nav(value_micros: int) -> str:
    return f"{Decimal(value_micros) / NAV_SCALE:.6f}"


def _canonical_hash(payload: JsonDict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _allocation_assessment(
    *,
    policy_record: JsonDict,
    role_summary: dict[str, JsonDict],
    valuation_complete: bool,
) -> JsonDict:
    policy = policy_record["policy"]
    if not valuation_complete:
        return {
            "available": False,
            "state": "NOT_EVALUATED",
            "reason_code": "VALUATION_UNAVAILABLE",
            "policy": policy_record,
        }

    core_actual = Decimal(str(role_summary["CORE"]["market_value_pct"]))
    satellite_actual = Decimal(str(role_summary["SATELLITE"]["market_value_pct"]))
    core_target = Decimal(str(policy["core_target_pct"]))
    satellite_target = Decimal(str(policy["satellite_target_pct"]))
    tolerance = Decimal(str(policy["tolerance_pct"]))
    trigger = Decimal(str(policy["transition_trigger_pct"]))
    core_delta = core_actual - core_target
    satellite_delta = satellite_actual - satellite_target
    max_deviation = max(abs(core_delta), abs(satellite_delta))
    unassigned_count = int(role_summary.get("UNASSIGNED", {}).get("position_count", 0))

    if unassigned_count:
        state = "BLOCKED_UNASSIGNED"
        reason_code = "ROLE_UNASSIGNED"
    elif max_deviation > trigger:
        state = "TRANSITION_REQUIRED"
        reason_code = "DEVIATION_EXCEEDS_TRANSITION_TRIGGER"
    elif max_deviation > tolerance:
        state = "OUTSIDE_TOLERANCE"
        reason_code = "DEVIATION_EXCEEDS_TOLERANCE"
    else:
        state = "ON_TARGET"
        reason_code = "WITHIN_TOLERANCE"

    for role, actual, target, delta in (
        ("CORE", core_actual, core_target, core_delta),
        ("SATELLITE", satellite_actual, satellite_target, satellite_delta),
    ):
        if abs(delta) <= tolerance:
            assessment = "ON_TARGET"
        elif delta < 0:
            assessment = "UNDER_TARGET"
        else:
            assessment = "OVER_TARGET"
        role_summary[role].update(
            {
                "target_pct": f"{target:.2f}",
                "deviation_pct_points": f"{delta:+.2f}",
                "assessment": assessment,
                "reason_code": reason_code,
                "actual_pct": f"{actual:.2f}",
            }
        )

    core_exit_min = Decimal(str(policy["transition_exit_core_min_pct"]))
    satellite_exit_max = Decimal(str(policy["transition_exit_satellite_max_pct"]))
    core_exit_met = core_actual >= core_exit_min
    satellite_exit_met = satellite_actual <= satellite_exit_max
    no_unassigned_met = unassigned_count == 0
    return {
        "available": True,
        "state": state,
        "reason_code": reason_code,
        "policy": policy_record,
        "actual": {
            "CORE": f"{core_actual:.2f}",
            "SATELLITE": f"{satellite_actual:.2f}",
        },
        "deviation_pct_points": {
            "CORE": f"{core_delta:+.2f}",
            "SATELLITE": f"{satellite_delta:+.2f}",
            "maximum_absolute": f"{max_deviation:.2f}",
        },
        "transition_required": state == "TRANSITION_REQUIRED",
        "transition_exit_condition_met": (
            no_unassigned_met and core_exit_met and satellite_exit_met
        ),
        "transition_exit_requirements": {
            "all_required": True,
            "no_unassigned": {
                "required": True,
                "actual_count": unassigned_count,
                "met": no_unassigned_met,
            },
            "core_min_pct": {
                "required": f"{core_exit_min:.2f}",
                "actual": f"{core_actual:.2f}",
                "met": core_exit_met,
            },
            "satellite_max_pct": {
                "required": f"{satellite_exit_max:.2f}",
                "actual": f"{satellite_actual:.2f}",
                "met": satellite_exit_met,
            },
        },
        "transition_principle": policy["transition_principle"],
        "automatic_selling_allowed": policy["automatic_selling_allowed"],
    }


def _contribution_allocation(
    *,
    policy_record: JsonDict,
    role_summary: dict[str, JsonDict],
    contribution_minor: int,
) -> JsonDict:
    policy = policy_record["policy"]
    core_minor = int(
        (Decimal(str(role_summary["CORE"]["market_value"])) * MONEY_SCALE).to_integral_exact()
    )
    satellite_minor = int(
        (Decimal(str(role_summary["SATELLITE"]["market_value"])) * MONEY_SCALE).to_integral_exact()
    )
    current_total_minor = core_minor + satellite_minor
    projected_total_minor = current_total_minor + contribution_minor
    core_target = Decimal(str(policy["core_target_pct"])) / Decimal(100)
    desired_core_minor = int(
        (Decimal(projected_total_minor) * core_target).to_integral_value(rounding=ROUND_HALF_UP)
    )
    core_contribution_minor = min(
        contribution_minor,
        max(0, desired_core_minor - core_minor),
    )
    satellite_contribution_minor = contribution_minor - core_contribution_minor
    projected_core_minor = core_minor + core_contribution_minor
    projected_satellite_minor = satellite_minor + satellite_contribution_minor
    projected_core_pct = (
        Decimal(projected_core_minor) / Decimal(projected_total_minor) * Decimal(100)
    )
    projected_satellite_pct = (
        Decimal(projected_satellite_minor) / Decimal(projected_total_minor) * Decimal(100)
    )
    exit_condition_met = projected_core_pct >= Decimal(
        str(policy["transition_exit_core_min_pct"])
    ) and projected_satellite_pct <= Decimal(str(policy["transition_exit_satellite_max_pct"]))
    return {
        "contribution_amount": _money(contribution_minor),
        "role_allocations": {
            "CORE": _money(core_contribution_minor),
            "SATELLITE": _money(satellite_contribution_minor),
        },
        "projected": {
            "total_market_value": _money(projected_total_minor),
            "CORE": {
                "market_value": _money(projected_core_minor),
                "actual_pct": f"{projected_core_pct:.2f}",
            },
            "SATELLITE": {
                "market_value": _money(projected_satellite_minor),
                "actual_pct": f"{projected_satellite_pct:.2f}",
            },
        },
        "transition_exit_condition_met": exit_condition_met,
        "calculation_method": "TARGET_WEIGHT_GAP_FILL",
        "scope": "ROLE_ONLY",
        "automatic_selling_allowed": False,
    }


def _instrument_plan_items(
    *,
    assignment: JsonDict,
    role_allocations: JsonDict,
    data_quality: str,
    signal_policy: JsonDict | None = None,
    signal_states: dict[str, JsonDict] | None = None,
) -> list[JsonDict]:
    """Split role amounts only across an explicitly approved local allowlist."""
    items: list[JsonDict] = []
    for role in ("CORE", "SATELLITE"):
        role_minor = int((Decimal(str(role_allocations[role])) * MONEY_SCALE).to_integral_exact())
        if role_minor == 0:
            continue
        authorized = [
            config
            for config in assignment["instruments"]
            if config["role"] == role
            and config["status"] == "ACTIVE"
            and config["contribution_eligible"]
            and config["thesis_status"] == "ACTIVE"
        ]
        eligible = authorized
        gate_facts: JsonDict | None = None
        if role == "SATELLITE" and signal_policy is not None:
            states = signal_states or {}

            eligible = [
                config
                for config in authorized
                if (_current_signal_state(config, states) or {}).get("state") == "OPEN"
            ]
            gate_facts = {
                "policy_id": signal_policy["id"],
                "policy_version": signal_policy["version"],
                "metric": signal_policy["metric"],
                "entry_max_percentile_bps": signal_policy[
                    "entry_max_percentile_bps"
                ],
                "states": {
                    code: {
                        "snapshot_id": item["id"],
                        "state": item["state"],
                        "reason_code": item["reason_code"],
                        "current_config_match": any(
                            str(config["instrument_code"]) == code
                            and _current_signal_state(config, states) is not None
                            for config in authorized
                        ),
                    }
                    for code, item in states.items()
                },
            }
        eligible.sort(key=lambda item: (item["priority"], item["instrument_code"]))
        if not eligible:
            reason_code = "NO_ELIGIBLE_INSTRUMENT"
            selection_boundary = "INSTANCE_ALLOWLIST_ONLY"
            if role == "SATELLITE" and signal_policy is not None:
                selection_boundary = "INSTANCE_ALLOWLIST_AND_OPEN_SIGNAL"
                if not authorized:
                    reason_code = "NO_SIGNAL_AUTHORIZED_INSTRUMENT"
                elif not signal_states:
                    reason_code = "SATELLITE_SIGNAL_SNAPSHOT_REQUIRED"
                elif not any(
                    _current_signal_state(config, states) is not None
                    for config in authorized
                ):
                    reason_code = "SATELLITE_SIGNAL_SNAPSHOT_STALE"
                else:
                    reason_code = "NO_OPEN_SATELLITE_SIGNAL"
            items.append(
                {
                    "instrument_id": None,
                    "instrument_code": None,
                    "instrument_name": None,
                    "role": role,
                    "valuation_state": "NOT_EVALUATED",
                    "base_amount": "0.00",
                    "multiplier": "0.0000",
                    "candidate_amount": "0.00",
                    "reserved_amount": _money(role_minor),
                    "action": "REVIEW_REQUIRED",
                    "data_quality": data_quality,
                    "reason_code": reason_code,
                    "explanation_facts": {
                        "role_allocation": _money(role_minor),
                        "selection_boundary": selection_boundary,
                        "signal_gate": gate_facts,
                    },
                }
            )
            continue

        configured_targets = [
            int(item["target_weight_bps"])
            for item in eligible
            if item["target_weight_bps"] is not None
        ]
        use_targets = len(configured_targets) == len(eligible) and sum(configured_targets) > 0
        weights = configured_targets if use_targets else [1 for _item in eligible]
        weight_total = sum(weights)
        floors = [role_minor * weight // weight_total for weight in weights]
        remainders = [(role_minor * weight) % weight_total for weight in weights]
        cents_left = role_minor - sum(floors)
        remainder_order = sorted(
            range(len(eligible)),
            key=lambda index: (
                -remainders[index],
                eligible[index]["priority"],
                eligible[index]["instrument_code"],
            ),
        )
        for index in remainder_order[:cents_left]:
            floors[index] += 1

        for config, base_minor in zip(eligible, floors, strict=True):
            candidate_minor = base_minor
            reserved_minor = 0
            reason_code = (
                "INSTANCE_TARGET_WEIGHT" if use_targets else "ELIGIBLE_INSTRUMENT_EQUAL_SPLIT"
            )
            action = "CONTRIBUTE"
            maximum = config["maximum_amount_minor"]
            minimum = int(config["minimum_amount_minor"])
            if maximum is not None and candidate_minor > int(maximum):
                reserved_minor = candidate_minor - int(maximum)
                candidate_minor = int(maximum)
                reason_code = "INSTRUMENT_MAXIMUM_APPLIED"
            if candidate_minor < minimum:
                reserved_minor += candidate_minor
                candidate_minor = 0
                action = "REVIEW_REQUIRED"
                reason_code = "BELOW_INSTRUMENT_MINIMUM"
            items.append(
                {
                    "instrument_id": config["instrument_id"],
                    "instrument_code": config["instrument_code"],
                    "instrument_name": config["instrument_name"],
                    "role": role,
                    "valuation_state": "NAV_ONLY",
                    "base_amount": _money(base_minor),
                    "multiplier": "1.0000",
                    "candidate_amount": _money(candidate_minor),
                    "reserved_amount": _money(reserved_minor),
                    "action": action,
                    "data_quality": data_quality,
                    "reason_code": reason_code,
                    "explanation_facts": {
                        "contribution_eligible": True,
                        "target_weight_bps": config["target_weight_bps"],
                        "priority": config["priority"],
                        "benchmark_code": config["benchmark_code"],
                        "thesis_status": config["thesis_status"],
                        "signal_gate": (
                            gate_facts
                            if role == "SATELLITE" and signal_policy is not None
                            else None
                        ),
                    },
                }
            )
    return items


def _current_signal_state(
    config: JsonDict,
    states: dict[str, JsonDict],
) -> JsonDict | None:
    item = states.get(str(config["instrument_code"]))
    if item is None:
        return None
    facts = item.get("facts") or {}
    if (
        facts.get("strategy_config_id") != config["id"]
        or facts.get("strategy_config_updated_at") != config["updated_at"]
    ):
        return None
    return item


def _apply_executable_projection(
    *,
    plan: JsonDict,
    role_summary: dict[str, JsonDict],
    instrument_items: list[JsonDict],
    policy_record: JsonDict,
) -> JsonDict:
    """Project only executable candidate amounts; reserved cash changes no allocation."""
    executable_by_role = {"CORE": 0, "SATELLITE": 0}
    reserved_by_role = {"CORE": 0, "SATELLITE": 0}
    for item in instrument_items:
        role = str(item["role"])
        if role not in executable_by_role:
            continue
        executable_by_role[role] += int(
            (Decimal(str(item["candidate_amount"])) * MONEY_SCALE).to_integral_exact()
        )
        reserved_by_role[role] += int(
            (Decimal(str(item["reserved_amount"])) * MONEY_SCALE).to_integral_exact()
        )
    current_by_role = {
        role: int(
            (Decimal(str(role_summary[role]["market_value"])) * MONEY_SCALE).to_integral_exact()
        )
        for role in ("CORE", "SATELLITE")
    }
    executable_total = sum(executable_by_role.values())
    projected_total = sum(current_by_role.values()) + executable_total
    projected: JsonDict = {"total_market_value": _money(projected_total)}
    for role in ("CORE", "SATELLITE"):
        value = current_by_role[role] + executable_by_role[role]
        pct = (
            Decimal(value) / Decimal(projected_total) * Decimal(100)
            if projected_total
            else Decimal("0")
        )
        projected[role] = {
            "market_value": _money(value),
            "actual_pct": f"{pct:.2f}",
        }
    plan["executable_role_allocations"] = {
        role: _money(value) for role, value in executable_by_role.items()
    }
    plan["reserved_role_allocations"] = {
        role: _money(value) for role, value in reserved_by_role.items()
    }
    plan["projected"] = projected
    plan["projection_basis"] = "EXECUTABLE_CANDIDATE_AMOUNT_ONLY"
    policy = policy_record["policy"]
    plan["transition_exit_condition_met"] = Decimal(
        str(projected["CORE"]["actual_pct"])
    ) >= Decimal(str(policy["transition_exit_core_min_pct"])) and Decimal(
        str(projected["SATELLITE"]["actual_pct"])
    ) <= Decimal(str(policy["transition_exit_satellite_max_pct"]))
    return plan


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

    @staticmethod
    def _quality(source_type: str, verification_status: str) -> tuple[str, list[str]]:
        if source_type in {"OFFICIAL", "PLATFORM"} and verification_status == "VERIFIED":
            return "PASS", []
        return "WARNING", [
            "NAV is single-source or unverified; use the deterministic result conservatively"
        ]

    def _satellite_signal_gate(
        self,
        *,
        portfolio_id: str,
        strategy_assignment_id: str,
        as_of_date: str,
    ) -> tuple[JsonDict | None, dict[str, JsonDict]]:
        with self._connect() as connection:
            policy = connection.execute(
                """
                SELECT * FROM satellite_signal_policies
                WHERE portfolio_id=? AND status='ACTIVE'
                ORDER BY version DESC LIMIT 1
                """,
                (portfolio_id,),
            ).fetchone()
            if policy is None:
                return None, {}
            rows = connection.execute(
                """
                SELECT s.*, i.code AS instrument_code
                FROM satellite_signal_snapshots s
                JOIN instruments i ON i.id=s.instrument_id
                WHERE s.portfolio_id=? AND s.policy_id=?
                  AND s.strategy_assignment_id=? AND s.as_of_date=?
                ORDER BY s.created_at DESC, s.rowid DESC
                """,
                (portfolio_id, policy["id"], strategy_assignment_id, as_of_date),
            ).fetchall()
        states: dict[str, JsonDict] = {}
        for row in rows:
            code = str(row["instrument_code"])
            if code not in states:
                states[code] = {
                    "id": str(row["id"]),
                    "state": str(row["state"]),
                    "reason_code": str(row["reason_code"]),
                    "facts_hash": str(row["facts_hash"]),
                    "facts": json.loads(str(row["facts_json"])),
                }
        return (
            {
                "id": str(policy["id"]),
                "version": int(policy["version"]),
                "metric": str(policy["metric"]),
                "entry_max_percentile_bps": int(policy["entry_max_percentile_bps"]),
                "content_hash": str(policy["content_hash"]),
            },
            states,
        )

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
            "source_lineage": row["source_lineage"],
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
        source_lineage: str | None = None,
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
        normalized_source_lineage = resolve_source_lineage(
            normalized_source_name,
            source_ref,
            source_lineage,
        )
        nav_micros = _scaled(nav, NAV_SCALE, "nav")
        payload = {
            "instrument_code": normalized_code,
            "nav_date": nav_date.isoformat(),
            "nav_micros": nav_micros,
            "currency": normalized_currency,
            "source_type": normalized_source_type,
            "source_name": normalized_source_name,
            "source_ref": source_ref.strip() if source_ref else None,
            "source_lineage": normalized_source_lineage,
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
                  AND m.source_lineage = ?
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
                    payload["source_lineage"],
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
                    ingested_at, record_hash, source_lineage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    payload["source_lineage"],
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
                            "source_lineage": normalized_source_lineage,
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

    @staticmethod
    def _verification_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": row["id"],
            "status": row["status"],
            "nav_delta": _nav(int(row["nav_delta_micros"])),
            "verified_at": row["verified_at"],
            "actor_ref": row["actor_ref"],
            "record_hash": row["record_hash"],
            "primary_snapshot": {
                "id": row["primary_snapshot_id"],
                "instrument_code": row["instrument_code"],
                "instrument_name": row["instrument_name"],
                "nav_date": row["primary_nav_date"],
                "nav": _nav(int(row["primary_nav_micros"])),
                "source_type": row["primary_source_type"],
                "source_name": row["primary_source_name"],
                "source_ref": row["primary_source_ref"],
                "source_lineage": row["primary_source_lineage"],
            },
            "evidence_snapshot": {
                "id": row["evidence_snapshot_id"],
                "instrument_code": row["instrument_code"],
                "instrument_name": row["instrument_name"],
                "nav_date": row["evidence_nav_date"],
                "nav": _nav(int(row["evidence_nav_micros"])),
                "source_type": row["evidence_source_type"],
                "source_name": row["evidence_source_name"],
                "source_ref": row["evidence_source_ref"],
                "source_lineage": row["evidence_source_lineage"],
            },
        }

    @staticmethod
    def _verification_query() -> str:
        return """
            SELECT
                v.*,
                i.code AS instrument_code,
                i.name AS instrument_name,
                p.nav_date AS primary_nav_date,
                p.nav_micros AS primary_nav_micros,
                p.source_type AS primary_source_type,
                p.source_name AS primary_source_name,
                p.source_ref AS primary_source_ref,
                p.source_lineage AS primary_source_lineage,
                e.nav_date AS evidence_nav_date,
                e.nav_micros AS evidence_nav_micros,
                e.source_type AS evidence_source_type,
                e.source_name AS evidence_source_name,
                e.source_ref AS evidence_source_ref,
                e.source_lineage AS evidence_source_lineage
            FROM market_nav_verifications v
            JOIN market_nav_snapshots p ON p.id = v.primary_snapshot_id
            JOIN market_nav_snapshots e ON e.id = v.evidence_snapshot_id
            JOIN instruments i ON i.id = p.instrument_id
        """

    def record_nav_verification(
        self,
        *,
        instrument_code: str,
        nav_date_value: str,
        nav: str,
        source_type: str,
        source_name: str,
        source_ref: str,
        source_lineage: str,
        observed_at_value: str,
        currency: str = "CNY",
        actor_ref: str = "hermes",
    ) -> JsonDict:
        """Compare independent evidence with a stored aggregator observation."""
        normalized_code = instrument_code.strip().upper()
        normalized_source_type = source_type.strip().upper()
        normalized_source_name = source_name.strip()
        normalized_source_ref = source_ref.strip()
        normalized_source_lineage = resolve_source_lineage(
            normalized_source_name,
            normalized_source_ref,
            source_lineage,
        )
        if normalized_source_type not in {"OFFICIAL", "PLATFORM"}:
            raise LedgerError(
                "INVALID_VERIFICATION_SOURCE",
                "independent verification requires an OFFICIAL or PLATFORM source",
            )
        if not normalized_source_ref:
            raise LedgerError(
                "VERIFICATION_EVIDENCE_REQUIRED",
                "source_ref is required for independent market data verification",
            )
        if normalized_source_lineage == "UNKNOWN":
            raise LedgerError(
                "SOURCE_LINEAGE_UNKNOWN",
                "verification evidence requires a registered upstream publisher",
            )
        try:
            nav_date = date.fromisoformat(nav_date_value)
        except ValueError as exc:
            raise LedgerError("INVALID_DATE", "nav_date must be an ISO date") from exc

        with self._connect() as connection:
            primary = connection.execute(
                """
                SELECT m.*, i.code AS instrument_code, i.name AS instrument_name
                FROM market_nav_snapshots m
                JOIN instruments i ON i.id = m.instrument_id
                WHERE i.code = ?
                  AND m.nav_date = ?
                  AND m.source_type = 'AGGREGATOR'
                ORDER BY m.observed_at DESC, m.rowid DESC
                LIMIT 1
                """,
                (normalized_code, nav_date.isoformat()),
            ).fetchone()
        if primary is None:
            raise LedgerError(
                "PRIMARY_NAV_MISSING",
                "no aggregator NAV exists for the requested instrument and date",
                http_status=409,
            )
        primary_lineage = str(primary["source_lineage"])
        if primary_lineage == "UNKNOWN":
            raise LedgerError(
                "PRIMARY_SOURCE_LINEAGE_UNKNOWN",
                "the primary NAV publisher lineage is unknown and cannot be corroborated",
            )
        if primary_lineage == normalized_source_lineage:
            raise LedgerError(
                "SOURCE_NOT_INDEPENDENT",
                "verification evidence resolves to the same upstream publisher",
                details={"source_lineage": primary_lineage},
            )

        evidence_result = self.record_nav_snapshot(
            instrument_code=normalized_code,
            nav_date_value=nav_date.isoformat(),
            nav=nav,
            currency=currency,
            source_type=normalized_source_type,
            source_name=normalized_source_name,
            source_ref=normalized_source_ref,
            source_lineage=normalized_source_lineage,
            verification_status="UNVERIFIED",
            observed_at_value=observed_at_value,
            actor_ref=actor_ref,
        )
        evidence = evidence_result["snapshot"]
        primary_micros = int(primary["nav_micros"])
        evidence_micros = _scaled(str(evidence["nav"]), NAV_SCALE, "nav")
        delta = abs(primary_micros - evidence_micros)
        status = "MATCH" if delta == 0 else "CONFLICT"
        record_payload = {
            "primary_snapshot_id": str(primary["id"]),
            "evidence_snapshot_id": str(evidence["id"]),
            "status": status,
            "nav_delta_micros": delta,
        }
        record_hash = _canonical_hash(record_payload)
        verified_at = _iso(self._now())

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                self._verification_query() + " WHERE v.record_hash = ?",
                (record_hash,),
            ).fetchone()
            created = row is None
            if row is None:
                verification_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO market_nav_verifications (
                        id, primary_snapshot_id, evidence_snapshot_id, status,
                        nav_delta_micros, verified_at, actor_ref, record_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        verification_id,
                        record_payload["primary_snapshot_id"],
                        record_payload["evidence_snapshot_id"],
                        status,
                        delta,
                        verified_at,
                        actor_ref,
                        record_hash,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        id, occurred_at, actor_type, actor_ref, action, entity_type,
                        entity_id, after_hash, details_json, trace_id
                    ) VALUES (?, ?, 'AGENT', ?, ?, 'market_nav_verification', ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        verified_at,
                        actor_ref,
                        "MARKET_NAV_CORROBORATED" if status == "MATCH" else "MARKET_NAV_CONFLICT",
                        verification_id,
                        record_hash,
                        json.dumps(
                            {
                                "instrument_code": normalized_code,
                                "nav_date": nav_date.isoformat(),
                                "primary_source_name": primary["source_name"],
                                "evidence_source_name": normalized_source_name,
                                "status": status,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        str(uuid4()),
                    ),
                )
                row = connection.execute(
                    self._verification_query() + " WHERE v.id = ?",
                    (verification_id,),
                ).fetchone()
            connection.commit()
            assert row is not None
            result = self._verification_data(row)
            result["created"] = created
            result["data_quality"] = "PASS" if status == "MATCH" else "SOURCE_ERROR"
            result["warnings"] = (
                []
                if status == "MATCH"
                else ["Independent NAV evidence conflicts with the primary observation"]
            )
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_nav_verifications(
        self, *, instrument_code: str | None = None, limit: int = 100
    ) -> list[JsonDict]:
        if limit < 1 or limit > 500:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 500")
        query = self._verification_query() + " WHERE 1 = 1"
        parameters: list[Any] = []
        if instrument_code:
            query += " AND i.code = ?"
            parameters.append(instrument_code.strip().upper())
        query += " ORDER BY v.verified_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            return [
                self._verification_data(row)
                for row in connection.execute(query, parameters).fetchall()
            ]

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
                corroboration_row = connection.execute(
                    self._verification_query()
                    + """
                      WHERE p.instrument_id = ?
                        AND p.nav_date = ?
                        AND v.status = 'MATCH'
                        AND p.nav_micros = e.nav_micros
                      ORDER BY v.verified_at DESC
                      LIMIT 1
                    """,
                    (holding["instrument_id"], row["nav_date"]),
                ).fetchone()
                corroboration = (
                    self._verification_data(corroboration_row)
                    if corroboration_row is not None
                    else None
                )
                if corroboration is not None:
                    snapshot["data_quality"] = "PASS"
                    snapshot["warnings"] = []
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
                        "corroboration": (
                            {
                                **corroboration,
                                "source_count": 2,
                            }
                            if corroboration is not None
                            else None
                        ),
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

    def portfolio_brief(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        as_of_date_value: str | None = None,
    ) -> JsonDict:
        """Return facts plus explicit decision boundaries for portfolio narration."""
        valuation = self.portfolio_valuation(
            portfolio_id=portfolio_id,
            account_id=account_id,
            as_of_date_value=as_of_date_value,
        )
        portfolios = {str(item["id"]): item for item in self._ledger.list_portfolios()}
        accounts = {
            str(item["id"]): item for item in self._ledger.list_accounts(portfolio_id=portfolio_id)
        }
        portfolio = portfolios.get(portfolio_id)
        account = accounts.get(account_id)
        if portfolio is None or account is None:
            raise LedgerError(
                "INVESTMENT_CONTEXT_NOT_FOUND",
                "portfolio or account is not active",
                http_status=404,
            )
        try:
            assignment = self._strategy.get_assignment(portfolio_id=portfolio_id)
            strategy_configs = {
                str(item["instrument_code"]): item
                for item in assignment["instruments"]
            }
        except LedgerError as exc:
            if exc.code != "STRATEGY_NOT_ASSIGNED":
                raise
            strategy_configs = {}

        role_summary: dict[str, JsonDict] = {}
        unassigned: list[JsonDict] = []
        source_lineages: set[str] = set()
        for position in valuation["positions"]:
            holding = position["holding"]
            role = str(holding["role"])
            group = role_summary.setdefault(
                role,
                {
                    "position_count": 0,
                    "market_value": "0.00" if valuation["totals"] is not None else None,
                    "market_value_pct": "0.00" if valuation["totals"] is not None else None,
                    "assessment": "NOT_AVAILABLE",
                    "reason_code": "ALLOCATION_POLICY_NOT_APPLICABLE",
                },
            )
            group["position_count"] = int(group["position_count"]) + 1
            snapshot = position.get("nav_snapshot")
            if isinstance(snapshot, dict):
                source_lineages.add(str(snapshot["source_lineage"]))
            if valuation["totals"] is not None:
                market_value = Decimal(str(group["market_value"])) + Decimal(
                    str(position["market_value"])
                )
                market_value_pct = Decimal(str(group["market_value_pct"])) + Decimal(
                    str(position["weight_pct"])
                )
                group["market_value"] = f"{market_value:.2f}"
                group["market_value_pct"] = f"{market_value_pct:.2f}"
            if role == "UNASSIGNED":
                unassigned.append(
                    {
                        "instrument_code": holding["instrument_code"],
                        "instrument_name": holding["instrument_name"],
                        "finding_code": "ROLE_UNASSIGNED",
                        "severity": "INFO",
                        "mutation_available": True,
                        "mutation_tool": "instrument_role_update",
                    }
                )
            config = strategy_configs.get(str(holding["instrument_code"]))
            if config is None:
                position["policy_assessment"] = {
                    "performance": "NOT_AVAILABLE",
                    "risk": "NOT_AVAILABLE",
                    "sell_rule": "NOT_EVALUATED",
                    "reason_code": "INSTRUMENT_NOT_IN_ACTIVE_STRATEGY",
                    "candidate_rule_count": 10,
                    "configured_rule_count": 0,
                    "configured_rule_codes": [],
                }
                continue
            lifecycle_rules = dict(config["lifecycle_rules"])
            configured_rule_codes = [
                "THESIS_REVIEW_REQUIRED",
                "SELL_02_THESIS_INVALID",
            ]
            if config["hard_stop_return_bps"] is not None:
                configured_rule_codes.append("SELL_01_HARD_STOP")
            if config["maximum_position_weight_bps"] is not None:
                configured_rule_codes.append("SELL_03_REBALANCE")
            if {
                "replacement_min_score_delta_bps",
                "replacement_min_consecutive_periods",
            }.issubset(lifecycle_rules):
                configured_rule_codes.append("SELL_04_REPLACE")
            if {
                "underperformance_threshold_bps",
                "underperformance_min_days",
            }.issubset(lifecycle_rules):
                configured_rule_codes.append("SELL_05_UNDERPERFORMANCE")
            if {
                "take_profit_return_bps",
                "take_profit_min_holding_days",
            }.issubset(lifecycle_rules):
                configured_rule_codes.append("SELL_06_TAKE_PROFIT")
            if "objective_sell_fraction_bps" in lifecycle_rules:
                configured_rule_codes.append("SELL_07_OBJECTIVE_COMPLETE")
            if any(
                key in lifecycle_rules
                for key in ("max_tracking_error_bps", "max_expense_ratio_bps")
            ):
                configured_rule_codes.append("CORE_TOOL_QUALITY")
            if "liquidity_priority" in lifecycle_rules:
                configured_rule_codes.append("SELL_08_LIQUIDITY")
            configured_count = len(configured_rule_codes)
            complete = configured_count == 10
            position["policy_assessment"] = {
                "performance": "NOT_AVAILABLE",
                "risk": "CONFIGURED" if complete else "PARTIAL",
                "sell_rule": "CONFIGURED" if complete else "PARTIALLY_CONFIGURED",
                "reason_code": (
                    "DETERMINISTIC_RULES_CONFIGURED"
                    if complete
                    else "DETERMINISTIC_RULES_PARTIALLY_CONFIGURED"
                ),
                "candidate_rule_count": 10,
                "configured_rule_count": configured_count,
                "configured_rule_codes": configured_rule_codes,
            }

        for role in ("CORE", "SATELLITE"):
            role_summary.setdefault(
                role,
                {
                    "position_count": 0,
                    "market_value": "0.00" if valuation["totals"] is not None else None,
                    "market_value_pct": "0.00" if valuation["totals"] is not None else None,
                    "assessment": "NOT_AVAILABLE",
                    "reason_code": "ALLOCATION_POLICY_NOT_EVALUATED",
                },
            )
        policy_record = self._ledger.get_allocation_policy(portfolio_id=portfolio_id)
        allocation = _allocation_assessment(
            policy_record=policy_record,
            role_summary=role_summary,
            valuation_complete=valuation["totals"] is not None,
        )

        capabilities = {
            "allocation_assessment": {
                "available": True,
                "reason_code": "VERSIONED_POLICY_CONFIGURED",
            },
            "risk_assessment": {
                "available": True,
                "reason_code": "CONFIGURED_RULE_SCAN_AVAILABLE",
                "tool": "risk_scan_run",
            },
            "sell_proposal": {
                "available": True,
                "reason_code": "RULE_HITS_CREATE_REVIEW_ONLY_PROPOSALS",
                "tool": "sell_proposal_list",
            },
            "weekly_plan": {
                "available": True,
                "reason_code": "REQUIRES_EXPLICIT_CONTRIBUTION_AMOUNT",
                "tool": "weekly_plan_preview",
            },
            "instrument_role_update": {
                "available": True,
                "reason_code": "AVAILABLE_WITH_EXPECTED_CURRENT_ROLE",
            },
            "strategy_instrument_configuration": {
                "available": True,
                "reason_code": "DRAFT_CONFIRM_COMMIT_REQUIRED",
                "tool": "strategy_instrument_config_draft_create",
            },
        }
        display_lines = [
            "投资状况概览",
            f"数据日期: {valuation['as_of_date']}",
            f"数据质量: {valuation['data_quality']}",
            "",
            "持仓事实:",
        ]
        for position in valuation["positions"]:
            holding = position["holding"]
            line = (
                f"- {holding['instrument_code']} {holding['instrument_name']} | "
                f"角色 {holding['role']} | 份额 {holding['total_shares']} | "
                f"成本 {_display_money(str(holding['cost_amount']))}"
            )
            if position["market_value"] is not None:
                line += (
                    f" | 市值 {_display_money(str(position['market_value']))} | "
                    f"未实现盈亏 {_display_money(str(position['unrealized_pnl']))} | "
                    f"收益率 {position['return_pct']}% | 权重 {position['weight_pct']}%"
                )
            else:
                line += f" | 估值不可用 ({position.get('error', 'SOURCE_ERROR')})"
            display_lines.append(line)
        if valuation["totals"] is not None:
            totals = valuation["totals"]
            display_lines.extend(
                [
                    "",
                    (
                        f"合计: 市值 {_display_money(str(totals['market_value']))} | "
                        f"成本 {_display_money(str(totals['cost_amount']))} | "
                        f"未实现盈亏 {_display_money(str(totals['unrealized_pnl']))}"
                    ),
                ]
            )
        if valuation["warnings"]:
            display_lines.extend(["", "数据限制:"])
            display_lines.extend(
                f"- {_display_warning(warning)}" for warning in valuation["warnings"]
            )
        if allocation["available"]:
            policy = policy_record["policy"]
            display_lines.extend(
                [
                    "",
                    "配置评估:",
                    (
                        f"- 目标: CORE {policy['core_target_pct']}% | "
                        f"SATELLITE {policy['satellite_target_pct']}%"
                    ),
                    (
                        f"- 实际: CORE {allocation['actual']['CORE']}% | "
                        f"SATELLITE {allocation['actual']['SATELLITE']}%"
                    ),
                    (
                        "- 偏离: "
                        f"CORE {allocation['deviation_pct_points']['CORE']} 个百分点 | "
                        "SATELLITE "
                        f"{allocation['deviation_pct_points']['SATELLITE']} 个百分点"
                    ),
                    (f"- 状态: {allocation['state']} ({allocation['reason_code']})"),
                    "- 过渡原则: 优先使用新增资金，不自动卖出。",  # noqa: RUF001
                ]
            )
        display_lines.extend(
            [
                "",
                "当前能力边界:",
                "- 确定性风险扫描可用; 只评估策略实例中明确配置的规则.",
                "- 规则命中只生成待复核提案, 不创建或执行交易.",
                "- 周度资金计划预览可用; 必须由用户明确提供本周新增资金金额.",
                "- 角色变更工具可用; 仅在用户明确指定新角色时调用.",
            ]
        )
        return {
            "as_of_date": valuation["as_of_date"],
            "context": {"portfolio": portfolio, "account": account},
            "valuation": valuation,
            "role_summary": role_summary,
            "allocation_assessment": allocation,
            "factual_findings": unassigned,
            "capabilities": capabilities,
            "source_evidence": {
                "upstream_lineages": sorted(source_lineages),
                "independence_assessment": (
                    "NO_EVIDENCE"
                    if not source_lineages
                    else ("SINGLE_UPSTREAM" if len(source_lineages) == 1 else "MULTIPLE_UPSTREAMS")
                ),
            },
            "narrative_contract": {
                "mode": "EXACT_TEXT",
                "response_field": "display_text",
                "additions_allowed": False,
                "prohibited_inferences": [
                    "ALLOCATION_IMBALANCE",
                    "PERFORMANCE_ADJECTIVE",
                    "RISK_TRIGGER",
                    "SELL_TRIGGER",
                    "DCA_RECOMMENDATION",
                    "UNAVAILABLE_MUTATION",
                ],
                "instruction": (
                    "Return display_text exactly. Do not add headings, summaries, interpretations, "
                    "priorities, recommendations, questions, or next actions."
                ),
            },
            "display_text": "\n".join(display_lines),
        }

    def weekly_plan_preview(
        self,
        *,
        portfolio_id: str,
        account_id: str,
        contribution_amount: str,
        as_of_date_value: str | None = None,
    ) -> JsonDict:
        """Create a deterministic instrument preview from an approved instance allowlist."""
        contribution_minor = _scaled(
            contribution_amount,
            MONEY_SCALE,
            "contribution_amount",
        )
        brief = self.portfolio_brief(
            portfolio_id=portfolio_id,
            account_id=account_id,
            as_of_date_value=as_of_date_value,
        )
        valuation = brief["valuation"]
        allocation = brief["allocation_assessment"]
        if valuation["totals"] is None:
            return {
                "available": False,
                "state": "BLOCKED",
                "reason_code": "VALUATION_UNAVAILABLE",
                "as_of_date": valuation["as_of_date"],
                "data_quality": valuation["data_quality"],
                "warnings": valuation["warnings"],
                "narrative_contract": {
                    "mode": "EXACT_TEXT",
                    "response_field": "display_text",
                    "additions_allowed": False,
                },
                "display_text": (
                    "周度资金计划预览\n"
                    f"数据日期: {valuation['as_of_date']}\n"
                    f"数据质量: {valuation['data_quality']}\n\n"
                    "状态: BLOCKED (VALUATION_UNAVAILABLE)\n"
                    "估值数据不可用，不能生成任何金额分配结论。"  # noqa: RUF001
                ),
            }
        if allocation["state"] == "BLOCKED_UNASSIGNED":
            return {
                "available": False,
                "state": "BLOCKED",
                "reason_code": "ROLE_UNASSIGNED",
                "as_of_date": valuation["as_of_date"],
                "data_quality": valuation["data_quality"],
                "warnings": valuation["warnings"],
                "narrative_contract": {
                    "mode": "EXACT_TEXT",
                    "response_field": "display_text",
                    "additions_allowed": False,
                },
                "display_text": (
                    "周度资金计划预览\n"
                    f"数据日期: {valuation['as_of_date']}\n"
                    f"数据质量: {valuation['data_quality']}\n\n"
                    "状态: BLOCKED (ROLE_UNASSIGNED)\n"
                    "存在未分配角色的持仓，不能生成资金分配结论。"  # noqa: RUF001
                ),
            }

        plan = _contribution_allocation(
            policy_record=allocation["policy"],
            role_summary=brief["role_summary"],
            contribution_minor=contribution_minor,
        )
        assignment = self._strategy.get_assignment(portfolio_id=portfolio_id)
        signal_policy, signal_states = self._satellite_signal_gate(
            portfolio_id=portfolio_id,
            strategy_assignment_id=str(assignment["id"]),
            as_of_date=str(valuation["as_of_date"]),
        )
        instrument_items = _instrument_plan_items(
            assignment=assignment,
            role_allocations=plan["role_allocations"],
            data_quality=valuation["data_quality"],
            signal_policy=signal_policy,
            signal_states=signal_states,
        )
        candidate_minor = sum(
            int((Decimal(str(item["candidate_amount"])) * MONEY_SCALE).to_integral_exact())
            for item in instrument_items
        )
        reserved_minor = sum(
            int((Decimal(str(item["reserved_amount"])) * MONEY_SCALE).to_integral_exact())
            for item in instrument_items
        )
        plan = _apply_executable_projection(
            plan=plan,
            role_summary=brief["role_summary"],
            instrument_items=instrument_items,
            policy_record=allocation["policy"],
        )
        plan.update(
            {
                "scope": "INSTRUMENT",
                "instrument_items": instrument_items,
                "candidate_amount": _money(candidate_minor),
                "reserved_amount": _money(reserved_minor),
                "selection_boundary": (
                    "INSTANCE_ALLOWLIST_AND_OPEN_SIGNAL"
                    if signal_policy is not None
                    else "INSTANCE_ALLOWLIST_ONLY"
                ),
                "satellite_signal_policy": signal_policy,
                "satellite_signal_snapshot_count": len(signal_states),
            }
        )
        if reserved_minor:
            plan_state = "REVIEW_REQUIRED"
            plan_reason_code = (
                "NO_EXECUTABLE_INSTRUMENT" if candidate_minor == 0 else "PARTIALLY_EXECUTABLE"
            )
        else:
            plan_state = (
                "TRANSITION_CONTRIBUTION"
                if allocation["state"] in {"TRANSITION_REQUIRED", "OUTSIDE_TOLERANCE"}
                else "MAINTENANCE_CONTRIBUTION"
            )
            plan_reason_code = allocation["reason_code"]
        warnings = valuation["warnings"]
        display_lines = [
            "周度资金计划预览",
            f"数据日期: {valuation['as_of_date']}",
            f"数据质量: {valuation['data_quality']}",
            (
                f"策略版本: {allocation['policy']['strategy_key']} "
                f"v{allocation['policy']['version']}"
            ),
            "",
            f"本周新增资金: {_display_money(plan['contribution_amount'])}",
            f"计划状态: {plan_state}",
            (
                "舱位分配: "
                f"CORE {_display_money(plan['role_allocations']['CORE'])} | "
                f"SATELLITE {_display_money(plan['role_allocations']['SATELLITE'])}"
            ),
            (
                "可执行金额: "
                f"{_display_money(plan['candidate_amount'])} | "
                f"保留金额: {_display_money(plan['reserved_amount'])}"
            ),
            "标的计划:",
        ]
        for item in instrument_items:
            if item["instrument_code"] is None:
                display_lines.append(
                    f"- {item['role']}: 保留 "
                    f"{_display_money(item['reserved_amount'])} "
                    f"({item['reason_code']})"
                )
            else:
                display_lines.append(
                    f"- {item['instrument_code']} {item['instrument_name']} | "
                    f"{item['role']} | 候选 "
                    f"{_display_money(item['candidate_amount'])} | "
                    f"{item['action']} ({item['reason_code']})"
                )
        display_lines.extend(
            [
                (
                    "按可执行金额投后预计: "
                    f"CORE {plan['projected']['CORE']['actual_pct']}% | "
                    f"SATELLITE {plan['projected']['SATELLITE']['actual_pct']}%"
                ),
                (
                    "过渡退出条件: "
                    + ("已满足" if plan["transition_exit_condition_met"] else "尚未满足")
                ),
                "",
                "执行边界:",
                "- 标的只能来自当前策略实例中明确批准可定投的配置.",
                (
                    "- 卫星标的还必须具有同日、同政策且状态为 OPEN 的信号快照."
                    if signal_policy is not None
                    else "- 当前没有已批准的卫星信号政策."
                ),
                "- 不创建交易草稿，不代表已买入，不自动卖出.",  # noqa: RUF001
            ]
        )
        if warnings:
            display_lines.extend(["", "数据限制:"])
            display_lines.extend(f"- {_display_warning(item)}" for item in warnings)
        return {
            "available": True,
            "state": plan_state,
            "reason_code": plan_reason_code,
            "as_of_date": valuation["as_of_date"],
            "data_quality": valuation["data_quality"],
            "warnings": warnings,
            "policy": allocation["policy"],
            "current_allocation": allocation,
            "plan": plan,
            "execution_boundary": {
                "instrument_selection": "APPROVED_INSTANCE_ALLOWLIST_ONLY",
                "transaction_draft_created": False,
                "trade_executed": False,
                "automatic_selling_allowed": False,
            },
            "narrative_contract": {
                "mode": "EXACT_TEXT",
                "response_field": "display_text",
                "additions_allowed": False,
                "instruction": (
                    "Return display_text exactly. Do not replace, add, or remove instruments; "
                    "do not create transaction drafts, claim execution, or add recommendations."
                ),
            },
            "display_text": "\n".join(display_lines),
        }
