from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "rollback_preflight", ROOT / "runtime/windows/rollback-preflight.py"
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(tmp_path, doctor_pass=True):
    snapshot = tmp_path / "snapshot"
    code = snapshot / "code"
    package = code / "src/investor_core"
    package.mkdir(parents=True)
    (code / "release-manifest.json").write_text(json.dumps({"version": "test-old"}))
    (package / "__init__.py").write_text("")
    (package / "config.py").write_text(
        "class Settings:\n def __init__(self, **kwargs): self.__dict__.update(kwargs)\n"
    )
    (package / "health.py").write_text(
        "import sqlite3\nfrom types import SimpleNamespace\n"
        "def build_doctor_report(settings):\n"
        " with sqlite3.connect(settings.db_path) as c:\n"
        "  assert c.execute('SELECT amount FROM ledger').fetchall()==[(7,)]\n"
        "  c.execute('CREATE TABLE isolated_probe(id INTEGER)')\n"
        f" return SimpleNamespace(status={'PASS' if doctor_pass else 'FAIL'!r},"
        "version='test-old')\n"
    )
    current = tmp_path / "current.db"
    with sqlite3.connect(current) as db:
        db.execute("CREATE TABLE ledger(amount INTEGER)")
        db.execute("INSERT INTO ledger VALUES (7)")
    shutil.copy2(current, snapshot / "investor.db")
    return snapshot, current


def test_old_code_checks_isolated_current_copy_without_mutating_live(tmp_path):
    snapshot, current = fixture(tmp_path)
    before = current.read_bytes()
    result = module.verify(snapshot, current)
    assert result["compatibility"].startswith("VERIFIED")
    assert result["database_restore"] is False
    assert current.read_bytes() == before
    assert not list(snapshot.glob("rollback-check-*"))


@pytest.mark.parametrize(
    "change",
    [
        "INSERT INTO ledger VALUES (8)",
        "UPDATE ledger SET amount=8",
        "CREATE TABLE additive(id INTEGER)",
    ],
)
def test_new_fact_or_additive_migration_never_implies_compatible(tmp_path, change):
    snapshot, current = fixture(tmp_path)
    with sqlite3.connect(current) as db:
        db.execute(change)
    before = current.read_bytes()
    with pytest.raises(RuntimeError, match="DATABASE_CHANGED_REVIEW_REQUIRED"):
        module.verify(snapshot, current)
    assert current.read_bytes() == before


def test_matching_database_does_not_skip_old_code_compatibility_probe(tmp_path):
    snapshot, current = fixture(tmp_path, doctor_pass=False)
    with pytest.raises(RuntimeError, match="COMPATIBILITY_UNVERIFIED"):
        module.verify(snapshot, current)


def test_current_wal_committed_fact_is_detected(tmp_path):
    snapshot, current = fixture(tmp_path)
    with sqlite3.connect(current) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO ledger VALUES (9)")
        db.commit()
        with pytest.raises(RuntimeError, match="DATABASE_CHANGED_REVIEW_REQUIRED"):
            module.verify(snapshot, current)
        assert db.execute("SELECT amount FROM ledger").fetchall() == [(7,), (9,)]
