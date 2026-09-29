from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date

import pytest
from fastapi.testclient import TestClient
from test_benchmarks import input_for, setup_mapping
from test_daily_client import dump

from investor_core.api.app import create_app
from investor_core.benchmarks import DiagnosticInput, MappingDraft
from investor_core.holding_review import (
    HoldingReviewService,
    ReviewCapture,
    ReviewHandling,
    fund_review,
)
from investor_core.ledger import LedgerError
from investor_core.market_data import MarketDataService
from investor_core.thesis import ThesisService


def setup(tmp_path):
    settings, pid, aid, _research, eid, bench, payload = setup_mapping(tmp_path)
    mapping = bench.create(MappingDraft.model_validate(payload))
    inputs = input_for(mapping, eid)
    inputs["distributions"] = []
    for point, value in zip(inputs["fund"]["points"], ["1", "0.8", "0.7"], strict=True):
        point["value"] = value
    bench.run(DiagnosticInput.model_validate(inputs))
    svc = HoldingReviewService(bench, ThesisService(bench.notebook), MarketDataService(settings))
    return settings, pid, aid, eid, svc, inputs


def capture(svc, pid, aid, key="capture"):
    return ReviewCapture(
        portfolio_id=pid,
        account_id=aid,
        expected_input_hash=svc.preview(pid, aid)["input_hash"],
        idempotency_key=key,
    )


def business(path):
    return [s for s in dump(path) if "holding_review_" not in s]


def test_full_loop_readonly_replay_and_no_money(tmp_path):
    settings, pid, aid, eid, svc, _ = setup(tmp_path)
    web = TestClient(create_app(settings))
    q = dict(portfolio_id=pid, account_id=aid)
    before = dump(settings.db_path)
    p = svc.preview(pid, aid)
    assert p == svc.preview(pid, aid)
    assert all(i["changes"]["status"] == "NO_BASELINE" for i in p["items"])
    assert dump(settings.db_path) == before
    plan = web.get(
        "/v1/weekly-plan-preview",
        params=dict(q, contribution_amount="200", as_of_date="2026-07-21"),
    ).json()
    original = business(settings.db_path)
    req = capture(svc, pid, aid)
    with ThreadPoolExecutor(max_workers=3) as pool:
        saved = list(pool.map(lambda _: svc.capture(req), range(3)))
    assert len({r["id"] for r in saved}) == 1
    initial = svc.history(pid, aid)
    assert len(initial["snapshots"]) == 1 and initial["tasks"]
    task = next(t for t in initial["tasks"] if t["reason"]["kind"] == "RELATIVE_LAG")
    assert (
        task["reason"]["sources"]
        and task["reason"]["basis"]["fund_return_basis"] == "NAV_NET_INTERNAL_FEES"
    )
    assert not task["reason"]["basis"]["mapping_approved"]
    handle = ReviewHandling(
        **q,
        expected_event_id=None,
        outcome="REVIEWED_WITH_LIMITATIONS",
        explanation="Compared the dated sources; candidate mapping remains unapproved",
        evidence_ids=[eid],
        idempotency_key="handle",
    )
    result = svc.handle(task["id"], handle)
    assert svc.handle(task["id"], handle) == result
    assert result["issue_resolved"] is None and not result["approval_effect"]
    repeated = svc.capture(capture(svc, pid, aid, "repeat"))
    assert repeated["created_task_ids"] == []
    assert len(svc.history(pid, aid)["tasks"]) == len(initial["tasks"])
    read = web.get("/v1/holding-review", params=q)
    assert read.status_code == 200
    assert read.json()["data"]["unified_ranking"] is None
    assert web.get("/v1/holding-review-history", params=q).status_code == 200
    assert business(settings.db_path) == original
    assert (
        web.get(
            "/v1/weekly-plan-preview",
            params=dict(q, contribution_amount="200", as_of_date="2026-07-21"),
        ).json()["data"]["plan"]
        == plan["data"]["plan"]
    )
    assert all(i["thesis"]["historical_buy_reason"] is None for i in read.json()["data"]["items"])


