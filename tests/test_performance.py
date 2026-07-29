from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings
from investor_core.performance import PerformanceService


def _portfolio_with_navs(tmp_path: Path) -> tuple[Settings, str]:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    client = TestClient(create_app(settings))
    portfolio = client.post("/v1/portfolios", json={"name": "绩效组合"}).json()["data"]
    account = client.post(
        "/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "name": "测试账户",
            "platform": "测试平台",
        },
    ).json()["data"]
    client.post("/v1/instruments", json={"code": "FUND001", "name": "测试基金"})
    draft = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "instrument_code": "FUND001",
            "as_of_date": "2026-07-01",
            "total_shares": "100.000000",
            "average_cost_nav": "1.250000",
            "platform": "测试平台",
            "idempotency_key": "performance-opening",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{draft['draft']['id']}/commit",
        json={
            "confirmation_token": draft["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    for nav_date, nav in (("2026-07-01", "1.250000"), ("2026-07-31", "1.500000")):
        response = client.post(
            "/v1/market-nav-snapshots",
            json={
                "instrument_code": "FUND001",
                "nav_date": nav_date,
                "nav": nav,
                "source_type": "OFFICIAL",
                "source_name": "基金公司",
                "source_lineage": "FUND_MANAGER_OFFICIAL",
                "verification_status": "VERIFIED",
                "observed_at": f"{nav_date}T12:00:00+08:00",
            },
        )
        assert response.status_code == 200
    return settings, str(portfolio["id"])


def test_modified_dietz_xirr_and_twr_are_ledger_backed(tmp_path: Path) -> None:
    settings, portfolio_id = _portfolio_with_navs(tmp_path)
    result = PerformanceService(settings).calculate(
        portfolio_id=portfolio_id,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        period_type="MONTHLY",
    )

    assert result["start_value"] == "125.00"
    assert result["end_value"] == "150.00"
    assert result["net_external_flow"] == "0.00"
    assert result["modified_dietz_bps"] == 2000
    assert result["twr_bps"] == 2000
    assert result["xirr_bps"] is not None
    assert result["benchmark_return_bps"] is None
    assert result["data_quality"] == "WARNING"


def test_periodic_review_is_immutable_and_idempotent(tmp_path: Path) -> None:
    settings, portfolio_id = _portfolio_with_navs(tmp_path)
    service = PerformanceService(
        settings,
        now=lambda: datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
    )

    first = service.prepare_review(
        portfolio_id=portfolio_id,
        review_type="MONTHLY",
        anchor_date=date(2026, 7, 31),
    )
    second = service.prepare_review(
        portfolio_id=portfolio_id,
        review_type="MONTHLY",
        anchor_date=date(2026, 7, 31),
    )

    assert first["status"] == "FINALIZED"
    assert first["automatic_trade"] is False
    assert first["revision"] == 1
    assert {item["code"] for item in first["action_items"]} == {
        "BENCHMARK_COVERAGE_REVIEW",
        "DATA_QUALITY_REVIEW",
    }
    assert second["id"] == first["id"]
    assert second["idempotent_replay"] is True
