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
        "instrument_create",
        "instrument_list",
        "market_nav_snapshot_record",
        "market_nav_snapshot_list",
        "portfolio_valuation_get",
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
            "022463",
            "2026-07-20",
            "100.000000",
            "支付宝",
            "opening-message-1",
            average_cost_nav="1.9904",
            note="支付宝持仓页",
            portfolio_id="portfolio-1",
            account_id="account-1",
        )
    )
    asyncio.run(
        server.opening_position_draft_commit("draft-1", "token-1", "user-1")
    )

    assert calls == [
        (
            "POST",
            "/v1/opening-position-drafts",
            {
                "portfolio_id": "portfolio-1",
                "account_id": "account-1",
                "instrument_code": "022463",
                "as_of_date": "2026-07-20",
                "total_shares": "100.000000",
                "cost_amount": None,
                "average_cost_nav": "1.9904",
                "platform": "支付宝",
                "idempotency_key": "opening-message-1",
                "note": "支付宝持仓页",
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
                    "account": {"id": "account-default", "name": "支付宝基金账户"},
                },
            }
        return {"ok": True}

    monkeypatch.setattr(server, "core_request", fake_core_request)

    asyncio.run(
        server.opening_position_draft_create(
            "000032",
            "2026-07-20",
            "32.79",
            "支付宝",
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
    asyncio.run(server.account_create("portfolio-1", "支付宝基金账户", "支付宝"))
    asyncio.run(server.instrument_create("000510", "中证A500", "INDEX", role="CORE"))
    asyncio.run(
        server.instrument_create(
            "022463", "富国中证A500ETF发起式联接A", "FUND", role="CORE"
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
                "name": "支付宝基金账户",
                "platform": "支付宝",
                "currency": "CNY",
                "actor_ref": "hermes",
            },
        ),
        (
            "POST",
            "/v1/instruments",
            {
                "code": "000510",
                "name": "中证A500",
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
                "code": "022463",
                "name": "富国中证A500ETF发起式联接A",
                "asset_type": "FUND",
                "currency": "CNY",
                "role": "CORE",
                "actor_ref": "hermes",
            },
        ),
    ]
