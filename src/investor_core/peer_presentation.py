"""Core presentation of peer research, without ranking or allocation."""

from __future__ import annotations

from typing import Any

from investor_core.execution import TZ
from investor_core.scheduler import instant

Json = dict[str, Any]


def present(result: Json, study: Json, *, code: str | None, details: bool) -> str:
    rows = [r for r in result["rows"] if not code or r["code"] == code]
    labels = [w["label"] for w in study["windows"]]
    archive_day = (
        instant(result["archived_at"]).astimezone(TZ).date().isoformat()
        if result.get("archived_at")
        else "尚未保存"
    )
    lines = [
        f"医疗主题同类比较 | 查询日 {result['query_date']} | 研究版本 {result['version']}",
        f"共同日序列研究截止日: {result['common_research_cutoff'] or '尚无可计算共同窗口'};"
        f"资料可用截止日: {result['knowledge_date']}",
        f"实际归档日: {archive_day} (上海时区)",
        "本次查询未获取新证据,复用已归档公开资料。归档新不表示净值/持仓数据同样新。",
        "小规模研究样本,非全市场筛选;无总分、排名、换仓信号或新增资金资格。",
        "同类条件:主动、境内医疗主题;股票型/混合型仓位约束不同。港股通和未知范围单列。",
        f"下表为日序列重算收益 / {result['drawdown_label']},均为百分比。",
        "",
        "|产品/份额|比较资格|" + "|".join(labels) + "|",
        "|---|---|" + "|".join("---" for _ in labels) + "|",
    ]
    for row in rows:
        cells = []
        for w in row["windows"]:
            c = w["calculated"]
            if c:
                cells.append(f"{c['return_pct']:.2f}% / {c['max_drawdown_pct']:.2f}%")
            elif w["reported"] and row["status"] in {"REFERENCE", "COMPARABLE"}:
                cells.append(f"披露{float(w['reported'][0]['return_pct']):.2f}%;回撤未知")
            else:
                cells.append("未计算")
        status_text = {
            "REFERENCE": "参照",
            "COMPARABLE": "可比较",
            "CONTEXT_ONLY": "范围参照",
            "UNVERIFIED": "范围待核",
            "EXCLUDED": "排除",
        }[row["status"]]
        lines.append(f"|{row['code']} {row['name']}|{status_text}|" + "|".join(cells) + "|")
    lines.append("")
    for row in rows:
        if row["status"] not in {"REFERENCE", "COMPARABLE"}:
            lines.append(f"{row['code']}: {row['admission_reason']};{row['admission_note']}")
    for row in rows:
        if row["code"] != result["anchor_code"] and row["status"] == "COMPARABLE":
            pairs = [w for w in row["windows"] if w["peer_difference_pp"] is not None]
            if pairs:
                lines.append(
                    row["code"]
                    + " 相对参照收益差额: "
                    + ";".join(
                        f"{w['label']} {w['peer_difference_pp']:+.2f}个百分点" for w in pairs
                    )
                )
                signs = {w["peer_difference_pp"] > 0 for w in pairs if w["peer_difference_pp"] != 0}
                lines.append(
                    "相对领先方向随窗口改变。"
                    if len(signs) > 1
                    else "已覆盖窗口方向一致也不代表其他窗口或未来持续占优。"
                )
        if row["status"] in {"REFERENCE", "COMPARABLE"}:
            for dimension, claims in row["dimensions"].items():
                title = {
                    "fees": "费用",
                    "concentration": "集中度",
                    "management": "经理",
                    "method": "方法",
                }[dimension]
                lines.append(
                    f"{row['code']} {title}: "
                    + ";".join(f"{c['text']}(资料截至{c['as_of'] or '未知'})" for c in claims)
                )
            if row["gaps"]:
                lines.append("待补证:" + ";".join(row["gaps"]))
        if row["other_shares"]:
            lines.append(
                row["code"]
                + " 同产品其他份额 "
                + ",".join(row["other_shares"])
                + " 不重复计为候选,不拼接净值。"
            )
    lines += [
        "解释:共同涨跌/相关性仅为观察;仓位、行业/个股暴露、经理任期和费用都可能影响差额,"
        "未做持仓贡献或风险调整归因,不能证明基金选择能力。",
        "质量 WARNING:官方单源;持续管理/托管/销售服务费用已在净值中,不再次扣减;"
        "个人申赎费、持有期限及渠道折扣未知。",
        "下一步:优先补相同季度的细分行业及持仓贡献、独立净值校验,核对共同管理任期;"
        "未知候选补正式产品范围和完整日序列,不降低同类标准。",
    ]
    if any("DISCLOSED_RETURN_MISMATCH" in w["warnings"] for r in rows for w in r["windows"]):
        lines.append("WARNING:部分同区间报告收益与重算超出披露舍入范围,详情保留差额,未抹平。")
    lines += study["limitations"]
    if result.get("validation"):
        lines.append("结论验证已归档:跨发布渠道逐日核对及同期结构;上游未证实独立,单源警告保留。")
        v = result["validation"]
        complete = sum(
            all(w["status"] == "EXACT_CHANNEL_AGREEMENT" for w in c["windows"]) for c in v["checks"]
        )
        lines.append(
            f"验证摘要:{complete}/{len(v['checks'])}份额全部归档窗口跨渠道一致;"
            f"同期结构截至{v['structural_date']},前十大共有{v['shared_top10_count']}只。"
        )
        lines.append("使用「验证比较结论」查看原窗口支持情况、新窗口方向及缺口。")
    if details:
        for row in rows:
            lines.append(
                f"详情 {row['code']}: {row['admission_note']};股票范围 "
                f"{row['stock_min_pct']}%-{row['stock_max_pct']}%;市场 {row['market']}"
            )
            for w in row["windows"]:
                lines.append(
                    f"{w['label']}: {w['start']} → {w['end']};日收益相关性 "
                    f"{w['daily_correlation']};缺口 {w['gaps']};"
                    f"参照缺口 {w['reference_gaps']};缺日 {w['missing_dates']};"
                    f"口径 {w['reported_basis']}(仅针对披露值)"
                )
                for r in w["reported"]:
                    lines.append(f"管理人披露 {r['return_pct']}%;与日重算分别保留。")
            for claims in row["dimensions"].values():
                for c in claims:
                    lines.append(
                        f"{c['kind']}: {c['limitation']};证据 {','.join(c['evidence_ids'])}"
                    )
        for key, source in study["sources"].items():
            lines.append(
                f"来源 {key}: {source['source_name']} | {source['source_ref']} | "
                f"发布 {source['published_date'] or '未知'} | 数据 {source['data_date']} | "
                f"获取 {source['retrieved_at']} | 指纹 {source['original_sha256']}"
            )
    lines.append("仅当前已知资料的事后研究,不是历史选基回放;不改变D1基线/待办、批准或投资金额。")
    return "\n".join(lines)
