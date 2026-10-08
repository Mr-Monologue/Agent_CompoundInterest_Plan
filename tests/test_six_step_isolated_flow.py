"""Six steps on one synthetic SQLite DB and one real loopback HTTP server.

Only parsed provider input is replaced; no external platform is contacted. This
is an engineering acceptance fixture, never production evidence or user consent.
"""

from __future__ import annotations

import socket
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta

import pytest
import test_stage_business_flow as journey
from manual_stage_flow import loopback_client
from test_planning import configured_services
from test_research_updates import source_probe, study

from investor_core.api.app import create_app
from investor_core.peer_models import PeerStudy
from investor_core.peer_research import PeerResearchService
from investor_core.research import ResearchService
from investor_core.research_update_build import build
from investor_core.research_updates import ResearchUpdates


def protected_rows(database):
    """All rows except the explicitly permitted independent research audit tables."""
    return [
        line
        for line in journey.facts(database)
        if not line.startswith(
            (
                'INSERT INTO "research_update_runs"',
                'INSERT INTO "peer_research_runs"',
            )
        )
    ]


@pytest.mark.parametrize("outcome", ["executed", "partial", "skip"])
def test_six_steps_one_synthetic_instance(tmp_path, monkeypatch, outcome):
    database = tmp_path / (outcome + ".db")
    services = configured_services(database)
    _, planning, pid, aid = services
    clock = [datetime.now(UTC)]
    research = ResearchService(planning.settings, now=lambda: clock[0])
    monkeypatch.setattr(
        "investor_core.api.app.ResearchUpdates", lambda service: ResearchUpdates(research)
    )
    peers = PeerResearchService(research)
    seed = study()
    seed["limitations"].append("SYNTHETIC SIX STEP FIXTURE; NOT REAL ACCOUNT OR MARKET DATA")
    peers.archive(PeerStudy.model_validate(seed))
    mode = ["normal"]
    probes = []

    def synthetic_build(old, _today, _network_probe):
        # Reuse the production build/calculation pipeline with parsed synthetic
        # provider records. Fetch/HTML parsing is intentionally outside this test.
        probe = source_probe(old, mode[0])

        def recorded_probe(*args):
            probes.append(args[0])
            return probe(*args)

        return build(old, date(2024, 1, 10), recorded_probe)

    monkeypatch.setattr("investor_core.research_updates.build", synthetic_build)
    connect = socket.socket.connect

    def loopback_only(sock, address):
        if isinstance(address, tuple):
            assert address[0] in {"127.0.0.1", "::1"}, "External network forbidden in fixture"
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    with loopback_client(create_app(planning.settings)) as client:
        def call(method, path, **kw):
            return journey.call(client, method, path, **kw)

        context = {"portfolio_id": pid, "account_id": aid}
        # 1. Read-only check, explicit update, stored result and exact-key replay.
        before = journey.facts(database)
        check = call("GET", "/v1/research-update-check")
        assert check["network_performed"] is False and journey.facts(database) == before
        protected = protected_rows(database)
        request = {"scope": "003096", "idempotency_key": "synthetic-six-step-update"}
        updated = call("POST", "/v1/research-updates", json=request)
        assert updated["status"] == "PARTIAL" and probes
        assert len(peers.read("003096")["history"]) == 2
        assert protected_rows(database) == protected
        attempts = len(probes)
        assert call("POST", "/v1/research-updates", json=request)["status"] == "PARTIAL"
        assert len(probes) == attempts
        # 2. Explain actual fixture holdings; separate independent synthetic peer
        # research from CORE01/SAT01. Never pretend those holdings were refreshed.
        before = journey.facts(database)
        summary = call("GET", "/v1/portfolio-research-summary", params=context)
        assert "CORE01" in summary["display_text"] and "SAT01" in summary["display_text"]
        assert journey.facts(database) == before
        # 3. Explicit budget and unknown account constraints remain fail-closed.
        preview = call(
            "GET", "/v1/weekly-plan-preview",
            params={**context, "contribution_amount": "100", "as_of_date": "2026-07-21"},
        )
        assessment = preview["execution_assessment"]
        assert assessment["status"] == "EXECUTION_CONSTRAINT_UNKNOWN"
        assert not assessment["executable_schedule_available"] and not assessment["schedule"]
        assert sum(i["candidate_minor"] for i in assessment["items"]) == 10000
        assert sum(i["unverified_minor"] for i in assessment["items"]) == 10000
        assert journey.facts(database) == before
        # 4/5. Reuse the unchanged journey, same DB, IDs and live HTTP server.
        # All approvals say isolated-user; these are deliberately synthetic facts.
        monkeypatch.setattr(journey, "configured_services", lambda path: services)
        monkeypatch.setattr(journey, "TestClient", lambda app: nullcontext(client))
        journey.test_complete_http_business_journey_and_lost_commit_response(tmp_path, outcome)
        # 6. A source failure is observable and retains the previous success,
        # peer version and all completed financial/report/plan facts.
        cooldown = client.post(
            "/v1/research-updates",
            json={"scope": "003096", "idempotency_key": "synthetic-six-step-failure"},
        )
        assert cooldown.status_code == 429
        assert cooldown.json()["error"]["code"] == "RESEARCH_UPDATE_COOLDOWN"
        clock[0] += timedelta(seconds=31)
        mode[0] = "failure"
        protected = protected_rows(database)
        failure = call(
            "POST", "/v1/research-updates",
            json={"scope": "003096", "idempotency_key": "synthetic-six-step-failure"},
        )
        assert failure["status"] == "FAILED" and failure["last_successful_result"]
        assert "没有变化" not in failure["display_text"]
        assert len(peers.read("003096")["history"]) == 2
        assert protected_rows(database) == protected
        before = journey.facts(database)
        latest = call("GET", "/v1/research-updates/latest")
        assert latest["status"] == "FAILED" and latest["last_successful_result"]
        assert journey.facts(database) == before
