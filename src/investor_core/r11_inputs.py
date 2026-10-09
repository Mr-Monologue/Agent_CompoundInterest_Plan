"""Frozen R1.1 input contracts; no I/O, inference, fill or financial writes."""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, datetime, time
from decimal import Decimal, localcontext
from importlib.resources import files
from itertools import pairwise
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from investor_core.execution import StrictModel
from investor_core.scheduler import digest

DEFINITION = json.loads(files("investor_core").joinpath("r11_definition.json").read_text())
VERSION = DEFINITION["definition_id"]
MODEL_VERSION = "1.0.2"
COMPUTATION_VERSION = "r11-rules-v4"
TZ = ZoneInfo("Asia/Shanghai")
D = Decimal
Json = dict[str, Any]


class Source(StrictModel):
    archive_id: str = Field(min_length=1)
    document_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    url: str = Field(pattern=r"^(https://|attachment:sha256:[a-f0-9]{64}$)")
    lineage: str = Field(min_length=1)
    published_at: datetime
    publication_precision: Literal["INSTANT", "DATE"] = "INSTANT"
    publication_timezone: str
    first_retrieved_at: datetime
    quality: Literal["OFFICIAL", "ACCOUNT_OBSERVATION", "UNVERIFIED", "CONFLICT"]
    account_ref: str | None = None

    @model_validator(mode="after")
    def aware(self) -> Source:
        if self.published_at.tzinfo is None or self.first_retrieved_at.tzinfo is None:
            raise ValueError("explicit timezone required")
        ZoneInfo(self.publication_timezone)
        return self

    def known_at(self) -> datetime:
        published = self.published_at
        if self.publication_precision == "DATE":
            zone = ZoneInfo(self.publication_timezone)
            published = datetime.combine(published.astimezone(zone).date(), time.max, zone)
        return max(published, self.first_retrieved_at)


class Context(StrictModel):
    as_of: datetime
    evidence_class: Literal["H", "K", "F"]
    dataset_kind: Literal["SYNTHETIC", "REAL"]
    sources: dict[str, Source]

    @model_validator(mode="after")
    def cutoff(self) -> Context:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of requires timezone")
        local = self.as_of.astimezone(TZ)
        self.as_of = local
        if local.weekday() != 5 or local.time() != time(12):
            raise ValueError("R1.1 cutoff is Saturday 12:00 Asia/Shanghai")
        return self

    @property
    def day(self) -> date:
        return self.as_of.astimezone(TZ).date()

    def source_gaps(self, key: str | None, *, account_ref: str | None = None) -> list[str]:
        if key is None or key not in self.sources:
            return ["SOURCE_MISSING"]
        src = self.sources[key]
        gaps = []
        account_capture = (
            account_ref is not None
            and src.account_ref == account_ref
            and src.quality == "ACCOUNT_OBSERVATION"
        )
        if src.quality != "OFFICIAL" and not account_capture:
            gaps.append("SOURCE_" + src.quality)
        if self.evidence_class != "H" and src.known_at() > self.as_of:
            gaps.append("NOT_KNOWN_AS_OF")
        return gaps


class Calendar(StrictModel):
    source: str
    covered_from: date
    covered_to: date
    dates: list[date] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> Calendar:
        if self.dates != sorted(set(self.dates)):
            raise ValueError("calendar must be unique and ordered")
        if self.covered_from > self.dates[0] or self.covered_to < self.dates[-1]:
            raise ValueError("calendar outside documented coverage")
        return self

    def grid(self, ctx: Context) -> tuple[list[date], list[str]]:
        gaps = ctx.source_gaps(self.source)
        if not self.covered_from <= ctx.day <= self.covered_to:
            gaps.append("CALENDAR_COVERAGE_UNKNOWN")
        days = [d for d in self.dates if d < ctx.day]
        if not days:
            gaps.append("CALENDAR_EMPTY")
        return days, gaps


class Point(StrictModel):
    day: date
    value: Decimal | None
    source: str

    @model_validator(mode="after")
    def finite(self) -> Point:
        if self.value is not None and not self.value.is_finite():
            raise ValueError("finite value required")
        return self


