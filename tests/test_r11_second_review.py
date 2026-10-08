"""Second independent review reproducers, always synthetic and isolated."""

import hashlib
import json
from copy import deepcopy
from datetime import date, timedelta

import pytest
from test_r11_calculators import candidates, macro
from test_r11_governance import window
from test_r11_review_fixes import enriched
from test_r11_service import archive_bundle, isolated  # noqa: F401

from investor_core.execution import ExecutionService, SourceArchive
from investor_core.r11_candidates import CandidateInput, calculate_candidates
from investor_core.r11_governance import engine_hash
from investor_core.r11_inputs import DEFINITION
from investor_core.r11_macro import MacroInput, calculate_macro
from investor_core.r11_sensitivity import assess_stress_rows, stress_diagnostics
from investor_core.r11_service import R11Request
from investor_core.scheduler import digest


def new_bundle(service, eid, key, change):
    with service.research._connect() as c:
        original = json.loads(service._evidence(c, eid)["facts_json"])
    change(original["facts"])
    original["source_ref"] += "/" + key
    fields = {k: v for k, v in original.items() if k in SourceArchive.model_fields}
    return ExecutionService(service.research).archive(SourceArchive.model_validate(fields))["id"]


@pytest.mark.parametrize(
    "kind,match,quality,expected",
    [
        ("K", False, "OFFICIAL", False),
        ("F", False, "OFFICIAL", False),
        ("K", True, "OFFICIAL", False),
        ("F", True, "OFFICIAL", False),
        ("H", True, "OFFICIAL", True),
        ("H", False, "OFFICIAL", False),
        ("H", True, "UNVERIFIED", False),
    ],
)
def test_actual_extraction_source_time_quality_and_field_association(
    isolated, kind, match, quality, expected  # noqa: F811
):
    _, _, service, _ = isolated
    body = macro()
    body["context"]["evidence_class"] = kind
    eid = archive_bundle(service, body)
    future = deepcopy(body["context"]["sources"]["official"])
    future.update(
        url="https://example.test/future",
        lineage="SYNTHETIC_SECOND_SOURCE",
        quality=quality,
        published_at="2026-10-17T09:00:00+08:00",
        first_retrieved_at="2026-10-17T10:00:00+08:00",
    )
    point = body["pb"]["points"][-1]
    raw = json.dumps(
        dict(publication=future["published_at"], day=point["day"], value=point["value"])
    )
    future["document_hash"] = hashlib.sha256(raw.encode()).hexdigest()
    future["archive_id"] = ExecutionService(service.research).archive(
        SourceArchive(
            instrument_code="CORE01",
            source_name="SYNTHETIC LATER SOURCE",
            source_ref=future["url"],
            source_lineage=future["lineage"],
            quality=quality,
            retrieved_at=future["first_retrieved_at"],
            published_date="2026-10-17",
            data_date=point["day"],
            excerpt=raw,
            original_sha256=future["document_hash"],
            facts=dict(
                dataset_kind="SYNTHETIC",
                r11_original_format="JSON_UTF8",
                r11_publication_pointer="/publication",
                publication_timezone="Asia/Shanghai",
                publication_precision="INSTANT",
                r11_source_binding={k: future[k] for k in ["published_at", "first_retrieved_at"]},
            ),
        )
    )["id"]

    def change(facts):
        data = facts["r11_input"]
        data["context"]["sources"]["future"] = future
        if match:
            data["pb"]["points"][-1]["source"] = "future"
        for field in ("day", "value"):
            path = f"/pb/points/{len(data['pb']['points']) - 1}/{field}"
            facts["r11_bindings"][path] = dict(source="future", pointer="/" + field)

    altered = new_bundle(service, eid, "changed", change)
    run = service.observe(
        R11Request(method="C", bundle_evidence_id=altered, idempotency_key="extraction")
    )
    assert (run["output"]["status"] == "CALCULATED_SHADOW") is expected
    reasons = str(run["output"]["extraction"]["failures"])
    if not match:
        assert "FIELD_SOURCE_BINDING_MISMATCH" in reasons
    if kind != "H":
        assert "NOT_KNOWN_AS_OF" in reasons
    if quality == "UNVERIFIED":
        assert "SOURCE_UNVERIFIED" in reasons
    assert service.replay("C", run["id"])["result"] == "PASS"
    assert run["output"]["dataset_kind"] == "SYNTHETIC"


