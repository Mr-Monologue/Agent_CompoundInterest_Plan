"""Governed satellite valuation-signal policies and immutable snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from investor_core.config import Settings
from investor_core.ledger import JsonDict, LedgerError
from investor_core.risk import RiskService
from investor_core.strategy import StrategyService

SIGNAL_CALCULATION_VERSION = "satellite-valuation-signal-v1"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SignalService:
    """Evaluate signals without changing strategy authorization or financial state."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.settings = settings
        self._now = now
        self._strategy = StrategyService(settings, now=now)
        self._risk = RiskService(settings, now=now)

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
                "USER" if actor_ref not in {"cron", "hermes"} else "AGENT",
                actor_ref,
                action,
                entity_type,
                entity_id,
                _hash(details),
                _json(details),
                str(uuid4()),
            ),
        )

    @staticmethod
    def _policy_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "portfolio_id": str(row["portfolio_id"]),
            "version": int(row["version"]),
            "status": str(row["status"]),
            "role": "SATELLITE",
            "metric": str(row["metric"]),
            "entry_max_percentile_bps": int(row["entry_max_percentile_bps"]),
            "entry_max_percentile": f"{int(row['entry_max_percentile_bps']) / 100:.2f}",
            "lookback_days": int(row["lookback_days"]),
            "minimum_sample_count": int(row["minimum_sample_count"]),
            "maximum_observation_age_days": int(row["maximum_observation_age_days"]),
            "allow_warning_data": bool(row["allow_warning_data"]),
            "content_hash": str(row["content_hash"]),
            "reason": str(row["reason"]),
            "approved_by": str(row["approved_by"]),
            "approved_at": str(row["approved_at"]),
            "automatic_trade": False,
        }

    def get_active_policy(self, *, portfolio_id: str) -> JsonDict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM satellite_signal_policies
                WHERE portfolio_id=? AND status='ACTIVE'
                ORDER BY version DESC LIMIT 1
                """,
                (portfolio_id,),
            ).fetchone()
        return self._policy_data(row) if row is not None else None

    def list_policies(self, *, portfolio_id: str | None = None) -> list[JsonDict]:
        query = "SELECT * FROM satellite_signal_policies WHERE 1=1"
        params: list[object] = []
        if portfolio_id:
            query += " AND portfolio_id=?"
            params.append(portfolio_id)
        query += " ORDER BY approved_at DESC, version DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._policy_data(row) for row in rows]

    def create_policy_draft(
        self,
        *,
        portfolio_id: str,
        metric: str,
        entry_max_percentile_bps: int,
        lookback_days: int,
        minimum_sample_count: int,
        maximum_observation_age_days: int,
        allow_warning_data: bool,
        reason: str,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        normalized_metric = metric.strip().upper()
        if normalized_metric not in {"PE", "PB"}:
            raise LedgerError("INVALID_SIGNAL_METRIC", "signal metric must be PE or PB")
        if not 0 <= entry_max_percentile_bps <= 10000:
            raise LedgerError(
                "INVALID_SIGNAL_PERCENTILE",
                "entry percentile must be between 0 and 10000 basis points",
            )
        if lookback_days < 30 or lookback_days > 7305:
            raise LedgerError("INVALID_SIGNAL_LOOKBACK", "lookback must be 30 to 7305 days")
        if minimum_sample_count < 1 or minimum_sample_count > 10000:
            raise LedgerError(
                "INVALID_SIGNAL_SAMPLE_COUNT", "minimum sample count must be 1 to 10000"
            )
        if maximum_observation_age_days < 0 or maximum_observation_age_days > 365:
            raise LedgerError(
                "INVALID_SIGNAL_OBSERVATION_AGE",
                "maximum observation age must be 0 to 365 days",
            )
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise LedgerError("SIGNAL_POLICY_REASON_REQUIRED", "policy reason is required")
        assignment = self._strategy.get_assignment(portfolio_id=portfolio_id)
        current = self.get_active_policy(portfolio_id=portfolio_id)
        request = {
            "portfolio_id": portfolio_id,
            "strategy_assignment_id": assignment["id"],
            "role": "SATELLITE",
            "metric": normalized_metric,
            "entry_max_percentile_bps": entry_max_percentile_bps,
            "lookback_days": lookback_days,
            "minimum_sample_count": minimum_sample_count,
            "maximum_observation_age_days": maximum_observation_age_days,
            "allow_warning_data": allow_warning_data,
            "reason": normalized_reason,
        }
        now = self._now()
        expires_at = now + timedelta(minutes=self.settings.confirmation_ttl_minutes)
        token = secrets.token_urlsafe(24)
        draft_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO satellite_signal_policy_drafts (
                    id, portfolio_id, request_json, request_hash, before_json,
                    confirmation_digest, status, created_by, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    draft_id,
                    portfolio_id,
                    _json(request),
                    _hash(request),
                    _json(current or {}),
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
                "before": current,
                "proposed": request,
                "expires_at": _iso(expires_at),
                "execution_status": "NOT_APPLIED",
            },
            "confirmation_token": token,
            "warnings": [
                "This policy can gate future weekly-plan candidates but never creates trades"
            ],
        }

    def get_policy_draft(self, *, draft_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM satellite_signal_policy_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "SIGNAL_POLICY_DRAFT_NOT_FOUND",
                    "signal policy draft was not found",
                    http_status=404,
                )
            status = str(row["status"])
            if status == "PENDING" and _parse_iso(str(row["expires_at"])) <= self._now():
                connection.execute(
                    "UPDATE satellite_signal_policy_drafts SET status='EXPIRED' WHERE id=?",
                    (draft_id,),
                )
                status = "EXPIRED"
            return {
                "id": str(row["id"]),
                "portfolio_id": str(row["portfolio_id"]),
                "status": status,
                "before": json.loads(str(row["before_json"])),
                "proposed": json.loads(str(row["request_json"])),
                "created_at": str(row["created_at"]),
                "expires_at": str(row["expires_at"]),
                "committed_at": row["committed_at"],
                "committed_by": row["committed_by"],
                "committed_policy_id": row["committed_policy_id"],
            }

    def commit_policy_draft(
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
                "SELECT * FROM satellite_signal_policy_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "SIGNAL_POLICY_DRAFT_NOT_FOUND",
                    "signal policy draft was not found",
                    http_status=404,
                )
            if str(row["status"]) == "COMMITTED":
                policy = connection.execute(
                    "SELECT * FROM satellite_signal_policies WHERE id=?",
                    (row["committed_policy_id"],),
                ).fetchone()
                connection.commit()
                assert policy is not None
                return {
                    "draft": self.get_policy_draft(draft_id=draft_id),
                    "policy": self._policy_data(policy),
                    "idempotent_replay": True,
                }
            if str(row["status"]) != "PENDING":
                raise LedgerError(
                    "INVALID_SIGNAL_POLICY_DRAFT_STATUS",
                    "signal policy draft is not pending",
                    http_status=409,
                )
            if _parse_iso(str(row["expires_at"])) <= self._now():
                connection.execute(
                    "UPDATE satellite_signal_policy_drafts SET status='EXPIRED' WHERE id=?",
                    (draft_id,),
                )
                raise LedgerError(
                    "CONFIRMATION_EXPIRED",
                    "signal policy confirmation expired",
                    http_status=409,
                )
            if not hmac.compare_digest(
                str(row["confirmation_digest"]), _token_digest(confirmation_token)
            ):
                raise LedgerError(
                    "CONFIRMATION_MISMATCH",
                    "confirmation token does not match this draft",
                    http_status=409,
                )
            request = json.loads(str(row["request_json"]))
            portfolio_id = str(row["portfolio_id"])
            assignment = connection.execute(
                """
                SELECT id FROM strategy_assignments
                WHERE portfolio_id=? AND status='ACTIVE'
                """,
                (portfolio_id,),
            ).fetchone()
            if assignment is None or str(assignment["id"]) != str(
                request["strategy_assignment_id"]
            ):
                raise LedgerError(
                    "SIGNAL_POLICY_STRATEGY_CHANGED",
                    "active strategy changed after the signal policy was drafted",
                    http_status=409,
                )
            latest = connection.execute(
                """
                SELECT COALESCE(MAX(version), 0) FROM satellite_signal_policies
                WHERE portfolio_id=?
                """,
                (portfolio_id,),
            ).fetchone()
            version = int(latest[0]) + 1
            content = {key: value for key, value in request.items() if key != "reason"}
            content["version"] = version
            content_hash = _hash(content)
            timestamp = _iso(self._now())
            policy_id = str(uuid4())
            connection.execute(
                """
                UPDATE satellite_signal_policies
                SET status='SUPERSEDED'
                WHERE portfolio_id=? AND status='ACTIVE'
                """,
                (portfolio_id,),
            )
            connection.execute(
                """
                INSERT INTO satellite_signal_policies (
                    id, portfolio_id, version, status, metric,
                    entry_max_percentile_bps, lookback_days, minimum_sample_count,
                    maximum_observation_age_days, allow_warning_data, content_hash,
                    reason, approved_by, approved_at
                ) VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id,
                    portfolio_id,
                    version,
                    request["metric"],
                    request["entry_max_percentile_bps"],
                    request["lookback_days"],
                    request["minimum_sample_count"],
                    request["maximum_observation_age_days"],
                    int(bool(request["allow_warning_data"])),
                    content_hash,
                    request["reason"],
                    confirmed_by.strip(),
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE satellite_signal_policy_drafts
                SET status='COMMITTED', committed_at=?, committed_by=?,
                    committed_policy_id=?
                WHERE id=?
                """,
                (timestamp, confirmed_by.strip(), policy_id, draft_id),
            )
            self._audit(
                connection,
                action="SATELLITE_SIGNAL_POLICY_COMMITTED",
                entity_type="satellite_signal_policy",
                entity_id=policy_id,
                actor_ref=confirmed_by.strip(),
                details={**content, "reason": request["reason"]},
            )
            policy = connection.execute(
                "SELECT * FROM satellite_signal_policies WHERE id=?", (policy_id,)
            ).fetchone()
            connection.commit()
            assert policy is not None
            return {
                "draft": self.get_policy_draft(draft_id=draft_id),
                "policy": self._policy_data(policy),
                "idempotent_replay": False,
                "strategy_changed": False,
                "transactions_created": False,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _snapshot_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "portfolio_id": str(row["portfolio_id"]),
            "policy_id": str(row["policy_id"]),
            "strategy_assignment_id": str(row["strategy_assignment_id"]),
            "instrument_id": str(row["instrument_id"]),
            "instrument_code": str(row["instrument_code"]),
            "instrument_name": str(row["instrument_name"]),
            "benchmark_code": row["benchmark_code"],
            "valuation_snapshot_id": row["valuation_snapshot_id"],
            "as_of_date": str(row["as_of_date"]),
            "state": str(row["state"]),
            "reason_code": str(row["reason_code"]),
            "percentile_bps": row["percentile_bps"],
            "percentile": (
                f"{int(row['percentile_bps']) / 100:.2f}"
                if row["percentile_bps"] is not None
                else None
            ),
            "sample_count": row["sample_count"],
            "data_quality": str(row["data_quality"]),
            "facts": json.loads(str(row["facts_json"])),
            "facts_hash": str(row["facts_hash"]),
            "created_at": str(row["created_at"]),
        }

    def _snapshot_query(self) -> str:
        return """
            SELECT s.*, i.code AS instrument_code, i.name AS instrument_name,
                   b.code AS benchmark_code
            FROM satellite_signal_snapshots s
            JOIN instruments i ON i.id=s.instrument_id
            LEFT JOIN instruments b ON b.id=s.benchmark_instrument_id
        """

    def list_snapshots(
        self,
        *,
        portfolio_id: str,
        as_of_date: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = self._snapshot_query() + " WHERE s.portfolio_id=?"
        params: list[object] = [portfolio_id]
        if as_of_date:
            query += " AND s.as_of_date=?"
            params.append(as_of_date)
        query += " ORDER BY s.as_of_date DESC, s.created_at DESC, i.code LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._snapshot_data(row) for row in rows]

    def build_snapshot(
        self,
        *,
        portfolio_id: str,
        as_of_date: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        policy = self.get_active_policy(portfolio_id=portfolio_id)
        if policy is None:
            raise LedgerError(
                "SATELLITE_SIGNAL_POLICY_NOT_CONFIGURED",
                "an approved satellite signal policy is required",
                http_status=409,
            )
        end = date.fromisoformat(as_of_date) if as_of_date else self._now().date()
        assignment = self._strategy.get_assignment(portfolio_id=portfolio_id)
        configs = [
            item
            for item in assignment["instruments"]
            if item["role"] == "SATELLITE"
            and item["status"] == "ACTIVE"
            and item["thesis_status"] == "ACTIVE"
        ]
        if not configs:
            raise LedgerError(
                "SATELLITE_SIGNAL_UNIVERSE_EMPTY",
                "the active strategy has no satellite instruments",
                http_status=409,
            )
        results: list[JsonDict] = []
        for config in configs:
            results.append(
                self._evaluate_config(
                    portfolio_id=portfolio_id,
                    assignment_id=str(assignment["id"]),
                    policy=policy,
                    config=config,
                    as_of_date=end,
                    actor_ref=actor_ref,
                )
            )
        counts: dict[str, int] = {}
        for item in results:
            counts[str(item["state"])] = counts.get(str(item["state"]), 0) + 1
        unavailable_count = counts.get("BLOCKED", 0) + counts.get("NOT_AUTHORIZED", 0)
        if counts.get("OPEN", 0):
            overall = "PARTIAL" if unavailable_count else "READY"
        elif counts.get("CLOSED", 0):
            overall = "PARTIAL" if unavailable_count else "NO_OPEN_SIGNAL"
        else:
            overall = "BLOCKED"
        lines = [
            "卫星舱估值信号快照",
            f"数据日期: {end.isoformat()}",
            (
                f"政策版本: v{policy['version']} | {policy['metric']} "
                f"<= {policy['entry_max_percentile']}%"
            ),
            f"总体状态: {overall}",
            "",
            "标的信号:",
        ]
        lines.extend(
            f"{item['instrument_code']} | {item['state']} | {item['reason_code']}"
            for item in results
        )
        lines.extend(
            [
                "",
                "边界: OPEN 只允许进入周计划候选，不代表建议、成交或自动交易。",  # noqa: RUF001
            ]
        )
        return {
            "portfolio_id": portfolio_id,
            "as_of_date": end.isoformat(),
            "state": overall,
            "policy": policy,
            "counts": counts,
            "items": results,
            "automatic_trade": False,
            "display_text": "\n".join(lines),
        }

    def _evaluate_config(
        self,
        *,
        portfolio_id: str,
        assignment_id: str,
        policy: JsonDict,
        config: JsonDict,
        as_of_date: date,
        actor_ref: str,
    ) -> JsonDict:
        benchmark_code = config["benchmark_code"]
        valuation: JsonDict | None = None
        state = "BLOCKED"
        reason_code = "BENCHMARK_REQUIRED"
        quality = "NOT_AVAILABLE"
        if benchmark_code and config["proxy_suitability"] != "STRONG":
            reason_code = "STRONG_PROXY_REQUIRED"
        elif benchmark_code:
            valuation = self._risk.valuation_snapshot(
                portfolio_id=portfolio_id,
                instrument_code=str(config["instrument_code"]),
                metric=str(policy["metric"]),
                as_of_date=as_of_date.isoformat(),
                lookback_days=int(policy["lookback_days"]),
            )
            quality = str(valuation.get("data_quality", "NOT_AVAILABLE"))
            if not valuation.get("available"):
                reason_code = str(valuation["reason_code"])
            elif int(valuation["sample_count"]) < int(policy["minimum_sample_count"]):
                reason_code = "VALUATION_SAMPLE_COUNT_INSUFFICIENT"
            elif (
                as_of_date - date.fromisoformat(str(valuation["latest_observation_date"]))
            ).days > int(policy["maximum_observation_age_days"]):
                reason_code = "VALUATION_OBSERVATION_STALE"
            elif quality == "WARNING" and not bool(policy["allow_warning_data"]):
                reason_code = "VALUATION_DATA_QUALITY_BLOCKED"
            elif not bool(config["contribution_eligible"]):
                state = "NOT_AUTHORIZED"
                reason_code = "CONTRIBUTION_NOT_AUTHORIZED"
            elif int(valuation["percentile_bps"]) <= int(policy["entry_max_percentile_bps"]):
                state = "OPEN"
                reason_code = "VALUATION_SIGNAL_OPEN"
            else:
                state = "CLOSED"
                reason_code = "VALUATION_SIGNAL_CLOSED"
        facts = {
            "calculation_version": SIGNAL_CALCULATION_VERSION,
            "policy_content_hash": policy["content_hash"],
            "strategy_assignment_id": assignment_id,
            "strategy_config_id": config["id"],
            "strategy_config_updated_at": config["updated_at"],
            "instrument_code": config["instrument_code"],
            "benchmark_code": benchmark_code,
            "proxy_suitability": config["proxy_suitability"],
            "contribution_eligible": config["contribution_eligible"],
            "as_of_date": as_of_date.isoformat(),
            "state": state,
            "reason_code": reason_code,
            "valuation_input_hash": valuation.get("input_hash") if valuation else None,
        }
        facts_hash = _hash(facts)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT id FROM satellite_signal_snapshots WHERE facts_hash=?",
                (facts_hash,),
            ).fetchone()
            snapshot_id = str(existing["id"]) if existing is not None else str(uuid4())
            if existing is None:
                benchmark = (
                    connection.execute(
                        "SELECT id FROM instruments WHERE code=?", (benchmark_code,)
                    ).fetchone()
                    if benchmark_code
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO satellite_signal_snapshots (
                        id, portfolio_id, policy_id, strategy_assignment_id,
                        instrument_id, benchmark_instrument_id, valuation_snapshot_id,
                        as_of_date, state, reason_code, percentile_bps, sample_count,
                        data_quality, facts_json, facts_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        portfolio_id,
                        policy["id"],
                        assignment_id,
                        config["instrument_id"],
                        benchmark["id"] if benchmark is not None else None,
                        valuation.get("id") if valuation else None,
                        as_of_date.isoformat(),
                        state,
                        reason_code,
                        valuation.get("percentile_bps") if valuation else None,
                        valuation.get("sample_count") if valuation else None,
                        quality,
                        _json(facts),
                        facts_hash,
                        _iso(self._now()),
                    ),
                )
                self._audit(
                    connection,
                    action="SATELLITE_SIGNAL_SNAPSHOT_RECORDED",
                    entity_type="satellite_signal_snapshot",
                    entity_id=snapshot_id,
                    actor_ref=actor_ref,
                    details=facts,
                )
            row = connection.execute(
                self._snapshot_query() + " WHERE s.id=?", (snapshot_id,)
            ).fetchone()
        assert row is not None
        return self._snapshot_data(row)
