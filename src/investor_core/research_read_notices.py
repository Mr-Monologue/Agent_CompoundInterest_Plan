"""Current peer evidence judgments overlay reads, never historical research state."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from investor_core.research import ResearchService


def dividend_judgments(study: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    judgments = []
    for source_key, source in study.get("sources", {}).items():
        facts = source.get("facts", {})
        if facts.get("treatment") != "EXCLUDED_BY_FORMAL_ANNUAL_DISCLOSURE":
            continue
        claim = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) cash_per_unit=([0-9.]+)", facts.get("claim", ""))
        if not claim or source.get("quality") != "OFFICIAL":
            continue
        day, cash = claim.groups()
        code = source["instrument_code"]
        row = next((r for r in result["rows"] if r["code"] == code), None)
        if row is None:
            continue
        windows = [
            dict(
                label=w["label"],
                start=w["start"],
                end=w["end"],
                crosses_disputed_date=w["start"] < day <= w["end"],
                calculation_restricted_by_this_claim=False,
            )
            for w in row["windows"]
        ]
        judgments.append(
            dict(
                instrument_code=code,
                ex_date=day,
                cash_per_unit=cash,
                treatment=facts["treatment"],
                source_origin_cause=facts.get("source_origin_cause", "UNKNOWN"),
                source_key=source_key,
                source_ref=source["source_ref"],
                data_date=source["data_date"],
                published_date=source.get("published_date"),
                retrieved_at=source["retrieved_at"],
                original_sha256=source.get("original_sha256"),
                applicability="start < ex_date <= end",
                windows=windows,
                other_warnings_cleared=False,
            )
        )
    return judgments


def attach_peer_limitations(
    research: ResearchService, data: dict[str, Any], codes: set[str], *, historical: bool = False
) -> dict[str, Any]:
    result = deepcopy(data)
    with research._connect() as connection:
        rows = connection.execute("SELECT * FROM peer_research_runs ORDER BY version").fetchall()
    latest = {}
    for row in rows:
        latest[(row["anchor_code"], row["cohort_key"])] = row
    notices = []
    for row in latest.values():
        value = json.loads(row["result_json"])
        matches = sorted(codes & {r["code"] for r in value["rows"]})
        if not matches or not value.get("limitations"):
            continue
        notices.append(
            dict(
                instrument_codes=matches,
                cohort_key=row["cohort_key"],
                version=row["version"],
                run_id=row["id"],
                research_cutoff=value["common_research_cutoff"],
                limitations=list(dict.fromkeys(value["limitations"])),
                evidence_judgments=dividend_judgments(json.loads(row["input_json"]), value),
            )
        )
    result["current_peer_limitations"] = notices
    result["historical_research_requested"] = historical
    if historical:
        result["display_text"] = "历史研究原文(当时状态,不是当前结论):\n" + result.get(
            "display_text", ""
        )
    if notices:
        lines = ["", "当前同类研究补充判断与限制(不改写历史或差异结果;本次未获取新资料):"]
        for n in notices:
            lines.append(
                f"{'、'.join(n['instrument_codes'])} 同类研究 v{n['version']} | "
                f"研究截至 {n['research_cutoff']}"
            )
            for judgment in n["evidence_judgments"]:
                day = judgment["ex_date"]
                cause = (
                    "渠道异常原因未知"
                    if judgment["source_origin_cause"] == "UNKNOWN"
                    else "渠道成因见证据"
                )
                lines.append(
                    f"{judgment['instrument_code']} {day[:4]}年争议分红已依据正式年报排除;{cause}。"
                )
                lines.append(
                    f"范围:仅起点 < {day} <= 终点的窗口涉及本条核查;"
                    "该分红不计收益,不因本条增加计算限制。"
                )
                unaffected = [
                    w["label"] for w in judgment["windows"] if not w["crosses_disputed_date"]
                ]
                lines.append("不跨该日的同类窗口不受本条影响:" + "、".join(unaffected))
                lines.append("五年重算差异、单源、上游独立性分别保留,不因本条排除而解除。")
            lines.extend("- " + text for text in n["limitations"])
        result["display_text"] = result.get("display_text", "") + "\n".join(lines)
    return result
