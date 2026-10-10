"""Isolated, read-only REAL/H query over reviewed local originals; no persistence."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from investor_core import r12_archive
from investor_core.r12_medical import MedicalInput, evaluate

GAPS = {
    "BENCHMARK_MAPPING_INCOMPLETE": "基准身份/适用范围/收益口径未齐",
    "DIVIDEND_COVERAGE_INCOMPLETE": "分红覆盖未齐",
    "OUTSIDE_REVIEWED_ORIGINAL_ADAPTER": "尚未进入已核验原件适配",
    "REQUIRED_SCORE_DIMENSION_MISSING": "必需评分分项未齐(含管理连续性)",
    "SOURCE_MISSING": "必需来源未绑定",
    "TOTAL_FUND_ASSETS_MISSING": "基金总资产证据未齐",
    "redemption_open:MISSING": "观察时点赎回资格未知",
    "subscription_open:MISSING": "观察时点申购资格未知",
}


def read_research(packet_path: Path | None, *, details: bool = False) -> dict[str, Any]:
    """Only server-configured paths are read. Never fall back to a saved result."""
    try:
        if packet_path is None:
            raise ValueError("ARCHIVE_NOT_CONFIGURED")
        packet = json.loads(packet_path.read_text("utf-8"))
        if not isinstance(packet.get("original_paths"), dict):
            raise ValueError("ARCHIVE_PATH_MAPPING_INVALID")
        raw = {}
        missing = []
        for key in r12_archive.manifest()["documents"]:
            path = packet["original_paths"].get(key)
            try:
                if not path:
                    raise FileNotFoundError(key)
                original = Path(path)
                if not original.is_absolute():
                    original = packet_path.parent / original
                raw[key] = base64.b64encode(original.read_bytes()).decode("ascii")
            except OSError:
                missing.append(key)
        if missing:
            raise ValueError("ORIGINALS_UNAVAILABLE:" + ",".join(missing))
        prepared = r12_archive.prepare(packet["base_input"], raw)
        result = evaluate(MedicalInput.model_validate(prepared), originals=raw)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        # No private paths or validation payloads in the default response.
        reason = str(exc).splitlines()[0]
        allowed = ("ORIGINAL", "ARCHIVE_", "REVIEWED_", "NAV_SHARE_", "DUPLICATE_NAV_")
        if not reason.startswith(allowed):
            reason = "ARCHIVE_INPUT_INVALID_OR_UNREADABLE"
        return {
            "status": "BLOCKED",
            "recomputed": False,
            "new_evidence_acquired": False,
            "rows": [],
            "blocker": reason,
            "display_text": "医疗影子研究未完成重算:原件缺失、不可读或输入校验失败。"
            + "\n"
            + reason
            + "\n未展示历史保存结果;未联网、未保存观察或触发前瞻。",
        }
    spec = result["source_review"]
    # Do not label unverified peer dates as an accepted common scoring window.
    product = next(p for p in prepared["products"] if p["code"] == "003096")
    cutoff = max(p["day"] for p in product["nav"]["points"])
    result["query_mode"] = "RECOMPUTED_ARCHIVED_H"
    result["recomputed"] = True
    result["new_evidence_acquired"] = False
    result["nav_cutoff_003096"] = cutoff
    result["material_dates"] = {
        k: v["value"]
        for k, v in spec["claims"].items()
        if k.endswith("date") or k.endswith("since")
    }
    lines = [
        "医疗影子研究:本次用本地原件重算历史 H 类观察,未获取新资料。",
        f"观察日期:{result['as_of']};003096净值截止:{cutoff}。",
        f"资产披露期:{product['assets_report_date']};"
        f"费用适用起点:{product['fees']['effective_from']}。",
        f"版本:{result['computation_version']} / {result['adapter_version']}。",
    ]
    names = {"003096": "中欧医疗健康C", "009163": "广发医疗保健股票C"}
    labels = {"return": "收益", "risk": "风险", "cost": "费用", "management": "管理"}
    for row in result["rows"]:
        scores = (
            "、".join(f"{labels[k]} {Decimal(v):.2f}分" for k, v in row["dimension_scores"].items())
            or "暂无已验证分项"
        )
        blockers = [GAPS.get(g, g) for g in row["gaps"]]
        if row["code"] == "003096" and "BENCHMARK_MAPPING_INCOMPLETE" in row["gaps"]:
            blockers = [
                "H11009历史方法版本未覆盖完整观察窗口(不是映射批准缺失)"
                if g == "BENCHMARK_MAPPING_INCOMPLETE"
                else GAPS.get(g, g)
                for g in row["gaps"]
            ]
        total = "不可用" if row["total_score"] is None else str(row["total_score"])
        lines += [
            f"{row['code']} {names[row['code']]}:{scores};总分{total}。",
            "阻断:" + ";".join(blockers),
        ]
    lines += [
        "两只基金到账上界仍未知,假想替换阻断;不排名,不形成前瞻记录。",
        "保留单源/上游独立性限制;部分发布时间未知,事后取得不等于当时可知。",
        "来源获取时间、有效范围及原件定位可在详情查看。查询不保存观察或业务记录。",
    ]
    if details:
        lines += [
            "详细来源(获取时间不替代发布时间):",
            json.dumps(spec, ensure_ascii=False, indent=2),
        ]
    result["display_text"] = "\n".join(lines)
    if not details:
        for key in ("inputs", "source_review"):
            result.pop(key, None)
    return result
