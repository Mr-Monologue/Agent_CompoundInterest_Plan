"""Core-owned scheduling authority. A launcher is never authority to execute."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from croniter import croniter

from investor_core.ledger import LedgerError

if TYPE_CHECKING:
    from investor_core.operations import OperationsService

BACKGROUND_JOBS = frozenset({"DAILY_MARKET_SYNC", "DAILY_RISK_SCAN", "SYSTEM_DOCTOR"})
RETRY_MINUTES = (5, 15, 30, 60, 120)
CATCHUP_HOURS = 24
HEARTBEAT_SECONDS = 180


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise LedgerError("SCHEDULER_TIMESTAMP_INVALID", "Timezone-aware timestamp required")
    return parsed.astimezone(UTC)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def control(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        "SELECT payload_json FROM scheduler_control WHERE id='daily'"
    ).fetchone()
    return (
        json.loads(row[0])
        if row
        else {
            "source": "HERMES",
            "generation": None,
            "cutover_at": None,
            "protected_misses": [],
            "changed_at": None,
        }
    )


def check_authority(
    connection: sqlite3.Connection,
    *,
    job_name: str,
    scheduled_for: str,
    generation: str | None,
    now: datetime,
) -> None:
    if job_name not in BACKGROUND_JOBS:
        if generation is not None:
            raise LedgerError(
                "SCHEDULER_SCOPE_DENIED", "Job outside Windows scheduler scope", http_status=409
            )
        return
    state = control(connection)
    valid = (state["source"] == "HERMES" and generation is None) or (
        state["source"] == "WINDOWS"
        and generation is not None
        and hmac.compare_digest(generation, state["generation"])
    )
    if not valid:
        raise LedgerError(
            "SCHEDULER_SOURCE_FENCED", "This scheduler is not the active owner", http_status=409
        )
    if state["cutover_at"]:
        try:
            scheduled = instant(scheduled_for)
        except ValueError as exc:
            raise LedgerError(
                "SCHEDULER_TIMESTAMP_INVALID", "Use an exact occurrence", http_status=409
            ) from exc
        if scheduled < instant(state["cutover_at"]) or scheduled > now:
            raise LedgerError(
                "SCHEDULER_OCCURRENCE_PROTECTED", "Outside the cutover boundary", http_status=409
            )
        if generation and now - scheduled > timedelta(hours=CATCHUP_HOURS):
            raise LedgerError(
                "SCHEDULER_CATCHUP_EXPIRED", "Catch-up window exceeded", http_status=409
            )


class SchedulerService:
    def __init__(self, operations: OperationsService) -> None:
        self.operations = operations

    def _policies(self) -> list[dict[str, Any]]:
        return [p for p in self.operations.list_policies() if p["job_name"] in BACKGROUND_JOBS]

    def status(self) -> dict[str, Any]:
        now = self.operations._now()
        with self.operations._connect() as connection:
            state = control(connection)
            row = connection.execute(
                "SELECT payload_json FROM scheduler_worker_snapshots WHERE id=?",
                (state["generation"] or "none",),
            ).fetchone()
            running = connection.execute(
                "SELECT job_name, scheduled_for, started_at FROM job_runs WHERE status='RUNNING'"
            ).fetchall()
        snapshot = json.loads(row[0]) if row else None
        anomalies = []
        if state["source"] == "HERMES":
            anomalies.append("HERMES_HEARTBEAT_NOT_VERIFIED")
        if state["source"] == "WINDOWS" and (
            not snapshot
            or (now - instant(snapshot["received_at"])).total_seconds() > HEARTBEAT_SECONDS
        ):
            anomalies.append("WINDOWS_HEARTBEAT_MISSING_OR_STALE")
        if snapshot and snapshot["failures"]:
            anomalies.append("WORKER_FAILURES_RECORDED")
        if any(r["job_name"] in BACKGROUND_JOBS for r in running):
            anomalies.append("RUNNING_JOBS_CHECK_BEFORE_SWITCH_OR_RECOVERY")
        policies = self._policies()
        for policy in policies:
            policy["next_run_at"] = (
                stamp(
                    croniter(
                        policy["schedule"],
                        now.astimezone(ZoneInfo(policy["timezone"])),
                    ).get_next(datetime)
                )
                if policy["enabled"] and state["source"] != "PAUSED"
                else None
            )
        return {
            **state,
            "policies": policies,
            "scope": sorted(BACKGROUND_JOBS),
            "heartbeat": snapshot,
            "anomalies": anomalies,
            "catchup_hours": CATCHUP_HOURS,
            "notification_delivery": "HERMES_OUTBOX_NOT_MIGRATED",
            "other_jobs_source": "HERMES",
        }

    def preview(self, *, target: str) -> dict[str, Any]:
        if target not in {"WINDOWS", "HERMES", "PAUSED"}:
            raise LedgerError("SCHEDULER_TARGET_INVALID", "Unknown scheduler source")
        status = self.status()
        return {
            "current_source": status["source"],
            "target_source": target,
            "scope": status["scope"],
            "policies": status["policies"],
            "catchup_hours": CATCHUP_HOURS,
            "earliest_execution": "AFTER_COMMIT",
            "historical_misses": self.operations.list_missed_runs(),
            "protected_misses": status["protected_misses"],
            "anomalies": status["anomalies"],
            "notification_delivery": status["notification_delivery"],
            "holdings_changed": False,
            "transactions_created": False,
            "state_hash": digest(
                {
                    k: status[k]
                    for k in (
                        "source",
                        "generation",
                        "cutover_at",
                        "policies",
                    )
                }
                | {
                    "policies": [
                        {k: v for k, v in policy.items() if k != "next_run_at"}
                        for policy in status["policies"]
                    ]
                }
            ),
        }

    def create_draft(self, *, target: str, actor_ref: str) -> dict[str, Any]:
        preview = self.preview(target=target)
        token = secrets.token_urlsafe(32)
        draft = {
            "id": str(uuid4()),
            "preview": preview,
            "status": "PENDING",
            "created_by": actor_ref,
            "expires_at": stamp(self.operations._now() + timedelta(minutes=15)),
            "confirmation_digest": digest(token),
        }
        with self.operations._connect() as connection:
            connection.execute(
                "INSERT INTO scheduler_change_drafts VALUES (?, ?)",
                (draft["id"], json.dumps(draft)),
            )
        return {
            "draft": {k: v for k, v in draft.items() if k != "confirmation_digest"},
            "confirmation_token": token,
        }

    def commit(
        self, *, draft_id: str, confirmation_token: str, confirmed_by: str
    ) -> dict[str, Any]:
        with self.operations._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM scheduler_change_drafts WHERE id=?", (draft_id,)
            ).fetchone()
            if not row:
                raise LedgerError(
                    "SCHEDULER_DRAFT_NOT_FOUND", "Scheduler draft not found", http_status=404
                )
            draft = json.loads(row[0])
            if not hmac.compare_digest(draft["confirmation_digest"], digest(confirmation_token)):
                raise LedgerError(
                    "SCHEDULER_CONFIRMATION_INVALID", "Invalid confirmation token", http_status=409
                )
            if draft["status"] == "COMMITTED":
                return {"state": draft["committed_state"], "idempotent_replay": True}
            if instant(draft["expires_at"]) <= self.operations._now():
                raise LedgerError(
                    "SCHEDULER_DRAFT_EXPIRED", "Preview again before confirming", http_status=409
                )
            target = draft["preview"]["target_source"]
            fresh = self.preview(target=target)
            if fresh["state_hash"] != draft["preview"]["state_hash"]:
                raise LedgerError(
                    "SCHEDULER_STATE_CHANGED",
                    "State or policy changed; preview again",
                    http_status=409,
                )
            running = connection.execute("SELECT job_name FROM job_runs WHERE status='RUNNING'")
            if any(row[0] in BACKGROUND_JOBS for row in running):
                raise LedgerError(
                    "SCHEDULER_RUN_IN_PROGRESS",
                    "Resolve running jobs before switching",
                    http_status=409,
                )
            previous = control(connection)
            now = stamp(self.operations._now())
            # Every switch starts a new execution epoch, including rollback. No backlog crosses it.
            protected = {
                f"{m['policy_id']}:{m['scheduled_for']}": m for m in previous["protected_misses"]
            }
            for missed in fresh["historical_misses"]:
                if missed["job_name"] in BACKGROUND_JOBS:
                    identity = f"{missed['policy_id']}:{missed['scheduled_for']}"
                    protected[identity] = {**missed, "recovery_state": "MANUAL_REVIEW_ONLY"}
            state = {
                "source": target,
                "generation": str(uuid4()),
                "cutover_at": now,
                "changed_at": now,
                "changed_by": confirmed_by,
                "protected_misses": list(protected.values()),
            }
            connection.execute(
                "INSERT OR REPLACE INTO scheduler_control VALUES ('daily', ?)", (json.dumps(state),)
            )
            draft.update(status="COMMITTED", committed_state=state)
            connection.execute(
                "UPDATE scheduler_change_drafts SET payload_json=? WHERE id=?",
                (json.dumps(draft), draft_id),
            )
        return {"state": state, "idempotent_replay": False}

    def heartbeat(
        self, *, generation: str, failures: list[dict[str, Any]], pending_count: int
    ) -> dict[str, Any]:
        with self.operations._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = control(connection)
            if state["source"] != "WINDOWS" or state["generation"] != generation:
                raise LedgerError(
                    "SCHEDULER_SOURCE_FENCED", "Stale worker heartbeat", http_status=409
                )
            snapshot = {
                "received_at": stamp(self.operations._now()),
                "failures": failures[-20:],
                "pending_count": pending_count,
            }
            connection.execute(
                "INSERT OR REPLACE INTO scheduler_worker_snapshots VALUES (?, ?)",
                (generation, json.dumps(snapshot)),
            )
        return snapshot
