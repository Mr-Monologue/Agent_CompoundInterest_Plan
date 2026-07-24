"""Validated local API request contracts."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self

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
    actor_ref: str = Field(default="hermes", min_length=1, max_length=120)


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


class RiskScanRequest(RequestModel):
    portfolio_id: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=80)
    as_of_date: date | None = None


class SellDecisionDraftCreateRequest(RequestModel):
    decision: Literal["APPROVE", "DEFER", "REJECT"]
    user_reason: str | None = Field(default=None, max_length=1000)
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
