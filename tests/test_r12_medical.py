"""Synthetic isolated candidate tests; no production or actual forward records."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_r11_calculators import candidates

from investor_core.r11_candidates import CandidateInput, calculate_candidates
from investor_core.r11_inputs import DEFINITION as OLD_DEFINITION
from investor_core.r11_provenance import leaves
from investor_core.r11_validation import _selection
from investor_core.r12_medical import (
    DEFINITION,
    MedicalInput,
    evaluate,
    replay,
    select_history,
    snapshot,
)
from investor_core.scheduler import digest


def medical(bound=1):
    value = candidates()
    for p in value["products"]:
        p["redemption_arrival_max_trading_days"] = bound
    value["arrival_rules"] = {
        p["code"]: dict(
            source="official",
            channel="SYNTHETIC_CHANNEL",
            effective_from="2020-01-01",
            object="INVESTOR_ACCOUNT_ARRIVAL",
            day_basis="TRADING_DAYS",
        )
        for p in value["products"]
    }
    return MedicalInput.model_validate(value)


@pytest.mark.parametrize(
    "bound,state", [(None, "UNKNOWN"), (1, "SATISFIED"), (5, "SATISFIED"), (6, "NOT_SATISFIED")]
)
def test_score_does_not_depend_on_arrival(bound, state):
    baseline = evaluate(medical(1))
    result = evaluate(medical(bound))
    for a, b in zip(result["rows"], baseline["rows"], strict=True):
        assert a["total_score"] == b["total_score"] is not None
        assert a["dimension_scores"] == b["dimension_scores"]
        assert a["redemption_arrival"]["state"] == state
    assert not result["formal_forward_record"]
    assert not result["actual_promotion_authorized"]
    if state != "SATISFIED":
        assert result["replacement"] == "BLOCKED"
        assert result["leading_weeks"] == 0
        assert result["delay_stress_status"] == "INCOMPLETE"


@pytest.mark.parametrize(
    "field,value",
    [
        ("manager_team_since", None),
        ("manager_source", None),
        ("fees", None),
        ("dividend_coverage_source", None),
        ("subscription_confirmation_max_trading_days", None),
        ("subscription_confirmation_max_trading_days", 6),
        ("delay_source", None),
    ],
)
def test_other_required_inputs_remain_required(field, value):
    data = medical(None)
    setattr(data.products[0], field, value)
    result = evaluate(data)
    assert result["rows"][0]["total_score"] is None
    assert result["rows"][0]["rank"] is None
    assert result["rows"][0]["weights"] == {
        "return": "0.25",
        "risk": "0.35",
        "cost": "0.20",
        "management": "0.20",
    }
    assert not result["money_action"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("object", "PAYMENT"),
        ("day_basis", "WORKDAYS_UNRESOLVED"),
        ("effective_to", "2026-01-01"),
        ("effective_from", "2027-01-01"),
        ("source", "missing"),
    ],
)
def test_inapplicable_arrival_evidence_never_passes(field, value):
    body = medical().model_dump(mode="json")
    body["arrival_rules"]["003096"][field] = value
    result = evaluate(MedicalInput.model_validate(body))
    assert result["rows"][0]["total_score"] is not None
    assert result["rows"][0]["redemption_arrival"]["state"] == "UNKNOWN"
    assert result["replacement"] == "BLOCKED"


def test_missing_arrival_rule_and_conflicting_source():
    data = medical()
    data.arrival_rules = {}
    assert evaluate(data)["rows"][0]["redemption_arrival"]["state"] == "UNKNOWN"
    data = medical()
    data.context.sources["arrival"] = data.context.sources["official"].model_copy(
        update={"quality": "CONFLICT"}
    )
    data.arrival_rules["003096"].source = "arrival"
    assert evaluate(data)["rows"][0]["total_score"] is not None
    assert evaluate(data)["rows"][0]["redemption_arrival"]["state"] == "UNKNOWN"


def archive(data):
    body = data.model_dump(mode="json")
    # One synthetic native original binds every field; no fabricated REAL provenance.
    source = data.context.sources["official"]
    source.publication_precision = "DATE"
    original = json.dumps(body, sort_keys=True)
    import hashlib

    source.document_hash = hashlib.sha256(original.encode()).hexdigest()
    evidence = {
        "official": dict(
            id=source.archive_id,
            source_ref=source.url,
            source_lineage=source.lineage,
            facts_json=json.dumps(
                dict(
                    excerpt=original,
                    original_sha256=source.document_hash,
                    quality=source.quality,
                    published_date="2020-01-01",
                    retrieved_at=source.first_retrieved_at.isoformat(),
                    facts=dict(
                        r11_original_format="JSON_UTF8",
                        publication_timezone="Asia/Shanghai",
                        publication_precision="DATE",
                    ),
                )
            ),
        )
    }
    return evidence, {p: dict(source="official", pointer=p) for p in leaves(body)}


def test_only_arrival_provenance_is_separated_and_shared_confirmation_is_not():
    data = medical()
    evidence, bindings = archive(data)
    bindings.pop("/products/0/redemption_arrival_max_trading_days")
    result = evaluate(data, evidence=evidence, bindings=bindings)
    assert result["rows"][0]["total_score"] is not None
    assert result["replacement"] == "BLOCKED"
    assert result["rows"][0]["redemption_arrival"]["state"] == "UNKNOWN"
    bindings.pop("/products/0/subscription_confirmation_max_trading_days")
    assert evaluate(data, evidence=evidence, bindings=bindings)["rows"][0]["total_score"] is None


def test_real_without_raw_evidence_cannot_get_qualified_score():
    data = medical(None)
    data.context.dataset_kind = "REAL"
    result = evaluate(data)
    assert all(row["total_score"] is None for row in result["rows"])


def test_versions_snapshots_and_replay_do_not_inherit_qualification():
    data = medical(None)
    old = calculate_candidates(CandidateInput.model_validate(candidates()))
    old_copy = deepcopy(old)
    result = snapshot(data, history=[old])
    assert replay(result)["result"] == "PASS"
    assert result == snapshot(data, history=[old])
    assert old == old_copy
    assert select_history([old]) == []
    assert _selection([{"output": result["output"]}], "MEDICAL", "F", "SYNTHETIC") == {}
    assert result["definition_hash"] != digest(OLD_DEFINITION)
    assert result["output"]["definition_id"] == DEFINITION["definition_id"]
    changed = deepcopy(result)
    changed["output"]["rows"][0]["total_score"] = "100"
    assert replay(changed)["result"] == "FAIL"
    changed["definition_hash"] = digest(OLD_DEFINITION)
    with pytest.raises(ValueError):
        replay(changed)


def test_nonmedical_cannot_use_revision():
    with pytest.raises(ValidationError):
        MedicalInput.model_validate(candidates("A500"))


@pytest.mark.parametrize("cohort", ["MEDICAL", "A500"])
@pytest.mark.parametrize("bound", [None, 1, 6])
def test_r11_exact_published_output_preserved(cohort, bound):
    body = candidates(cohort)
    body["products"][0]["redemption_arrival_max_trading_days"] = bound
    output = calculate_candidates(CandidateInput.model_validate(body))
    golden = json.loads(
        (Path(__file__).parent / "fixtures/r11_v046_candidate_hashes.json").read_text()
    )
    assert digest(output) == golden[f"{cohort}:{bound}"]


def test_archive_failure_is_scoped_and_queries_do_not_mutate_inputs():
    data = medical()
    evidence, bindings = archive(data)
    before = data.model_dump(mode="json")
    bindings.pop("/arrival_rules/003096/channel")
    result = evaluate(data, evidence=evidence, bindings=bindings)
    assert result["rows"][0]["redemption_arrival"]["state"] == "UNKNOWN"
    assert result["rows"][1]["redemption_arrival"]["state"] == "SATISFIED"
    assert all(row["total_score"] is not None for row in result["rows"])
    assert data.model_dump(mode="json") == before
    assert result == evaluate(data, evidence=evidence, bindings=bindings)
    evidence["official"]["source_ref"] = "https://invalid.example/different"
    result = evaluate(data, evidence=evidence, bindings=bindings)
    assert all(row["total_score"] is None for row in result["rows"])


def test_replay_rejects_changed_definition_and_old_leading_history():
    data = medical(None)
    old = calculate_candidates(CandidateInput.model_validate(candidates()))
    old["leading_weeks"] = 100
    assert evaluate(data, history=[old]) == evaluate(data)
    record = snapshot(data)
    record["definition"]["change"] = "tampered"
    with pytest.raises(ValueError):
        replay(record)


def test_snapshot_binds_input_and_computation():
    record = snapshot(medical(None))
    record["input"]["products"][0]["redemption_arrival_max_trading_days"] = 4
    with pytest.raises(ValueError):
        replay(record)
    record = snapshot(medical(None))
    record["computation_fingerprint"] = "old-engine"
    with pytest.raises(ValueError):
        replay(record)
