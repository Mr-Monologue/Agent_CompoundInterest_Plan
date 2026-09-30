from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from threading import Event

import httpx
import pytest
from conftest import migrate_database
from fastapi.testclient import TestClient
from test_peer_research import payload

from investor_core.api.app import create_app
from investor_core.config import Settings
from investor_core.daily_client import AssistantError, DailyClient
from investor_core.peer_models import PeerStudy
from investor_core.peer_research import PeerResearchService, evaluate
from investor_core.research import ResearchService
from investor_core.research_update_build import build, diffs
from investor_core.research_update_sources import fingerprint, parse
from investor_core.research_updates import ResearchUpdates, UpdateRequest


def study():
    x = json.loads(json.dumps(payload()).replace('"R"', '"003096"').replace('"P"', '"009163"'))
    x["windows"] = [dict(x["windows"][i % 2], label=f"w{i}") for i in range(6)]
    for w in x["windows"][-2:]:
        w["end"] = x["calendar_dates"][-1]
    return x


def services(tmp_path):
    settings = Settings(db_path=tmp_path / "test.db", _env_file=None)
    migrate_database(settings.db_path)
    clock = [datetime(2024, 1, 10, tzinfo=UTC)]
    research = ResearchService(settings, now=lambda: clock[0])
    peers = PeerResearchService(research)
    peers.archive(PeerStudy.model_validate(study()))
    return research, peers, clock


def source_probe(old, mode="normal"):
    s = old["archived_input"]
    pp = {p["code"]: p for p in s["products"]}

    def probe(key, url, kind, code):
        base = dict(
            source=key,
            url=url,
            kind=kind,
            evidence_id=key,
            raw_hash="b" * 64,
            retrieved_at="2024-01-10T00:00:00Z",
        )
        if mode == "failure" and key == "003096-nav":
            return dict(base, status="FAILED", error="OFFLINE")
        if kind in ("nav", "channel_nav"):
            value = deepcopy(pp[code]["nav"]["points"])
            for r in value:
                r["value"] = str(r["value"])
            if mode == "conflict" and key == "003096-channel_nav":
                value[1]["value"] = "123"
            if mode == "missing" and key == "009163-nav":
                value.pop(1)
        elif kind == "calendar":
            value = [dict(day=d, value="100") for d in s["calendar_dates"]]
        elif kind == "disclosure":
            value = dict(links=[], completeness="UNVERIFIED")
        else:
            value = []
        return dict(base, status="SUCCESS", parsed=value)

    return probe


def test_real_pipeline_semantic_repeat_and_fixed_history(tmp_path):
    _r, p, _ = services(tmp_path)
    old = p.read("003096", details=True)
    first = build(old, date(2024, 1, 10), source_probe(old))
    assert first["recomputed_windows"] == 12 and first["publication_needed"]
    assert not first["changes"]
    x = first["candidate"]
    p.archive(PeerStudy.model_validate(x))
    current = p.read("003096", details=True)
    second = build(current, date(2024, 1, 11), source_probe(current))
    assert not second["publication_needed"]
    assert second["content_status"] == "NO_NEW_CONTENT"
    assert second["candidate"]["windows"][:4] == old["archived_input"]["windows"][:4]
    assert len(p.read("003096")["history"]) == 2


@pytest.mark.parametrize("mode", ["failure", "missing", "conflict"])
def test_incomplete_or_conflict_never_becomes_unchanged(tmp_path, mode):
    _, p, _ = services(tmp_path)
    old = p.read("003096", details=True)
    result = build(old, date(2024, 1, 10), source_probe(old, mode))
    assert result["status"] == "BLOCKED" and result["candidate"] is None
    assert "content_status" not in result
    assert len(p.read("003096")["history"]) == 1


def fake_build(old, today, probe):
    # Synthetic changes only in isolated test data.
    s = deepcopy(old["archived_input"])
    s["products"][0]["nav"]["points"][-1]["value"] = "1.3"
    s["expected_previous_version"] = old["version"]
    s["idempotency_key"] = "stable-content"
    return dict(
        anchor_code="003096",
        status="SUCCESS",
        candidate=s,
        publication_needed=True,
        changes=[],
        content_status="NEW_EVIDENCE",
    )


