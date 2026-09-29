from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from conftest import migrate_database
from fastapi.testclient import TestClient
from pydantic import ValidationError

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings
from investor_core.peer_models import PeerStudy
from investor_core.peer_research import PeerResearchService, admission, evaluate
from investor_core.research import ResearchService


def payload():
    days = ["2023-01-02", "2023-01-03", "2023-01-04", "2024-01-02", "2024-01-03", "2024-01-04"]
    sources = {}
    products = []
    for code in ("R", "P"):
        sources[code] = dict(
            instrument_code=code,
            source_name="Official fixture",
            source_ref="https://example.test/" + code,
            source_lineage="ISSUER",
            retrieved_at="2024-01-05T00:00:00Z",
            published_date="2024-01-05",
            data_date="2024-01-04",
            excerpt="isolated fixture only",
            original_sha256="a" * 64,
            quality="OFFICIAL",
        )
        claim = dict(
            text="Unknown",
            kind="UNKNOWN",
            evidence_ids=[],
            as_of=None,
            limitation="No fee or manager fact invented",
        )
        products.append(
            dict(
                code=code,
                name=code,
                product_key="product-" + code,
                share_class="C",
                product_type="MIXED",
                active=True,
                market="CN_A",
                stock_min_pct=60,
                stock_max_pct=95,
                medical_non_cash_min_pct=80,
                share_inception="2020-01-01",
                admission_sources=[code],
                admission_note="Official research scope, not eligibility",
                dimensions={k: [claim] for k in ("fees", "management", "concentration", "method")},
                nav=dict(
                    provider="ISSUER",
                    code=code,
                    currency="CNY",
                    return_basis="NAV_NET_INTERNAL_FEES",
                    evidence_ids=[code],
                    points=[
                        dict(day=d, value=v)
                        for d, v in zip(days, [1, 0.9, 1.1, 1, 1.1, 1.2], strict=True)
                    ],
                ),
                distribution_from="2020-01-01",
                distribution_to="2024-01-04",
                distribution_sources=[code],
            )
        )
    return dict(
        anchor_code="R",
        cohort_key="health-active",
        scope_version="v1",
        scope_defined_at="2024-01-05",
        scope_rationale="Predefined complete windows",
        stock_floor_pct=60,
        medical_floor_pct=80,
        knowledge_date="2024-01-05",
        expected_previous_version=0,
        idempotency_key="one",
        sources=sources,
        products=products,
        calendar_dates=days,
        calendar_sources=["R"],
        windows=[
            dict(label="first", start=days[0], end=days[2]),
            dict(label="second", start=days[3], end=days[5]),
        ],
        limitations=["Fixture"],
    )


def test_dividend_reinvestment_drawdown_and_no_fee_double_deduction():
    raw = payload()
    raw["products"][1]["distributions"] = [dict(ex_date="2023-01-03", cash_per_unit="0.1")]
    result = evaluate(PeerStudy.model_validate(raw))
    ref, peer = [r["windows"][0]["calculated"] for r in result["rows"]]
    assert ref["return_pct"] == pytest.approx(10)
    assert ref["max_drawdown_pct"] == pytest.approx(-10)
    assert peer["return_pct"] == pytest.approx((1.1 / 0.9 - 1) * 100)
    assert peer["max_drawdown_pct"] == 0
    before = deepcopy(result)
    raw["products"][0]["dimensions"]["fees"][0]["text"] = "Unknown fees must not be deducted"
    assert (
        evaluate(PeerStudy.model_validate(raw))["rows"][0]["windows"]
        == before["rows"][0]["windows"]
    )


@pytest.mark.parametrize(
    "change,expected",
    [
        ("missing", "DAILY_NAV_MISSING_NO_FILL"),
        ("dividend_unknown", "DIVIDEND_COVERAGE_INCOMPLETE"),
        ("inception", "EXACT_SHARE_HISTORY_INCOMPLETE"),
        ("endpoint", "EXACT_COMMON_ENDPOINT_MISSING"),
        ("conflict", "SOURCE_CONFLICT"),
    ],
)
def test_missing_dates_and_coverage_never_become_zero(change, expected):
    raw = payload()
    p = raw["products"][1]
    if change == "missing":
        p["nav"]["points"].pop(1)
    elif change == "dividend_unknown":
        p["distribution_sources"] = []
    elif change == "inception":
        p["share_inception"] = "2023-01-03"
    elif change == "endpoint":
        raw["windows"][0]["start"] = "2023-01-01"
    else:
        p["nav"]["validation"] = "CONFLICT"
    w = evaluate(PeerStudy.model_validate(raw))["rows"][1]["windows"][0]
    assert w["calculated"] is None and expected in w["gaps"]
    assert w["peer_difference_pp"] is None


