"""Archived synthetic inputs, actual isolated HTTP, no market fetch or production writes."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_daily_client import dump
from test_planning import configured_services
from test_r11_calculators import candidates, macro

from investor_core.api.app import create_app
from investor_core.execution import ExecutionService, SourceArchive
from investor_core.ledger import LedgerError
from investor_core.r11_candidates import CandidateInput
from investor_core.r11_macro import MacroInput
from investor_core.r11_provenance import leaves
from investor_core.r11_service import R11Request, R11Service
from investor_core.research import ResearchService
from investor_core.shadow_models import ReviewConfirmation, ReviewRequest


@pytest.fixture
def isolated(tmp_path):
    db = tmp_path / "r11-synthetic.db"
    _, planning, _, _ = configured_services(db)
    clock = [datetime(2026, 10, 31, 4, tzinfo=UTC)]
    research = ResearchService(planning.settings, now=lambda: clock[0])
    return db, planning.settings, R11Service(research), clock


def archive_bundle(service, data, key="bundle"):
    normalized = (
        MacroInput.model_validate(data) if "pb" in data else CandidateInput.model_validate(data)
    ).model_dump(mode="json")
    if "products" in normalized:
        for product in normalized["products"]:
            product["nav"]["points"] = product["nav"]["points"][-300:]
            product["calendar"]["dates"] = product["calendar"]["dates"][-300:]
            product["calendar"]["covered_from"] = product["calendar"]["dates"][0]
        if normalized["benchmark"]:
            normalized["benchmark"]["points"] = normalized["benchmark"]["points"][-300:]
            normalized["benchmark_calendar"]["dates"] = normalized["benchmark_calendar"]["dates"][
                -300:
            ]
            normalized["benchmark_calendar"]["covered_from"] = normalized["benchmark_calendar"][
                "dates"
            ][0]
    data.clear()
    data.update(normalized)
    src = data["context"]["sources"]["official"]
    archive = ExecutionService(service.research)
    values = leaves(data)
    document = {"publication": src["published_at"], "values": list(values.values())}
    if "members" in data:
        document["constituent_codes"] = [member["code"] for member in data["members"]]
    original = json.dumps(document, separators=(",", ":"))
    src["document_hash"] = hashlib.sha256(original.encode()).hexdigest()
    args = dict(
        instrument_code="CORE01",
        source_name="SYNTHETIC R11 ONLY",
        source_ref=src["url"],
        source_lineage=src["lineage"],
        retrieved_at=src["first_retrieved_at"],
        published_date=src["published_at"][:10],
        data_date="2020-01-01",
        excerpt=original,
        original_sha256=src["document_hash"],
        quality=src["quality"],
        facts={
            "r11_original_format": "JSON_UTF8",
            "r11_publication_pointer": "/publication",
            "publication_timezone": src["publication_timezone"],
            "publication_precision": src["publication_precision"],
            "dataset_kind": data["context"]["dataset_kind"],
            "r11_source_binding": {k: src[k] for k in ["published_at", "first_retrieved_at"]},
        },
    )
    if "members" in data:
        args["facts"]["r11_constituent_collection"] = {
            "pointer": "/constituent_codes",
            "code_pointer": "",
        }
    source = archive.archive(SourceArchive.model_validate(args))
    src["archive_id"] = source["id"]
    args.update(
        source_ref="https://example.test/" + key,
        facts={
            "r11_input": data,
            "r11_bindings": {
                path: {"source": "official", "pointer": f"/values/{i}"}
                for i, path in enumerate(values)
            },
        },
    )
    if "members" in data:
        args["facts"]["r11_bindings"]["/members"] = {
            "source": "official",
            "pointer": "/constituent_codes",
            "code_pointer": "",
        }
    return archive.archive(SourceArchive.model_validate(args))["id"]


def business_rows(db):
    return [
        line
        for line in dump(db)
        if not line.startswith(('INSERT INTO "shadow_models"', 'INSERT INTO "shadow_records"'))
    ]


@pytest.mark.parametrize("method", ["C", "MEDICAL", "A500"])
def test_archived_run_replay_and_exact_request_idempotence(isolated, method):
    db, _, service, _ = isolated
    data = macro() if method == "C" else candidates(method)
    evidence = archive_bundle(service, data)
    before = business_rows(db)
    request = R11Request(method=method, bundle_evidence_id=evidence, idempotency_key=method)
    result = service.observe(request)
    assert result["output"]["status"] == "CALCULATED_SHADOW"
    assert service.observe(request) == result
    assert service.replay(method, result["id"])["result"] == "PASS"
    assert business_rows(db) == before
    assert not service.shadow.gate(result["model_id"], "ACTIVE")["eligible"]
    assert not service.shadow.gate(result["model_id"], "ADVISORY")["eligible"]


def test_revision_explicit_and_old_input_replay_retained(isolated):
    _, _, service, _ = isolated
    evidence = archive_bundle(service, macro())
    first = service.observe(
        R11Request(method="C", bundle_evidence_id=evidence, idempotency_key="first")
    )
    data = macro()
    data["pmi"]["points"][-1]["value"] = "49"
    revised_evidence = archive_bundle(service, data, key="revision")
    req = R11Request(method="C", bundle_evidence_id=revised_evidence, idempotency_key="second")
    with pytest.raises(LedgerError) as conflict:
        service.observe(req)
    assert conflict.value.code == "R11_REVISION_REQUIRED"
    second = service.observe(req.model_copy(update={"supersedes": first["id"]}))
    assert second["supersedes"] == first["id"] and second["output"] != first["output"]
    assert len(service.runs("C")) == 2
    assert service.replay("C", first["id"])["result"] == "PASS"


def test_pause_blocks_new_observation_but_returns_exact_old_request(isolated):
    db, _, service, _ = isolated
    evidence = archive_bundle(service, candidates())
    req = R11Request(method="MEDICAL", bundle_evidence_id=evidence, idempotency_key="first")
    result = service.observe(req)
    draft = service.shadow.create_review(
        ReviewRequest(
            model_id=result["model_id"],
            target="OFF",
            reason="synthetic pause",
            idempotency_key="off",
        )
    )
    service.shadow.confirm(
        result["model_id"],
        draft["id"],
        ReviewConfirmation(
            confirmation_token=draft["confirmation_token"], confirmed_by="isolated-test"
        ),
    )
    before = dump(db)
    assert service.observe(req) == result
    assert dump(db) == before
    with pytest.raises(LedgerError) as paused:
        service.observe(req.model_copy(update={"idempotency_key": "another"}))
    assert paused.value.code == "SHADOW_PAUSED"


def test_source_binding_cannot_change_retrieval_or_quality(isolated):
    _, _, service, _ = isolated
    data = macro()
    evidence = archive_bundle(service, data)
    with service.research._connect() as c:
        import json

        row = c.execute(
            "SELECT facts_json FROM market_research_evidence WHERE id=?", (evidence,)
        ).fetchone()
        facts = json.loads(row[0])
        facts["facts"]["r11_input"]["context"]["sources"]["official"]["first_retrieved_at"] = (
            "2019-01-01T00:00:00Z"
        )
        c.execute(
            "UPDATE market_research_evidence SET facts_json=? WHERE id=?",
            (json.dumps(facts), evidence),
        )
    with pytest.raises(LedgerError) as invalid:
        service.observe(
            R11Request(method="C", bundle_evidence_id=evidence, idempotency_key="invalid")
        )
    assert invalid.value.code == "R11_SOURCE_BINDING_MISMATCH"


def test_real_forward_cannot_backfill_a_late_bundle(isolated):
    _, _, service, _ = isolated
    data = macro()
    data["context"]["dataset_kind"] = "REAL"
    evidence = archive_bundle(service, data)
    with pytest.raises(LedgerError) as late:
        service.observe(R11Request(method="C", bundle_evidence_id=evidence, idempotency_key="late"))
    assert late.value.code == "R11_SOURCE_CAPTURE_LATE"


def test_http_read_replay_and_unknown_endpoint_have_no_business_effects(isolated, monkeypatch):
    db, settings, service, _ = isolated
    monkeypatch.setattr("investor_core.api.shadow.R11Service", lambda research: service)
    evidence = archive_bundle(service, candidates("A500"))
    before = business_rows(db)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/r11/research-runs",
            json=dict(method="A500", bundle_evidence_id=evidence, idempotency_key="http"),
        )
        assert response.status_code == 200, response.text
        record = response.json()["data"]
        assert record["output"]["status"] == "CALCULATED_SHADOW"
        replay = client.get(f"/v1/r11/research-runs/A500/{record['id']}/replay")
        assert replay.json()["data"]["result"] == "PASS"
        assert len(client.get("/v1/r11/research-runs/A500").json()["data"]["items"]) == 1
        assert client.get("/v1/r11/research-runs/A500/not-found/replay").status_code == 400
    assert business_rows(db) == before