def test_failed_extraction_third_week_cannot_create_or_carry_new_stable_state(isolated):  # noqa: F811
    _, _, service, clock = isolated
    runs = []
    for i in range(6):
        day = date(2026, 10, 10) + timedelta(weeks=i)
        clock[0] = clock[0] + timedelta(weeks=1)
        eid = archive_bundle(service, macro(day), key=f"week-{i}")
        if i == 2:
            eid = new_bundle(
                service,
                eid,
                "bad-binding",
                lambda f: f["r11_bindings"].update(
                    {"/pb/points/1260/value": dict(source="missing", pointer="/value")}
                ),
            )
        runs.append(
            service.observe(R11Request(method="C", bundle_evidence_id=eid, idempotency_key=str(i)))
        )
    invalid = runs[2]["output"]
    assert invalid["dominant_season"] == "UNKNOWN"
    assert invalid["last_stable"] is None and invalid["stable_since"] is None
    assert invalid["confidence"] == "LOW" and invalid["uncertain"]
    assert runs[3]["output"]["pending_weeks"] == 1 and runs[3]["output"]["last_stable"] is None
    assert runs[4]["output"]["pending_weeks"] == 2 and runs[4]["output"]["last_stable"] is None
    assert runs[5]["output"]["dominant_season"] == "SPRING"


def test_account_observation_exact_scope_allows_replacement_and_wrong_account_blocks():
    history = []
    for i in range(4):
        body = enriched(date(2026, 10, 10) + timedelta(weeks=i))
        capture = deepcopy(body["context"]["sources"]["official"])
        capture.update(quality="ACCOUNT_OBSERVATION", account_ref="SYNTHETIC")
        body["context"]["sources"]["account"] = capture
        body["replacement_evidence"].update(holding_source="account", thesis_source="account")
        output = calculate_candidates(CandidateInput.model_validate(body), history)
        history.append(output)
    assert output["replacement"] == "SHADOW_REPLACEMENT_REVIEW", output["replacement_blockers"]
    body["context"]["sources"]["account"]["account_ref"] = "ANOTHER_ACCOUNT"
    wrong = calculate_candidates(CandidateInput.model_validate(body), history[:-1])
    assert (
        wrong["replacement"] == "BLOCKED"
        and "SOURCE_ACCOUNT_OBSERVATION" in wrong["replacement_blockers"]
    )


@pytest.mark.parametrize("method", ["C", "MEDICAL", "A500"])
def test_all_registered_stress_scenarios_execute_and_omission_is_incomplete(method):
    periods = window().model_copy(update={"method": method})
    body = macro() if method == "C" else candidates(method)
    typed = (
        MacroInput.model_validate(body) if method == "C" else CandidateInput.model_validate(body)
    )
    output = calculate_macro(typed) if method == "C" else calculate_candidates(typed)
    record = dict(
        id="synthetic",
        input=typed.model_dump(mode="json"),
        output=output,
        previous_outputs=[output],
    )
    stress = stress_diagnostics(periods, [record])
    assert stress["result"] == "PASS", stress
    assert {r["scenario"] for r in stress["rows"]} >= {
        "SOURCE_REVISION",
        "IDENTITY_CHANGE",
        "SOURCE_OUTAGE",
    }
    assert assess_stress_rows(method, ["synthetic"], stress["rows"][:-1])["result"] == "INCOMPLETE"
    damaged = deepcopy(stress["rows"])
    damaged[0]["checks"].pop("no_financial_action")
    assert assess_stress_rows(method, ["synthetic"], damaged)["result"] == "INCOMPLETE"


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_frozen_historical_evaluators_replay_without_counting_as_current(isolated, version):  # noqa: F811
    _, _, service, _ = isolated
    if version == "v1":
        from investor_core.r11_legacy.macro import MacroInput as HistoricalInput
        from investor_core.r11_legacy.macro import calculate_macro as historical_calculate
    else:
        from investor_core.r11_legacy.v2.macro import MacroInput as HistoricalInput
        from investor_core.r11_legacy.v2.macro import calculate_macro as historical_calculate
    data = HistoricalInput.model_validate(macro())
    inputs = data.model_dump(mode="json")
    output = historical_calculate(data)
    evidence = dict(bundle={"facts_json": json.dumps({"facts": {"r11_bindings": {}}})}, sources={})
    if version == "v2":
        from investor_core.r11_legacy.v2.provenance import reconcile as old_reconcile

        output["extraction"] = old_reconcile(inputs, {}, {})
    model = service.shadow.register(
        service.definition("C", "SYNTHETIC").model_copy(update={"version": "1.0.0"})
    )
    with service.research._connect() as c:
        run = service.shadow._append(
            c,
            model["id"],
            "R11_OBSERVATION",
            version,
            dict(
                request_hash=version,
                input=inputs,
                output=output,
                output_hash=digest(output),
                evidence=evidence,
                evidence_hash=digest(evidence),
                definition_hash=digest(DEFINITION),
                previous_outputs=[],
                engine_hash="historical-not-" + engine_hash(),
            ),
        )
    result = service.replay("C", run["id"])
    assert result["result"] == "PASS" and result["historical_evaluator"]
    assert not result["qualifies_current_version"]
    assert service.runs("C", current_version=True) == []
