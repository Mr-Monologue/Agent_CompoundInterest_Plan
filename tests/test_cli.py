from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from investor_core.cli.app import app
from investor_core.config import get_settings


def test_migrate_then_doctor(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    env = {
        "INVESTOR_ENVIRONMENT": "test",
        "INVESTOR_DB_PATH": str(database_path),
    }
    runner = CliRunner()

    get_settings.cache_clear()
    migration = runner.invoke(app, ["db", "migrate"], env=env)
    assert migration.exit_code == 0, migration.output

    get_settings.cache_clear()
    doctor = runner.invoke(app, ["doctor", "--json"], env=env)
    assert doctor.exit_code == 0, doctor.output
    report = json.loads(doctor.output)
    assert report["status"] in {"PASS", "DEGRADED"}
    assert {check["name"] for check in report["checks"]} >= {
        "python",
        "business-timezone",
        "sqlite-integrity",
        "sqlite-wal",
        "database-schema",
    }

    backup_path = tmp_path / "backups" / "investor.db"
    get_settings.cache_clear()
    backup = runner.invoke(app, ["db", "backup", "--output", str(backup_path)], env=env)
    assert backup.exit_code == 0, backup.output
    backup_report = json.loads(backup.output)
    assert backup_report["quick_check"] == "ok"
    assert backup_report["verification_status"] == "PASS"
    assert backup_report["file_size_bytes"] == backup_path.stat().st_size
    assert backup_report["sha256"] == hashlib.sha256(backup_path.read_bytes()).hexdigest()
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    with sqlite3.connect(database_path) as connection:
        evidence = connection.execute(
            """
            SELECT id, database_schema_version, file_name, file_size_bytes,
                   sha256, encrypted, verification_status, verified_at, notes
            FROM backups
            """
        ).fetchone()
    assert evidence is not None
    assert evidence[:7] == (
        backup_report["evidence_id"],
        backup_report["database_schema_version"],
        backup_path.name,
        backup_path.stat().st_size,
        backup_report["sha256"],
        0,
        "PASS",
    )
    assert evidence[7] is not None
    assert evidence[8] == "CLI_SQLITE_BACKUP_QUICK_CHECK_OK"

    get_settings.cache_clear()
