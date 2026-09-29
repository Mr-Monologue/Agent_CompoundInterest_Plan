from copy import deepcopy
from math import sqrt
from statistics import stdev

import pytest
from fastapi.testclient import TestClient
from test_daily_client import dump
from test_holding_review import capture, setup
from test_peer_research import payload

from investor_core.api.app import create_app
from investor_core.peer_models import PeerStudy
from investor_core.peer_research import admission, evaluate
from investor_core.research_coverage import summarize


def index_payload():
    x = payload()
    x["research_method"] = "INDEX_FEEDER"
    for p in x["products"]:
        p.update(
            product_type="INDEX",
            active=False,
            share_class="A",
            index_profile=dict(
                structure="ETF_FEEDER",
                provider="CSI",
                index_code="TEST",
                index_name="Fixture",
                variant="PRICE",
                currency="CNY",
                target_etf_code="ETF-" + p["code"],
                official_benchmark="95% index + 5% cash; not reconstructed",
                effective_from="2020-01-01",
                evidence_ids=[p["code"]],
            ),
        )
    x["tracking_reference"] = dict(
        provider="CSI",
        index_code="TEST",
        variant="PRICE",
        evidence_ids=["R"],
        series=dict(
            provider="CSI",
            code="TEST",
            currency="CNY",
            return_basis="PRICE",
            evidence_ids=["R"],
            points=[
                dict(day=d, value=v)
                for d, v in zip(x["calendar_dates"], [1, 1, 1, 1, 1, 1], strict=True)
            ],
        ),
    )
    return x


def test_tracking_sample_formula_no_fees_again():
    x = index_payload()
    r = evaluate(PeerStudy.model_validate(x))["rows"][0]["windows"][0]
    t = r["tracking"]
    assert t["calculated"]["tracking_difference_pp"] == pytest.approx(10)
    assert t["calculated"]["tracking_error_pct"] == pytest.approx(
        stdev([-0.1, 1.1 / 0.9 - 1]) * sqrt(252) * 100
    )
    assert t["calculated"]["samples"] == 2
    assert t["official_benchmark_calculated"] is False
    x["products"][0]["dimensions"]["fees"][0]["text"] = "Fee changes are not deducted again"
    assert evaluate(PeerStudy.model_validate(x))["rows"][0]["windows"][0] == r


@pytest.mark.parametrize(
    "change,status",
    [
        ("variant", "EXCLUDED"),
        ("share", "EXCLUDED"),
        ("structure", "CONTEXT_ONLY"),
        ("unknown", "UNVERIFIED"),
    ],
)
def test_scope_identity_not_name(change, status):
    x = index_payload()
    p = x["products"][1]
    if change == "variant":
        p["index_profile"]["variant"] = "NET_TOTAL_RETURN"
    if change == "share":
        p["share_class"] = "C"
    if change == "structure":
        p["index_profile"]["structure"] = "ETF"
    if change == "unknown":
        p["index_profile"] = None
    s = PeerStudy.model_validate(x)
    assert admission(s.products[1], s)[0] == status


def test_structure_period_and_missing_tracking_block_without_filling():
    x = index_payload()
    x["products"][1]["index_profile"]["effective_from"] = "2023-01-03"
    r = evaluate(PeerStudy.model_validate(x))["rows"][1]["windows"][0]
    assert r["calculated"] is None and "PRODUCT_STRUCTURE_NOT_EFFECTIVE_FOR_WINDOW" in r["gaps"]
    x = index_payload()
    x["tracking_reference"]["series"]["points"].pop(1)
    r = evaluate(PeerStudy.model_validate(x))["rows"][0]["windows"][0]
    assert r["calculated"] and r["tracking"]["calculated"] is None
    assert r["tracking"]["gaps"] == ["INDEX_COMMON_DATES_INCOMPLETE"]


def test_coverage_readonly_historical_records_and_no_name_guess(tmp_path):
    settings, pid, aid, _eid, svc, _ = setup(tmp_path)
    svc.capture(capture(svc, pid, aid))
    before = dump(settings.db_path)
    web = TestClient(create_app(settings))
    q = dict(portfolio_id=pid, account_id=aid)
    result = web.get("/v1/portfolio-research-coverage", params=q)
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["holding_count"] == len(svc.build(pid, aid)["items"])
    assert data["history_snapshot_count"] == 1
    assert all(r["product_type"] == "未知" for r in data["items"])
    assert result.json() == web.get("/v1/portfolio-research-coverage", params=q).json()
    assert (
        web.get(
            "/v1/portfolio-research-coverage", params=dict(q, instrument_code="not-held")
        ).status_code
        == 404
    )
    assert dump(settings.db_path) == before
    item = svc.build(pid, aid)["items"][0]
    item["reasons"] = [r for r in item["reasons"] if r["category"] == "CONTEXT_OBSERVATION"]
    p = [dict(profile=dict(product_type="正式类型"), data_date="2024-01-01")]
    r = summarize(item, [], p)
    assert r["priority"] == "MAINTENANCE" and not r["context_is_risk"]


def test_coverage_preserves_same_window_source_difference(tmp_path):
    _settings, pid, aid, _eid, svc, _ = setup(tmp_path)
    item = svc.build(pid, aid)["items"][0]
    w = item["windows"][0]
    peer = dict(
        anchor_code=item["instrument_code"],
        cohort_key="fixture",
        version=1,
        input=dict(sources={}, products=[dict(code=item["instrument_code"])]),
        result=dict(
            common_research_cutoff=w["end"],
            rows=[
                dict(
                    code=item["instrument_code"],
                    windows=[dict(start=w["start"], end=w["end"], calculated=dict(return_pct=99))],
                )
            ],
        ),
    )
    original = deepcopy(item)
    r = summarize(item, [peer], [])
    assert r["source_differences"] and r["source_differences"][0]["d1"] == w
    assert item == original


def test_tracking_never_substitutes_return_variant():
    x = index_payload()
    x["tracking_reference"]["series"]["return_basis"] = "TOTAL_RETURN"
    w = evaluate(PeerStudy.model_validate(x))["rows"][0]["windows"][0]
    assert w["calculated"] and w["tracking"]["calculated"] is None
    assert w["tracking"]["gaps"] == ["INDEX_RETURN_BASIS_MISMATCH"]
