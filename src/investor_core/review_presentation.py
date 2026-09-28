"""Core-owned presentation only; no change to calculation, evidence or capture inputs."""

from __future__ import annotations

from collections import Counter
from typing import Any

from investor_core.holding_review import CATEGORY_LABELS, THESIS_LABELS, semantic

Json = dict[str, Any]
REASON_LABELS = {
    "RELATIVE_LAG": "部分窗口落后",
    "WINDOW_SENSITIVITY": "领先/落后随窗口变化",
    "DISCLOSURE_CONFLICT": "重算与披露差异待核",
    "THESIS_REVIEW": "已确认论点需复核",
    "MAPPING_PENDING": "映射待批准",
    "COVERAGE_GAP": "比较数据缺口",
    "FEES": "有费用披露",
    "CONCENTRATION": "有集中度披露",
    "FEES_UNKNOWN": "费用证据不足",
    "CONCENTRATION_UNKNOWN": "集中度证据不足",
}
QUALITY_LABELS = {
    "SINGLE_SOURCE_WARNING": "官方单源",
    "DISCLOSED_RETURN_MISMATCH": "披露差异未解释",
    "ISSUER_COMPOSITE_NOT_COMPONENT_RECONSTRUCTION": "管理人公布路径,非独立重建",
    "CORRELATION_INSUFFICIENT_OR_CONSTANT": "相关性证据不足",
}


def window_label(w: Json) -> str:
    return f"{w['start']}—{w['end']}"


def disclosure_sections(item: Json) -> Json:
    """Keep original claims; separate execution text from mandate/type evidence."""
    mandate, restrictions = [], []
    for claim in item["comparison"]["disclosed_mandate"]:
        text = claim["text"]
        # Conservative: any purchase/suspension limit wording cannot establish type.
        constrained = any(
            x in text for x in ("限购", "限额", "不超过", "暂停申购", "累计申购", "受理截止")
        )
        (restrictions if constrained else mandate).append(claim)
    excluded = {e for c in restrictions for e in c["evidence_ids"]}
    labels = [p for p in item["comparison"]["product_labels"] if p["source"]["id"] not in excluded]
    return dict(
        product_types=labels,
        investment_scope=mandate,
        purchase_disclosures=restrictions,
        account_applicability="NOT_VERIFIED",
        purchase_schedule_effect=False,
    )


def group_reasons(item: Json) -> list[Json]:
    groups: dict[str, Json] = {}
    for reason in item["reasons"]:
        kind = reason["kind"]
        group = groups.setdefault(
            kind,
            dict(
                kind=kind,
                category=reason["category"],
                label=REASON_LABELS.get(kind, reason["text"]),
                reason_keys=[],
                evidence_versions=[],
                windows=[],
                next_steps=[],
                texts=[],
            ),
        )
        group["reason_keys"].append(reason["reason_key"])
        group["evidence_versions"].append(reason["evidence_version"])
        basis = reason["basis"]
        windows = [basis["window"]] if "window" in basis else basis.get("windows", [])
        for window in windows:
            label = window_label(window)
            if label not in group["windows"]:
                group["windows"].append(label)
        for key, value in (("next_steps", reason["next_step"]), ("texts", reason["text"])):
            if value not in group[key]:
                group[key].append(value)
    return list(groups.values())


