"""Independent HTTP worker with a durable local transport journal (no ledger access)."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter

from investor_core.scheduler import BACKGROUND_JOBS, CATCHUP_HOURS, RETRY_MINUTES, instant, stamp


class CoreClient:
    def __init__(self, url: str) -> None:
        parsed = httpx.URL(url)
        if parsed.scheme != "http" or parsed.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Background worker requires a loopback Core HTTP URL")
        self.client = httpx.Client(
            base_url=url, timeout=httpx.Timeout(300, connect=5), trust_env=False
        )

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.request(method, path, json=body)
        response.raise_for_status()
        result: dict[str, Any] = response.json()["data"]
        return result


class BackgroundWorker:
    def __init__(
        self,
        path: Path,
        request: Callable[..., dict[str, Any]],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.path, self.request, self.now = path, request, now
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS queue (
                    id TEXT PRIMARY KEY, generation TEXT NOT NULL, policy_json TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL,
                    next_attempt TEXT NOT NULL, actual_at TEXT, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, code TEXT NOT NULL);
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _event(self, code: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(observed_at,code) VALUES (?,?)", (stamp(self.now()), code)
            )

    def _enqueue(self, manifest: dict[str, Any]) -> None:
        if manifest["source"] != "WINDOWS":
            return
        now = self.now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for policy in manifest["policies"]:
                if not policy["enabled"] or policy["job_name"] not in BACKGROUND_JOBS:
                    continue
                key = f"cursor:{manifest['generation']}:{policy['content_hash']}"
                row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
                start = max(instant(manifest["cutover_at"]), instant(policy["approved_at"]))
                cursor = instant(row[0]) if row else start
                # Bounded scan after long shutdowns; preserve the excluded range explicitly.
                if now - cursor > timedelta(days=7):
                    db.execute(
                        "INSERT INTO events(observed_at,code) VALUES (?,?)",
                        (
                            stamp(now),
                            f"MISSED_RANGE:{policy['job_name']}:{stamp(cursor)}:"
                            f"{stamp(now - timedelta(days=7))}",
                        ),
                    )
                    cursor = now - timedelta(days=7)
                cron = croniter(policy["schedule"], cursor.astimezone(ZoneInfo(policy["timezone"])))
                for _ in range(10081):
                    due = cron.get_next(datetime).astimezone(UTC)
                    if due > now:
                        break
                    identity = (
                        f"{manifest['generation']}:{policy['job_name']}:"
                        f"{policy['portfolio_id']}:{stamp(due)}"
                    )
                    state = "EXPIRED" if now - due > timedelta(hours=CATCHUP_HOURS) else "PENDING"
                    db.execute(
                        "INSERT OR IGNORE INTO queue VALUES (?,?,?,?,?,0,?,NULL,?)",
                        (
                            identity,
                            manifest["generation"],
                            json.dumps(policy),
                            stamp(due),
                            state,
                            stamp(now),
                            "MISSED_CATCHUP_WINDOW" if state == "EXPIRED" else "",
                        ),
                    )
                db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, stamp(now)))

    def _transport_failure(self, row: sqlite3.Row, code: str) -> None:
        attempts = int(row["attempts"]) + 1
        policy = json.loads(row["policy_json"])
        self._event(
            f"TRANSPORT_FAILURE:{policy['job_name']}:{row['scheduled_for']}:{attempts}:{code}"
        )
        maximum = int(json.loads(row["policy_json"])["config"]["max_attempts"])
        state = "FAILED_TRANSPORT" if attempts >= maximum else "PENDING"
        next_at = stamp(
            self.now() + timedelta(minutes=RETRY_MINUTES[min(attempts - 1, len(RETRY_MINUTES) - 1)])
        )
        with self.connect() as db:
            db.execute(
                "UPDATE queue SET state=?,attempts=?,next_attempt=?,actual_at=?,detail=? "
                "WHERE id=?",
                (state, attempts, next_at, stamp(self.now()), code, row["id"]),
            )

    def _snapshot(self, generation: str) -> None:
        with self.connect() as db:
            failures = [
                dict(r)
                for r in db.execute(
                    "SELECT json_extract(policy_json, '$.job_name') AS job_name, "
                    "scheduled_for,actual_at,state,detail FROM queue WHERE state IN "
                    "('FAILED_TRANSPORT','FAILED','EXPIRED','UNKNOWN','FENCED') "
                    "ORDER BY scheduled_for DESC LIMIT 20"
                )
            ]
            events = [
                dict(r)
                for r in db.execute("SELECT observed_at,code FROM events ORDER BY id DESC LIMIT 10")
            ]
            pending = db.execute("SELECT COUNT(*) FROM queue WHERE state='PENDING'").fetchone()[0]
        self.request(
            "POST",
            "/v1/background-scheduler/heartbeat",
            {"generation": generation, "failures": failures + events, "pending_count": pending},
        )

    def tick(self) -> dict[str, Any]:
        # This connection is only a process mutex. Queue commits survive process termination.
        lock = sqlite3.connect(str(self.path) + ".lock", timeout=0)
        try:
            try:
                lock.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                return {"state": "BUSY"}
            return self._tick()
        finally:
            lock.close()

    def _tick(self) -> dict[str, Any]:
        online = True
        try:
            manifest = self.request("GET", "/v1/background-scheduler")
            with self.connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('manifest',?)", (json.dumps(manifest),)
                )
        except (httpx.HTTPError, OSError) as exc:
            online = False
            self._event(f"CORE_UNAVAILABLE:{type(exc).__name__}")
            with self.connect() as db:
                cached = db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
            if cached is None:
                return {"state": "CORE_UNAVAILABLE_NO_CACHED_POLICY"}
            manifest = json.loads(cached[0])
        self._enqueue(manifest)
        if manifest["source"] != "WINDOWS":
            return {"state": "INACTIVE", "source": manifest["source"]}
        with self.connect() as db:
            db.execute(
                "UPDATE queue SET state='FENCED',detail='SOURCE_CHANGED' "
                "WHERE generation<>? AND state='PENDING'",
                (manifest["generation"],),
            )
            rows = db.execute(
                "SELECT * FROM queue WHERE state='PENDING' AND next_attempt<=? "
                "ORDER BY scheduled_for LIMIT 20",
                (stamp(self.now()),),
            ).fetchall()
        for row in rows:
            policy = json.loads(row["policy_json"])
            if self.now() - instant(row["scheduled_for"]) > timedelta(hours=CATCHUP_HOURS):
                with self.connect() as db:
                    db.execute(
                        "UPDATE queue SET state='EXPIRED',detail='MISSED_CATCHUP_WINDOW' "
                        "WHERE id=?",
                        (row["id"],),
                    )
                continue
            if not online:
                self._transport_failure(row, "CORE_UNAVAILABLE")
                continue
            current = next((p for p in manifest["policies"] if p["id"] == policy["id"]), None)
            if (
                current is None
                or not current["enabled"]
                or current["content_hash"] != policy["content_hash"]
            ):
                with self.connect() as db:
                    db.execute(
                        "UPDATE queue SET state='FENCED',detail='POLICY_CHANGED' WHERE id=?",
                        (row["id"],),
                    )
                continue
            try:
                result = self.request(
                    "POST",
                    "/v1/background-scheduler/run",
                    {
                        "job_name": policy["job_name"],
                        "portfolio_id": policy["portfolio_id"],
                        "scheduled_for": row["scheduled_for"],
                        "scheduler_generation": manifest["generation"],
                        "scheduler_policy_hash": policy["content_hash"],
                        "actor_ref": "windows-background",
                    },
                )
                run = result["job_run"]
                state, next_at = run["status"], stamp(self.now())
                if state == "FAILED" and run.get("next_retry_at"):
                    state, next_at = "PENDING", run["next_retry_at"]
                # An in-flight or crash-interrupted claim is never executed again automatically.
                if state == "RUNNING":
                    state = "UNKNOWN"
                with self.connect() as db:
                    db.execute(
                        "UPDATE queue SET state=?,next_attempt=?,actual_at=?,detail=? WHERE id=?",
                        (state, next_at, stamp(self.now()), run.get("error_code") or "", row["id"]),
                    )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {400, 409, 422}:
                    with self.connect() as db:
                        db.execute(
                            "UPDATE queue SET state='FENCED',actual_at=?,detail=? WHERE id=?",
                            (
                                stamp(self.now()),
                                f"CORE_REJECTED_{exc.response.status_code}",
                                row["id"],
                            ),
                        )
                else:
                    self._transport_failure(row, f"HTTP_{exc.response.status_code}")
            except (httpx.HTTPError, OSError) as exc:
                self._transport_failure(row, type(exc).__name__)
        if online:
            try:
                self._snapshot(manifest["generation"])
            except (httpx.HTTPError, OSError) as exc:
                self._event(f"HEARTBEAT_FAILED:{type(exc).__name__}")
        return {"state": "OBSERVED" if online else "CORE_UNAVAILABLE", "considered": len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-url", default="http://127.0.0.1:8710")
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    client = CoreClient(args.core_url)
    try:
        print(json.dumps(BackgroundWorker(args.state, client.request).tick()))
    finally:
        client.client.close()


if __name__ == "__main__":
    main()
