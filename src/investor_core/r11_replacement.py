"""Read-only hypothetical replacement gates. No portfolio mutation or orders."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator

from investor_core.execution import StrictModel
from investor_core.r11_inputs import Context
from investor_core.scheduler import digest


class Corroboration(StrictModel):
    primary_source: str
    independent_source: str
    independence_evidence_source: str
    # Hashes bind the entire exact product input, including every NAV/dividend.
    primary_product_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    corroborated_product_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewed_by: str = Field(min_length=1)
    prepared_by: str = Field(min_length=1)
    review_source: str


def corroboration_gaps(
    ctx: Context, product: dict[str, Any], proof: Corroboration | None
) -> list[str]:
    if proof is None:
        return ["INDEPENDENT_VERIFICATION_REQUIRED"]
    gaps = []
    for key in (
        proof.primary_source,
        proof.independent_source,
        proof.independence_evidence_source,
        proof.review_source,
    ):
        gaps += ctx.source_gaps(key)
    primary = ctx.sources.get(proof.primary_source)
    independent = ctx.sources.get(proof.independent_source)
    if (
        not primary
        or not independent
        or primary.lineage == independent.lineage
        or primary.document_hash == independent.document_hash
        or primary.url == independent.url
    ):
        gaps.append("INDEPENDENT_UPSTREAM_NOT_ESTABLISHED")
    expected = digest({k: v for k, v in product.items() if k != "corroboration"})
    if proof.primary_product_hash != expected or proof.corroborated_product_hash != expected:
        gaps.append("CORROBORATION_CONTENT_MISMATCH")
    if proof.reviewed_by.strip().casefold() == proof.prepared_by.strip().casefold():
        gaps.append("INDEPENDENT_REVIEWER_REQUIRED")
    return sorted(set(gaps))


class PlatformEvidence(StrictModel):
    code: str
    share_class: str
    account_ref: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    source: str
    effective_from: datetime
    effective_to: datetime
    # None explicitly documents no limit; missing properties are invalid.
    per_order_limit_cny: Decimal | None = Field(ge=0)
    remaining_limit_cny: Decimal | None = Field(ge=0)
    restriction: Literal["NONE", "BLOCKED"]
    calendar_source: str
    calendar_from: date
    calendar_to: date
    subscription_days: list[date]
    redemption_days: list[date]

    @model_validator(mode="after")
    def dates(self) -> PlatformEvidence:
        if self.effective_from.tzinfo is None or self.effective_to.tzinfo is None:
            raise ValueError("explicit platform validity timezone required")
        if self.effective_to <= self.effective_from or self.calendar_to < self.calendar_from:
            raise ValueError("ordered platform/calendar interval required")
        for days in (self.subscription_days, self.redemption_days):
            if days != sorted(set(days)) or any(
                not self.calendar_from <= d <= self.calendar_to for d in days
            ):
                raise ValueError("unique explicit days within documented interval required")
        return self


class ReplacementEvidence(StrictModel):
    account_ref: str = Field(min_length=1)
    reference_code: str
    held_since: date
    holding_source: str
    thesis: Literal["INTACT", "WEAKENED", "UNKNOWN"]
    thesis_source: str
    thesis_as_of: date
    platforms: list[PlatformEvidence] = Field(min_length=2, max_length=2)


def platform_gaps(ctx: Context, p: PlatformEvidence, account_ref: str, share: str) -> list[str]:
    gaps = ctx.source_gaps(p.source, account_ref=account_ref) + ctx.source_gaps(p.calendar_source)
    if p.account_ref != account_ref or p.share_class != share:
        gaps.append("PLATFORM_SCOPE_MISMATCH")
    if not p.effective_from <= ctx.as_of < p.effective_to:
        gaps.append("PLATFORM_NOT_EFFECTIVE")
    end = ctx.day + timedelta(days=7)
    if not p.calendar_from <= ctx.day < end <= p.calendar_to:
        gaps.append("PLATFORM_CALENDAR_INCOMPLETE")
    if not any(ctx.day < d <= end for d in p.subscription_days) or not any(
        ctx.day < d <= end for d in p.redemption_days
    ):
        gaps.append("PLATFORM_NO_EVIDENCED_OPEN_DAY")
    if p.restriction == "BLOCKED" or p.per_order_limit_cny == 0 or p.remaining_limit_cny == 0:
        gaps.append("PLATFORM_RESTRICTED")
    return sorted(set(gaps))


def replacement_result(
    ctx: Context,
    rows: list[dict[str, Any]],
    leader: str | None,
    weeks: int,
    evidence: ReplacementEvidence | None,
    required_weeks: int = 4,
) -> dict[str, Any]:
    blockers = []
    if leader is None or weeks < required_weeks:
        blockers.append("FOUR_CONSECUTIVE_VALID_LEADING_WEEKS_REQUIRED")
    if any(r["warnings"] or r["gaps"] or r["exclusions"] for r in rows):
        blockers.append("CURRENT_PRODUCT_WARNING_OR_INCOMPLETE")
    for row in rows:
        cost = row["metrics"].get("standard_external_cost_pct")
        if cost is None or Decimal(cost) > 1:
            blockers.append(row["code"] + ":REPLACEMENT_COST_ABOVE_ONE_PERCENT_OR_UNKNOWN")
    if evidence is None:
        blockers += [
            "ACTUAL_HOLDING_AND_WEAKENED_THESIS_REQUIRED",
            "PLATFORM_LIMIT_EVIDENCE_REQUIRED",
        ]
    else:
        blockers += ctx.source_gaps(evidence.holding_source) + ctx.source_gaps(
            evidence.thesis_source
        )
        if ctx.dataset_kind == "REAL":
            for key in (evidence.holding_source, evidence.thesis_source):
                source = ctx.sources.get(key)
                if (
                    not source
                    or source.quality != "ACCOUNT_OBSERVATION"
                    or source.account_ref != evidence.account_ref
                ):
                    blockers.append("EXACT_ACCOUNT_HOLDING_AND_THESIS_EVIDENCE_REQUIRED")
        codes = {r["code"] for r in rows}
        if evidence.reference_code not in codes or evidence.reference_code == leader:
            blockers.append("REFERENCE_MUST_BE_OTHER_PRODUCT_IN_SAME_POOL")
        if (ctx.day - evidence.held_since).days < 365:
            blockers.append("REFERENCE_HOLDING_BELOW_365_DAYS")
        if evidence.thesis != "WEAKENED" or evidence.thesis_as_of > ctx.day:
            blockers.append("EVIDENCED_WEAKENED_THESIS_REQUIRED")
        if {p.code for p in evidence.platforms} != codes:
            blockers.append("EXACT_PLATFORM_PAIR_REQUIRED")
    return dict(
        replacement="BLOCKED" if blockers else "SHADOW_REPLACEMENT_REVIEW",
        replacement_blockers=sorted(set(blockers)),
        reference_code=evidence.reference_code if evidence else None,
        replacement_evidence=evidence.model_dump(mode="json") if evidence else None,
        money_action=False,
        production_signal=None,
        cost_benefit_prediction=None,
    )