def test_readonly_replay_parallel_and_response_loss(tmp_path, monkeypatch):
    research, peers, clock = services(tmp_path)
    monkeypatch.setattr("investor_core.research_updates.build", fake_build)
    service = ResearchUpdates(research, fetch_factory=lambda: None)

    def count():
        with sqlite3.connect(research.settings.db_path) as c:
            return list(c.iterdump())

    before = count()
    service.check()
    assert before == count()
    req = UpdateRequest(scope="003096", idempotency_key="original")
    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(lambda _: service.run(req), range(2)))
    assert len(peers.read("003096")["history"]) == 2
    assert service.run(req)["status"] == "SUCCESS"
    assert len(peers.read("003096")["history"]) == 2
    # New intentional check can log an event; exact publication content still reused.
    clock[0] += timedelta(minutes=1)
    result = service.run(UpdateRequest(scope="003096", idempotency_key="second"))
    assert result["cases"][0]["publication"]["reused"]
    assert len(peers.read("003096")["history"]) == 2
    before = count()
    web = TestClient(create_app(research.settings))
    assert web.get("/v1/research-update-check").status_code == 200
    assert web.get("/v1/research-updates/latest").status_code == 200
    assert before == count()


def test_failure_preserves_last_success_and_old_version(tmp_path, monkeypatch):
    research, peers, clock = services(tmp_path)
    svc = ResearchUpdates(research, fetch_factory=lambda: None)
    monkeypatch.setattr("investor_core.research_updates.build", fake_build)
    svc.run(UpdateRequest(scope="003096", idempotency_key="ok"))
    clock[0] += timedelta(minutes=1)

    def fail(*args):
        raise ValueError("PARSE_FAILED")

    monkeypatch.setattr("investor_core.research_updates.build", fail)
    result = svc.run(UpdateRequest(scope="003096", idempotency_key="fail"))
    assert result["status"] == "FAILED" and result["last_successful_result"]
    assert len(peers.read("003096")["history"]) == 2
    assert "没有变化" not in result["display_text"]


def test_staged_restart_atomic_publication(tmp_path, monkeypatch):
    research, peers, clock = services(tmp_path)
    svc = ResearchUpdates(research, fetch_factory=lambda: None)
    original = peers.archive
    monkeypatch.setattr("investor_core.research_updates.build", fake_build)

    def crash(*a, **kw):
        raise RuntimeError("process stopped before commit")

    monkeypatch.setattr(svc.peers, "archive", crash)
    req = UpdateRequest(scope="003096", idempotency_key="recover")
    with pytest.raises(RuntimeError):
        svc.run(req)
    assert len(peers.read("003096")["history"]) == 1
    clock[0] += timedelta(minutes=31)
    assert svc.read("recover")["status"] == "INTERRUPTED"
    monkeypatch.setattr(svc.peers, "archive", original)
    monkeypatch.setattr(
        "investor_core.research_updates.build", lambda *a: pytest.fail("must reuse staged input")
    )
    result = svc.run(req.model_copy(update={"resume": True}))
    assert result["status"] == "SUCCESS" and len(peers.read("003096")["history"]) == 2


def test_same_scope_concurrent_distinct_keys_returns_active(tmp_path, monkeypatch):
    research, peers, _ = services(tmp_path)
    entered = Event()
    release = Event()

    def slow(*a):
        entered.set()
        assert release.wait(10)
        return fake_build(*a)

    monkeypatch.setattr("investor_core.research_updates.build", slow)
    svc = ResearchUpdates(research, fetch_factory=lambda: None)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f = ex.submit(svc.run, UpdateRequest(scope="003096", idempotency_key="a"))
        assert entered.wait(10)
        second = svc.run(UpdateRequest(scope="003096", idempotency_key="b"))
        assert second["status"] == "RUNNING"
        release.set()
        assert f.result()["status"] == "SUCCESS"
    assert len(peers.read("003096")["history"]) == 2


def test_parser_identity_dividends_and_semantic_decorations():
    raw = dict(
        code=0, data=[dict(productCode="003096", navDate="2024-01-02", relatePrice="1.0000")]
    )
    a = parse(json.dumps(raw).encode(), "nav", "003096")
    raw["cached_at"] = "later"
    raw["data"][0]["relatePrice"] = "1.0"
    assert fingerprint(a) == fingerprint(
        parse(json.dumps(raw, sort_keys=True).encode(), "nav", "003096")
    )
    with pytest.raises(ValueError):
        parse(json.dumps(raw).encode(), "nav", "009163")
    div = dict(
        code=0,
        data=dict(
            total=1,
            pages=1,
            list=[dict(productId=391, divideinterest="2024-01-02", sendinterest="1.25")],
        ),
    )
    assert parse(json.dumps(div).encode(), "div", "003096")[0]["cash_per_unit"] == "0.125"
    div["data"]["total"] = 2
    with pytest.raises(ValueError):
        parse(json.dumps(div).encode(), "div", "003096")
    html = '<title>003096</title><table class="cfxq">每10份分红<tbody>unrecognized</tbody></table>'
    with pytest.raises(ValueError):
        parse(html.encode(), "channel_div", "003096")


