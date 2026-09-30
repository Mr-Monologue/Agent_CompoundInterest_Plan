"""Bounded public adapters for the two existing research cases, never valuation feeds."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from investor_core.execution import TZ

Json = dict[str, Any]
CASE_CODES = {"022463": ("022463", "022424"), "003096": ("003096", "009163")}
HOSTS = {
    "www.fullgoal.com.cn",
    "www.gffunds.com.cn",
    "www.zofund.com",
    "www.csindex.com.cn",
    "fund.eastmoney.com",
    "fundf10.eastmoney.com",
    "www.pbc.gov.cn",
    "gfwx.gffunds.com.cn",
}
CASH_URL = (
    "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125440/125838/125888/2887261/index.html"
)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def endpoint(url: str, end: str) -> str:
    s = urlsplit(url)
    q = dict(parse_qsl(s.query))
    for k in ("endDate", "enddate"):
        if k in q:
            q[k] = end if "-" in q[k] else end.replace("-", "")
    return urlunsplit((s.scheme, s.netloc, s.path, urlencode(q), ""))


class Fetcher:
    """TLS verified, fixed public hosts, no auth, no redirects, bounded bytes/time/retry."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.last: dict[str, float] = {}
        self.client = httpx.Client(timeout=15, follow_redirects=False, trust_env=True)

    def __call__(self, url: str) -> bytes:
        host = urlsplit(url).hostname
        if urlsplit(url).scheme != "https" or host not in HOSTS:
            raise ValueError("SOURCE_NOT_ALLOWLISTED")
        if time.monotonic() - self.started > 480:
            raise ValueError("CASE_REQUEST_TIME_BUDGET_EXHAUSTED")
        for attempt in range(2):
            time.sleep(max(0, 0.6 - (time.monotonic() - self.last.get(host, 0))))
            self.last[host] = time.monotonic()
            try:
                with self.client.stream(
                    "GET",
                    url,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://" + host + "/"},
                ) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 12_000_000 or time.monotonic() - self.started > 480:
                            raise ValueError("SOURCE_SIZE_OR_TIME_LIMIT")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.TransportError:
                if attempt:
                    raise
                time.sleep(1)
        raise ValueError("FETCH_FAILED")

    def close(self) -> None:
        self.client.close()


def points(rows: list[Json], code: str, kind: str) -> list[Json]:
    values: dict[str, str] = {}
    for r in rows:
        if kind == "csi":
            identity, day, value = r["indexCode"], r["tradeDate"], r["close"]
        elif kind == "gf":
            identity, day, value = r["FUNDCODE"], r["NAVDATE"], r["NAVUNIT"]
        else:
            identity, day, value = r["productCode"], r["navDate"], r["relatePrice"]
        if str(identity) != code:
            raise ValueError("EXACT_SHARE_OR_INDEX_IDENTITY_MISMATCH")
        day = str(day)
        if len(day) == 8:
            day = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        datetime.strptime(day, "%Y-%m-%d")
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0:
            raise ValueError("INVALID_NAV")
        text = format(number.normalize(), "f")
        if day in values and values[day] != text:
            raise ValueError("CONFLICTING_DUPLICATE_DATE")
        values[day] = text
    if not values:
        raise ValueError("EMPTY_SERIES")
    return [dict(day=d, value=v) for d, v in sorted(values.items())]


