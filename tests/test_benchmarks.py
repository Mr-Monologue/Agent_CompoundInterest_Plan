from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_daily_client import dump
from test_notebook import setup_case

from investor_core.api.app import create_app
from investor_core.benchmarks import (
    BenchmarkService,
    DiagnosticInput,
    MappingApproval,
    MappingDraft,
    diagnose,
)
from investor_core.ledger import LedgerError
from investor_core.notebook import NotebookService


def setup_mapping(tmp_path):
    settings, pid, aid, research, case = setup_case(tmp_path)
    eid = case["return_driver"]["evidence_ids"][0]

    def period(start, end, equity):
        return dict(
            effective_from=start,
            effective_to=end,
            observed_on="2026-09-23",
            components=[
                dict(
                    name=code,
                    provider="issuer",
                    code=code,
                    currency="CNY",
                    return_basis="PRICE",
                    weight_bps=weight,
                    evidence_ids=[eid],
                    availability="test series",
                )
                for code, weight in [("EQ", equity), ("BOND", 10000 - equity)]
            ],
            evidence_ids=[eid],
            method="DAILY_REBALANCED",
            limitation="Synthetic isolated test",
        )

    periods = [period("2020-01-01", "2026-09-20", 6500), period("2026-09-21", None, 9000)]
    payload = dict(
        portfolio_id=pid,
        instrument_code="CORE01",
        expected_previous_version=0,
        idempotency_key="benchmark-1",
        official_disclosures=periods,
        diagnostic_mapping=periods,
        rationale="Research only",
        limitations=["No risk activation"],
    )
    service = BenchmarkService(NotebookService(research))
    return settings, pid, aid, research, eid, service, payload


def input_for(mapping, eid):
    dates = ["2026-09-18", "2026-09-21", "2026-09-22"]

    def series(code, values, basis="PRICE"):
        return dict(
            provider="issuer",
            code=code,
            currency="CNY",
            return_basis=basis,
            evidence_ids=[eid],
            points=[dict(day=d, value=v) for d, v in zip(dates, values, strict=True)],
        )

    return dict(
        mapping_id=mapping["id"],
        idempotency_key="run-1",
        start=dates[0],
        end=dates[-1],
        expected_dates=dates,
        calendar_evidence_ids=[eid],
        fund=series("CORE01", ["1", "0.9", "0.99"], "NAV_NET_INTERNAL_FEES"),
        benchmarks=[series("EQ", ["100", "110", "99"]), series("BOND", ["100", "100", "100"])],
        distributions=[dict(ex_date="2026-09-21", cash_per_unit="0.1")],
        distribution_coverage_from=dates[0],
        distribution_coverage_to=dates[-1],
        distribution_evidence_ids=[eid],
        limitations=["isolated synthetic"],
    )


def test_version_concurrency_confirmation_scope_and_business_unchanged(tmp_path):
    settings, pid, _, _, _eid, service, payload = setup_mapping(tmp_path)
    before = dump(settings.db_path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(lambda _: service.create(MappingDraft.model_validate(payload)), range(3))
        )
    assert len({x["id"] for x in results}) == 1
    first = next(x for x in results if "confirmation_token" in x)
    with pytest.raises(LedgerError):
        service.approve(
            first["id"], MappingApproval(confirmation_token="wrong", confirmed_by="user")
        )
    approved = service.approve(
        first["id"],
        MappingApproval(confirmation_token=first["confirmation_token"], confirmed_by="user"),
    )
    assert approved["status"] == "APPROVED_RESEARCH" and not approved["money_action"]
    read = service.read(pid, "CORE01")
    assert read["approved_research_version"] == 1 and "confirmation_token" not in str(read)
    changed = deepcopy(payload)
    changed["rationale"] = "changed"
    with pytest.raises(LedgerError, match="mismatch"):
        service.create(MappingDraft.model_validate(changed))
    after = dump(settings.db_path)
    prefix = 'INSERT INTO "research_benchmark_mappings"'
    assert [v for v in before if not v.startswith(prefix)] == [
        v for v in after if not v.startswith(prefix)
    ]


def test_stale_expired_and_unknown_mapping_cannot_be_approved(tmp_path):
    _, _, _, research, _, service, payload = setup_mapping(tmp_path)
    first = service.create(MappingDraft.model_validate(payload))
    revised = deepcopy(payload)
    revised.update(idempotency_key="second", expected_previous_version=1)
    second = service.create(MappingDraft.model_validate(revised))
    with pytest.raises(LedgerError, match="newer"):
        service.approve(
            first["id"],
            MappingApproval(confirmation_token=first["confirmation_token"], confirmed_by="user"),
        )
    research._now = lambda: datetime(2026, 9, 25, tzinfo=UTC)
    with pytest.raises(LedgerError, match="new draft"):
        service.approve(
            second["id"],
            MappingApproval(confirmation_token=second["confirmation_token"], confirmed_by="user"),
        )
    overlap = deepcopy(payload)
    overlap["diagnostic_mapping"][0]["effective_to"] = None
    with pytest.raises(ValidationError):
        MappingDraft.model_validate(overlap)


