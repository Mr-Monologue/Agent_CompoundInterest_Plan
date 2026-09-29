"""Public research inputs, independent from approved investment instruments."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from investor_core.benchmarks import Distribution, Series
from investor_core.execution import SourceArchive, StrictModel
from investor_core.notebook import Claim


class PeerWindow(StrictModel):
    label: str = Field(min_length=1)
    start: date
    end: date

    @model_validator(mode="after")
    def valid(self) -> PeerWindow:
        if self.start >= self.end:
            raise ValueError("Window needs exact ordered endpoints")
        return self


class PeerReported(StrictModel):
    start: date
    end: date
    return_pct: Decimal = Field(gt=-100, allow_inf_nan=False)
    evidence_ids: list[str] = Field(min_length=1)
    basis: Literal["ISSUER_REPORTED_NET_NAV_GROWTH"] = "ISSUER_REPORTED_NET_NAV_GROWTH"


class PeerProduct(StrictModel):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1)
    product_key: str = Field(min_length=1)
    share_class: Literal["A", "C", "SINGLE", "UNKNOWN"]
    other_shares: list[str] = Field(default_factory=list)
    product_type: Literal["EQUITY", "MIXED", "INDEX", "OTHER", "UNKNOWN"]
    active: bool | None
    market: Literal["CN_A", "CN_A_H", "QDII", "UNKNOWN"]
    currency: str = "CNY"
    stock_min_pct: Decimal | None = Field(default=None, ge=0, le=100)
    stock_max_pct: Decimal | None = Field(default=None, ge=0, le=100)
    medical_non_cash_min_pct: Decimal | None = Field(default=None, ge=0, le=100)
    share_inception: date | None = None
    admission_sources: list[str]
    admission_note: str = Field(min_length=1)
    dimensions: dict[str, list[Claim]]
    attempts_and_gaps: list[str] = Field(default_factory=list)
    nav: Series | None = None
    distributions: list[Distribution] = Field(default_factory=list)
    distribution_from: date | None = None
    distribution_to: date | None = None
    distribution_sources: list[str] = Field(default_factory=list)
    reported: list[PeerReported] = Field(default_factory=list)

    @model_validator(mode="after")
    def identity(self) -> PeerProduct:
        if self.nav and (self.nav.code != self.code or self.nav.currency != self.currency):
            raise ValueError("NAV must identify the exact share and currency")
        if self.nav and self.nav.return_basis == "FUND_TOTAL_RETURN" and self.distributions:
            raise ValueError("Cannot add dividends to an already reinvested series")
        if len({d.ex_date for d in self.distributions}) != len(self.distributions):
            raise ValueError("Duplicate dividend dates")
        if (
            self.stock_min_pct is not None
            and self.stock_max_pct is not None
            and self.stock_min_pct > self.stock_max_pct
        ):
            raise ValueError("Invalid stock bounds")
        if set(self.dimensions) != {"fees", "concentration", "management", "method"}:
            raise ValueError("All dimensions must explicitly contain facts or unknowns")
        if any(not v for v in self.dimensions.values()):
            raise ValueError("Missing dimension must contain an explicit UNKNOWN")
        return self


class PeerStudy(StrictModel):
    anchor_code: str
    cohort_key: str = Field(min_length=1, max_length=100)
    scope_version: str = Field(min_length=1)
    scope_defined_at: date
    scope_rationale: str = Field(min_length=1)
    # Research admission rules, never investment eligibility or risk thresholds.
    stock_floor_pct: Decimal = Field(ge=0, le=100)
    medical_floor_pct: Decimal = Field(ge=0, le=100)
    knowledge_date: date
    expected_previous_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    windows: list[PeerWindow] = Field(min_length=2, max_length=12)
    calendar_dates: list[date] = Field(min_length=2, max_length=20000)
    calendar_sources: list[str] = Field(min_length=1)
    sources: dict[str, SourceArchive]
    products: list[PeerProduct] = Field(min_length=2, max_length=10)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def integrity(self) -> PeerStudy:
        if len({p.code for p in self.products}) != len(self.products):
            raise ValueError("Duplicate share")
        if len({p.product_key for p in self.products}) != len(self.products):
            raise ValueError("Different shares of one product are not independent candidates")
        if self.anchor_code not in {p.code for p in self.products}:
            raise ValueError("Reference missing")
        if self.calendar_dates != sorted(set(self.calendar_dates)):
            raise ValueError("Calendar must contain unique ordered dates")
        if len({w.label for w in self.windows}) != len(self.windows):
            raise ValueError("Duplicate window")
        if self.scope_defined_at > self.knowledge_date:
            raise ValueError("Scope is not yet known")
        for source in self.sources.values():
            if source.quality != "OFFICIAL" or not source.original_sha256:
                raise ValueError("First-stage evidence requires official source fingerprints")
            if source.retrieved_at.date() > self.knowledge_date:
                raise ValueError("Evidence not available at historical knowledge date")
            if source.published_date and source.published_date > self.knowledge_date:
                raise ValueError("Future publication cannot enter historical comparison")
            if source.data_date > self.knowledge_date:
                raise ValueError("Future data")
        refs = list(self.calendar_sources)
        for p in self.products:
            refs += p.admission_sources + p.distribution_sources
            if p.nav:
                refs += p.nav.evidence_ids
                if any(point.day > self.knowledge_date for point in p.nav.points):
                    raise ValueError("Future NAV point")
            for rows in p.dimensions.values():
                for claim in rows:
                    refs += claim.evidence_ids
                    if claim.as_of and claim.as_of > self.knowledge_date:
                        raise ValueError("Future claim")
            for report in p.reported:
                refs += report.evidence_ids
                if report.start >= report.end or report.end > self.knowledge_date:
                    raise ValueError("Invalid report period")
                if any(
                    key in self.sources and self.sources[key].instrument_code != p.code
                    for key in report.evidence_ids
                ):
                    raise ValueError("Report refers to a different share")
            for key in (
                p.admission_sources + p.distribution_sources + (p.nav.evidence_ids if p.nav else [])
            ):
                if key in self.sources and self.sources[key].instrument_code != p.code:
                    raise ValueError("Evidence refers to a different share")
        if any(key not in self.sources for key in refs):
            raise ValueError("Missing evidence reference")
        if any(w.end > self.knowledge_date for w in self.windows):
            raise ValueError("Future window")
        return self