def parse(raw: bytes, kind: str, code: str) -> Any:
    text = raw.decode("utf-8-sig")
    if kind in {"nav", "calendar"}:
        d = json.loads(text)
        if d.get("code") not in (None, 0, "200") or d.get("errorno") not in (None, "20000"):
            raise ValueError("PROVIDER_ERROR")
        return points(
            d["data"], code, "csi" if kind == "calendar" else "gf" if "errorno" in d else "fg"
        )
    if kind == "div":
        d = json.loads(text)
        if "errorno" in d:
            if d["errorno"] != "20000":
                raise ValueError("PROVIDER_ERROR")
            if str(d.get("totalrows")) == "0" and d["data"] in ("", []):
                return []
            # Unknown non-empty format cannot become a silent zero-dividend assumption.
            raise ValueError("GF_NONEMPTY_DIVIDEND_FORMAT_REQUIRES_VERIFICATION")
        if d.get("code") != 0:
            raise ValueError("PROVIDER_ERROR")
        page = d["data"]
        if page["total"] != len(page["list"]) or page.get("pages", 1) > 1:
            raise ValueError("DIVIDEND_PAGINATION_INCOMPLETE")
        out = []
        for r in page["list"]:
            if code != "003096" or r.get("productId") != 391:
                raise ValueError("DIVIDEND_IDENTITY_OR_UNIT_UNVERIFIED")
            out.append(
                dict(
                    ex_date=r["divideinterest"],
                    cash_per_unit=format((Decimal(str(r["sendinterest"])) / 10).normalize(), "f"),
                )
            )
        return sorted(out, key=lambda x: x["ex_date"])
    if kind == "channel_nav":
        identity = re.search(r'var\s+fS_code\s*=\s*["\'](\d+)["\']', text)
        series = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.S)
        if not identity or identity[1] != code or not series:
            raise ValueError("CHANNEL_EXACT_SHARE_MISSING")
        rows = [
            dict(
                productCode=code,
                navDate=datetime.fromtimestamp(r["x"] / 1000, TZ).date().isoformat(),
                relatePrice=r["y"],
            )
            for r in json.loads(series[1])
        ]
        return points(rows, code, "fg")
    if kind == "channel_div":
        if not re.search(r"<title>[^<]*\(" + re.escape(code) + r"\)", text):
            raise ValueError("CHANNEL_DIVIDEND_IDENTITY_MISSING")
        match = re.search(
            r'<table[^>]*class=["\'][^"\']*cfxq[^"\']*["\'][^>]*>(.*?)</table>', text, re.S
        )
        if not match or "每10份分红" not in match[1]:
            raise ValueError("CHANNEL_DIVIDEND_TABLE_MISSING")
        table = match[1]
        headers = [
            re.sub("<[^>]+>", "", h).strip()
            for h in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)
        ]
        if headers != ["年份", "权益登记日", "除息日", "每10份分红", "分红发放日"]:
            raise ValueError("CHANNEL_DIVIDEND_COLUMNS_UNVERIFIED")
        if "暂无分红信息" in table:
            return []
        out = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
            cells = [
                re.sub("<[^>]+>", "", c).strip()
                for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            ]
            if not cells:
                continue
            if len(cells) != 5:
                raise ValueError("CHANNEL_DIVIDEND_COLUMNS_UNVERIFIED")
            amount = re.fullmatch(r"每10份派现金([\d.]+)元", cells[3])
            if not amount:
                raise ValueError("CHANNEL_DIVIDEND_UNIT_UNKNOWN")
            datetime.strptime(cells[2], "%Y-%m-%d")
            out.append(
                dict(
                    ex_date=cells[2],
                    cash_per_unit=format((Decimal(amount[1]) / 10).normalize(), "f"),
                )
            )
        if not out:
            raise ValueError("DIVIDEND_TABLE_UNRECOGNIZED")
        return sorted(out, key=lambda x: x["ex_date"])
    if kind == "disclosure":
        if code not in text:
            raise ValueError("DISCLOSURE_PAGE_IDENTITY_UNVERIFIED")
        links = sorted(
            set(re.findall(r'(?:href|src)=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', text))
        )
        return dict(
            links=links,
            completeness="UNVERIFIED",
            limitation="实际检查产品页;不能由静态页面证明全部新公告已列出,新文件须内容核验",
        )
    if kind == "cash":
        rows = []
        if "活期存款" not in text or "调整时间" not in text:
            raise ValueError("CASH_RULE_NOT_IDENTIFIED")
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I):
            cells = [
                re.sub(r"\s+", "", re.sub("<[^>]+>", "", cell))
                for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
            ]
            if cells and re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", cells[0]):
                rate = Decimal(cells[1])
                if not rate.is_finite() or rate < 0:
                    raise ValueError("CASH_RATE_INVALID")
                rows.append(
                    dict(
                        adjustment_date=cells[0].replace(".", "-"),
                        demand_rate_annual_pct=format(rate.normalize(), "f"),
                    )
                )
        if not rows:
            raise ValueError("CASH_RATE_TABLE_UNRECOGNIZED")
        return dict(
            rows=sorted(rows, key=lambda r: r["adjustment_date"]),
            applicability="UNVERIFIED",
            limitation="一般存款基准不能证明基金现金腿税后、日计息及假日规则",
        )
    raise ValueError("UNSUPPORTED_SOURCE")
