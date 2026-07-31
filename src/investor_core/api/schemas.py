"""Validated local API request contracts."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class PortfolioCreateRequest(RequestModel):
    name: str = Field(min_length=1, max_length=120)
    base_currency: str = Field(default="CNY", min_length=3, max_length=3)
    actor_ref: str = Field(default="local-user", min_length=1, max_length=120)


class AccountCreateRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(min_length=1, max_length=120)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    actor_ref: str = Field(default="local-user", min_length=1, max_length=120)


class InvestmentContextSetRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=80)
    actor_ref: str = Field(default="local-user", min_length=1, max_length=120)


class InstrumentCreateRequest(RequestModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    asset_type: Literal["FUND", "ETF", "STOCK", "INDEX", "CASH"] = "FUND"
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    actor_ref: str = Field(default="local-user", min_length=1, max_length=120)


class InstrumentRoleUpdateRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    role: Literal["CORE", "SATELLITE", "UNASSIGNED"]
    expected_current_role: Literal["CORE", "SATELLITE", "UNASSIGNED"]
    reason: str = Field(min_length=1, max_length=500)
    actor_ref: str = Field(default="local-user", min_length=1, max_length=120)


class StrategyInstrumentConfigDraftRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    instrument_code: str = Field(min_length=1, max_length=40)
    contribution_eligible: bool
    reason: str = Field(min_length=1, max_length=500)
    role: Literal["CORE", "SATELLITE", "CASH", "WATCH", "UNASSIGNED"] | None = None
    target_weight_bps: int | None = Field(default=None, ge=0, le=10000)
    priority: int | None = Field(default=None, ge=0)
    minimum_amount_minor: int | None = Field(default=None, ge=0)
    maximum_amount_minor: int | None = Field(default=None, gt=0)
    benchmark_code: str | None = Field(default=None, min_length=1, max_length=40)
    proxy_suitability: Literal["STRONG", "WEAK", "NOT_APPLICABLE"] | None = None
    thesis_status: Literal["ACTIVE", "REVIEW_REQUIRED", "INVALID"] | None = None
    hard_stop_return_bps: int | None = Field(default=None, ge=-10000, lt=0)
    maximum_position_weight_bps: int | None = Field(default=None, gt=0, le=10000)
    lifecycle_rules: dict[str, Any] | None = None
    redemption_policy: dict[str, Any] | None = None
    exposure_profile: dict[str, Any] | None = None
    fund_destination: str | None = Field(default=None, max_length=200)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class TransactionDraftCreateRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=80)
    instrument_code: str = Field(min_length=1, max_length=40)
    side: Literal["BUY", "SELL"]
    trade_date: date
    amount: Decimal = Field(gt=0)
    nav: Decimal = Field(gt=0)
    shares: Decimal = Field(gt=0)
    platform: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
    sell_proposal_id: str | None = Field(default=None, min_length=1, max_length=80)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_sell_proposal_link(self) -> Self:
        if self.sell_proposal_id is not None and self.side != "SELL":
            raise ValueError("sell_proposal_id is only valid for SELL drafts")
        return self


class OpeningPositionDraftCreateRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=80)
    instrument_code: str = Field(min_length=1, max_length=40)
    as_of_date: date
    total_shares: Decimal = Field(gt=0)
    cost_amount: Decimal | None = Field(default=None, gt=0)
    average_cost_nav: Decimal | None = Field(default=None, gt=0)
    platform: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_cost_basis(self) -> Self:
        if (self.cost_amount is None) == (self.average_cost_nav is None):
            raise ValueError("provide exactly one of cost_amount or average_cost_nav")
        return self


class TransactionReversalDraftCreateRequest(RequestModel):
    transaction_id: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class TransactionDraftCommitRequest(RequestModel):
    confirmation_token: str = Field(min_length=1, max_length=200)
    confirmed_by: str = Field(min_length=1, max_length=120)


class MarketNavSnapshotCreateRequest(RequestModel):
    instrument_code: str = Field(min_length=1, max_length=40)
    nav_date: date
    nav: Decimal = Field(gt=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    source_type: Literal["OFFICIAL", "PLATFORM", "AGGREGATOR", "USER"]
    source_name: str = Field(min_length=1, max_length=200)
    source_ref: str | None = Field(default=None, max_length=1000)
    source_lineage: Literal["EASTMONEY", "WIND", "FUND_MANAGER_OFFICIAL", "ALIPAY"] | None = None
    verification_status: Literal["VERIFIED", "UNVERIFIED"] = "UNVERIFIED"
    observed_at: datetime
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class MarketDataCanaryRequest(RequestModel):
    provider_id: Literal["AKSHARE_OPEN_FUND"] = "AKSHARE_OPEN_FUND"
    instrument_code: str | None = Field(default=None, min_length=1, max_length=40)
    as_of_date: date | None = None


class MarketDataSyncRequest(RequestModel):
    provider_id: Literal["AKSHARE_OPEN_FUND"] = "AKSHARE_OPEN_FUND"
    instrument_codes: list[str] = Field(min_length=1, max_length=100)
    as_of_date: date | None = None
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class MarketNavVerificationCreateRequest(RequestModel):
    instrument_code: str = Field(min_length=1, max_length=40)
    nav_date: date
    nav: Decimal = Field(gt=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    source_type: Literal["OFFICIAL", "PLATFORM"]
    source_name: str = Field(min_length=1, max_length=200)
    source_ref: str = Field(min_length=1, max_length=1000)
    source_lineage: Literal["EASTMONEY", "WIND", "FUND_MANAGER_OFFICIAL", "ALIPAY"]
    observed_at: datetime
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class ValuationObservationCreateRequest(RequestModel):
    instrument_code: str = Field(min_length=1, max_length=40)
    metric: Literal["PE", "PB"]
    observation_date: date
    value: Decimal = Field(gt=0)
    source_type: Literal["OFFICIAL", "PROFESSIONAL", "AGGREGATOR", "USER"]
    source_name: str = Field(min_length=1, max_length=200)
    source_ref: str | None = Field(default=None, max_length=1000)
    verification_status: Literal["VERIFIED", "UNVERIFIED"] = "UNVERIFIED"
    observed_at: datetime
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class LifecycleObservationCreateRequest(RequestModel):
    instrument_code: str = Field(min_length=1, max_length=40)
    observation_type: Literal[
        "RELATIVE_PERFORMANCE",
        "REPLACEMENT_CANDIDATE",
        "OBJECTIVE_STATUS",
        "TOOL_QUALITY",
        "REDEMPTION_TERMS",
        "EXPOSURE_PROFILE",
    ]
    observation_date: date
    facts: dict[str, Any]
    source_type: Literal["OFFICIAL", "PROFESSIONAL", "AGGREGATOR", "PLATFORM", "USER"]
    source_name: str = Field(min_length=1, max_length=200)
    source_ref: str | None = Field(default=None, max_length=1000)
    verification_status: Literal["VERIFIED", "UNVERIFIED"] = "UNVERIFIED"
    observed_at: datetime
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class RiskScanRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=80)
    as_of_date: date | None = None
    liquidity_amount: Decimal | None = Field(default=None, gt=0)
    liquidity_destination: str | None = Field(default=None, min_length=1, max_length=200)
    include_rule_hits: bool = False


class SellDecisionDraftCreateRequest(RequestModel):
    decision: Literal["APPROVE", "DEFER", "REJECT"]
    user_reason: str | None = Field(default=None, max_length=1000)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class SellFollowupEvaluateRequest(RequestModel):
    as_of_date: date | None = None
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class AutomationPolicyDraftCreateRequest(RequestModel):
    portfolio_id: str | None = Field(default=None, min_length=1, max_length=80)
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
    ]
    enabled: bool
    schedule: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    config: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class AutomationJobRunRequest(RequestModel):
    portfolio_id: str | None = Field(default=None, min_length=1, max_length=80)
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
    ]
    scheduled_for: str | None = Field(default=None, min_length=1, max_length=80)
    actor_ref: str = Field(default="operations-runner", min_length=1, max_length=120)


class AutomationSchedulerJobSnapshot(RequestModel):
    managed_name: str = Field(min_length=1, max_length=120)
    schedule: str = Field(min_length=1, max_length=120)
    enabled: bool
    no_agent: bool
    script: str = Field(min_length=1, max_length=240)
    delivery_target: str = Field(min_length=1, max_length=200)
    last_status: str | None = Field(default=None, max_length=80)
    last_run_at: str | None = Field(default=None, max_length=80)
    next_run_at: str | None = Field(default=None, max_length=80)


class AutomationSchedulerSnapshotRequest(RequestModel):
    profile: str = Field(min_length=1, max_length=80)
    gateway_status: Literal["RUNNING", "STOPPED", "UNKNOWN"]
    jobs: list[AutomationSchedulerJobSnapshot] = Field(max_length=100)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class NotificationDeliveryReceiptRequest(RequestModel):
    outbox_id: str = Field(min_length=1, max_length=80)
    attempt_id: str = Field(min_length=1, max_length=80)
    receipt_token: str = Field(min_length=20, max_length=200)
    outcome: Literal["DELIVERED", "FAILED"]
    provider: str = Field(min_length=1, max_length=120)
    provider_message_id: str | None = Field(default=None, max_length=240)
    evidence: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=120)
    actor_ref: str = Field(
        default="hermes-delivery-adapter",
        min_length=1,
        max_length=120,
    )


class NotificationTestCreateRequest(RequestModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmation: Literal["SEND_TEST_NOTIFICATION"]
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class CashEventDraftCreateRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=80)
    event_type: Literal["DEPOSIT", "WITHDRAWAL", "DIVIDEND", "INTEREST", "FEE"]
    event_date: date
    amount: Decimal = Field(gt=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    source: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class OfficialNavBackfillObservation(RequestModel):
    instrument_code: str = Field(min_length=1, max_length=40)
    nav_date: date
    nav: Decimal = Field(gt=0)
    observed_at: datetime


class OfficialNavBackfillRequest(RequestModel):
    source_name: str = Field(min_length=1, max_length=200)
    source_ref: str = Field(min_length=1, max_length=1000)
    source_lineage: Literal["FUND_MANAGER_OFFICIAL", "WIND"]
    observations: list[OfficialNavBackfillObservation] = Field(min_length=1, max_length=1000)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class MarketResearchEvidenceRequest(RequestModel):
    instrument_code: str = Field(min_length=1, max_length=40)
    evidence_date: date
    evidence_type: Literal[
        "FUND_PROFILE",
        "HOLDINGS",
        "MANAGER",
        "FEES",
        "BENCHMARK",
        "MARKET_REGIME",
        "OTHER",
    ]
    source_name: str = Field(min_length=1, max_length=200)
    source_ref: str = Field(min_length=1, max_length=1000)
    source_lineage: str = Field(min_length=1, max_length=120)
    facts: dict[str, Any] = Field(min_length=1)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class MarketDiscoveryScanRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    instrument_codes: list[str] = Field(min_length=1, max_length=200)
    as_of_date: date
    lookback_days: int = Field(default=180, ge=30, le=730)


class ReviewTrendSnapshotRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    as_of_date: date
    review_type: Literal["ALL", "MONTHLY", "QUARTERLY", "ANNUAL"] = "ALL"
    lookback_reviews: int = Field(default=12, ge=1, le=120)


class ReviewActionDecisionDraftRequest(RequestModel):
    decision: Literal["ACKNOWLEDGE", "RESOLVE"]
    reason: str = Field(min_length=1, max_length=1000)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class ResearchWatchlistTransitionDraftRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    instrument_code: str = Field(min_length=1, max_length=40)
    new_state: Literal[
        "CANDIDATE",
        "OBSERVING",
        "REVIEW_DUE",
        "ADOPTED",
        "REJECTED",
        "ARCHIVED",
    ]
    reason: str = Field(min_length=1, max_length=1000)
    review_due_date: date | None = None
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class ResearchWatchlistReviewSnapshotRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    as_of_date: date


class ReviewActionOutcomeDraftRequest(RequestModel):
    outcome: Literal["COMPLETED", "PARTIAL", "NOT_COMPLETED", "NOT_APPLICABLE"]
    evidence_quality: Literal["VERIFIED", "USER_REPORTED", "UNVERIFIED"]
    evidence_ref: str | None = Field(default=None, max_length=1000)
    note: str = Field(min_length=1, max_length=2000)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class WeeklyPlanDraftCreateRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=80)
    contribution_amount: Decimal = Field(gt=0)
    plan_date: date
    as_of_date: date | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


class WeeklyPlanConfirmRequest(RequestModel):
    confirmation_token: str = Field(min_length=1, max_length=200)
    confirmed_by: str = Field(min_length=1, max_length=120)


class WeeklyPlanSkipRequest(WeeklyPlanConfirmRequest):
    reason: str = Field(min_length=1, max_length=500)


class WeeklyPlanExecutedRequest(RequestModel):
    transaction_ids: list[str] = Field(min_length=1, max_length=100)
    confirmed_by: str = Field(min_length=1, max_length=120)