def test_changed_evidence_preserves_history_and_stale_request(tmp_path):
    _settings, pid, aid, _, svc, inputs = setup(tmp_path)
    req = capture(svc, pid, aid)
    svc.capture(req)
    old = deepcopy(svc.history(pid, aid))
    inputs["idempotency_key"] = "revised-series"
    inputs["fund"]["points"][-1]["value"] = "0.6"
    svc.benchmarks.run(DiagnosticInput.model_validate(inputs))
    p = svc.preview(pid, aid)
    assert any(i["changes"]["status"] == "CHANGED" for i in p["items"])
    with pytest.raises(LedgerError, match="current review"):
        svc.capture(req.model_copy(update={"idempotency_key": "stale"}))
    new = svc.capture(capture(svc, pid, aid, "new"))
    history = svc.history(pid, aid)
    assert history["snapshots"][0] == old["snapshots"][0]
    assert history["tasks"][: len(old["tasks"])] == old["tasks"]
    assert any(
        t["supersedes_task_id"] for t in history["tasks"] if t["id"] in new["created_task_ids"]
    )
    assert all(i["changes"]["status"] == "UNCHANGED" for i in svc.preview(pid, aid)["items"])


def test_missing_noncomparable_and_historical_window(tmp_path):
    _, pid, aid, _, svc, _ = setup(tmp_path)
    record = svc.benchmarks.read(pid, "CORE01")
    record["thesis"] = svc.theses.read(pid, "CORE01")
    record["evidence"] = svc.research.list_evidence(instrument_code="CORE01", limit=1000)
    historical = fund_review("CORE01", "bond-like product", record, date(2026, 9, 1))
    assert historical["windows"] == [] and historical["comparison"]["eligibility"] == "NO_WINDOW"
    assert not historical["comparison"]["peer_ranking_eligible"]
    assert historical["comparison"]["product_labels"] == []
    assert not any(r["kind"] == "RELATIVE_LAG" for r in historical["reasons"])
    result = svc.preview(pid, aid)
    assert result["unified_ranking"] is None
    assert all(not i["comparison"]["peer_ranking_eligible"] for i in result["items"])
    assert any(r["kind"] == "FEES_UNKNOWN" for i in result["items"] for r in i["reasons"])


def test_handling_scope_conflicts_and_api(tmp_path):
    settings, pid, aid, eid, svc, _ = setup(tmp_path)
    web = TestClient(create_app(settings))
    q = dict(portfolio_id=pid, account_id=aid)
    req = capture(svc, pid, aid)
    res = web.post("/v1/holding-review-captures", json=req.model_dump())
    assert res.status_code == 200
    task = svc.history(pid, aid)["tasks"][0]
    body = dict(
        q,
        expected_event_id=None,
        outcome="REQUEST_EVIDENCE",
        explanation="Need independent data",
        evidence_ids=[eid],
        idempotency_key="handle",
    )
    url = "/v1/holding-review-tasks/" + task["id"] + "/handling"
    res = web.post(url, json=body)
    assert res.status_code == 200
    with pytest.raises(LedgerError, match="latest handling"):
        svc.handle(task["id"], ReviewHandling(**dict(body, idempotency_key="stale")))
    with pytest.raises(LedgerError, match="key content"):
        svc.handle(task["id"], ReviewHandling(**dict(body, explanation="different")))
    with pytest.raises(LedgerError, match="not in this scope"):
        svc.handle(
            task["id"], ReviewHandling(**dict(body, account_id="wrong", idempotency_key="scope"))
        )


def test_new_migration_preserves_old_tables(tmp_path):
    import sqlite3
    from contextlib import closing

    from test_migrations import migrate_database, migrate_to

    path = tmp_path / "old.db"
    migrate_to(path, "0041_thesis_governance")
    with closing(sqlite3.connect(path)) as c:
        before = list(c.iterdump())
    migrate_database(path)
    migrate_database(path)
    with closing(sqlite3.connect(path)) as c:
        after = list(c.iterdump())

    def old(lines):
        return [
            x
            for x in lines
            if "alembic_version" not in x
            and "holding_review_" not in x
            and "peer_research_runs" not in x
        ]

    assert old(before) == old(after)


def test_identical_calculation_with_new_request_does_not_duplicate_reason(tmp_path):
    _settings, pid, aid, _eid, svc, inputs = setup(tmp_path)
    before = svc.preview(pid, aid)
    svc.capture(capture(svc, pid, aid))
    inputs["idempotency_key"] = "same-data-new-request"
    svc.benchmarks.run(DiagnosticInput.model_validate(inputs))
    after = svc.preview(pid, aid)
    assert after["input_hash"] == before["input_hash"]
    assert all(i["changes"]["status"] == "UNCHANGED" for i in after["items"])
    result = svc.capture(capture(svc, pid, aid, "repeat-calculation"))
    assert result["created_task_ids"] == []
    assert len(svc.history(pid, aid)["snapshots"]) == 1
