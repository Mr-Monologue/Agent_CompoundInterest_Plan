"""Stage exact input groups and derive evidence/window changes before publication."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from investor_core.peer_models import PeerStudy
from investor_core.peer_research import evaluate
from investor_core.research_update_sources import CASE_CODES, CASH_URL, endpoint, fingerprint

Json = dict[str, Any]


def normalize_points(rows: list[Json], start: str) -> list[Json]:
    return [
        dict(day=r["day"], value=format(Decimal(str(r["value"])).normalize(), "f"))
        for r in rows
        if r["day"] >= start
    ]


def diffs(old: Json, new: Json) -> list[Json]:
    before = {(p["code"], w["label"]): w for p in old["rows"] for w in p["windows"]}
    changes = []
    for p in new["rows"]:
        for w in p["windows"]:
            b = before.get((p["code"], w["label"]))
            if not b:
                continue
            moved = (b["start"], b["end"]) != (w["start"], w["end"])
            for metric, unit in [
                ("return_pct", "%"),
                ("max_drawdown_pct", "%"),
                ("peer_difference_pp", "百分点"),
            ]:

                def value(x: Json, metric: str = metric) -> Any:
                    return (
                        x.get(metric)
                        if metric == "peer_difference_pp"
                        else (x.get("calculated") or {}).get(metric)
                    )

                a, z = value(b), value(w)
                if a == z and not moved:
                    continue
                visible = a is None or z is None or f"{a:.2f}" != f"{z:.2f}"
                changes.append(
                    dict(
                        code=p["code"],
                        window=w["label"],
                        metric=metric,
                        unit=unit,
                        before=a,
                        after=z,
                        before_end=b["end"],
                        after_end=w["end"],
                        kind="WINDOW_ADVANCE" if moved else "HISTORICAL_REVISION",
                        materiality="VISIBLE" if visible else "DISPLAY_PRECISION_ONLY",
                        evidence=w["evidence_ids"],
                    )
                )
    return changes


def build(old: Json, today: date, probe: Callable[..., Json]) -> Json:
    """Each probe persists raw/semantic provenance; no finance or D1 writes here."""
    s = deepcopy(old["archived_input"])
    anchor = s["anchor_code"]
    codes = CASE_CODES[anchor]
    start = min(w["start"] for w in s["windows"])
    primary: dict[str, Json] = {}
    checks: dict[str, Json] = {}
    auxiliary: list[Json] = []
    products = {p["code"]: p for p in s["products"]}
    for code in codes:
        p = products[code]
        for kind, ref in [
            ("nav", p["nav"]["evidence_ids"][0]),
            ("div", p["distribution_sources"][0]),
        ]:
            source = s["sources"][ref]
            query = dict(parse_qsl(urlsplit(source["source_ref"]).query))
            if any(query[k] != code for k in ("productCode", "fundcode") if k in query):
                raise ValueError("SOURCE_REQUEST_SHARE_IDENTITY_MISMATCH")
            primary[code + "-" + kind] = probe(
                code + "-" + kind, endpoint(source["source_ref"], today.isoformat()), kind, code
            )
        for kind, url in [
            ("channel_nav", f"https://fund.eastmoney.com/pingzhongdata/{code}.js"),
            ("channel_div", f"https://fundf10.eastmoney.com/fhsp_{code}.html"),
        ]:
            checks[code + "-" + kind] = probe(code + "-" + kind, url, kind, code)
        if code == "022463":
            page = "https://www.fullgoal.com.cn/fundDetail/022463/index.html"
        elif code == "003096":
            page = "https://www.zofund.com/fundDetail/003096/index.html"
        else:
            page = "https://gfwx.gffunds.com.cn/funds/?fundcode=" + code
        auxiliary.append(probe(code + "-disclosure", page, "disclosure", code))
    index = "000510" if anchor == "022463" else "000933"
    calref = s["calendar_sources"][0]
    primary["calendar"] = probe(
        "calendar",
        endpoint(s["sources"][calref]["source_ref"], today.isoformat()),
        "calendar",
        index,
    )
    if anchor == "022463":
        auxiliary.append(probe("cash", CASH_URL, "cash", "PUBLIC"))
    blocked = [
        k + ":" + v.get("error", "UNKNOWN") for k, v in primary.items() if v["status"] != "SUCCESS"
    ]
    result: Json = dict(
        anchor_code=anchor,
        before_version=old["version"],
        before_cutoff=old["common_research_cutoff"],
        checks=list(primary.values()) + list(checks.values()) + auxiliary,
        limitations=[
            "仅两准确份额净值/分红/实际交易日历更新;结构及规则披露保留原日期,目录完整性未证实",
            "不同发布渠道不证明独立上游;单源与既有警告保留",
        ],
        changes=[],
    )
    if blocked:
        return dict(result, status="BLOCKED", blockers=blocked, candidate=None)
    calendar = [
        r["day"] for r in primary["calendar"]["parsed"] if start <= r["day"] <= today.isoformat()
    ]
    navs = {code: normalize_points(primary[code + "-nav"]["parsed"], start) for code in codes}
    if not calendar or any(not v for v in navs.values()):
        return dict(
            result, status="BLOCKED", blockers=["EMPTY_COMPLETE_CALENDAR_OR_NAV"], candidate=None
        )
    if any(r["day"] > today.isoformat() for rows in navs.values() for r in rows):
        return dict(result, status="BLOCKED", blockers=["FUTURE_NAV_DATE"], candidate=None)
    cutoff = min(calendar[-1], *(v[-1]["day"] for v in navs.values()))
    dates = [d for d in calendar if d <= cutoff]
    missing = {code: sorted(set(dates) - {v["day"] for v in navs[code]}) for code in codes}
    if any(missing.values()) or cutoff < old["common_research_cutoff"]:
        return dict(
            result,
            status="BLOCKED",
            blockers=["COMMON_CALENDAR_INCOMPLETE_OR_REGRESSED"],
            missing_dates=missing,
            candidate=None,
        )
    # Primary and channel conflicts block publication, preserving both raw sources.
    channel_reports: list[Json] = []
    conflict = False
    for code in codes:
        a, b = checks[code + "-channel_nav"], checks[code + "-channel_div"]
        if a["status"] != "SUCCESS" or b["status"] != "SUCCESS":
            channel_reports.append(dict(code=code, status="FAILED", upstream="UNKNOWN"))
            continue
        values = {r["day"]: Decimal(r["value"]) for r in a["parsed"]}
        official = {r["day"]: Decimal(r["value"]) for r in navs[code]}
        absent = [d for d in dates if d not in values]
        differences = [
            dict(day=d, official=str(official[d]), channel=str(values[d]))
            for d in dates
            if d in values and official[d] != values[d]
        ]
        da = {
            r["ex_date"]: Decimal(r["cash_per_unit"])
            for r in primary[code + "-div"]["parsed"]
            if start < r["ex_date"] <= cutoff
        }
        db = {
            r["ex_date"]: Decimal(r["cash_per_unit"])
            for r in b["parsed"]
            if start < r["ex_date"] <= cutoff
        }
        # Do not invent a tolerance for an undocumented new source precision.
        status = (
            "CONFLICT"
            if differences or da != db
            else "INCOMPLETE"
            if absent
            else "EXACT_CHANNEL_AGREEMENT"
        )
        conflict |= status == "CONFLICT"
        channel_reports.append(
            dict(
                code=code,
                status=status,
                upstream="UNKNOWN",
                dates_checked=len(dates) - len(absent),
                missing_dates=absent,
                nav_differences=differences,
                dividend_differences=[
                    dict(ex_date=d, official=str(da.get(d)), channel=str(db.get(d)))
                    for d in sorted(set(da) | set(db))
                    if da.get(d) != db.get(d)
                ],
                precision_policy="逐项精确相等;差异保留待核,不拟合容差",
                evidence=[a["evidence_id"], b["evidence_id"]],
            )
        )
    result["channel_checks"] = channel_reports
    if conflict:
        return dict(result, status="BLOCKED", blockers=["CROSS_CHANNEL_CONFLICT"], candidate=None)
    evidence_changes = []

    def replace_source(ref: str, receipt: Json, code: str, value: Any, previous: Any) -> None:
        if fingerprint(value) == fingerprint(previous):
            return
        evidence_changes.append(
            dict(
                source=ref,
                evidence_id=receipt["evidence_id"],
                kind="NEW_EVIDENCE",
                prior_hash=fingerprint(previous),
                current_hash=fingerprint(value),
            )
        )
        s["sources"][ref] = dict(
            instrument_code=code,
            source_name=ref,
            source_ref=receipt["url"],
            source_lineage="CHOICE_UPSTREAM_UNCONFIRMED"
            if "channel" in receipt["kind"]
            else "OFFICIAL_ISSUER",
            retrieved_at=receipt["retrieved_at"],
            published_date=None,
            data_date=cutoff,
            excerpt="本次原始公开返回已暂存核验;语义指纹与内容可回读。",
            original_sha256=receipt["raw_hash"],
            quality="REPOST" if "channel" in receipt["kind"] else "OFFICIAL",
            facts=dict(update_evidence_id=receipt["evidence_id"], semantic_value=value),
        )

    for code in codes:
        p = products[code]
        ref = p["nav"]["evidence_ids"][0]
        newpoints = [r for r in navs[code] if r["day"] <= cutoff]
        replace_source(
            ref,
            primary[code + "-nav"],
            code,
            newpoints,
            normalize_points(p["nav"]["points"], start),
        )
        p["nav"]["points"] = newpoints
        divs = primary[code + "-div"]["parsed"]
        ref = p["distribution_sources"][0]
        olddivs = [
            dict(
                ex_date=d["ex_date"],
                cash_per_unit=format(Decimal(d["cash_per_unit"]).normalize(), "f"),
            )
            for d in p["distributions"]
        ]
        replace_source(
            ref, primary[code + "-div"], code, divs, sorted(olddivs, key=lambda x: x["ex_date"])
        )
        p["distributions"] = divs
        p["distribution_to"] = cutoff
        for kind in ["channel_nav", "channel_div"]:
            receipt = checks[code + "-" + kind]
            if receipt["status"] != "SUCCESS":
                continue
            ref = "update-" + code + "-" + kind
            val = (
                normalize_points(receipt["parsed"], start)
                if kind == "channel_nav"
                else receipt["parsed"]
            )
            previous = s["sources"].get(ref, {}).get("facts", {}).get("semantic_value")
            replace_source(ref, receipt, code, val, previous)
        if s.get("validation"):
            ch = next(c for c in s["validation"]["checks"] if c["code"] == code)
            if all(
                checks[code + "-" + k]["status"] == "SUCCESS"
                for k in ["channel_nav", "channel_div"]
            ):
                ch["nav"]["points"] = normalize_points(
                    checks[code + "-channel_nav"]["parsed"], start
                )
                ch["nav"]["evidence_ids"] = ["update-" + code + "-channel_nav"]
                ch["distributions"] = checks[code + "-channel_div"]["parsed"]
                ch["distribution_sources"] = ["update-" + code + "-channel_div"]
                ch["distribution_to"] = cutoff
    replace_source(
        calref,
        primary["calendar"],
        index,
        dates,
        [d for d in s["calendar_dates"] if start <= d <= old["common_research_cutoff"]],
    )
    s["calendar_dates"] = dates
    if s.get("tracking_reference"):
        s["tracking_reference"]["series"]["points"] = [
            r for r in primary["calendar"]["parsed"] if r["day"] <= cutoff
        ]
    # The original four fixed windows are immutable; only the two explicitly rolling ends move.
    for w in s["windows"][-2:]:
        w["end"] = cutoff
    s["expected_previous_version"] = old["version"]
    s["knowledge_date"] = today.isoformat()
    s["idempotency_key"] = "update-" + fingerprint({"anchor": anchor, "inputs": s})
    candidate = PeerStudy.model_validate(s)
    calculated = evaluate(candidate)
    if any(w["gaps"] for row in calculated["rows"] if row["code"] in codes for w in row["windows"]):
        return dict(result, status="BLOCKED", blockers=["RECOMPUTATION_INCOMPLETE"], candidate=None)
    result["changes"] = diffs(old, calculated)
    result["evidence_changes"] = evidence_changes
    result["after_cutoff"] = cutoff
    result["recomputed_windows"] = sum(
        len(r["windows"]) for r in calculated["rows"] if r["code"] in codes
    )
    result["calculated"] = calculated
    result["candidate"] = s
    result["publication_needed"] = bool(evidence_changes or cutoff != old["common_research_cutoff"])
    result["status"] = (
        "PARTIAL"
        if any(c["status"] != "EXACT_CHANNEL_AGREEMENT" for c in channel_reports)
        or any(a["status"] != "SUCCESS" or a["kind"] == "disclosure" for a in auxiliary)
        else "SUCCESS"
    )
    result["content_status"] = "NEW_EVIDENCE" if evidence_changes else "NO_NEW_CONTENT"
    result["blockers"] = []
    return result
