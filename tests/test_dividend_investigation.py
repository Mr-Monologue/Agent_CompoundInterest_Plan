from copy import deepcopy

import pytest
from test_daily_client import dump
from test_peer_research import payload
from test_research_updates import services

from investor_core.peer_models import PeerStudy
from investor_core.peer_research import evaluate
from investor_core.research_read_notices import attach_peer_limitations
from investor_core.research_update_sources import parse


def page(headers=None, cells=None, identity="003096"):
    headers = headers or ["年份", "权益登记日", "除息日", "每10份分红", "分红发放日"]
    cells = cells or ["2024年", "2024-08-22", "2024-08-23", "每10份派现金0.5700元", "2024-08-26"]
    return (
        f'<title>中欧医疗健康混合C({identity})</title><table class="cfxq"><tr>'
        + "".join(f"<th>{x}</th>" for x in headers)
        + "</tr><tr>"
        + "".join(f"<td>{x}</td>" for x in cells)
        + "</tr></table>"
    ).encode()


def test_exact_share_date_column_and_per_ten_conversion():
    assert parse(page(), "channel_div", "003096") == [
        dict(ex_date="2024-08-23", cash_per_unit="0.057")
    ]
    with pytest.raises(ValueError, match="IDENTITY"):
        parse(page(identity="0030960"), "channel_div", "003096")


@pytest.mark.parametrize(
    "raw",
    [
        page(headers=["年份", "除息日", "权益登记日", "每10份分红", "分红发放日"]),
        page(cells=["2024", "2024-08-23"]),
    ],
)
def test_unknown_or_reordered_columns_block(raw):
    with pytest.raises(ValueError, match="COLUMNS_UNVERIFIED"):
        parse(raw, "channel_div", "003096")


@pytest.mark.parametrize(
    "day, expected", [("2023-01-01", 10), ("2023-01-02", 10), ("2023-01-04", 20)]
)
def test_distribution_boundary_start_exclusive_end_inclusive(day, expected):
    data = payload()
    data["products"][0]["distributions"] = [dict(ex_date=day, cash_per_unit="0.1")]
    result = evaluate(PeerStudy.model_validate(data))["rows"][0]["windows"][0]["calculated"]
    assert result["return_pct"] == pytest.approx(expected)


def test_latest_limitations_do_not_mutate_history_or_replay(tmp_path):
    research, peers, _ = services(tmp_path)
    first = peers.read("003096", details=True)
    request = deepcopy(first["archived_input"])
    request.update(expected_previous_version=1, idempotency_key="limitation-v2")
    request["limitations"] = ["WARNING: disputed distribution; formal disclosure pending"]
    peers.archive(PeerStudy.model_validate(request))
    before = dump(research.settings.db_path)
    original = dict(
        display_text="Archive unchanged", input_hash="same", baseline={"id": 1}, delta={"NEW": 0}
    )
    snapshot = deepcopy(original)
    result = attach_peer_limitations(research, original, {"003096"})
    assert original == snapshot
    assert result["delta"] == original["delta"] and result["input_hash"] == "same"
    assert result["current_peer_limitations"][0]["version"] == 2
    assert "disputed distribution" in result["display_text"]
    assert result == attach_peer_limitations(research, original, {"003096"})
    assert not attach_peer_limitations(research, original, {"OTHER"})["current_peer_limitations"]
    assert dump(research.settings.db_path) == before


def test_http_current_notices_leave_baseline_and_business_unchanged(tmp_path):
    import json

    from fastapi.testclient import TestClient
    from test_holding_review import capture, setup

    from investor_core.api.app import create_app
    from investor_core.peer_research import PeerResearchService

    settings, pid, aid, _, reviews, _ = setup(tmp_path)
    reviews.capture(capture(reviews, pid, aid))
    data = json.loads(json.dumps(payload()).replace('"R"', '"CORE01"'))
    data["limitations"] = ["WARNING: unresolved dividend evidence; do not clear on disappearance"]
    PeerResearchService(reviews.research).archive(PeerStudy.model_validate(data))
    before = dump(settings.db_path)
    query = dict(portfolio_id=pid, account_id=aid)
    client = TestClient(create_app(settings))
    for endpoint in [
        "holding-review",
        "holding-review-delta",
        "portfolio-research-summary",
        "portfolio-research-coverage",
    ]:
        response = client.get("/v1/" + endpoint, params=query)
        assert response.status_code == 200, response.text
        result = response.json()["data"]
        assert "unresolved dividend evidence" in result["display_text"], endpoint
        assert result["current_peer_limitations"][0]["instrument_codes"] == ["CORE01"]
    assert dump(settings.db_path) == before


def test_formal_exclusion_has_exact_windows_and_preserves_unrelated_warnings(tmp_path):
    from investor_core.research_read_notices import dividend_judgments

    research, peers, _ = services(tmp_path)
    old = peers.read("003096", details=True)
    request = deepcopy(old["archived_input"])
    request.update(expected_previous_version=1, idempotency_key="formal-exclusion")
    source = (
        deepcopy(request["sources"]["003096"])
        if "003096" in request["sources"]
        else deepcopy(next(iter(request["sources"].values())))
    )
    source.update(
        instrument_code="003096",
        quality="OFFICIAL",
        facts=dict(
            claim="2023-01-03 cash_per_unit=0.057",
            treatment="EXCLUDED_BY_FORMAL_ANNUAL_DISCLOSURE",
            source_origin_cause="UNKNOWN",
        ),
    )
    request["sources"]["formal-report"] = source
    request["limitations"] = [
        "five-year reconciliation warning",
        "single-source warning",
        "independent upstream unknown",
    ]
    peers.archive(PeerStudy.model_validate(request))
    before = dump(research.settings.db_path)
    result = attach_peer_limitations(
        research,
        {"display_text": "old unresolved statement", "rows": old["rows"]},
        {"003096"},
        historical=True,
    )
    notice = result["current_peer_limitations"][0]
    judgement = notice["evidence_judgments"][0]
    assert judgement["windows"][0]["crosses_disputed_date"]
    assert not judgement["windows"][-1]["crosses_disputed_date"]
    assert all(not w["calculation_restricted_by_this_claim"] for w in judgement["windows"])
    assert not judgement["other_warnings_cleared"]
    assert notice["limitations"] == request["limitations"]
    assert result["display_text"].startswith("历史研究原文")
    assert (
        "已依据正式年报排除" in result["display_text"]
        and "渠道异常原因未知" in result["display_text"]
    )
    assert result["rows"] == old["rows"]
    source["quality"] = "UNVERIFIED"
    assert not dividend_judgments(request, evaluate(PeerStudy.model_validate(request)))
    assert dump(research.settings.db_path) == before
