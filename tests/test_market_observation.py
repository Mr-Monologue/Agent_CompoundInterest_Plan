"""Public-data replay and failure cases for the read-only research entry point."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from investor_core import daily_client
from investor_core.market_observation import ObservationPacket, load_report, observe

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/research/market-week-replay.json"


def packet() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_public_week_replay_distinguishes_week_and_last_session() -> None:
    report = load_report(EXAMPLE)
    assert [row.period_return_pct for row in report.results] == ["1.39", "3.16", "2.14"]
    assert [row.last_session_return_pct for row in report.results] == ["-1.10", "-2.57", "-3.96"]
    assert all("区间上涨" in row.interpretation for row in report.results)
    assert report.quality == "UNVERIFIED"
    assert report.sources[0].accessed_on.isoformat() == "2026-11-09"


def test_missing_endpoint_does_not_forward_fill_or_block_other_series() -> None:
    data = packet()
    data["series"][0]["closes"][0]["date"] = "2026-10-29"
    report = observe(ObservationPacket.model_validate(data))
    assert report.results[0].period_return_pct is None
    assert report.results[0].last_session_return_pct == "-1.10"
    assert str(report.results[0].missing_dates[0]) == "2026-10-30"
    assert report.results[1].period_return_pct == "3.16"


@pytest.mark.parametrize(
    "failure",
    [
        "duplicate",
        "unknown_source",
        "future",
        "zero",
        "nan",
        "source_after_cutoff",
        "intraday_cutoff",
        "duplicate_series",
    ],
)
def test_invalid_evidence_is_rejected(failure: str) -> None:
    data = packet()
    point = data["series"][0]["closes"][0]
    if failure == "duplicate":
        data["series"][0]["closes"].append(point.copy())
    elif failure == "unknown_source":
        point["source_id"] = "invented"
    elif failure == "future":
        point["date"] = "2026-11-10"
    elif failure == "zero":
        point["value"] = "0"
    elif failure == "nan":
        point["value"] = "NaN"
    elif failure == "source_after_cutoff":
        data["sources"][0]["accessed_on"] = "2026-11-10"
    elif failure == "intraday_cutoff":
        data["as_of"] = "2026-11-09T10:00:00+08:00"
    else:
        data["series"].append(data["series"][0])
    with pytest.raises(ValidationError):
        ObservationPacket.model_validate(data)


def test_daily_command_is_offline_and_does_not_initialize_core(monkeypatch, capsys) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("Research replay must not open Core or the request journal")

    monkeypatch.setattr(daily_client, "DailyClient", forbidden)
    monkeypatch.setattr(
        "sys.argv", ["investor-assistant", "--json", "market-observation", str(EXAMPLE)]
    )
    daily_client.main()
    result = json.loads(capsys.readouterr().out)
    assert result["results"][0]["period_return_pct"] == "1.39"


def test_daily_command_reports_invalid_file_without_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["investor-assistant", "market-observation", "absent.json"])
    with pytest.raises(SystemExit) as error:
        daily_client.main()
    assert error.value.code == 2
    assert "Invalid observation packet" in capsys.readouterr().err
