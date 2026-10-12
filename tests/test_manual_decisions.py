"""Decision context, scope, replay and read-only contract; all synthetic isolated data."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_daily_client import dump
from test_notebook import setup_case
from test_stage_business_flow import facts

from investor_core.api.app import create_app
from investor_core.daily_client import DailyClient
from investor_core.ledger import LedgerError
from investor_core.manual_decisions import ManualDecisionDraft, ManualDecisionService
from investor_core.notebook import CaseDraft, JournalEntry, NotebookService
from investor_core.planning import PlanningService
from investor_core.thesis import ThesisConfirmation
from investor_core.weekly_reports import WeeklyReportService


def setup(tmp_path):
    settings, pid, aid, research, payload = setup_case(tmp_path)
    notebook = NotebookService(research)
    case = notebook.create(CaseDraft.model_validate(payload))
    planning = PlanningService(settings)
    plan = planning.list(portfolio_id=pid)[0]
    service = ManualDecisionService(notebook, planning, WeeklyReportService(settings))
    request = ManualDecisionDraft(
        portfolio_id=pid,
        plan_id=plan["id"],
        plan_revision=1,
        intent="DEFER",
        reason_status="UNKNOWN",
        system_explanation="test system explanation",
        research_references=[{"kind": "CASE", "record_id": case["id"]}],
        idempotency_key="decision",
    )
    return settings, pid, aid, notebook, service, request, payload


def confirm(service, draft):
    return service.confirm(
        draft["id"],
        ThesisConfirmation(
            confirmation_token=draft["confirmation_token"], confirmed_by="isolated-user"
        ),
    )


def test_frozen_evidence_replay_reviews_and_readonly(tmp_path):
    settings, pid, aid, notebook, service, request, payload = setup(tmp_path)
    draft = service.create(request)
    original = deepcopy(draft["original_context"])
    assert "confirmation_digest" not in draft
    confirmed = confirm(service, draft)
    assert "confirmation_digest" not in confirmed
    assert service.create(request)["idempotent_replay"]
    assert confirm(service, draft)["idempotent_replay"]
    altered = deepcopy(payload)
    altered.update(
        expected_previous_version=1, idempotency_key="case-2", revision_reason="new facts"
    )
    altered["thesis"]["proposed_why_hold"] = "changed hypothesis"
    notebook.create(CaseDraft.model_validate(altered))
    review = dict(
        portfolio_id=pid,
        instrument_code="CORE01",
        thesis_version=1,
        manual_decision_id=draft["id"],
        idempotency_key="review",
        decision_frame="post-review",
        known_facts=[],
        unknowns=["return unknown"],
        expected_scenarios=[],
        chosen_action="RESEARCH_ONLY",
    )
    notebook.journal(JournalEntry.model_validate(review))
    before = facts(settings.db_path)
    current = service.read(draft["id"])
    assert current["decision"]["original_context"] == original
    assert current["decision"]["user_quote"] is None
    assert len(current["reviews"]) == 1
    assert facts(settings.db_path) == before
    with TestClient(create_app(settings)) as http:
        client = DailyClient(client=http, portfolio_id=pid, account_id=aid)
        result = client.get("/v1/manual-decisions", **client.scope())
        assert len(result["items"]) == 1
        assert result["items"][0]["decision"]["original_context"] == original
        assert facts(settings.db_path) == before
    review.update(thesis_version=2, idempotency_key="bad-version")
    with pytest.raises(LedgerError, match="original cited"):
        notebook.journal(JournalEntry.model_validate(review))


def test_concurrency_and_conflicting_replay(tmp_path):
    _, _, _, _, service, request, _ = setup(tmp_path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: service.create(request), range(3)))
    assert len({x["id"] for x in results}) == 1
    draft = next(x for x in results if "confirmation_token" in x)
    with ThreadPoolExecutor(max_workers=3) as pool:
        confirms = list(pool.map(lambda _: confirm(service, draft), range(3)))
    assert len({x["id"] for x in confirms}) == 1
    with pytest.raises(LedgerError, match="content differs"):
        service.create(request.model_copy(update={"system_explanation": "changed"}))


def test_revision_change_keeps_original_and_blocks_old_confirmation(tmp_path):
    settings, _, _, _, service, request, _ = setup(tmp_path)
    saved = service.create(request)
    confirm(service, saved)
    pending = service.create(request.model_copy(update={"idempotency_key": "pending"}))
    # Exact isolated fixture mutation of revision pointer and revision row.
    with sqlite3.connect(settings.db_path) as c:
        c.execute("UPDATE plan_revisions SET revision=2 WHERE plan_id=?", (request.plan_id,))
        c.execute("UPDATE investment_plans SET current_revision=2 WHERE id=?", (request.plan_id,))
    result = service.read(saved["id"])
    assert result["applicability"] == "REVISION_CHANGED_REVIEW_REQUIRED"
    assert result["decision"]["original_context"] == saved["original_context"]
    with pytest.raises(LedgerError, match="Plan changed"):
        confirm(service, pending)
    assert confirm(service, saved)["idempotent_replay"]


def test_scope_missing_original_expiry_and_reason(tmp_path):
    settings, _, _, _, service, request, _ = setup(tmp_path)
    with pytest.raises(ValidationError):
        ManualDecisionDraft.model_validate(
            {**request.model_dump(), "user_quote": "system cannot impersonate"}
        )
    with pytest.raises(LedgerError, match="portfolio"):
        service.create(request.model_copy(update={"portfolio_id": "other"}))
    with pytest.raises(LedgerError, match="unavailable"):
        service.create(
            ManualDecisionDraft.model_validate(
                {
                    **request.model_dump(),
                    "research_references": [{"kind": "CASE", "record_id": "missing"}],
                }
            )
        )
    draft = service.create(request)
    old_now = service.research._now
    service.research._now = lambda: old_now() + timedelta(days=2)
    with pytest.raises(LedgerError, match="fresh decision"):
        confirm(service, draft)
    # Readback still works, but does not expose secret digest.
    assert "confirmation_digest" not in service.read(draft["id"])["decision"]
    assert dump(settings.db_path)


def test_actual_cli_http_is_readonly(tmp_path):
    import json
    import subprocess
    import sys

    from manual_stage_flow import loopback_client

    settings, pid, aid, _, service, request, _ = setup(tmp_path)
    confirm(service, service.create(request))
    with loopback_client(create_app(settings)) as http:
        command = [
            sys.executable,
            "-m",
            "investor_core.daily_client",
            "--core-url",
            str(http.base_url).rstrip("/"),
            "--portfolio",
            pid,
            "--account",
            aid,
        ]
        before = facts(settings.db_path)
        for name in ("decision-review", "查看人工决定复盘"):
            run = subprocess.run(
                [*command, "--json", name],
                capture_output=True,
                encoding="utf-8",
                timeout=30,
                check=True,
            )
            data = json.loads(run.stdout)
            assert data["items"][0]["decision"]["user_quote"] is None
            assert "用户原话: 未知" in data["display_text"]
            assert data["items"][0]["decision"]["original_context"]["research"]
        assert facts(settings.db_path) == before


def test_additive_migration_preserves_every_prior_table(tmp_path):
    from conftest import migrate_database
    from test_migrations import migrate_to

    path = tmp_path / "migration.db"
    migrate_to(path, "0045_shadow_models")
    from investor_core.config import Environment, Settings
    from investor_core.ledger import LedgerService

    ledger = LedgerService(Settings(environment=Environment.TEST, db_path=path))
    ledger.create_portfolio(name="preserve me")

    def snapshot():
        with sqlite3.connect(path) as c:
            tables = [
                r[0]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if r[0] not in {"alembic_version", "manual_investment_decisions"}
            ]
            return {t: c.execute('SELECT * FROM "' + t + '"').fetchall() for t in tables}

    before = snapshot()
    migrate_database(path)
    migrate_database(path)
    assert snapshot() == before
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT count(*) FROM manual_investment_decisions").fetchone() == (0,)


def test_daily_confirmation_gate_and_lost_response(tmp_path):
    import httpx

    from investor_core.daily_client import AssistantError

    settings, _, _, _, _, request, _ = setup(tmp_path)
    with TestClient(create_app(settings)) as web:
        client = DailyClient(client=web, journal=tmp_path / "client.db")
        created = client.workflow("manual-decision-draft", request.model_dump(mode="json"))["data"]
        payload = dict(
            confirmation_token=created["confirmation_token"], confirmed_by="isolated-user"
        )
        with pytest.raises(AssistantError, match="确认"):
            client.workflow("manual-decision-confirm", payload, subject=created["id"])
        sent = []

        def lose_after_commit(req):
            sent.append(req)
            response = web.post(
                req.url.path, content=req.content, headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 200
            raise httpx.ReadTimeout("lost")

        wire = httpx.Client(
            base_url="http://127.0.0.1", transport=httpx.MockTransport(lose_after_commit)
        )
        losing = DailyClient(client=wire, journal=tmp_path / "client.db")
        with pytest.raises(AssistantError, match="未确认"):
            losing.workflow(
                "manual-decision-confirm", payload, subject=created["id"], confirmation="确认"
            )
        assert client.inspect("manual-decision", created["id"])["decision"]["status"] == "CONFIRMED"
        with pytest.raises(AssistantError, match="已发送"):
            losing.workflow(
                "manual-decision-confirm", payload, subject=created["id"], confirmation="确认"
            )
        assert len(sent) == 1
        assert created["confirmation_token"].encode() not in (tmp_path / "client.db").read_bytes()


def test_unreferenced_review_cannot_invent_original_research(tmp_path):
    _, pid, _, notebook, service, request, _ = setup(tmp_path)
    draft = service.create(request.model_copy(update={"research_references": []}))
    confirm(service, draft)
    review = dict(
        portfolio_id=pid,
        instrument_code="CORE01",
        thesis_version=None,
        manual_decision_id=draft["id"],
        idempotency_key="unreferenced-review",
        decision_frame="review",
        known_facts=[],
        unknowns=["original reason unknown"],
        expected_scenarios=[],
        chosen_action="RESEARCH_ONLY",
    )
    recorded = notebook.journal(JournalEntry.model_validate(review))
    assert service.read(draft["id"])["reviews"][0]["thesis_version"] is None
    with pytest.raises(LedgerError, match="original cited"):
        notebook.journal(
            JournalEntry.model_validate({**review, "idempotency_key": "bad", "thesis_version": 1})
        )
    with pytest.raises(LedgerError, match="cannot change"):
        notebook.journal(
            JournalEntry.model_validate(
                {
                    **review,
                    "idempotency_key": "detach",
                    "manual_decision_id": None,
                    "thesis_version": 1,
                    "supersedes_id": recorded["id"],
                }
            )
        )


def test_benchmark_and_peer_reference_keep_exact_source_format(tmp_path):
    from test_benchmarks import input_for, setup_mapping
    from test_peer_research import payload as peer_payload

    from investor_core.benchmarks import DiagnosticInput, MappingDraft
    from investor_core.peer_models import PeerStudy
    from investor_core.peer_research import PeerResearchService

    settings, pid, _, research, eid, benchmark, mapping_payload = setup_mapping(tmp_path)
    mapping = benchmark.create(MappingDraft.model_validate(mapping_payload))
    run = benchmark.run(DiagnosticInput.model_validate(input_for(mapping, eid)))
    planning = PlanningService(settings)
    service = ManualDecisionService(
        NotebookService(research), planning, WeeklyReportService(settings)
    )
    plan = planning.list(portfolio_id=pid)[0]
    args = dict(
        portfolio_id=pid,
        plan_id=plan["id"],
        plan_revision=1,
        intent="DEFER",
        reason_status="UNKNOWN",
        idempotency_key="bench-decision",
        research_references=[{"kind": "BENCHMARK", "record_id": run["id"]}],
    )
    draft = service.create(ManualDecisionDraft.model_validate(args))
    ref = draft["original_context"]["research"][0]
    assert ref["data_cutoff"] == "2026-09-22"
    assert ref["evidence_cutoffs"][0]["facts_hash"]
    assert ref["content"]["warnings"] == run["warnings"]
    # Rename exact synthetic anchor consistently, not a real product or share splice.
    raw = peer_payload()
    raw["anchor_code"] = "CORE01"
    raw["products"][0]["code"] = "CORE01"
    raw["products"][0]["nav"]["code"] = "CORE01"
    raw["sources"]["R"]["instrument_code"] = "CORE01"
    peer = PeerResearchService(research).archive(PeerStudy.model_validate(raw))
    args.update(
        idempotency_key="peer-decision",
        research_references=[{"kind": "PEER", "record_id": peer["id"]}],
    )
    ref = service.create(ManualDecisionDraft.model_validate(args))["original_context"]["research"][
        0
    ]
    assert ref["data_cutoff"] == "2024-01-04"
    assert ref["knowledge_date"] == "2024-01-05"
    assert ref["content"]["version"] == 1
    assert {e["id"] for e in ref["evidence"]} == {"R", "P"}
    assert all(e["facts_hash"] for e in ref["evidence_cutoffs"])


def test_same_revision_content_drift_and_invalid_confirmation(tmp_path):
    settings, _, _, _, service, request, _ = setup(tmp_path)
    draft = service.create(request)
    with pytest.raises(LedgerError, match="Invalid content"):
        service.confirm(
            draft["id"], ThesisConfirmation(confirmation_token="wrong", confirmed_by="test")
        )
    with sqlite3.connect(settings.db_path) as c:
        c.execute(
            "UPDATE plan_revisions SET input_hash='changed' WHERE plan_id=?", (request.plan_id,)
        )
    with pytest.raises(LedgerError, match="Plan changed"):
        confirm(service, draft)
    assert service.read(draft["id"])["decision"]["original_context"] == draft["original_context"]


def test_future_and_missing_evidence_are_not_silently_omitted(tmp_path):
    settings, _, _, _, service, request, _ = setup(tmp_path)
    before = facts(settings.db_path)
    now = service.research._now
    service.research._now = lambda: now() - timedelta(days=1)
    with pytest.raises(LedgerError, match="Future research"):
        service.create(request)
    assert facts(settings.db_path) == before
    service.research._now = now
    with sqlite3.connect(settings.db_path) as c:
        # Isolated corruption fixture: archived version points at a nonexistent original.
        row = c.execute("SELECT id,payload_json FROM research_case_versions").fetchone()
        import json

        value = json.loads(row[1])
        value["evidence_ids"].append("missing-original")
        c.execute(
            "UPDATE research_case_versions SET payload_json=? WHERE id=?",
            (json.dumps(value), row[0]),
        )
    with pytest.raises(LedgerError, match="original is missing"):
        service.create(request)
