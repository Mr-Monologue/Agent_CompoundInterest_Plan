"""Governed deterministic automation, fact bundles, alerts and delivery outbox."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from investor_core.config import Settings
from investor_core.health import build_doctor_report
from investor_core.ledger import JsonDict, LedgerError, LedgerService, utc_now
from investor_core.market_sync import MarketSyncService
from investor_core.performance import PerformanceService
from investor_core.planning import PlanningService
from investor_core.risk import RiskService

SUPPORTED_JOBS = {
    "DAILY_MARKET_SYNC",
    "DAILY_RISK_SCAN",
    "WEEKLY_PLAN_PREPARE",
    "SELL_FOLLOWUP_DUE",
    "SYSTEM_DOCTOR",
    "MONTHLY_REVIEW",
    "QUARTERLY_REVIEW",
    "ANNUAL_REVIEW",
}
PORTFOLIO_JOBS = SUPPORTED_JOBS - {"SYSTEM_DOCTOR"}
MANAGED_JOB_PREFIX = "value-dca-"
RETRY_JOB_NAME = f"{MANAGED_JOB_PREFIX}retry-due"
DELIVERY_JOB_NAME = f"{MANAGED_JOB_PREFIX}notification-delivery"
MISSED_RUN_GRACE_MINUTES = 10
MISSED_RUN_LOOKBACK_DAYS = 7
DELIVERY_RECEIPT_TIMEOUT_MINUTES = 15
DELIVERY_RETRY_MINUTES = (5, 15, 30, 60, 120)
NOTIFICATION_TEST_COOLDOWN_SECONDS = 60
NOTIFICATION_TEST_CONFIRMATION = "SEND_TEST_NOTIFICATION"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class OperationsService:
    """Run only explicitly approved deterministic jobs and persist every outcome."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] = utc_now,
        ledger: LedgerService | None = None,
        market_sync: MarketSyncService | None = None,
        risk: RiskService | None = None,
        planning: PlanningService | None = None,
        performance: PerformanceService | None = None,
    ) -> None:
        self.settings = settings
        self._now = now
        self._ledger = ledger or LedgerService(settings, now=now)
        self._market_sync = market_sync or MarketSyncService(settings, now=now)
        self._risk = risk or RiskService(settings, now=now)
        self._planning = planning or PlanningService(settings, now=now)
        self._performance = performance or PerformanceService(settings, now=now)

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
                (
                    "CRON"
                    if actor_ref == "cron"
                    or actor_ref.startswith("hermes-cron")
                    or actor_ref.startswith("operations-")
                    else "USER"
                ),
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
    def _normalize_job(job_name: str) -> str:
        normalized = job_name.strip().upper().replace("-", "_")
        if normalized not in SUPPORTED_JOBS:
            raise LedgerError(
                "AUTOMATION_JOB_UNSUPPORTED",
                "automation job is not supported",
                details={"supported_jobs": sorted(SUPPORTED_JOBS)},
            )
        return normalized

    @staticmethod
    def _validate_config(job_name: str, config: JsonDict) -> JsonDict:
        allowed_common = {"delivery_target", "max_attempts"}
        allowed_by_job = {
            "DAILY_MARKET_SYNC": {"provider_id"},
            "DAILY_RISK_SCAN": set(),
            "WEEKLY_PLAN_PREPARE": {"contribution_amount"},
            "SELL_FOLLOWUP_DUE": set(),
            "SYSTEM_DOCTOR": set(),
            "MONTHLY_REVIEW": set(),
            "QUARTERLY_REVIEW": set(),
            "ANNUAL_REVIEW": set(),
        }
        unknown = set(config) - allowed_common - allowed_by_job[job_name]
        if unknown:
            raise LedgerError(
                "AUTOMATION_CONFIG_INVALID",
                "automation config contains unsupported fields",
                details={"fields": sorted(unknown)},
            )
        normalized = dict(config)
        target = str(normalized.get("delivery_target", "origin")).strip()
        if not target or len(target) > 200:
            raise LedgerError(
                "AUTOMATION_CONFIG_INVALID",
                "delivery_target must be between 1 and 200 characters",
            )
        normalized["delivery_target"] = target
        attempts = int(normalized.get("max_attempts", 3))
        if attempts < 1 or attempts > 5:
            raise LedgerError(
                "AUTOMATION_CONFIG_INVALID",
                "max_attempts must be between 1 and 5",
            )
        normalized["max_attempts"] = attempts
        if job_name == "DAILY_MARKET_SYNC":
            provider_id = str(normalized.get("provider_id", "AKSHARE_OPEN_FUND")).strip()
            if provider_id != "AKSHARE_OPEN_FUND":
                raise LedgerError(
                    "AUTOMATION_CONFIG_INVALID",
                    "the configured market provider is unsupported",
                )
            normalized["provider_id"] = provider_id
        if job_name == "WEEKLY_PLAN_PREPARE":
            try:
                amount = Decimal(str(normalized["contribution_amount"]))
            except (KeyError, InvalidOperation) as exc:
                raise LedgerError(
                    "AUTOMATION_CONFIG_INVALID",
                    "weekly plan automation requires an explicit contribution_amount",
                ) from exc
            if (
                not amount.is_finite()
                or amount <= 0
                or amount * 100 != (amount * 100).to_integral_value()
            ):
                raise LedgerError(
                    "AUTOMATION_CONFIG_INVALID",
                    (
                        "contribution_amount must be a positive currency amount "
                        "with at most 2 decimals"
                    ),
                )
            normalized["contribution_amount"] = f"{amount:.2f}"
        return normalized

    @staticmethod
    def _validate_cron_schedule(schedule: str) -> str:
        normalized = " ".join(schedule.strip().split())
        if len(normalized.split(" ")) != 5 or not croniter.is_valid(normalized):
            raise LedgerError(
                "AUTOMATION_SCHEDULE_UNSUPPORTED",
                "Hermes scheduler policies require a valid five-field cron expression",
                details={"schedule": schedule},
            )
        return normalized

    @staticmethod
    def _scheduled_occurrence(
        policy: sqlite3.Row | JsonDict,
        *,
        before: datetime,
    ) -> datetime:
        timezone = ZoneInfo(str(policy["timezone"]))
        localized = before.astimezone(timezone)
        occurrence = croniter(
            str(policy["schedule"]),
            localized + timedelta(microseconds=1),
            ret_type=datetime,
        ).get_prev(datetime)
        if occurrence.tzinfo is None:
            occurrence = occurrence.replace(tzinfo=timezone)
        return occurrence.astimezone(UTC)

    @staticmethod
    def _legacy_scheduled_date(scheduled_for: str, *, timezone: str) -> str | None:
        if "T" not in scheduled_for:
            return None
        try:
            scheduled = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        except ValueError:
            return None
        if scheduled.tzinfo is None:
            return None
        return scheduled.astimezone(ZoneInfo(timezone)).date().isoformat()

    @staticmethod
    def _business_date(scheduled_for: str, *, timezone: str) -> str:
        if "T" not in scheduled_for:
            return scheduled_for[:10]
        scheduled = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        return scheduled.astimezone(ZoneInfo(timezone)).date().isoformat()

    def _find_existing_run(
        self,
        connection: sqlite3.Connection,
        *,
        job_name: str,
        portfolio_id: str | None,
        scheduled_for: str,
        policy_version: int,
        timezone: str,
    ) -> sqlite3.Row | None:
        idempotency_key = f"{job_name}:{portfolio_id or 'global'}:{scheduled_for}:v{policy_version}"
        existing = connection.execute(
            "SELECT * FROM job_runs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return cast(sqlite3.Row, existing)
        legacy_date = self._legacy_scheduled_date(scheduled_for, timezone=timezone)
        if legacy_date is None:
            return None
        legacy_key = f"{job_name}:{portfolio_id or 'global'}:{legacy_date}:v{policy_version}"
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM job_runs WHERE idempotency_key = ?",
                (legacy_key,),
            ).fetchone(),
        )

    def create_policy_draft(
        self,
        *,
        job_name: str,
        enabled: bool,
        schedule: str,
        timezone: str,
        config: JsonDict,
        reason: str,
        portfolio_id: str | None = None,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        job = self._normalize_job(job_name)
        normalized_portfolio = portfolio_id.strip() if portfolio_id else None
        if job in PORTFOLIO_JOBS and not normalized_portfolio:
            context = self._ledger.get_investment_context()
            normalized_portfolio = str(context["portfolio"]["id"])
        if job == "SYSTEM_DOCTOR":
            normalized_portfolio = None
        normalized_schedule = self._validate_cron_schedule(schedule)
        if not normalized_schedule or len(normalized_schedule) > 120:
            raise LedgerError(
                "AUTOMATION_SCHEDULE_INVALID",
                "schedule must be between 1 and 120 characters",
            )
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise LedgerError(
                "INVALID_TIMEZONE",
                "configured automation timezone is not available",
                details={"timezone": timezone},
            ) from exc
        normalized_config = self._validate_config(job, config)
        payload = {
            "portfolio_id": normalized_portfolio,
            "job_name": job,
            "enabled": bool(enabled),
            "schedule": normalized_schedule,
            "timezone": timezone,
            "config": normalized_config,
            "reason": reason.strip(),
        }
        if not payload["reason"]:
            raise LedgerError("MISSING_REQUIRED_FIELD", "reason is required")
        content_hash = _hash(payload)
        token = secrets.token_urlsafe(24)
        draft_id = str(uuid4())
        created_at = self._now()
        expires_at = created_at + timedelta(minutes=self.settings.confirmation_ttl_minutes)
        with self._connect() as connection:
            if normalized_portfolio is not None:
                portfolio = connection.execute(
                    "SELECT id FROM portfolios WHERE id = ? AND status = 'ACTIVE'",
                    (normalized_portfolio,),
                ).fetchone()
                if portfolio is None:
                    raise LedgerError(
                        "PORTFOLIO_NOT_FOUND",
                        "active portfolio was not found",
                        http_status=404,
                    )
            connection.execute(
                """
                INSERT INTO automation_policy_drafts (
                    id, portfolio_id, job_name, enabled, schedule, timezone,
                    config_json, reason, content_hash, confirmation_digest,
                    status, created_by, created_at, expires_at, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, NULL)
                """,
                (
                    draft_id,
                    normalized_portfolio,
                    job,
                    int(enabled),
                    normalized_schedule,
                    timezone,
                    _json(normalized_config),
                    str(payload["reason"]),
                    content_hash,
                    _token_digest(token),
                    actor_ref,
                    _iso(created_at),
                    _iso(expires_at),
                ),
            )
            self._audit(
                connection,
                action="AUTOMATION_POLICY_DRAFT_CREATED",
                entity_type="automation_policy_draft",
                entity_id=draft_id,
                actor_ref=actor_ref,
                details={**payload, "automatic_trade": False},
            )
        return {
            "draft": {
                "id": draft_id,
                **payload,
                "status": "PENDING",
                "content_hash": content_hash,
                "created_at": _iso(created_at),
                "expires_at": _iso(expires_at),
            },
            "confirmation_token": token,
            "holdings_changed": False,
            "transactions_created": False,
        }

    def get_policy_draft(self, *, draft_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM automation_policy_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "AUTOMATION_POLICY_DRAFT_NOT_FOUND",
                    "automation policy draft was not found",
                    http_status=404,
                )
            if str(row["status"]) == "PENDING" and self._now() >= datetime.fromisoformat(
                str(row["expires_at"]).replace("Z", "+00:00")
            ):
                connection.execute(
                    "UPDATE automation_policy_drafts SET status='EXPIRED' WHERE id = ?",
                    (draft_id,),
                )
                row = connection.execute(
                    "SELECT * FROM automation_policy_drafts WHERE id = ?", (draft_id,)
                ).fetchone()
                assert row is not None
            return self._draft_data(row)

    @staticmethod
    def _draft_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "portfolio_id": row["portfolio_id"],
            "job_name": str(row["job_name"]),
            "enabled": bool(row["enabled"]),
            "schedule": str(row["schedule"]),
            "timezone": str(row["timezone"]),
            "config": json.loads(str(row["config_json"])),
            "reason": str(row["reason"]),
            "content_hash": str(row["content_hash"]),
            "status": str(row["status"]),
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "expires_at": str(row["expires_at"]),
            "committed_at": row["committed_at"],
        }

    def commit_policy_draft(
        self,
        *,
        draft_id: str,
        confirmation_token: str,
        confirmed_by: str,
    ) -> JsonDict:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM automation_policy_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(
                    "AUTOMATION_POLICY_DRAFT_NOT_FOUND",
                    "automation policy draft was not found",
                    http_status=404,
                )
            if str(row["status"]) != "PENDING":
                raise LedgerError(
                    "STATE_CONFLICT",
                    "automation policy draft is not pending",
                    details={"status": str(row["status"])},
                    http_status=409,
                )
            if now >= datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00")):
                connection.execute(
                    "UPDATE automation_policy_drafts SET status='EXPIRED' WHERE id = ?",
                    (draft_id,),
                )
                connection.commit()
                raise LedgerError(
                    "CONFIRMATION_EXPIRED",
                    "automation policy confirmation expired",
                    http_status=409,
                )
            if not hmac.compare_digest(
                str(row["confirmation_digest"]), _token_digest(confirmation_token)
            ):
                raise LedgerError(
                    "CONFIRMATION_MISMATCH",
                    "automation policy confirmation token does not match",
                    http_status=409,
                )
            current = connection.execute(
                """
                SELECT * FROM automation_policies
                WHERE portfolio_id IS ? AND job_name = ? AND status = 'ACTIVE'
                ORDER BY version DESC LIMIT 1
                """,
                (row["portfolio_id"], row["job_name"]),
            ).fetchone()
            version = int(current["version"]) + 1 if current is not None else 1
            if current is not None:
                connection.execute(
                    "UPDATE automation_policies SET status='RETIRED' WHERE id = ?",
                    (current["id"],),
                )
            policy_id = str(uuid4())
            timestamp = _iso(now)
            connection.execute(
                """
                INSERT INTO automation_policies (
                    id, portfolio_id, job_name, version, enabled, schedule,
                    timezone, config_json, content_hash, status, approved_by,
                    approved_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    policy_id,
                    row["portfolio_id"],
                    row["job_name"],
                    version,
                    row["enabled"],
                    row["schedule"],
                    row["timezone"],
                    row["config_json"],
                    row["content_hash"],
                    confirmed_by,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE automation_policy_drafts
                SET status='COMMITTED', committed_at=?
                WHERE id=?
                """,
                (timestamp, draft_id),
            )
            self._audit(
                connection,
                action="AUTOMATION_POLICY_COMMITTED",
                entity_type="automation_policy",
                entity_id=policy_id,
                actor_ref=confirmed_by,
                details={
                    "job_name": str(row["job_name"]),
                    "version": version,
                    "enabled": bool(row["enabled"]),
                    "portfolio_id": row["portfolio_id"],
                    "automatic_trade": False,
                },
            )
            policy = connection.execute(
                "SELECT * FROM automation_policies WHERE id = ?", (policy_id,)
            ).fetchone()
            assert policy is not None
        return {
            "policy": self._policy_data(policy),
            "holdings_changed": False,
            "transactions_created": False,
        }

    @staticmethod
    def _policy_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "portfolio_id": row["portfolio_id"],
            "job_name": str(row["job_name"]),
            "version": int(row["version"]),
            "enabled": bool(row["enabled"]),
            "schedule": str(row["schedule"]),
            "timezone": str(row["timezone"]),
            "config": json.loads(str(row["config_json"])),
            "content_hash": str(row["content_hash"]),
            "status": str(row["status"]),
            "approved_by": str(row["approved_by"]),
            "approved_at": str(row["approved_at"]),
        }

    def list_policies(
        self, *, portfolio_id: str | None = None, active_only: bool = True
    ) -> list[JsonDict]:
        query = "SELECT * FROM automation_policies WHERE 1=1"
        params: list[object] = []
        if portfolio_id is not None:
            query += " AND portfolio_id IS ?"
            params.append(portfolio_id)
        if active_only:
            query += " AND status='ACTIVE'"
        query += " ORDER BY job_name, version DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._policy_data(row) for row in rows]

    @staticmethod
    def _managed_job_name(job_name: str) -> str:
        return f"{MANAGED_JOB_PREFIX}{job_name.lower().replace('_', '-')}"

    def scheduler_manifest(self, *, profile: str = "investor") -> JsonDict:
        """Return the desired Hermes jobs without installing or mutating the scheduler."""
        normalized_profile = profile.strip()
        if not normalized_profile or len(normalized_profile) > 80:
            raise LedgerError(
                "AUTOMATION_PROFILE_INVALID",
                "Hermes profile must be between 1 and 80 characters",
            )
        desired_jobs: list[JsonDict] = []
        timezones: set[str] = set()
        for policy in self.list_policies(active_only=True):
            if not bool(policy["enabled"]):
                continue
            job_name = str(policy["job_name"])
            timezone = str(policy["timezone"])
            config = dict(policy["config"])
            timezones.add(timezone)
            desired_jobs.append(
                {
                    "managed_name": self._managed_job_name(job_name),
                    "job_name": job_name,
                    "schedule": self._validate_cron_schedule(str(policy["schedule"])),
                    "timezone": timezone,
                    "script": f"value_dca_{job_name.lower()}.py",
                    "no_agent": True,
                    "delivery_target": str(config["delivery_target"]),
                    "policy_id": str(policy["id"]),
                    "policy_version": int(policy["version"]),
                    "policy_content_hash": str(policy["content_hash"]),
                }
            )
        retry_timezone = next(iter(timezones), self.settings.timezone)
        if desired_jobs:
            desired_jobs.append(
                {
                    "managed_name": RETRY_JOB_NAME,
                    "job_name": "AUTOMATION_RETRY_DUE",
                    "schedule": "*/5 * * * *",
                    "timezone": retry_timezone,
                    "script": "value_dca_retry_due.py",
                    "no_agent": True,
                    "delivery_target": "origin",
                    "policy_id": None,
                    "policy_version": None,
                    "policy_content_hash": None,
                }
            )
            desired_jobs.append(
                {
                    "managed_name": DELIVERY_JOB_NAME,
                    "job_name": "NOTIFICATION_DELIVERY",
                    "schedule": "* * * * *",
                    "timezone": retry_timezone,
                    "script": "value_dca_notification_delivery.py",
                    "no_agent": True,
                    "delivery_target": "local",
                    "policy_id": None,
                    "policy_version": None,
                    "policy_content_hash": None,
                }
            )
        timezone_status = "PASS" if len(timezones) <= 1 else "CONFLICT"
        return {
            "profile": normalized_profile,
            "managed_prefix": MANAGED_JOB_PREFIX,
            "timezone_status": timezone_status,
            "expected_timezone": retry_timezone,
            "jobs": sorted(desired_jobs, key=lambda item: str(item["managed_name"])),
            "automatic_trade": False,
            "reconcile_contract": {
                "create_or_update_managed_jobs_only": True,
                "never_delete_unmanaged_jobs": True,
                "duplicate_managed_name": "STOP",
                "record_snapshot_after_reconcile": True,
                "retry_job_also_recovers_missed_runs": True,
                "missed_run_grace_minutes": MISSED_RUN_GRACE_MINUTES,
                "missed_run_lookback_days": MISSED_RUN_LOOKBACK_DAYS,
            },
        }

    def _scheduler_drift(self, *, jobs: list[JsonDict], gateway_status: str) -> JsonDict:
        desired = {
            str(item["managed_name"]): item
            for item in self.scheduler_manifest(profile="snapshot")["jobs"]
        }
        actual: dict[str, JsonDict] = {}
        duplicates: list[str] = []
        for job in jobs:
            name = str(job["managed_name"])
            if not name.startswith(MANAGED_JOB_PREFIX):
                continue
            if name in actual:
                duplicates.append(name)
            actual[name] = job
        missing = sorted(set(desired) - set(actual))
        unexpected = sorted(set(actual) - set(desired))
        mismatches: list[JsonDict] = []
        compared_fields = ("schedule", "no_agent", "script", "delivery_target")
        for name in sorted(set(desired) & set(actual)):
            differences = {
                field: {"expected": desired[name][field], "actual": actual[name].get(field)}
                for field in compared_fields
                if desired[name][field] != actual[name].get(field)
            }
            if not bool(actual[name].get("enabled")):
                differences["enabled"] = {"expected": True, "actual": False}
            if differences:
                mismatches.append({"managed_name": name, "fields": differences})
        if gateway_status != "RUNNING":
            status = "BLOCKED"
        elif missing or unexpected or mismatches or duplicates:
            status = "DRIFT"
        else:
            status = "IN_SYNC"
        return {
            "status": status,
            "missing": missing,
            "unexpected": unexpected,
            "mismatches": mismatches,
            "duplicates": sorted(set(duplicates)),
        }

    def record_scheduler_snapshot(
        self,
        *,
        profile: str,
        gateway_status: str,
        jobs: list[JsonDict],
        actor_ref: str = "hermes",
    ) -> JsonDict:
        """Record observed Hermes state; this never creates, edits or removes a Cron job."""
        normalized_profile = profile.strip()
        normalized_gateway = gateway_status.strip().upper()
        if normalized_gateway not in {"RUNNING", "STOPPED", "UNKNOWN"}:
            raise LedgerError(
                "AUTOMATION_GATEWAY_STATUS_INVALID",
                "gateway_status must be RUNNING, STOPPED or UNKNOWN",
            )
        normalized_jobs = sorted(
            [dict(item) for item in jobs],
            key=lambda item: str(item["managed_name"]),
        )
        drift = self._scheduler_drift(
            jobs=normalized_jobs,
            gateway_status=normalized_gateway,
        )
        snapshot_id = str(uuid4())
        timestamp = _iso(self._now())
        content = {
            "profile": normalized_profile,
            "gateway_status": normalized_gateway,
            "jobs": normalized_jobs,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO automation_scheduler_snapshots (
                    id, profile, gateway_status, jobs_json, content_hash,
                    reconciliation_status, drift_json, recorded_by, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    normalized_profile,
                    normalized_gateway,
                    _json(normalized_jobs),
                    _hash(content),
                    str(drift["status"]),
                    _json(drift),
                    actor_ref,
                    timestamp,
                ),
            )
            self._audit(
                connection,
                action="AUTOMATION_SCHEDULER_SNAPSHOT_RECORDED",
                entity_type="automation_scheduler_snapshot",
                entity_id=snapshot_id,
                actor_ref=actor_ref,
                details={
                    "profile": normalized_profile,
                    "gateway_status": normalized_gateway,
                    "reconciliation_status": drift["status"],
                },
            )
        return {
            "id": snapshot_id,
            **content,
            "content_hash": _hash(content),
            "reconciliation_status": drift["status"],
            "drift": drift,
            "recorded_by": actor_ref,
            "recorded_at": timestamp,
            "holdings_changed": False,
            "transactions_created": False,
        }

    def latest_scheduler_snapshot(self, *, profile: str | None = None) -> JsonDict | None:
        query = "SELECT * FROM automation_scheduler_snapshots"
        params: list[object] = []
        if profile:
            query += " WHERE profile=?"
            params.append(profile.strip())
        query += " ORDER BY recorded_at DESC, rowid DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "profile": str(row["profile"]),
            "gateway_status": str(row["gateway_status"]),
            "jobs": json.loads(str(row["jobs_json"])),
            "content_hash": str(row["content_hash"]),
            "reconciliation_status": str(row["reconciliation_status"]),
            "drift": json.loads(str(row["drift_json"])),
            "recorded_by": str(row["recorded_by"]),
            "recorded_at": str(row["recorded_at"]),
        }

    def _resolve_policy(
        self, *, job_name: str, portfolio_id: str | None
    ) -> tuple[sqlite3.Row | None, str | None, str | None]:
        job = self._normalize_job(job_name)
        resolved_portfolio = portfolio_id.strip() if portfolio_id else None
        account_id: str | None = None
        if job in PORTFOLIO_JOBS and not resolved_portfolio:
            context = self._ledger.get_investment_context()
            resolved_portfolio = str(context["portfolio"]["id"])
            account_id = str(context["account"]["id"])
        elif job in PORTFOLIO_JOBS:
            context = self._ledger.get_investment_context()
            if str(context["portfolio"]["id"]) != resolved_portfolio:
                raise LedgerError(
                    "INVESTMENT_CONTEXT_MISMATCH",
                    "automation portfolio differs from the saved default context",
                    http_status=409,
                )
            account_id = str(context["account"]["id"])
        with self._connect() as connection:
            policy = connection.execute(
                """
                SELECT * FROM automation_policies
                WHERE portfolio_id IS ? AND job_name = ? AND status='ACTIVE'
                ORDER BY version DESC LIMIT 1
                """,
                (resolved_portfolio, job),
            ).fetchone()
        return policy, resolved_portfolio, account_id

    def run_job(
        self,
        *,
        job_name: str,
        scheduled_for: str | None = None,
        portfolio_id: str | None = None,
        actor_ref: str = "operations-runner",
    ) -> JsonDict:
        job = self._normalize_job(job_name)
        policy, resolved_portfolio, account_id = self._resolve_policy(
            job_name=job, portfolio_id=portfolio_id
        )
        timezone = str(policy["timezone"]) if policy is not None else self.settings.timezone
        derived_schedule = scheduled_for is None
        scheduled = scheduled_for.strip() if scheduled_for is not None else None
        if scheduled is None and policy is not None:
            scheduled = _iso(self._scheduled_occurrence(policy, before=self._now()))
        if scheduled is None:
            scheduled = self._now().astimezone(ZoneInfo(timezone)).date().isoformat()
        if not scheduled or len(scheduled) > 80:
            raise LedgerError(
                "AUTOMATION_SCHEDULED_FOR_INVALID",
                "scheduled_for must be a stable date or timestamp",
            )
        if policy is None:
            return self._record_skip(
                job_name=job,
                scheduled_for=scheduled,
                portfolio_id=resolved_portfolio,
                reason_code="AUTOMATION_POLICY_NOT_CONFIGURED",
            )
        policy_data = self._policy_data(policy)
        if not bool(policy["enabled"]):
            return self._record_skip(
                job_name=job,
                scheduled_for=scheduled,
                portfolio_id=resolved_portfolio,
                reason_code="AUTOMATION_PAUSED",
                policy=policy_data,
            )
        if derived_schedule:
            occurrence = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
            approved_at = datetime.fromisoformat(str(policy["approved_at"]).replace("Z", "+00:00"))
            if occurrence < approved_at:
                return self._record_skip(
                    job_name=job,
                    scheduled_for=scheduled,
                    portfolio_id=resolved_portfolio,
                    reason_code="AUTOMATION_NOT_DUE",
                    policy=policy_data,
                )
        config = json.loads(str(policy["config_json"]))
        policy_version = int(policy["version"])
        idempotency_key = f"{job}:{resolved_portfolio or 'global'}:{scheduled}:v{policy_version}"
        with self._connect() as connection:
            existing = self._find_existing_run(
                connection,
                job_name=job,
                portfolio_id=resolved_portfolio,
                scheduled_for=scheduled,
                policy_version=policy_version,
                timezone=timezone,
            )
            if existing is not None and str(existing["status"]) in {
                "RUNNING",
                "SUCCESS",
                "DEGRADED",
                "SKIPPED",
            }:
                return {
                    "job_run": self._run_data(existing),
                    "idempotent_replay": True,
                    "display_text": self._display_for_run(connection, str(existing["id"])),
                }
            max_attempts = int(config["max_attempts"])
            if existing is not None and int(existing["attempt_count"]) >= max_attempts:
                return {
                    "job_run": self._run_data(existing),
                    "idempotent_replay": True,
                    "reason_code": "MAX_ATTEMPTS_EXHAUSTED",
                    "display_text": self._display_for_run(connection, str(existing["id"])),
                }
            now = _iso(self._now())
            if existing is None:
                run_id = str(uuid4())
                attempt_count = 1
                connection.execute(
                    """
                    INSERT INTO job_runs (
                        id, job_name, scheduled_for, idempotency_key, status,
                        started_at, finished_at, input_json, output_json,
                        error_code, error_summary, trace_id, attempt_count,
                        max_attempts, heartbeat_at, next_retry_at
                    ) VALUES (?, ?, ?, ?, 'RUNNING', ?, NULL, ?, '{}',
                              NULL, NULL, ?, 1, ?, ?, NULL)
                    """,
                    (
                        run_id,
                        job,
                        scheduled,
                        idempotency_key,
                        now,
                        _json(
                            {
                                "portfolio_id": resolved_portfolio,
                                "account_id": account_id,
                                "policy_id": str(policy["id"]),
                                "policy_version": policy_version,
                                "actor_ref": actor_ref,
                            }
                        ),
                        str(uuid4()),
                        max_attempts,
                        now,
                    ),
                )
            else:
                run_id = str(existing["id"])
                attempt_count = int(existing["attempt_count"]) + 1
                connection.execute(
                    """
                    UPDATE job_runs
                    SET status='RUNNING', started_at=?, finished_at=NULL,
                        error_code=NULL, error_summary=NULL, heartbeat_at=?,
                        next_retry_at=NULL, attempt_count=?
                    WHERE id=?
                    """,
                    (now, now, attempt_count, run_id),
                )
        try:
            facts, quality, notify, reason_code = self._execute(
                job_name=job,
                scheduled_for=scheduled,
                timezone=timezone,
                portfolio_id=resolved_portfolio,
                account_id=account_id,
                config=config,
            )
            bundle = self._finish_success(
                run_id=run_id,
                job_name=job,
                scheduled_for=scheduled,
                portfolio_id=resolved_portfolio,
                facts=facts,
                quality=quality,
                notify=notify,
                reason_code=reason_code,
                delivery_target=str(config["delivery_target"]),
                actor_ref=actor_ref,
            )
            return {
                "job_run": self.get_run(run_id=run_id),
                "report_bundle": bundle,
                "idempotent_replay": False,
                "display_text": "[SILENT]" if not notify else bundle["display_text"],
            }
        except LedgerError as exc:
            return self._finish_failure(
                run_id=run_id,
                job_name=job,
                portfolio_id=resolved_portfolio,
                scheduled_for=scheduled,
                error=exc,
                attempt_count=attempt_count,
                max_attempts=max_attempts,
                delivery_target=str(config["delivery_target"]),
                actor_ref=actor_ref,
            )
        except Exception as exc:
            return self._finish_failure(
                run_id=run_id,
                job_name=job,
                portfolio_id=resolved_portfolio,
                scheduled_for=scheduled,
                error=LedgerError(
                    "INTERNAL_ERROR",
                    "automation job failed unexpectedly",
                    details={"error_type": type(exc).__name__},
                ),
                attempt_count=attempt_count,
                max_attempts=max_attempts,
                delivery_target=str(config["delivery_target"]),
                actor_ref=actor_ref,
            )

    def _execute(
        self,
        *,
        job_name: str,
        scheduled_for: str,
        timezone: str,
        portfolio_id: str | None,
        account_id: str | None,
        config: JsonDict,
    ) -> tuple[JsonDict, str, bool, str]:
        business_date = self._business_date(scheduled_for, timezone=timezone)
        if job_name == "SYSTEM_DOCTOR":
            report = build_doctor_report(self.settings).model_dump(mode="json")
            notify = report["status"] != "PASS"
            return (
                report,
                "PASS" if not notify else "SOURCE_ERROR",
                notify,
                ("SYSTEM_HEALTHY" if not notify else "SYSTEM_HEALTH_FAILED"),
            )
        assert portfolio_id is not None and account_id is not None
        if job_name == "DAILY_MARKET_SYNC":
            holdings = self._ledger.list_holdings(portfolio_id=portfolio_id, account_id=account_id)
            codes = [
                str(item["instrument_code"])
                for item in holdings
                if Decimal(str(item["total_shares"])) != 0
            ]
            if not codes:
                return (
                    {"items": [], "requested_count": 0},
                    "PASS",
                    False,
                    "NO_ACTIVE_HOLDINGS",
                )
            result = self._market_sync.sync_navs(
                provider_id=str(config["provider_id"]),
                instrument_codes=codes,
                as_of_date_value=business_date,
                actor_ref="cron",
            )
            quality = str(result["data_quality"])
            notify = str(result["status"]) != "PASS"
            return (
                result,
                quality,
                notify,
                ("MARKET_SYNC_COMPLETED" if not notify else "MARKET_SYNC_DEGRADED"),
            )
        if job_name == "DAILY_RISK_SCAN":
            result = self._risk.scan(
                portfolio_id=portfolio_id,
                account_id=account_id,
                as_of_date=business_date,
            )
            proposals = list(result["sell_proposals"])
            notify = bool(proposals) or str(result["state"]) == "DATA_BLOCKED"
            return result, str(result["data_quality"]), notify, str(result["reason_code"])
        if job_name == "WEEKLY_PLAN_PREPARE":
            result = self._planning.create_draft(
                portfolio_id=portfolio_id,
                account_id=account_id,
                contribution_amount=str(config["contribution_amount"]),
                plan_date_value=business_date,
                as_of_date_value=business_date,
                idempotency_key=f"automation-weekly:{portfolio_id}:{business_date}",
                actor_ref="cron",
            )
            facts = {key: value for key, value in result.items() if key != "confirmation_token"}
            return (
                facts,
                ("WARNING" if result["warnings"] else "PASS"),
                True,
                "WEEKLY_PLAN_DRAFT_READY",
            )
        if job_name == "SELL_FOLLOWUP_DUE":
            followups = self._risk.list_followups(portfolio_id=portfolio_id, status=None, limit=500)
            due = [
                item
                for item in followups
                if str(item["status"]) in {"PENDING", "DUE", "DATA_BLOCKED"}
                and str(item["due_at"]) <= business_date
            ]
            items = [
                self._risk.evaluate_followup(
                    followup_id=str(item["id"]),
                    as_of_date=business_date,
                    actor_ref="cron",
                )
                for item in due
            ]
            blocked = any(str(item["status"]) == "DATA_BLOCKED" for item in items)
            return (
                {"items": items, "due_count": len(due)},
                "SOURCE_ERROR" if blocked else "PASS",
                bool(items),
                "SELL_FOLLOWUPS_DUE" if items else "NO_SELL_FOLLOWUP_DUE",
            )
        if job_name in {"MONTHLY_REVIEW", "QUARTERLY_REVIEW", "ANNUAL_REVIEW"}:
            review_type = job_name.removesuffix("_REVIEW")
            # Scheduled review jobs always close the immediately completed period.
            result = self._performance.prepare_review(
                portfolio_id=portfolio_id,
                review_type=review_type,
                anchor_date=datetime.fromisoformat(business_date).date() - timedelta(days=1),
            )
            quality = str(result["data_quality"])
            return (
                result,
                quality,
                True,
                str(result["reason_code"]),
            )
        raise AssertionError(f"unsupported job dispatch: {job_name}")

    def _record_skip(
        self,
        *,
        job_name: str,
        scheduled_for: str,
        portfolio_id: str | None,
        reason_code: str,
        policy: JsonDict | None = None,
    ) -> JsonDict:
        key = f"{job_name}:{portfolio_id or 'global'}:{scheduled_for}:{reason_code}"
        created = False
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM job_runs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is None:
                created = True
                run_id = str(uuid4())
                timestamp = _iso(self._now())
                output = {"reason_code": reason_code, "policy": policy}
                connection.execute(
                    """
                    INSERT INTO job_runs (
                        id, job_name, scheduled_for, idempotency_key, status,
                        started_at, finished_at, input_json, output_json,
                        error_code, error_summary, trace_id, attempt_count,
                        max_attempts, heartbeat_at, next_retry_at
                    ) VALUES (?, ?, ?, ?, 'SKIPPED', ?, ?, '{}', ?, NULL, NULL,
                              ?, 1, 1, ?, NULL)
                    """,
                    (
                        run_id,
                        job_name,
                        scheduled_for,
                        key,
                        timestamp,
                        timestamp,
                        _json(output),
                        str(uuid4()),
                        timestamp,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM job_runs WHERE id=?", (run_id,)
                ).fetchone()
                assert existing is not None
            return {
                "job_run": self._run_data(existing),
                "idempotent_replay": not created,
                "display_text": "[SILENT]",
            }

    def _finish_success(
        self,
        *,
        run_id: str,
        job_name: str,
        scheduled_for: str,
        portfolio_id: str | None,
        facts: JsonDict,
        quality: str,
        notify: bool,
        reason_code: str,
        delivery_target: str,
        actor_ref: str,
    ) -> JsonDict:
        timestamp = _iso(self._now())
        facts_hash = _hash(facts)
        bundle_id = str(uuid4())
        display_text = f"{job_name} 已生成待查看事实包, 数据质量: {quality}, 原因: {reason_code}。"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE job_runs SET status=?, finished_at=?, heartbeat_at=?,
                    output_json=?, error_code=NULL, error_summary=NULL,
                    next_retry_at=NULL
                WHERE id=?
                """,
                (
                    "DEGRADED" if quality != "PASS" else "SUCCESS",
                    timestamp,
                    timestamp,
                    _json(
                        {
                            "reason_code": reason_code,
                            "data_quality": quality,
                            "delivery_action": "NOTIFY" if notify else "SILENT",
                            "facts_hash": facts_hash,
                        }
                    ),
                    run_id,
                ),
            )
            existing = connection.execute(
                """
                SELECT * FROM report_bundles
                WHERE portfolio_id IS ? AND bundle_type=?
                  AND scheduled_for=? AND facts_hash=?
                """,
                (portfolio_id, job_name, scheduled_for, facts_hash),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO report_bundles (
                        id, portfolio_id, job_run_id, bundle_type, scheduled_for,
                        facts_json, facts_hash, data_quality, delivery_action,
                        reason_code, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bundle_id,
                        portfolio_id,
                        run_id,
                        job_name,
                        scheduled_for,
                        _json(facts),
                        facts_hash,
                        quality,
                        "NOTIFY" if notify else "SILENT",
                        reason_code,
                        timestamp,
                    ),
                )
                if notify:
                    connection.execute(
                        """
                        INSERT INTO notification_outbox (
                            id, report_bundle_id, alert_id, delivery_target,
                            status, dedup_key, attempt_count, max_attempts,
                            next_attempt_at, last_error_code, created_at, sent_at
                        ) VALUES (?, ?, NULL, ?, 'PENDING', ?, 0, 5, ?, NULL, ?, NULL)
                        """,
                        (
                            str(uuid4()),
                            bundle_id,
                            delivery_target,
                            f"bundle:{bundle_id}:{delivery_target}",
                            timestamp,
                            timestamp,
                        ),
                    )
                self._audit(
                    connection,
                    action="AUTOMATION_REPORT_BUNDLE_CREATED",
                    entity_type="report_bundle",
                    entity_id=bundle_id,
                    actor_ref=actor_ref,
                    details={
                        "job_name": job_name,
                        "delivery_action": "NOTIFY" if notify else "SILENT",
                        "reason_code": reason_code,
                    },
                )
                row = connection.execute(
                    "SELECT * FROM report_bundles WHERE id=?", (bundle_id,)
                ).fetchone()
            else:
                row = existing
            assert row is not None
            data = self._bundle_data(row)
            data["display_text"] = display_text
            return data

    def _finish_failure(
        self,
        *,
        run_id: str,
        job_name: str,
        portfolio_id: str | None,
        scheduled_for: str,
        error: LedgerError,
        attempt_count: int,
        max_attempts: int,
        delivery_target: str,
        actor_ref: str,
    ) -> JsonDict:
        now = self._now()
        retry_minutes = (5, 15, 30, 60, 120)[min(attempt_count - 1, 4)]
        next_retry = (
            _iso(now + timedelta(minutes=retry_minutes)) if attempt_count < max_attempts else None
        )
        context = {
            "job_name": job_name,
            "scheduled_for": scheduled_for,
            "error_code": error.code,
            "error_summary": error.message,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "next_retry_at": next_retry,
        }
        fingerprint = _hash(
            {
                "portfolio_id": portfolio_id,
                "job_name": job_name,
                "scheduled_for": scheduled_for,
                "error_code": error.code,
            }
        )
        timestamp = _iso(now)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE job_runs
                SET status='FAILED', finished_at=?, heartbeat_at=?,
                    output_json=?, error_code=?, error_summary=?,
                    next_retry_at=?
                WHERE id=?
                """,
                (
                    timestamp,
                    timestamp,
                    _json(context),
                    error.code,
                    error.message,
                    next_retry,
                    run_id,
                ),
            )
            alert = connection.execute(
                "SELECT * FROM alerts WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if alert is None:
                alert_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO alerts (
                        id, portfolio_id, job_run_id, code, severity, status,
                        fingerprint, context_json, occurrence_count, created_at,
                        last_seen_at, acknowledged_at, acknowledged_by
                    ) VALUES (?, ?, ?, ?, 'CRITICAL', 'OPEN', ?, ?, 1, ?, ?, NULL, NULL)
                    """,
                    (
                        alert_id,
                        portfolio_id,
                        run_id,
                        error.code,
                        fingerprint,
                        _json(context),
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO notification_outbox (
                        id, report_bundle_id, alert_id, delivery_target, status,
                        dedup_key, attempt_count, max_attempts, next_attempt_at,
                        last_error_code, created_at, sent_at
                    ) VALUES (?, NULL, ?, ?, 'PENDING', ?, 0, 5, ?, NULL, ?, NULL)
                    """,
                    (
                        str(uuid4()),
                        alert_id,
                        delivery_target,
                        f"alert:{alert_id}:{delivery_target}",
                        timestamp,
                        timestamp,
                    ),
                )
                self._audit(
                    connection,
                    action="AUTOMATION_FAILURE_ALERT_CREATED",
                    entity_type="alert",
                    entity_id=alert_id,
                    actor_ref=actor_ref,
                    details=context,
                )
            else:
                alert_id = str(alert["id"])
                connection.execute(
                    """
                    UPDATE alerts
                    SET occurrence_count=occurrence_count+1, last_seen_at=?,
                        context_json=?, status='OPEN'
                    WHERE id=?
                    """,
                    (timestamp, _json(context), alert_id),
                )
            run = connection.execute("SELECT * FROM job_runs WHERE id=?", (run_id,)).fetchone()
            assert run is not None
        return {
            "job_run": self._run_data(run),
            "alert_id": alert_id,
            "idempotent_replay": False,
            "display_text": (
                f"{job_name} 执行失败: {error.code}。"
                f"{' 已安排重试。' if next_retry else ' 已达到最大重试次数。'}"
            ),
        }

    @staticmethod
    def _run_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "job_name": str(row["job_name"]),
            "scheduled_for": str(row["scheduled_for"]),
            "idempotency_key": str(row["idempotency_key"]),
            "status": str(row["status"]),
            "started_at": str(row["started_at"]),
            "finished_at": row["finished_at"],
            "input": json.loads(str(row["input_json"])),
            "output": json.loads(str(row["output_json"])),
            "error_code": row["error_code"],
            "error_summary": row["error_summary"],
            "attempt_count": int(row["attempt_count"]),
            "max_attempts": int(row["max_attempts"]),
            "heartbeat_at": row["heartbeat_at"],
            "next_retry_at": row["next_retry_at"],
        }

    def get_run(self, *, run_id: str) -> JsonDict:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM job_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise LedgerError(
                    "AUTOMATION_RUN_NOT_FOUND",
                    "automation job run was not found",
                    http_status=404,
                )
            return self._run_data(row)

    def list_runs(
        self, *, job_name: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[JsonDict]:
        if limit < 1 or limit > 500:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 500")
        query = "SELECT * FROM job_runs WHERE 1=1"
        params: list[object] = []
        if job_name:
            query += " AND job_name=?"
            params.append(self._normalize_job(job_name))
        if status:
            query += " AND status=?"
            params.append(status.strip().upper())
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._run_data(row) for row in rows]

    @staticmethod
    def _bundle_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "portfolio_id": row["portfolio_id"],
            "job_run_id": str(row["job_run_id"]),
            "bundle_type": str(row["bundle_type"]),
            "scheduled_for": str(row["scheduled_for"]),
            "facts": json.loads(str(row["facts_json"])),
            "facts_hash": str(row["facts_hash"]),
            "data_quality": str(row["data_quality"]),
            "delivery_action": str(row["delivery_action"]),
            "reason_code": str(row["reason_code"]),
            "created_at": str(row["created_at"]),
        }

    def list_report_bundles(
        self,
        *,
        portfolio_id: str | None = None,
        bundle_type: str | None = None,
        delivery_action: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        query = "SELECT * FROM report_bundles WHERE 1=1"
        params: list[object] = []
        if portfolio_id:
            query += " AND portfolio_id=?"
            params.append(portfolio_id)
        if bundle_type:
            query += " AND bundle_type=?"
            params.append(self._normalize_job(bundle_type))
        if delivery_action:
            query += " AND delivery_action=?"
            params.append(delivery_action.strip().upper())
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._bundle_data(row) for row in rows]

    def list_alerts(
        self,
        *,
        portfolio_id: str | None = None,
        status: str | None = "OPEN",
        limit: int = 100,
    ) -> list[JsonDict]:
        query = "SELECT * FROM alerts WHERE 1=1"
        params: list[object] = []
        if portfolio_id:
            query += " AND portfolio_id=?"
            params.append(portfolio_id)
        if status:
            query += " AND status=?"
            params.append(status.strip().upper())
        query += " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [
            {
                "id": str(row["id"]),
                "portfolio_id": row["portfolio_id"],
                "job_run_id": str(row["job_run_id"]),
                "code": str(row["code"]),
                "severity": str(row["severity"]),
                "status": str(row["status"]),
                "context": json.loads(str(row["context_json"])),
                "occurrence_count": int(row["occurrence_count"]),
                "created_at": str(row["created_at"]),
                "last_seen_at": str(row["last_seen_at"]),
                "acknowledged_at": row["acknowledged_at"],
                "acknowledged_by": row["acknowledged_by"],
            }
            for row in rows
        ]

    def list_outbox(self, *, status: str | None = "PENDING", limit: int = 100) -> list[JsonDict]:
        query = "SELECT * FROM notification_outbox WHERE 1=1"
        params: list[object] = []
        if status:
            query += " AND status=?"
            params.append(status.strip().upper())
        query += " ORDER BY created_at LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._outbox_data(row) for row in rows]

    def create_notification_test(
        self,
        *,
        idempotency_key: str,
        confirmation: str,
        actor_ref: str = "hermes",
    ) -> JsonDict:
        """Queue one fixed, non-financial test message through the real outbox."""
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise LedgerError(
                "NOTIFICATION_TEST_IDEMPOTENCY_KEY_INVALID",
                "idempotency_key must be between 1 and 200 characters",
            )
        if confirmation != NOTIFICATION_TEST_CONFIRMATION:
            raise LedgerError(
                "NOTIFICATION_TEST_CONFIRMATION_REQUIRED",
                f"confirmation must exactly equal {NOTIFICATION_TEST_CONFIRMATION}",
            )
        now = self._now()
        timestamp = _iso(now)
        cooldown_cutoff = _iso(
            now - timedelta(seconds=NOTIFICATION_TEST_COOLDOWN_SECONDS)
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT n.*, o.id AS outbox_id
                    FROM notification_test_requests n
                    JOIN notification_outbox o
                      ON o.notification_test_request_id=n.id
                    WHERE n.idempotency_key=?
                    """,
                    (key,),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    result = self.get_notification_test(
                        test_request_id=str(existing["id"])
                    )
                    result["idempotent_replay"] = True
                    return result
                recent = connection.execute(
                    """
                    SELECT id, created_at FROM notification_test_requests
                    WHERE created_at > ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (cooldown_cutoff,),
                ).fetchone()
                if recent is not None:
                    raise LedgerError(
                        "NOTIFICATION_TEST_RATE_LIMITED",
                        "a notification test was already queued within the cooldown window",
                        details={
                            "cooldown_seconds": NOTIFICATION_TEST_COOLDOWN_SECONDS,
                            "latest_test_request_id": str(recent["id"]),
                        },
                    )
                test_id = str(uuid4())
                outbox_id = str(uuid4())
                display_text = (
                    "Value DCA 通知链路测试\n"
                    f"测试 ID: {test_id}\n"
                    f"创建时间: {timestamp}\n"
                    "此消息只验证 Core、通知队列、Hermes 与渠道回执;"
                    "不会创建交易, 也不会修改持仓或策略。"
                )
                connection.execute(
                    """
                    INSERT INTO notification_test_requests (
                        id, idempotency_key, display_text, delivery_target,
                        requested_by, created_at
                    ) VALUES (?, ?, ?, 'origin', ?, ?)
                    """,
                    (test_id, key, display_text, actor_ref, timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO notification_outbox (
                        id, report_bundle_id, alert_id,
                        notification_test_request_id, delivery_target, status,
                        dedup_key, attempt_count, max_attempts, next_attempt_at,
                        last_error_code, created_at, sent_at
                    ) VALUES (?, NULL, NULL, ?, 'origin', 'PENDING', ?, 0, 5,
                              ?, NULL, ?, NULL)
                    """,
                    (
                        outbox_id,
                        test_id,
                        f"notification-test:{test_id}:origin",
                        timestamp,
                        timestamp,
                    ),
                )
                self._audit(
                    connection,
                    action="NOTIFICATION_TEST_QUEUED",
                    entity_type="notification_test_request",
                    entity_id=test_id,
                    actor_ref=actor_ref,
                    details={
                        "outbox_id": outbox_id,
                        "delivery_target": "origin",
                        "financial_state_changed": False,
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        result = self.get_notification_test(test_request_id=test_id)
        result["idempotent_replay"] = False
        return result

    def get_notification_test(self, *, test_request_id: str) -> JsonDict:
        """Read the durable outbox and receipt state for one test request."""
        with self._connect() as connection:
            request = connection.execute(
                "SELECT * FROM notification_test_requests WHERE id=?",
                (test_request_id,),
            ).fetchone()
            if request is None:
                raise LedgerError(
                    "NOTIFICATION_TEST_NOT_FOUND",
                    "notification test request was not found",
                    http_status=404,
                )
            outbox = connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE notification_test_request_id=?
                """,
                (test_request_id,),
            ).fetchone()
            assert outbox is not None
            attempts = connection.execute(
                """
                SELECT * FROM notification_delivery_attempts
                WHERE outbox_id=? ORDER BY attempt_number DESC
                """,
                (outbox["id"],),
            ).fetchall()
        return {
            "test_request": {
                "id": str(request["id"]),
                "delivery_target": str(request["delivery_target"]),
                "requested_by": str(request["requested_by"]),
                "created_at": str(request["created_at"]),
            },
            "outbox": self._outbox_data(outbox),
            "attempts": [self._delivery_attempt_data(row) for row in attempts],
            "safety": {
                "holdings_changed": False,
                "transactions_created": False,
                "strategy_changed": False,
                "automatic_trade": False,
            },
            "display_text": (
                "通知测试已投递。"
                if str(outbox["status"]) == "DELIVERED"
                else "通知测试已进入真实投递队列, 等待 Hermes 渠道回执。"
            ),
        }

    @staticmethod
    def _delivery_payload(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> JsonDict:
        if row["report_bundle_id"] is not None:
            bundle = connection.execute(
                "SELECT * FROM report_bundles WHERE id=?",
                (row["report_bundle_id"],),
            ).fetchone()
            assert bundle is not None
            return {
                "source_type": "REPORT_BUNDLE",
                "source_id": str(bundle["id"]),
                "display_text": (
                    f"{bundle['bundle_type']} 自动化事实包\n"
                    f"计划时间: {bundle['scheduled_for']}\n"
                    f"数据质量: {bundle['data_quality']}\n"
                    f"原因码: {bundle['reason_code']}"
                ),
                "facts": json.loads(str(bundle["facts_json"])),
                "facts_hash": str(bundle["facts_hash"]),
            }
        if row["notification_test_request_id"] is not None:
            request = connection.execute(
                "SELECT * FROM notification_test_requests WHERE id=?",
                (row["notification_test_request_id"],),
            ).fetchone()
            assert request is not None
            facts = {
                "test_request_id": str(request["id"]),
                "purpose": "END_TO_END_NOTIFICATION_DELIVERY_TEST",
                "financial_state_changed": False,
            }
            return {
                "source_type": "NOTIFICATION_TEST",
                "source_id": str(request["id"]),
                "display_text": str(request["display_text"]),
                "facts": facts,
                "facts_hash": _hash(facts),
            }
        alert = connection.execute(
            "SELECT * FROM alerts WHERE id=?",
            (row["alert_id"],),
        ).fetchone()
        assert alert is not None
        return {
            "source_type": "ALERT",
            "source_id": str(alert["id"]),
            "display_text": (
                f"Value DCA 自动化告警\n"
                f"严重度: {alert['severity']}\n"
                f"错误码: {alert['code']}"
            ),
            "facts": json.loads(str(alert["context_json"])),
            "facts_hash": _hash(json.loads(str(alert["context_json"]))),
        }

    def claim_delivery_attempts(
        self,
        *,
        delivery_target: str | None = None,
        limit: int = 20,
        actor_ref: str = "hermes-delivery-adapter",
    ) -> JsonDict:
        """Claim due outbox records without treating dispatch as channel delivery."""
        if limit < 1 or limit > 100:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 100")
        normalized_target = delivery_target.strip() if delivery_target else None
        if normalized_target is not None and (
            not normalized_target or len(normalized_target) > 200
        ):
            raise LedgerError(
                "DELIVERY_TARGET_INVALID",
                "delivery_target must be between 1 and 200 characters",
            )
        now = self._now()
        timestamp = _iso(now)
        receipt_deadline = _iso(now + timedelta(minutes=DELIVERY_RECEIPT_TIMEOUT_MINUTES))
        claimed: list[JsonDict] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                stale = connection.execute(
                    """
                    SELECT * FROM notification_outbox
                    WHERE status='DISPATCHED' AND next_attempt_at IS NOT NULL
                      AND next_attempt_at <= ?
                    ORDER BY next_attempt_at, created_at
                    """,
                    (timestamp,),
                ).fetchall()
                for row in stale:
                    attempt = connection.execute(
                        """
                        SELECT * FROM notification_delivery_attempts
                        WHERE outbox_id=? AND status='DISPATCHED'
                        ORDER BY attempt_number DESC LIMIT 1
                        """,
                        (row["id"],),
                    ).fetchone()
                    if attempt is not None:
                        connection.execute(
                            """
                            UPDATE notification_delivery_attempts
                            SET status='TIMED_OUT', error_code='DELIVERY_RECEIPT_TIMEOUT',
                                receipt_at=?
                            WHERE id=?
                            """,
                            (timestamp, attempt["id"]),
                        )
                    exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
                    connection.execute(
                        """
                        UPDATE notification_outbox
                        SET status=?, next_attempt_at=?, last_error_code=?
                        WHERE id=?
                        """,
                        (
                            "FAILED" if exhausted else "PENDING",
                            None if exhausted else timestamp,
                            "DELIVERY_RECEIPT_TIMEOUT",
                            row["id"],
                        ),
                    )

                query = """
                    SELECT * FROM notification_outbox
                    WHERE status='PENDING' AND attempt_count < max_attempts
                      AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                """
                params: list[object] = [timestamp]
                if normalized_target is not None:
                    query += " AND delivery_target=?"
                    params.append(normalized_target)
                query += " ORDER BY created_at LIMIT ?"
                params.append(limit)
                rows = connection.execute(query, params).fetchall()
                for row in rows:
                    token = secrets.token_urlsafe(32)
                    attempt_id = str(uuid4())
                    attempt_number = int(row["attempt_count"]) + 1
                    connection.execute(
                        """
                        UPDATE notification_outbox
                        SET status='DISPATCHED', attempt_count=?, next_attempt_at=?,
                            last_error_code=NULL, dispatched_at=?
                        WHERE id=?
                        """,
                        (
                            attempt_number,
                            receipt_deadline,
                            timestamp,
                            row["id"],
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO notification_delivery_attempts (
                            id, outbox_id, attempt_number, status, receipt_digest,
                            delivery_target, provider, provider_message_id,
                            evidence_json, error_code, claimed_at, receipt_at
                        ) VALUES (?, ?, ?, 'DISPATCHED', ?, ?, NULL, NULL,
                                  '{}', NULL, ?, NULL)
                        """,
                        (
                            attempt_id,
                            row["id"],
                            attempt_number,
                            _token_digest(token),
                            row["delivery_target"],
                            timestamp,
                        ),
                    )
                    claimed.append(
                        {
                            "outbox_id": str(row["id"]),
                            "attempt_id": attempt_id,
                            "attempt_number": attempt_number,
                            "receipt_token": token,
                            "delivery_target": str(row["delivery_target"]),
                            "receipt_deadline": receipt_deadline,
                            "payload": self._delivery_payload(connection, row),
                        }
                    )
                    self._audit(
                        connection,
                        action="NOTIFICATION_DISPATCH_CLAIMED",
                        entity_type="notification_delivery_attempt",
                        entity_id=attempt_id,
                        actor_ref=actor_ref,
                        details={
                            "outbox_id": str(row["id"]),
                            "attempt_number": attempt_number,
                            "delivery_target": str(row["delivery_target"]),
                            "receipt_deadline": receipt_deadline,
                        },
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "claimed_count": len(claimed),
            "items": claimed,
            "delivery_state": "DISPATCHED" if claimed else "EMPTY",
            "delivered_count": 0,
            "display_text": "[SILENT]" if not claimed else "NOTIFICATION_DISPATCH_CLAIMED",
        }

    def record_delivery_receipt(
        self,
        *,
        outbox_id: str,
        attempt_id: str,
        receipt_token: str,
        outcome: str,
        provider: str,
        provider_message_id: str | None,
        evidence: JsonDict,
        error_code: str | None,
        actor_ref: str = "hermes-delivery-adapter",
    ) -> JsonDict:
        """Record channel evidence; only a verified DELIVERED receipt means delivered."""
        normalized_outcome = outcome.strip().upper()
        if normalized_outcome not in {"DELIVERED", "FAILED"}:
            raise LedgerError(
                "DELIVERY_OUTCOME_INVALID",
                "outcome must be DELIVERED or FAILED",
            )
        normalized_provider = provider.strip()
        if not normalized_provider or len(normalized_provider) > 120:
            raise LedgerError(
                "DELIVERY_PROVIDER_INVALID",
                "provider must be between 1 and 120 characters",
            )
        normalized_message_id = provider_message_id.strip() if provider_message_id else None
        if normalized_outcome == "DELIVERED" and not normalized_message_id:
            raise LedgerError(
                "DELIVERY_EVIDENCE_REQUIRED",
                "DELIVERED requires a provider_message_id",
            )
        normalized_error = error_code.strip().upper() if error_code else None
        if normalized_outcome == "FAILED" and not normalized_error:
            raise LedgerError(
                "DELIVERY_ERROR_REQUIRED",
                "FAILED requires an error_code",
            )
        timestamp = _iso(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                attempt = connection.execute(
                    """
                    SELECT * FROM notification_delivery_attempts
                    WHERE id=? AND outbox_id=?
                    """,
                    (attempt_id, outbox_id),
                ).fetchone()
                if attempt is None:
                    raise LedgerError(
                        "DELIVERY_ATTEMPT_NOT_FOUND",
                        "delivery attempt was not found",
                    )
                if not hmac.compare_digest(
                    str(attempt["receipt_digest"]),
                    _token_digest(receipt_token),
                ):
                    raise LedgerError(
                        "DELIVERY_RECEIPT_TOKEN_INVALID",
                        "delivery receipt token does not match",
                    )
                current_attempt_status = str(attempt["status"])
                if current_attempt_status in {"DELIVERED", "FAILED"}:
                    if current_attempt_status != normalized_outcome:
                        raise LedgerError(
                            "DELIVERY_RECEIPT_CONFLICT",
                            "delivery attempt already has a different terminal outcome",
                        )
                    outbox = connection.execute(
                        "SELECT * FROM notification_outbox WHERE id=?",
                        (outbox_id,),
                    ).fetchone()
                    assert outbox is not None
                    connection.commit()
                    return {
                        "outbox": self._outbox_data(outbox),
                        "attempt": self._delivery_attempt_data(attempt),
                        "idempotent_replay": True,
                        "delivered": normalized_outcome == "DELIVERED",
                    }
                if current_attempt_status != "DISPATCHED":
                    raise LedgerError(
                        "DELIVERY_ATTEMPT_NOT_RECEIPTABLE",
                        "delivery attempt is not awaiting a receipt",
                    )
                outbox = connection.execute(
                    "SELECT * FROM notification_outbox WHERE id=?",
                    (outbox_id,),
                ).fetchone()
                assert outbox is not None
                connection.execute(
                    """
                    UPDATE notification_delivery_attempts
                    SET status=?, provider=?, provider_message_id=?,
                        evidence_json=?, error_code=?, receipt_at=?
                    WHERE id=?
                    """,
                    (
                        normalized_outcome,
                        normalized_provider,
                        normalized_message_id,
                        _json(evidence),
                        normalized_error,
                        timestamp,
                        attempt_id,
                    ),
                )
                if normalized_outcome == "DELIVERED":
                    connection.execute(
                        """
                        UPDATE notification_outbox
                        SET status='DELIVERED', next_attempt_at=NULL,
                            last_error_code=NULL, delivered_at=?,
                            provider_message_id=?, sent_at=?
                        WHERE id=?
                        """,
                        (
                            timestamp,
                            normalized_message_id,
                            timestamp,
                            outbox_id,
                        ),
                    )
                else:
                    exhausted = int(outbox["attempt_count"]) >= int(outbox["max_attempts"])
                    retry_index = min(
                        max(int(outbox["attempt_count"]) - 1, 0),
                        len(DELIVERY_RETRY_MINUTES) - 1,
                    )
                    next_attempt = (
                        None
                        if exhausted
                        else _iso(
                            self._now()
                            + timedelta(minutes=DELIVERY_RETRY_MINUTES[retry_index])
                        )
                    )
                    connection.execute(
                        """
                        UPDATE notification_outbox
                        SET status=?, next_attempt_at=?, last_error_code=?
                        WHERE id=?
                        """,
                        (
                            "FAILED" if exhausted else "PENDING",
                            next_attempt,
                            normalized_error,
                            outbox_id,
                        ),
                    )
                self._audit(
                    connection,
                    action=f"NOTIFICATION_DELIVERY_{normalized_outcome}",
                    entity_type="notification_delivery_attempt",
                    entity_id=attempt_id,
                    actor_ref=actor_ref,
                    details={
                        "outbox_id": outbox_id,
                        "provider": normalized_provider,
                        "provider_message_id": normalized_message_id,
                        "error_code": normalized_error,
                        "evidence_hash": _hash(evidence),
                    },
                )
                updated_outbox = connection.execute(
                    "SELECT * FROM notification_outbox WHERE id=?",
                    (outbox_id,),
                ).fetchone()
                updated_attempt = connection.execute(
                    "SELECT * FROM notification_delivery_attempts WHERE id=?",
                    (attempt_id,),
                ).fetchone()
                assert updated_outbox is not None and updated_attempt is not None
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "outbox": self._outbox_data(updated_outbox),
            "attempt": self._delivery_attempt_data(updated_attempt),
            "idempotent_replay": False,
            "delivered": normalized_outcome == "DELIVERED",
        }

    @staticmethod
    def _outbox_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "report_bundle_id": row["report_bundle_id"],
            "alert_id": row["alert_id"],
            "notification_test_request_id": row["notification_test_request_id"],
            "delivery_target": str(row["delivery_target"]),
            "status": str(row["status"]),
            "attempt_count": int(row["attempt_count"]),
            "max_attempts": int(row["max_attempts"]),
            "next_attempt_at": row["next_attempt_at"],
            "last_error_code": row["last_error_code"],
            "created_at": str(row["created_at"]),
            "dispatched_at": row["dispatched_at"],
            "delivered_at": row["delivered_at"],
            "provider_message_id": row["provider_message_id"],
            "sent_at": row["sent_at"],
        }

    @staticmethod
    def _delivery_attempt_data(row: sqlite3.Row) -> JsonDict:
        return {
            "id": str(row["id"]),
            "outbox_id": str(row["outbox_id"]),
            "attempt_number": int(row["attempt_number"]),
            "status": str(row["status"]),
            "delivery_target": str(row["delivery_target"]),
            "provider": row["provider"],
            "provider_message_id": row["provider_message_id"],
            "evidence": json.loads(str(row["evidence_json"])),
            "error_code": row["error_code"],
            "claimed_at": str(row["claimed_at"]),
            "receipt_at": row["receipt_at"],
        }

    def list_delivery_attempts(
        self,
        *,
        outbox_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        if limit < 1 or limit > 500:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 500")
        query = "SELECT * FROM notification_delivery_attempts WHERE 1=1"
        params: list[object] = []
        if outbox_id:
            query += " AND outbox_id=?"
            params.append(outbox_id)
        if status:
            query += " AND status=?"
            params.append(status.strip().upper())
        query += " ORDER BY claimed_at DESC, attempt_number DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._delivery_attempt_data(row) for row in rows]

    def list_missed_runs(
        self,
        *,
        grace_minutes: int = MISSED_RUN_GRACE_MINUTES,
        lookback_days: int = MISSED_RUN_LOOKBACK_DAYS,
        limit: int = 100,
    ) -> list[JsonDict]:
        """List latest approved schedule occurrences with no durable run record."""
        if grace_minutes < 1 or grace_minutes > 1440:
            raise LedgerError(
                "AUTOMATION_RECOVERY_WINDOW_INVALID",
                "grace_minutes must be between 1 and 1440",
            )
        if lookback_days < 1 or lookback_days > 31:
            raise LedgerError(
                "AUTOMATION_RECOVERY_WINDOW_INVALID",
                "lookback_days must be between 1 and 31",
            )
        if limit < 1 or limit > 100:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 100")
        now = self._now()
        cutoff = now - timedelta(minutes=grace_minutes)
        earliest = now - timedelta(days=lookback_days)
        with self._connect() as connection:
            policies = connection.execute(
                """
                SELECT * FROM automation_policies
                WHERE status='ACTIVE' AND enabled=1
                ORDER BY approved_at, job_name, portfolio_id
                """
            ).fetchall()
            missed: list[JsonDict] = []
            for policy in policies:
                occurrence = self._scheduled_occurrence(policy, before=cutoff)
                approved_at = datetime.fromisoformat(
                    str(policy["approved_at"]).replace("Z", "+00:00")
                )
                if occurrence < approved_at or occurrence < earliest:
                    continue
                scheduled_for = _iso(occurrence)
                existing = self._find_existing_run(
                    connection,
                    job_name=str(policy["job_name"]),
                    portfolio_id=(
                        str(policy["portfolio_id"]) if policy["portfolio_id"] is not None else None
                    ),
                    scheduled_for=scheduled_for,
                    policy_version=int(policy["version"]),
                    timezone=str(policy["timezone"]),
                )
                if existing is not None:
                    continue
                missed.append(
                    {
                        "policy_id": str(policy["id"]),
                        "policy_version": int(policy["version"]),
                        "portfolio_id": policy["portfolio_id"],
                        "job_name": str(policy["job_name"]),
                        "scheduled_for": scheduled_for,
                        "timezone": str(policy["timezone"]),
                        "detected_at": _iso(now),
                        "grace_minutes": grace_minutes,
                        "lookback_days": lookback_days,
                        "recovery_state": "DUE",
                    }
                )
        return sorted(
            missed,
            key=lambda item: (str(item["scheduled_for"]), str(item["job_name"])),
        )[:limit]

    def catch_up_due(
        self,
        *,
        grace_minutes: int = MISSED_RUN_GRACE_MINUTES,
        lookback_days: int = MISSED_RUN_LOOKBACK_DAYS,
        limit: int = 20,
    ) -> JsonDict:
        """Run the latest missed occurrence per active policy after a safety grace period."""
        missed = self.list_missed_runs(
            grace_minutes=grace_minutes,
            lookback_days=lookback_days,
            limit=limit,
        )
        results: list[JsonDict] = []
        for item in missed:
            results.append(
                self.run_job(
                    job_name=str(item["job_name"]),
                    scheduled_for=str(item["scheduled_for"]),
                    portfolio_id=(
                        str(item["portfolio_id"]) if item["portfolio_id"] is not None else None
                    ),
                    actor_ref="operations-catch-up",
                )
            )
        displays = [
            str(item["display_text"])
            for item in results
            if str(item.get("display_text", "[SILENT]")) != "[SILENT]"
        ]
        return {
            "recovered_count": len(results),
            "items": results,
            "grace_minutes": grace_minutes,
            "lookback_days": lookback_days,
            "display_text": "\n".join(displays) if displays else "[SILENT]",
            "automatic_trade": False,
        }

    def status_summary(self) -> JsonDict:
        """Return deterministic automation state without running or delivering anything."""
        with self._connect() as connection:
            policies = connection.execute(
                """
                SELECT job_name, portfolio_id, version, enabled, schedule, timezone,
                       approved_at
                FROM automation_policies
                WHERE status='ACTIVE'
                ORDER BY job_name, portfolio_id
                """
            ).fetchall()
            run_counts = connection.execute(
                "SELECT status, COUNT(*) AS count FROM job_runs GROUP BY status"
            ).fetchall()
            latest_runs = connection.execute(
                """
                SELECT * FROM job_runs
                WHERE job_name IN (
                    'DAILY_MARKET_SYNC','DAILY_RISK_SCAN','WEEKLY_PLAN_PREPARE',
                    'SELL_FOLLOWUP_DUE','SYSTEM_DOCTOR','MONTHLY_REVIEW',
                    'QUARTERLY_REVIEW','ANNUAL_REVIEW'
                )
                ORDER BY started_at DESC LIMIT 20
                """
            ).fetchall()
            open_alerts = int(
                connection.execute("SELECT COUNT(*) FROM alerts WHERE status='OPEN'").fetchone()[0]
            )
            pending_outbox = int(
                connection.execute(
                    "SELECT COUNT(*) FROM notification_outbox WHERE status='PENDING'"
                ).fetchone()[0]
            )
            outbox_counts = connection.execute(
                "SELECT status, COUNT(*) AS count FROM notification_outbox GROUP BY status"
            ).fetchall()
            due_retries = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM job_runs
                    WHERE status='FAILED' AND next_retry_at IS NOT NULL
                      AND next_retry_at <= ? AND attempt_count < max_attempts
                    """,
                    (_iso(self._now()),),
                ).fetchone()[0]
            )
        scheduler_snapshot = self.latest_scheduler_snapshot()
        missed_runs = self.list_missed_runs()
        return {
            "policies": [
                {
                    "job_name": str(row["job_name"]),
                    "portfolio_id": row["portfolio_id"],
                    "version": int(row["version"]),
                    "enabled": bool(row["enabled"]),
                    "schedule": str(row["schedule"]),
                    "timezone": str(row["timezone"]),
                    "approved_at": str(row["approved_at"]),
                }
                for row in policies
            ],
            "run_counts": {str(row["status"]): int(row["count"]) for row in run_counts},
            "latest_runs": [self._run_data(row) for row in latest_runs],
            "open_alert_count": open_alerts,
            "pending_outbox_count": pending_outbox,
            "outbox_counts": {
                str(row["status"]): int(row["count"]) for row in outbox_counts
            },
            "due_retry_count": due_retries,
            "missed_run_count": len(missed_runs),
            "missed_runs": missed_runs,
            "scheduler_manifest": self.scheduler_manifest(),
            "scheduler_snapshot": scheduler_snapshot,
            "scheduler_status": (
                str(scheduler_snapshot["reconciliation_status"])
                if scheduler_snapshot is not None
                else "NOT_RECONCILED"
            ),
            "automatic_trade": False,
        }

    def retry_due(self, *, limit: int = 20) -> JsonDict:
        """Retry due failed jobs while preserving their original idempotency identity."""
        if limit < 1 or limit > 100:
            raise LedgerError("INVALID_LIMIT", "limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_runs
                WHERE status='FAILED' AND next_retry_at IS NOT NULL
                  AND next_retry_at <= ? AND attempt_count < max_attempts
                ORDER BY next_retry_at, started_at LIMIT ?
                """,
                (_iso(self._now()), limit),
            ).fetchall()
        results: list[JsonDict] = []
        for row in rows:
            input_data = json.loads(str(row["input_json"]))
            results.append(
                self.run_job(
                    job_name=str(row["job_name"]),
                    scheduled_for=str(row["scheduled_for"]),
                    portfolio_id=(
                        str(input_data["portfolio_id"]) if input_data.get("portfolio_id") else None
                    ),
                    actor_ref="operations-retry",
                )
            )
        return {
            "retried_count": len(results),
            "items": results,
            "display_text": "[SILENT]" if not results else "AUTOMATION_RETRIES_COMPLETED",
        }

    def recover_due(self, *, limit: int = 20) -> JsonDict:
        """Catch up missed schedules, then retry previously failed deterministic runs."""
        catch_up = self.catch_up_due(limit=limit)
        retries = self.retry_due(limit=limit)
        displays = [
            str(item["display_text"])
            for item in (catch_up, retries)
            if str(item["display_text"]) != "[SILENT]"
        ]
        return {
            "catch_up": catch_up,
            "retries": retries,
            "display_text": "\n".join(displays) if displays else "[SILENT]",
            "automatic_trade": False,
        }

    @staticmethod
    def _display_for_run(connection: sqlite3.Connection, run_id: str) -> str:
        row = connection.execute(
            """
            SELECT delivery_action, bundle_type, data_quality, reason_code
            FROM report_bundles WHERE job_run_id=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None or str(row["delivery_action"]) == "SILENT":
            return "[SILENT]"
        return (
            f"{row['bundle_type']} 已生成待查看事实包, "
            f"数据质量: {row['data_quality']}, 原因: {row['reason_code']}。"
        )
