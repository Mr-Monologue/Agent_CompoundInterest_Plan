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
    (code / "pyproject.toml").write_text('[project]\nname="test"\nversion="1.0"\n')
    (code / "uv.lock").write_text('version=1\n[[package]]\nname="test"\nversion="1.0"\n')
    (package / "config.py").write_text(
        "class Settings:\n def __init__(self, **kwargs): self.__dict__.update(kwargs)\n"
    )
    (package / "health.py").write_text(
        "import sqlite3\nfrom types import SimpleNamespace\n"
        "def build_doctor_report(settings):\n"
        " with sqlite3.connect(settings.db_path) as c:\n"
        "  assert c.execute('SELECT amount FROM ledger').fetchall()\n"
        "  c.execute('CREATE TABLE isolated_probe(id INTEGER)')\n"
        f" return SimpleNamespace(status={'PASS' if doctor_pass else 'FAIL'!r},"
        "version='test-old')\n"
    )
    current = tmp_path / "current.db"
    with sqlite3.connect(current) as db:
        db.execute("CREATE TABLE alembic_version(version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('test')")
        db.execute("CREATE TABLE ledger(amount INTEGER)")
        db.execute("INSERT INTO ledger VALUES (7)")
    shutil.copy2(current, snapshot / "investor.db")
    current_code = tmp_path / "current-code"
    shutil.copytree(code, current_code)
    return snapshot, current, current_code


def test_old_code_checks_isolated_current_copy_without_mutating_live(tmp_path):
    snapshot, current, current_code = fixture(tmp_path)
    before = current.read_bytes()
    result = module.verify(snapshot, current, current_code)
    assert result["compatibility"].startswith("VERIFIED")
    assert result["database_restore"] is False
    assert current.read_bytes() == before
    assert not list(snapshot.glob("rollback-check-*"))


@pytest.mark.parametrize("change", ["INSERT INTO ledger VALUES (8)", "UPDATE ledger SET amount=8"])
def test_new_facts_do_not_grant_snapshot_restore_or_block_identical_code(tmp_path, change):
    snapshot, current, current_code = fixture(tmp_path)
    with sqlite3.connect(current) as db:
        db.execute(change)
    before = current.read_bytes()
    result = module.verify(snapshot, current, current_code)
    assert result["current_data_differs_from_snapshot"] is True
    assert result["snapshot_restore_permission"] == "REQUIRES_SEPARATE_REVIEW"
    assert result["database_restore"] is False
    assert current.read_bytes() == before


def test_additive_schema_change_is_not_implicitly_compatible(tmp_path):
    snapshot, current, current_code = fixture(tmp_path)
    with sqlite3.connect(current) as db:
        db.execute("CREATE TABLE additive(id INTEGER)")
    with pytest.raises(RuntimeError, match="DATABASE_STRUCTURE_CHANGED"):
        module.verify(snapshot, current, current_code)


@pytest.mark.parametrize("path", ["src/investor_core/health.py", "pyproject.toml", "uv.lock"])
def test_unknown_business_or_dependency_change_blocks(tmp_path, path):
    snapshot, current, current_code = fixture(tmp_path)
    file = current_code / path
    with file.open("a") as target:
        target.write("\nextra=1\n")
    with pytest.raises(RuntimeError, match="CONTRACT_CHANGED"):
        module.verify(snapshot, current, current_code)


def test_matching_database_does_not_skip_old_code_compatibility_probe(tmp_path):
    snapshot, current, current_code = fixture(tmp_path, doctor_pass=False)
    with pytest.raises(RuntimeError, match="COMPATIBILITY_UNVERIFIED"):
        module.verify(snapshot, current, current_code)


def test_current_wal_committed_fact_is_detected(tmp_path):
    snapshot, current, current_code = fixture(tmp_path)
    with sqlite3.connect(current) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO ledger VALUES (9)")
        db.commit()
        result = module.verify(snapshot, current, current_code)
        assert result["current_data_differs_from_snapshot"] is True
        assert result["database_restore"] is False
        assert db.execute("SELECT amount FROM ledger").fetchall() == [(7,), (9,)]


def test_version_label_change_is_allowed_but_version_code_change_is_not(tmp_path):
    snapshot, current, current_code = fixture(tmp_path)
    name = "src/investor_core/version.py"
    (snapshot / "code" / name).write_text('__version__="1.0"\n')
    (current_code / name).write_text('__version__="1.1"\n')
    assert module.verify(snapshot, current, current_code)["compatibility"].startswith("VERIFIED")
    (current_code / name).write_text('__version__="1.1"\nside_effect=1\n')
    with pytest.raises(RuntimeError, match="CONTRACT_CHANGED"):
        module.verify(snapshot, current, current_code)


def test_data_only_migration_revision_is_not_implicitly_compatible(tmp_path):
    snapshot, current, current_code = fixture(tmp_path)
    with sqlite3.connect(current) as db:
        db.execute("UPDATE alembic_version SET version_num='new'")
    with pytest.raises(RuntimeError, match="DATABASE_REVISION_CHANGED"):
        module.verify(snapshot, current, current_code)


def test_backup_audit_append_preserves_current_database(tmp_path):
    snapshot, current, current_code = fixture(tmp_path)
    for database in (current, snapshot / "investor.db"):
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE backups(id TEXT PRIMARY KEY, verified TEXT)")
    with sqlite3.connect(current) as db:
        db.execute("INSERT INTO backups VALUES ('new-backup','PASS')")
    result = module.verify(snapshot, current, current_code)
    assert result["current_data_differs_from_snapshot"]
    assert result["database_restore"] is False
    with sqlite3.connect(current) as db:
        assert db.execute("SELECT id FROM backups").fetchall() == [("new-backup",)]
