"""Validated local API request contracts."""

from __future__ import annotations

from datetime import date
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
    role: Literal["CORE", "SATELLITE", "UNASSIGNED"] = "UNASSIGNED"
    actor_ref: str = Field(default="local-user", min_length=1, max_length=120)


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
