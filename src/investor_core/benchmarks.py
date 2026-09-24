"""Versioned research benchmarks and strict, non-trading relative diagnostics."""

from __future__ import annotations

import hmac
import json
import math
import secrets
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from statistics import correlation
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from investor_core.execution import StrictModel
from investor_core.ledger import LedgerError
from investor_core.notebook import NotebookService
from investor_core.scheduler import digest, instant, stamp

Json = dict[str, Any]


class Component(StrictModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    code: str | None = None
    currency: str = Field(min_length=3, max_length=3)
    return_basis: Literal["PRICE", "TOTAL_RETURN", "PUBLISHED_INDEX", "CASH_RATE", "UNKNOWN"]
    weight_bps: int = Field(gt=0, le=10000)
    evidence_ids: list[str] = Field(min_length=1)
    availability: str = Field(min_length=1)


class BenchmarkPeriod(StrictModel):
    effective_from: date | None
    effective_to: date | None = None  # inclusive
    observed_on: date
    components: list[Component] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    method: Literal["DAILY_REBALANCED", "UNKNOWN"] = "UNKNOWN"
    limitation: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self) -> BenchmarkPeriod:
        if sum(c.weight_bps for c in self.components) != 10000:
            raise ValueError("Composite weights must sum to 10000 bps")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("Invalid validity interval")
        identities = [(c.provider, c.code or c.name) for c in self.components]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate component identity")
        return self


class MappingDraft(StrictModel):
    portfolio_id: str
    instrument_code: str
    expected_previous_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    official_disclosures: list[BenchmarkPeriod] = Field(min_length=1)
    diagnostic_mapping: list[BenchmarkPeriod] = Field(min_length=1)
    purpose: Literal["RESEARCH_REFERENCE"] = "RESEARCH_REFERENCE"
    suitability: Literal["UNASSESSED", "LIMITED_REFERENCE"] = "LIMITED_REFERENCE"
    rationale: str = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    actor_ref: str = "codex"

    @model_validator(mode="after")
    def periods(self) -> MappingDraft:
        for periods in [self.official_disclosures, self.diagnostic_mapping]:
            known = sorted(
                (p for p in periods if p.effective_from), key=lambda p: p.effective_from or date.min
            )
            for a, b in pairwise(known):
                if a.effective_to is None or a.effective_to >= (b.effective_from or date.min):
                    raise ValueError("Overlapping benchmark versions")
        return self


class MappingApproval(StrictModel):
    confirmation_token: str
    confirmed_by: str = Field(min_length=1)


class Point(StrictModel):
    day: date
    value: Decimal = Field(gt=0, allow_inf_nan=False)


class Series(StrictModel):
    provider: str
    code: str
    currency: str
    return_basis: Literal[
        "NAV_NET_INTERNAL_FEES",
        "FUND_TOTAL_RETURN",
        "PRICE",
        "TOTAL_RETURN",
        "PUBLISHED_INDEX",
        "CASH_RATE",
        "UNKNOWN",
    ]
    evidence_ids: list[str] = Field(min_length=1)
    points: list[Point] = Field(default_factory=list, max_length=20000)
    validation: Literal["SINGLE_SOURCE", "CROSS_CHECKED", "CONFLICT"] = "SINGLE_SOURCE"

    @model_validator(mode="after")
    def ordered(self) -> Series:
        days = [p.day for p in self.points]
        if days != sorted(set(days)):
            raise ValueError("Series must have unique, ordered dates")
        return self


class Distribution(StrictModel):
    ex_date: date
    cash_per_unit: Decimal = Field(ge=0, allow_inf_nan=False)


class ReportedWindow(StrictModel):
    start: date
    end: date
    fund_return_pct: Decimal = Field(gt=-100, allow_inf_nan=False)
    benchmark_return_pct: Decimal = Field(gt=-100, allow_inf_nan=False)
    evidence_ids: list[str] = Field(min_length=1)
    return_basis: Literal["ISSUER_REPORTED_NET_NAV_GROWTH"] = "ISSUER_REPORTED_NET_NAV_GROWTH"
    limitation: str = Field(min_length=1)

    @model_validator(mode="after")
    def interval(self) -> ReportedWindow:
        if self.end <= self.start:
            raise ValueError("Invalid reported window")
        return self


