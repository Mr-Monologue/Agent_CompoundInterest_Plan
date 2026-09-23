from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_daily_client import dump
from test_weekly_reports import terminal_plan

from investor_core.api.app import create_app
from investor_core.execution import ExecutionService, QuotaCapture, SourceArchive
from investor_core.ledger import LedgerError
from investor_core.notebook import CaseDraft, JournalEntry, NotebookService
from investor_core.research import ResearchService


def setup_case(tmp_path):
    settings, pid, aid, _ = terminal_plan(tmp_path / "research.db", outcome="SKIPPED")
    research = ResearchService(settings, now=lambda: datetime(2026, 9, 23, 8, tzinfo=UTC))
    source = ExecutionService(research).archive(
        SourceArchive(
            instrument_code="CORE01",
            source_name="official",
            source_ref="https://example.org/report",
            source_lineage="issuer",
            retrieved_at=datetime(2026, 9, 23, tzinfo=UTC),
            published_date="2026-09-01",
            data_date="2026-06-30",
            excerpt="public source",
            quality="OFFICIAL",
        )
    )
    claim = dict(
        text="mandate",
        kind="PUBLIC_FACT",
        evidence_ids=[source["id"]],
        as_of="2026-06-30",
        limitation="historical",
    )
    payload = dict(
        portfolio_id=pid,
        instrument_code="CORE01",
        expected_previous_version=0,
        idempotency_key="case-1",
        as_of="2026-09-23",
        thesis=dict(
            proposed_why_hold="test hypothesis",
            expected_horizon="unknown",
            expected_adversity=["drawdown"],
            invalidation_conditions=["mandate change"],
            add_conditions=["approval"],
            hold_conditions=["review"],
            sell_review_conditions=["review only"],
        ),
        return_driver=dict(
            primary="MIXED",
            evidence_ids=[source["id"]],
            rationale="hypothesis",
            data_date="2026-06-30",
            effective_from="2026-09-23",
        ),
        maper={k: [claim] for k in "MAPER"},
        counter_evidence=[dict(claim, kind="COUNTER_EVIDENCE")],
        unresolved_questions=["original reason unknown"],
        revision_reason="first review",
    )
    return settings, pid, aid, research, payload


def test_versions_concurrency_idempotence_and_no_business_changes(tmp_path):
    settings, pid, _, research, payload = setup_case(tmp_path)
    service = NotebookService(research)
    before = dump(settings.db_path)
    request = CaseDraft.model_validate(payload)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.create(request), range(4)))
    assert len({r["id"] for r in results}) == 1
    altered = deepcopy(payload)
    altered["revision_reason"] = "different"
    with pytest.raises(LedgerError, match="Key already"):
        service.create(CaseDraft.model_validate(altered))
    altered["idempotency_key"] = "case-2"
    with pytest.raises(LedgerError, match="latest"):
        service.create(CaseDraft.model_validate(altered))
    altered["expected_previous_version"] = 1
    second = service.create(CaseDraft.model_validate(altered))
    assert second["version"] == 2
    saved = service.read(pid, "CORE01")
    assert len(saved["versions"]) == 2
    assert saved["versions"][0]["revision_reason"] == "first review"
    assert not saved["latest"]["approved"] and not saved["money_action"]
    after = dump(settings.db_path)
    assert [v for v in before if not v.startswith('INSERT INTO "research_case_versions"')] == [
        v for v in after if not v.startswith('INSERT INTO "research_case_versions"')
    ]


def test_evidence_scope_original_reason_and_future_rejected(tmp_path):
    _, _, _, research, payload = setup_case(tmp_path)
    service = NotebookService(research)
    for bad in [dict(payload, as_of="2099-01-01"), dict(payload, portfolio_id="wrong")]:
        with pytest.raises(LedgerError):
            service.create(CaseDraft.model_validate(bad))
    payload["maper"]["M"][0]["evidence_ids"] = ["missing"]
    with pytest.raises(LedgerError):
        service.create(CaseDraft.model_validate(payload))
    payload["thesis"]["original_buy_reason"] = "invented"
    with pytest.raises(ValueError):
        CaseDraft.model_validate(payload)


