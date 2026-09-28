"""Read-only acceptance on a private backup through an isolated actual HTTP server.

Usage: python tests/manual_delta_review.py --backup <readable backup> --output <private folder>
Never accepts the installed database as a backup; makes its own disposable copy.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from manual_stage_flow import loopback_client

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings
from investor_core.daily_client import DailyClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.backup.resolve(), args.output.resolve()
    if source.name == "investor.db" or "data" in source.parts:
        raise SystemExit("Provide an explicitly created technical backup, never the installed DB")
    output.mkdir(parents=True, exist_ok=True)
    clone = output / "isolated-delta-readonly.db"
    if clone.exists():
        raise SystemExit("Choose a new acceptance output directory; preserve earlier proof")
    shutil.copy2(source, clone)

    def contents() -> list[str]:
        with closing(sqlite3.connect(clone)) as db:
            return list(db.iterdump())

    before = contents()
    settings = Settings(
        _env_file=None,
        db_path=clone,
        environment=Environment.TEST,
        project_root=Path(__file__).resolve().parents[1],
        core_autostart=False,
        market_akshare_enabled=False,
    )
    with loopback_client(create_app(settings)) as http:
        client = DailyClient(client=http, journal=output / "must-not-exist.sqlite3")
        q = client.scope()
        history = client.get("/v1/holding-review-history", **q)
        result = client.get("/v1/holding-review-delta", **q)
        assert result == client.get("/v1/holding-review-delta", **q)
        for status in ("NEW", "RESOLVED", "CHANGED", "REGRESSED", "UNCHANGED", "STALE"):
            client.get("/v1/holding-review-delta", **q, status=status)
        details = [
            client.get("/v1/holding-review-delta", **q, view="DETAIL", instrument_code=h["code"])
            for h in result["current"]["holdings"]
        ]
        after = client.get("/v1/holding-review-history", **q)
        assert after == history and contents() == before
        assert not client.journal.exists()
        assert not result["approval_mutation"] and not result["holding_mutation"]
        report = dict(
            environment="ISOLATED_REAL_ARCHIVE_HTTP_NOT_INSTALLED_ACCEPTANCE",
            baseline=result["baseline"],
            current_persistence=result["persistence"],
            holdings=len(result["current"]["holdings"]),
            windows=result["current"]["window_count"],
            summary=result["summary"],
            snapshot_count_before=len(history["snapshots"]),
            snapshot_count_after=len(after["snapshots"]),
            tasks_unchanged=True,
            all_database_contents_unchanged=True,
            approval_mutation=False,
            holding_mutation=False,
        )
        for name, value in (("acceptance", report), ("delta", result), ("details", details)):
            (output / (name + ".json")).write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (output / "ACTUAL_REVIEW.md").write_text(result["display_text"], encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
