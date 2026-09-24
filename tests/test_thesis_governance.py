from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_notebook import dump, setup_case

from investor_core.api.app import create_app
from investor_core.execution import ExecutionService, SourceArchive
from investor_core.ledger import LedgerError
from investor_core.notebook import CaseDraft, NotebookService
from investor_core.thesis import ThesisConfirmation, ThesisDraft, ThesisObservation, ThesisService


def setup(tmp_path):
    settings, pid, aid, research, payload = setup_case(tmp_path)
    notebook = NotebookService(research)
    case = notebook.create(CaseDraft.model_validate(payload))
    return settings, pid, aid, research, payload, notebook, case, ThesisService(notebook)


def proposal(pid, case, **changes):
    return ThesisDraft.model_validate(
        dict(
            portfolio_id=pid,
            instrument_code="CORE01",
            case_version=case["version"],
            expected_decision_id=None,
            action="ACTIVATE",
            idempotency_key="activate-1",
            rationale="current research, never historic intent",
            **changes,
        )
    )


def confirm(svc, draft):
    return svc.confirm(
        draft["id"],
        ThesisConfirmation(
            confirmation_token=draft["confirmation_token"], confirmed_by="test-user"
        ),
    )


def test_history_unknown_reason_and_money_unchanged(tmp_path):
    settings, pid, aid, _research, payload, notebook, case, svc = setup(tmp_path)
    web = TestClient(create_app(settings))
    scope = dict(portfolio_id=pid, account_id=aid)
    before = web.get(
        "/v1/weekly-plan-preview",
        params=dict(scope, contribution_amount="200", as_of_date="2026-07-21"),
    ).json()
    ledger_before = [
        x
        for x in dump(settings.db_path)
        if not any(t in x for t in ("research_thesis_", "research_case_versions"))
    ]
    draft = svc.create(proposal(pid, case))
    with pytest.raises(LedgerError, match="Invalid content"):
        svc.confirm(
            draft["id"], ThesisConfirmation(confirmation_token="wrong", confirmed_by="test-user")
        )
    approved = confirm(svc, draft)
    assert confirm(svc, draft)["idempotent_replay"]
    first = svc.read(pid, "CORE01")
    assert first["status"] == "ACTIVE_RESEARCH" and first["original_buy_reason"] is None
    revised = deepcopy(payload)
    revised.update(expected_previous_version=1, idempotency_key="case-2")
    revised["thesis"]["playbook"] = "STRATEGIC_DCA"
    second = notebook.create(CaseDraft.model_validate(revised))
    assert svc.read(pid, "CORE01")["active_case"]["version"] == 1
    assert "playbook" in svc.read(pid, "CORE01")["candidate_changes"]
    d2 = svc.create(
        ThesisDraft(
            portfolio_id=pid,
            instrument_code="CORE01",
            case_version=2,
            expected_decision_id=approved["id"],
            action="ACTIVATE",
            idempotency_key="activate-2",
            rationale="explicit new playbook research only",
        )
    )
    confirm(svc, d2)
    result = svc.read(pid, "CORE01")
    assert result["active_case"]["version"] == 2 and "playbook" in result["changed_fields"]
    assert result["history"][0]["reviewed_context"]["case"]["thesis"]["playbook"] == "RESEARCH_ONLY"
    assert notebook.read(pid, "CORE01")["versions"][0] == case
    assert not second["approved"]  # Original immutable draft is not rewritten.
    assert all("confirmation_digest" not in x for x in result["history"])
    assert not result["money_action"]
    assert (
        web.get(
            "/v1/weekly-plan-preview",
            params=dict(scope, contribution_amount="200", as_of_date="2026-07-21"),
        ).json()["data"]["plan"]
        == before["data"]["plan"]
    )
    assert [
        x
        for x in dump(settings.db_path)
        if not any(t in x for t in ("research_thesis_", "research_case_versions"))
    ] == ledger_before
    current = dump(settings.db_path)
    response = web.get("/v1/portfolio-research-summary", params=scope)
    assert response.status_code == 200
    item = next(i for i in response.json()["data"]["items"] if i["instrument_code"] == "CORE01")
    assert item["thesis"]["active_case"]["version"] == 2
    assert "历史买入理由" in item["thesis"]["display_text"]
    assert dump(settings.db_path) == current


