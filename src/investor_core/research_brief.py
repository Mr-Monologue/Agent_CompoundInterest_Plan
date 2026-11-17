"""On-demand public research: fetch, compare sources, calculate and explain; never write Core."""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from investor_core.market_providers import AkshareOpenFundProvider

INDEXES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指"}
MAX_BYTES = 5 * 1024 * 1024
Json = dict[str, Any]


def pct(start: Decimal, end: Decimal) -> str:
    return str(((end / start - 1) * 100).quantize(Decimal("0.01")))


def valid_points(points: list[tuple[date, Decimal]]) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    for day, value in points:
        if (
            day in result
            or not value.is_finite()
            or not Decimal("1e-9") <= value <= Decimal("1e12")
        ):
            raise ValueError("Duplicate date or invalid price")
        result[day] = value
    if not result:
        raise ValueError("Empty price series")
    return result


def metrics(points: dict[date, Decimal], start: date, end: date) -> Json:
    eligible = sorted(day for day in points if day <= end)
    if not eligible:
        raise ValueError("No observations at or before requested end")
    latest = eligible[-1]
    result: Json = {
        "latest_date": latest.isoformat(),
        "latest_value": str(points[latest]),
        "lag_calendar_days": (end - latest).days,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "period_return_pct": pct(points[start], points[end])
        if start in points and end in points
        else None,
        "last_observation_return_pct": None,
        "history_sample_size": 0,
        "absolute_move_percentile": None,
        "missing_endpoints": [d.isoformat() for d in (start, end) if d not in points],
    }
    if len(eligible) >= 2:
        previous = eligible[-2]
        result["previous_observation_date"] = previous.isoformat()
        result["last_observation_return_pct"] = pct(points[previous], points[latest])
        # Compare unrounded magnitudes, excluding the observation under evaluation.
        moves = [(points[b] / points[a] - 1) for a, b in pairwise(eligible)]
        history = moves[:-1][-252:]
        result["history_sample_size"] = len(history)
        if len(history) >= 120:
            rank = sum(abs(move) <= abs(moves[-1]) for move in history)
            result["absolute_move_percentile"] = str(
                (Decimal(rank) / len(history) * 100).quantize(Decimal("0.1"))
            )
    if start in points and end in points:
        start_index = eligible.index(start)
        span = eligible.index(end) - start_index
        if span > 0:
            historical = [
                (points[eligible[i]] / points[eligible[i - span]] - 1)
                for i in range(span, start_index + 1)
            ][-252:]
            result["period_observation_intervals"] = span
            result["period_history_sample_size"] = len(historical)
            result["period_absolute_move_percentile"] = None
            if len(historical) >= 120:
                current = points[end] / points[start] - 1
                rank = sum(abs(move) <= abs(current) for move in historical)
                result["period_absolute_move_percentile"] = str(
                    (Decimal(rank) / len(historical) * 100).quantize(Decimal("0.1"))
                )
    for window in (20, 60):
        if len(eligible) > window:
            baseline = eligible[-window - 1]
            result[f"return_{window}_observations_pct"] = pct(points[baseline], points[latest])
            result[f"start_{window}_observations"] = baseline.isoformat()
    recent = eligible[-60:]
    peak_day = max(recent, key=lambda day: points[day])
    result["recent_peak_date"] = peak_day.isoformat()
    result["from_recent_peak_pct"] = pct(points[peak_day], points[latest])
    result["recent_peak_observations"] = len(recent)
    return result


