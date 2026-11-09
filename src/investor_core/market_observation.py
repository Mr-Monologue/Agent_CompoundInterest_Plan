"""Source-bound, read-only observations; no database, network or trading side effects."""

from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(StrictModel):
    id: str = Field(min_length=1)
    url: HttpUrl
    publisher: str = Field(min_length=1)
    published_on: date
    accessed_on: date


class Close(StrictModel):
    date: date
    value: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    source_id: str


class Series(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    # Explicit return definition prevents index price changes becoming fund total returns.
    basis: Literal["PRICE_INDEX", "TOTAL_RETURN_INDEX"]
    currency: str = Field(min_length=1)
    closes: list[Close]

    @model_validator(mode="after")
    def unique_dates(self) -> Series:
        if len({point.date for point in self.closes}) != len(self.closes):
            raise ValueError(f"{self.id}: duplicate observation date")
        return self


class ObservationPacket(StrictModel):
    title: str = Field(min_length=1)
    as_of: date
    period_start: date
    period_end: date
    previous_session: date
    sources: list[Source] = Field(min_length=1)
    series: list[Series] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence(self) -> ObservationPacket:
        if not self.period_start <= self.previous_session < self.period_end <= self.as_of:
            raise ValueError("Require start <= previous session < end <= as_of")
        sources = {source.id: source for source in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("Duplicate source id")
        if len({item.id for item in self.series}) != len(self.series):
            raise ValueError("Duplicate series id")
        for source in self.sources:
            if not source.published_on <= source.accessed_on <= self.as_of:
                raise ValueError("Source unavailable at observation cutoff")
        for item in self.series:
            for point in item.closes:
                point_source = sources.get(point.source_id)
                if point_source is None:
                    raise ValueError(f"{item.id}: unknown source {point.source_id}")
                if point.date > point_source.published_on or point.date > self.period_end:
                    raise ValueError(
                        f"{item.id}: future observation or inconsistent publication date"
                    )
        return self


class ObservationResult(StrictModel):
    id: str
    name: str
    basis: str
    currency: str
    period_return_pct: str | None
    last_session_return_pct: str | None
    missing_dates: list[date]
    evidence: list[Close]
    interpretation: str


class ObservationReport(StrictModel):
    title: str
    as_of: date
    period_start: date
    period_end: date
    previous_session: date
    results: list[ObservationResult]
    sources: list[Source]
    quality: Literal["UNVERIFIED"] = "UNVERIFIED"
    limitations: list[str]
    display_text: str


def change(points: dict[date, Close], start: date, end: date) -> str | None:
    if start not in points or end not in points:
        return None
    result = (points[end].value / points[start].value - 1) * 100
    return str(result.quantize(Decimal("0.01")))


def observe(packet: ObservationPacket) -> ObservationReport:
    """Compute only exact requested endpoints; never silently forward-fill a close."""
    results = []
    for series in packet.series:
        points = {point.date: point for point in series.closes}

        period = change(points, packet.period_start, packet.period_end)
        daily = change(points, packet.previous_session, packet.period_end)
        interpretation = "仅描述已提供指数, 不能推断全市场、基金收益或买卖时机。"
        if period is not None and daily is not None:
            if Decimal(period) > 0 and Decimal(daily) < 0:
                interpretation = "区间上涨、最后交易日回落; 单日下跌不等于整个区间下跌。"
            elif Decimal(period) < 0 and Decimal(daily) > 0:
                interpretation = "区间下跌、最后交易日反弹; 单日反弹不等于区间趋势反转。"
        requested = sorted({packet.period_start, packet.previous_session, packet.period_end})
        results.append(
            ObservationResult(
                id=series.id,
                name=series.name,
                basis=series.basis,
                currency=series.currency,
                period_return_pct=period,
                last_session_return_pct=daily,
                missing_dates=[day for day in requested if day not in points],
                evidence=[points[day] for day in requested if day in points],
                interpretation=interpretation,
            )
        )
    limitations = [
        "计算校验不等于来源独立核验; 来源与日期由输入提供, 保留 UNVERIFIED。",
        "前一交易日由调用方按相应市场日历提供, 本工具未独立核验交易日历。",
        "未提供历史分布: 异常分位、统计显著性与未来方向均未判断。",
        "指数变化不是组合收益; 没有持仓穿透和匹配净值, 不能生成持仓归因。",
        "本工具不抓取行情, 不代表数据已更新; 不写入账本、策略或计划。",
    ]
    lines = [
        packet.title,
        f"观察截止 {packet.as_of}; 端点 {packet.period_start} → "
        f"{packet.period_end}; 前一交易日 {packet.previous_session}",
    ]
    for result in results:
        period_text = (
            f"{result.period_return_pct}%" if result.period_return_pct is not None else "缺失"
        )
        day_text = (
            f"{result.last_session_return_pct}%"
            if result.last_session_return_pct is not None
            else "缺失"
        )
        lines.append(f"{result.name}: 区间 {period_text}; 末日 {day_text}。{result.interpretation}")
        if result.missing_dates:
            lines.append("缺少端点: " + ", ".join(map(str, result.missing_dates)))
    lines.extend(limitations)
    lines.extend(f"[{source.id}] {source.publisher} {source.url}" for source in packet.sources)
    return ObservationReport(
        title=packet.title,
        as_of=packet.as_of,
        period_start=packet.period_start,
        period_end=packet.period_end,
        previous_session=packet.previous_session,
        results=results,
        sources=packet.sources,
        limitations=limitations,
        display_text="\n".join(lines),
    )


def load_report(path: Path) -> ObservationReport:
    return observe(ObservationPacket.model_validate_json(path.read_text(encoding="utf-8")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        report = load_report(args.packet)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Invalid observation packet: {exc}\n")
    print(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
        if args.json
        else report.display_text
    )


if __name__ == "__main__":
    main()
