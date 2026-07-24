from __future__ import annotations

import asyncio
from typing import Any

from investor_mcp import server
from investor_mcp.server import mcp


def test_phase1_mcp_exposes_guarded_ledger_tools() -> None:
    tools = asyncio.run(mcp.list_tools())

    assert [tool.name for tool in tools] == [
        "system_health_get",
        "portfolio_create",
        "portfolio_list",
        "account_create",
        "account_list",
        "investment_context_get",
        "investment_context_set",
        "allocation_policy_get",
        "allocation_policy_set",
        "instrument_create",
        "instrument_list",
        "instrument_role_update",
        "market_nav_snapshot_record",
        "market_nav_snapshot_list",
        "market_data_canary_run",
        "market_data_status_get",
        "market_data_sync",
        "market_nav_verification_record",
        "market_nav_verification_list",
        "portfolio_valuation_get",
        "portfolio_brief_get",
        "holding_list",
        "opening_position_draft_create",
        "transaction_list",
        "transaction_draft_get",
        "transaction_draft_create",
        "transaction_reversal_draft_create",
        "transaction_draft_commit",
        "opening_position_draft_commit",
    ]


def test_opening_position_tools_use_dedicated_core_paths(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def fake_core_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del params
        calls.append((method, path, payload))
        return {"ok": True}

    monkeypatch.setattr(server, "core_request", fake_core_request)
    asyncio.run(
        server.opening_position_draft_create(
            "FUND007",
            "2026-07-20",
            "100.000000",
            "测试平台",
            "opening-message-1",
            average_cost_nav="1.250000",
            note="测试持仓页",
            portfolio_id="portfolio-1",
            account_id="account-1",
        )
    )
    asyncio.run(server.opening_position_draft_commit("draft-1", "token-1", "user-1"))

    assert calls == [
        (
            "POST",
            "/v1/opening-position-drafts",
            {
                "portfolio_id": "portfolio-1",
                "account_id": "account-1",
                "instrument_code": "FUND007",
                "as_of_date": "2026-07-20",
                "total_shares": "100.000000",
                "cost_amount": None,
                "average_cost_nav": "1.250000",
                "platform": "测试平台",
                "idempotency_key": "opening-message-1",
                "note": "测试持仓页",
                "actor_ref": "hermes",
            },
        ),
        (
            "POST",
            "/v1/opening-position-drafts/draft-1/commit",
            {"confirmation_token": "token-1", "confirmed_by": "user-1"},
        ),
    ]


def test_opening_position_uses_default_context_when_ids_are_omitted(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def fake_core_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del params
        calls.append((method, path, payload))
        if path == "/v1/investment-context":
            return {
                "ok": True,
                "data": {
                    "portfolio": {"id": "portfolio-default", "name": "个人投资组合"},
                    "account": {"id": "account-default", "name": "测试账户"},
                },
            }
        return {"ok": True}

    monkeypatch.setattr(server, "core_request", fake_core_request)

    asyncio.run(
        server.opening_position_draft_create(
            "FUND002",
            "2026-07-20",
            "32.79",
            "测试平台",
            "opening-default-context",
            average_cost_nav="1.1131",
        )
    )

    assert calls[0] == ("GET", "/v1/investment-context", None)
    assert calls[1][1] == "/v1/opening-position-drafts"
    assert calls[1][2] is not None
    assert calls[1][2]["portfolio_id"] == "portfolio-default"
    assert calls[1][2]["account_id"] == "account-default"


def test_setup_tools_send_guarded_idempotent_core_payloads(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def fake_core_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del params
        calls.append((method, path, payload))
        return {"ok": True}

    monkeypatch.setattr(server, "core_request", fake_core_request)

    asyncio.run(server.portfolio_create("个人投资组合"))
    asyncio.run(server.account_create("portfolio-1", "测试账户", "测试平台"))
    asyncio.run(server.instrument_create("INDEX001", "测试指数", "INDEX", role="CORE"))
    asyncio.run(server.instrument_create("FUND007", "测试基金G", "FUND", role="CORE"))
    asyncio.run(
        server.instrument_role_update(
            "FUND007",
            "SATELLITE",
            "CORE",
            "用户明确将该标的归入卫星角色",
        )
    )

    assert calls == [
        (
            "POST",
            "/v1/portfolios",
            {"name": "个人投资组合", "base_currency": "CNY", "actor_ref": "hermes"},
        ),
        (
            "POST",
            "/v1/accounts",
            {
                "portfolio_id": "portfolio-1",
                "name": "测试账户",
                "platform": "测试平台",
                "currency": "CNY",
                "actor_ref": "hermes",
            },
        ),
        (
            "POST",
            "/v1/instruments",
            {
                "code": "INDEX001",
                "name": "测试指数",
                "asset_type": "INDEX",
                "currency": "CNY",
                "role": "CORE",
                "actor_ref": "hermes",
            },
        ),
        (
            "POST",
            "/v1/instruments",
            {
                "code": "FUND007",
                "name": "测试基金G",
                "asset_type": "FUND",
                "currency": "CNY",
                "role": "CORE",
                "actor_ref": "hermes",
            },
        ),
        (
            "PATCH",
            "/v1/instruments/FUND007/role",
            {
                "role": "SATELLITE",
                "expected_current_role": "CORE",
                "reason": "用户明确将该标的归入卫星角色",
                "actor_ref": "hermes",
            },
        ),
    ]


def test_market_data_sync_resolves_context_and_current_holding_codes(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None, float]] = []

    async def fake_core_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        calls.append((method, path, params, payload, timeout_seconds))
        if path == "/v1/investment-context":
            return {
                "ok": True,
                "data": {
                    "portfolio": {"id": "portfolio-default", "name": "个人投资组合"},
                    "account": {"id": "account-default", "name": "测试账户"},
                },
            }
        if path == "/v1/holdings":
            return {
                "ok": True,
                "data": {
                    "items": [
                        {"instrument_code": "FUND001"},
                        {"instrument_code": "FUND002"},
                        {"instrument_code": "FUND001"},
                    ]
                },
            }
        return {"ok": True}

    monkeypatch.setattr(server, "core_request", fake_core_request)

    result = asyncio.run(server.market_data_sync(as_of_date="2026-07-21"))

    assert result == {"ok": True}
    assert calls[0][1] == "/v1/investment-context"
    assert calls[1][1] == "/v1/holdings"
    assert calls[2] == (
        "POST",
        "/v1/market-data/sync",
        None,
        {
            "provider_id": "AKSHARE_OPEN_FUND",
            "instrument_codes": ["FUND001", "FUND002"],
            "as_of_date": "2026-07-21",
            "actor_ref": "hermes",
        },
        120.0,
    )


def test_market_nav_verification_records_external_tool_evidence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def fake_core_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        del params, timeout_seconds
        calls.append((method, path, payload))
        return {"ok": True}

    monkeypatch.setattr(server, "core_request", fake_core_request)

    result = asyncio.run(
        server.market_nav_verification_record(
            instrument_code="FUND001",
            nav_date="2026-07-21",
            nav="1.534500",
            source_type="PLATFORM",
            source_name="Independent professional platform",
            source_ref="professional:FUND001:2026-07-21",
            source_lineage="WIND",
            observed_at="2026-07-21T22:05:00+08:00",
        )
    )

    assert result == {"ok": True}
    assert calls == [
        (
            "POST",
            "/v1/market-data/verifications",
            {
                "instrument_code": "FUND001",
                "nav_date": "2026-07-21",
                "nav": "1.534500",
                "currency": "CNY",
                "source_type": "PLATFORM",
                "source_name": "Independent professional platform",
                "source_ref": "professional:FUND001:2026-07-21",
                "source_lineage": "WIND",
                "observed_at": "2026-07-21T22:05:00+08:00",
                "actor_ref": "hermes",
            },
        )
    ]


def test_portfolio_brief_uses_default_context(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def fake_core_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        del payload, timeout_seconds
        calls.append((method, path, params))
        if path == "/v1/investment-context":
            return {
                "ok": True,
                "data": {
                    "portfolio": {"id": "portfolio-default"},
                    "account": {"id": "account-default"},
                },
            }
        return {"ok": True}

    monkeypatch.setattr(server, "core_request", fake_core_request)
    result = asyncio.run(server.portfolio_brief_get(as_of_date="2026-07-22"))

    assert result == {"ok": True}
    assert calls[-1] == (
        "GET",
        "/v1/portfolio-brief",
        {
            "portfolio_id": "portfolio-default",
            "account_id": "account-default",
            "as_of_date": "2026-07-22",
        },
    )
