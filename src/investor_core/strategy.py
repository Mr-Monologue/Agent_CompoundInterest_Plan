"""Reusable strategy definitions and explicitly approved portfolio instances."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
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
                    "approved_by": str(config["approved_by"]),
                    "approved_at": str(config["approved_at"]),
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
                before_hash=(
                    str(current["instance_config_hash"])
                    if current is not None
                    else None
                ),
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
    ) -> JsonDict:
        """Protected operation: approve one instrument for a strategy instance."""
        normalized_role = role.strip().upper()
        normalized_thesis = thesis_status.strip().upper()
        if normalized_role not in {"CORE", "SATELLITE", "CASH", "WATCH", "UNASSIGNED"}:
            raise LedgerError("INVALID_ROLE", "unsupported strategy instrument role")
        if normalized_thesis not in {"ACTIVE", "REVIEW_REQUIRED", "INVALID"}:
            raise LedgerError("INVALID_THESIS_STATUS", "unsupported thesis status")
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
                        approved_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)
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
                        approved_by = ?, approved_at = ?, updated_at = ?
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
            target_weight_bps=(
                config["target_weight_bps"] if config is not None else None
            ),
            priority=int(config["priority"]) if config is not None else 100,
            minimum_amount_minor=(
                int(config["minimum_amount_minor"]) if config is not None else 1
            ),
            maximum_amount_minor=(
                config["maximum_amount_minor"] if config is not None else None
            ),
            benchmark_code=(
                config["benchmark_code"] if config is not None else None
            ),
            thesis_status=(
                str(config["thesis_status"]) if config is not None else "ACTIVE"
            ),
            approved_by=actor_ref,
            reason=reason,
        )
        return {
            "portfolio_id": portfolio_id,
            "instrument_code": normalized_code,
            "previous_role": current_role,
            "changed": current_role != role.strip().upper(),
            "assignment": updated,
        }
