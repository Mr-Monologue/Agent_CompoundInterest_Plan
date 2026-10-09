"""Versioned, bounded originals for research preparation, never evidence of eligibility.

The old v4 provenance and promotion readers intentionally do not consume this format.
An envelope proves preservation of supplied bytes, not publisher authentication.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from investor_core.execution import ExecutionService, SourceArchive, StrictModel

ENVELOPE_VERSION = "r11-source-bytes-v1"
MAX_RAW_BYTES = 2 * 1024 * 1024
MAX_BASE64_CHARS = 4 * ((MAX_RAW_BYTES + 2) // 3)
MAX_JSON_NODES = 100_000
MAX_JSON_DEPTH = 32


class OriginalEnvelope(StrictModel):
    version: Literal["r11-source-bytes-v1"] = "r11-source-bytes-v1"
    content_base64: str = Field(min_length=4, max_length=MAX_BASE64_CHARS)
    byte_length: int = Field(ge=1, le=MAX_RAW_BYTES, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: Literal["application/json", "text/html", "application/xlsx", "application/pdf"]
    charset: Literal["utf-8", "gb18030"] | None = None
    request_url: str = Field(max_length=1000)
    final_url: str = Field(max_length=1000)
    first_retrieved_at: datetime
    publication_text: str | None = Field(default=None, max_length=200)
    publication_precision: Literal["INSTANT", "DATE", "UNKNOWN"] = "UNKNOWN"
    publication_timezone: str | None = None
    publisher_revision: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def valid(self) -> OriginalEnvelope:
        for url in (self.request_url, self.final_url):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("public HTTPS URL without credentials required")
        if self.first_retrieved_at.tzinfo is None:
            raise ValueError("actual retrieval requires timezone")
        if self.publication_timezone is not None:
            ZoneInfo(self.publication_timezone)
        if self.publication_precision != "UNKNOWN" and not self.publication_text:
            raise ValueError("publication evidence text required")
        if self.media_type in {"application/json", "text/html"} and self.charset is None:
            raise ValueError("explicit text encoding required")
        self.raw_bytes()
        return self

    def raw_bytes(self) -> bytes:
        try:
            raw = base64.b64decode(self.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid original encoding") from exc
        if (
            len(raw) != self.byte_length
            or hashlib.sha256(raw).hexdigest() != self.sha256
            or base64.b64encode(raw).decode("ascii") != self.content_base64
        ):
            raise ValueError("original size/hash/encoding mismatch")
        return raw


def envelope(raw: bytes, **metadata: Any) -> OriginalEnvelope:
    if not 0 < len(raw) <= MAX_RAW_BYTES:
        raise ValueError("original exceeds resource limit or is empty; never truncate")
    return OriginalEnvelope(
        content_base64=base64.b64encode(raw).decode("ascii"),
        byte_length=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        **metadata,
    )


def read_envelope(body: dict[str, Any]) -> OriginalEnvelope:
    original = OriginalEnvelope.model_validate(body["facts"]["r11_original"])
    if (
        body.get("original_sha256") != original.sha256
        or body.get("source_ref") != original.request_url
        or datetime.fromisoformat(body["retrieved_at"]) != original.first_retrieved_at
    ):
        raise ValueError("envelope/archive binding mismatch")
    return original


def archive_original(service: ExecutionService, request: SourceArchive) -> dict[str, Any]:
    """Use existing immutable evidence storage; reuse the earliest identical capture.

    Only retrieval times may differ. Publication, quality, revision and payload changes
    are new evidence. Repeated capture cannot move the historical first-seen time.
    """
    read_envelope(request.model_dump(mode="json", exclude={"actor_ref"}))
    return service.archive(request)


def capture_identity(body: dict[str, Any]) -> dict[str, Any]:
    """Validated identity for the existing transaction's atomic deduplication."""
    read_envelope(body)
    result: dict[str, Any] = json.loads(json.dumps(body))
    result.pop("retrieved_at", None)
    result["facts"]["r11_original"].pop("first_retrieved_at", None)
    return result


def native_json(original: OriginalEnvelope) -> Any:
    if original.media_type != "application/json" or original.charset != "utf-8":
        raise ValueError("native UTF-8 JSON required")
    text = original.raw_bytes().decode("utf-8", errors="strict")
    # Bound nesting before invoking the recursive stdlib decoder. Strings are skipped.
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON depth exceeds resource limit")
        elif char in "]}":
            depth -= 1

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate original JSON key")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise ValueError("nonfinite JSON value: " + value)

    document = json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    pending = [document]
    nodes = 0
    while pending:
        item = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise ValueError("JSON node count exceeds resource limit")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, float):
            import math

            if not math.isfinite(item):
                raise ValueError("nonfinite JSON numeric value")
    return document
