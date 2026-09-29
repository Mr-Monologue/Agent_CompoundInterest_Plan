from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_peer_research import payload

from investor_core.peer_models import PeerStudy
from investor_core.peer_research import evaluate
from investor_core.peer_validation import present_validation


def validation_input():
    raw = payload()
    raw["windows"] += [
        dict(label="YTD", start="2024-01-02", end="2024-01-04"),
        dict(label="recent", start="2024-01-03", end="2024-01-04"),
    ]
    checks, structures = [], []
    for p in raw["products"]:
        code = p["code"]
        raw["sources"][code + "-channel"] = dict(raw["sources"][code], quality="REPOST")
        nav = deepcopy(p["nav"])
        nav["evidence_ids"] = [code + "-channel"]
        checks.append(
            dict(
                code=code,
                nav=nav,
                distribution_from="2020-01-01",
                distribution_to="2024-01-04",
                distribution_sources=[code + "-channel"],
                upstream="UNKNOWN",
                lineage_note="Upstream unknown",
                lineage_sources=[code + "-channel"],
                official_decimals=4,
                channel_decimals=4,
                precision_sources=[code],
            )
        )
        structures.append(
            dict(
                code=code,
                as_of="2024-01-04",
                evidence_ids=[code],
                stock_nav_pct=80,
                top10=[dict(security_code=str(i), name=str(i), weight_pct=5) for i in range(10)],
                sector_basis="Official broad sectors",
                sectors_pct={"manufacturing": 80},
                limitations=["Snapshot not attribution"],
            )
        )
    raw["validation"] = dict(
        original_labels=["first", "second"],
        additional_labels=["YTD", "recent"],
        window_rule="Predetermined exact endpoints",
        checks=checks,
        structures=structures,
    )
    return raw


def test_exact_channel_crosscheck_does_not_claim_independence_or_change_returns():
    raw = validation_input()
    result = evaluate(PeerStudy.model_validate(raw))
    assert not result["validation"]["independent_validation"]
    for check in result["validation"]["checks"]:
        assert all(w["status"] == "EXACT_CHANNEL_AGREEMENT" for w in check["windows"])
        assert all(w["return_difference_pp"] == 0 for w in check["windows"])
    assert (
        result["rows"][0]["windows"][:2]
        == evaluate(PeerStudy.model_validate(payload()))["rows"][0]["windows"]
    )
    assert result["validation"]["shared_top10_count"] == 10
    assert result["validation"]["top10_min_weight_sum_pct"] == "50"


@pytest.mark.parametrize(
    "change,status",
    [
        ("missing", "INCOMPLETE"),
        ("conflict", "CONFLICT"),
        ("rounding", "ROUNDING_DIFFERENCE_REVIEW"),
        ("dividend", "CONFLICT"),
        ("coverage", "INCOMPLETE"),
    ],
)
def test_conflicts_and_missing_block_new_windows_and_preserve_both(change, status):
    raw = validation_input()
    ch = raw["validation"]["checks"][0]
    if change == "missing":
        ch["nav"]["points"].pop()
    if change == "conflict":
        ch["nav"]["points"][-1]["value"] = "1.3"
    if change == "rounding":
        ch["nav"]["points"][-1]["value"] = "1.2001"
    if change == "dividend":
        ch["distributions"] = [dict(ex_date="2024-01-04", cash_per_unit="0.1")]
    if change == "coverage":
        ch["distribution_to"] = "2024-01-03"
    result = evaluate(PeerStudy.model_validate(raw))
    w = result["validation"]["checks"][0]["windows"][-1]
    assert w["status"] == status
    assert result["rows"][1]["windows"][-1]["calculated"] is None
    assert "NEW_WINDOW_CROSS_CHECK_BLOCKED" in result["rows"][1]["windows"][-1]["gaps"]
    assert result["rows"][0]["windows"][0]["calculated"] is not None
    assert raw["products"][0]["nav"]["points"][-1]["value"] == 1.2


def test_out_of_window_dividend_conflict_preserved_without_invalidating_later_period():
    raw = validation_input()
    ch = raw["validation"]["checks"][0]
    ch["distributions"] = [dict(ex_date="2021-01-18", cash_per_unit="0.03531")]
    ch["historical_conflicts"] = ["Earlier dividend omitted by issuer endpoint"]
    check = evaluate(PeerStudy.model_validate(raw))["validation"]["checks"][0]
    assert check["historical_conflicts"]
    assert all(w["status"] == "EXACT_CHANNEL_AGREEMENT" for w in check["windows"])