def test_delta_precision_and_window_advance_not_ability():
    old = evaluate(PeerStudy.model_validate(study()))
    new = deepcopy(old)
    new["rows"][0]["windows"][0]["calculated"]["return_pct"] += 0.00001
    d = diffs(old, new)
    assert d[0]["materiality"] == "DISPLAY_PRECISION_ONLY"
    new["rows"][0]["windows"][0]["end"] = "2024-01-05"
    assert all(x["kind"] == "WINDOW_ADVANCE" for x in diffs(old, new))


def test_client_lost_response_reads_original_no_new_post(tmp_path):
    sent = []

    def handler(req):
        if req.method == "POST":
            sent.append(json.loads(req.content))
            raise httpx.ReadTimeout("lost")
        return httpx.Response(
            200, json={"data": {"status": "SUCCESS", "display_text": "recovered"}}
        )

    client = DailyClient(
        client=httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)),
        journal=tmp_path / "journal.db",
    )
    with pytest.raises(AssistantError):
        client.update_research("003096")
    assert client.update_research("003096")["display_text"] == "recovered"
    assert len(sent) == 1


def test_update_migration_preserves_every_existing_table(tmp_path):
    from test_migrations import migrate_to

    db = tmp_path / "old.db"
    migrate_to(db, "0043_peer_research")
    with sqlite3.connect(db) as c:
        before = list(c.iterdump())
    migrate_database(db)
    migrate_database(db)
    with sqlite3.connect(db) as c:
        after = list(c.iterdump())

    def original(lines):
        return [
            s
            for s in lines
            if not any(
                n in s
                for n in (
                    "alembic_version",
                    "research_update_runs",
                    "research_update_evidence",
                    "uq_research_update_active",
                )
            )
        ]

    assert original(before) == original(after)


def test_failed_parse_raw_evidence_retained(tmp_path, monkeypatch):
    research, peers, _ = services(tmp_path)

    def fake(old, today, probe):
        receipt = probe("bad", "https://www.zofund.com/sample", "nav", "003096")
        assert receipt["status"] == "FAILED" and receipt["evidence_id"]
        return dict(
            anchor_code="003096", status="BLOCKED", candidate=None, checks=[receipt], changes=[]
        )

    monkeypatch.setattr("investor_core.research_updates.build", fake)
    svc = ResearchUpdates(research, fetch_factory=lambda: lambda _: b"not-json")
    result = svc.run(UpdateRequest(scope="003096", idempotency_key="raw-failure"))
    eid = result["cases"][0]["checks"][0]["evidence_id"]
    assert svc.evidence(eid)["raw_base64"] == "bm90LWpzb24="
    assert len(peers.read("003096")["history"]) == 1


def test_failure_of_one_case_continues_other(tmp_path, monkeypatch):
    research, _, _ = services(tmp_path)
    monkeypatch.setattr("investor_core.research_updates.build", fake_build)
    svc = ResearchUpdates(research, fetch_factory=lambda: None)
    result = svc.run(UpdateRequest(idempotency_key="two"))
    assert result["status"] == "PARTIAL"
    assert [r["status"] for r in result["cases"]] == ["FAILED", "SUCCESS"]


def test_cash_page_decoration_is_not_new_evidence():
    table = (
        "<table><tr><th>调整时间</th><th>活期存款</th></tr>"
        "<tr><td>2012.07.06</td><td>0.35</td></tr></table>"
    )
    first = parse(("<script>clock=1</script>" + table).encode(), "cash", "PUBLIC")
    second = parse(
        ("<style>new-style</style>" + table + "<footer>new decoration</footer>").encode(),
        "cash",
        "PUBLIC",
    )
    assert fingerprint(first) == fingerprint(second)
    assert first["applicability"] == "UNVERIFIED"
