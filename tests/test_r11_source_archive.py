"""Bounded byte preservation, collection integrity and old trust-boundary isolation."""

import base64
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from investor_core.execution import ExecutionService, SourceArchive
from investor_core.ledger import LedgerError
from investor_core.r11_provenance import reconcile
from investor_core.r11_receipts import original_json
from investor_core.r11_source_archive import (
    MAX_RAW_BYTES,
    OriginalEnvelope,
    archive_original,
    envelope,
    native_json,
    read_envelope,
)
from investor_core.r11_source_json import extract_json, fullgoal_nav_collection

URL = "https://www.fullgoal.com.cn/ws-business-server/fund/getFundNavPage"
TIME = datetime(2026, 10, 9, 5, tzinfo=UTC)


def original(raw=b'{"value":"1.25"}', **kwargs):
    args = dict(
        media_type="application/json",
        charset="utf-8",
        request_url="https://example.test/",
        final_url="https://example.test/",
        first_retrieved_at=TIME,
    )
    args.update(kwargs)
    return envelope(raw, **args)


def request(value):
    return SourceArchive(
        instrument_code="CORE01",
        source_name="SYNTHETIC ONLY",
        source_ref=value.request_url,
        source_lineage="SYNTHETIC",
        retrieved_at=value.first_retrieved_at,
        published_date=None,
        data_date="2026-10-08",
        excerpt="Display summary; not the original",
        original_sha256=value.sha256,
        quality="UNVERIFIED",
        facts={"r11_original": value.model_dump(mode="json"), "dataset_kind": "SYNTHETIC"},
    )


def test_long_original_roundtrip_and_legacy_does_not_upgrade():
    raw = json.dumps({"value": "1.25", "padding": "x" * 140000}).encode()
    item = original(raw)
    body = request(item).model_dump(mode="json")
    assert read_envelope(body).raw_bytes() == raw
    assert extract_json(item, "/value")["value"] == "1.25"
    archive = {"facts_json": json.dumps(body)}
    result = reconcile(
        {"value": "1.25"}, {"s": archive}, {"/value": {"source": "s", "pointer": "/value"}}
    )
    assert result["status"] == "UNVERIFIED"
    with pytest.raises(LedgerError, match="complete artifact original"):
        original_json(archive)


@pytest.mark.parametrize("change", ["hash", "length", "base64", "version"])
def test_tampered_original_rejected(change):
    body = original().model_dump(mode="json")
    if change == "hash":
        body["sha256"] = "0" * 64
    if change == "length":
        body["byte_length"] += 1
    if change == "base64":
        body["content_base64"] = base64.b64encode(b"other").decode()
    if change == "version":
        body["version"] = "future"
    with pytest.raises(ValueError):
        OriginalEnvelope.model_validate(body)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"[" * 33 + b"0" + b"]" * 33,
        b'"\xff"',
        b"[" + b"0," * 100001 + b"0]",
    ],
    ids=["duplicate-key", "NaN", "overflow", "depth", "encoding", "nodes"],
)
def test_unsafe_json_rejected(raw):
    with pytest.raises(ValueError):
        native_json(original(raw))


def test_resource_limit_no_truncation():
    with pytest.raises(ValueError, match="never truncate"):
        original(b"x" * (MAX_RAW_BYTES + 1))
    with pytest.raises(ValueError):
        original(b"")
    with pytest.raises(ValueError):
        original(first_retrieved_at=TIME.replace(tzinfo=None))


def test_archive_binding_rejected():
    body = request(original()).model_dump(mode="json")
    body["retrieved_at"] = (TIME + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="binding"):
        read_envelope(body)


def test_first_capture_reused_and_revision_retained(tmp_path):
    from test_planning import configured_services

    from investor_core.research import ResearchService

    _, planning, _, _ = configured_services(tmp_path / "isolated.db")
    service = ExecutionService(ResearchService(planning.settings))
    first = archive_original(service, request(original()))
    repeated = archive_original(
        service, request(original(first_retrieved_at=TIME + timedelta(days=1)))
    )
    assert repeated["id"] == first["id"] and repeated["idempotent_replay"]
    assert repeated["facts"]["retrieved_at"] == first["facts"]["retrieved_at"]
    assert read_envelope(repeated["facts"]).first_retrieved_at == TIME
    revision = archive_original(service, request(original(b'{"value":"1.26"}')))
    assert revision["id"] != first["id"]
    with pytest.raises(ValueError, match="backdate"):
        archive_original(service, request(original(first_retrieved_at=TIME - timedelta(days=1))))
    from concurrent.futures import ThreadPoolExecutor

    concurrent = request(original(b'{"value":"1.27"}'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        saves = list(pool.map(lambda _: archive_original(service, concurrent), range(4)))
    assert len({save["id"] for save in saves}) == 1


def page(number=1, size=2, total=3, rows=None, **changes):
    rows = (
        rows
        if rows is not None
        else (
            [
                {"navDate": "2026-10-08", "productCode": "022463", "relatePrice": "1.25"},
                {"navDate": "2026-10-07", "productCode": "022463", "relatePrice": "1.24"},
            ]
            if number == 1
            else [
                {"navDate": "2026-10-06", "productCode": "022463", "relatePrice": "1.23"},
            ]
        )
    )
    data = dict(
        pageNum=number, pageSize=size, pages=(total + size - 1) // size, total=total, list=rows
    )
    data.update(changes)
    url = (
        f"{URL}?productCode=022463&pageNum={number}&pageSize={size}"
        "&startDate=&endDate=&isPreview=&siteno=main"
    )
    return original(json.dumps(dict(code=0, data=data)).encode(), request_url=url, final_url=url)


def test_complete_pagination_binds_each_native_page():
    originals = [page(), page(2)]
    result = fullgoal_nav_collection(originals)
    assert result["total"] == 3
    assert len(result["rows"]) == 3
    assert result["rows"][-1]["original_sha256"] == originals[1].sha256
    assert result["rows"][-1]["value_pointer"] == "/data/list/0/relatePrice"
    assert not result["atomic_snapshot_verified"]
    assert not result["calendar_coverage_verified"]


@pytest.mark.parametrize("kind", ["missing", "repeat", "drift", "duplicate_day", "identity", "nav"])
def test_pagination_negative_cases(kind):
    pages = [page(), page(2)]
    if kind == "missing":
        pages.pop()
    if kind == "repeat":
        pages[1] = pages[0]
    if kind == "drift":
        pages[1] = page(2, total=4)
    if kind in {"duplicate_day", "identity", "nav"}:
        row = copy.deepcopy(native_json(pages[1])["data"]["list"])
        row[0][
            {"duplicate_day": "navDate", "identity": "productCode", "nav": "relatePrice"}[kind]
        ] = {"duplicate_day": "2026-10-08", "identity": "different", "nav": "NaN"}[kind]
        pages[1] = page(2, rows=row)
    with pytest.raises(ValueError):
        fullgoal_nav_collection(pages)
