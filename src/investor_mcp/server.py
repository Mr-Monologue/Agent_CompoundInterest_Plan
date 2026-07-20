"""Guarded MCP adapter for the deterministic local Investor Core.

STDIO logging must never write application messages to stdout because that would
corrupt the MCP JSON-RPC stream.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP

from investor_core.config import get_settings
from investor_mcp.runtime import ensure_core_ready

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("value-dca-investor")


def dependency_error() -> dict[str, Any]:
    return {
        "ok": False,
        "data": {},
        "meta": {"schema_version": "1.0", "data_quality": "SOURCE_ERROR"},
        "warnings": ["Investor Core is unavailable"],
        "error": {"code": "DEPENDENCY_UNAVAILABLE"},
    }


async def core_request(
    method: Literal["GET", "POST"],
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(base_url=settings.core_base_url, timeout=10.0) as client:
                response = await client.request(method, path, params=params, json=payload)
                result = response.json()
                if isinstance(result, dict):
                    return result
                logger.warning("Core returned a non-object JSON response")
                return dependency_error()
        except (httpx.HTTPError, ValueError) as exc:
            if attempt == 0 and await ensure_core_ready(settings):
                logger.info("Investor Core recovered; retrying the MCP request")
                continue
            logger.warning("Core request failed: %s", type(exc).__name__)
            return dependency_error()
    return dependency_error()


async def fetch_core_status(detail_level: Literal["summary", "full"]) -> dict[str, Any]:
    settings = get_settings()
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(base_url=settings.core_base_url, timeout=5.0) as client:
                health_response = await client.get("/health")
                health_response.raise_for_status()
                result: dict[str, Any] = {"health": health_response.json()}
                if detail_level == "full":
                    ready_response = await client.get("/ready")
                    result["ready"] = (
                        ready_response.json()
                        if ready_response.is_success
                        else {"status": "FAIL", "http_status": ready_response.status_code}
                    )
                return result
        except httpx.HTTPError:
            if attempt == 0 and await ensure_core_ready(settings):
                logger.info("Investor Core recovered; retrying the health request")
                continue
            raise
    raise httpx.ConnectError("Investor Core is unavailable")


@mcp.tool()
async def system_health_get(
    detail_level: Literal["summary", "full"] = "summary",
) -> dict[str, Any]:
    """Get Core liveness and optional database readiness without changing state."""
    try:
        data = await fetch_core_status(detail_level)
        return {
            "ok": True,
            "data": data,
            "meta": {"schema_version": "1.0", "data_quality": "PASS"},
            "warnings": [],
        }
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Core health request failed: %s", type(exc).__name__)
        return dependency_error()


@mcp.tool()
async def portfolio_create(name: str, base_currency: str = "CNY") -> dict[str, Any]:
    """Idempotently create a portfolio configuration; this does not change holdings."""
    return await core_request(
        "POST",
        "/v1/portfolios",
        payload={
            "name": name,
            "base_currency": base_currency,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def portfolio_list() -> dict[str, Any]:
    """List configured portfolios without changing state."""
    return await core_request("GET", "/v1/portfolios")


@mcp.tool()
async def account_create(
    portfolio_id: str,
    name: str,
    platform: str,
    currency: str = "CNY",
) -> dict[str, Any]:
    """Idempotently create an account configuration; this does not move money."""
    return await core_request(
        "POST",
        "/v1/accounts",
        payload={
            "portfolio_id": portfolio_id,
            "name": name,
            "platform": platform,
            "currency": currency,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def account_list(portfolio_id: str = "") -> dict[str, Any]:
    """List accounts, optionally restricted to one portfolio."""
    params = {"portfolio_id": portfolio_id} if portfolio_id else None
    return await core_request("GET", "/v1/accounts", params=params)


@mcp.tool()
async def instrument_create(
    code: str,
    name: str,
    asset_type: Literal["FUND", "ETF", "STOCK", "INDEX", "CASH"] = "FUND",
    currency: str = "CNY",
    role: Literal["CORE", "SATELLITE", "UNASSIGNED"] = "UNASSIGNED",
) -> dict[str, Any]:
    """Idempotently register an instrument; INDEX records are non-tradable benchmarks."""
    return await core_request(
        "POST",
        "/v1/instruments",
        payload={
            "code": code,
            "name": name,
            "asset_type": asset_type,
            "currency": currency,
            "role": role,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def instrument_list() -> dict[str, Any]:
    """List instruments registered for local transaction recording."""
    return await core_request("GET", "/v1/instruments")


@mcp.tool()
async def holding_list(portfolio_id: str = "", account_id: str = "") -> dict[str, Any]:
    """List latest deterministic holdings reconstructed from committed records."""
    params: dict[str, Any] = {}
    if portfolio_id:
        params["portfolio_id"] = portfolio_id
    if account_id:
        params["account_id"] = account_id
    return await core_request("GET", "/v1/holdings", params=params or None)


@mcp.tool()
async def opening_position_draft_create(
    portfolio_id: str,
    account_id: str,
    instrument_code: str,
    as_of_date: str,
    total_shares: str,
    platform: str,
    idempotency_key: str,
    cost_amount: str = "",
    average_cost_nav: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Create an old-holding draft with exactly one cost basis; this is not a BUY."""
    return await core_request(
        "POST",
        "/v1/opening-position-drafts",
        payload={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": instrument_code,
            "as_of_date": as_of_date,
            "total_shares": total_shares,
            "cost_amount": cost_amount or None,
            "average_cost_nav": average_cost_nav or None,
            "platform": platform,
            "idempotency_key": idempotency_key,
            "note": note or None,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def transaction_list(
    portfolio_id: str = "", account_id: str = "", limit: int = 100
) -> dict[str, Any]:
    """List committed local transactions and reversals without changing state."""
    params: dict[str, Any] = {"limit": limit}
    if portfolio_id:
        params["portfolio_id"] = portfolio_id
    if account_id:
        params["account_id"] = account_id
    return await core_request("GET", "/v1/transactions", params=params)


@mcp.tool()
async def transaction_draft_get(draft_id: str) -> dict[str, Any]:
    """Read one transaction draft and its status without exposing its token."""
    return await core_request("GET", f"/v1/transaction-drafts/{draft_id}")


@mcp.tool()
async def transaction_draft_create(
    portfolio_id: str,
    account_id: str,
    instrument_code: str,
    side: Literal["BUY", "SELL"],
    trade_date: str,
    amount: str,
    nav: str,
    shares: str,
    platform: str,
    idempotency_key: str,
    note: str = "",
) -> dict[str, Any]:
    """Create an expiring BUY or SELL record draft; this does not change holdings."""
    return await core_request(
        "POST",
        "/v1/transaction-drafts",
        payload={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "instrument_code": instrument_code,
            "side": side,
            "trade_date": trade_date,
            "amount": amount,
            "nav": nav,
            "shares": shares,
            "platform": platform,
            "idempotency_key": idempotency_key,
            "note": note or None,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def transaction_reversal_draft_create(
    transaction_id: str, idempotency_key: str
) -> dict[str, Any]:
    """Create an expiring reversal draft; the original record remains active until commit."""
    return await core_request(
        "POST",
        "/v1/transaction-reversal-drafts",
        payload={
            "transaction_id": transaction_id,
            "idempotency_key": idempotency_key,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def transaction_draft_commit(
    draft_id: str, confirmation_token: str, confirmed_by: str
) -> dict[str, Any]:
    """Commit one matching, unexpired draft to the local ledger after explicit confirmation.

    This records an externally executed transaction. It never sends an order to a broker.
    """
    return await core_request(
        "POST",
        f"/v1/transaction-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def opening_position_draft_commit(
    draft_id: str, confirmation_token: str, confirmed_by: str
) -> dict[str, Any]:
    """Commit one exact opening-position draft after the user explicitly confirms it.

    This imports a historical holding baseline. It never records a BUY or sends an order.
    """
    return await core_request(
        "POST",
        f"/v1/opening-position-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