class Series(StrictModel):
    identity: str
    unit: str
    points: list[Point] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self) -> Series:
        if len({p.day for p in self.points}) != len(self.points):
            raise ValueError("duplicate dates require a separate immutable revision")
        return self

    def values(self, ctx: Context, days: list[date], unit: str) -> tuple[list[Decimal], list[str]]:
        gaps = [] if self.unit == unit else ["UNIT_MISMATCH"]
        points = {p.day: p for p in self.points}
        values = []
        for day in days:
            point = points.get(day)
            if point is None or point.value is None:
                gaps.append("POINT_MISSING:" + day.isoformat())
                continue
            if day > ctx.day:
                gaps.append("FUTURE_DATA_PERIOD")
            gaps.extend(ctx.source_gaps(point.source))
            values.append(point.value)
        return values, sorted(set(gaps))

    def window(
        self, ctx: Context, calendar: Calendar, count: int, lag: int, unit: str
    ) -> tuple[list[Decimal], list[date], list[str]]:
        grid, gaps = calendar.grid(ctx)
        eligible = [p.day for p in self.points if p.day in grid]
        if not eligible:
            return [], [], sorted(set([*gaps, "SERIES_EMPTY"]))
        end = max(eligible)
        pos = grid.index(end)
        if len(grid) - 1 - pos > lag:
            gaps.append("STALE_DATA")
        days = grid[max(0, pos + 1 - count) : pos + 1]
        if len(days) != count:
            gaps.append("HISTORY_INSUFFICIENT")
        values, missing = self.values(ctx, days, unit)
        return values, days, sorted(set(gaps + missing))


def monthly_points(series: Series, ctx: Context, count: int = 4) -> list[Point]:
    eligible = [
        p
        for p in series.points
        if p.day <= ctx.day
        and (
            ctx.evidence_class == "H"
            or p.source not in ctx.sources
            or ctx.sources[p.source].known_at() <= ctx.as_of
        )
    ]
    return sorted(eligible, key=lambda p: p.day)[-count:]


def monthly(
    series: Series, ctx: Context, unit: str, count: int = 4
) -> tuple[list[Decimal], list[str]]:
    points = monthly_points(series, ctx, count)
    gaps = []
    if len(points) != count:
        gaps.append("FOUR_MONTHS_REQUIRED")
    if points and (ctx.day - points[-1].day).days > 62:
        gaps.append("MONTHLY_STALE")
    months = [p.day.year * 12 + p.day.month for p in points]
    if any(b - a != 1 for a, b in pairwise(months)):
        gaps.append("MONTH_SEQUENCE_GAP")
    if any(p.day.day != monthrange(p.day.year, p.day.month)[1] for p in points):
        gaps.append("MONTH_END_REQUIRED")
    values, missing = series.values(ctx, [p.day for p in points], unit)
    return values, sorted(set(gaps + missing))


def clip(x: Decimal) -> Decimal:
    return min(D(1), max(D(-1), x))


def up(x: Decimal, a: str, b: str) -> Decimal:
    return D(100) * min(D(1), max(D(0), (x - D(a)) / (D(b) - D(a))))


def down(x: Decimal, a: str, b: str) -> Decimal:
    return D(100) - up(x, a, b)


def sample_sd(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        raise ValueError("sample requires at least two values")
    with localcontext() as context:
        context.prec = 34
        mean = sum(values, D(0)) / len(values)
        return (sum(((x - mean) ** 2 for x in values), D(0)) / (len(values) - 1)).sqrt()


def base_output(ctx: Context, inputs: StrictModel, method: str) -> Json:
    body = inputs.model_dump(mode="json")
    return dict(
        definition_id=VERSION,
        computation_version=COMPUTATION_VERSION,
        definition_source=DEFINITION,
        method=method,
        as_of=ctx.as_of.isoformat(),
        evidence_class=ctx.evidence_class,
        dataset_kind=ctx.dataset_kind,
        input_hash=digest(body),
        inputs=body,
        probabilities=None,
        probability_status="NOT_CALIBRATED",
        money_action=False,
        actual_promotion_authorized=False,
        warnings=["RESEARCH_HYPOTHESIS_NOT_VALIDATED", "NOT_INVESTMENT_QUALIFICATION"],
        retrospective_vintages=[k for k, s in ctx.sources.items() if s.known_at() > ctx.as_of],
    )
