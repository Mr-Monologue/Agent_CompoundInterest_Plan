"""Read wrapper contracts; synthetic fixtures never claim real acceptance."""

import base64
import hashlib
import json
import sys

import pytest
from fastapi.testclient import TestClient
from test_r12_archive import archived  # noqa: F401

from investor_core import daily_client
from investor_core.api.medical_shadow import create_app
from investor_core.medical_shadow_read import read_research


@pytest.fixture
def packet(tmp_path, archived):  # noqa: F811
    base, originals, _ = archived
    paths = {}
    for key, value in originals.items():
        p = tmp_path / key
        p.write_bytes(base64.b64decode(value))
        paths[key] = key
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(dict(base_input=base, original_paths=paths)), encoding="utf-8")
    return path


def fingerprints(folder):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()
    }


def test_http_repeat_and_details_are_read_only(packet, monkeypatch):
    before = fingerprints(packet.parent)
    with TestClient(create_app(packet)) as client:
        a = client.get("/v1/medical-shadow-research").json()["data"]
        assert a == client.get("/v1/medical-shadow-research").json()["data"]
        detail = client.get("/v1/medical-shadow-research?view=DETAIL").json()["data"]
        assert a["rows"] == detail["rows"]
        assert "source_review" not in a and "source_review" in detail
        assert a["recomputed"] and not a["new_evidence_acquired"]
        assert a["rows"][0]["total_score"] is None
        assert a["rows"][1]["dimension_scores"] == {}
        assert a["leader"] is None and not a["formal_forward_record"]
        assert "历史方法版本" in a["display_text"]
        assert client.post("/v1/medical-shadow-research").status_code == 405
    assert fingerprints(packet.parent) == before


def test_no_outbound_network_or_database(packet, monkeypatch):
    import socket
    import sqlite3

    def forbidden(*args, **kwargs):
        raise AssertionError("network or database")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert read_research(packet)["recomputed"]


@pytest.mark.parametrize("mode", ["missing", "tampered", "invalid", "unconfigured", "nonobject"])
def test_failure_never_uses_saved_result(packet, mode):
    (packet.parent / "output.json").write_text('{"total_score":99}', encoding="utf-8")
    if mode == "missing":
        (packet.parent / "zo-legal").unlink()
    elif mode == "tampered":
        (packet.parent / "zo-legal").write_bytes(b"changed")
    elif mode == "nonobject":
        packet.write_text("[]", encoding="utf-8")
    elif mode == "invalid":
        packet.write_text("{}", encoding="utf-8")
    result = read_research(None if mode == "unconfigured" else packet)
    assert result["status"] == "BLOCKED" and not result["recomputed"]
    assert result["rows"] == []
    assert "未展示历史保存结果" in result["display_text"]
    assert str(packet.parent) not in result["display_text"]


@pytest.mark.parametrize("command", ["medical-shadow", "查看医疗影子研究"])
def test_daily_command_routes_without_context_or_journal(packet, monkeypatch, capsys, command):
    with TestClient(create_app(packet)) as local:

        class LocalDaily:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, path, **params):
                assert path == "/v1/medical-shadow-research"
                return local.get(path, params=params).json()["data"]

            def close(self):
                pass

        monkeypatch.setattr(daily_client, "DailyClient", LocalDaily)
        monkeypatch.setattr(sys, "argv", ["investor-assistant", command])
        daily_client.main()
        assert "003096" in capsys.readouterr().out
