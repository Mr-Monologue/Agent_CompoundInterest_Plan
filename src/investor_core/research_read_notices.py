"""Current peer limitations overlay read responses, never historical research state."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from investor_core.research import ResearchService


def attach_peer_limitations(
    research: ResearchService, data: dict[str, Any], codes: set[str]
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
            )
        )
    result["current_peer_limitations"] = notices
    if notices:
        lines = ["", "当前同类研究补充限制(不改写历史基线、论点或差异结果;本次未获取新资料):"]
        for n in notices:
            lines.append(
                f"{'、'.join(n['instrument_codes'])} 同类研究 v{n['version']} | "
                f"研究截至 {n['research_cutoff']}"
            )
            lines.extend("- " + text for text in n["limitations"])
        result["display_text"] = result.get("display_text", "") + "\n".join(lines)
    return result
