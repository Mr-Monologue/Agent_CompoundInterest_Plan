"""Exact JSON extraction and bounded Fullgoal NAV response collections.

No merged response is represented as an official document. A consistent collection
does not prove an atomic publisher snapshot, calendar coverage, or independent origin.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from investor_core.r11_provenance import pointer
from investor_core.r11_source_archive import OriginalEnvelope, native_json
from investor_core.scheduler import digest

EXTRACTOR_VERSION = "r11-native-json-v1"
COLLECTION_VERSION = "r11-fullgoal-nav-collection-v1"
MAX_PAGES = 32
MAX_COLLECTION_BYTES = 16 * 1024 * 1024
LIMITATIONS = [
    "EXTRACTION_IS_NOT_PUBLISHER_AUTHENTICATION",
    "INDEPENDENT_UPSTREAM_NOT_VERIFIED",
    "NOT_AN_R11_OBSERVATION_OR_PROMOTION_RECEIPT",
]


def extract_json(original: OriginalEnvelope, location: str) -> dict[str, Any]:
    value = pointer(native_json(original), location)
    return dict(
        extractor_version=EXTRACTOR_VERSION,
        original_sha256=original.sha256,
        pointer=location,
        value=value,
        value_hash=digest(value),
        limitations=LIMITATIONS,
    )


def fullgoal_nav_collection(pages: list[OriginalEnvelope]) -> dict[str, Any]:
    if not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError("bounded nonempty page collection required")
    if sum(page.byte_length for page in pages) > MAX_COLLECTION_BYTES:
        raise ValueError("collection byte limit exceeded")
    counts: tuple[int, int, int] | None = None
    filters: dict[str, str] | None = None
    page_rows: dict[int, list[dict[str, Any]]] = {}
    sources: dict[int, OriginalEnvelope] = {}
    for original in pages:
        parsed = urlsplit(original.request_url)
        if (
            parsed.hostname != "www.fullgoal.com.cn"
            or parsed.path != "/ws-business-server/fund/getFundNavPage"
            or original.final_url != original.request_url
        ):
            raise ValueError("unexpected NAV endpoint or redirect")
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query = dict(pairs)
        if len(query) != len(pairs) or set(query) != {
            "productCode",
            "pageNum",
            "pageSize",
            "startDate",
            "endDate",
            "isPreview",
            "siteno",
        }:
            raise ValueError("ambiguous NAV query")
        fixed = {k: v for k, v in query.items() if k != "pageNum"}
        if fixed != {
            "productCode": "022463",
            "pageSize": query["pageSize"],
            "startDate": "",
            "endDate": "",
            "isPreview": "",
            "siteno": "main",
        }:
            raise ValueError("only the observed unfiltered 022463 NAV contract is supported")
        if filters is not None and filters != fixed:
            raise ValueError("page query filters differ")
        filters = fixed
        document = native_json(original)
        if not isinstance(document, dict) or type(document.get("code")) is not int:
            raise ValueError("invalid NAV response")
        if document["code"] != 0 or not isinstance(document.get("data"), dict):
            raise ValueError("unsuccessful NAV response")
        data = document["data"]
        for key in ("pageNum", "pageSize", "pages", "total"):
            if type(data.get(key)) is not int or data[key] < 1:
                raise ValueError("positive integer pagination metadata required")
        page, size, total_pages, total = (
            data[k] for k in ("pageNum", "pageSize", "pages", "total")
        )
        if (
            str(page) != query["pageNum"]
            or str(size) != query["pageSize"]
            or total_pages != (total + size - 1) // size
            or not 1 <= page <= total_pages <= MAX_PAGES
        ):
            raise ValueError("request/response pagination mismatch")
        current = (size, total_pages, total)
        if counts is not None and counts != current:
            raise ValueError("pagination totals drifted")
        counts = current
        rows = data.get("list")
        expected = min(size, total - (page - 1) * size)
        if not isinstance(rows, list) or len(rows) != expected or page in page_rows:
            raise ValueError("missing, repeated or wrongly sized page")
        page_rows[page], sources[page] = rows, original
    assert counts is not None and filters is not None
    if set(page_rows) != set(range(1, counts[1] + 1)):
        raise ValueError("incomplete page collection")
    rows_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in sorted(page_rows):
        for index, row in enumerate(page_rows[page]):
            if not isinstance(row, dict) or row.get("productCode") != filters["productCode"]:
                raise ValueError("NAV product identity mismatch")
            day = row.get("navDate")
            if (
                not isinstance(day, str)
                or date.fromisoformat(day).isoformat() != day
                or day in seen
            ):
                raise ValueError("invalid or duplicate NAV date")
            seen.add(day)
            raw_value = row.get("relatePrice")
            if not isinstance(raw_value, str):
                raise ValueError("native NAV string required")
            try:
                value = Decimal(raw_value)
            except InvalidOperation as exc:
                raise ValueError("invalid NAV") from exc
            if not value.is_finite() or value <= 0:
                raise ValueError("positive finite NAV required")
            rows_out.append(
                dict(
                    day=day,
                    value=raw_value,
                    original_sha256=sources[page].sha256,
                    day_pointer=f"/data/list/{index}/navDate",
                    value_pointer=f"/data/list/{index}/relatePrice",
                    identity_pointer=f"/data/list/{index}/productCode",
                    page=page,
                )
            )
    days = [row["day"] for row in rows_out]
    if days != sorted(days, reverse=True):
        raise ValueError("unexpected NAV ordering across pages")
    manifest = dict(
        collection_version=COLLECTION_VERSION,
        filters=filters,
        pages=[
            dict(
                page=p,
                original_sha256=sources[p].sha256,
                first_retrieved_at=sources[p].first_retrieved_at.isoformat(),
                request_url=sources[p].request_url,
            )
            for p in sorted(sources)
        ],
        total=counts[2],
        rows=rows_out,
    )
    return dict(
        **manifest,
        manifest_hash=digest(manifest),
        status="CONSISTENT_RESPONSE_COLLECTION",
        atomic_snapshot_verified=False,
        calendar_coverage_verified=False,
        limitations=[*LIMITATIONS, "PAGINATION_CONSISTENCY_IS_NOT_ATOMIC_SNAPSHOT_PROOF"],
    )
