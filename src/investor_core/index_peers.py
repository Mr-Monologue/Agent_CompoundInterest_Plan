"""Index feeder scope and daily tracking; separate from active fund research."""

from __future__ import annotations

from datetime import date
from itertools import pairwise
from math import sqrt
from statistics import stdev
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from investor_core.benchmarks import Series
from investor_core.execution import StrictModel

if TYPE_CHECKING:
    from investor_core.peer_models import PeerProduct, PeerStudy, PeerWindow


class IndexProfile(StrictModel):
    structure: Literal["ETF_FEEDER", "ETF", "INDEX_FUND"]
    provider: str
    index_code: str
    index_name: str
    variant: Literal["PRICE", "GROSS_TOTAL_RETURN", "NET_TOTAL_RETURN", "UNKNOWN"]
    currency: str
    target_etf_code: str | None = None
    target_etf_name: str | None = None
    official_benchmark: str
    effective_from: date
    effective_to: date | None = None
    evidence_ids: list[str] = Field(min_length=1)


class TrackingReference(StrictModel):
    provider: str
    index_code: str
    variant: Literal["PRICE", "GROSS_TOTAL_RETURN", "NET_TOTAL_RETURN"]
    series: Series
    evidence_ids: list[str] = Field(min_length=1)
    # Explicit target index comparison, never pretend the 95/5 official benchmark.
    purpose: Literal["TARGET_INDEX_ONLY"] = "TARGET_INDEX_ONLY"


def admission(p: PeerProduct, study: PeerStudy) -> tuple[str, str]:
    a = next(x for x in study.products if x.code == study.anchor_code)
    x, y = p.index_profile, a.index_profile
    if not x or not y or x.variant == "UNKNOWN" or y.variant == "UNKNOWN":
        return "UNVERIFIED", "缺少正式指数身份、收益变体或产品结构证据"
    if x.currency != p.currency or y.currency != a.currency:
        return "UNVERIFIED", "指数与基金币种不一致且缺汇率适配"
    if p.market != a.market or p.market == "UNKNOWN":
        return "UNVERIFIED", "投资市场范围不同或未知"
    if not x.target_etf_code or not y.target_etf_code:
        return "UNVERIFIED", "目标ETF身份未取得"
    if p.product_type != "INDEX" or p.active is not False:
        return "EXCLUDED", "非被动指数产品"
    if not p.admission_sources or not p.share_inception or p.share_class == "UNKNOWN":
        return "UNVERIFIED", "准确份额及生效历史未核实"
    if x.structure != "ETF_FEEDER" or y.structure != "ETF_FEEDER":
        return "CONTEXT_ONLY", "ETF与联接结构不同,不进入联接同类主表"
    if (x.provider, x.index_code, x.variant, x.currency, p.currency, p.share_class) != (
        y.provider,
        y.index_code,
        y.variant,
        y.currency,
        a.currency,
        a.share_class,
    ):
        return "EXCLUDED", "指数身份、收益变体、币种或份额结构不同"
    return "COMPARABLE", "相同目标指数、币种及ETF联接份额结构;目标ETF独立,费用和现金拖累分别解释"


def tracking(
    p: PeerProduct, w: PeerWindow, study: PeerStudy, path: dict[str, Any]
) -> dict[str, Any]:
    ref, profile = study.tracking_reference, p.index_profile
    result: dict[str, Any] = dict(
        calculated=None,
        purpose="TARGET_INDEX_ONLY",
        official_benchmark_calculated=False,
        formula="累计基金总收益减目标指数收益(百分点);日超额收益样本标准差(ddof=1)*sqrt(252)*100%",
        frequency="DAILY",
        annualization=252,
        limitation="目标价格指数不含股息,与分红再投资基金有口径差异;不是95/5现金复合官方基准,也不是投资阈值",
    )
    if not ref or not profile:
        return dict(result, gaps=["EXACT_TARGET_INDEX_SERIES_MISSING"])
    if (ref.provider, ref.index_code, ref.variant, ref.series.code, ref.series.currency) != (
        profile.provider,
        profile.index_code,
        profile.variant,
        profile.index_code,
        profile.currency,
    ) or ref.series.validation == "CONFLICT":
        return dict(result, gaps=["INDEX_IDENTITY_OR_SOURCE_CONFLICT"])
    expected_basis = "PRICE" if ref.variant == "PRICE" else "TOTAL_RETURN"
    if ref.series.return_basis != expected_basis or ref.series.provider != ref.provider:
        return dict(result, gaps=["INDEX_RETURN_BASIS_MISMATCH"])
    if not path["calculated"]:
        return dict(result, gaps=["FUND_WINDOW_INCOMPLETE"])
    days = [d for d in study.calendar_dates if w.start <= d <= w.end]
    points = {v.day: v.value for v in ref.series.points}
    if any(d not in points for d in days) or len(days) < 3:
        return dict(result, gaps=["INDEX_COMMON_DATES_INCOMPLETE"])
    index_returns = [float(points[b] / points[a] - 1) for a, b in pairwise(days)]
    active = [f - i for f, i in zip(path["daily_returns"], index_returns, strict=True)]
    index_return = float((points[days[-1]] / points[days[0]] - 1) * 100)
    return dict(
        result,
        gaps=[],
        evidence_ids=ref.evidence_ids + ref.series.evidence_ids,
        calculated=dict(
            index_return_pct=index_return,
            tracking_difference_pp=path["calculated"]["return_pct"] - index_return,
            tracking_error_pct=stdev(active) * sqrt(252) * 100,
            samples=len(active),
        ),
    )
