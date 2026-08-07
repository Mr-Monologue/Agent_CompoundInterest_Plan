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
    method: Literal["GET", "POST", "PUT", "PATCH"],
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    settings = get_settings()
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(
                base_url=settings.core_base_url, timeout=timeout_seconds
            ) as client:
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


def context_error(message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "data": {},
        "meta": {"schema_version": "1.0", "data_quality": "PASS"},
        "warnings": [],
        "error": {
            "code": "INVESTMENT_CONTEXT_MISMATCH",
            "message": message,
            "details": details or {},
        },
    }


async def resolve_investment_context(
    portfolio_id: str = "", account_id: str = ""
) -> tuple[str, str, dict[str, Any] | None]:
    """Fill omitted identifiers from the deterministic saved investment context."""
    if portfolio_id and account_id:
        return portfolio_id, account_id, None

    result = await core_request("GET", "/v1/investment-context")
    if not result.get("ok"):
        return "", "", result
    data = result.get("data")
    if not isinstance(data, dict):
        return "", "", context_error("Investor Core returned an invalid context payload")
    portfolio = data.get("portfolio")
    account = data.get("account")
    if not isinstance(portfolio, dict) or not isinstance(account, dict):
        return "", "", context_error("Investor Core returned an incomplete context payload")
    resolved_portfolio_id = str(portfolio.get("id", ""))
    resolved_account_id = str(account.get("id", ""))
    if not resolved_portfolio_id or not resolved_account_id:
        return "", "", context_error("Investor Core returned an incomplete context payload")
    if portfolio_id and portfolio_id != resolved_portfolio_id:
        return (
            "",
            "",
            context_error(
                "the supplied portfolio differs from the saved default context",
                details={"saved_portfolio_name": portfolio.get("name")},
            ),
        )
    if account_id and account_id != resolved_account_id:
        return (
            "",
            "",
            context_error(
                "the supplied account differs from the saved default context",
                details={"saved_account_name": account.get("name")},
            ),
        )
    return resolved_portfolio_id, resolved_account_id, None


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
async def automation_policy_draft_create(
    job_name: Literal[
        "DAILY_MARKET_SYNC",
        "DAILY_RISK_SCAN",
        "WEEKLY_PLAN_PREPARE",
        "SELL_FOLLOWUP_DUE",
        "SYSTEM_DOCTOR",
        "MONTHLY_REVIEW",
        "QUARTERLY_REVIEW",
        "ANNUAL_REVIEW",
        "WEEKLY_MARKET_DISCOVERY",
        "WATCHLIST_REVIEW_DUE",
        "REVIEW_QUALITY_SNAPSHOT",
    ],
    enabled: bool,
    schedule: str,
    reason: str,
    config: dict[str, Any] | None = None,
    timezone: str = "Asia/Shanghai",
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Draft an exact local automation policy; this never runs a job or changes holdings."""
    resolved_portfolio = portfolio_id
    if job_name != "SYSTEM_DOCTOR" and not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/automation-policy-drafts",
        payload={
            "portfolio_id": resolved_portfolio or None,
            "job_name": job_name,
            "enabled": enabled,
            "schedule": schedule,
            "timezone": timezone,
            "config": config or {},
            "reason": reason,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def automation_policy_draft_get(draft_id: str) -> dict[str, Any]:
    """Read an automation policy draft without exposing its confirmation token."""
    return await core_request("GET", f"/v1/automation-policy-drafts/{draft_id}")


@mcp.tool()
async def automation_policy_draft_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Commit one exact automation policy after explicit user confirmation."""
    return await core_request(
        "POST",
        f"/v1/automation-policy-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def automation_policy_list(
    portfolio_id: str = "",
    active_only: bool = True,
) -> dict[str, Any]:
    """List governed automation policies without running any job."""
    return await core_request(
        "GET",
        "/v1/automation-policies",
        params={
            "portfolio_id": portfolio_id or None,
            "active_only": active_only,
        },
    )


@mcp.tool()
async def automation_status_get() -> dict[str, Any]:
    """Get active policies, run counts, due retries, open alerts and pending delivery facts."""
    return await core_request("GET", "/v1/automation-status")


@mcp.tool()
async def automation_scheduler_manifest_get(
    profile: str = "investor",
) -> dict[str, Any]:
    """Get the exact managed Hermes Cron jobs required by approved Core policies."""
    return await core_request(
        "GET",
        "/v1/automation-scheduler-manifest",
        params={"profile": profile},
    )


@mcp.tool()
async def automation_scheduler_snapshot_record(
    profile: str,
    gateway_status: Literal["RUNNING", "STOPPED", "UNKNOWN"],
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Record observed Hermes Cron state after reconciliation; this never edits Cron itself."""
    return await core_request(
        "POST",
        "/v1/automation-scheduler-snapshots",
        payload={
            "profile": profile,
            "gateway_status": gateway_status,
            "jobs": jobs,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def automation_run_list(
    job_name: str = "",
    status: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List deterministic automation run status, retry state and outcomes."""
    return await core_request(
        "GET",
        "/v1/automation-runs",
        params={
            "job_name": job_name or None,
            "status": status or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def automation_missed_run_list(
    grace_minutes: int = 10,
    lookback_days: int = 7,
    limit: int = 100,
) -> dict[str, Any]:
    """List approved schedule windows that are due but have no durable Core run."""
    return await core_request(
        "GET",
        "/v1/automation-missed-runs",
        params={
            "grace_minutes": grace_minutes,
            "lookback_days": lookback_days,
            "limit": limit,
        },
    )


@mcp.tool()
async def automation_report_bundle_list(
    portfolio_id: str = "",
    bundle_type: str = "",
    delivery_action: Literal["", "SILENT", "NOTIFY"] = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Read committed automation fact bundles; SILENT bundles require no user message."""
    return await core_request(
        "GET",
        "/v1/report-bundles",
        params={
            "portfolio_id": portfolio_id or None,
            "bundle_type": bundle_type or None,
            "delivery_action": delivery_action or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def automation_alert_list(
    portfolio_id: str = "",
    status: str = "OPEN",
    limit: int = 100,
) -> dict[str, Any]:
    """List deterministic operational alerts without acknowledging or mutating them."""
    return await core_request(
        "GET",
        "/v1/alerts",
        params={
            "portfolio_id": portfolio_id or None,
            "status": status or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def portfolio_performance_get(
    period_start: str,
    period_end: str,
    period_type: Literal[
        "CUSTOM", "MONTHLY", "QUARTERLY", "ANNUAL", "SINCE_INCEPTION"
    ] = "CUSTOM",
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Calculate ledger-backed performance and benchmark attribution without trading."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/portfolio-performance",
        params={
            "portfolio_id": resolved_portfolio,
            "period_start": period_start,
            "period_end": period_end,
            "period_type": period_type,
        },
    )


@mcp.tool()
async def periodic_review_list(
    review_type: Literal["", "MONTHLY", "QUARTERLY", "ANNUAL"] = "",
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List immutable periodic review facts and open review actions without trading."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/periodic-reviews",
        params={
            "portfolio_id": resolved_portfolio,
            "review_type": review_type or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def review_trend_snapshot_build(
    as_of_date: str,
    review_type: Literal["ALL", "MONTHLY", "QUARTERLY", "ANNUAL"] = "ALL",
    lookback_reviews: int = 12,
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Build an immutable cross-period review trend without changing investments."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/review-trend-snapshots",
        payload={
            "portfolio_id": resolved_portfolio,
            "as_of_date": as_of_date,
            "review_type": review_type,
            "lookback_reviews": lookback_reviews,
        },
    )


@mcp.tool()
async def review_trend_snapshot_list(
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List immutable cross-period review trends and action backlog facts."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/review-trend-snapshots",
        params={"portfolio_id": resolved_portfolio, "limit": limit},
    )


@mcp.tool()
async def review_quality_snapshot_build(
    as_of_date: str,
    lookback_reviews: int = 12,
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Build immutable review-process quality facts, never a strategy score."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/review-quality-snapshots",
        payload={
            "portfolio_id": resolved_portfolio,
            "as_of_date": as_of_date,
            "lookback_reviews": lookback_reviews,
        },
    )


@mcp.tool()
async def review_quality_snapshot_list(
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List immutable review continuity, closure and traceability facts."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/review-quality-snapshots",
        params={"portfolio_id": resolved_portfolio, "limit": limit},
    )


@mcp.tool()
async def market_research_evidence_record(
    instrument_code: str,
    evidence_date: str,
    evidence_type: Literal[
        "FUND_PROFILE",
        "HOLDINGS",
        "MANAGER",
        "FEES",
        "BENCHMARK",
        "MARKET_REGIME",
        "OTHER",
    ],
    source_name: str,
    source_ref: str,
    source_lineage: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    """Record immutable sourced research facts; this never changes strategy or eligibility."""
    return await core_request(
        "POST",
        "/v1/market-research-evidence",
        payload={
            "instrument_code": instrument_code,
            "evidence_date": evidence_date,
            "evidence_type": evidence_type,
            "source_name": source_name,
            "source_ref": source_ref,
            "source_lineage": source_lineage,
            "facts": facts,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def research_source_contract_get(portfolio_id: str = "") -> dict[str, Any]:
    """Read the generic external-research adapter contract and safety boundary."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-source-contract",
        params={"portfolio_id": resolved_portfolio},
    )


@mcp.tool()
async def research_source_config_draft_create(
    connector_key: str,
    display_name: str,
    enabled: bool,
    evidence_types: list[str],
    source_lineages: list[str],
    reason: str,
    credential_ref: str = "",
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Draft a portfolio-local source capability; credential_ref is only an env name."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/research-source-config-drafts",
        payload={
            "portfolio_id": resolved_portfolio,
            "connector_key": connector_key,
            "display_name": display_name,
            "enabled": enabled,
            "evidence_types": evidence_types,
            "source_lineages": source_lineages,
            "credential_ref": credential_ref or None,
            "reason": reason,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def research_source_config_draft_get(draft_id: str) -> dict[str, Any]:
    """Read one exact source configuration draft without exposing a confirmation token."""
    return await core_request("GET", f"/v1/research-source-config-drafts/{draft_id}")


@mcp.tool()
async def research_source_config_draft_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str = "user",
) -> dict[str, Any]:
    """Commit one exact local source capability after explicit confirmation."""
    return await core_request(
        "POST",
        f"/v1/research-source-config-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def research_source_config_list(
    include_disabled: bool = True,
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List current portfolio-local connector capabilities without secrets."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-source-configs",
        params={
            "portfolio_id": resolved_portfolio,
            "include_disabled": include_disabled,
            "limit": limit,
        },
    )


@mcp.tool()
async def research_collection_run_record(
    connector_key: str,
    adapter_version: str,
    source_name: str,
    source_lineage: str,
    started_at: str,
    finished_at: str,
    items: list[dict[str, Any]],
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Record one audited external-research batch with exact per-item outcomes."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/research-collection-runs",
        payload={
            "portfolio_id": resolved_portfolio,
            "connector_key": connector_key,
            "adapter_version": adapter_version,
            "source_name": source_name,
            "source_lineage": source_lineage,
            "started_at": started_at,
            "finished_at": finished_at,
            "items": items,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def research_collection_run_list(
    connector_key: str = "",
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List audited connector batches without inferring recommendations."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-collection-runs",
        params={
            "portfolio_id": resolved_portfolio,
            "connector_key": connector_key or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def research_coverage_snapshot_build(
    instrument_codes: list[str],
    required_evidence_types: list[str],
    as_of_date: str,
    max_age_days: int = 120,
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Build immutable evidence gaps and bounded connector tasks without executing them."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/research-coverage-snapshots",
        payload={
            "portfolio_id": resolved_portfolio,
            "instrument_codes": instrument_codes,
            "required_evidence_types": required_evidence_types,
            "as_of_date": as_of_date,
            "max_age_days": max_age_days,
        },
    )


@mcp.tool()
async def research_coverage_snapshot_list(
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List immutable research evidence-coverage audits without recommendations."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-coverage-snapshots",
        params={"portfolio_id": resolved_portfolio, "limit": limit},
    )


@mcp.tool()
async def research_collection_task_claim(
    connector_key: str,
    adapter_version: str,
    max_tasks: int = 20,
    lease_seconds: int = 300,
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Lease bounded evidence tasks to one configured external connector."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/research-collection-task-claims",
        payload={
            "portfolio_id": resolved_portfolio,
            "connector_key": connector_key,
            "adapter_version": adapter_version,
            "max_tasks": max_tasks,
            "lease_seconds": lease_seconds,
        },
    )


@mcp.tool()
async def research_collection_task_complete(
    claim_id: str,
    claim_token: str,
    collection_run_id: str,
) -> dict[str, Any]:
    """Link one exact audited collection run to a live task claim."""
    return await core_request(
        "POST",
        f"/v1/research-collection-task-claims/{claim_id}/complete",
        payload={
            "claim_token": claim_token,
            "collection_run_id": collection_run_id,
        },
    )


@mcp.tool()
async def research_collection_task_list(
    status: str = "",
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List persisted collection tasks and lease state without executing them."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-collection-tasks",
        params={
            "portfolio_id": resolved_portfolio,
            "status": status or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def research_collection_task_claim_list(
    connector_key: str = "",
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List immutable task claims and completion receipts without claim tokens."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-collection-task-claims",
        params={
            "portfolio_id": resolved_portfolio,
            "connector_key": connector_key or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def research_connector_health_record(
    connector_key: str,
    adapter_version: str,
    observed_at: str,
    state: Literal["HEALTHY", "DEGRADED", "UNAVAILABLE"],
    reason_code: str,
    latency_ms: int | None = None,
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Record one immutable external connector runtime-health receipt."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/research-connector-health-receipts",
        payload={
            "portfolio_id": resolved_portfolio,
            "connector_key": connector_key,
            "adapter_version": adapter_version,
            "observed_at": observed_at,
            "state": state,
            "reason_code": reason_code,
            "latency_ms": latency_ms,
        },
    )


@mcp.tool()
async def research_connector_health_list(
    stale_after_seconds: int = 900,
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List current connector runtime facts; STALE is not source verification."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-connector-health",
        params={
            "portfolio_id": resolved_portfolio,
            "stale_after_seconds": stale_after_seconds,
            "limit": limit,
        },
    )


@mcp.tool()
async def research_coverage_change_list(
    instrument_code: str = "",
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List factual evidence-coverage improvements and regressions without signals."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-coverage-changes",
        params={
            "portfolio_id": resolved_portfolio,
            "instrument_code": instrument_code or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def market_research_evidence_list(
    instrument_code: str = "",
    evidence_type: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List source-attributed market research facts without interpreting them as advice."""
    return await core_request(
        "GET",
        "/v1/market-research-evidence",
        params={
            "instrument_code": instrument_code or None,
            "evidence_type": evidence_type or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def market_research_evidence_change_list(
    instrument_code: str = "",
    change_type: Literal["", "INITIAL", "UNCHANGED", "CHANGED"] = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List source-content fact changes without interpreting them as market signals."""
    return await core_request(
        "GET",
        "/v1/market-research-evidence-changes",
        params={
            "instrument_code": instrument_code or None,
            "change_type": change_type or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def market_discovery_scan(
    instrument_codes: list[str],
    as_of_date: str,
    lookback_days: int = 180,
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Build an immutable facts-only candidate package from an explicit registered universe."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/market-discovery-runs",
        payload={
            "portfolio_id": resolved_portfolio,
            "instrument_codes": instrument_codes,
            "as_of_date": as_of_date,
            "lookback_days": lookback_days,
        },
    )


@mcp.tool()
async def market_discovery_run_list(
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List immutable discovery packages and their data-quality boundaries."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/market-discovery-runs",
        params={"portfolio_id": resolved_portfolio, "limit": limit},
    )


@mcp.tool()
async def market_discovery_change_list(
    run_id: str = "",
    attention_only: bool = False,
    portfolio_id: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """List factual changes between comparable discovery runs without ranking funds."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/market-discovery-changes",
        params={
            "portfolio_id": resolved_portfolio,
            "run_id": run_id or None,
            "attention_only": attention_only,
            "limit": limit,
        },
    )


@mcp.tool()
async def research_watchlist_transition_draft_create(
    instrument_code: str,
    new_state: Literal[
        "CANDIDATE",
        "OBSERVING",
        "REVIEW_DUE",
        "ADOPTED",
        "REJECTED",
        "ARCHIVED",
    ],
    reason: str,
    review_due_date: str = "",
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Draft an explicit research-watchlist transition without changing strategy or trades."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/research-watchlist-transition-drafts",
        payload={
            "portfolio_id": resolved_portfolio,
            "instrument_code": instrument_code,
            "new_state": new_state,
            "reason": reason,
            "review_due_date": review_due_date or None,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def research_watchlist_transition_draft_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str = "user",
) -> dict[str, Any]:
    """Commit one exact watchlist transition after explicit confirmation."""
    return await core_request(
        "POST",
        f"/v1/research-watchlist-transition-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def research_watchlist_list(
    state: Literal[
        "",
        "CANDIDATE",
        "OBSERVING",
        "REVIEW_DUE",
        "ADOPTED",
        "REJECTED",
        "ARCHIVED",
    ] = "",
    portfolio_id: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """List the portfolio-local research watchlist without ranking or selecting funds."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-watchlist",
        params={
            "portfolio_id": resolved_portfolio,
            "state": state or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def research_watchlist_review_snapshot_build(
    as_of_date: str,
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Build immutable due-review facts; this never changes watchlist state or investments."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "POST",
        "/v1/research-watchlist-review-snapshots",
        payload={
            "portfolio_id": resolved_portfolio,
            "as_of_date": as_of_date,
        },
    )


@mcp.tool()
async def research_watchlist_review_snapshot_list(
    portfolio_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List immutable watchlist review packages without ranking or recommendations."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/research-watchlist-review-snapshots",
        params={"portfolio_id": resolved_portfolio, "limit": limit},
    )


@mcp.tool()
async def review_action_decision_draft_create(
    action_item_id: str,
    decision: Literal["ACKNOWLEDGE", "RESOLVE"],
    reason: str,
) -> dict[str, Any]:
    """Draft an exact review-action state decision; this never changes investments."""
    return await core_request(
        "POST",
        f"/v1/review-action-items/{action_item_id}/decision-drafts",
        payload={
            "decision": decision,
            "reason": reason,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def review_action_decision_draft_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Commit one confirmed review-action decision without changing strategy or holdings."""
    return await core_request(
        "POST",
        f"/v1/review-action-decision-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def review_action_outcome_draft_create(
    action_item_id: str,
    outcome: Literal["COMPLETED", "PARTIAL", "NOT_COMPLETED", "NOT_APPLICABLE"],
    evidence_quality: Literal["VERIFIED", "USER_REPORTED", "UNVERIFIED"],
    note: str,
    evidence_ref: str = "",
) -> dict[str, Any]:
    """Draft a factual outcome for one resolved review action; this never trades."""
    return await core_request(
        "POST",
        f"/v1/review-action-items/{action_item_id}/outcome-drafts",
        payload={
            "outcome": outcome,
            "evidence_quality": evidence_quality,
            "evidence_ref": evidence_ref or None,
            "note": note,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def review_action_outcome_draft_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str = "user",
) -> dict[str, Any]:
    """Commit one exact review-action outcome after explicit confirmation."""
    return await core_request(
        "POST",
        f"/v1/review-action-outcome-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def review_action_outcome_list(
    portfolio_id: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """List immutable review-action outcomes and their evidence quality."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account_id, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/review-action-outcomes",
        params={"portfolio_id": resolved_portfolio, "limit": limit},
    )


@mcp.tool()
async def cash_event_draft_create(
    event_type: Literal["DEPOSIT", "WITHDRAWAL", "DIVIDEND", "INTEREST", "FEE"],
    event_date: str,
    amount: str,
    source: str,
    idempotency_key: str,
    note: str = "",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Draft one exact cash-ledger fact; this never changes holdings or creates a trade."""
    resolved_portfolio, resolved_account, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "POST",
        "/v1/cash-event-drafts",
        payload={
            "portfolio_id": resolved_portfolio,
            "account_id": resolved_account,
            "event_type": event_type,
            "event_date": event_date,
            "amount": amount,
            "source": source,
            "idempotency_key": idempotency_key,
            "note": note or None,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def cash_event_draft_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Commit exactly one confirmed cash event; this never creates an investment trade."""
    return await core_request(
        "POST",
        f"/v1/cash-event-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def cash_ledger_event_list(
    portfolio_id: str = "",
    account_id: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """List confirmed cash facts and the deterministic cash balance."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account, error = await resolve_investment_context()
        if error is not None:
            return error
    return await core_request(
        "GET",
        "/v1/cash-ledger-events",
        params={
            "portfolio_id": resolved_portfolio,
            "account_id": account_id or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def official_nav_backfill_record(
    source_name: str,
    source_ref: str,
    source_lineage: Literal["FUND_MANAGER_OFFICIAL", "WIND"],
    observations: list[dict[str, str]],
) -> dict[str, Any]:
    """Record an immutable batch of independently sourced official NAV facts."""
    return await core_request(
        "POST",
        "/v1/official-nav-backfills",
        payload={
            "source_name": source_name,
            "source_ref": source_ref,
            "source_lineage": source_lineage,
            "observations": observations,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def official_nav_backfill_list(limit: int = 100) -> dict[str, Any]:
    """List immutable official NAV backfill batches and conflicts."""
    return await core_request(
        "GET",
        "/v1/official-nav-backfills",
        params={"limit": limit},
    )


@mcp.tool()
async def runtime_mode_get(
    as_of_date: str,
    portfolio_id: str = "",
) -> dict[str, Any]:
    """Return Core's deterministic L0-L3 capability boundary; never infer missing facts."""
    resolved_portfolio = portfolio_id
    if not resolved_portfolio:
        resolved_portfolio, _account, error = await resolve_investment_context()
        if error is not None:
            resolved_portfolio = ""
    return await core_request(
        "GET",
        "/v1/runtime-mode",
        params={
            "portfolio_id": resolved_portfolio or None,
            "as_of_date": as_of_date,
        },
    )


@mcp.tool()
async def automation_delivery_status_list(
    status: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Read notification outbox state; DISPATCHED is not proof of delivery."""
    return await core_request(
        "GET",
        "/v1/notification-outbox",
        params={
            "status": status or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def automation_delivery_attempt_list(
    outbox_id: str = "",
    status: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Read immutable delivery attempts and verified channel receipt evidence."""
    return await core_request(
        "GET",
        "/v1/notification-delivery-attempts",
        params={
            "outbox_id": outbox_id or None,
            "status": status or None,
            "limit": limit,
        },
    )


@mcp.tool()
async def notification_test_send(
    idempotency_key: str,
    confirmation: Literal["SEND_TEST_NOTIFICATION"],
) -> dict[str, Any]:
    """Queue one fixed end-to-end test notification; never change investment state."""
    return await core_request(
        "POST",
        "/v1/notification-tests",
        payload={
            "idempotency_key": idempotency_key,
            "confirmation": confirmation,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def notification_test_get(test_request_id: str) -> dict[str, Any]:
    """Read the real outbox, retry and channel receipt state for one test."""
    return await core_request(
        "GET",
        f"/v1/notification-tests/{test_request_id}",
    )


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
async def investment_context_get() -> dict[str, Any]:
    """Get the saved default portfolio and account; auto-select when each is unambiguous."""
    return await core_request("GET", "/v1/investment-context")


@mcp.tool()
async def investment_context_set(portfolio_id: str, account_id: str) -> dict[str, Any]:
    """Set the default portfolio and account configuration; this does not change holdings."""
    return await core_request(
        "POST",
        "/v1/investment-context",
        payload={
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def strategy_definition_list() -> dict[str, Any]:
    """List reusable public strategy versions without any user's portfolio data."""
    return await core_request("GET", "/v1/strategies")


@mcp.tool()
async def strategy_current_get(portfolio_id: str = "", account_id: str = "") -> dict[str, Any]:
    """Read the approved strategy instance and local instrument configuration."""
    resolved_portfolio_id, _, error = await resolve_investment_context(portfolio_id, account_id)
    if error is not None:
        return error
    return await core_request(
        "GET",
        "/v1/strategy-assignment",
        params={"portfolio_id": resolved_portfolio_id},
    )


@mcp.tool()
async def instrument_create(
    code: str,
    name: str,
    asset_type: Literal["FUND", "ETF", "STOCK", "INDEX", "CASH"] = "FUND",
    currency: str = "CNY",
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
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def instrument_list() -> dict[str, Any]:
    """List instruments registered for local transaction recording."""
    return await core_request("GET", "/v1/instruments")


@mcp.tool()
async def instrument_role_update(
    code: str,
    role: Literal["CORE", "SATELLITE", "UNASSIGNED"],
    expected_current_role: Literal["CORE", "SATELLITE", "UNASSIGNED"],
    reason: str,
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Update an explicitly requested portfolio-local role with stale-write protection."""
    resolved_portfolio_id, _, error = await resolve_investment_context(portfolio_id, account_id)
    if error is not None:
        return error
    return await core_request(
        "PATCH",
        f"/v1/strategy-instruments/{code}/role",
        payload={
            "portfolio_id": resolved_portfolio_id,
            "role": role,
            "expected_current_role": expected_current_role,
            "reason": reason,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def strategy_instrument_config_draft_create(
    instrument_code: str,
    contribution_eligible: bool,
    reason: str,
    role: Literal["CORE", "SATELLITE", "CASH", "WATCH", "UNASSIGNED"] | None = None,
    target_weight_bps: int | None = None,
    priority: int | None = None,
    minimum_amount_minor: int | None = None,
    maximum_amount_minor: int | None = None,
    benchmark_code: str = "",
    proxy_suitability: Literal["STRONG", "WEAK", "NOT_APPLICABLE"] | None = None,
    thesis_status: Literal["ACTIVE", "REVIEW_REQUIRED", "INVALID"] | None = None,
    hard_stop_return_bps: int | None = None,
    maximum_position_weight_bps: int | None = None,
    lifecycle_rules: dict[str, Any] | None = None,
    redemption_policy: dict[str, Any] | None = None,
    exposure_profile: dict[str, Any] | None = None,
    fund_destination: str = "",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Preview a portfolio-local strategy configuration; NAV never decides eligibility."""
    resolved_portfolio_id, _, error = await resolve_investment_context(portfolio_id, account_id)
    if error is not None:
        return error
    return await core_request(
        "POST",
        "/v1/strategy-instrument-config-drafts",
        payload={
            "portfolio_id": resolved_portfolio_id,
            "instrument_code": instrument_code,
            "contribution_eligible": contribution_eligible,
            "reason": reason,
            "role": role,
            "target_weight_bps": target_weight_bps,
            "priority": priority,
            "minimum_amount_minor": minimum_amount_minor,
            "maximum_amount_minor": maximum_amount_minor,
            "benchmark_code": benchmark_code or None,
            "proxy_suitability": proxy_suitability,
            "thesis_status": thesis_status,
            "hard_stop_return_bps": hard_stop_return_bps,
            "maximum_position_weight_bps": maximum_position_weight_bps,
            "lifecycle_rules": lifecycle_rules,
            "redemption_policy": redemption_policy,
            "exposure_profile": exposure_profile,
            "fund_destination": fund_destination or None,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def strategy_instrument_config_draft_get(draft_id: str) -> dict[str, Any]:
    """Read one strategy configuration preview without exposing its token."""
    return await core_request(
        "GET",
        f"/v1/strategy-instrument-config-drafts/{draft_id}",
    )


@mcp.tool()
async def strategy_instrument_config_draft_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Apply one exact portfolio-local strategy config after explicit confirmation."""
    return await core_request(
        "POST",
        f"/v1/strategy-instrument-config-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def market_nav_snapshot_record(
    instrument_code: str,
    nav_date: str,
    nav: str,
    source_type: Literal["OFFICIAL", "PLATFORM", "AGGREGATOR", "USER"],
    source_name: str,
    observed_at: str,
    verification_status: Literal["VERIFIED", "UNVERIFIED"] = "UNVERIFIED",
    source_ref: str = "",
    source_lineage: Literal["EASTMONEY", "WIND", "FUND_MANAGER_OFFICIAL", "ALIPAY"] | None = None,
    currency: str = "CNY",
) -> dict[str, Any]:
    """Record an immutable sourced NAV observation; this never changes holdings."""
    return await core_request(
        "POST",
        "/v1/market-nav-snapshots",
        payload={
            "instrument_code": instrument_code,
            "nav_date": nav_date,
            "nav": nav,
            "currency": currency,
            "source_type": source_type,
            "source_name": source_name,
            "source_ref": source_ref or None,
            "source_lineage": source_lineage,
            "verification_status": verification_status,
            "observed_at": observed_at,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def market_nav_snapshot_list(instrument_code: str = "", limit: int = 100) -> dict[str, Any]:
    """List sourced NAV observations without estimating missing market data."""
    params: dict[str, Any] = {"limit": limit}
    if instrument_code:
        params["instrument_code"] = instrument_code
    return await core_request("GET", "/v1/market-nav-snapshots", params=params)


@mcp.tool()
async def market_data_canary_run(
    provider_id: Literal["AKSHARE_OPEN_FUND"] = "AKSHARE_OPEN_FUND",
    instrument_code: str = "",
    as_of_date: str = "",
) -> dict[str, Any]:
    """Test the configured market-data adapter contract without recording a NAV."""
    return await core_request(
        "POST",
        "/v1/market-data/canary",
        payload={
            "provider_id": provider_id,
            "instrument_code": instrument_code or None,
            "as_of_date": as_of_date or None,
        },
    )


@mcp.tool()
async def market_data_status_get(limit: int = 20) -> dict[str, Any]:
    """Read provider canary and synchronization status without fetching new data."""
    return await core_request("GET", "/v1/market-data/status", params={"limit": limit})


@mcp.tool()
async def market_data_sync(
    as_of_date: str = "",
    provider_id: Literal["AKSHARE_OPEN_FUND"] = "AKSHARE_OPEN_FUND",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Sync sourced NAVs for current committed FUND holdings after a provider canary."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    holdings = await core_request(
        "GET",
        "/v1/holdings",
        params={
            "portfolio_id": resolved_portfolio_id,
            "account_id": resolved_account_id,
        },
    )
    if not holdings.get("ok"):
        return holdings
    data = holdings.get("data")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return context_error("Investor Core returned an invalid holdings payload")
    instrument_codes = list(
        dict.fromkeys(
            str(item.get("instrument_code", ""))
            for item in items
            if isinstance(item, dict) and item.get("instrument_code")
        )
    )
    return await core_request(
        "POST",
        "/v1/market-data/sync",
        payload={
            "provider_id": provider_id,
            "instrument_codes": instrument_codes,
            "as_of_date": as_of_date or None,
            "actor_ref": "hermes",
        },
        timeout_seconds=120.0,
    )


@mcp.tool()
async def market_nav_verification_record(
    instrument_code: str,
    nav_date: str,
    nav: str,
    source_type: Literal["OFFICIAL", "PLATFORM"],
    source_name: str,
    source_ref: str,
    source_lineage: Literal["EASTMONEY", "WIND", "FUND_MANAGER_OFFICIAL", "ALIPAY"],
    observed_at: str,
    currency: str = "CNY",
) -> dict[str, Any]:
    """Corroborate a synced NAV with independent tool-sourced evidence."""
    return await core_request(
        "POST",
        "/v1/market-data/verifications",
        payload={
            "instrument_code": instrument_code,
            "nav_date": nav_date,
            "nav": nav,
            "currency": currency,
            "source_type": source_type,
            "source_name": source_name,
            "source_ref": source_ref,
            "source_lineage": source_lineage,
            "observed_at": observed_at,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def market_nav_verification_list(
    instrument_code: str = "", limit: int = 100
) -> dict[str, Any]:
    """List immutable cross-source NAV matches and conflicts."""
    params: dict[str, Any] = {"limit": limit}
    if instrument_code:
        params["instrument_code"] = instrument_code
    return await core_request("GET", "/v1/market-data/verifications", params=params)


@mcp.tool()
async def valuation_observation_record(
    instrument_code: str,
    metric: Literal["PE", "PB"],
    observation_date: str,
    value: str,
    source_type: Literal["OFFICIAL", "PROFESSIONAL", "AGGREGATOR", "USER"],
    source_name: str,
    observed_at: str,
    verification_status: Literal["VERIFIED", "UNVERIFIED"] = "UNVERIFIED",
    source_ref: str = "",
) -> dict[str, Any]:
    """Record sourced PE/PB evidence for an index; never infer or scrape a value."""
    return await core_request(
        "POST",
        "/v1/valuation-observations",
        payload={
            "instrument_code": instrument_code,
            "metric": metric,
            "observation_date": observation_date,
            "value": value,
            "source_type": source_type,
            "source_name": source_name,
            "source_ref": source_ref or None,
            "verification_status": verification_status,
            "observed_at": observed_at,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def valuation_snapshot_get(
    instrument_code: str,
    metric: Literal["PE", "PB"] = "PE",
    as_of_date: str = "",
    lookback_days: int = 1826,
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Calculate a deterministic percentile from stored benchmark evidence."""
    resolved_portfolio_id, _, error = await resolve_investment_context(portfolio_id, account_id)
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "instrument_code": instrument_code,
        "metric": metric,
        "lookback_days": lookback_days,
    }
    if as_of_date:
        params["as_of_date"] = as_of_date
    return await core_request("GET", "/v1/valuation-snapshot", params=params)


@mcp.tool()
async def satellite_signal_policy_draft_create(
    reason: str,
    metric: Literal["PE", "PB"] = "PE",
    entry_max_percentile_bps: int = 3000,
    lookback_days: int = 1826,
    minimum_sample_count: int = 30,
    maximum_observation_age_days: int = 10,
    allow_warning_data: bool = False,
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Draft one exact satellite valuation-signal policy; this never changes strategy."""
    resolved_portfolio_id, _, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "POST",
        "/v1/satellite-signal-policy-drafts",
        payload={
            "portfolio_id": resolved_portfolio_id,
            "metric": metric,
            "entry_max_percentile_bps": entry_max_percentile_bps,
            "lookback_days": lookback_days,
            "minimum_sample_count": minimum_sample_count,
            "maximum_observation_age_days": maximum_observation_age_days,
            "allow_warning_data": allow_warning_data,
            "reason": reason,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def satellite_signal_policy_draft_get(draft_id: str) -> dict[str, Any]:
    """Read one signal-policy draft without exposing its confirmation token."""
    return await core_request("GET", f"/v1/satellite-signal-policy-drafts/{draft_id}")


@mcp.tool()
async def satellite_signal_policy_draft_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Commit one exact signal policy after explicit confirmation; never trade."""
    return await core_request(
        "POST",
        f"/v1/satellite-signal-policy-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def satellite_signal_policy_list(
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """List approved satellite signal policies without evaluating instruments."""
    resolved_portfolio_id, _, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "GET",
        "/v1/satellite-signal-policies",
        params={"portfolio_id": resolved_portfolio_id},
    )


@mcp.tool()
async def satellite_signal_snapshot_build(
    as_of_date: str = "",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Build immutable PE/PB gate facts; OPEN is not advice, execution or a trade."""
    resolved_portfolio_id, _, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "POST",
        "/v1/satellite-signal-snapshots",
        payload={
            "portfolio_id": resolved_portfolio_id,
            "as_of_date": as_of_date or None,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def satellite_signal_snapshot_list(
    as_of_date: str = "",
    limit: int = 100,
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """List persisted satellite signal facts without generating new observations."""
    resolved_portfolio_id, _, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "limit": limit,
    }
    if as_of_date:
        params["as_of_date"] = as_of_date
    return await core_request("GET", "/v1/satellite-signal-snapshots", params=params)


@mcp.tool()
async def risk_scan_run(
    as_of_date: str = "",
    liquidity_amount: str = "",
    liquidity_destination: str = "",
    portfolio_id: str = "",
    account_id: str = "",
    include_rule_hits: bool = False,
) -> dict[str, Any]:
    """Run configured rules; defaults to compact summaries and never creates transactions."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "POST",
        "/v1/risk-scans",
        payload={
            "portfolio_id": resolved_portfolio_id,
            "account_id": resolved_account_id,
            "as_of_date": as_of_date or None,
            "liquidity_amount": liquidity_amount or None,
            "liquidity_destination": liquidity_destination or None,
            "include_rule_hits": include_rule_hits,
        },
    )


@mcp.tool()
async def risk_rule_hit_list(
    instrument_code: str = "",
    rule_code: str = "",
    status: Literal[
        "HIT",
        "EVALUATED_NOT_HIT",
        "NOT_CONFIGURED",
        "DATA_UNAVAILABLE",
        "NOT_APPLICABLE",
        "DATA_BLOCKED",
        "EXEMPT",
    ]
    | None = None,
    limit: int = 50,
    offset: int = 0,
    include_details: bool = False,
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """List paginated deterministic rule facts; details are opt-in to protect context."""
    resolved_portfolio_id, _, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "limit": limit,
        "offset": offset,
        "include_details": include_details,
    }
    if instrument_code:
        params["instrument_code"] = instrument_code
    if rule_code:
        params["rule_code"] = rule_code
    if status:
        params["status"] = status
    return await core_request("GET", "/v1/rule-hits", params=params)


@mcp.tool()
async def lifecycle_observation_record(
    instrument_code: str,
    observation_type: Literal[
        "RELATIVE_PERFORMANCE",
        "REPLACEMENT_CANDIDATE",
        "OBJECTIVE_STATUS",
        "TOOL_QUALITY",
        "REDEMPTION_TERMS",
        "EXPOSURE_PROFILE",
    ],
    observation_date: str,
    facts: dict[str, Any],
    source_type: Literal["OFFICIAL", "PROFESSIONAL", "AGGREGATOR", "PLATFORM", "USER"],
    source_name: str,
    observed_at: str,
    verification_status: Literal["VERIFIED", "UNVERIFIED"] = "UNVERIFIED",
    source_ref: str = "",
) -> dict[str, Any]:
    """Record sourced lifecycle evidence; unverified evidence cannot trigger a sale."""
    return await core_request(
        "POST",
        "/v1/lifecycle-observations",
        payload={
            "instrument_code": instrument_code,
            "observation_type": observation_type,
            "observation_date": observation_date,
            "facts": facts,
            "source_type": source_type,
            "source_name": source_name,
            "source_ref": source_ref or None,
            "verification_status": verification_status,
            "observed_at": observed_at,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def lifecycle_observation_list(
    instrument_code: str = "",
    observation_type: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List immutable lifecycle evidence without making an investment conclusion."""
    params: dict[str, Any] = {"limit": limit}
    if instrument_code:
        params["instrument_code"] = instrument_code
    if observation_type:
        params["observation_type"] = observation_type
    return await core_request("GET", "/v1/lifecycle-observations", params=params)


@mcp.tool()
async def sell_proposal_list(
    status: str = "",
    limit: int = 100,
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """List deterministic sell proposals; every item remains unexecuted."""
    resolved_portfolio_id, _, error = await resolve_investment_context(portfolio_id, account_id)
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "limit": limit,
    }
    if status:
        params["status"] = status
    return await core_request("GET", "/v1/sell-proposals", params=params)


@mcp.tool()
async def sell_proposal_context_get(proposal_id: str) -> dict[str, Any]:
    """Read rule evidence and diagnostics; approval is not a SELL transaction."""
    return await core_request("GET", f"/v1/sell-proposals/{proposal_id}")


@mcp.tool()
async def sell_decision_draft_create(
    proposal_id: str,
    decision: Literal["APPROVE", "DEFER", "REJECT"],
    user_reason: str = "",
) -> dict[str, Any]:
    """Preview a human decision on a proposal; creates no transaction."""
    return await core_request(
        "POST",
        f"/v1/sell-proposals/{proposal_id}/decision-drafts",
        payload={
            "decision": decision,
            "user_reason": user_reason or None,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def sell_decision_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Commit a proposal decision; even APPROVE does not change holdings."""
    return await core_request(
        "POST",
        f"/v1/sell-decision-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def sell_followup_list(
    status: str = "",
    limit: int = 100,
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """List six-month reviews for committed proposal-linked SELL records."""
    resolved_portfolio_id, _, error = await resolve_investment_context(portfolio_id, account_id)
    if error is not None:
        return error
    params: dict[str, Any] = {"portfolio_id": resolved_portfolio_id, "limit": limit}
    if status:
        params["status"] = status
    return await core_request("GET", "/v1/sell-followups", params=params)


@mcp.tool()
async def sell_followup_evaluate(
    followup_id: str,
    as_of_date: str = "",
) -> dict[str, Any]:
    """Evaluate a due sell follow-up from stored NAV evidence; never changes strategy."""
    return await core_request(
        "POST",
        f"/v1/sell-followups/{followup_id}/evaluate",
        payload={"as_of_date": as_of_date or None, "actor_ref": "hermes"},
    )


@mcp.tool()
async def portfolio_valuation_get(
    as_of_date: str = "", portfolio_id: str = "", account_id: str = ""
) -> dict[str, Any]:
    """Value committed holdings from stored NAV observations in the default context."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "account_id": resolved_account_id,
    }
    if as_of_date:
        params["as_of_date"] = as_of_date
    return await core_request("GET", "/v1/portfolio-valuation", params=params)


@mcp.tool()
async def portfolio_brief_get(
    as_of_date: str = "", portfolio_id: str = "", account_id: str = ""
) -> dict[str, Any]:
    """Get a deterministic brief. Return data.display_text exactly, with no added analysis."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "account_id": resolved_account_id,
    }
    if as_of_date:
        params["as_of_date"] = as_of_date
    return await core_request("GET", "/v1/portfolio-brief", params=params)


@mcp.tool()
async def investment_workspace_get(
    view: Literal["DAILY", "WEEKLY", "READINESS", "FULL"] = "DAILY",
    as_of_date: str = "",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Get the deterministic daily/weekly workbench; return data.display_text exactly."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "account_id": resolved_account_id,
        "view": view,
    }
    if as_of_date:
        params["as_of_date"] = as_of_date
    return await core_request("GET", "/v1/investment-workspace", params=params)


@mcp.tool()
async def weekly_plan_preview(
    contribution_amount: str,
    as_of_date: str = "",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Preview an explicit weekly contribution by role; never creates or executes trades."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "account_id": resolved_account_id,
        "contribution_amount": contribution_amount,
    }
    if as_of_date:
        params["as_of_date"] = as_of_date
    return await core_request("GET", "/v1/weekly-plan-preview", params=params)


@mcp.tool()
async def weekly_plan_draft_create(
    contribution_amount: str,
    plan_date: str,
    idempotency_key: str,
    as_of_date: str = "",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Create a DRAFT from the exact Core plan; this creates no transaction."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "POST",
        "/v1/weekly-plans",
        payload={
            "portfolio_id": resolved_portfolio_id,
            "account_id": resolved_account_id,
            "contribution_amount": contribution_amount,
            "plan_date": plan_date,
            "as_of_date": as_of_date or None,
            "idempotency_key": idempotency_key,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def weekly_plan_list(
    status: str = "",
    portfolio_id: str = "",
    account_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List audited weekly plans without changing plan or transaction state."""
    resolved_portfolio_id, _, error = await resolve_investment_context(portfolio_id, account_id)
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "limit": limit,
    }
    if status:
        params["status"] = status
    return await core_request("GET", "/v1/weekly-plans", params=params)


@mcp.tool()
async def weekly_plan_get(plan_id: str) -> dict[str, Any]:
    """Read one audited weekly plan without exposing its confirmation token."""
    return await core_request("GET", f"/v1/weekly-plans/{plan_id}")


@mcp.tool()
async def weekly_plan_freeze(
    plan_id: str,
    confirmation_token: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Freeze one exact DRAFT after explicit user confirmation; this never trades."""
    return await core_request(
        "POST",
        f"/v1/weekly-plans/{plan_id}/freeze",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def weekly_plan_skip(
    plan_id: str,
    confirmation_token: str,
    confirmed_by: str,
    reason: str,
) -> dict[str, Any]:
    """Skip one DRAFT or FROZEN plan after explicit user confirmation."""
    return await core_request(
        "POST",
        f"/v1/weekly-plans/{plan_id}/skip",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
            "reason": reason,
        },
    )


@mcp.tool()
async def weekly_plan_mark_executed(
    plan_id: str,
    transaction_ids: list[str],
    confirmed_by: str,
) -> dict[str, Any]:
    """Link a FROZEN plan to separately committed BUY records; never execute a trade."""
    return await core_request(
        "POST",
        f"/v1/weekly-plans/{plan_id}/executed",
        payload={
            "transaction_ids": transaction_ids,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def weekly_plan_transaction_link(
    plan_id: str,
    transaction_id: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Link one confirmed external BUY fact; never create or execute a trade."""
    return await core_request(
        "POST",
        f"/v1/weekly-plans/{plan_id}/transactions",
        payload={
            "transaction_id": transaction_id,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def external_subscription_draft_create(
    weekly_plan_id: str,
    instrument_code: str,
    requested_amount: str,
    submitted_at: str,
    submitted_business_date: str,
    external_platform: str,
    idempotency_key: str,
    external_reference: str = "",
    expected_confirmation_date: str = "",
    source: Literal["USER_REPORTED", "PLATFORM_RECEIPT"] = "USER_REPORTED",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Draft a user-reported external fund subscription; never place an order."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "POST",
        "/v1/external-subscription-drafts",
        payload={
            "portfolio_id": resolved_portfolio_id,
            "account_id": resolved_account_id,
            "weekly_plan_id": weekly_plan_id,
            "instrument_code": instrument_code,
            "requested_amount": requested_amount,
            "submitted_at": submitted_at,
            "submitted_business_date": submitted_business_date,
            "external_platform": external_platform,
            "external_reference": external_reference or None,
            "expected_confirmation_date": expected_confirmation_date or None,
            "source": source,
            "idempotency_key": idempotency_key,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def external_subscription_status_draft_create(
    subscription_id: str,
    target_status: Literal["PENDING_CONFIRMATION", "CANCELLED", "REJECTED"],
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Draft an external subscription status fact; never infer platform failure."""
    return await core_request(
        "POST",
        f"/v1/external-subscriptions/{subscription_id}/status-drafts",
        payload={
            "target_status": target_status,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def external_subscription_confirmation_draft_create(
    subscription_id: str,
    confirmed_at: str,
    confirmation_business_date: str,
    nav_date: str,
    nav: str,
    confirmed_shares: str,
    confirmed_amount: str,
    idempotency_key: str,
    fee: str = "0",
    refunded_amount: str = "0",
    external_reference: str = "",
) -> dict[str, Any]:
    """Draft one platform-confirmed fund share fact; holding remains unchanged."""
    return await core_request(
        "POST",
        f"/v1/external-subscriptions/{subscription_id}/confirmation-drafts",
        payload={
            "confirmed_at": confirmed_at,
            "confirmation_business_date": confirmation_business_date,
            "nav_date": nav_date,
            "nav": nav,
            "confirmed_shares": confirmed_shares,
            "confirmed_amount": confirmed_amount,
            "fee": fee,
            "refunded_amount": refunded_amount,
            "external_reference": external_reference or None,
            "idempotency_key": idempotency_key,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def external_subscription_draft_get(draft_id: str) -> dict[str, Any]:
    """Read an external subscription draft without changing financial facts."""
    return await core_request("GET", f"/v1/external-subscription-drafts/{draft_id}")


@mcp.tool()
async def external_subscription_confirmation_reversal_draft_create(
    subscription_id: str,
    confirmation_id: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Draft correction of one unposted share confirmation; never change holdings."""
    return await core_request(
        "POST",
        f"/v1/external-subscriptions/{subscription_id}/confirmation-reversal-drafts",
        payload={
            "confirmation_id": confirmation_id,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "actor_ref": "hermes",
        },
    )


@mcp.tool()
async def external_subscription_draft_commit(
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Commit exactly one explicitly confirmed external subscription fact."""
    return await core_request(
        "POST",
        f"/v1/external-subscription-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def external_subscription_list(
    weekly_plan_id: str = "",
    status: str = "",
    portfolio_id: str = "",
    account_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List external subscriptions, including pending and cross-week facts."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    params: dict[str, Any] = {
        "portfolio_id": resolved_portfolio_id,
        "account_id": resolved_account_id,
        "limit": limit,
    }
    if weekly_plan_id:
        params["weekly_plan_id"] = weekly_plan_id
    if status:
        params["status"] = status
    return await core_request("GET", "/v1/external-subscriptions", params=params)


@mcp.tool()
async def external_subscription_get(subscription_id: str) -> dict[str, Any]:
    """Read one external subscription and its audited confirmations."""
    return await core_request("GET", f"/v1/external-subscriptions/{subscription_id}")


@mcp.tool()
async def external_subscription_transaction_draft_create(
    confirmation_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Draft a BUY ledger fact from one user-confirmed platform result; never trade."""
    return await core_request(
        "POST",
        f"/v1/external-subscription-confirmations/{confirmation_id}/transaction-drafts",
        payload={"idempotency_key": idempotency_key, "actor_ref": "hermes"},
    )


@mcp.tool()
async def external_subscription_transaction_draft_commit(
    confirmation_id: str,
    draft_id: str,
    confirmation_token: str,
    confirmed_by: str,
) -> dict[str, Any]:
    """Post one confirmed platform result to the ledger after explicit confirmation."""
    return await core_request(
        "POST",
        f"/v1/external-subscription-confirmations/{confirmation_id}/"
        f"transaction-drafts/{draft_id}/commit",
        payload={
            "confirmation_token": confirmation_token,
            "confirmed_by": confirmed_by,
        },
    )


@mcp.tool()
async def holding_list(portfolio_id: str = "", account_id: str = "") -> dict[str, Any]:
    """List latest deterministic holdings reconstructed from committed records."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "GET",
        "/v1/holdings",
        params={
            "portfolio_id": resolved_portfolio_id,
            "account_id": resolved_account_id,
        },
    )


@mcp.tool()
async def opening_position_draft_create(
    instrument_code: str,
    as_of_date: str,
    total_shares: str,
    platform: str,
    idempotency_key: str,
    cost_amount: str = "",
    average_cost_nav: str = "",
    note: str = "",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Create an old-holding draft in the default context; this is not a BUY."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "POST",
        "/v1/opening-position-drafts",
        payload={
            "portfolio_id": resolved_portfolio_id,
            "account_id": resolved_account_id,
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
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    params: dict[str, Any] = {
        "limit": limit,
        "portfolio_id": resolved_portfolio_id,
        "account_id": resolved_account_id,
    }
    return await core_request("GET", "/v1/transactions", params=params)


@mcp.tool()
async def transaction_draft_get(draft_id: str) -> dict[str, Any]:
    """Read one transaction draft and its status without exposing its token."""
    return await core_request("GET", f"/v1/transaction-drafts/{draft_id}")


@mcp.tool()
async def transaction_draft_create(
    instrument_code: str,
    side: Literal["BUY", "SELL"],
    trade_date: str,
    amount: str,
    nav: str,
    shares: str,
    platform: str,
    idempotency_key: str,
    note: str = "",
    sell_proposal_id: str = "",
    portfolio_id: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Create a BUY or SELL draft in the default context; this does not change holdings."""
    resolved_portfolio_id, resolved_account_id, error = await resolve_investment_context(
        portfolio_id, account_id
    )
    if error is not None:
        return error
    return await core_request(
        "POST",
        "/v1/transaction-drafts",
        payload={
            "portfolio_id": resolved_portfolio_id,
            "account_id": resolved_account_id,
            "instrument_code": instrument_code,
            "side": side,
            "trade_date": trade_date,
            "amount": amount,
            "nav": nav,
            "shares": shares,
            "platform": platform,
            "idempotency_key": idempotency_key,
            "note": note or None,
            "sell_proposal_id": sell_proposal_id or None,
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
