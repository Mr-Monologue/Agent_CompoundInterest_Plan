"""Read-only decision context. No new policy, model or business state is activated."""

from __future__ import annotations

from typing import Any

from investor_core.version import __version__

Json = dict[str, Any]


def execution_context(preview: Json, assignment: Json) -> Json:
    """Distinguish strategy allocations from verified platform execution constraints."""
    result = dict(preview)
    configs = {i["instrument_code"]: i for i in assignment["instruments"]}
    items = []
    for item in preview.get("plan", {}).get("instrument_items", []):
        code = item.get("instrument_code")
        if not code:
            continue
        config = configs[code]
        items.append(
            {
                "instrument_code": code,
                "instrument_name": item["instrument_name"],
                "strategy_minimum_amount_minor": config["minimum_amount_minor"],
                "strategy_maximum_amount_minor": config["maximum_amount_minor"],
                "strategy_approved_at": config["approved_at"],
                "platform_status": "EXECUTION_CONSTRAINT_UNKNOWN",
                "source": None,
                "effective_from": None,
                "effective_to": None,
                "maximum_daily_amount_minor": None,
                "confirmation_rule": None,
                "trading_calendar": None,
                "minimum_trading_days": None,
            }
        )
    result["execution_assessment"] = {
        "status": "EXECUTION_CONSTRAINT_UNKNOWN" if items else "NOT_EVALUATED",
        "allocation_only": True,
        "executable_schedule_available": False,
        "schedule": [],
        "items": items,
        "missing": [
            "ACCOUNT_PLATFORM_LIMIT_SOURCE",
            "EFFECTIVE_DATES",
            "SHARE_CLASS_SUBSCRIPTION_STATUS",
            "CONFIRMATION_RULE",
            "TRADING_CALENDAR",
        ],
    }
    result["version_axes"] = {
        "software": __version__,
        "strategy": assignment["strategy"]["version"],
        "model": None,
        "candidate_architecture": "architecture-v1.7.0-draft.2",
        "candidate_strategy_activated": False,
    }
    result["display_text"] = preview["display_text"].replace("可执行金额", "候选分配金额") + (
        "\n\n执行条件核查: EXECUTION_CONSTRAINT_UNKNOWN"
        "\n当前只提供已批准策略的资金分配预览, 不提供可直接执行的拆单日历。"
        "\n策略最小/最大金额不等于平台单日限额; 来源、生效期、份额类别、确认规则"
        "和交易日历尚未核实。"
        "\n历史限额不能作为永久常量; 不据此推算交易日数量或默认 T+1。"
        "\n候选架构 v1.7 尚未激活; 当前 WARNING 预览不能宣称满足 v1.7 的金额门槛。"
    )
    return result


