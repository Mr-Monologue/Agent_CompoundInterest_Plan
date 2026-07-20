from __future__ import annotations

from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings


def test_health_is_process_only(tmp_path: Path) -> None:
    settings = Settings(environment=Environment.TEST, db_path=tmp_path / "missing.db")
    response = TestClient(create_app(settings)).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_after_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)

    response = TestClient(create_app(settings)).get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] in {"PASS", "DEGRADED"}
    checks = {item["name"]: item for item in response.json()["checks"]}
    assert checks["sqlite-integrity"]["status"] == "PASS"
    assert checks["sqlite-wal"]["status"] == "PASS"
    assert checks["database-schema"]["status"] == "PASS"


def test_ready_fails_before_migration(tmp_path: Path) -> None:
    settings = Settings(environment=Environment.TEST, db_path=tmp_path / "missing.db")

    response = TestClient(create_app(settings)).get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "FAIL"


def test_ready_fails_when_business_timezone_is_unavailable(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(
        environment=Environment.TEST,
        db_path=database_path,
        timezone="Missing/Timezone",
    )

    response = TestClient(create_app(settings)).get("/ready")

    assert response.status_code == 503
    checks = {item["name"]: item for item in response.json()["detail"]["checks"]}
    assert checks["business-timezone"]["status"] == "FAIL"


def test_transaction_draft_api_requires_commit_before_holding_changes(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
    client = TestClient(create_app(settings))

    portfolio = client.post(
        "/v1/portfolios", json={"name": "测试组合", "base_currency": "CNY"}
    ).json()["data"]
    account = client.post(
        "/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "name": "测试账户",
            "platform": "模拟平台",
        },
    ).json()["data"]
    instrument = client.post(
        "/v1/instruments",
        json={"code": "DEMO001", "name": "模拟基金", "role": "CORE"},
    )
    assert instrument.status_code == 200

    draft_response = client.post(
        "/v1/transaction-drafts",
        json={
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "instrument_code": "DEMO001",
            "side": "BUY",
            "trade_date": "2026-07-20",
            "amount": "100.00",
            "nav": "1.250000",
            "shares": "80.000000",
            "platform": "模拟平台",
            "idempotency_key": "api-message-001",
        },
    )
    assert draft_response.status_code == 200
    draft_result = draft_response.json()["data"]
    assert draft_result["draft"]["status"] == "PENDING"
    assert client.get("/v1/holdings").json()["data"]["items"] == []

    commit_response = client.post(
        f"/v1/transaction-drafts/{draft_result['draft']['id']}/commit",
        json={
            "confirmation_token": draft_result["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    assert commit_response.status_code == 200
    commit_result = commit_response.json()["data"]
    assert commit_result["transaction"]["side"] == "BUY"
    assert commit_result["holding"]["total_shares"] == "80.000000"
    assert commit_result["holding"]["cost_amount"] == "100.00"


def test_opening_position_api_uses_a_dedicated_confirmed_import_path(tmp_path: Path) -> None:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    settings = Settings(environment=Environment.TEST, db_path=database_path)
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
        json={
            "code": "022463",
            "name": "富国中证A500ETF发起式联接A",
            "asset_type": "FUND",
            "role": "CORE",
        },
    )

    draft_response = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio["id"],
            "account_id": account["id"],
            "instrument_code": "022463",
            "as_of_date": "2026-07-20",
            "total_shares": "123.910000",
            "average_cost_nav": "1.9904",
            "platform": "支付宝",
            "idempotency_key": "api-opening-001",
            "note": "支付宝持仓页",
        },
    )
    assert draft_response.status_code == 200
    draft_result = draft_response.json()["data"]
    assert draft_result["draft"]["action"] == "OPENING"
    assert draft_result["draft"]["cost_amount"] == "246.63"
    assert draft_result["draft"]["average_cost_nav"] == "1.990400"
    assert draft_result["cost_basis_input"] == "AVERAGE_COST_NAV"
    assert client.get("/v1/holdings").json()["data"]["items"] == []

    wrong_commit = client.post(
        f"/v1/transaction-drafts/{draft_result['draft']['id']}/commit",
        json={
            "confirmation_token": draft_result["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    assert wrong_commit.status_code == 409
    assert wrong_commit.json()["error"]["code"] == "DRAFT_TYPE_MISMATCH"

    commit_response = client.post(
        f"/v1/opening-position-drafts/{draft_result['draft']['id']}/commit",
        json={
            "confirmation_token": draft_result["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    assert commit_response.status_code == 200
    commit_result = commit_response.json()["data"]
    assert commit_result["transaction"]["kind"] == "OPENING"
    assert commit_result["holding"]["total_shares"] == "123.910000"
    assert commit_result["holding"]["cost_amount"] == "246.63"
    assert commit_result["holding"]["average_cost_nav"] == "1.990400"


def test_opening_position_api_requires_exactly_one_cost_basis(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            Settings(environment=Environment.TEST, db_path=tmp_path / "not-used.db")
        )
    )
    payload = {
        "portfolio_id": "portfolio-1",
        "account_id": "account-1",
        "instrument_code": "005827",
        "as_of_date": "2026-07-17",
        "total_shares": "123.91",
        "platform": "支付宝",
        "idempotency_key": "invalid-opening",
    }

    missing = client.post("/v1/opening-position-drafts", json=payload)
    both = client.post(
        "/v1/opening-position-drafts",
        json={**payload, "cost_amount": "246.63", "average_cost_nav": "1.9904"},
    )

    assert missing.status_code == 422
    assert both.status_code == 422