class DiagnosticInput(StrictModel):
    mapping_id: str
    idempotency_key: str = Field(min_length=1, max_length=200)
    start: date
    end: date
    expected_dates: list[date] = Field(default_factory=list, max_length=20000)
    calendar_evidence_ids: list[str] = Field(default_factory=list)
    fund: Series
    benchmarks: list[Series] = Field(default_factory=list)
    distributions: list[Distribution] = Field(default_factory=list)
    distribution_coverage_from: date | None = None
    distribution_coverage_to: date | None = None
    distribution_evidence_ids: list[str] = Field(default_factory=list)
    reported_windows: list[ReportedWindow] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def dates(self) -> DiagnosticInput:
        if self.end <= self.start or self.expected_dates != sorted(set(self.expected_dates)):
            raise ValueError("Invalid diagnostic dates")
        if len({(s.provider, s.code) for s in self.benchmarks}) != len(self.benchmarks):
            raise ValueError("Duplicate benchmark series")
        if len({d.ex_date for d in self.distributions}) != len(self.distributions):
            raise ValueError("Duplicate distribution date")
        if self.fund.return_basis == "FUND_TOTAL_RETURN" and self.distributions:
            raise ValueError("Already reinvested fund series cannot add dividends twice")
        return self


def diagnose(mapping: Json, request: DiagnosticInput) -> Json:
    """Exact endpoints/calendar. No fill, no implicit FX, no fee double deduction."""
    gaps: set[str] = set()
    dates = request.expected_dates
    if (
        not dates
        or dates[0] != request.start
        or dates[-1] != request.end
        or not request.calendar_evidence_ids
    ):
        gaps.add("EXACT_COMMON_CALENDAR_MISSING")
    if request.fund.code != mapping["instrument_code"]:
        gaps.add("FUND_IDENTITY_MISMATCH")
    if request.fund.return_basis not in {"NAV_NET_INTERNAL_FEES", "FUND_TOTAL_RETURN"}:
        gaps.add("FUND_RETURN_BASIS_UNKNOWN")
    if request.fund.validation == "CONFLICT":
        gaps.add("FUND_SOURCE_CONFLICT")
    if request.fund.return_basis == "NAV_NET_INTERNAL_FEES" and (
        not request.distribution_evidence_ids
        or not request.distribution_coverage_from
        or not request.distribution_coverage_to
        or request.distribution_coverage_from > request.start
        or request.distribution_coverage_to < request.end
    ):
        gaps.add("DIVIDEND_COVERAGE_INCOMPLETE")
    if any(
        d.ex_date > request.start and d.ex_date <= request.end and d.ex_date not in dates
        for d in request.distributions
    ):
        gaps.add("DIVIDEND_DATE_NOT_IN_CALENDAR")
    fund = {p.day: p.value for p in request.fund.points}
    if any(d not in fund for d in dates):
        gaps.add("FUND_NAV_MISSING")
    series = {(s.provider, s.code): s for s in request.benchmarks}
    values_by_key = {key: {p.day: p.value for p in value.points} for key, value in series.items()}
    selected: list[Json] = []
    for previous, day in pairwise(dates):
        periods = [
            p
            for p in mapping["diagnostic_mapping"]
            if p["effective_from"]
            and p["effective_from"] <= day.isoformat()
            and (not p["effective_to"] or day.isoformat() <= p["effective_to"])
        ]
        if len(periods) != 1:
            gaps.add("BENCHMARK_HISTORY_UNCOVERED")
            continue
        period = periods[0]
        selected.append(period)
        if period["method"] != "DAILY_REBALANCED":
            gaps.add("REBALANCING_METHOD_UNKNOWN")
        for component in period["components"]:
            s = series.get((component["provider"], component["code"]))
            if not component["code"] or component["return_basis"] == "UNKNOWN":
                gaps.add("BENCHMARK_IDENTITY_OR_BASIS_UNKNOWN")
            if s is None:
                gaps.add("BENCHMARK_SERIES_MISSING")
                continue
            if s.currency != request.fund.currency or s.currency != component["currency"]:
                gaps.add("CURRENCY_OR_FX_MISMATCH")
            if s.return_basis != component["return_basis"] or s.return_basis == "CASH_RATE":
                gaps.add("BENCHMARK_RETURN_BASIS_MISMATCH")
            if s.validation == "CONFLICT":
                gaps.add("BENCHMARK_SOURCE_CONFLICT")
            values = values_by_key[(s.provider, s.code)]
            if day not in values or previous not in values:
                gaps.add("BENCHMARK_DATE_MISSING")
    disclosed = []
    for w in request.reported_windows:
        disclosed.append(
            dict(
                w.model_dump(mode="json"),
                excess_percentage_points=float(w.fund_return_pct - w.benchmark_return_pct),
                basis="ISSUER_DISCLOSURE_NOT_DAILY_RECONSTRUCTION",
            )
        )
    result: Json = dict(
        status="DATA_BLOCKED" if gaps else "COMPUTED_RESEARCH_ONLY",
        mapping_id=mapping["id"],
        mapping_version=mapping["version"],
        mapping_status=mapping["status"],
        money_action=False,
        start=request.start.isoformat(),
        end=request.end.isoformat(),
        gaps=sorted(gaps),
        warnings=[
            "RESEARCH_ONLY",
            "NO_ALPHA_ATTRIBUTION",
            "OFFICIAL_BENCHMARK_NOT_STRONG_PROXY",
            "FUND_INTERNAL_FEES_ALREADY_IN_NAV",
            "INVESTOR_ENTRY_EXIT_FEES_NOT_INCLUDED",
        ],
        reported_windows=disclosed,
        calculated=None,
        limitations=request.limitations,
    )
    if any(s.validation != "CROSS_CHECKED" for s in [request.fund, *request.benchmarks]):
        result["warnings"].append("SINGLE_SOURCE_WARNING")
    if not gaps:
        fund_wealth = benchmark_wealth = Decimal(1)
        peak = Decimal(1)
        drawdown = Decimal(0)
        fr: list[float] = []
        br: list[float] = []
        rows = []
        dividends = {d.ex_date: d.cash_per_unit for d in request.distributions}
        for prev, day, period in zip(dates[:-1], dates[1:], selected, strict=True):
            f = (fund[day] + dividends.get(day, Decimal(0))) / fund[prev] - 1
            b = Decimal(0)
            for component in period["components"]:
                values = values_by_key[(component["provider"], component["code"])]
                b += Decimal(component["weight_bps"]) / 10000 * (values[day] / values[prev] - 1)
            fund_wealth *= 1 + f
            benchmark_wealth *= 1 + b
            peak = max(peak, fund_wealth)
            drawdown = min(drawdown, fund_wealth / peak - 1)
            fr.append(float(f))
            br.append(float(b))
            rows.append(
                dict(
                    day=day.isoformat(),
                    fund_return=float(f),
                    benchmark_return=float(b),
                    benchmark_effective_from=period["effective_from"],
                )
            )
        corr = None
        if len(fr) >= 20 and len(set(fr)) > 1 and len(set(br)) > 1:
            corr = correlation(fr, br)
            if not math.isfinite(corr):
                corr = None
        else:
            result["warnings"].append("CORRELATION_INSUFFICIENT_OR_CONSTANT")
        result["calculated"] = dict(
            fund_return_pct=float((fund_wealth - 1) * 100),
            benchmark_return_pct=float((benchmark_wealth - 1) * 100),
            excess_percentage_points=float((fund_wealth - benchmark_wealth) * 100),
            fund_max_drawdown_pct=float(drawdown * 100),
            daily_correlation=corr,
            observations=len(fr),
            daily=rows,
            method="CHAIN_DAILY_WEIGHTED_RETURNS; EFFECTIVE_DATE_SELECTS_END_OF_DAY_RETURN",
        )
    reconciliation = []
    if result["calculated"]:
        for window in request.reported_windows:
            if window.start != request.start or window.end != request.end:
                continue
            actual = result["calculated"]
            fund_delta = actual["fund_return_pct"] - float(window.fund_return_pct)
            benchmark_delta = actual["benchmark_return_pct"] - float(window.benchmark_return_pct)
            within_rounding = abs(fund_delta) <= 0.005 and abs(benchmark_delta) <= 0.005
            reconciliation.append(
                dict(
                    fund_difference_pp=fund_delta,
                    benchmark_difference_pp=benchmark_delta,
                    within_two_decimal_rounding=within_rounding,
                    independent_validation=False,
                )
            )
            if not within_rounding:
                result["warnings"].append("DISCLOSED_RETURN_MISMATCH")
                result["status"] = "COMPUTED_WITH_RECONCILIATION_WARNING"
    result["reconciliation"] = reconciliation
    lines = ["相对表现研究 | " + result["status"] + " | 不参与正式风险动作"]
    for disclosure in disclosed:
        lines.append(
            f"披露区间 {disclosure['start']} 至 {disclosure['end']}: "
            f"基金 {disclosure['fund_return_pct']}%, "
            f"基准 {disclosure['benchmark_return_pct']}%, "
            f"差额 {disclosure['excess_percentage_points']:.2f} 个百分点;非日序列重建"
        )
    if result["calculated"]:
        x = result["calculated"]
        lines.append(
            f"实际序列重算: 基金 {x['fund_return_pct']:.4f}%, "
            f"基准 {x['benchmark_return_pct']:.4f}%, "
            f"差额 {x['excess_percentage_points']:.4f} 个百分点"
        )
    lines.append("验证警告: " + ", ".join(result["warnings"]))
    lines.extend(
        [
            "缺口: " + ", ".join(sorted(gaps)),
            "超额差额不是选股 Alpha;累计亏损不是最大回撤;相关性不证明因果。",
        ]
    )
    result["display_text"] = "\n".join(lines)
    return result


