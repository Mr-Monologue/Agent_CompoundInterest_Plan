"""Deterministic peer research. No investment registry, approval or D1 writes."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from itertools import pairwise
from statistics import correlation
from typing import Any
from uuid import uuid4

from investor_core.benchmarks import net_nav_period_return
from investor_core.execution import TZ
from investor_core.ledger import LedgerError
from investor_core.peer_models import PeerProduct, PeerStudy, PeerWindow
from investor_core.research import ResearchService
from investor_core.research_summary import DRAWDOWN_LABEL
from investor_core.scheduler import digest, instant, stamp

Json = dict[str, Any]
COMPUTATION_VERSION = "peer-common-calendar-v1"


def admission(p: PeerProduct, study: PeerStudy) -> tuple[str, str]:
    if p.active is False or p.product_type in {"INDEX", "OTHER"} or p.market == "QDII":
        return "EXCLUDED", "非本轮境内主动医疗同类;指数/QDII不可混入"
    if (
        p.active is None
        or p.market == "UNKNOWN"
        or p.product_type == "UNKNOWN"
        or p.share_class == "UNKNOWN"
        or not p.admission_sources
        or p.stock_min_pct is None
        or p.stock_max_pct is None
        or p.medical_non_cash_min_pct is None
        or p.share_inception is None
    ):
        return "UNVERIFIED", "正式产品范围、份额或仓位证据未齐,不推定合格"
    if p.stock_min_pct < study.stock_floor_pct or (
        p.medical_non_cash_min_pct < study.medical_floor_pct
    ):
        return "EXCLUDED", "不满足预先规定的股票/医疗主题研究范围"
    if p.market == "CN_A_H":
        return "CONTEXT_ONLY", "可投港股,市场暴露不同;单列范围参照,不混入共同收益主表"
    if p.currency != "CNY":
        return "EXCLUDED", "不同币种且缺可验证汇率转换,不直接比较"
    return "COMPARABLE", "主动境内医疗主题;股票仓位上下限及份额费用差异仍须考虑"


def path_for(p: PeerProduct, window: PeerWindow, study: PeerStudy) -> Json:
    days = [d for d in study.calendar_dates if window.start <= d <= window.end]
    gaps: list[str] = []
    if not days or days[0] != window.start or days[-1] != window.end:
        gaps.append("EXACT_COMMON_ENDPOINT_MISSING")
    if not p.nav:
        return dict(calculated=None, gaps=["OFFICIAL_NAV_SERIES_MISSING"], missing_dates=[])
    if p.nav.return_basis not in {"NAV_NET_INTERNAL_FEES", "FUND_TOTAL_RETURN"}:
        gaps.append("RETURN_BASIS_UNKNOWN")
    if p.nav.validation == "CONFLICT":
        gaps.append("SOURCE_CONFLICT")
    if p.share_inception is None or p.share_inception > window.start:
        gaps.append("EXACT_SHARE_HISTORY_INCOMPLETE")
    if p.nav.return_basis == "NAV_NET_INTERNAL_FEES" and (
        not p.distribution_sources
        or p.distribution_from is None
        or p.distribution_to is None
        or p.distribution_from > window.start
        or p.distribution_to < window.end
    ):
        gaps.append("DIVIDEND_COVERAGE_INCOMPLETE")
    points = {v.day: v.value for v in p.nav.points}
    missing = [d.isoformat() for d in days if d not in points]
    if missing:
        gaps.append("DAILY_NAV_MISSING_NO_FILL")
    dividends = {d.ex_date: d.cash_per_unit for d in p.distributions}
    if any(window.start < d <= window.end and d not in days for d in dividends):
        gaps.append("DIVIDEND_DATE_NOT_IN_CALENDAR")
    if gaps:
        return dict(calculated=None, gaps=gaps, missing_dates=missing)
    wealth = peak = Decimal(1)
    drawdown = Decimal(0)
    returns = []
    for previous, day in pairwise(days):
        r = net_nav_period_return(points[previous], points[day], dividends.get(day, Decimal(0)))
        returns.append(float(r))
        wealth *= 1 + r
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1)
    return dict(
        calculated=dict(
            return_pct=float((wealth - 1) * 100),
            max_drawdown_pct=float(drawdown * 100),
            observations=len(returns),
        ),
        daily_returns=returns,
        gaps=[],
        missing_dates=[],
    )


def evaluate(study: PeerStudy) -> Json:
    anchor = next(p for p in study.products if p.code == study.anchor_code)
    if admission(anchor, study)[0] != "COMPARABLE":
        raise LedgerError("PEER_REFERENCE_NOT_COMPARABLE", "参照产品范围尚未核实")
    reference = {w.label: path_for(anchor, w, study) for w in study.windows}
    rows: list[Json] = []
    for p in study.products:
        status, reason = admission(p, study)
        windows: list[Json] = []
        for w in study.windows:
            own: Json = (
                path_for(p, w, study)
                if status == "COMPARABLE"
                else dict(calculated=None, gaps=["OUTSIDE_DIRECT_COMPARISON"], missing_dates=[])
            )
            ref = reference[w.label]
            c, rc = own["calculated"], ref["calculated"]
            difference = c["return_pct"] - rc["return_pct"] if c and rc else None
            corr = None
            if (
                c
                and rc
                and c["observations"] >= 20
                and (len(set(own["daily_returns"])) > 1 and len(set(ref["daily_returns"])) > 1)
            ):
                corr = correlation(own["daily_returns"], ref["daily_returns"])
            reported = [
                r.model_dump(mode="json")
                for r in p.reported
                if r.start == w.start and r.end == w.end
            ]
            disclosed_difference = None
            ar = [r for r in anchor.reported if r.start == w.start and r.end == w.end]
            if status == "COMPARABLE" and reported and ar:
                disclosed_difference = float(Decimal(reported[0]["return_pct"]) - ar[0].return_pct)
            warnings = [
                "OFFICIAL_SINGLE_SOURCE",
                "NAV_INCLUDES_ONGOING_FEES",
                "ENTRY_EXIT_FEES_AND_CHANNEL_DISCOUNTS_NOT_INCLUDED",
                "NO_ALPHA_ATTRIBUTION",
            ]
            if c and any(abs(c["return_pct"] - float(r["return_pct"])) > 0.005 for r in reported):
                warnings.append("DISCLOSED_RETURN_MISMATCH")
            windows.append(
                dict(
                    label=w.label,
                    start=w.start.isoformat(),
                    end=w.end.isoformat(),
                    calculated=c,
                    peer_difference_pp=difference,
                    daily_correlation=corr,
                    gaps=own["gaps"],
                    reference_gaps=ref["gaps"],
                    missing_dates=own["missing_dates"],
                    reported=reported,
                    reported_difference_pp=disclosed_difference,
                    reported_basis="ISSUER_DISCLOSURE_NOT_DAILY_RECONSTRUCTION",
                    warnings=warnings,
                    evidence_ids=sorted(
                        set(
                            p.admission_sources
                            + p.distribution_sources
                            + study.calendar_sources
                            + (p.nav.evidence_ids if p.nav else [])
                            + (anchor.nav.evidence_ids if anchor.nav else [])
                            + anchor.distribution_sources
                        )
                    ),
                )
            )
        rows.append(
            dict(
                code=p.code,
                name=p.name,
                product_key=p.product_key,
                share_class=p.share_class,
                status="REFERENCE" if p.code == study.anchor_code else status,
                admission_reason=reason,
                admission_note=p.admission_note,
                market=p.market,
                stock_min_pct=p.stock_min_pct,
                stock_max_pct=p.stock_max_pct,
                other_shares=p.other_shares,
                dimensions={
                    k: [c.model_dump(mode="json") for c in v] for k, v in p.dimensions.items()
                },
                gaps=p.attempts_and_gaps,
                windows=windows,
            )
        )
    shared = [
        w.end.isoformat()
        for w in study.windows
        if reference[w.label]["calculated"]
        and any(
            p["code"] != study.anchor_code
            and any(
                x["label"] == w.label and x["peer_difference_pp"] is not None for x in p["windows"]
            )
            for p in rows
        )
    ]
    return dict(
        anchor_code=study.anchor_code,
        cohort_key=study.cohort_key,
        computation_version=COMPUTATION_VERSION,
        scope_version=study.scope_version,
        scope_defined_at=study.scope_defined_at.isoformat(),
        scope_rationale=study.scope_rationale,
        knowledge_date=study.knowledge_date.isoformat(),
        common_research_cutoff=max(shared) if shared else None,
        rows=rows,
        limitations=study.limitations,
        drawdown_label=DRAWDOWN_LABEL,
        retrospective_only=True,
        money_action=False,
        approval_mutation=False,
        holding_mutation=False,
    )


class PeerResearchService:
    def __init__(self, research: ResearchService) -> None:
        self.research = research

    def archive(self, study: PeerStudy) -> Json:
        if study.knowledge_date > self.research._now().astimezone(TZ).date():
            raise LedgerError("PEER_FUTURE_KNOWLEDGE", "不能归档未来研究日期")
        payload = study.model_dump(mode="json")
        content = {
            k: v
            for k, v in payload.items()
            if k not in {"idempotency_key", "expected_previous_version"}
        }
        content["sources"] = {
            k: {f: v for f, v in s.items() if f != "retrieved_at"}
            for k, s in content["sources"].items()
        }
        fingerprint = digest(content)
        result = evaluate(study)
        with self.research._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            records = c.execute(
                "SELECT * FROM peer_research_runs WHERE anchor_code=? AND cohort_key=? "
                "ORDER BY version",
                (study.anchor_code, study.cohort_key),
            ).fetchall()
            for row in records:
                if (
                    row["idempotency_key"] == study.idempotency_key
                    and row["content_hash"] != fingerprint
                ):
                    raise LedgerError("PEER_IDEMPOTENCY_CONFLICT", "同一请求内容已改变,请先回读")
                if row["content_hash"] == fingerprint:
                    return dict(
                        id=row["id"], version=row["version"], reused=True, approval_mutation=False
                    )
            previous = records[-1]["version"] if records else 0
            if previous != study.expected_previous_version:
                raise LedgerError("PEER_VERSION_CHANGED", "研究版本已改变,请先回读")
            key = str(uuid4())
            c.execute(
                "INSERT INTO peer_research_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    study.anchor_code,
                    study.cohort_key,
                    previous + 1,
                    study.idempotency_key,
                    fingerprint,
                    stamp(self.research._now()),
                    study.knowledge_date.isoformat(),
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False, default=str),
                ),
            )
        return dict(id=key, version=previous + 1, reused=False, approval_mutation=False)

    def read(
        self,
        anchor_code: str,
        *,
        cohort_key: str | None = None,
        run_id: str | None = None,
        as_of: date | None = None,
        code: str | None = None,
        details: bool = False,
    ) -> Json:
        with self.research._connect() as c:
            rows = c.execute(
                "SELECT * FROM peer_research_runs WHERE anchor_code=? ORDER BY created_at,id",
                (anchor_code,),
            ).fetchall()
        rows = [
            r
            for r in rows
            if (not cohort_key or r["cohort_key"] == cohort_key)
            and (not run_id or r["id"] == run_id)
            and (
                not as_of
                or (
                    r["knowledge_date"] <= as_of.isoformat()
                    and instant(r["created_at"]).astimezone(TZ).date() <= as_of
                )
            )
        ]
        if not rows:
            raise LedgerError("PEER_RESEARCH_MISSING", "尚无该时点可用的同类研究", http_status=404)
        if not cohort_key and len({r["cohort_key"] for r in rows}) > 1:
            raise LedgerError(
                "PEER_COHORT_AMBIGUOUS", "存在多个研究范围,请明确选择", http_status=409
            )
        rows.sort(key=lambda record: record["version"])
        row = rows[-1]
        result: Json = json.loads(row["result_json"])
        study = json.loads(row["input_json"])
        if code and code not in {p["code"] for p in result["rows"]}:
            raise LedgerError("PEER_PRODUCT_MISSING", "不在当前研究范围", http_status=404)
        result.update(
            run_id=row["id"],
            version=row["version"],
            query_date=self.research._now().astimezone(TZ).date().isoformat(),
            archived_at=row["created_at"],
            evidence_acquisition="NOT_PERFORMED_ON_QUERY",
            writes_performed=False,
        )
        from investor_core.peer_presentation import present

        result["display_text"] = present(result, study, code=code, details=details)
        if details:
            result["archived_input"] = study
        result["history"] = [
            dict(
                id=r["id"],
                version=r["version"],
                knowledge_date=r["knowledge_date"],
                archived_at=r["created_at"],
            )
            for r in rows
        ]
        return result
