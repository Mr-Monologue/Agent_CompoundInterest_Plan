"""Core HTTP API entry point."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from investor_core.api.schemas import (
    AccountCreateRequest,
    AutomationJobRunRequest,
    AutomationPolicyDraftCreateRequest,
    AutomationSchedulerSnapshotRequest,
    InstrumentCreateRequest,
    InstrumentRoleUpdateRequest,
    InvestmentContextSetRequest,
    LifecycleObservationCreateRequest,
    MarketDataCanaryRequest,
    MarketDataSyncRequest,
    MarketNavSnapshotCreateRequest,
    MarketNavVerificationCreateRequest,
    NotificationDeliveryReceiptRequest,
    OpeningPositionDraftCreateRequest,
    PortfolioCreateRequest,
    RiskScanRequest,
    SellDecisionDraftCreateRequest,
    SellFollowupEvaluateRequest,
    StrategyInstrumentConfigDraftRequest,
    TransactionDraftCommitRequest,
    TransactionDraftCreateRequest,
    TransactionReversalDraftCreateRequest,
    ValuationObservationCreateRequest,
    WeeklyPlanConfirmRequest,
    WeeklyPlanDraftCreateRequest,
    WeeklyPlanExecutedRequest,
    WeeklyPlanSkipRequest,
)
from investor_core.config import Settings, get_settings
from investor_core.health import build_doctor_report
from investor_core.ledger import LedgerError, LedgerService
from investor_core.logging_config import build_uvicorn_log_config
from investor_core.market_data import MarketDataService
from investor_core.market_sync import MarketSyncService
from investor_core.operations import OperationsService
from investor_core.performance import PerformanceService
from investor_core.planning import PlanningService
from investor_core.risk import RiskService
from investor_core.strategy import StrategyService
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
    strategies = StrategyService(runtime_settings)
    planning = PlanningService(runtime_settings)
    risk = RiskService(runtime_settings)
    operations = OperationsService(runtime_settings)
    performance = PerformanceService(runtime_settings)
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

    @app.post("/v1/automation-policy-drafts")
    def automation_policy_draft_create(
        request: AutomationPolicyDraftCreateRequest,
    ) -> dict[str, Any]:
        return success(operations.create_policy_draft(**request.model_dump()))

    @app.get("/v1/automation-policy-drafts/{draft_id}")
    def automation_policy_draft_get(draft_id: str) -> dict[str, Any]:
        return success(operations.get_policy_draft(draft_id=draft_id))

    @app.post("/v1/automation-policy-drafts/{draft_id}/commit")
    def automation_policy_draft_commit(
        draft_id: str,
        request: TransactionDraftCommitRequest,
    ) -> dict[str, Any]:
        return success(
            operations.commit_policy_draft(
                draft_id=draft_id,
                confirmation_token=request.confirmation_token,
                confirmed_by=request.confirmed_by,
            )
        )

    @app.get("/v1/automation-policies")
    def automation_policy_list(
        portfolio_id: str | None = None,
        active_only: bool = True,
    ) -> dict[str, Any]:
        return success(
            {
                "items": operations.list_policies(
                    portfolio_id=portfolio_id,
                    active_only=active_only,
                )
            }
        )

    @app.post("/v1/automation-runs")
    def automation_run(request: AutomationJobRunRequest) -> dict[str, Any]:
        result = operations.run_job(**request.model_dump())
        quality = (
            "SOURCE_ERROR"
            if result["job_run"]["status"] == "FAILED"
            else ("WARNING" if result["job_run"]["status"] == "DEGRADED" else "PASS")
        )
        return success(
            result,
            warnings=(
                ["Automation run failed or produced degraded facts"] if quality != "PASS" else []
            ),
            data_quality=quality,
        )

    @app.get("/v1/automation-runs")
    def automation_run_list(
        job_name: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": operations.list_runs(
                    job_name=job_name,
                    status=status,
                    limit=limit,
                )
            }
        )

    @app.get("/v1/automation-status")
    def automation_status() -> dict[str, Any]:
        return success(operations.status_summary())

    @app.get("/v1/automation-scheduler-manifest")
    def automation_scheduler_manifest(profile: str = "investor") -> dict[str, Any]:
        return success(operations.scheduler_manifest(profile=profile))

    @app.post("/v1/automation-scheduler-snapshots")
    def automation_scheduler_snapshot_record(
        request: AutomationSchedulerSnapshotRequest,
    ) -> dict[str, Any]:
        return success(
            operations.record_scheduler_snapshot(
                **request.model_dump(),
            )
        )

    @app.post("/v1/automation-retries/run")
    def automation_retries_run(
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return success(operations.retry_due(limit=limit))

    @app.get("/v1/automation-missed-runs")
    def automation_missed_run_list(
        grace_minutes: int = Query(default=10, ge=1, le=1440),
        lookback_days: int = Query(default=7, ge=1, le=31),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        return success(
            {
                "items": operations.list_missed_runs(
                    grace_minutes=grace_minutes,
                    lookback_days=lookback_days,
                    limit=limit,
                )
            }
        )

    @app.post("/v1/automation-recovery/run")
    def automation_recovery_run(
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return success(operations.recover_due(limit=limit))

    @app.get("/v1/report-bundles")
    def report_bundle_list(
        portfolio_id: str | None = None,
        bundle_type: str | None = None,
        delivery_action: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": operations.list_report_bundles(
                    portfolio_id=portfolio_id,
                    bundle_type=bundle_type,
                    delivery_action=delivery_action,
                    limit=limit,
                )
            }
        )

    @app.get("/v1/alerts")
    def alert_list(
        portfolio_id: str | None = None,
        status: str | None = "OPEN",
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": operations.list_alerts(
                    portfolio_id=portfolio_id,
                    status=status,
                    limit=limit,
                )
            }
        )

    @app.get("/v1/notification-outbox")
    def notification_outbox_list(
        status: str | None = "PENDING",
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success({"items": operations.list_outbox(status=status, limit=limit)})

    @app.post("/v1/notification-deliveries/claim")
    def notification_delivery_claim(
        delivery_target: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        return success(
            operations.claim_delivery_attempts(
                delivery_target=delivery_target,
                limit=limit,
            )
        )

    @app.post("/v1/notification-deliveries/receipt")
    def notification_delivery_receipt(
        request: NotificationDeliveryReceiptRequest,
    ) -> dict[str, Any]:
        return success(
            operations.record_delivery_receipt(
                **request.model_dump(),
            )
        )

    @app.get("/v1/notification-delivery-attempts")
    def notification_delivery_attempt_list(
        outbox_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": operations.list_delivery_attempts(
                    outbox_id=outbox_id,
                    status=status,
                    limit=limit,
                )
            }
        )

    @app.get("/v1/portfolio-performance")
    def portfolio_performance_get(
        portfolio_id: str,
        period_start: date,
        period_end: date,
        period_type: str = "CUSTOM",
    ) -> dict[str, Any]:
        result = performance.calculate(
            portfolio_id=portfolio_id,
            period_start=period_start,
            period_end=period_end,
            period_type=period_type,
            persist=False,
        )
        quality = str(result["data_quality"])
        return success(
            result,
            warnings=list(result["warnings"]),
            data_quality=quality,
        )

    @app.get("/v1/periodic-reviews")
    def periodic_review_list(
        portfolio_id: str,
        review_type: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": performance.list_reviews(
                    portfolio_id=portfolio_id,
                    review_type=review_type,
                    limit=limit,
                )
            }
        )

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

    @app.get("/v1/allocation-policy")
    def allocation_policy_get(portfolio_id: str) -> dict[str, Any]:
        return success(ledger.get_allocation_policy(portfolio_id=portfolio_id))

    @app.get("/v1/strategies")
    def strategy_definition_list() -> dict[str, Any]:
        return success({"items": strategies.list_definitions()})

    @app.get("/v1/strategy-assignment")
    def strategy_assignment_get(portfolio_id: str) -> dict[str, Any]:
        return success(strategies.get_assignment(portfolio_id=portfolio_id))

    @app.post("/v1/instruments")
    def instrument_create(request: InstrumentCreateRequest) -> dict[str, Any]:
        return success(
            ledger.create_instrument(
                code=request.code,
                name=request.name,
                asset_type=request.asset_type,
                currency=request.currency,
                actor_ref=request.actor_ref,
            )
        )

    @app.get("/v1/instruments")
    def instrument_list() -> dict[str, Any]:
        return success({"items": ledger.list_instruments()})

    @app.patch("/v1/strategy-instruments/{instrument_code}/role")
    def instrument_role_update(
        instrument_code: str, request: InstrumentRoleUpdateRequest
    ) -> dict[str, Any]:
        return success(
            strategies.update_instrument_role(
                portfolio_id=request.portfolio_id,
                instrument_code=instrument_code,
                role=request.role,
                expected_current_role=request.expected_current_role,
                reason=request.reason,
                actor_ref=request.actor_ref,
            )
        )

    @app.post("/v1/strategy-instrument-config-drafts")
    def strategy_instrument_config_draft_create(
        request: StrategyInstrumentConfigDraftRequest,
    ) -> dict[str, Any]:
        result = strategies.create_config_draft(**request.model_dump())
        return success(result, warnings=result["warnings"])

    @app.get("/v1/strategy-instrument-config-drafts/{draft_id}")
    def strategy_instrument_config_draft_get(draft_id: str) -> dict[str, Any]:
        return success(strategies.get_config_draft(draft_id=draft_id))

    @app.post("/v1/strategy-instrument-config-drafts/{draft_id}/commit")
    def strategy_instrument_config_draft_commit(
        draft_id: str,
        request: TransactionDraftCommitRequest,
    ) -> dict[str, Any]:
        return success(
            strategies.commit_config_draft(
                draft_id=draft_id,
                confirmation_token=request.confirmation_token,
                confirmed_by=request.confirmed_by,
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
            sell_proposal_id=request.sell_proposal_id,
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

    @app.get("/v1/weekly-plan-preview")
    def weekly_plan_preview_get(
        portfolio_id: str,
        account_id: str,
        contribution_amount: str,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        result = market_data.weekly_plan_preview(
            portfolio_id=portfolio_id,
            account_id=account_id,
            contribution_amount=contribution_amount,
            as_of_date_value=as_of_date,
        )
        return success(
            result,
            warnings=result["warnings"],
            data_quality=result["data_quality"],
        )

    @app.post("/v1/weekly-plans")
    def weekly_plan_draft_create(
        request: WeeklyPlanDraftCreateRequest,
    ) -> dict[str, Any]:
        result = planning.create_draft(
            portfolio_id=request.portfolio_id,
            account_id=request.account_id,
            contribution_amount=str(request.contribution_amount),
            plan_date_value=request.plan_date.isoformat(),
            as_of_date_value=(request.as_of_date.isoformat() if request.as_of_date else None),
            idempotency_key=request.idempotency_key,
            actor_ref=request.actor_ref,
        )
        return success(
            result,
            warnings=result["warnings"],
            data_quality=result["plan"]["revision"]["data_quality"],
        )

    @app.get("/v1/weekly-plans")
    def weekly_plan_list(
        portfolio_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": planning.list(
                    portfolio_id=portfolio_id,
                    status=status,
                    limit=limit,
                )
            }
        )

    @app.get("/v1/weekly-plans/{plan_id}")
    def weekly_plan_get(plan_id: str) -> dict[str, Any]:
        return success(planning.get(plan_id=plan_id))

    @app.post("/v1/weekly-plans/{plan_id}/freeze")
    def weekly_plan_freeze(
        plan_id: str,
        request: WeeklyPlanConfirmRequest,
    ) -> dict[str, Any]:
        return success(
            planning.freeze(
                plan_id=plan_id,
                confirmation_token=request.confirmation_token,
                confirmed_by=request.confirmed_by,
            )
        )

    @app.post("/v1/weekly-plans/{plan_id}/skip")
    def weekly_plan_skip(
        plan_id: str,
        request: WeeklyPlanSkipRequest,
    ) -> dict[str, Any]:
        return success(
            planning.skip(
                plan_id=plan_id,
                confirmation_token=request.confirmation_token,
                confirmed_by=request.confirmed_by,
                reason=request.reason,
            )
        )

    @app.post("/v1/weekly-plans/{plan_id}/executed")
    def weekly_plan_executed(
        plan_id: str,
        request: WeeklyPlanExecutedRequest,
    ) -> dict[str, Any]:
        return success(
            planning.mark_executed(
                plan_id=plan_id,
                transaction_ids=request.transaction_ids,
                confirmed_by=request.confirmed_by,
            )
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

    @app.post("/v1/valuation-observations")
    def valuation_observation_create(
        request: ValuationObservationCreateRequest,
    ) -> dict[str, Any]:
        result = risk.record_valuation_observation(
            instrument_code=request.instrument_code,
            metric=request.metric,
            observation_date=request.observation_date.isoformat(),
            value=str(request.value),
            source_type=request.source_type,
            source_name=request.source_name,
            source_ref=request.source_ref,
            verification_status=request.verification_status,
            observed_at=request.observed_at.isoformat(),
            actor_ref=request.actor_ref,
        )
        quality = (
            "PASS"
            if request.verification_status == "VERIFIED"
            and request.source_type in {"OFFICIAL", "PROFESSIONAL"}
            else "WARNING"
        )
        return success(
            result,
            warnings=(
                []
                if quality == "PASS"
                else ["Valuation observation is single-source or unverified"]
            ),
            data_quality=quality,
        )

    @app.get("/v1/valuation-snapshot")
    def valuation_snapshot_get(
        portfolio_id: str,
        instrument_code: str,
        metric: str = "PE",
        as_of_date: str | None = None,
        lookback_days: int = Query(default=1826, ge=30, le=7305),
    ) -> dict[str, Any]:
        result = risk.valuation_snapshot(
            portfolio_id=portfolio_id,
            instrument_code=instrument_code,
            metric=metric,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
        )
        return success(
            result,
            warnings=(
                ["Valuation evidence is single-source or unverified"]
                if result.get("data_quality") == "WARNING"
                else []
            ),
            data_quality=str(result.get("data_quality", "PASS")),
        )

    @app.post("/v1/risk-scans")
    def risk_scan(request: RiskScanRequest) -> dict[str, Any]:
        result = risk.scan(
            portfolio_id=request.portfolio_id,
            account_id=request.account_id,
            as_of_date=(request.as_of_date.isoformat() if request.as_of_date else None),
            liquidity_amount=(
                str(request.liquidity_amount) if request.liquidity_amount is not None else None
            ),
            liquidity_destination=request.liquidity_destination,
        )
        return success(
            result,
            warnings=(
                ["Risk scan was data-blocked"] if result["data_quality"] == "SOURCE_ERROR" else []
            ),
            data_quality=result["data_quality"],
        )

    @app.post("/v1/lifecycle-observations")
    def lifecycle_observation_create(
        request: LifecycleObservationCreateRequest,
    ) -> dict[str, Any]:
        result = risk.record_lifecycle_observation(
            instrument_code=request.instrument_code,
            observation_type=request.observation_type,
            observation_date=request.observation_date.isoformat(),
            facts=request.facts,
            source_type=request.source_type,
            source_name=request.source_name,
            source_ref=request.source_ref,
            verification_status=request.verification_status,
            observed_at=request.observed_at.isoformat(),
            actor_ref=request.actor_ref,
        )
        quality = "PASS" if result["verification_status"] == "VERIFIED" else "WARNING"
        return success(
            result,
            warnings=[] if quality == "PASS" else ["Lifecycle evidence is unverified"],
            data_quality=quality,
        )

    @app.get("/v1/lifecycle-observations")
    def lifecycle_observation_list(
        instrument_code: str | None = None,
        observation_type: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": risk.list_lifecycle_observations(
                    instrument_code=instrument_code,
                    observation_type=observation_type,
                    limit=limit,
                )
            }
        )

    @app.get("/v1/sell-proposals")
    def sell_proposal_list(
        portfolio_id: str,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": risk.list_proposals(
                    portfolio_id=portfolio_id,
                    status=status,
                    limit=limit,
                )
            }
        )

    @app.get("/v1/sell-proposals/{proposal_id}")
    def sell_proposal_get(proposal_id: str) -> dict[str, Any]:
        return success(risk.get_proposal(proposal_id=proposal_id))

    @app.post("/v1/sell-proposals/{proposal_id}/decision-drafts")
    def sell_decision_draft_create(
        proposal_id: str,
        request: SellDecisionDraftCreateRequest,
    ) -> dict[str, Any]:
        return success(
            risk.create_decision_draft(
                proposal_id=proposal_id,
                decision=request.decision,
                user_reason=request.user_reason,
                actor_ref=request.actor_ref,
            )
        )

    @app.post("/v1/sell-decision-drafts/{draft_id}/commit")
    def sell_decision_commit(
        draft_id: str,
        request: TransactionDraftCommitRequest,
    ) -> dict[str, Any]:
        return success(
            risk.commit_decision(
                draft_id=draft_id,
                confirmation_token=request.confirmation_token,
                confirmed_by=request.confirmed_by,
            )
        )

    @app.get("/v1/sell-followups")
    def sell_followup_list(
        portfolio_id: str,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return success(
            {
                "items": risk.list_followups(
                    portfolio_id=portfolio_id,
                    status=status,
                    limit=limit,
                )
            }
        )

    @app.post("/v1/sell-followups/{followup_id}/evaluate")
    def sell_followup_evaluate(
        followup_id: str,
        request: SellFollowupEvaluateRequest,
    ) -> dict[str, Any]:
        return success(
            risk.evaluate_followup(
                followup_id=followup_id,
                as_of_date=(request.as_of_date.isoformat() if request.as_of_date else None),
                actor_ref=request.actor_ref,
            )
        )

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