def present_review(result: Json) -> Json:
    rows, details, presentations = [], [], []
    heading = (
        f"持仓复核|查询日期:{result['observed_on']};研究数据日期逐项列示。"
        "仅各自基准比较,不是统一排名或买卖信号。"
    )
    for item in result["items"]:
        groups = group_reasons(item)
        sections = disclosure_sections(item)
        dates = sorted({w["end"] for w in item["windows"]})
        cutoff = dates[-1] if dates else None
        findings = [
            g["label"]
            for g in groups
            if g["category"] in {"OBSERVED_REVIEW", "EVIDENCE_RECONCILIATION"}
        ]
        if not findings:
            findings = ["仅配置/证据待补;未观察到负差额" if item["windows"] else "无可比窗口"]
        mapping = "映射已批准" if item["comparison"]["mapping_approved"] else "映射候选"
        thesis = THESIS_LABELS[item["thesis"]["status"]]
        active = item["thesis"]["active_case"]
        if active:
            thesis += f" v{active['version']}"
        quality = [label for key, label in QUALITY_LABELS.items() if key in item["warnings"]]
        if item["comparison"]["gaps"]:
            quality.append("覆盖不足")
        unknown = [
            g["label"] for g in groups if g["kind"] in {"FEES_UNKNOWN", "CONCENTRATION_UNKNOWN"}
        ]
        quality.extend(unknown)
        if not sections["product_types"]:
            quality.append("类型未核实")
        change = {
            "NO_BASELINE": "尚无基线",
            "CHANGED": "较基线有变化",
            "UNCHANGED": "较基线无变化",
        }[item["changes"]["status"]]
        next_step = groups[0]["next_steps"][0] if groups else "保留观察,暂无新增原因"
        line = (
            f"{item['instrument_code']} {item['instrument_name']}|{';'.join(findings)}"
            f"|研究截至 {cutoff or '未知'}|{thesis}/{mapping}|{change}"
            f"|WARNING:{'、'.join(quality) or '研究口径有限'}|下一步:{next_step}"
        )
        rows.append(line)
        presentations.append(
            dict(
                instrument_code=item["instrument_code"],
                research_data_through=cutoff,
                window_end_dates=dates,
                groups=groups,
                disclosures=sections,
                quality_limits=quality,
                summary_line=line,
            )
        )
        details.extend(
            [
                line,
                "产品类型:" + ("、".join(p["label"] for p in sections["product_types"]) or "未知"),
                "投资范围:" + (";".join(c["text"] for c in sections["investment_scope"]) or "未知"),
                "购买/渠道限制披露:"
                + (";".join(c["text"] for c in sections["purchase_disclosures"]) or "本研究未提供"),
                "限制披露不等于当前账户适用性或未来可执行额度。",
            ]
        )
        for group in groups:
            details.append(
                f"[{CATEGORY_LABELS[group['category']]}] {group['label']}"
                f"(保留{len(group['reason_keys'])}条底层原因)"
                + ";观察窗口:"
                + ("、".join(group["windows"]) or "非单窗口原因,见底层依据")
            )
            details.extend("  " + t for t in group["texts"])
            details.append("  下一步:" + ";".join(group["next_steps"]))
        details.append("全部观察窗口(研究截止日不等于查询日):")
        for w in item["windows"]:
            dd = w["fund_max_drawdown_pct"]
            dd_text = "缺失" if dd is None else f"{dd:.2f}%"
            basis = (
                "仅管理人披露"
                if w["basis"] == "ISSUER_REPORTED_ONLY"
                else "日序列/管理人公布路径,非成分独立重建"
                if w["benchmark_path"] == "ISSUER_PUBLISHED"
                else "日序列/成分计算,质量限制保留"
            )
            details.append(
                f"  {window_label(w)}:基金 {w['fund_return_pct']:.2f}%,"
                f"基准 {w['benchmark_return_pct']:.2f}%;"
                f"差额 {w['excess_percentage_points']:+.2f} 个百分点;"
                f"{item['drawdown_label']} {dd_text};{basis}"
            )
        details.append("已确认论点:" + (active["thesis"]["proposed_why_hold"] if active else "无"))
        details.append("历史买入理由:未知;当前研究假设不补写历史。")
        details.extend("反证:" + c["text"] for c in item["thesis"]["counter_evidence"])
        details.append("全部质量限制:" + ";".join(item["comparison"]["gaps"] + item["warnings"]))
        sources = {s["id"]: s for r in item["reasons"] for s in r["sources"]}
        details.append("来源汇总(底层原因保留对应关系):")
        details.extend(
            f"  {s['source_name']}|数据/证据日期 {s['evidence_date']}|{s['source_ref']}"
            for s in sources.values()
        )
        details.append("")
    summary = "\n".join(
        [
            heading,
            *rows,
            "查看详情可见合并原因所含窗口、完整收益/回撤、来源与口径。查询不保存快照或待办。",
        ]
    )
    return dict(
        presentation_version="holding-review-display-v2",
        presentation=presentations,
        display_text=summary,
        detail_text="\n".join([heading, *details]),
    )


def baseline_presentation(current: Json, history: Json) -> Json:
    captures = [e for e in history["events"] if e["kind"] == "CAPTURE"]
    previous = next(
        (s for s in history["snapshots"] if captures and s["id"] == captures[-1]["snapshot_id"]),
        None,
    )
    known = {t["reason"]["evidence_version"] for t in history["tasks"]}
    reasons = [r for i in current["items"] for r in i["reasons"]]
    new = {r["evidence_version"]: r for r in reasons if r["evidence_version"] not in known}
    counts = dict(Counter(r["category"] for r in new.values()))
    old = {i["instrument_code"]: i for i in previous["snapshot"]["items"]} if previous else {}
    changes = [
        dict(
            instrument_code=i["instrument_code"],
            changed=semantic(i) != semantic(old.get(i["instrument_code"])),
        )
        for i in current["items"]
    ]
    existing = previous is not None
    text = [
        f"观察基线只读预览|查询日期:{current['observed_on']}",
        "已有基线:复用保存记录,仅展示当前变化,不重复创建。"
        if existing
        else "尚无基线:拟保存一份当前持仓范围内的研究观察快照(实际数量见下)。",
        f"本次{'对照' if existing else '拟保存'} {len(current['items'])}只持仓;"
        "包含原始窗口计算、证据版本、批准状态、论点、反证与质量限制。",
        "保存时间只在实际提交时记录,不追溯为研究数据日期。",
    ]
    for i in current["items"]:
        dates = sorted({w["end"] for w in i["windows"]})
        text.append(
            f"{i['instrument_code']} {i['instrument_name']}:{len(i['windows'])}个窗口;"
            f"研究截至 {dates[-1] if dates else '未知'};"
            f"{len(i['reasons'])}条底层原因;"
            + ("映射已批准" if i["comparison"]["mapping_approved"] else "映射候选")
            + ";"
            + THESIS_LABELS[i["thesis"]["status"]]
        )
    text.append(
        ("当前新增原因候选(本次不创建):" if existing else "若确认保存,将创建未处理待办:")
        + str(len(new))
        + "条;"
        + "、".join(f"{CATEGORY_LABELS[k]} {v}" for k, v in counts.items())
    )
    if existing:
        text.append(
            "相较已存基线变化:"
            + ("、".join(c["instrument_code"] for c in changes if c["changed"]) or "无")
        )
    text.append(
        "同类原因仅展示合并,底层各窗口证据待办不删除。保存观察不等于问题已处理,不批准映射或论点,不改变金额、策略、账本,不提交周报。本次未保存任何记录。"
    )
    return dict(
        action="REUSE_EXISTING" if existing else "PROPOSE_FIRST_BASELINE",
        observed_on=current["observed_on"],
        existing_snapshot=previous,
        proposed_snapshot=None if existing else current,
        current_observation=current,
        changes=changes if existing else [],
        proposed_task_count=len(new) if not existing else 0,
        new_reason_count=len(new),
        category_counts=counts,
        proposed_tasks=list(new.values()) if not existing else [],
        existing_task_count=len(history["tasks"]),
        writes_performed=0,
        expected_input_hash=current["input_hash"],
        money_action=False,
        display_text="\n".join(text),
    )