class PublicResearch:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport

    def download(self, url: str, params: dict[str, str] | None = None) -> tuple[str, Json]:
        with (
            httpx.Client(
                timeout=15,
                transport=self.transport,
                trust_env=self.transport is None,
                follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as client,
            client.stream("GET", url, params=params) as response,
        ):
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ValueError("Provider response exceeded limit")
            text = bytes(body).decode("utf-8-sig")
            return text, {
                "url": str(response.url),
                "retrieved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "sha256": hashlib.sha256(body).hexdigest(),
            }

    def index(self, symbol: str, start: date, end: date) -> Json:
        sources: list[Json] = []
        errors: list[Json] = []
        histories: dict[str, dict[date, Decimal]] = {}
        for provider in ("EASTMONEY", "SINA"):
            try:
                if provider == "EASTMONEY":
                    raw, source = self.download(
                        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                        {
                            "secid": ("1." if symbol.startswith("sh") else "0.") + symbol[2:],
                            "klt": "101",
                            "fqt": "0",
                            "beg": min(start, end - timedelta(days=900)).strftime("%Y%m%d"),
                            "end": end.strftime("%Y%m%d"),
                            "lmt": "640",
                            "fields1": "f1,f2,f3,f4,f5,f6",
                            "fields2": "f51,f52,f53,f54,f55,f56,f57",
                        },
                    )
                    data = json.loads(raw)
                    if data.get("rc") != 0 or data["data"]["code"] != symbol[2:]:
                        raise ValueError("Unexpected index identity")
                    rows = [row.split(",") for row in data["data"]["klines"]]
                    raw_points = [(date.fromisoformat(row[0]), Decimal(row[2])) for row in rows]
                else:
                    raw, source = self.download(
                        "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                        {"symbol": symbol, "scale": "240", "ma": "no", "datalen": "640"},
                    )
                    rows = json.loads(raw)
                    raw_points = [
                        (date.fromisoformat(row["day"]), Decimal(row["close"])) for row in rows
                    ]
                parsed = valid_points(raw_points)
                if not any(day <= end for day in parsed):
                    raise ValueError("No eligible observations")
                histories[provider] = parsed
                sources.append({**source, "provider": provider})
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
                ArithmeticError,
                AttributeError,
            ) as exc:
                errors.append({"provider": provider, "error": type(exc).__name__})
        result: Json = {
            "code": symbol,
            "name": INDEXES[symbol],
            "basis": "PRICE_INDEX",
            "sources": sources,
            "fetch_errors": errors,
        }
        if not histories:
            return {**result, "status": "UNAVAILABLE"}
        selected = next(iter(histories))
        points = histories[selected]
        result.update(metrics(points, start, end))
        # Validate every eligible observation used in rolling history, not just today's close.
        eligible = sorted(day for day in points if day <= end)
        span = len([day for day in eligible if start < day <= end])
        dates = eligible[-(254 + span) :]
        conflicts = []
        matched = 0
        if len(histories) == 2:
            other = histories["SINA"]
            for day in dates:
                if day in other:
                    if abs(points[day] - other[day]) <= Decimal("0.011"):
                        matched += 1
                    else:
                        conflicts.append(day.isoformat())
        result.update(
            {
                "status": "SOURCE_CONFLICT" if conflicts else "OBSERVED",
                "selected_provider": selected,
                "matched_observations": matched,
                "compared_observations": len(dates),
                "conflict_dates": conflicts,
                "quality": "TWO_CHANNELS_AGREE"
                if matched == len(dates) and not conflicts
                else "WARNING",
                "independent_upstream_verified": False,
            }
        )
        result["observations"] = [
            {"date": day.isoformat(), "close": str(points[day])} for day in dates
        ]
        if conflicts:
            result["absolute_move_percentile"] = None
            result["period_absolute_move_percentile"] = None
        return result

    def fund(self, code: str, start: date, end: date) -> Json:
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("Fund code must contain exactly six digits")
        url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
        result: Json = {"code": code, "basis": "UNIT_NAV_NOT_TOTAL_RETURN", "quality": "WARNING"}
        try:
            raw, source = self.download(url)
            identity = re.search(r'fS_code\s*=\s*["\'](\d{6})["\']', raw)
            if not identity or identity[1] != code:
                raise ValueError("Unexpected fund identity")
            name = re.search(r'fS_name\s*=\s*["\']([^"\']+)["\']', raw)
            points = valid_points(AkshareOpenFundProvider._parse_payload(raw, code))
            result.update(metrics(points, start, end))
            # Do not infer unusual investment returns from unadjusted NAV/dividend changes.
            result.pop("absolute_move_percentile", None)
            result.pop("period_absolute_move_percentile", None)
            result["observations"] = [
                {"date": day.isoformat(), "nav": str(points[day])}
                for day in sorted(points)
                if start <= day <= end
            ]
            return {
                **result,
                "name": name[1] if name else code,
                "status": "OBSERVED",
                "sources": [source],
            }
        except Exception as exc:
            # A single unavailable fund must not suppress the remaining portfolio research.
            return {**result, "status": "UNAVAILABLE", "error": type(exc).__name__}

    def run(self, start: date, end: date, funds: list[str], *, now: datetime | None = None) -> Json:
        clock = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        if clock.tzinfo is None:
            raise ValueError("Timezone-aware clock required")
        clock = clock.astimezone(ZoneInfo("Asia/Shanghai"))
        if start >= end or end > clock.date():
            raise ValueError("Require start < end <= today")
        if end == clock.date() and clock.hour < 16:
            raise ValueError("Today's completed daily close is only requested after 16:00 Shanghai")
        if any(not re.fullmatch(r"\d{6}", code) for code in funds):
            raise ValueError("Fund code must contain exactly six digits")
        if len(set(funds)) > 30:
            raise ValueError("At most 30 funds per request")
        with ThreadPoolExecutor(max_workers=4) as pool:
            indices = list(pool.map(lambda symbol: self.index(symbol, start, end), INDEXES))
            fund_results = list(
                pool.map(lambda code: self.fund(code, start, end), sorted(set(funds)))
            )
        report: Json = {
            "generated_at": clock.isoformat(),
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "indices": indices,
            "funds": fund_results,
            "mode": "PUBLIC_RESEARCH_NO_CORE_WRITES",
            "portfolio": None,
        }
        report["display_text"] = render(report)
        return report


