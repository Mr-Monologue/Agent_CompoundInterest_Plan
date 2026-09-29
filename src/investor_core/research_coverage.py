"""Dynamic research work coverage; no investment priority, approval or storage."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from investor_core.delta_review_service import ReviewService
from investor_core.holding_review import HoldingReviewService
from investor_core.ledger import LedgerError

Json = dict[str, Any]
PRIORITIES = {
    "FINDING_REVIEW": "已有发现值得复核",
    "EVIDENCE_FIRST": "比较前必须补证",
    "CONFIG_DECISION": "等待用户配置决定",
    "MAINTENANCE": "常规资料维护",
}


def summarize(item: Json, peers: list[Json], profiles: list[Json]) -> Json:
    code = item["instrument_code"]
    reasons = item["reasons"]
    groups: dict[str, Json] = {}
    for reason in reasons:
        key = reason["kind"]
        group = groups.setdefault(
            key,
            dict(
                kind=key,
                category=reason["category"],
                text=reason["text"],
                next_step=reason["next_step"],
                records=[],
            ),
        )
        group["records"].append(reason)
    findings = [
        g
        for g in groups.values()
        if g["category"] in {"EVIDENCE_RECONCILIATION", "OBSERVED_REVIEW"}
    ]
    gaps = [g for g in groups.values() if g["category"] == "DATA_GAP"]
    configs = [g for g in groups.values() if g["category"] == "APPROVAL_GAP"]
    if not profiles:
        priority, finding, next_step = (
            "EVIDENCE_FIRST",
            "正式产品分类尚未结构化核实",
            "取得准确份额产品概要中的类型、投资范围及比较方法",
        )
    elif findings:
        priority, finding, next_step = (
            "FINDING_REVIEW",
            findings[0]["text"],
            findings[0]["next_step"],
        )
    elif gaps:
        priority, finding, next_step = "EVIDENCE_FIRST", gaps[0]["text"], gaps[0]["next_step"]
    elif configs:
        priority, finding, next_step = (
            "CONFIG_DECISION",
            configs[0]["text"],
            configs[0]["next_step"],
        )
    else:
        priority, finding, next_step = (
            "MAINTENANCE",
            "没有新增的已知复核问题",
            "按已有披露周期维护资料,不据此保证最新情况无变化",
        )
    # Same-period values stay alongside one another; no source overwrites another.
    conflicts = []
    coverage = []
    for peer in peers:
        row = next((r for r in peer["result"]["rows"] if r["code"] == code), None)
        if not row:
            continue
        coverage.append(
            dict(
                anchor_code=peer["anchor_code"],
                cohort=peer["cohort_key"],
                version=peer["version"],
                method=peer["input"].get("research_method", "MEDICAL_ACTIVE"),
                cutoff=peer["result"]["common_research_cutoff"],
                validation=peer["result"].get("validation") is not None,
                sources=peer["input"]["sources"],
                product=next(p for p in peer["input"]["products"] if p["code"] == code),
                windows=row["windows"],
            )
        )
        for w in row["windows"]:
            if not w["calculated"]:
                continue
            for d in item["windows"]:
                if (d["start"], d["end"]) == (w["start"], w["end"]) and d.get(
                    "fund_return_pct"
                ) != w["calculated"]["return_pct"]:
                    difference = abs(d["fund_return_pct"] - w["calculated"]["return_pct"])
                    precision = 0.005 if d.get("basis") == "ISSUER_REPORTED_ONLY" else 1e-10
                    conflicts.append(
                        dict(
                            start=w["start"],
                            end=w["end"],
                            d1=d,
                            d2=w,
                            d2_version=peer["version"],
                            material=difference > precision,
                            comparison_note="超过披露舍入/计算精度,待核"
                            if difference > precision
                            else "仅披露舍入或浮点精度差异",
                            interpretation="不同来源/精度或方法待核,不静默覆盖,不直接判定经济事实冲突",
                        )
                    )
    if coverage and not any(c["material"] for c in conflicts):
        best = coverage[-1]
        # Interpret all fixed windows, never select only the favorable result.
        signs: set[bool] = set()
        for peer in peers:
            if peer["cohort_key"] == best["cohort"]:
                for other in peer["result"]["rows"]:
                    if other["code"] != code and other["status"] == "COMPARABLE":
                        signs.update(
                            w["peer_difference_pp"] > 0
                            for w in other["windows"]
                            if w["peer_difference_pp"] not in (None, 0)
                        )
        priority = "FINDING_REVIEW"
        finding = (
            "同类相对领先方向随窗口改变" if len(signs) > 1 else "已完成固定窗口同类比较,范围有限"
        )
        if best["validation"]:
            finding += ";已渠道核对,上游独立性未确认"
        if best["method"] == "INDEX_FEEDER":
            next_step = "补95/5官方基准税后现金计息历史;区分目标ETF和现金配置影响"
        else:
            next_step = "补共同期间持仓变动,核验行业与个股暴露对窗口反转的解释"
        if any(g["category"] == "EVIDENCE_RECONCILIATION" for g in groups.values()):
            finding += ";原披露差异仍保留"
    elif gaps and not findings:
        coverage_gap = next((g for g in gaps if g["kind"] == "COVERAGE_GAP"), None)
        if coverage_gap:
            next_step = "取得准确份额的完整日净值、分红及适配共同日期;当前仅部分报告可读"
    dates = sorted(
        {w["end"] for w in item["windows"]} | {p["cutoff"] for p in coverage if p["cutoff"]}
    )
    if any(c["material"] for c in conflicts):
        priority, finding, next_step = (
            "FINDING_REVIEW",
            "D1与同类研究同窗口值存在差异,原口径均保留",
            "核对详情中的来源、版本、舍入精度与收益方法",
        )
    if not item["thesis"].get("active_case") and "已确认论点" in next_step:
        next_step = "先核对现有研究假设(尚未生效)与对应窗口;具体论点仍须另行确认"
    profile_types = {p["profile"]["product_type"] for p in profiles}
    if len(profile_types) > 1:
        priority, finding, next_step = (
            "EVIDENCE_FIRST",
            "正式产品分类来源不一致",
            "核对产品变更的有效日期;当前并列全部来源",
        )
    return dict(
        instrument_code=code,
        instrument_name=item["instrument_name"],
        product_profiles=profiles,
        product_type=" / ".join(sorted(profile_types)) or "未知",
        coverage=coverage,
        d1_window_count=len(item["windows"]),
        research_cutoff=dates[-1] if dates else None,
        priority=priority,
        priority_reason=PRIORITIES[priority],
        key_finding=finding,
        next_step=next_step,
        mapping_approved=item["comparison"]["mapping_approved"],
        thesis_status=item["thesis"]["status"],
        quality=item["data_quality"],
        warnings=item["warnings"],
        groups=list(groups.values()),
        source_differences=conflicts,
        scope=item["comparison"],
        context_is_risk=False,
        ranking=False,
    )


class CoverageService:
    def __init__(self, reviews: HoldingReviewService) -> None:
        self.reviews = reviews

    def read(
        self, portfolio: str, account: str, *, code: str | None = None, details: bool = False
    ) -> Json:
        current = self.reviews.build(portfolio, account)
        history = self.reviews.history(portfolio, account)
        delta = (
            ReviewService.compare(history["snapshots"][0], current)
            if history["snapshots"]
            else None
        )
        with self.reviews.research._connect() as c:
            records = c.execute("SELECT * FROM peer_research_runs ORDER BY version").fetchall()
        latest = {}
        for r in records:
            latest[(r["anchor_code"], r["cohort_key"])] = dict(
                anchor_code=r["anchor_code"],
                cohort_key=r["cohort_key"],
                version=r["version"],
                input=json.loads(r["input_json"]),
                result=json.loads(r["result_json"]),
            )
        rows = []
        for item in current["items"]:
            profiles = []
            evidence = self.reviews.research.list_evidence(
                instrument_code=item["instrument_code"], limit=1000
            )
            for e in evidence:
                f = e.get("facts", {})
                profile = f.get("facts", {}).get("product_research_profile")
                if (
                    isinstance(profile, dict)
                    and f.get("quality") in {"OFFICIAL", "REPOST"}
                    and all(
                        isinstance(profile.get(k), str) and profile.get(k)
                        for k in ("product_type", "investment_scope", "comparison_method", "quote")
                    )
                    and profile["quote"] in f.get("excerpt", "")
                ):
                    profiles.append(
                        dict(
                            profile=profile,
                            evidence_id=e["id"],
                            source_ref=e["source_ref"],
                            data_date=f["data_date"],
                            published_date=f.get("published_date"),
                            quality=f.get("quality"),
                        )
                    )
            # Never discard contradictory observations; all dated profiles remain readable.
            rows.append(summarize(item, list(latest.values()), profiles))
        if code and code not in {r["instrument_code"] for r in rows}:
            raise LedgerError(
                "COVERAGE_HOLDING_MISSING", "该基金不在当前有效持仓中", http_status=404
            )
        result = dict(
            query_date=current["observed_on"],
            holding_count=len(rows),
            items=rows,
            priority_counts=dict(Counter(r["priority"] for r in rows)),
            baseline_delta=delta,
            history_snapshot_count=len(history["snapshots"]),
            historical_classification_count=len(history["tasks"]),
            evidence_acquisition="NOT_PERFORMED_ON_QUERY",
            writes_performed=False,
            approval_mutation=False,
            holding_mutation=False,
            money_action=False,
        )
        result["display_text"] = present(result, code=code, details=details)
        return result


def present(result: Json, *, code: str | None, details: bool) -> str:
    rows = [r for r in result["items"] if not code or r["instrument_code"] == code]
    lines = [
        f"组合研究覆盖 | 查询日 {result['query_date']} | 当前{result['holding_count']}只持仓",
        "本次未获取新资料,复用归档;各行研究截止不同,归档未变不代表最新情况未变。",
        "研究事项分类用于决定下一步取证,不是基金优劣排名或交易紧迫性。",
        "",
        "|持仓|正式类型/依据|覆盖|研究截止/质量|主要发现|下一步|",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        peer = (
            "、".join(
                f"{'指数联接' if p['method'] == 'INDEX_FEEDER' else '医疗主动'} v{p['version']}"
                + ("已渠道核验" if p["validation"] else "未交叉核验")
                for p in r["coverage"]
            )
            or "尚无同类比较"
        )
        basis = (
            "、".join(sorted({str(p["data_date"]) for p in r["product_profiles"]})) or "待正式资料"
        )
        cells = [
            f"{r['instrument_code']} {r['instrument_name']}",
            f"{r['product_type']}({basis};"
            + (
                "转载待原站核验"
                if any(p.get("quality") == "REPOST" for p in r["product_profiles"])
                else "正式披露"
            )
            + ")",
            f"D1 {r['d1_window_count']}窗口;{peer};"
            + ("映射已批准研究" if r["mapping_approved"] else "映射候选")
            + ("/论点已生效" if r["thesis_status"] == "ACTIVE_RESEARCH" else "/论点未生效"),
            f"{r['research_cutoff'] or '未知'} / {r['quality']}",
            f"{r['priority_reason']}:{r['key_finding']}",
            r["next_step"],
        ]
        lines.append(
            "|" + "|".join(str(c).replace("|", "/").replace("\n", " ") for c in cells) + "|"
        )
    lines += [
        "",
        "质量限制:官方单源、历史重算差异和未批准映射均保留;"
        "渠道一致不等于独立来源。历史买入理由未知处仍未知。",
        "研究分类与投资角色、批准映射分别保存;背景观察不计为风险告警。本查询不处理原分类记录。",
    ]
    lines += ["", "优先研究事项(按工作类型,不作基金排名):"]
    for key in ("FINDING_REVIEW", "EVIDENCE_FIRST", "CONFIG_DECISION", "MAINTENANCE"):
        members = [r["instrument_code"] for r in rows if r["priority"] == key]
        if members:
            lines.append(PRIORITIES[key] + ": " + "、".join(members))
    if details:
        for r in rows:
            lines += [
                f"详情 {r['instrument_code']}:"
                f"映射{'已批准研究' if r['mapping_approved'] else '候选待批准'};"
                f"论点 {r['thesis_status']}",
                "正式资料:" + json.dumps(r["product_profiles"], ensure_ascii=False),
                "各方法/来源/窗口:" + json.dumps(r["coverage"], ensure_ascii=False, default=str),
                "合并问题及全部底层证据:"
                + json.dumps(r["groups"], ensure_ascii=False, default=str),
                "来源值差异:"
                + json.dumps(r["source_differences"], ensure_ascii=False, default=str),
            ]
    return "\n".join(lines)
