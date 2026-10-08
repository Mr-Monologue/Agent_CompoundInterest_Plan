"""Reconcile immutable input leaves to exact archived machine-readable originals.

This proves extraction equality, not publisher identity or upstream independence.
Unsupported PDF/HTML extraction stays explicitly unverified; no guessed values.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from investor_core.r11_legacy.v3.inputs import Context
from investor_core.scheduler import digest


def leaves(value: Any, path: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if path == "" and key == "context":
                continue
            if key == "source" or key.endswith("_source"):
                continue
            escaped = key.replace("~", "~0").replace("/", "~1")
            result.update(leaves(child, path + "/" + escaped))
        return result
    if isinstance(value, list) and value:
        return {
            k: v for i, child in enumerate(value) for k, v in leaves(child, f"{path}/{i}").items()
        }
    return {path: value}


def pointer(document: Any, path: str) -> Any:
    if path == "":
        return document
    if not path.startswith("/"):
        raise ValueError("RFC6901 absolute pointer required")
    for part in path[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(document, list):
            if not key.isdigit() or str(int(key)) != key:
                raise ValueError("canonical nonnegative list index required")
            document = document[int(key)]
        elif isinstance(document, dict):
            document = document[key]
        else:
            raise ValueError("pointer traverses a scalar")
    return document


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate original JSON key")
        obj[key] = value
    return obj


def declared_source(inputs: dict[str, Any], path: str) -> tuple[set[str] | None, str | None]:
    """Resolve the actual field's declared source, not an unrelated bundle source."""
    node: Any = inputs
    expected: set[str] | None = None
    account = None
    roles = {
        "index_identity": "identity_source",
        "constituents_date": "constituents_source",
        "members": "constituents_source",
        "listed_on": "listing_source",
        "manager_team_since": "manager_source",
        "held_since": "holding_source",
        "thesis": "thesis_source",
        "thesis_as_of": "thesis_source",
        "dividend_coverage_from": "dividend_coverage_source",
        "dividend_coverage_to": "dividend_coverage_source",
        "subscription_confirmation_max_trading_days": "delay_source",
        "redemption_arrival_max_trading_days": "delay_source",
        "benchmark_identity": "benchmark_mapping_source",
        "benchmark_currency": "benchmark_mapping_source",
        "benchmark_return_basis": "benchmark_mapping_source",
        "benchmark_effective_from": "benchmark_mapping_source",
        "benchmark_effective_to": "benchmark_mapping_source",
        "subscription_days": "calendar_source",
        "redemption_days": "calendar_source",
        "calendar_from": "calendar_source",
        "calendar_to": "calendar_source",
    }
    for escaped in path[1:].split("/"):
        key = escaped.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if node.get("account_ref"):
                account = node["account_ref"]
            declared = (
                node.get(roles.get(key, "")) or node.get("source") or node.get("facts_source")
            )
            if declared:
                expected = {declared}
            elif key in {"identity", "unit"} and isinstance(node.get("points"), list):
                expected = {p["source"] for p in node["points"] if p.get("source")}
            node = node[key]
        else:
            node = node[int(key)]
    return expected, account


def reconcile(
    inputs: dict[str, Any], evidence: dict[str, Any], bindings: dict[str, Any]
) -> dict[str, Any]:
    """Every value, date, unit and identity must match an explicit source pointer.

    No substring search, rounding, coercion, interpolation, or caller PASS flag.
    The whole UTF-8 original must be preserved, not a hash of a selected excerpt.
    """
    required = leaves(inputs)
    context = Context.model_validate(inputs["context"]) if "context" in inputs else None
    declared_paths = set(required)
    not_selected = []
    if context is not None and context.evidence_class != "H":
        for name in ("pmi", "social_financing_yoy"):
            for index, point in enumerate(inputs.get(name, {}).get("points", [])):
                source = context.sources.get(point.get("source"))
                if source is not None and source.known_at() > context.as_of:
                    prefix = f"/{name}/points/{index}/"
                    for path in list(required):
                        if path.startswith(prefix):
                            required.pop(path)
                            not_selected.append(
                                dict(path=path, reason="UNPUBLISHED_MONTH_NOT_SELECTED")
                            )
    failures = []
    matched = []
    documents = {}
    for key, row in evidence.items():
        try:
            archive = json.loads(row["facts_json"])
            text = archive["excerpt"]
            if hashlib.sha256(text.encode()).hexdigest() != archive["original_sha256"]:
                raise ValueError("original bytes unavailable")
            if archive.get("facts", {}).get("r11_original_format") != "JSON_UTF8":
                raise ValueError("unsupported original format")
            documents[key] = json.loads(text, object_pairs_hook=_unique)
        except (KeyError, ValueError, TypeError):
            continue
    for path, value in required.items():
        binding = bindings.get(path)
        if (
            not isinstance(binding, dict)
            or set(binding) != {"source", "pointer"}
            or not isinstance(binding.get("source"), str)
            or not isinstance(binding.get("pointer"), str)
        ):
            failures.append(dict(path=path, reason="EXTRACTION_BINDING_MISSING"))
            continue
        source = binding["source"]
        if context is not None:
            expected, account_ref = declared_source(inputs, path)
            problems = context.source_gaps(source, account_ref=account_ref)
            if expected is not None and source not in expected:
                problems.append("FIELD_SOURCE_BINDING_MISMATCH")
            if problems:
                failures.append(dict(path=path, reason="|".join(sorted(set(problems)))))
                continue
        try:
            extracted = pointer(documents[source], binding["pointer"])
            # JSON's bool/int equality is intentionally not accepted.
            if digest(extracted) != digest(value):
                raise ValueError("value differs")
        except (KeyError, ValueError, TypeError, IndexError):
            failures.append(dict(path=path, reason="ORIGINAL_VALUE_NOT_VERIFIED"))
        else:
            matched.append(dict(path=path, source=source, pointer=binding["pointer"]))
    if set(bindings) - declared_paths:
        failures.append(dict(path="", reason="EXTRACTION_BINDING_OUT_OF_SCOPE"))
    return dict(
        status="MATCHED" if not failures else "UNVERIFIED",
        expected_count=len(required),
        matched_count=len(matched),
        failures=failures,
        matched=matched,
        not_selected=not_selected,
        independent_upstream_pass=False,
        limitations=["EXTRACTION_EQUALITY_IS_NOT_PUBLISHER_OR_INDEPENDENCE_VERIFICATION"],
    )


def publication_matches(source: Any, archived: dict[str, Any]) -> bool:
    """Date-only archives cannot manufacture an exact timestamp via nested claims."""
    if (
        archived.get("published_date")
        != source.published_at.astimezone(ZoneInfo(source.publication_timezone)).date().isoformat()
    ):
        return False
    if source.publication_precision == "DATE":
        return True
    # INSTANT is supported only when the exact original actually contains it.
    try:
        text = archived["excerpt"]
        if hashlib.sha256(text.encode()).hexdigest() != archived["original_sha256"]:
            return False
        facts = archived["facts"]
        if facts.get("r11_original_format") != "JSON_UTF8":
            return False
        raw = pointer(json.loads(text, object_pairs_hook=_unique), facts["r11_publication_pointer"])
        timestamp = datetime.fromisoformat(raw)
        return timestamp.tzinfo is not None and timestamp == source.published_at
    except (KeyError, TypeError, ValueError, IndexError):
        return False
