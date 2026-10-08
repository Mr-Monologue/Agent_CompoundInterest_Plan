"""Synthetic model governance; never a model activation or production dataset."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_daily_client import dump
from test_planning import configured_services

from investor_core.api.app import create_app
from investor_core.execution import ExecutionService, SourceArchive
from investor_core.ledger import LedgerError
from investor_core.research import ResearchService
from investor_core.shadow_models import (
    C_INPUTS,
    D_INPUTS,
    ModelDefinition,
    ReviewConfirmation,
    ReviewRequest,
    ShadowInput,
    ShadowObservation,
    ShadowService,
)


@pytest.fixture
def rig(tmp_path):
    db = tmp_path / "synthetic.db"
    _ledger, planning, pid, aid = configured_services(db)
    clock = [datetime(2026, 9, 25, tzinfo=UTC)]
    research = ResearchService(planning.settings, now=lambda: clock[0])
    return db, planning.settings, ShadowService(research), clock, pid, aid


def definition(kind="MACRO_REGIME", version="1.0.0", **kw):
    slots = C_INPUTS if kind == "MACRO_REGIME" else D_INPUTS
    kw.setdefault(
        "input_contract",
        {slot: [{"name": slot + "_fixture", "value_type": "NUMBER"}] for slot in slots},
    )
    return ModelDefinition(
        model_key="synthetic-" + kind,
        version=version,
        model_type=kind,
        scope="REGION:TEST",
        rationale="Synthetic only",
        **kw,
    )


def source(service, *, scope="REGION:TEST", subject="CORE01", **kw):
    values = dict(
        instrument_code="CORE01",
        source_name="Synthetic fixture",
        source_ref="https://example.test/shadow/" + str(kw),
        source_lineage="TEST_ISSUER",
        retrieved_at="2026-09-23T10:00:00Z",
        published_date="2026-09-23",
        data_date="2026-09-22",
        excerpt="SYNTHETIC ONLY",
        quality="OFFICIAL",
        facts={
            "publication_timezone": "Asia/Shanghai",
            "shadow_scope": scope,
            "shadow_subject": subject,
            "values": {
                "fee": "1",
                "product_type": "INDEX",
                **{slot + "_fixture": "1" for slot in C_INPUTS | D_INPUTS},
            },
        },
    )
    values.update(kw)
    return ExecutionService(service.research).archive(SourceArchive.model_validate(values))["id"]


def observation(model, refs=None, key="observe", subject="CORE01"):
    return ShadowObservation(
        model_id=model["id"],
        scope="REGION:TEST",
        as_of="2026-09-24T12:00:00Z",
        subjects=[subject],
        inputs=refs or [],
        idempotency_key=key,
    )


def complete(service, model, kind="MACRO_REGIME", **kw):
    eid = source(service, **kw)
    return observation(
        model,
        [
            ShadowInput(slot=s, evidence_id=eid, subject="CORE01")
            for s in sorted(C_INPUTS if kind == "MACRO_REGIME" else D_INPUTS)
        ],
    )


def original_rows(db):
    return [
        line
        for line in dump(db)
        if not line.startswith(('INSERT INTO "shadow_models"', 'INSERT INTO "shadow_records"'))
    ]


def test_immutable_versions_and_concurrent_registration(rig):
    _, _, service, _, _, _ = rig
    with ThreadPoolExecutor(max_workers=2) as pool:
        models = list(pool.map(service.register, [definition(), definition()]))
    assert models[0] == models[1]
    with pytest.raises(LedgerError, match="new model version"):
        service.register(definition().model_copy(update={"rationale": "changed"}))
    second = service.register(definition(version="1.1.0"))
    assert second["id"] != models[0]["id"]
    assert service.read(models[0]["id"])["definition"]["version"] == "1.0.0"


def test_missing_input_is_not_zero_or_probability_and_cannot_promote(rig):
    db, _, service, _, _, _ = rig
    before = original_rows(db)
    model = service.register(definition())
    record = service.observe(observation(model))
    assert record["output"]["status"] == "INSUFFICIENT_DATA"
    assert len(record["output"]["rows"][0]["missing"]) == 5
    assert record["output"]["probabilities"] is None
    validation = service.validate(model["id"], record["id"])
    assert validation["checks"]["deterministic_replay"]
    assert not validation["checks"]["input_coverage"]
    for target in ("ADVISORY", "ACTIVE"):
        assert not service.gate(model["id"], target)["eligible"]
        with pytest.raises(LedgerError, match="Promotion evidence"):
            service.create_review(
                ReviewRequest(
                    model_id=model["id"], target=target, reason="synthetic", idempotency_key=target
                )
            )
    assert original_rows(db) == before


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"scope": "SECTOR:OTHER"}, "EVIDENCE_SCOPE_MISMATCH"),
        ({"subject": "OTHER"}, "EVIDENCE_SCOPE_MISMATCH"),
        ({"quality": "UNVERIFIED"}, "QUALITY_UNVERIFIED"),
        ({"published_date": None}, "TIME_PROVENANCE_MISSING"),
        ({"retrieved_at": "2026-09-25T00:00:00Z"}, "NOT_KNOWN_AS_OF"),
        ({"published_date": "2026-09-24"}, "NOT_KNOWN_AS_OF"),
    ],
)
def test_quality_scope_and_knowledge_time_fail_closed(rig, kw, reason):
    _, _, service, _, _, _ = rig
    model = service.register(definition())
    record = service.observe(complete(service, model, **kw))
    assert record["output"]["status"] == "INSUFFICIENT_DATA"
    assert all(reason in s for s in record["output"]["rows"][0]["missing"])


def test_snapshot_replay_and_no_business_changes(rig):
    db, _, service, _, _, _ = rig
    model = service.register(definition())
    request = complete(service, model)
    before = original_rows(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(service.observe, [request, request]))
    assert records[0] == records[1]
    assert records[0]["output"]["status"] == "EVIDENCE_COMPLETE"
    assert records[0]["output"]["dominant_season"] == "UNKNOWN"
    assert service.validate(model["id"], records[0]["id"])["checks"]["input_coverage"]
    assert original_rows(db) == before
    # Later source evidence cannot rewrite the previously stored input/observation.
    source(service, excerpt="SYNTHETIC revision", data_date="2026-09-23")
    assert service.observe(request) == records[0]
    assert service.validate(model["id"], records[0]["id"])["checks"]["deterministic_replay"]
    with pytest.raises(LedgerError, match="different immutable"):
        service.observe(request.model_copy(update={"as_of": request.as_of + timedelta(hours=1)}))


def test_d_filters_are_sourced_and_never_ranking_or_eligibility(rig):
    _, _, service, _, _, _ = rig
    model = service.register(
        definition(
            "SATELLITE_RANKING",
            rules=[{"slot": "cost", "field": "fee", "operator": "MAX", "value": "0.5"}],
        )
    )
    output = service.observe(complete(service, model, "SATELLITE_RANKING"))["output"]
    assert output["rows"][0]["filter_result"] == "EXCLUDED"
    assert output["rows"][0]["exclusions"] == ["cost.fee:RULE_NOT_MET"]
    assert output["ranking"] is None and output["money_action"] is False
    assert "SCOPE_NOT_APPROVED" in output["limitations"]
    missing = service.observe(observation(model, key="missing"))["output"]
    assert missing["rows"][0]["filter_result"] == "UNKNOWN"


def test_review_confirmation_pause_resume_and_replay(rig):
    db, _, service, _, _, _ = rig
    model = service.register(definition())
    baseline = original_rows(db)
    args = ReviewRequest(
        model_id=model["id"], target="OFF", reason="Synthetic pause", idempotency_key="pause"
    )
    draft = service.create_review(args)
    assert "confirmation_token" not in service.create_review(args)
    assert "confirmation_digest" not in str(service.read(model["id"]))
    assert "confirmation_token" not in str(service.read(model["id"]))
    with pytest.raises(LedgerError, match="Exact confirmation"):
        service.confirm(
            model["id"],
            draft["id"],
            ReviewConfirmation(confirmation_token="wrong", confirmed_by="synthetic-user"),
        )
    approval = ReviewConfirmation(
        confirmation_token=draft["confirmation_token"], confirmed_by="synthetic-user"
    )
    first = service.confirm(model["id"], draft["id"], approval)
    assert service.confirm(model["id"], draft["id"], approval) == first
    assert service.read(model["id"])["mode"] == "OFF"
    with pytest.raises(LedgerError, match="paused"):
        service.observe(observation(model))
    resume = service.create_review(
        args.model_copy(update={"target": "SHADOW", "idempotency_key": "resume"})
    )
    service.confirm(
        model["id"],
        resume["id"],
        ReviewConfirmation(
            confirmation_token=resume["confirmation_token"], confirmed_by="synthetic-user"
        ),
    )
    assert service.read(model["id"])["mode"] == "SHADOW"
    assert original_rows(db) == baseline


def test_expiry_drift_and_future_scope_rejection(rig):
    _, _, service, clock, _, _ = rig
    model = service.register(definition())
    args = ReviewRequest(
        model_id=model["id"], target="OFF", reason="Synthetic", idempotency_key="expire"
    )
    draft = service.create_review(args)
    clock[0] += timedelta(hours=1)
    with pytest.raises(LedgerError, match="Preview a new"):
        service.confirm(
            model["id"],
            draft["id"],
            ReviewConfirmation(
                confirmation_token=draft["confirmation_token"], confirmed_by="synthetic-user"
            ),
        )
    draft = service.create_review(args.model_copy(update={"idempotency_key": "drift"}))
    service.observe(observation(model))
    with pytest.raises(LedgerError, match="changed"):
        service.confirm(
            model["id"],
            draft["id"],
            ReviewConfirmation(
                confirmation_token=draft["confirmation_token"], confirmed_by="synthetic-user"
            ),
        )
    for field, value in (("scope", "SECTOR:OTHER"), ("as_of", clock[0] + timedelta(days=1))):
        with pytest.raises(LedgerError):
            service.observe(observation(model).model_copy(update={field: value}))


def test_http_readonly_and_no_self_reported_validation(rig):
    db, settings, _, _, _, _ = rig
    with TestClient(create_app(settings)) as web:
        model = web.post("/v1/shadow-models", json=definition().model_dump(mode="json")).json()[
            "data"
        ]
        request = observation(model).model_dump(mode="json")
        result = web.post("/v1/shadow-observations", json=request)
        assert result.status_code == 200
        assert result.json()["meta"]["data_quality"] == "WARNING"
        bad = dict(request, probabilities={"SPRING": 1})
        assert web.post("/v1/shadow-observations", json=bad).status_code == 422
        before = dump(db)
        assert web.get("/v1/shadow-models/" + model["id"]).status_code == 200
        assert not web.get("/v1/shadow-models/" + model["id"] + "/promotion-check").json()["data"][
            "eligible"
        ]
        assert dump(db) == before


def test_schema_rejects_implicit_rules_and_nonfinite_thresholds():
    for threshold in ("NaN", "Infinity", "not-a-number"):
        with pytest.raises(ValidationError):
            definition(
                "SATELLITE_RANKING",
                rules=[{"slot": "cost", "field": "fee", "operator": "MAX", "value": threshold}],
            )


def test_additive_migration_preserves_populated_business_database(rig):
    from conftest import migrate_database
    from test_migrations import downgrade_to

    db, _, service, _, _, _ = rig
    downgrade_to(db, "0044_research_updates")
    before = [s for s in dump(db) if "alembic_version" not in s]
    migrate_database(db)
    migrate_database(db)
    after = [
        s
        for s in dump(db)
        if not any(
            name in s
            for name in (
                "alembic_version",
                "shadow_models",
                "shadow_records",
                "uq_shadow_model_version",
            )
        )
    ]
    assert after == before
    assert service.register(definition())["status"] == "DRAFT"


def test_real_http_shadow_evidence_leaves_budget_and_financial_rows_unchanged(rig):
    from manual_stage_flow import loopback_client

    db, settings, _, _, pid, aid = rig
    with loopback_client(create_app(settings)) as client:

        def get(path, **params):
            response = client.get(path, params=params)
            assert response.status_code == 200
            return response.json()["data"]

        params = dict(
            portfolio_id=pid, account_id=aid, contribution_amount="100", as_of_date="2026-07-21"
        )
        before = original_rows(db)
        budget = get("/v1/weekly-plan-preview", **params)["plan"]
        model = client.post("/v1/shadow-models", json=definition().model_dump(mode="json")).json()[
            "data"
        ]
        created = client.post(
            "/v1/shadow-observations", json=observation(model).model_dump(mode="json")
        )
        assert created.status_code == 200
        record = created.json()["data"]
        validation = client.post(
            f"/v1/shadow-models/{model['id']}/observations/{record['id']}/validate"
        )
        assert validation.status_code == 200
        gate = get(f"/v1/shadow-models/{model['id']}/promotion-check", target="ACTIVE")
        assert "NO_DIRECT_SHADOW_TO_ACTIVE" in gate["blockers"]
        assert get("/v1/weekly-plan-preview", **params)["plan"] == budget
        assert original_rows(db) == before


def test_review_source_identity_and_malformed_time_are_limited(rig):
    _, _, service, _, _, _ = rig
    model = service.register(definition("SATELLITE_RANKING"))
    request = complete(service, model, "SATELLITE_RANKING", instrument_code="SAT01")
    output = service.observe(request)["output"]
    assert any("SOURCE_IDENTITY_MISMATCH" in x for x in output["rows"][0]["missing"])
    # Generic evidence API permits arbitrary facts; malformed provenance must not
    # crash shadow evaluation or be treated as verified timing.
    for index, timestamp in enumerate((123, "2026-09-23T10:00:00", None)):
        eid = service.research.record_evidence(
            instrument_code="CORE01",
            evidence_date=date(2026, 9, 22),
            evidence_type="OTHER",
            source_name="Synthetic malformed",
            source_ref="https://example.test/" + str(index),
            source_lineage="TEST",
            actor_ref="synthetic",
            facts={
                "quality": "OFFICIAL",
                "published_date": "2026-09-23",
                "retrieved_at": timestamp,
                "data_date": "2026-09-22",
                "facts": {
                    "publication_timezone": "Asia/Shanghai",
                    "shadow_scope": "REGION:TEST",
                    "shadow_subject": "CORE01",
                    "values": {"example": "1"},
                },
            },
        )["id"]
        inputs = [ShadowInput(slot=s, subject="CORE01", evidence_id=eid) for s in D_INPUTS]
        result = service.observe(observation(model, inputs, key=f"malformed-{index}"))["output"]
        assert any("TIME_PROVENANCE_MISSING" in x for x in result["rows"][0]["missing"])


def test_as_of_data_day_uses_archived_timezone(rig):
    _, _, service, _, _, _ = rig
    model = service.register(definition())
    request = complete(service, model, retrieved_at="2026-09-23T20:00:00Z", data_date="2026-09-24")
    request = request.model_copy(update={"as_of": datetime(2026, 9, 23, 21, tzinfo=UTC)})
    assert service.observe(request)["output"]["status"] == "EVIDENCE_COMPLETE"


def test_missing_source_timezone_does_not_borrow_runtime_timezone(rig):
    _, _, service, _, _, _ = rig
    model = service.register(definition())
    request = complete(
        service,
        model,
        facts={"shadow_scope": "REGION:TEST", "shadow_subject": "CORE01", "values": {"value": "1"}},
    )
    output = service.observe(request)["output"]
    assert output["status"] == "INSUFFICIENT_DATA"
    assert any("TIME_PROVENANCE_MISSING" in x for x in output["rows"][0]["missing"])


@pytest.mark.parametrize("bad", [None, "", " ", "NaN", True, [], "not-numeric"])
def test_review_dimension_values_must_satisfy_versioned_contract(rig, bad):
    _, _, service, _, _, _ = rig
    contract = {s: [{"name": "value", "value_type": "NUMBER"}] for s in C_INPUTS}
    model = service.register(definition(input_contract=contract))
    request = complete(
        service,
        model,
        facts={
            "publication_timezone": "Asia/Shanghai",
            "shadow_scope": "REGION:TEST",
            "shadow_subject": "CORE01",
            "values": {"value": bad},
        },
    )
    record = service.observe(request)
    assert record["output"]["status"] == "INSUFFICIENT_DATA"
    assert len(record["output"]["rows"][0]["missing"]) == 5
    assert not service.validate(model["id"], record["id"])["checks"]["input_coverage"]


def test_review_undefined_contract_and_thin_candidate_evidence_never_pass(rig):
    _, _, service, _, _, _ = rig
    model = service.register(definition(input_contract={}))
    record = service.observe(complete(service, model))
    assert record["output"]["status"] == "INSUFFICIENT_DATA"
    assert all("INPUT_CONTRACT_UNDEFINED" in x for x in record["output"]["rows"][0]["missing"])
    candidate = service.register(
        definition(
            "SATELLITE_RANKING",
            rules=[{"slot": "cost", "field": "fee", "operator": "MAX", "value": "2"}],
        )
    )
    request = complete(
        service,
        candidate,
        "SATELLITE_RANKING",
        facts={
            "publication_timezone": "Asia/Shanghai",
            "shadow_scope": "REGION:TEST",
            "shadow_subject": "CORE01",
            "values": {"fee": "1", "product_type": "INDEX"},
        },
    )
    request = request.model_copy(update={"idempotency_key": "thin-candidate"})
    result = service.observe(request)["output"]
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["rows"][0]["filter_result"] == "UNKNOWN"
    assert any("benchmark" in x for x in result["rows"][0]["missing"])
    with pytest.raises(LedgerError, match="new model version"):
        service.register(
            definition(
                input_contract={s: [{"name": "different", "value_type": "TEXT"}] for s in C_INPUTS}
            )
        )


@pytest.mark.parametrize("table", ["shadow_models", "shadow_records"])
def test_review_readiness_rejects_each_missing_shadow_table(rig, table):
    import sqlite3

    from investor_core.database import check_database

    db, settings, _, _, _, _ = rig
    with sqlite3.connect(db) as c:
        c.execute('DROP TABLE "' + table + '"')  # table is from the fixed parametrization above.
    check = next(c for c in check_database(settings) if c.name == "database-schema")
    assert check.status == "FAIL" and table in check.message


def test_review_paused_exact_observation_replay_is_readonly_and_conflicts_rejected(rig):
    db, _, service, _, _, _ = rig
    model = service.register(definition())
    request = observation(model)
    saved = service.observe(request)
    draft = service.create_review(
        ReviewRequest(
            model_id=model["id"], target="OFF", reason="Synthetic pause", idempotency_key="pause"
        )
    )
    service.confirm(
        model["id"],
        draft["id"],
        ReviewConfirmation(
            confirmation_token=draft["confirmation_token"], confirmed_by="synthetic-user"
        ),
    )
    before = dump(db)
    assert service.observe(request) == saved
    assert dump(db) == before
    with pytest.raises(LedgerError) as conflict:
        service.observe(request.model_copy(update={"as_of": request.as_of - timedelta(hours=1)}))
    assert conflict.value.code == "SHADOW_KEY_CONFLICT"
    with pytest.raises(LedgerError) as paused:
        service.observe(request.model_copy(update={"idempotency_key": "new"}))
    assert paused.value.code == "SHADOW_PAUSED"
    assert dump(db) == before


def test_validator_version_does_not_reuse_legacy_coverage_receipt(rig):
    from investor_core.scheduler import digest

    _, _, service, _, _, _ = rig
    model = service.register(definition())
    observation_record = service.observe(observation(model))
    with service.research._connect() as c:
        legacy = service._append(
            c,
            model["id"],
            "VALIDATION",
            observation_record["id"],
            {
                "request_hash": digest(observation_record),
                "observation_id": observation_record["id"],
                "checks": {"input_coverage": True},
                "result": "INSUFFICIENT_DATA",
            },
        )
    current = service.validate(model["id"], observation_record["id"])
    assert current["id"] != legacy["id"]
    assert current["evaluator_version"] == "shadow-evidence-v2"
    assert not current["checks"]["input_coverage"]
    assert service.validate(model["id"], observation_record["id"]) == current
    assert legacy in service.read(model["id"])["history"]