def test_chain_change_date_dividend_and_no_fee_double_deduction(tmp_path):
    _, _, _, _, eid, service, payload = setup_mapping(tmp_path)
    mapping = service.create(MappingDraft.model_validate(payload))
    request = input_for(mapping, eid)
    result = service.run(DiagnosticInput.model_validate(request), persist=False)
    x = result["calculated"]
    assert x["fund_return_pct"] == pytest.approx(10)
    assert x["benchmark_return_pct"] == pytest.approx(-0.81)
    assert x["excess_percentage_points"] == pytest.approx(10.81)
    assert x["fund_max_drawdown_pct"] == 0
    assert x["daily"][0]["benchmark_effective_from"] == "2026-09-21"
    # Counterfactual old weights differs. Never retroactively rewrite old periods.
    old = deepcopy(mapping)
    old["diagnostic_mapping"] = old["diagnostic_mapping"][:1]
    old["diagnostic_mapping"][0]["effective_to"] = None
    other = diagnose(old, DiagnosticInput.model_validate(request))
    assert other["calculated"]["benchmark_return_pct"] == pytest.approx(-0.4225)
    changed = deepcopy(request)
    changed["fund"]["return_basis"] = "FUND_TOTAL_RETURN"
    with pytest.raises(ValidationError):
        DiagnosticInput.model_validate(changed)


@pytest.mark.parametrize(
    "mutation,gap",
    [
        ("missing", "BENCHMARK_DATE_MISSING"),
        ("fx", "CURRENCY_OR_FX_MISMATCH"),
        ("dividend", "DIVIDEND_COVERAGE_INCOMPLETE"),
        ("calendar", "EXACT_COMMON_CALENDAR_MISSING"),
        ("conflict", "BENCHMARK_SOURCE_CONFLICT"),
        ("basis", "BENCHMARK_RETURN_BASIS_MISMATCH"),
    ],
)
def test_missing_or_incomparable_data_does_not_compute(tmp_path, mutation, gap):
    _, _, _, _, eid, service, payload = setup_mapping(tmp_path)
    mapping = service.create(MappingDraft.model_validate(payload))
    request = input_for(mapping, eid)
    if mutation == "missing":
        request["benchmarks"][0]["points"].pop(1)
    if mutation == "fx":
        request["benchmarks"][0]["currency"] = "USD"
    if mutation == "dividend":
        request["distribution_coverage_to"] = None
    if mutation == "calendar":
        request["calendar_evidence_ids"] = []
    if mutation == "conflict":
        request["benchmarks"][0]["validation"] = "CONFLICT"
    if mutation == "basis":
        request["benchmarks"][0]["return_basis"] = "TOTAL_RETURN"
    result = service.run(DiagnosticInput.model_validate(request), persist=False)
    assert result["calculated"] is None and gap in result["gaps"]


def test_preview_is_read_only_run_is_idempotent_and_http_reads(tmp_path):
    settings, pid, _, _, eid, service, payload = setup_mapping(tmp_path)
    mapping = service.create(MappingDraft.model_validate(payload))
    request = input_for(mapping, eid)
    before = dump(settings.db_path)
    app = TestClient(create_app(settings))
    response = app.post("/v1/benchmark-research-preview", json=request)
    assert response.status_code == 200 and response.json()["data"]["calculated"]
    assert before == dump(settings.db_path)
    result = service.run(DiagnosticInput.model_validate(request))
    assert service.run(DiagnosticInput.model_validate(request))["id"] == result["id"]
    read = app.get(
        "/v1/benchmark-research", params={"portfolio_id": pid, "instrument_code": "CORE01"}
    )
    assert read.status_code == 200 and len(read.json()["data"]["runs"]) == 1
    after = dump(settings.db_path)
    prefix = 'INSERT INTO "research_benchmark_runs"'
    assert [v for v in before if not v.startswith(prefix)] == [
        v for v in after if not v.startswith(prefix)
    ]


def test_reported_windows_are_distinct_from_reconstructed_series(tmp_path):
    _, _, _, _, eid, service, payload = setup_mapping(tmp_path)
    mapping = service.create(MappingDraft.model_validate(payload))
    request = input_for(mapping, eid)
    request.update(
        expected_dates=[],
        benchmarks=[],
        reported_windows=[
            dict(
                start="2025-12-31",
                end="2026-06-30",
                fund_return_pct="3.45",
                benchmark_return_pct="-5.14",
                evidence_ids=[eid],
                limitation="issuer table, not reconstructed",
            )
        ],
    )
    result = service.run(DiagnosticInput.model_validate(request), persist=False)
    assert result["status"] == "DATA_BLOCKED" and result["calculated"] is None
    assert result["reported_windows"][0]["excess_percentage_points"] == 8.59
    assert "NOT_DAILY_RECONSTRUCTION" in result["reported_windows"][0]["basis"]


def test_reconstruction_difference_is_not_hidden(tmp_path):
    _, _, _, _, eid, service, payload = setup_mapping(tmp_path)
    mapping = service.create(MappingDraft.model_validate(payload))
    request = input_for(mapping, eid)
    request["reported_windows"] = [
        dict(
            start=request["start"],
            end=request["end"],
            fund_return_pct="9.9",
            benchmark_return_pct="-0.81",
            evidence_ids=[eid],
            limitation="test mismatch",
        )
    ]
    result = service.run(DiagnosticInput.model_validate(request), persist=False)
    assert result["status"] == "COMPUTED_WITH_RECONCILIATION_WARNING"
    assert "DISCLOSED_RETURN_MISMATCH" in result["display_text"]
    assert not result["reconciliation"][0]["independent_validation"]
