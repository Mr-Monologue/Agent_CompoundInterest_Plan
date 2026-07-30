from __future__ import annotations

from datetime import date
from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings
from investor_core.performance import PerformanceService


def _client(tmp_path: Path) -> tuple[TestClient, Settings, str]:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    client = TestClient(create_app(settings))
    portfolio = client.post("/v1/portfolios", json={"name": "研究组合"}).json()["data"]
    client.post(
        "/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "name": "研究账户",
            "platform": "测试平台",
        },
    )
    client.post(
        "/v1/instruments",
        json={"code": "FUND001", "name": "公共候选基金", "asset_type": "FUND"},
    )
    for index in range(130):
        nav_date = date(2026, 1, 1).fromordinal(date(2026, 1, 1).toordinal() + index)
        response = client.post(
            "/v1/market-nav-snapshots",
            json={
                "instrument_code": "FUND001",
                "nav_date": nav_date.isoformat(),
                "nav": f"{1 + index / 1000:.6f}",
                "source_type": "OFFICIAL",
                "source_name": "基金管理人",
                "source_ref": "https://official.example/fund001",
                "source_lineage": "FUND_MANAGER_OFFICIAL",
                "verification_status": "VERIFIED",
                "observed_at": f"{nav_date.isoformat()}T18:00:00+08:00",
            },
        )
        assert response.status_code == 200
    return client, settings, str(portfolio["id"])


def test_sourced_research_and_discovery_are_immutable_facts(tmp_path: Path) -> None:
    client, _settings, portfolio_id = _client(tmp_path)
    evidence_payload = {
        "instrument_code": "FUND001",
        "evidence_date": "2026-05-10",
        "evidence_type": "FEES",
        "source_name": "基金管理人",
        "source_ref": "https://official.example/fund001/fees",
        "source_lineage": "FUND_MANAGER_OFFICIAL",
        "facts": {"management_fee_bps": 50},
    }
    first = client.post("/v1/market-research-evidence", json=evidence_payload)
    replay = client.post("/v1/market-research-evidence", json=evidence_payload)

    assert first.status_code == 200
    assert first.json()["data"]["automatic_trade"] is False
    assert replay.json()["data"]["idempotent_replay"] is True

    payload = {
        "portfolio_id": portfolio_id,
        "instrument_codes": ["FUND001"],
        "as_of_date": "2026-05-10",
        "lookback_days": 180,
    }
    scan = client.post("/v1/market-discovery-runs", json=payload)
    scan_replay = client.post("/v1/market-discovery-runs", json=payload)

    assert scan.status_code == 200
    data = scan.json()["data"]
    assert data["items"][0]["state"] == "REVIEW"
    assert data["items"][0]["return_20d_bps"] is not None
    assert data["items"][0]["return_60d_bps"] is not None
    assert data["items"][0]["return_120d_bps"] is not None
    assert data["items"][0]["selection_boundary"] == "FACTS_ONLY_NOT_A_RECOMMENDATION"
    assert data["strategy_changed"] is False
    assert data["contribution_eligibility_changed"] is False
    assert data["automatic_trade"] is False
    assert scan_replay.json()["data"]["idempotent_replay"] is True


def test_discovery_requires_explicit_registered_universe(tmp_path: Path) -> None:
    client, _settings, portfolio_id = _client(tmp_path)
    response = client.post(
        "/v1/market-discovery-runs",
        json={
            "portfolio_id": portfolio_id,
            "instrument_codes": ["UNKNOWN"],
            "as_of_date": "2026-05-10",
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DISCOVERY_INSTRUMENT_NOT_FOUND"


def test_review_action_requires_confirmed_decision(tmp_path: Path) -> None:
    client, settings, portfolio_id = _client(tmp_path)
    # A temporary opening balance makes the existing performance review produce
    # deterministic action items while keeping market discovery independent.
    account_id = client.get("/v1/accounts").json()["data"]["items"][0]["id"]
    opening = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": "FUND001",
            "as_of_date": "2026-01-01",
            "total_shares": "10",
            "average_cost_nav": "1",
            "platform": "测试平台",
            "idempotency_key": "research-review-opening",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{opening['draft']['id']}/commit",
        json={
            "confirmation_token": opening["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    review = PerformanceService(settings).prepare_review(
        portfolio_id=portfolio_id,
        review_type="MONTHLY",
        anchor_date=date(2026, 1, 31),
    )
    action = review["action_items"][0]
    draft = client.post(
        f"/v1/review-action-items/{action['id']}/decision-drafts",
        json={
            "decision": "ACKNOWLEDGE",
            "reason": "已看到该数据缺口, 后续补齐",
        },
    ).json()["data"]

    unchanged = client.get(
        "/v1/periodic-reviews",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]["items"][0]
    assert next(
        item for item in unchanged["action_items"] if item["id"] == action["id"]
    )["status"] == "OPEN"

    committed = client.post(
        f"/v1/review-action-decision-drafts/{draft['draft']['id']}/commit",
        json={
            "confirmation_token": draft["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    assert committed.status_code == 200
    assert committed.json()["data"]["decision"]["new_status"] == "ACKNOWLEDGED"
    assert committed.json()["data"]["holdings_changed"] is False
    assert committed.json()["data"]["transactions_created"] is False