class BenchmarkService:
    def __init__(self, notebook: NotebookService) -> None:
        self.notebook = notebook
        self.research = notebook.research

    def create(self, request: MappingDraft) -> Json:
        payload = request.model_dump(mode="json")
        ids = {
            e
            for p in [*request.official_disclosures, *request.diagnostic_mapping]
            for e in p.evidence_ids
        }
        ids |= {
            e
            for p in [*request.official_disclosures, *request.diagnostic_mapping]
            for x in p.components
            for e in x.evidence_ids
        }
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self.notebook._scope(c, request.portfolio_id, request.instrument_code)
            replay = c.execute(
                "SELECT payload_json FROM research_benchmark_mappings WHERE request_key=?",
                (request.idempotency_key,),
            ).fetchone()
            if replay:
                data = json.loads(replay[0])
                if data["request_hash"] != digest(payload):
                    raise LedgerError("IDEMPOTENCY_CONFLICT", "Mapping key content mismatch")
                data.pop("confirmation_digest", None)
                return dict(data, idempotent_replay=True)
            self.notebook._evidence(c, request.instrument_code, ids)
            previous = c.execute(
                "SELECT COALESCE(MAX(version),0) FROM research_benchmark_mappings "
                "WHERE portfolio_id=? AND instrument_code=?",
                (request.portfolio_id, request.instrument_code),
            ).fetchone()[0]
            if previous != request.expected_previous_version:
                raise LedgerError(
                    "BENCHMARK_VERSION_STALE", "Read current mapping before creating a new version"
                )
            token = secrets.token_urlsafe(32)
            data = dict(
                payload,
                id=str(uuid4()),
                version=previous + 1,
                status="DRAFT",
                money_action=False,
                request_hash=digest(payload),
                created_at=stamp(self.research._now()),
                expires_at=stamp(self.research._now() + timedelta(hours=24)),
                confirmation_digest=digest(token),
            )
            c.execute(
                "INSERT INTO research_benchmark_mappings VALUES (?,?,?,?,?,?)",
                (
                    data["id"],
                    request.portfolio_id,
                    request.instrument_code,
                    data["version"],
                    request.idempotency_key,
                    json.dumps(data, ensure_ascii=False),
                ),
            )
            data.pop("confirmation_digest")
            return dict(data, confirmation_token=token)

    def read(self, portfolio_id: str, code: str) -> Json:
        with self.research._connect() as c:
            self.notebook._scope(c, portfolio_id, code)
            versions = [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT payload_json FROM research_benchmark_mappings "
                    "WHERE portfolio_id=? AND instrument_code=? ORDER BY version",
                    (portfolio_id, code),
                )
            ]
            runs = [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT payload_json FROM research_benchmark_runs "
                    "WHERE portfolio_id=? AND instrument_code=? ORDER BY rowid",
                    (portfolio_id, code),
                )
            ]
        for v in versions:
            v.pop("confirmation_digest", None)
        lines = [
            f"{code} 基准研究 | {len(versions)} 个映射版本 / {len(runs)} 次研究",
            "不修改策略估值代理或风险动作;官方基准不是 STRONG 代理结论。",
        ]
        if versions:
            latest = versions[-1]
            lines.append(f"最新映射草稿版本 {latest['version']} | {latest['status']}")
            for period in latest["diagnostic_mapping"]:
                weights = " + ".join(
                    f"{x['name']} ({x['code'] or '代码未核实'}) {x['weight_bps'] / 100:g}%"
                    for x in period["components"]
                )
                lines.append(
                    f"{period['effective_from'] or '起始未核实'} 至 "
                    f"{period['effective_to'] or '未公告终止'}: {weights}"
                )
            lines += [latest["rationale"], *latest["limitations"]]
        if runs:
            lines.append(runs[-1]["display_text"])
        approved = [v["version"] for v in versions if v["status"] == "APPROVED_RESEARCH"]
        return dict(
            versions=versions,
            runs=runs,
            money_action=False,
            approved_research_version=max(approved, default=None),
            display_text="\n".join(lines),
        )

    def approve(self, mapping_id: str, request: MappingApproval) -> Json:
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT payload_json FROM research_benchmark_mappings WHERE id=?", (mapping_id,)
            ).fetchone()
            if not row:
                raise LedgerError("BENCHMARK_NOT_FOUND", "Mapping not found", http_status=404)
            data: Json = json.loads(row[0])
            if not hmac.compare_digest(
                data["confirmation_digest"], digest(request.confirmation_token)
            ):
                raise LedgerError("CONFIRMATION_MISMATCH", "Invalid confirmation")
            if data["status"] == "APPROVED_RESEARCH":
                data.pop("confirmation_digest")
                return dict(data, idempotent_replay=True)
            if instant(data["expires_at"]) <= self.research._now():
                raise LedgerError("DRAFT_EXPIRED", "Review a new draft")
            latest = c.execute(
                "SELECT MAX(version) FROM research_benchmark_mappings "
                "WHERE portfolio_id=? AND instrument_code=?",
                (data["portfolio_id"], data["instrument_code"]),
            ).fetchone()[0]
            if data["version"] != latest:
                raise LedgerError("BENCHMARK_VERSION_STALE", "A newer draft exists")
            if any(
                not p["effective_from"]
                or p["method"] == "UNKNOWN"
                or any(not x["code"] or x["return_basis"] == "UNKNOWN" for x in p["components"])
                for p in data["diagnostic_mapping"]
            ):
                raise LedgerError(
                    "BENCHMARK_INCOMPLETE", "Identity, validity and method must be resolved"
                )
            data.update(
                status="APPROVED_RESEARCH",
                confirmed_by=request.confirmed_by,
                confirmed_at=stamp(self.research._now()),
            )
            c.execute(
                "UPDATE research_benchmark_mappings SET payload_json=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), mapping_id),
            )
            data.pop("confirmation_digest")
            return data

    def run(self, request: DiagnosticInput, *, persist: bool = True) -> Json:
        payload = request.model_dump(mode="json")
        with self.research._connect() as c:
            if persist:
                c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT payload_json FROM research_benchmark_mappings WHERE id=?",
                (request.mapping_id,),
            ).fetchone()
            if not row:
                raise LedgerError("BENCHMARK_NOT_FOUND", "Mapping not found", http_status=404)
            mapping = json.loads(row[0])
            code = mapping["instrument_code"]
            replay = c.execute(
                "SELECT payload_json FROM research_benchmark_runs WHERE request_key=?",
                (request.idempotency_key,),
            ).fetchone()
            if replay:
                result = json.loads(replay[0])
                if result["request_hash"] != digest(payload):
                    raise LedgerError("IDEMPOTENCY_CONFLICT", "Research request mismatch")
                return dict(result, idempotent_replay=True)
            ids = set(request.calendar_evidence_ids + request.distribution_evidence_ids)
            ids |= {e for s in [request.fund, *request.benchmarks] for e in s.evidence_ids}
            ids |= {e for w in request.reported_windows for e in w.evidence_ids}
            self.notebook._evidence(c, code, ids)
            if request.end > self.research._now().date() or any(
                w.end > self.research._now().date() for w in request.reported_windows
            ):
                raise LedgerError("RESEARCH_FUTURE", "Future observations cannot be reported")
            result = diagnose(mapping, request)
            result.update(
                id=str(uuid4()),
                input=payload,
                request_hash=digest(payload),
                created_at=stamp(self.research._now()),
            )
            if persist:
                c.execute(
                    "INSERT INTO research_benchmark_runs VALUES (?,?,?,?,?)",
                    (
                        result["id"],
                        mapping["portfolio_id"],
                        code,
                        request.idempotency_key,
                        json.dumps(result, ensure_ascii=False),
                    ),
                )
            return result
