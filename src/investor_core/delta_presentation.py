"""Core-only presentation of already evaluated deltas; no comparison logic here."""

from __future__ import annotations

import json
from typing import Any

from investor_core.research_summary import DRAWDOWN_LABEL

Json = dict[str, Any]
LABELS = {
    "NEW": "新增",
    "RESOLVED": "已解决",
    "CHANGED": "状态变化",
    "REGRESSED": "退化",
    "STALE": "过期",
    "UNCHANGED": "无实质变化",
}
SUBJECTS = {
    "benchmark_mapping": "研究映射",
    "comparison_coverage": "数据覆盖与比较资格",
    "relative_performance": "窗口相对表现",
    "fees": "费用披露",
    "concentration": "集中度披露",
    "thesis": "研究论点",
    "research_quality": "研究质量",
    "disclosure_conflict": "披露口径核对",
    "window_sensitivity": "窗口敏感性",
    "evidence_watch": "证据有效期",
    "mapping_candidate": "新的映射候选",
}


def display_value(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: display_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [display_value(v, key) for v in value]
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        if key.endswith("_pct"):
            return f"{value:.2f}%"
        if key == "excess_percentage_points":
            return f"{value:.2f}个百分点"
    return value


def describe(record: Json | None) -> str:
    if record is None:
        return "无对应观察,不能据此证明问题解决"
    return f"{record['category']};" + json.dumps(
        display_value(record["value"]), ensure_ascii=False, sort_keys=True
    )


def brief(record: Json | None) -> str:
    if record is None:
        return "无对应观察;不能据此证明解决"
    v = record["value"]
    if record["subject"] == "relative_performance":
        values = display_value(v)
        return (
            f"基金 {values.get('fund_return_pct', '未知')},"
            f"基准 {values.get('benchmark_return_pct', '未知')},"
            f"差额 {values.get('excess_percentage_points', '未知')},"
            f"{DRAWDOWN_LABEL}{values.get('fund_max_drawdown_pct') or '未知'}"
        )
    if record["subject"] == "benchmark_mapping":
        return f"版本 {v.get('version')},{'已批准研究' if v.get('approved') else '候选待确认'}"
    if record["subject"] == "thesis":
        return f"{v.get('status')},候选版本 {v.get('candidate_version')}"
    if record["subject"] == "comparison_coverage":
        return "、".join(v.get("gaps", [])) or "已覆盖,仍需保留证据质量限制"
    return str(record["category"]) + ";" + str(record["description"])


def present(
    result: Json, *, status: str | None = None, code: str | None = None, details: bool = False
) -> Json:
    current, summary = result["current"], result["summary"]
    names = {h["code"]: h["name"] for h in current["holdings"]}
    if code and code not in names and not any(d["holding_id"] == code for d in result["deltas"]):
        from investor_core.ledger import LedgerError

        raise LedgerError("REVIEW_HOLDING_MISSING", "该基金不在本次比较范围", http_status=404)
    date = result["baseline"]["observed_on"]
    lines = [f"本次与 {date} 首次观察基线相比(查询日 {current['observed_on']}):"]
    cutoffs = sorted({w["end"] for i in result["raw_current"]["items"] for w in i["windows"]})
    lines.append("归档研究窗口截止日: " + ("、".join(cutoffs) if cutoffs else "未知"))
    if result["evidence_acquisition"]["status"] == "NOT_PERFORMED":
        lines.append(
            "本次未获取新证据,仅比较Core当前归档研究;"
            "归档内容无变化不表示最新市场或产品情况无变化。"
        )
    material = summary["material_states"]
    lines.append(
        ";".join(
            f"{LABELS[s]}:{material[s]}"
            for s in ("NEW", "RESOLVED", "CHANGED", "REGRESSED", "STALE")
        )
    )
    lines.append(
        f"重新检查 {len(names)} 只持仓、{current['window_count']} 个研究窗口;"
        f"细节变化:{summary['materiality']['MINOR']}。"
    )
    if summary["materiality"]["MATERIAL"] == 0:
        lines.append(f"就现有归档研究,与 {date} 基线相比,没有发现需要你处理的实质变化。")
    selected = [
        d
        for d in result["deltas"]
        if (not code or d["holding_id"] == code)
        and (not status or d["status"] == status)
        and (details or d["materiality"] == "MATERIAL" or status == "UNCHANGED")
    ]
    if status == "UNCHANGED" and not details:
        changed = {d["holding_id"] for d in result["deltas"] if d["materiality"] == "MATERIAL"}
        lines.extend(
            f"{c} {n}:无实质变化"
            for c, n in names.items()
            if c not in changed and (not code or code == c)
        )
    else:
        for d in selected:
            title = SUBJECTS.get(d["subject"], d["subject"])
            lines.append(
                f"{d['holding_id']} {names.get(d['holding_id'], '')} | "
                f"{LABELS[d['status']]} | {title} {d['window']}"
            )
            if d["transition"]:
                lines.append(f"分类:{d['transition']['from']} → {d['transition']['to']}")
            if details:
                lines += [
                    "Baseline:" + describe(d["before"]),
                    "Current:" + describe(d["after"]),
                    f"Delta:{d['status']} / {d['materiality']};依据 {d['change_reason']}",
                ]
                record = d["after"] or d["before"]
                lines.append(
                    "Evidence:"
                    + ";".join(
                        f"{s.get('source_name', '')} {s.get('source_ref', '')} "
                        f"日期 {s.get('evidence_date', '未知')}"
                        for s in d["evidence"]
                    )
                )
                lines.append("Unknowns:" + ";".join(record["unknowns"]))
                lines.append(
                    f"Approval state:{record['approval_state']};"
                    f"研究数据日期:{', '.join(record['data_dates']) or '未知/见证据'}"
                )
            else:
                lines.append("此前:" + brief(d["before"]))
                lines.append("现在:" + brief(d["after"]))
                lines.append("影响:仅研究状态变化;核对对应证据和批准状态,未改变金额或投资规则。")
    lines.append("研究限制:原有单源、数据不足和口径差异保留;没有变化不代表问题已解决。")
    for kind, values in result["warnings"].items():
        if values:
            lines.append(
                {"research": "研究 WARNING", "data": "数据 WARNING", "system": "系统 WARNING"}[kind]
                + ":"
                + "、".join(values)
            )
    lines.append("本次只读,未保存观察点、处理待办或批准配置;背景事实不是风险告警。")
    return dict(
        display_text="\n".join(lines),
        selected_deltas=selected,
        unchanged_holdings=[
            c
            for c in names
            if not any(
                d["holding_id"] == c and d["materiality"] == "MATERIAL" for d in result["deltas"]
            )
        ],
    )
