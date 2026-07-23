"""Core HTTP API entry point."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from investor_core.api.schemas import (
    AccountCreateRequest,
    InstrumentCreateRequest,
    InstrumentRoleUpdateRequest,
    InvestmentContextSetRequest,
    MarketDataCanaryRequest,
    MarketDataSyncRequest,
    MarketNavSnapshotCreateRequest,
    MarketNavVerificationCreateRequest,
    OpeningPositionDraftCreateRequest,
    PortfolioCreateRequest,
    TransactionDraftCommitRequest,
    TransactionDraftCreateRequest,
    TransactionReversalDraftCreateRequest,
)
from investor_core.config import Settings, get_settings
from investor_core.health import build_doctor_report
from investor_core.ledger import LedgerError, LedgerService
from investor_core.logging_config import build_uvicorn_log_config
from investor_core.market_data import MarketDataService
from investor_core.market_sync import MarketSyncService
from investor_core.version import __version__


def success(
    data: Any,
    *,
    warnings: list[str] | None = None,
    data_quality: str = "PASS",
) -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "meta": {"schema_version": "1.0", "data_quality": data_quality},
        "warnings": warnings or [],
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    ledger = LedgerService(runtime_settings)
    market_data = MarketDataService(runtime_settings)
    market_sync = MarketSyncService(runtime_settings)
    app = FastAPI(
        title="Value DCA Investor Core",
        version=__version__,
        docs_url=None if runtime_settings.environment == "production" else "/docs",
        redoc_url=None,
    )

    @app.exception_handler(LedgerError)
    async def ledger_error_handler(_request: Request, exc: LedgerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "ok": False,
                "data": {},
                "meta": {"schema_version": "1.0", "data_quality": "PASS"},
                "warnings": [],
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            },
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": runtime_settings.app_name, "version": __version__}

    @app.get("/ready")
    def ready() -> dict[str, object]:
        report = build_doctor_report(runtime_settings)
        if report.status == "FAIL":
            raise HTTPException(status_code=503, detail=report.model_dump(mode="json"))
        return report.model_dump(mode="json")

    @app.post("/v1/portfolios")
    def portfolio_create(request: PortfolioCreateRequest) -> dict[str, Any]:
        return success(
            ledger.create_portfolio(
                name=request.name,
                base_currency=request.base_currency,
                actor_ref=request.actor_ref,
            )
        )

    @app.get("/v1/portfolios")
    def portfolio_list() -> dict[str, Any]:
        return success({"items": ledger.list_portfolios()})

    @app.post("/v1/accounts")
    def account_create(request: AccountCreateRequest) -> dict[str, Any]:
        return success(
            ledger.create_account(
                portfolio_id=request.portfolio_id,
                name=request.name,
                platform=request.platform,
                currency=request.currency,
                actor_ref=request.actor_ref,
            )
        )

    @app.get("/v1/accounts")
    def account_list(portfolio_id: str | None = None) -> dict[str, Any]:
        return success({"items": ledger.list_accounts(portfolio_id)})

    @app.get("/v1/investment-context")
    def investment_context_get() -> dict[str, Any]:
        return success(ledger.get_investment_context())

    @app.post("/v1/investment-context")
    def investment_context_set(request: InvestmentContextSetRequest) -> dict[str, Any]:
        return success(
            ledger.set_investment_context(
                portfolio_id=request.portfolio_id,
                account_id=request.account_id,
                actor_ref=request.actor_ref,
            )
        )

    @app.post("/v1/instruments")
    def instrument_create(request: InstrumentCreateRequest) -> dict[str, Any]:
        return success(
            ledger.create_instrument(
                code=request.code,
                name=request.name,
                asset_type=request.asset_type,
                currency=request.currency,
                role=request.role,
                actor_ref=request.actor_ref,
            )
        )

    @app.get("/v1/instruments")
    def instrument_list() -> dict[str, Any]:
        return success({"items": ledger.list_instruments()})

    @app.patch("/v1/instruments/{instrument_code}/role")
    def instrument_role_update(
        instrument_code: str, request: InstrumentRoleUpdateRequest
    ) -> dict[str, Any]:
        return success(
            ledger.update_instrument_role(
                code=instrument_code,
                role=request.role,
                expected_current_role=request.expected_current_role,
                reason=request.reason,
                actor_ref=request.actor_ref,
            )
        )

    @app.post("/v1/transaction-drafts")
    def transaction_draft_create(request: TransactionDraftCreateRequest) -> dict[str, Any]:
        result = ledger.create_transaction_draft(
            portfolio_id=request.portfolio_id,
            account_id=request.account_id,
            instrument_code=request.instrument_code,
            side=request.side,
            trade_date_value=request.trade_date.isoformat(),
            amount=str(request.amount),
            nav=str(request.nav),
            shares=str(request.shares),
            platform=request.platform,
            idempotency_key=request.idempotency_key,
            note=request.note,
            actor_ref=request.actor_ref,
        )
        return success(result, warnings=result.pop("warnings"))

    @app.post("/v1/opening-position-drafts")
    def opening_position_draft_create(
        request: OpeningPositionDraftCreateRequest,
    ) -> dict[str, Any]:
        result = ledger.create_opening_position_draft(
            portfolio_id=request.portfolio_id,
            account_id=request.account_id,
            instrument_code=request.instrument_code,
            as_of_date_value=request.as_of_date.isoformat(),
            total_shares=str(request.total_shares),
            platform=request.platform,
            idempotency_key=request.idempotency_key,
            cost_amount=(str(request.cost_amount) if request.cost_amount is not None else None),
            average_cost_nav=(
                str(request.average_cost_nav) if request.average_cost_nav is not None else None
            ),
            note=request.note,
            actor_ref=request.actor_ref,
        )
        return success(result, warnings=result.pop("warnings"))

    @app.post("/v1/transaction-reversal-drafts")
    def transaction_reversal_draft_create(
        request: TransactionReversalDraftCreateRequest,
    ) -> dict[str, Any]:
        result = ledger.create_reversal_draft(
            transaction_id=request.transaction_id,
            idempotency_key=request.idempotency_key,
            actor_ref=request.actor_ref,
        )
        return success(result, warnings=result.pop("warnings"))

    @app.get("/v1/transaction-drafts/{draft_id}")
    def transaction_draft_get(draft_id: str) -> dict[str, Any]:
        return success(ledger.get_transaction_draft(draft_id))

    @app.post("/v1/transaction-drafts/{draft_id}/commit")
    def transaction_draft_commit(
        draft_id: str, request: TransactionDraftCommitRequest
    ) -> dict[str, Any]:
        return success(
            ledger.commit_transaction_draft(
                draft_id=draft_id,
                confirmation_token=request.confirmation_token,
                confirmed_by=request.confirmed_by,
            )
        )

    @app.post("/v1/opening-position-drafts/{draft_id}/commit")
    def opening_position_draft_commit(
        draft_id: str, request: TransactionDraftCommitRequest
    ) -> dict[str, Any]:
        return success(
            ledger.commit_opening_position_draft(
                draft_id=draft_id,
                confirmation_token=request.confirmation_token,
                confirmed_by=request.confirmed_by,
            )
        )

    @app.get("/v1/holdings")
    def holding_list(
        portfolio_id: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        return success(
            {"items": ledger.list_holdings(portfolio_id=portfolio_id, account_id=account_id)}
        )

    @app.get("/v1/transactions")
    def transaction_list(
        portfolio_id: str | None = None,
        account_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": ledger.list_transactions(
                    portfolio_id=portfolio_id,
                    account_id=account_id,
                    limit=limit,
                )
            }
        )

    @app.post("/v1/market-nav-snapshots")
    def market_nav_snapshot_create(
        request: MarketNavSnapshotCreateRequest,
    ) -> dict[str, Any]:
        result = market_data.record_nav_snapshot(
            instrument_code=request.instrument_code,
            nav_date_value=request.nav_date.isoformat(),
            nav=str(request.nav),
            currency=request.currency,
            source_type=request.source_type,
            source_name=request.source_name,
            source_ref=request.source_ref,
            source_lineage=request.source_lineage,
            verification_status=request.verification_status,
            observed_at_value=request.observed_at.isoformat(),
            actor_ref=request.actor_ref,
        )
        warnings = result.pop("warnings")
        quality = result["snapshot"]["data_quality"]
        return success(result, warnings=warnings, data_quality=quality)

    @app.get("/v1/market-nav-snapshots")
    def market_nav_snapshot_list(
        instrument_code: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        items = market_data.list_nav_snapshots(instrument_code=instrument_code, limit=limit)
        quality = "WARNING" if any(i["data_quality"] == "WARNING" for i in items) else "PASS"
        warnings = list(dict.fromkeys(warning for item in items for warning in item["warnings"]))
        return success({"items": items}, warnings=warnings, data_quality=quality)

    @app.get("/v1/portfolio-valuation")
    def portfolio_valuation_get(
        portfolio_id: str,
        account_id: str,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        result = market_data.portfolio_valuation(
            portfolio_id=portfolio_id,
            account_id=account_id,
            as_of_date_value=as_of_date,
        )
        return success(
            result,
            warnings=result["warnings"],
            data_quality=result["data_quality"],
        )

    @app.get("/v1/portfolio-brief")
    def portfolio_brief_get(
        portfolio_id: str,
        account_id: str,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        result = market_data.portfolio_brief(
            portfolio_id=portfolio_id,
            account_id=account_id,
            as_of_date_value=as_of_date,
        )
        return success(
            result,
            warnings=result["valuation"]["warnings"],
            data_quality=result["valuation"]["data_quality"],
        )

    @app.post("/v1/market-data/canary")
    def market_data_canary_run(request: MarketDataCanaryRequest) -> dict[str, Any]:
        result = market_sync.run_canary(
            provider_id=request.provider_id,
            instrument_code=request.instrument_code,
            as_of_date_value=(request.as_of_date.isoformat() if request.as_of_date else None),
        )
        quality = "PASS" if result["status"] == "PASS" else "SOURCE_ERROR"
        warnings = [] if quality == "PASS" else ["Market data provider canary failed"]
        return success(result, warnings=warnings, data_quality=quality)

    @app.post("/v1/market-data/sync")
    def market_data_sync(request: MarketDataSyncRequest) -> dict[str, Any]:
        result = market_sync.sync_navs(
            provider_id=request.provider_id,
            instrument_codes=request.instrument_codes,
            as_of_date_value=(request.as_of_date.isoformat() if request.as_of_date else None),
            actor_ref=request.actor_ref,
        )
        return success(
            result,
            warnings=result["warnings"],
            data_quality=result["data_quality"],
        )

    @app.get("/v1/market-data/status")
    def market_data_status_get(
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return success(market_sync.status(limit=limit))

    @app.post("/v1/market-data/verifications")
    def market_nav_verification_create(
        request: MarketNavVerificationCreateRequest,
    ) -> dict[str, Any]:
        result = market_data.record_nav_verification(
            instrument_code=request.instrument_code,
            nav_date_value=request.nav_date.isoformat(),
            nav=str(request.nav),
            currency=request.currency,
            source_type=request.source_type,
            source_name=request.source_name,
            source_ref=request.source_ref,
            source_lineage=request.source_lineage,
            observed_at_value=request.observed_at.isoformat(),
            actor_ref=request.actor_ref,
        )
        warnings = result.pop("warnings")
        quality = result.pop("data_quality")
        return success(result, warnings=warnings, data_quality=quality)

    @app.get("/v1/market-data/verifications")
    def market_nav_verification_list(
        instrument_code: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        items = market_data.list_nav_verifications(
            instrument_code=instrument_code,
            limit=limit,
        )
        quality = "SOURCE_ERROR" if any(item["status"] == "CONFLICT" for item in items) else "PASS"
        warnings = (
            ["One or more independent NAV observations conflict with the primary source"]
            if quality == "SOURCE_ERROR"
            else []
        )
        return success({"items": items}, warnings=warnings, data_quality=quality)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "investor_core.api.app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=build_uvicorn_log_config(settings.core_log_path, settings.log_level),
    )