def render(report: Json) -> str:
    indices = report["indices"]
    usable = [
        r for r in indices if r["status"] == "OBSERVED" and r.get("period_return_pct") is not None
    ]
    if len(usable) == len(INDEXES):
        changes = [Decimal(r["period_return_pct"]) for r in usable]
        direction = (
            "共同上涨"
            if all(x > 0 for x in changes)
            else ("共同下跌" if all(x < 0 for x in changes) else "表现分化")
        )
        view = f"所选三个主要指数在指定区间{direction}。"
    else:
        view = "部分指数端点缺失或来源冲突, 仅对可核对的部分形成判断。"
    if len(usable) == len(INDEXES) and all(
        r.get("return_20_observations_pct") is not None
        and Decimal(r["return_20_observations_pct"]) < 0
        for r in usable
    ):
        view += "三个指数最近20个观测间隔也均下跌, 回落并不限于今天。"
    if len(usable) == len(INDEXES) and all(
        r.get("absolute_move_percentile") is not None
        and Decimal(r["absolute_move_percentile"]) < 50
        for r in usable
    ):
        view += "最新单次波动绝对值均低于各自历史样本中位位置, 应重点看累计变化。"
    lines = [
        f"投资研究简报 | {report['period_start']} → {report['period_end']}",
        "当前判断: " + view + "这描述价格方向, 不代表已经解释原因或识别买卖时机。",
        "证据:",
    ]
    for row in indices:
        if row["status"] == "UNAVAILABLE":
            lines.append(f"- {row['name']}: 两个渠道获取失败, 已保留错误。")
            continue
        lines.append(
            f"- {row['name']}: 区间 {row['period_return_pct'] or '缺端点'}%; "
            f"最近两次观测 {row['last_observation_return_pct']}%; "
            f"最新 {row['latest_date']}; 来源状态 {row['quality']}。"
        )
        if row["status"] == "SOURCE_CONFLICT":
            lines.append("  来源有冲突, 上述为选定渠道的待核对值, 不用于总体判断。")
        if row.get("absolute_move_percentile") is not None:
            lines.append(
                f"  最近一次波动绝对值位于此前 {row['history_sample_size']} 个观测间隔的 "
                f"{row['absolute_move_percentile']} 百分位; 非预测概率或显著性检验。"
            )
        if row.get("period_absolute_move_percentile") is not None:
            lines.append(
                f"  区间累计波动绝对值: 历史相同 {row['period_observation_intervals']} "
                f"个观测间隔窗口的 {row['period_absolute_move_percentile']} 百分位 "
                f"({row['period_history_sample_size']} 个重叠样本, 均结束于本区间开始前或当日)。"
            )
        if row.get("return_20_observations_pct") is not None:
            lines.append(
                f"  最近20个观测间隔 {row['return_20_observations_pct']}%; "
                f"距最近{row['recent_peak_observations']}次观测的最高收盘 "
                f"{row['from_recent_peak_pct']}%。"
            )
    lines.extend(
        [
            "反证与解释边界: 三个指数不能代表全市场广度; 单日反弹或回落不能证明趋势反转。",
            "历史百分位基于渠道返回的相邻观测, 未独立补齐交易日历; 来源同向不证明独立上游。",
        ]
    )
    if report["funds"]:
        lines.append("关注基金的直接证据 (单位净值变化, 未处理分红, 不是个人收益):")
        for row in report["funds"]:
            if row["status"] == "UNAVAILABLE":
                lines.append(f"- {row['code']}: 本次获取失败 ({row['error']})。")
            else:
                lines.append(
                    f"- {row['code']} {row['name']}: 最新净值 {row['latest_value']} "
                    f"({row['latest_date']}); 指定区间变化 "
                    f"{row['period_return_pct'] or '缺端点'}%; 单源待核验。"
                )
    lines.extend(
        [
            "对新增资金的判断: 这些价格证据不足以支持仅因跌幅扩大就增加风险预算; "
            "若要改变原安排, 需把资金期限、承受回撤及持有理由一起检查。",
            "选择比较 (研究参考, 不改变既有安排):",
            "- 维持原安排: 避免因短期价格波动频繁切换, 代价是继续承担原有风险暴露。",
            "- 暂缓新增: 降低新增资金的即时风险, 代价是可能错失上涨和持有现金的机会成本。",
            "- 调整持有标的: 需先证明持有理由或工具条件改变; 指数涨跌本身不是充分依据。",
            "改变判断所需证据: 下一完整交易日是否延续同方向; 对应基金净值是否跟随; "
            "若基金背离, 检查分红、汇率、披露持仓和跟踪基准, 不直接解释为经理能力。",
            "本轮数据支持价格与相对波动观察; 尚不支持估值便宜、盈利拐点或投资胜率结论。",
        ]
    )
    portfolio = report.get("portfolio")
    if portfolio:
        lines.append("持仓连接: 已按 Core 当前持仓选择基金; 不以旧聊天记录补持仓。")
    else:
        lines.append("未连接当前持仓; 上述基金仅为关注清单, 未计算组合收益或资金方案。")
    for row in indices + report["funds"]:
        for source in row.get("sources", []):
            lines.append(f"来源 {row['code']}: {source['url']} (读取 {source['retrieved_at']})")
    return "\n".join(lines)


def default_start(end: date) -> date:
    return end - timedelta(days=end.weekday() + 3)
