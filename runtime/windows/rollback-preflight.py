"""Conservative code rollback check; never restore or write the live database."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from contextlib import closing
from pathlib import Path


def fingerprint(connection):
    digest = hashlib.sha256()
    for statement in connection.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\n")
    return digest.digest()


def business_contract(root: Path):
    """Only version labels may differ; unknown business/dependency changes block."""
    files = {}
    source = root / "src"
    if not source.is_dir():
        raise RuntimeError("BUSINESS_SOURCE_UNAVAILABLE")
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        value = path.read_bytes()
        if path.relative_to(source).as_posix() == "investor_core/version.py":
            tree = ast.parse(value)
            for node in tree.body:
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "__version__"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    node.value.value = "VERSION_LABEL"
            value = ast.dump(tree).encode()
        files[path.relative_to(source).as_posix()] = hashlib.sha256(value).hexdigest()
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8-sig"))
    project["project"].pop("version", None)
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8-sig"))
    for package in lock.get("package", []):
        if package["name"] == project["project"]["name"]:
            package["version"] = "VERSION_LABEL"
    return files, project, lock


def schema(connection):
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


def verify(snapshot: Path, current: Path, current_code: Path):
    code = (snapshot / "code").resolve()
    old_contract = business_contract(code)
    new_contract = business_contract(current_code)
    if old_contract != new_contract:
        old_files, new_files = old_contract[0], new_contract[0]
        changed = sorted(
            k for k in old_files.keys() | new_files.keys() if old_files.get(k) != new_files.get(k)
        )
        changed += [
            name
            for index, name in ((1, "pyproject.toml"), (2, "uv.lock"))
            if old_contract[index] != new_contract[index]
        ]
        raise RuntimeError(
            "BUSINESS_OR_DEPENDENCY_CONTRACT_CHANGED_REVIEW_REQUIRED: " + ", ".join(changed[:20])
        )
    with closing(sqlite3.connect(current.resolve().as_uri() + "?mode=ro", uri=True)) as live:
        live.execute("BEGIN")
        with closing(
            sqlite3.connect((snapshot / "investor.db").resolve().as_uri() + "?mode=ro", uri=True)
        ) as old:
            if live.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("CURRENT_DATABASE_INTEGRITY_FAILED")
            if schema(live) != schema(old):
                raise RuntimeError("DATABASE_STRUCTURE_CHANGED_REVIEW_REQUIRED")
            revision_query = "SELECT version_num FROM alembic_version ORDER BY version_num"
            if live.execute(revision_query).fetchall() != old.execute(revision_query).fetchall():
                raise RuntimeError("DATABASE_REVISION_CHANGED_REVIEW_REQUIRED")
            if live.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("CURRENT_DATABASE_FOREIGN_KEY_CHECK_FAILED")
            data_changed = fingerprint(live) != fingerprint(old)
        with tempfile.TemporaryDirectory(prefix="rollback-check-", dir=snapshot) as temporary:
            isolated = Path(temporary) / "current.db"
            with closing(sqlite3.connect(isolated)) as target:
                live.backup(target)
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
        "compatibility": "VERIFIED_SAME_BUSINESS_CONTRACT_SCHEMA_AND_OLD_DOCTOR",
        "current_data_differs_from_snapshot": data_changed,
        "snapshot_restore_permission": "REQUIRES_SEPARATE_REVIEW",
        "database_restore": False,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(verify(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))))
    except Exception as error:
        print(
            json.dumps(
                {"compatibility": "BLOCKED", "reason": str(error), "database_restore": False}
            )
        )
        sys.exit(1)
