"""Evidence-limited cross-channel validation, not investment approval or attribution."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_validator

from investor_core.benchmarks import Distribution, Series
from investor_core.execution import StrictModel


class ChannelCheck(StrictModel):
    code: str
    nav: Series
    distributions: list[Distribution] = Field(default_factory=list)
    distribution_from: date
    distribution_to: date
    distribution_sources: list[str] = Field(min_length=1)
    upstream: Literal["SAME_UPSTREAM", "UNKNOWN"]
    lineage_note: str = Field(min_length=1)
    lineage_sources: list[str] = Field(min_length=1)
    # Each increment is backed by source precision, not fitted to observed errors.
    official_decimals: int = Field(ge=0, le=8)
    channel_decimals: int = Field(ge=0, le=8)
    precision_sources: list[str] = Field(min_length=1)
    historical_conflicts: list[str] = Field(default_factory=list)
    conflict_sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self) -> ChannelCheck:
        if self.nav.code != self.code or self.nav.currency != "CNY":
            raise ValueError("Cross-check must identify exact share/currency")
        if self.nav.return_basis != "NAV_NET_INTERNAL_FEES":
            raise ValueError("Cross-channel comparison requires unadjusted unit NAV")
        if self.distribution_from > self.distribution_to:
            raise ValueError("Invalid dividend coverage")
        if len({d.ex_date for d in self.distributions}) != len(self.distributions):
            raise ValueError("Duplicate distribution")
        return self


class Position(StrictModel):
    security_code: str
    name: str
    weight_pct: Decimal = Field(ge=0, le=100)


class Structure(StrictModel):
    code: str
    as_of: date
    evidence_ids: list[str] = Field(min_length=1)
    stock_nav_pct: Decimal = Field(ge=0, le=100)
    top10: list[Position] = Field(min_length=10, max_length=10)
    sector_basis: str = Field(min_length=1)
    sectors_pct: dict[str, Decimal]
    medical_subsectors: Literal["UNKNOWN"] = "UNKNOWN"
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self) -> Structure:
        if len({p.security_code for p in self.top10}) != 10:
            raise ValueError("Top ten must identify ten distinct securities")
        if any(v < 0 or v > 100 for v in self.sectors_pct.values()):
            raise ValueError("Invalid sector weights")
        if sum(p.weight_pct for p in self.top10) > self.stock_nav_pct + Decimal("0.05"):
            raise ValueError("Top-ten rounded weights exceed total stock NAV exposure")
        return self


class ComparisonValidation(StrictModel):
    original_labels: list[str] = Field(min_length=1)
    additional_labels: list[str] = Field(min_length=2, max_length=2)
    window_rule: str = Field(min_length=1)
    checks: list[ChannelCheck] = Field(min_length=2, max_length=2)
    structures: list[Structure] = Field(min_length=2, max_length=2)
    attempts_and_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self) -> ComparisonValidation:
        if len({c.code for c in self.checks}) != 2:
            raise ValueError("Two independent exact products required")
        if {c.code for c in self.checks} != {s.code for s in self.structures}:
            raise ValueError("Structure/check identities differ")
        if len({s.as_of for s in self.structures}) != 1:
            raise ValueError("Structural disclosure dates must match")
        labels = self.original_labels + self.additional_labels
        if len(set(labels)) != len(labels):
            raise ValueError("Window labels must be disjoint")
        return self


def evaluate_validation(study: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    from investor_core.peer_research import path_for

    spec = study.validation
    checks = []
    for ch in spec.checks:
        product = next(p for p in study.products if p.code == ch.code)
        official = {p.day: p.value for p in product.nav.points}
        channel = {p.day: p.value for p in ch.nav.points}
        dividend_a = {d.ex_date: d.cash_per_unit for d in product.distributions}
        dividend_b = {d.ex_date: d.cash_per_unit for d in ch.distributions}
        bound = (Decimal(10) ** -ch.official_decimals + Decimal(10) ** -ch.channel_decimals) / 2
        alternative = product.model_copy(
            update=dict(
                nav=ch.nav,
                distributions=ch.distributions,
                distribution_from=ch.distribution_from,
                distribution_to=ch.distribution_to,
                distribution_sources=ch.distribution_sources,
            )
        )
        windows = []
        for w in study.windows:
            days = [d for d in study.calendar_dates if w.start <= d <= w.end]
            missing = [d.isoformat() for d in days if d not in official or d not in channel]
            differences = [
                dict(
                    day=d.isoformat(),
                    official=str(official[d]),
                    channel=str(channel[d]),
                    difference=str(channel[d] - official[d]),
                    exceeds_rounding_bound=abs(channel[d] - official[d]) > bound,
                )
                for d in days
                if d in official and d in channel and official[d] != channel[d]
            ]
            dividend_diff = [
                dict(
                    ex_date=d.isoformat(),
                    official=str(dividend_a.get(d)),
                    channel=str(dividend_b.get(d)),
                )
                for d in sorted(set(dividend_a) | set(dividend_b))
                if w.start < d <= w.end and dividend_a.get(d) != dividend_b.get(d)
            ]
            a, b = path_for(product, w, study), path_for(alternative, w, study)
            status = "EXACT_CHANNEL_AGREEMENT"
            if missing or a["gaps"] or b["gaps"]:
                status = "INCOMPLETE"
            elif differences or dividend_diff:
                status = (
                    "CONFLICT"
                    if dividend_diff or any(d["exceeds_rounding_bound"] for d in differences)
                    else "ROUNDING_DIFFERENCE_REVIEW"
                )
            windows.append(
                dict(
                    label=w.label,
                    start=str(w.start),
                    end=str(w.end),
                    status=status,
                    dates_checked=len(days) - len(missing),
                    missing_dates=missing,
                    nav_differences=differences,
                    dividend_differences=dividend_diff,
                    official_calculated=a["calculated"],
                    channel_calculated=b["calculated"],
                    gaps=a["gaps"] + b["gaps"],
                    return_difference_pp=(
                        b["calculated"]["return_pct"] - a["calculated"]["return_pct"]
                    )
                    if a["calculated"] and b["calculated"]
                    else None,
                )
            )
        checks.append(
            dict(
                code=ch.code,
                upstream=ch.upstream,
                lineage_note=ch.lineage_note,
                independent_validation=False,
                rounding_bound=str(bound),
                precision_sources=ch.precision_sources,
                windows=windows,
                historical_conflicts=ch.historical_conflicts,
                conflict_sources=ch.conflict_sources,
            )
        )
    # New windows require complete cross-channel coverage. Official paths stay in validation
    # detail as observations when a cross-check fails; no apparently validated main-table result.
    for row in rows:
        for w in row["windows"]:
            if w["label"] in spec.additional_labels and any(
                next(x for x in c["windows"] if x["label"] == w["label"])["status"]
                != "EXACT_CHANNEL_AGREEMENT"
                for c in checks
            ):
                w.update(calculated=None, peer_difference_pp=None, daily_correlation=None)
                w["gaps"].append("NEW_WINDOW_CROSS_CHECK_BLOCKED")
    structures = [s.model_dump(mode="json") for s in spec.structures]
    a, b = spec.structures
    wa = {p.security_code: p.weight_pct for p in a.top10}
    wb = {p.security_code: p.weight_pct for p in b.top10}
    shared = sorted(set(wa) & set(wb))
    for s, obj in zip(structures, spec.structures, strict=True):
        s["top10_nav_pct"] = str(sum(p.weight_pct for p in obj.top10))
        s["publication_dates"] = [str(study.sources[k].published_date) for k in obj.evidence_ids]
        product = next(p for p in study.products if p.code == obj.code)
        s["fees_and_management"] = [
            c.model_dump(mode="json")
            for dimension in ("fees", "management")
            for c in product.dimensions[dimension]
        ]
    direct = next(
        r
        for r in rows
        if r["code"] != study.anchor_code and r["code"] in {c.code for c in spec.checks}
    )
    return dict(
        method_version="peer-validation-v1",
        checks=checks,
        structures=structures,
        structural_date=str(a.as_of),
        shared_top10_codes=shared,
        shared_top10_count=len(shared),
        top10_min_weight_sum_pct=str(sum(min(wa[k], wb[k]) for k in shared)),
        overlap_basis="SUM_MIN_NAV_WEIGHTS_TOP10_ONLY_NOT_FULL_PORTFOLIO",
        stability=[
            dict(
                label=w["label"],
                period="ORIGINAL" if w["label"] in spec.original_labels else "ADDITIONAL",
                difference_pp=w["peer_difference_pp"],
                status="CALCULATED" if w["peer_difference_pp"] is not None else "BLOCKED",
            )
            for w in direct["windows"]
        ],
        window_rule=spec.window_rule,
        attempts_and_gaps=spec.attempts_and_gaps,
        independent_validation=False,
        no_alpha_attribution=True,
    )


def present_validation(result: dict[str, Any], *, details: bool = False) -> str:
    v = result.get("validation")
    if not v:
        return "此历史研究版本未包含结论验证;未自动获取或保存新资料。"
    lines = [
        "结论稳定性报告:" + " / ".join(c["code"] for c in v["checks"]),
        f"查询日 {result['query_date']};共同研究截止 {result['common_research_cutoff']};"
        f"同期结构披露数据日 {v['structural_date']};资料可用截止 {result['knowledge_date']}",
        "本次查询未获取新证据,读取已归档的版本化核验。不同发布渠道不等于独立上游。",
        "",
        "|窗口|"
        + next(c["code"] for c in v["checks"] if c["code"] != result["anchor_code"])
        + "相对"
        + result["anchor_code"]
        + "收益差额|核验|",
        "|---|---|---|",
    ]
    for s in v["stability"]:
        states = [
            next(w for w in c["windows"] if w["label"] == s["label"])["status"] for c in v["checks"]
        ]
        ok = all(x == "EXACT_CHANNEL_AGREEMENT" for x in states)
        value = f"{s['difference_pp']:+.2f}个百分点" if s["difference_pp"] is not None else "阻断"
        lines.append(
            f"|{s['label']}|{value}|" + ("逐日值及窗口内分红一致" if ok else ",".join(states)) + "|"
        )
    supported = [
        s["label"]
        for s in v["stability"]
        if s["period"] == "ORIGINAL"
        and all(
            next(w for w in c["windows"] if w["label"] == s["label"])["status"]
            == "EXACT_CHANNEL_AGREEMENT"
            for c in v["checks"]
        )
    ]
    lines += [
        "",
        "原结论进一步支持的窗口:" + ("、".join(supported) or "尚无完整核验"),
        (
            "相对差额正负随窗口变化。"
            if len(
                {
                    s["difference_pp"] > 0
                    for s in v["stability"]
                    if s["difference_pp"] is not None and s["difference_pp"] != 0
                }
            )
            > 1
            else "已覆盖窗口未观察到领先方向反转。"
        )
        + "不能据此认定持续优胜或选股Alpha。",
        "结构证据(期末,不是期间平均):",
    ]
    for s in v["structures"]:
        lines.append(
            f"{s['code']}:股票占净资产{float(s['stock_nav_pct']):.2f}%;"
            f"前十大占净资产{float(s['top10_nav_pct']):.2f}%;"
            f"披露日{','.join(s['publication_dates'])}。"
        )
        lines.append(";".join(c["text"] for c in s["fees_and_management"]))
        if details:
            lines.append(s["sector_basis"] + ":" + str(s["sectors_pct"]))
    lines += [
        f"前十大共有{v['shared_top10_count']}只,重合名称权重取两者较小值之和"
        f"{float(v['top10_min_weight_sum_pct']):.2f}%;仅截取前十大,不是完整组合重合。",
        "费用/经理:当期披露条款与任期见同类比较详情;持续费用已含净值,不再扣减。"
        "期末结构支持暴露不同,不能证明期间收益差额由此造成。",
        "WARNING:核验上游未证实独立,原单源及既有重算差异警告保留。"
        "细分医疗行业、期间持仓变动/贡献、个人申赎费仍未知。",
        "最值得补证:同口径期间持仓与变动,以区分仓位、行业和个股暴露;现有证据不足以证明选择能力。",
    ]
    for c in v["checks"]:
        lines.append(f"{c['code']}:{c['lineage_note']}")
        lines += c["historical_conflicts"]
    lines += v["attempts_and_gaps"]
    if details:
        for c in v["checks"]:
            lines.append(
                f"{c['code']}精度舍入上界{c['rounding_bound']}元;"
                "非零差异即保留/待核,未用容差清除冲突。"
            )
            for w in c["windows"]:
                lines.append(
                    f"{w['label']} {w['start']}至{w['end']}:核对{w['dates_checked']}日;"
                    f"缺日{w['missing_dates']};净值差异{w['nav_differences']};"
                    f"分红差异{w['dividend_differences']};缺口{w['gaps']}"
                )
    return "\n".join(lines)
