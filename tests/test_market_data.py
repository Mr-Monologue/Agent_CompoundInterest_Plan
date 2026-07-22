from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings


def _client_with_holding(tmp_path: Path) -> tuple[TestClient, str, str]:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(
        environment=Environment.TEST,
        db_path=database_path,
        market_nav_max_age_days=7,
    )
    client = TestClient(create_app(settings))
    portfolio = client.post("/v1/portfolios", json={"name": "个人投资组合"}).json()[
        "data"
    ]
    account = client.post(
        "/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "name": "支付宝基金账户",
            "platform": "支付宝",
        },
    ).json()["data"]
    client.post(
        "/v1/instruments",
        json={"code": "005827", "name": "易方达蓝筹精选混合"},
    )
    draft = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "instrument_code": "005827",
            "as_of_date": "2026-07-17",
            "total_shares": "123.91",
            "average_cost_nav": "1.9904",
            "platform": "支付宝",
            "idempotency_key": "opening-005827",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{draft['draft']['id']}/commit",
        json={
            "confirmation_token": draft["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    return client, str(portfolio["id"]), str(account["id"])


def test_market_nav_is_idempotent_and_preserves_source_evidence(tmp_path: Path) -> None:
    client, _, _ = _client_with_holding(tmp_path)
    payload = {
        "instrument_code": "005827",
        "nav_date": "2026-07-20",
        "nav": "1.500000",
        "source_type": "PLATFORM",
        "source_name": "支付宝资产详情页",
        "source_ref": "user-screenshot-20260720",
        "verification_status": "VERIFIED",
        "observed_at": "2026-07-21T18:00:00+08:00",
    }

    first = client.post("/v1/market-nav-snapshots", json=payload)
    second = client.post("/v1/market-nav-snapshots", json=payload)

    assert first.status_code == 200
    assert first.json()["data"]["created"] is True
    assert first.json()["meta"]["data_quality"] == "PASS"
    assert second.json()["data"]["created"] is False
    assert first.json()["data"]["snapshot"]["record_hash"] == second.json()["data"][
        "snapshot"
    ]["record_hash"]


def test_portfolio_valuation_is_deterministic_from_holding_and_nav(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    client.post(
        "/v1/market-nav-snapshots",
        json={
            "instrument_code": "005827",
            "nav_date": "2026-07-20",
            "nav": "1.500000",
            "source_type": "PLATFORM",
            "source_name": "支付宝资产详情页",
            "verification_status": "VERIFIED",
            "observed_at": "2026-07-21T18:00:00+08:00",
        },
    )

    response = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["meta"]["data_quality"] == "PASS"
    assert result["data"]["totals"] == {
        "market_value": "185.87",
        "cost_amount": "246.63",
        "unrealized_pnl": "-60.76",
    }
    position = result["data"]["positions"][0]
    assert position["market_value"] == "185.87"
    assert position["return_pct"] == "-24.64"
    assert position["weight_pct"] == "100.00"


def test_missing_or_stale_nav_blocks_portfolio_amount_conclusions(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    missing = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    ).json()
    assert missing["meta"]["data_quality"] == "SOURCE_ERROR"
    assert missing["data"]["totals"] is None
    assert missing["data"]["positions"][0]["market_value"] is None

    client.post(
        "/v1/market-nav-snapshots",
        json={
            "instrument_code": "005827",
            "nav_date": "2026-07-01",
            "nav": "1.500000",
            "source_type": "AGGREGATOR",
            "source_name": "测试聚合源",
            "verification_status": "UNVERIFIED",
            "observed_at": datetime(2026, 7, 1, tzinfo=UTC).isoformat(),
        },
    )
    stale = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    ).json()
    assert stale["meta"]["data_quality"] == "SOURCE_ERROR"
    assert stale["data"]["totals"] is None
    assert stale["data"]["positions"][0]["error"] == "NAV_STALE"


def test_conflicting_latest_navs_block_amount_conclusions(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client_with_holding(tmp_path)
    common = {
        "instrument_code": "005827",
        "nav_date": "2026-07-20",
        "source_type": "PLATFORM",
        "verification_status": "VERIFIED",
        "observed_at": "2026-07-21T18:00:00+08:00",
    }
    client.post(
        "/v1/market-nav-snapshots",
        json={**common, "nav": "1.500000", "source_name": "支付宝"},
    )
    client.post(
        "/v1/market-nav-snapshots",
        json={**common, "nav": "1.510000", "source_name": "基金公司"},
    )

    result = client.get(
        "/v1/portfolio-valuation",
        params={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "as_of_date": "2026-07-21",
        },
    ).json()

    assert result["meta"]["data_quality"] == "SOURCE_ERROR"
    assert result["data"]["totals"] is None
    assert result["data"]["positions"][0]["error"] == "NAV_CONFLICT"