def test_expiry_contradiction_and_explicit_invalidation(tmp_path):
    _, pid, _, research, _, _, case, svc = setup(tmp_path)
    eid = case["evidence_ids"][0]
    draft = svc.create(
        proposal(
            pid,
            case,
            evidence_watches=[
                dict(evidence_id=eid, valid_until="2026-09-23", basis="explicit reviewed validity")
            ],
        )
    )
    approved = confirm(svc, draft)
    original = deepcopy(approved)
    research._now = lambda: datetime(2026, 9, 24, 8, tzinfo=UTC)
    assert svc.read(pid, "CORE01")["status"] == "REVIEW_REQUIRED"
    assert svc.read(pid, "CORE01")["review_reasons"][0]["code"] == "EVIDENCE_EXPIRED"
    observation = ThesisObservation(
        portfolio_id=pid,
        instrument_code="CORE01",
        case_version=1,
        kind="CONTRADICTS",
        evidence_ids=[eid],
        observed_on="2026-09-24",
        explanation="identified contrary disclosure, needs review",
        idempotency_key="counter-1",
    )
    item = svc.observe(observation)
    assert svc.observe(observation)["id"] == item["id"]
    result = svc.read(pid, "CORE01")
    assert result["decision"] == original and result["active_case"]["version"] == 1
    assert any(
        x["code"] == "CONTRADICTS" and x["observation_id"] == item["id"]
        for x in result["review_reasons"]
    )
    invalid = svc.create(
        ThesisDraft(
            portfolio_id=pid,
            instrument_code="CORE01",
            case_version=1,
            expected_decision_id=approved["id"],
            action="INVALIDATE",
            idempotency_key="invalidate-1",
            rationale="review conclusion for this exact thesis",
        )
    )
    assert svc.read(pid, "CORE01")["status"] == "REVIEW_REQUIRED"
    confirm(svc, invalid)
    assert svc.read(pid, "CORE01")["status"] == "INVALIDATED"
    assert svc.read(pid, "CORE01")["history"][0] == original


def test_content_drift_expired_confirmation_and_concurrent_replay(tmp_path):
    _, pid, _, research, _, _, case, svc = setup(tmp_path)
    request = proposal(pid, case)
    draft = svc.create(request)
    svc.observe(
        ThesisObservation(
            portfolio_id=pid,
            instrument_code="CORE01",
            case_version=1,
            kind="DATA_GAP",
            evidence_ids=case["evidence_ids"],
            observed_on="2026-09-23",
            explanation="unresolved period",
            idempotency_key="gap",
        )
    )
    with pytest.raises(LedgerError, match="changed"):
        confirm(svc, draft)
    fresh = svc.create(request.model_copy(update={"idempotency_key": "fresh"}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: confirm(svc, fresh), range(4)))
    assert len({x["id"] for x in results}) == 1
    assert len([x for x in svc.read(pid, "CORE01")["history"] if x["status"] == "CONFIRMED"]) == 1
    stale = svc.create(
        request.model_copy(
            update={"idempotency_key": "expired", "expected_decision_id": fresh["id"]}
        )
    )
    research._now = lambda: datetime(2026, 9, 25, 8, tzinfo=UTC)
    with pytest.raises(LedgerError, match="fresh draft"):
        confirm(svc, stale)


def test_source_change_is_traceable_not_automatic_invalidation(tmp_path):
    _, pid, _, research, _, _, case, svc = setup(tmp_path)
    confirm(svc, svc.create(proposal(pid, case)))
    newer = ExecutionService(research).archive(
        SourceArchive(
            instrument_code="CORE01",
            source_name="official",
            source_ref="https://example.org/report",
            source_lineage="issuer",
            retrieved_at=datetime(2026, 9, 23, 9, tzinfo=UTC),
            published_date="2026-09-23",
            data_date="2026-09-23",
            excerpt="revised public source",
            quality="OFFICIAL",
        )
    )
    result = svc.read(pid, "CORE01")
    reason = next(x for x in result["review_reasons"] if x["code"] == "SOURCE_CHANGED")
    assert reason["evidence_ids"] == [case["evidence_ids"][0], newer["id"]]
    assert result["status"] == "REVIEW_REQUIRED" and result["active_case"] is not None
    assert result["money_action"] is False


