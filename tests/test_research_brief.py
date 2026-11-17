from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest

from investor_core import daily_client
from investor_core.research_brief import PublicResearch, metrics, valid_points

START = date(2026, 11, 13)
END = date(2026, 11, 17)
NOW = datetime(2026, 11, 17, 19, tzinfo=ZoneInfo("Asia/Shanghai"))


def transport(*, conflict=False, fail_primary=False, fail_all=False):
    def handle(request):
        assert request.method == "GET"
        if fail_all or (fail_primary and "eastmoney" in request.url.host):
            return httpx.Response(503)
        if "push2his" in request.url.host:
            code = request.url.params["secid"].split(".")[1]
            return httpx.Response(
                200,
                json={
                    "rc": 0,
                    "data": {
                        "code": code,
                        "klines": [
                            "2026-11-13,100,100,101,99,10,100",
                            "2026-11-16,100,101,102,100,10,100",
                            "2026-11-17,101,99,102,98,10,100",
                        ],
                    },
                },
            )
        if "sina" in request.url.host:
            return httpx.Response(
                200,
                json=[
                    {"day": "2026-11-13", "close": "100"},
                    {"day": "2026-11-16", "close": "101"},
                    {"day": "2026-11-17", "close": "98" if conflict else "99.001"},
                ],
            )
        return httpx.Response(200, text='var fS_code="999999";')

    return httpx.MockTransport(handle)


def test_fetch_compute_and_source_comparison():
    report = PublicResearch(transport()).run(START, END, [], now=NOW)
    assert len(report["indices"]) == 3
    assert all(r["quality"] == "TWO_CHANNELS_AGREE" for r in report["indices"])
    assert report["indices"][0]["period_return_pct"] == "-1.00"
    assert "共同下跌" in report["display_text"]
    assert report["portfolio"] is None


def test_source_conflict_excluded_from_conclusion():
    report = PublicResearch(transport(conflict=True)).run(START, END, [], now=NOW)
    assert all(r["status"] == "SOURCE_CONFLICT" for r in report["indices"])
    assert "共同下跌" not in report["display_text"]


def test_fallback_and_partial_failure_preserve_research():
    report = PublicResearch(transport(fail_primary=True)).run(START, END, ["022463"], now=NOW)
    assert report["indices"][0]["selected_provider"] == "SINA"
    assert report["indices"][0]["quality"] == "WARNING"
    assert report["funds"][0]["status"] == "UNAVAILABLE"
    assert report["indices"][0]["fetch_errors"]


def test_all_failed_not_a_no_risk_conclusion():
    report = PublicResearch(transport(fail_all=True)).run(START, END, [], now=NOW)
    assert all(r["status"] == "UNAVAILABLE" for r in report["indices"])
    assert "共同上涨" not in report["display_text"]


def test_percentile_excludes_latest_and_missing_endpoint_not_substituted():
    first = date(2025, 1, 1)
    points = {first + timedelta(days=i): Decimal("100") for i in range(130)}
    last = max(points)
    points[last] = Decimal("90")
    result = metrics(points, first - timedelta(days=1), last)
    assert result["absolute_move_percentile"] == "100.0"
    assert result["history_sample_size"] == 128
    assert result["period_return_pct"] is None
    assert result["missing_endpoints"] == ["2024-12-31"]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1"])
def test_invalid_prices_rejected(value):
    with pytest.raises(ValueError):
        valid_points([(START, Decimal(value))])


def test_intraday_and_invalid_code_rejected_before_network():
    public = PublicResearch(transport(fail_all=True))
    with pytest.raises(ValueError, match="16:00"):
        public.run(START, END, [], now=NOW.replace(hour=10))
    with pytest.raises(ValueError, match="six digits"):
        public.run(START, END, ["../secret"], now=NOW)


def test_cli_core_read_only_and_fund_selection(monkeypatch, capsys):
    calls = []

    class Core:
        def __init__(self, *args, **kwargs):
            pass

        def scope(self):
            return {"portfolio_id": "synthetic", "account_id": "synthetic"}

        def get(self, path, **kwargs):
            calls.append(path)
            return {"valuation": {"positions": [{"holding": {"instrument_code": "022463"}}]}}

        def close(self):
            pass

    monkeypatch.setattr(daily_client, "DailyClient", Core)
    monkeypatch.setattr(
        "investor_core.research_brief.PublicResearch", lambda: PublicResearch(transport())
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "investor-assistant",
            "--json",
            "research-brief",
            "--start",
            "2026-11-13",
            "--end",
            "2026-11-16",
        ],
    )
    daily_client.main()
    report = json.loads(capsys.readouterr().out)
    assert calls == ["/v1/portfolio-brief"]
    assert report["funds"][0]["code"] == "022463"
    assert report["portfolio"] is not None


def test_same_horizon_rank_does_not_include_evaluation_period():
    start = date(2025, 1, 1)
    points = {start + timedelta(days=i): Decimal(100) for i in range(260)}
    end = max(points)
    points[end - timedelta(days=1)] = Decimal(90)
    points[end] = Decimal(80)
    result = metrics(points, end - timedelta(days=2), end)
    assert result["period_observation_intervals"] == 2
    assert result["period_history_sample_size"] == 252
    assert result["period_absolute_move_percentile"] == "100.0"
    assert result["period_return_pct"] == "-20.00"


def test_empty_eligible_primary_falls_back():
    base = transport()

    def handle(request):
        if "push2his" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "rc": 0,
                    "data": {
                        "code": request.url.params["secid"].split(".")[1],
                        "klines": ["2099-01-01,100,100,100,100,1,1"],
                    },
                },
            )
        return base.handle_request(request)

    result = PublicResearch(httpx.MockTransport(handle)).index("sh000001", START, END)
    assert result["selected_provider"] == "SINA"
    assert result["fetch_errors"][0]["provider"] == "EASTMONEY"


def test_public_live_snapshot_can_reproduce_published_statistics():
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "examples/research/live-index-evidence-2026-11-17.json"
    )
    report = json.loads(path.read_text(encoding="utf-8"))
    for row in report["indices"]:
        points = {date.fromisoformat(p["date"]): Decimal(p["close"]) for p in row["observations"]}
        recalculated = metrics(
            points,
            date.fromisoformat(report["period_start"]),
            date.fromisoformat(report["period_end"]),
        )
        for key in (
            "period_return_pct",
            "absolute_move_percentile",
            "period_absolute_move_percentile",
            "return_20_observations_pct",
        ):
            assert recalculated[key] == row[key]


def test_fund_uses_actual_nav_dates_and_never_calls_nav_change_total_return():
    def handle(request):
        points = [
            {
                "x": int(
                    datetime(2026, 11, day, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
                ),
                "y": value,
            }
            for day, value in ((13, 1.0), (16, 0.95))
        ]
        return httpx.Response(
            200,
            text='var fS_code="022463";var fS_name="synthetic";'
            "var Data_netWorthTrend=" + json.dumps(points) + ";",
        )

    result = PublicResearch(httpx.MockTransport(handle)).fund("022463", START, END)
    assert result["status"] == "OBSERVED"
    assert result["latest_date"] == "2026-11-16"
    assert result["period_return_pct"] is None
    assert result["basis"] == "UNIT_NAV_NOT_TOTAL_RETURN"
    assert "absolute_move_percentile" not in result
    assert "period_absolute_move_percentile" not in result
