"""Conservative code rollback check; never restore or write the live database."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def fingerprint(connection):
    digest = hashlib.sha256()
    for statement in connection.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.digest()


def verify(snapshot: Path, current: Path):
    # A verified baseline plus identical logical contents is deliberately stricter
    # than merely checking migration names. Changes require a reviewed recovery.
    with closing(sqlite3.connect(current.resolve().as_uri() + "?mode=ro", uri=True)) as live:
        live.execute("BEGIN")
        with closing(
            sqlite3.connect((snapshot / "investor.db").resolve().as_uri() + "?mode=ro", uri=True)
        ) as old:
            if live.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("CURRENT_DATABASE_INTEGRITY_FAILED")
            if fingerprint(live) != fingerprint(old):
                raise RuntimeError("DATABASE_CHANGED_REVIEW_REQUIRED_NO_RESTORE")
        with tempfile.TemporaryDirectory(
            prefix="rollback-check-", dir=snapshot.parent
        ) as temporary:
            isolated = Path(temporary) / "current.db"
            with closing(sqlite3.connect(isolated)) as target:
                live.backup(target)
            code = (snapshot / "code").resolve()
            expected = json.loads((code / "release-manifest.json").read_text(encoding="utf-8-sig"))[
                "version"
            ]
            probe = """
import pathlib,sys
sys.path.insert(0,sys.argv[1])
import investor_core.health as health
from investor_core.config import Settings
assert pathlib.Path(health.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]).resolve())
settings=Settings(_env_file=None, db_path=pathlib.Path(sys.argv[2]),
                  project_root=pathlib.Path(sys.argv[3]))
r=health.build_doctor_report(settings)
assert r.status == 'PASS' and r.version == sys.argv[4]
"""
            environment = {k: v for k, v in os.environ.items() if not k.startswith("INVESTOR_")}
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            run = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    probe,
                    str(code / "src"),
                    str(isolated),
                    str(code),
                    expected,
                ],
                cwd=temporary,
                env=environment,
                capture_output=True,
                timeout=60,
            )
            if run.returncode:
                raise RuntimeError("OLD_CODE_CURRENT_DATABASE_COMPATIBILITY_UNVERIFIED")
    return {
        "compatibility": "VERIFIED_IDENTICAL_DATABASE_AND_OLD_DOCTOR",
        "database_restore": False,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(verify(Path(sys.argv[1]), Path(sys.argv[2]))))
    except Exception as error:
        print(
            json.dumps(
                {"compatibility": "BLOCKED", "reason": str(error), "database_restore": False}
            )
        )
        sys.exit(1)