def research_context(
    topic: str, brief: Json, assignment: Json, evidence: dict[str, list[Json]]
) -> Json:
    """Project saved facts and explicit unknowns; never infer a thesis from a status flag."""
    keys = ("医疗", "医药", "健康") if topic == "医疗" else (topic,)
    instruments = [
        i for i in assignment["instruments"] if any(k in i["instrument_name"] for k in keys)
    ]
    positions = {p["holding"]["instrument_code"]: p for p in brief["valuation"]["positions"]}
    dossiers = []
    lines = [
        f"{topic}主题研究诊断 | 截至 {brief['as_of_date']} | WARNING",
        "事实来自 Core 已保存快照; 基金名称仅用于识别候选相关性, 不代表穿透行业暴露。",
    ]
    for i in instruments:
        code = i["instrument_code"]
        position = positions.get(code, {})
        nav = position.get("nav_snapshot") or {}
        records = evidence.get(code, [])
        missing = [
            "VERSIONED_THESIS",
            "RETURN_DRIVER_PROFILE",
            "FUND_MANDATE",
            "COUNTER_EVIDENCE",
            "SECTOR_INDEX_AND_EVENT_EVIDENCE",
            "HOLDINGS_LOOKTHROUGH",
        ]
        dossier = {
            "instrument": i,
            "holding": position,
            "source_ref": nav.get("source_ref"),
            "saved_research_evidence": records,
            "history_may_be_truncated": len(records) == 100,
            "why_hold": None,
            "playbook": None,
            "return_driver": "UNKNOWN",
            "evidence_maturity": "UNVERIFIED",
            "thesis_version": None,
            "legacy_thesis_status": i["thesis_status"],
            "missing": missing,
            "decision_frame": {
                "question": "现有持有理由和新增资金资格是否有足够证据支持",
                "known_facts": {
                    "approved_role": i["strategy_role"],
                    "contribution_eligible": i["contribution_eligible"],
                    "nav_date": nav.get("nav_date"),
                    "nav_quality": position.get("data_quality", "UNKNOWN"),
                },
                "unknowns": missing,
                "chosen_action": "RESEARCH_ONLY_NO_MONEY_ACTION",
                "process_quality": "NOT_REVIEWED",
                "outcome_quality": "UNKNOWN",
                "persisted_decision_journal": False,
            },
            "relative_diagnosis": "DATA_BLOCKED",
        }
        dossiers.append(dossier)
        lines += [
            f"- {code} {i['instrument_name']}: 市值 {position.get('market_value', '缺失')}; "
            f"组合权重 {position.get('weight_pct', '未知')}%; "
            f"净值日期 {nav.get('nav_date', '缺失')}; "
            f"来源 {nav.get('source_ref') or '缺失'}; "
            f"质量 {position.get('data_quality', 'UNKNOWN')}",
            f"  批准角色 {i['strategy_role']}; 基准 {i.get('benchmark_code') or '未配置'}; "
            f"已有研究证据 {len(records)} 条(数量不代表决策级证据)。",
            "  新增资金: "
            + (
                "当前不具备定投资格。"
                if not i["contribution_eligible"]
                else "仍需预算、舱位、在途、信号及执行条件核查。"
            ),
            f"  原持有理由/收益来源: 未形成可读取的版本化证据; 旧状态 {i['thesis_status']} "
            "不等于完整投资论点。",
            "  MAPER 待补: 合同边界、产品优势、长期空间、执行约束、收益可靠性及反方证据。",
            "  相对表现: DATA_BLOCKED; 需确认适用基准、窗口与可比收益数据。",
        ]
        for record in records:
            archived = record.get("facts", {})
            if archived.get("kind") == "EXECUTION_SOURCE_V1":
                lines.append(
                    f"  可复用证据: {record['source_name']}; 数据 {archived['data_date']}; "
                    f"发布 {archived['published_date'] or '未注明'}; "
                    f"获取 {archived['retrieved_at']}; {record['source_ref']}"
                )
    if not dossiers:
        lines.append("当前策略中没有名称匹配的候选; 不代表组合没有相关行业暴露。")
    lines += [
        "上涨及原因未获证实; 不把净值变化、旧论点状态或证据条数当作买入理由。",
        "研究结论: DEEP_RESEARCH_REQUIRED; 过程质量尚未评审, 不能用盈亏倒推过程正确。",
        "四季与候选排位尚未实现/验证, 不生成概率或评分, 不影响金额。",
        "下一步: 补齐合同、持仓、原始论点与反证后再评审; 配置或策略变化须单独确认。",
    ]
    return {
        "topic": topic,
        "as_of_date": brief["as_of_date"],
        "data_quality": "WARNING",
        "evidence": dossiers,
        "causal_claim": "NOT_ESTABLISHED",
        "lookthrough_exposure": "UNKNOWN",
        "research_outcome": "DEEP_RESEARCH_REQUIRED",
        "money_action": False,
        "model_state": "NOT_IMPLEMENTED",
        "probabilities": None,
        "ranking": None,
        "strategy_version": assignment["strategy"]["version"],
        "display_text": "\n".join(lines),
    }
