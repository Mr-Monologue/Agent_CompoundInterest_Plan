"""Isolated lifecycle and diagnostic checks; simulated clocks never count as real F."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from fastapi.testclient import TestClient
from test_r11_calculators import candidates
from test_r11_service import business_rows, isolated  # noqa: F401

from investor_core.api.app import create_app
from investor_core.ledger import LedgerError
from investor_core.r11_candidates import CandidateInput, calculate_candidates
from investor_core.r11_governance import (
    GovernanceConfirmation,
    GovernanceDraft,
    R11Governance,
    RegistrationRequest,
    ValidationRequest,
)
from investor_core.r11_rules import registry, scenarios
from investor_core.r11_sensitivity import baseline_sets, sensitivity
from investor_core.r11_validation import ValidationWindow


def window():
    return ValidationWindow(
        method="A500",
        dataset_kind="SYNTHETIC",
        history_start="2025-10-11",
        history_end="2026-10-03",
        forward_start="2026-10-10",
        forward_end="2027-01-02",
    )


def diagnostic_record(day, kind, index, *, extra_point=True):
    body = candidates("A500", day)
    # Synthetic fixtures remain in the explicit simulation domain throughout.
    body["context"].update(evidence_class=kind, dataset_kind="SYNTHETIC")
    count = 254 if extra_point else 253
    for p in body["products"]:
        p["assets_report_date"] = str(day - timedelta(days=30))
        p["nav"]["points"] = p["nav"]["points"][-count:]
        p["calendar"]["dates"] = p["calendar"]["dates"][-count:]
        p["calendar"]["covered_from"] = p["calendar"]["dates"][0]
    body["products"][1]["nav"]["points"] = [
        dict(p, value=str(Decimal("1.0001") ** i))
        for i, p in enumerate(body["products"][1]["nav"]["points"])
    ]
    body["benchmark"]["points"] = body["benchmark"]["points"][-count:]
    body["benchmark_calendar"]["dates"] = body["benchmark_calendar"]["dates"][-count:]
    body["benchmark_calendar"]["covered_from"] = body["benchmark_calendar"]["dates"][0]
    data = CandidateInput.model_validate(body)
    return dict(
        id=f"{kind}-{index}", input=data.model_dump(mode="json"), output=calculate_candidates(data)
    )


def test_scenario_registry_is_exhaustive_normalized_and_decimal_context_independent():
    for method in ("C", "MEDICAL", "A500"):
        registered = registry(method)
        for values in scenarios(method).values():
            for key, value in values.items():
                if key.endswith("weights"):
                    assert abs(sum(value) - 1) < Decimal("1e-27")
                elif isinstance(value, int):
                    assert value >= 1
        with localcontext() as context:
            context.prec = 8
            assert registry(method) == registered
        assert not any("coverage" in name or "source" in name for name in registered["scenarios"])


def test_each_scenario_uses_separate_frozen_h_f_denominators():
    periods = window()
    records = [
        diagnostic_record(periods.history_start + timedelta(weeks=i), "H", i) for i in range(26)
    ]
    records += [
        diagnostic_record(periods.forward_start + timedelta(weeks=i), "F", i) for i in range(8)
    ]
    frozen = baseline_sets(periods, records)
    assert {k: len(v) for k, v in frozen.items()} == {"H": 26, "F": 8}
    result = sensitivity(periods, records, frozen)
    assert len(result["scenarios"]) == len(scenarios("A500"))
    assert result["result"] == "PASS"
    assert all(
        s["strata"]["H"]["denominator"] == 26 and s["strata"]["F"]["denominator"] == 8
        for s in result["scenarios"]
    )
    with pytest.raises(ValueError, match="Baseline set drift"):
        sensitivity(periods, records, dict(H=frozen["H"][:-1], F=frozen["F"]))


def test_missing_perturbed_warmup_stays_in_denominator_and_blocks():
    periods = window()
    records = [diagnostic_record(periods.forward_start, "F", 0, extra_point=False)]
    frozen = baseline_sets(periods, records)
    result = sensitivity(periods, records, frozen)
    scenario = next(s for s in result["scenarios"] if s["scenario"] == "return_window.plus1period")
    assert scenario["strata"]["F"]["denominator"] == 1
    assert scenario["strata"]["F"]["numerator"] == 0
    assert not scenario["strata"]["F"]["complete"]
    assert result["result"] == "NOT_PASSED"


def test_preregistration_no_backfill_and_no_synthetic_promotion(isolated):  # noqa: F811
    db, _, service, clock = isolated
    governance = R11Governance(service)
    request = RegistrationRequest(
        window=window().model_copy(update={"dataset_kind": "REAL"}), idempotency_key="register"
    )
    with pytest.raises(LedgerError) as late:
        governance.register(request)
    assert late.value.code == "R11_REGISTRATION_LATE"
    clock[0] = datetime(2026, 10, 8, 12, tzinfo=UTC)
    record = governance.register(request)
    assert governance.register(request) == record
    validation = ValidationRequest(registration_id=record["id"], idempotency_key="validate")
    with pytest.raises(LedgerError) as early:
        governance.validate(validation)
    assert early.value.code == "R11_FORWARD_PERIOD_IN_PROGRESS"
    clock[0] = datetime(2027, 1, 2, 12, tzinfo=UTC)
    result = governance.validate(validation)
    before = business_rows(db)
    gate = governance.gate("A500", "ADVISORY", result["id"])
    assert not gate["eligible"] and "forward_sample" in gate["blockers"]
    assert "ENGINEERING_RECEIPT_MISSING" in gate["blockers"]
    assert not governance.gate("A500", "ACTIVE", result["id"])["eligible"]
    with pytest.raises(LedgerError) as blocked:
        governance.draft(
            GovernanceDraft(
                method="A500",
                target="ADVISORY",
                validation_id=result["id"],
                reason="synthetic",
                idempotency_key="blocked",
            )
        )
    assert blocked.value.code == "R11_PROMOTION_BLOCKED"
    assert business_rows(db) == before


def test_actual_http_confirm_pause_resume_expiry_and_drift(isolated, monkeypatch):  # noqa: F811
    db, settings, service, clock = isolated
    monkeypatch.setattr("investor_core.api.shadow.R11Service", lambda research: service)
    before = business_rows(db)
    with TestClient(create_app(settings)) as client:
        draft = client.post(
            "/v1/r11/reviews",
            json=dict(
                method="A500", target="OFF", reason="synthetic pause", idempotency_key="pause"
            ),
        ).json()["data"]
        url = f"/v1/r11/reviews/{draft['id']}/confirm"
        assert (
            client.post(url, json=dict(confirmation_token="wrong", confirmed_by="test")).status_code
            == 400
        )
        confirmation = dict(
            confirmation_token=draft["confirmation_token"], confirmed_by="isolated-test"
        )
        event = client.post(url, json=confirmation).json()["data"]
        assert event["target"] == "OFF"
        assert client.post(url, json=confirmation).json()["data"] == event
        assert (
            "confirmation_token"
            not in client.post(
                "/v1/r11/reviews",
                json=dict(
                    method="A500", target="OFF", reason="synthetic pause", idempotency_key="pause"
                ),
            ).json()["data"]
        )
        resume = client.post(
            "/v1/r11/reviews",
            json=dict(
                method="A500", target="SHADOW", reason="synthetic resume", idempotency_key="resume"
            ),
        ).json()["data"]
        clock[0] += timedelta(days=1)
        assert (
            client.post(
                f"/v1/r11/reviews/{resume['id']}/confirm",
                json=dict(confirmation_token=resume["confirmation_token"], confirmed_by="test"),
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/r11/reviews",
                json=dict(
                    method="A500", target="ACTIVE", reason="forbidden", idempotency_key="active"
                ),
            ).status_code
            == 422
        )
    assert business_rows(db) == before


def test_quality_discovery_downgrades_only_research_and_never_auto_restores(isolated):  # noqa: F811
    db, _, service, _ = isolated
    governance = R11Governance(service)
    model = service.shadow.register(service.definition("C"))
    # A pre-existing advisory state is seeded solely to exercise recovery. This
    # bypass is private test setup, not an API and not evidence of qualification.
    with service.research._connect() as c:
        service.shadow._append(
            c,
            model["id"],
            "REVIEW_CONFIRMATION",
            "synthetic-initial-state",
            dict(
                request_hash="fixture",
                draft_id="fixture",
                target="ADVISORY",
                confirmed_by="SYNTHETIC_TEST",
            ),
        )
    before = business_rows(db)
    result = governance.assess("C", "missing-data")
    assert result["critical"] == ["NO_REAL_FORWARD_OBSERVATION"]
    assert governance.gate("C", "ADVISORY")["current"] == "SHADOW"
    assert governance.assess("C", "missing-data") == result
    assert governance.gate("C", "ADVISORY")["current"] == "SHADOW"
    assert business_rows(db) == before


def test_review_confirmation_rejects_intervening_evidence(isolated):  # noqa: F811
    _, _, service, _ = isolated
    governance = R11Governance(service)
    draft = governance.draft(
        GovernanceDraft(method="C", target="OFF", reason="synthetic", idempotency_key="drift")
    )
    governance.assess("C", "new-health-evidence")
    with pytest.raises(LedgerError) as drift:
        governance.confirm(
            draft["id"],
            GovernanceConfirmation(
                confirmation_token=draft["confirmation_token"], confirmed_by="test"
            ),
        )
    assert drift.value.code == "R11_REVIEW_DRIFT"