@pytest.mark.parametrize(
    "change", ["wrong_share", "duplicate_product", "double_dividend", "future"]
)
def test_identity_and_future_publications_block(change):
    raw = payload()
    if change == "wrong_share":
        raw["products"][1]["nav"]["code"] = "R"
    elif change == "duplicate_product":
        raw["products"][1]["product_key"] = "product-R"
    elif change == "double_dividend":
        raw["products"][1]["nav"]["return_basis"] = "FUND_TOTAL_RETURN"
        raw["products"][1]["distributions"] = [dict(ex_date="2023-01-03", cash_per_unit="0.1")]
    else:
        raw["sources"]["P"]["published_date"] = "2024-01-06"
    with pytest.raises(ValidationError):
        PeerStudy.model_validate(raw)


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("market", "CN_A_H", "CONTEXT_ONLY"),
        ("market", "QDII", "EXCLUDED"),
        ("product_type", "INDEX", "EXCLUDED"),
        ("active", None, "UNVERIFIED"),
        ("medical_non_cash_min_pct", 50, "EXCLUDED"),
    ],
)
def test_admission_does_not_loosen_for_returns(field, value, status):
    raw = payload()
    raw["products"][1][field] = value
    study = PeerStudy.model_validate(raw)
    assert admission(study.products[1], study)[0] == status
    result = evaluate(study)["rows"][1]
    assert all(w["calculated"] is None for w in result["windows"])


def test_reported_only_and_reconstruction_are_distinct():
    raw = payload()
    p = raw["products"][1]
    p["reported"] = [dict(start="2023-01-02", end="2023-01-04", return_pct=99, evidence_ids=["P"])]
    w = evaluate(PeerStudy.model_validate(raw))["rows"][1]["windows"][0]
    assert "DISCLOSED_RETURN_MISMATCH" in w["warnings"]
    p["nav"] = None
    w = evaluate(PeerStudy.model_validate(raw))["rows"][1]["windows"][0]
    assert w["calculated"] is None and w["reported"][0]["return_pct"] == "99"


def test_append_only_replay_history_and_get_are_readonly(tmp_path):
    db = tmp_path / "test.db"
    migrate_database(db)
    settings = Settings(_env_file=None, db_path=db, environment=Environment.TEST)
    service = PeerResearchService(
        ResearchService(settings, now=lambda: datetime(2024, 1, 6, tzinfo=UTC))
    )

    def dump():
        with sqlite3.connect(db) as c:
            return list(c.iterdump())

    original = dump()
    study = PeerStudy.model_validate(payload())
    with ThreadPoolExecutor(max_workers=3) as workers:
        records = list(workers.map(lambda _: service.archive(study), range(3)))
    assert len({r["id"] for r in records}) == 1
    after = dump()
    assert [s for s in after if 'INSERT INTO "peer_research_runs"' not in s] == original
    web = TestClient(create_app(settings))
    q = dict(anchor_code="R")
    r = web.get("/v1/peer-research", params=q).json()["data"]
    assert r == web.get("/v1/peer-research", params=q).json()["data"]
    assert not r["writes_performed"] and not r["money_action"] and not r["approval_mutation"]
    assert (
        web.get("/v1/peer-research", params=dict(q, view="DETAIL", instrument_code="P")).status_code
        == 200
    )
    assert web.get("/v1/peer-research", params=dict(q, as_of="2024-01-04")).status_code == 404
    assert dump() == after
    raw = payload()
    raw["expected_previous_version"] = 1
    raw["idempotency_key"] = "two"
    raw["products"][1]["nav"]["points"][2]["value"] = 1.3
    second = service.archive(PeerStudy.model_validate(raw))
    assert second["version"] == 2
    assert service.read("R")["version"] == 2
    assert service.read("R", run_id=records[0]["id"])["rows"] == r["rows"]
    assert (
        service.read("R", details=True)["archived_input"]["sources"]["P"]["excerpt"]
        == "isolated fixture only"
    )
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM peer_research_runs").fetchone() == (2,)
        assert c.execute("SELECT COUNT(*) FROM holding_review_snapshots").fetchone() == (0,)


def test_future_series_and_wrong_share_report_rejected():
    for field in ("series", "report"):
        raw = payload()
        if field == "series":
            raw["products"][1]["nav"]["points"][-1]["day"] = "2025-01-01"
        else:
            raw["products"][1]["reported"] = [
                dict(start="2023-01-02", end="2023-01-04", return_pct=1, evidence_ids=["R"])
            ]
        with pytest.raises(ValidationError):
            PeerStudy.model_validate(raw)


def test_duplicate_requests_conflict_and_version_guard(tmp_path):
    from investor_core.ledger import LedgerError

    db = tmp_path / "test.db"
    migrate_database(db)
    service = PeerResearchService(
        ResearchService(Settings(_env_file=None, db_path=db, environment=Environment.TEST))
    )
    first = service.archive(PeerStudy.model_validate(payload()))
    raw = payload()
    raw["idempotency_key"] = "different-key-same-data"
    assert service.archive(PeerStudy.model_validate(raw))["id"] == first["id"]
    raw = payload()
    raw["products"][0]["nav"]["points"][1]["value"] = "0.8"
    with pytest.raises(LedgerError, match="同一请求"):
        service.archive(PeerStudy.model_validate(raw))
    raw["idempotency_key"] = "different"
    with pytest.raises(LedgerError, match="研究版本"):
        service.archive(PeerStudy.model_validate(raw))


def test_peer_migration_preserves_every_existing_table(tmp_path):
    from test_migrations import migrate_to

    db = tmp_path / "old.db"
    migrate_to(db, "0042_holding_review")
    with sqlite3.connect(db) as c:
        before = list(c.iterdump())
    migrate_database(db)
    migrate_database(db)
    with sqlite3.connect(db) as c:
        after = list(c.iterdump())
        assert c.execute("SELECT COUNT(*) FROM peer_research_runs").fetchone() == (0,)

    def original(lines):
        return [
            line
            for line in lines
            if "alembic_version" not in line and "peer_research_runs" not in line
        ]

    assert original(before) == original(after)
