"""Read-only research review, with no allocation or risk-action authority."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Any

Json = dict[str, Any]
DRAWDOWN_LABEL = "基金最大回撤（共同交易日）"  # noqa: RUF001 -- User-specified column title.


def summarize_fund(record: Json, *, today: date) -> Json:
    versions = record["versions"]
    approved = [m for m in versions if m["status"] == "APPROVED_RESEARCH"]
    mapping = (approved or versions or [None])[-1]
    windows: dict[tuple[str, str], Json] = {}
    gaps: set[str] = set()
    warnings: set[str] = set()
    missing: dict[str, set[str]] = {}
    if mapping:
        for run in record["runs"]:
            if run["mapping_id"] != mapping["id"]:
                continue
            gaps.update(run["gaps"])
            warnings.update(run["warnings"])
            for name, days in run.get("missing_observations", {}).items():
                missing.setdefault(name, set()).update(days)
            calculated = run.get("calculated")
            if calculated is not None:
                candidates = [dict(calculated, start=run["start"], end=run["end"])]
                basis = "DAILY_SERIES"
            else:
                candidates = run.get("reported_windows", [])
                basis = "ISSUER_REPORTED_ONLY"
            for value in candidates:
                key = (value["start"], value["end"])
                item = {
                    k: value.get(k)
                    for k in (
                        "start",
                        "end",
                        "fund_return_pct",
                        "benchmark_return_pct",
                        "excess_percentage_points",
                        "fund_max_drawdown_pct",
                        "daily_correlation",
                    )
                }
                # Persisted Decimal values may be JSON strings, unlike live calculations.
                for field in (
                    "fund_return_pct",
                    "benchmark_return_pct",
                    "excess_percentage_points",
                    "fund_max_drawdown_pct",
                    "daily_correlation",
                ):
                    if item[field] is not None:
                        item[field] = float(Decimal(str(item[field])))
                item.update(basis=basis, warnings=run["warnings"], run_id=run["id"])
                item["benchmark_path"] = (
                    "ISSUER_PUBLISHED"
                    if "ISSUER_COMPOSITE_NOT_COMPONENT_RECONSTRUCTION" in run["warnings"]
                    else "COMPONENT_RECONSTRUCTION"
                )
                # Keep a daily reconstruction when a later report-only attempt is blocked.
                if key not in windows or basis == "DAILY_SERIES":
                    windows[key] = item

    def evidence_ids(value: Any) -> set[str]:
        if isinstance(value, dict):
            ids = set(value.get("evidence_ids", []))
            for key, child in value.items():
                if key.endswith("evidence_ids") and isinstance(child, list):
                    ids.update(child)
                else:
                    ids.update(evidence_ids(child))
            return ids
        if isinstance(value, list):
            return {eid for child in value for eid in evidence_ids(child)}
        return set()

    used = evidence_ids(mapping)
    for run in record["runs"]:
        if mapping and run["mapping_id"] == mapping["id"]:
            used.update(evidence_ids(run.get("input", {})))
    sources = [
        {key: source[key] for key in ("id", "source_name", "source_ref", "evidence_date")}
        for source in record.get("evidence", [])
        if source["id"] in used
    ]
    values = sorted(windows.values(), key=lambda x: (x["end"], x["start"]), reverse=True)
    signs = {
        1 if w["excess_percentage_points"] > 0 else -1 if w["excess_percentage_points"] < 0 else 0
        for w in values
    }
    reasons = []
    if -1 in signs:
        reasons.append("存在落后对应基准的观察窗口, 复核持有解释; 不是卖出信号")
    if -1 in signs and 1 in signs:
        reasons.append("窗口选择敏感, 不能只选领先窗口")
    if "DISCLOSED_RETURN_MISMATCH" in warnings:
        reasons.append("重算与官方披露不一致, 先核对数据口径")
    if values and not reasons:
        reasons.append("已观察窗口未见落后, 不代表未来安全或已证明主动能力")
    if not values:
        reasons.append("尚无可比观察窗口; 数据不足不等于表现不佳")
    latest_end = max((w["end"] for w in values), default=None)
    next_steps = []
    if not approved:
        next_steps.append("研究映射待具体确认, 不进入正式风险动作")
    if gaps or not values:
        next_steps.append("补齐列明的身份、有效期、日期和收益口径缺口")
    if any(w["basis"] == "ISSUER_REPORTED_ONLY" for w in values):
        next_steps.append("报告比较可读; 日序列、共同交易日回撤及独立复算仍缺")
    if "DISCLOSED_RETURN_MISMATCH" in warnings:
        next_steps.append("查明披露差异, 保留原值和警告")
    next_steps.append("独立来源及归因仍需核对; 不据历史差额自动买卖")
    return dict(
        mapping_version=mapping["version"] if mapping else None,
        mapping_approved=bool(approved),
        newer_candidate_pending=bool(approved and versions[-1]["id"] != mapping["id"]),
        observed_through=latest_end,
        observation_age_days=(today - date.fromisoformat(latest_end)).days if latest_end else None,
        classification="DATA_INSUFFICIENT"
        if not values
        else "REVIEW"
        if -1 in signs or "DISCLOSED_RETURN_MISMATCH" in warnings
        else "OBSERVED_ONLY",
        reasons=reasons,
        next_steps=next_steps,
        windows=values,
        evidence_ids=sorted(used),
        sources=sources,
        data_quality="WARNING",
        gaps=sorted(gaps),
        warnings=sorted(warnings),
        missing_observations={name: sorted(days) for name, days in missing.items()},
        drawdown_label=DRAWDOWN_LABEL,
        limitations=mapping["limitations"] if mapping else ["尚无研究映射"],
        money_action=False,
    )


def portfolio_summary(positions: list[Json], records: dict[str, Json], *, today: date) -> Json:
    items = []
    lines = [f"组合研究摘要 | 查询日期 {today} | 仅研究, 不产生买卖信号"]
    for position in positions:
        holding = position["holding"]
        if Decimal(str(holding["total_shares"])) <= 0:
            continue
        code = holding["instrument_code"]
        item = summarize_fund(records[code], today=today)
        item.update(instrument_code=code, instrument_name=holding["instrument_name"])
        item["holding_data_quality"] = position["data_quality"]
        item["holding_nav_date"] = (position.get("nav_snapshot") or {}).get("nav_date")
        if "thesis" in records[code]:
            item["thesis"] = records[code]["thesis"]
            if item["thesis"]["status"] == "REVIEW_REQUIRED":
                item["classification"] = "REVIEW"
                item["reasons"].append("已确认研究论点存在可追溯复核触发, 见论点证据")
                item["next_steps"].append("复核具体论点版本及证据; 不自动改变投资金额")
        items.append(item)
        approval = "研究映射已批准" if item["mapping_approved"] else "研究映射待确认"
        lines += [
            f"- {code} {item['instrument_name']} | 观察截至 {item['observed_through'] or '缺失'}"
            f" | {approval} | WARNING",
            "  依据: " + "; ".join(item["reasons"]),
            "  局限: " + "; ".join(item["gaps"] or item["limitations"]),
            "  下一步: " + "; ".join(item["next_steps"]),
        ]
        if "thesis" in item:
            lines.append(item["thesis"]["display_text"])
        for window in item["windows"]:
            drawdown = window["fund_max_drawdown_pct"]
            lines.append(
                f"  {window['start']} 至 {window['end']}: "
                f"基金 {window['fund_return_pct']:.2f}%, "
                f"基准 {window['benchmark_return_pct']:.2f}%, "
                f"差额 {window['excess_percentage_points']:+.2f} 个百分点; "
                + (
                    f"{DRAWDOWN_LABEL} {drawdown:.2f}%"
                    if drawdown is not None
                    else f"{DRAWDOWN_LABEL}: 缺少日序列"
                )
                + (
                    "; 仅管理人披露"
                    if window["basis"] == "ISSUER_REPORTED_ONLY"
                    else "; 管理人公布路径, 非成分独立重建"
                    if window["benchmark_path"] == "ISSUER_PUBLISHED"
                    else ""
                )
            )
        for name, days in item["missing_observations"].items():
            lines.append(f"  缺少观测: {name}, {len(days)} 日; " + ", ".join(days))
        for source in item["sources"]:
            lines.append(f"  来源: {source['source_name']} {source['source_ref']}")
    lines.append(f"{DRAWDOWN_LABEL}仅来自日序列路径; 报告累计亏损不能替代。")
    return dict(
        query_date=today.isoformat(),
        items=items,
        money_action=False,
        classification_counts=dict(Counter(i["classification"] for i in items)),
        data_quality="WARNING",
        drawdown_label=DRAWDOWN_LABEL,
        display_text="\n".join(lines),
    )


def notification_status(outbox: list[Json], attempts: list[Json]) -> Json:
    """A delivered database flag is not recipient receipt evidence."""
    counts = dict(Counter(x["status"] for x in outbox))
    evidence = dict(
        Counter((x.get("evidence") or {}).get("evidence_level", "UNKNOWN") for x in attempts)
    )
    failures = sorted({x["last_error_code"] for x in outbox if x.get("last_error_code")})
    truncated = len(outbox) == 500 or len(attempts) == 500
    return dict(
        observed_outbox_counts=counts,
        observed_evidence_levels=evidence,
        recipient_delivery_verified=None,
        history_may_be_truncated=truncated,
        latest_attempt_at=attempts[0]["claimed_at"] if attempts else None,
        failures=failures,
        sends_performed=0,
        display_text="通知链只读检查: "
        + str(counts)
        + "; 回执证据层级 "
        + str(evidence)
        + "\n平台受理或 DELIVERED 标记不等于用户实际收到; 接收端验收状态未知。"
        + "\n下一步: 核对目标渠道及独立投递进程, 再进行一条经授权的收件测试。"
        + ("\n历史记录达到查询上限, 计数不是全量。" if truncated else "")
        + ("\n失败原因: " + ", ".join(failures) if failures else ""),
    )