def test_journal_append_reviews_and_http_reads_no_writes(tmp_path):
    settings, pid, aid, research, payload = setup_case(tmp_path)
    service = NotebookService(research)
    service.create(CaseDraft.model_validate(payload))
    entry = dict(
        portfolio_id=pid,
        instrument_code="CORE01",
        thesis_version=1,
        idempotency_key="journal-1",
        decision_frame="research",
        known_facts=payload["maper"]["M"],
        unknowns=["reason"],
        expected_scenarios=["adversity"],
        chosen_action="RESEARCH_ONLY",
    )
    first = service.journal(JournalEntry.model_validate(entry))
    assert service.journal(JournalEntry.model_validate(entry))["id"] == first["id"]
    with pytest.raises(ValueError):
        JournalEntry.model_validate(dict(entry, process_quality="PASS"))
    second = service.journal(
        JournalEntry.model_validate(
            dict(
                entry,
                idempotency_key="review-1",
                supersedes_id=first["id"],
                lesson="cannot infer process from return",
            )
        )
    )
    assert second["id"] != first["id"] and second["outcome_quality"] == "UNKNOWN"
    web = TestClient(create_app(settings))
    before = dump(settings.db_path)
    result = web.get(
        "/v1/research-case", params={"portfolio_id": pid, "instrument_code": "CORE01"}
    ).json()["data"]
    assert len(result["journal"]) == 2 and result["latest"]["thesis"]["original_buy_reason"] is None
    risk = web.get("/v1/risk-coverage", params={"portfolio_id": pid, "account_id": aid}).json()[
        "data"
    ]
    assert not risk["money_action"] and all(
        i["proposal"]["proposed_values"] is None for i in risk["items"]
    )
    assert dump(settings.db_path) == before


def test_dynamic_quota_separate_scoped_and_idempotent(tmp_path):
    settings, _, aid, research, _ = setup_case(tmp_path)
    svc = ExecutionService(research)
    time = "2026-09-23T07:00:00Z"
    source = svc.archive(
        SourceArchive(
            instrument_code="CORE01",
            source_name="account screenshot",
            source_ref="attachment:sha256:" + "a" * 64,
            original_sha256="a" * 64,
            source_lineage="account",
            retrieved_at=datetime(2026, 9, 23, 7, tzinfo=UTC),
            published_date=None,
            data_date="2026-09-23",
            excerpt="remaining",
            quality="ACCOUNT_OBSERVATION",
            facts=dict(
                account_id=aid,
                share_class="A",
                channel="test",
                observed_at=time,
                remaining_minor=500,
                quota_scope="CHANNEL_ACCOUNT",
            ),
        )
    )
    request = QuotaCapture(
        account_id=aid,
        instrument_code="CORE01",
        share_class="A",
        channel="test",
        observation=dict(
            business_date="2026-09-23",
            observed_at=time,
            valid_until="2026-09-23T08:00:00Z",
            remaining_minor=500,
            quota_scope="CHANNEL_ACCOUNT",
            source_ids=[source["id"]],
        ),
    )
    before = dump(settings.db_path)
    assert svc.record_quota(request)["id"] == svc.record_quota(request)["id"]
    assert len(svc.list_quotas(aid)) == 1 and not svc.list_constraints(aid)
    with pytest.raises(LedgerError):
        svc.record_quota(request.model_copy(update={"channel": "other"}))
    for field, value in [("remaining_minor", 999), ("quota_scope", "FUND_ACCOUNT_ALL_CHANNELS")]:
        mismatched = request.model_copy(
            update={"observation": request.observation.model_copy(update={field: value})}
        )
        with pytest.raises(LedgerError):
            svc.record_quota(mismatched)
    after = dump(settings.db_path)
    assert [
        v for v in before if not v.startswith('INSERT INTO "execution_quota_observations"')
    ] == [v for v in after if not v.startswith('INSERT INTO "execution_quota_observations"')]