def test_scope_conditions_missing_reason_and_http_confirmation(tmp_path):
    settings, pid, _, _, _, _, case, svc = setup(tmp_path)
    with pytest.raises(LedgerError):
        svc.observe(
            ThesisObservation(
                portfolio_id=pid,
                instrument_code="CORE01",
                case_version=1,
                kind="INVALIDATION_OBSERVED",
                evidence_ids=case["evidence_ids"],
                observed_on="2026-09-23",
                explanation="unsupported condition",
                invalidation_condition="invented numeric threshold",
                idempotency_key="bad",
            )
        )
    with pytest.raises(LedgerError):
        svc.create(
            proposal(pid, case, evidence_watches=[dict(evidence_id="wrong", basis="wrong fund")])
        )
    with TestClient(create_app(settings)) as web:
        data = proposal(pid, case).model_dump(mode="json")
        d = web.post("/v1/research-thesis-drafts", json=data).json()["data"]
        assert (
            web.get(
                "/v1/research-thesis", params={"portfolio_id": pid, "instrument_code": "CORE01"}
            ).json()["data"]["status"]
            == "UNAPPROVED"
        )
        res = web.post(
            f"/v1/research-thesis-drafts/{d['id']}/confirm",
            json={"confirmation_token": d["confirmation_token"], "confirmed_by": "test-user"},
        )
        assert res.status_code == 200
        before = dump(settings.db_path)
        out = web.get(
            "/v1/research-case", params={"portfolio_id": pid, "instrument_code": "CORE01"}
        ).json()["data"]
        assert out["governance"]["original_buy_reason"] is None
        assert out["governance"]["decision"]["status"] == "CONFIRMED"
        assert dump(settings.db_path) == before


def test_competing_content_confirmations_and_late_replay(tmp_path):
    _, pid, _, research, _, _, case, svc = setup(tmp_path)
    one = svc.create(proposal(pid, case))
    two = svc.create(proposal(pid, case).model_copy(update={"idempotency_key": "other"}))
    approved = confirm(svc, one)
    with pytest.raises(LedgerError, match="changed"):
        confirm(svc, two)
    research._now = lambda: datetime(2026, 9, 30, tzinfo=UTC)
    assert confirm(svc, one)["id"] == approved["id"]
    assert confirm(svc, one)["idempotent_replay"]


def test_migration_preserves_existing_research_and_facts(tmp_path):
    import sqlite3
    from contextlib import closing

    from test_migrations import migrate_database, migrate_to

    path = tmp_path / "old.db"
    migrate_to(path, "0040_research_benchmarks")
    with closing(sqlite3.connect(path)) as c:
        c.execute(
            "INSERT INTO settings VALUES "
            "('thesis-test',1,'{}','fixture','ACTIVE','test-user','2026-09-24','2026-09-24')"
        )
        c.commit()
        old = list(c.iterdump())
    migrate_database(path)
    migrate_database(path)
    with closing(sqlite3.connect(path)) as c:
        after = list(c.iterdump())
        tables = {x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"research_thesis_actions", "research_thesis_observations"} <= tables

    def existing(lines):
        return [x for x in lines if "alembic_version" not in x and "research_thesis_" not in x]

    assert existing(old) == existing(after)


def test_evidence_expiry_uses_business_timezone(tmp_path):
    _, pid, _, research, _, _, case, svc = setup(tmp_path)
    confirm(
        svc,
        svc.create(
            proposal(
                pid,
                case,
                evidence_watches=[
                    dict(
                        evidence_id=case["evidence_ids"][0],
                        valid_until="2026-09-23",
                        basis="reviewed cutoff",
                    )
                ],
            )
        ),
    )
    research._now = lambda: datetime(2026, 9, 23, 15, 59, tzinfo=UTC)
    assert svc.read(pid, "CORE01")["status"] == "ACTIVE_RESEARCH"
    research._now = lambda: datetime(2026, 9, 23, 16, 1, tzinfo=UTC)
    assert svc.read(pid, "CORE01")["status"] == "REVIEW_REQUIRED"