@pytest.mark.parametrize(
    "change",
    [
        "share",
        "future",
        "nonofficial_scope",
        "structure_date",
        "duplicate_stock",
        "adjusted",
        "independent",
    ],
)
def test_identity_sources_and_temporal_integrity(change):
    raw = validation_input()
    v = raw["validation"]
    if change == "share":
        v["checks"][0]["nav"]["code"] = "WRONG"
    if change == "future":
        v["checks"][0]["nav"]["points"][-1]["day"] = "2025-01-01"
    if change == "nonofficial_scope":
        raw["sources"]["R"]["quality"] = "REPOST"
    if change == "structure_date":
        v["structures"][0]["as_of"] = "2023-12-31"
    if change == "duplicate_stock":
        v["structures"][0]["top10"][0]["security_code"] = "1"
    if change == "adjusted":
        v["checks"][0]["nav"]["return_basis"] = "FUND_TOTAL_RETURN"
    if change == "independent":
        v["checks"][0]["upstream"] = "INDEPENDENT"
    with pytest.raises(ValidationError):
        PeerStudy.model_validate(raw)


def test_fee_description_changes_do_not_subtract_again():
    raw = validation_input()
    before = evaluate(PeerStudy.model_validate(raw))
    raw["products"][0]["dimensions"]["fees"][0]["text"] = (
        "Newly disclosed ongoing fee, not historic"
    )
    after = evaluate(PeerStudy.model_validate(raw))
    assert before["rows"][0]["windows"] == after["rows"][0]["windows"]
    assert before["validation"]["checks"] == after["validation"]["checks"]
    after.update(query_date="2024-01-05")
    assert "R / P" in present_validation(after)
    assert "Alpha" in present_validation(after)


def test_versions_replay_http_readonly_and_original_windows_preserved(tmp_path):
    import json
    import sqlite3

    from conftest import migrate_database
    from fastapi.testclient import TestClient

    from investor_core.api.app import create_app
    from investor_core.config import Environment, Settings
    from investor_core.ledger import LedgerError
    from investor_core.peer_research import PeerResearchService
    from investor_core.research import ResearchService
    from investor_core.scheduler import digest

    db = tmp_path / "validation.db"
    migrate_database(db)
    settings = Settings(_env_file=None, db_path=db, environment=Environment.TEST)
    service = PeerResearchService(ResearchService(settings))
    first = service.archive(PeerStudy.model_validate(payload()))
    with sqlite3.connect(db) as c:
        record = c.execute("SELECT input_json,content_hash FROM peer_research_runs").fetchone()
        original = list(c.iterdump())
    old = json.loads(record[0])
    assert "validation" not in old
    content = {
        k: v for k, v in old.items() if k not in {"idempotency_key", "expected_previous_version"}
    }
    content["sources"] = {
        k: {f: v for f, v in s.items() if f != "retrieved_at"}
        for k, s in content["sources"].items()
    }
    assert digest(content) == record[1]  # Exact legacy hashing contract.
    raw = validation_input()
    raw.update(expected_previous_version=1, idempotency_key="two")
    second = service.archive(PeerStudy.model_validate(raw))
    assert second["version"] == 2
    assert service.archive(PeerStudy.model_validate(raw))["id"] == second["id"]
    assert service.archive(PeerStudy.model_validate(payload()))["id"] == first["id"]
    assert service.read("R", run_id=first["id"], details=True)["archived_input"] == old
    with sqlite3.connect(db) as c:
        before = list(c.iterdump())
    client = TestClient(create_app(settings))
    for _ in range(2):
        r = client.get(
            "/v1/peer-comparison-validation", params=dict(anchor_code="R", view="DETAIL")
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert not any(
            data[k]
            for k in ["writes_performed", "money_action", "approval_mutation", "holding_mutation"]
        )
        assert data["version"] == 2 and len(data["history"]) == 2
        assert data["archived_input"]["validation"]["checks"][0]["upstream"] == "UNKNOWN"
    with sqlite3.connect(db) as c:
        assert list(c.iterdump()) == before
    def strip(rows):
        return [r for r in rows if 'INSERT INTO "peer_research_runs"' not in r]
    assert strip(original) == strip(before)
    raw["expected_previous_version"] = 2
    raw["idempotency_key"] = "three"
    raw["windows"][0]["start"] = "2023-01-03"
    with pytest.raises(LedgerError, match="原窗口"):
        service.archive(PeerStudy.model_validate(raw))
