from __future__ import annotations

from pathlib import Path

from conftest import migrate_database
from fastapi.testclient import TestClient

from investor_core.api.app import create_app
from investor_core.config import Environment, Settings


def _client(tmp_path: Path) -> tuple[TestClient, str, str]:
    database_path = tmp_path / "investor.db"
    migrate_database(database_path)
    client = TestClient(
        create_app(Settings(environment=Environment.TEST, db_path=database_path))
    )
    portfolio = client.post("/v1/portfolios", json={"name": "现金账本组合"}).json()["data"]
    account = client.post(
        "/v1/accounts",
        json={
            "portfolio_id": portfolio["id"],
            "name": "现金账户",
            "platform": "外部平台",
        },
    ).json()["data"]
    client.post("/v1/investment-context", json={
        "portfolio_id": portfolio["id"],
        "account_id": account["id"],
    })
    client.post("/v1/instruments", json={"code": "FUND001", "name": "测试基金"})
    return client, str(portfolio["id"]), str(account["id"])


def _commit_cash(
    client: TestClient,
    *,
    portfolio_id: str,
    account_id: str,
    event_type: str,
    event_date: str,
    amount: str,
    key: str,
) -> dict[str, object]:
    created = client.post(
        "/v1/cash-event-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "event_type": event_type,
            "event_date": event_date,
            "amount": amount,
            "source": "平台资金明细",
            "idempotency_key": key,
        },
    )
    assert created.status_code == 200
    data = created.json()["data"]
    committed = client.post(
        f"/v1/cash-event-drafts/{data['draft']['id']}/commit",
        json={
            "confirmation_token": data["draft"]["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    assert committed.status_code == 200
    return committed.json()["data"]


def test_confirmed_cash_ledger_never_creates_a_trade(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client(tmp_path)
    result = _commit_cash(
        client,
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type="DEPOSIT",
        event_date="2026-07-02",
        amount="100.00",
        key="cash-deposit-1",
    )

    assert result["event"]["signed_amount"] == "100.00"
    assert result["event"]["is_external_flow"] is True
    assert result["event"]["holdings_changed"] is False
    assert result["event"]["transactions_created"] is False
    ledger = client.get(
        "/v1/cash-ledger-events",
        params={"portfolio_id": portfolio_id},
    ).json()["data"]
    assert ledger["cash_balance"] == "100.00"
    assert client.get("/v1/transactions").json()["data"]["items"] == []


def test_official_backfill_is_idempotent_and_enables_l0(tmp_path: Path) -> None:
    client, portfolio_id, account_id = _client(tmp_path)
    opening = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": "FUND001",
            "as_of_date": "2026-07-01",
            "total_shares": "100",
            "average_cost_nav": "1",
            "platform": "外部平台",
            "idempotency_key": "opening-1",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{opening['draft']['id']}/commit",
        json={
            "confirmation_token": opening["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    _commit_cash(
        client,
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type="DEPOSIT",
        event_date="2026-07-01",
        amount="1.00",
        key="cash-coverage",
    )
    payload = {
        "source_name": "测试基金管理人",
        "source_ref": "https://official.example/fund001",
        "source_lineage": "FUND_MANAGER_OFFICIAL",
        "observations": [
            {
                "instrument_code": "FUND001",
                "nav_date": "2026-07-01",
                "nav": "1.000000",
                "observed_at": "2026-07-01T18:00:00+08:00",
            }
        ],
    }
    first = client.post("/v1/official-nav-backfills", json=payload)
    second = client.post("/v1/official-nav-backfills", json=payload)

    assert first.status_code == 200
    assert first.json()["data"]["created_count"] == 1
    assert second.json()["data"]["idempotent_replay"] is True
    mode = client.get(
        "/v1/runtime-mode",
        params={"portfolio_id": portfolio_id, "as_of_date": "2026-07-01"},
    ).json()["data"]
    assert mode["level"] == "L0"
    assert mode["capabilities"]["automatic_trade"] is False


def test_official_backfill_conflict_is_quarantined_and_degrades_runtime(
    tmp_path: Path,
) -> None:
    client, portfolio_id, account_id = _client(tmp_path)
    opening = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": "FUND001",
            "as_of_date": "2026-07-01",
            "total_shares": "100",
            "average_cost_nav": "1",
            "platform": "外部平台",
            "idempotency_key": "opening-conflict",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{opening['draft']['id']}/commit",
        json={
            "confirmation_token": opening["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    _commit_cash(
        client,
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type="DEPOSIT",
        event_date="2026-07-01",
        amount="100.00",
        key="cash-conflict",
    )
    base = {
        "source_name": "基金管理人甲",
        "source_ref": "https://official-a.example/fund001",
        "source_lineage": "FUND_MANAGER_OFFICIAL",
        "observations": [
            {
                "instrument_code": "FUND001",
                "nav_date": "2026-07-01",
                "nav": "1.000000",
                "observed_at": "2026-07-01T18:00:00+08:00",
            }
        ],
    }
    conflict = {
        **base,
        "source_name": "基金管理人乙",
        "source_ref": "https://official-b.example/fund001",
        "observations": [{**base["observations"][0], "nav": "1.100000"}],
    }

    assert client.post("/v1/official-nav-backfills", json=base).status_code == 200
    response = client.post("/v1/official-nav-backfills", json=conflict)

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "CONFLICT"
    assert response.json()["data"]["conflict_count"] == 1
    assert response.json()["data"]["created_count"] == 0
    snapshots = client.get(
        "/v1/market-nav-snapshots",
        params={"instrument_code": "FUND001", "nav_date": "2026-07-01"},
    ).json()["data"]["items"]
    assert len(snapshots) == 1
    assert snapshots[0]["nav"] == "1.000000"
    mode = client.get(
        "/v1/runtime-mode",
        params={"portfolio_id": portfolio_id, "as_of_date": "2026-07-01"},
    ).json()["data"]
    assert mode["level"] == "L2"
    assert mode["reason_code"] == "OFFICIAL_NAV_CONFLICT"
    assert mode["facts"]["official_nav_conflict_codes"] == ["FUND001"]


def test_cash_ledger_enables_daily_linked_twr_with_intraperiod_flow(
    tmp_path: Path,
) -> None:
    client, portfolio_id, account_id = _client(tmp_path)
    opening = client.post(
        "/v1/opening-position-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": "FUND001",
            "as_of_date": "2026-07-01",
            "total_shares": "100",
            "average_cost_nav": "1",
            "platform": "外部平台",
            "idempotency_key": "opening-twr",
        },
    ).json()["data"]
    client.post(
        f"/v1/opening-position-drafts/{opening['draft']['id']}/commit",
        json={
            "confirmation_token": opening["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    _commit_cash(
        client,
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type="DEPOSIT",
        event_date="2026-07-02",
        amount="10.00",
        key="deposit-twr",
    )
    trade = client.post(
        "/v1/transaction-drafts",
        json={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": "FUND001",
            "side": "BUY",
            "trade_date": "2026-07-02",
            "amount": "10.00",
            "nav": "1.100000",
            "shares": "9.090909",
            "platform": "外部平台",
            "idempotency_key": "buy-twr",
        },
    ).json()["data"]
    client.post(
        f"/v1/transaction-drafts/{trade['draft']['id']}/commit",
        json={
            "confirmation_token": trade["confirmation_token"],
            "confirmed_by": "test-user",
        },
    )
    observations = [
        {
            "instrument_code": "FUND001",
            "nav_date": day,
            "nav": nav,
            "observed_at": f"{day}T18:00:00+08:00",
        }
        for day, nav in (
            ("2026-07-01", "1.000000"),
            ("2026-07-02", "1.100000"),
            ("2026-07-03", "1.210000"),
        )
    ]
    client.post(
        "/v1/official-nav-backfills",
        json={
            "source_name": "测试基金管理人",
            "source_ref": "https://official.example/fund001/history",
            "source_lineage": "FUND_MANAGER_OFFICIAL",
            "observations": observations,
        },
    )
    result = client.get(
        "/v1/portfolio-performance",
        params={
            "portfolio_id": portfolio_id,
            "period_start": "2026-07-01",
            "period_end": "2026-07-03",
        },
    ).json()["data"]

    assert result["methodology"]["cash_ledger"] is True
    assert result["methodology"]["buy_sell_cash_convention"] == "INTERNAL_CASH_MOVEMENT"
    assert result["net_external_flow"] == "10.00"
    assert result["twr_bps"] == 2100
    assert len(result["twr_checkpoints"]) == 2
    assert "TWR_UNAVAILABLE_WITH_INTRAPERIOD_FLOWS" not in result["warnings"]
