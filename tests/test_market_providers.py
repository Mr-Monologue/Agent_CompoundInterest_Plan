from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import httpx
import pytest

from investor_core.config import Settings
from investor_core.ledger import LedgerError
from investor_core.market_providers import AkshareOpenFundProvider, build_provider


def _timestamp_ms(value: str) -> int:
    return round(datetime.fromisoformat(value).timestamp() * 1000)


def _payload() -> str:
    older = _timestamp_ms("2026-07-20T00:00:00+08:00")
    latest = _timestamp_ms("2026-07-21T00:00:00+08:00")
    return (
        "var irrelevant = {x: 1, y: 999};\n"
        f'var Data_netWorthTrend = [{{"x":{older},"y":1.2345,"equityReturn":0}},'
        f'{{"x":{latest},"y":1.2501,"equityReturn":1.2}}];\n'
        "var Data_ACWorthTrend = [];"
    )


def test_direct_parser_reads_only_the_nav_series() -> None:
    observations = AkshareOpenFundProvider._parse_payload(_payload(), "FUND001")

    assert observations == [
        (date(2026, 7, 20), Decimal("1.2345")),
        (date(2026, 7, 21), Decimal("1.2501")),
    ]


def test_direct_parser_rejects_contract_drift() -> None:
    with pytest.raises(LedgerError) as error:
        AkshareOpenFundProvider._parse_payload("var OtherData = [];", "FUND001")

    assert error.value.code == "PROVIDER_CONTRACT_MISMATCH"


def test_fetch_uses_latest_eligible_nav_and_reports_stage_timings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AkshareOpenFundProvider(timeout_seconds=60)
    monkeypatch.setattr(provider, "_download_payload", lambda _code: (_payload(), 2020))

    observation = provider.fetch_nav("FUND001", date(2026, 7, 20))

    assert observation.nav_date == date(2026, 7, 20)
    assert observation.nav == Decimal("1.2345")
    assert observation.timings_ms is not None
    assert observation.timings_ms["download"] == 2020
    assert observation.timings_ms["parse"] >= 0
    assert observation.source_ref.endswith("/FUND001.js")


def test_network_timeout_has_a_specific_provider_error() -> None:
    def raise_timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fixture timeout")

    provider = AkshareOpenFundProvider(
        timeout_seconds=1,
        transport=httpx.MockTransport(raise_timeout),
    )

    with pytest.raises(LedgerError) as error:
        provider.fetch_nav("FUND001", date(2026, 7, 21))

    assert error.value.code == "PROVIDER_TIMEOUT"


def test_default_market_provider_budget_is_sixty_seconds() -> None:
    settings = Settings(_env_file=None)
    provider = build_provider(
        "AKSHARE_OPEN_FUND",
        timeout_seconds=settings.market_provider_timeout_seconds,
    )

    assert settings.market_provider_timeout_seconds == 60
    assert isinstance(provider, AkshareOpenFundProvider)
    assert provider.timeout_seconds == 60
