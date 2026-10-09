"""Full original collection binding; neither a submitted count nor selected leaves."""

from __future__ import annotations

from typing import Any


def codes_from_collection(document: Any, code_pointer: str) -> list[str]:
    from investor_core.r11_provenance import pointer

    if not isinstance(document, list) or not document:
        raise ValueError("complete nonempty constituent array required")
    codes = [pointer(item, code_pointer) for item in document]
    if any(not isinstance(code, str) or not code.strip() for code in codes):
        raise ValueError("constituent identities must be nonempty strings")
    if len(set(codes)) != len(codes):
        raise ValueError("duplicate constituent identity in original collection")
    return sorted(codes)


def reconcile_constituents(
    inputs: dict[str, Any],
    documents: dict[str, Any],
    binding: Any,
    context: Any,
    declarations: dict[str, Any],
) -> dict[str, Any]:
    from investor_core.r11_provenance import pointer

    result: dict[str, Any] = dict(
        status="UNVERIFIED",
        input_count=len(inputs["members"]),
        original_count=None,
        missing_codes=[],
        added_codes=[],
        failures=[],
    )
    if (
        not isinstance(binding, dict)
        or set(binding) != {"source", "pointer", "code_pointer"}
        or any(not isinstance(value, str) for value in binding.values())
    ):
        result["failures"] = ["COMPLETE_CONSTITUENT_COLLECTION_BINDING_REQUIRED"]
        return result
    source = binding["source"]
    result.update(source=source, pointer=binding["pointer"], code_pointer=binding["code_pointer"])
    if context is None or source != inputs["constituents_source"]:
        result["failures"] = ["CONSTITUENT_COLLECTION_SOURCE_MISMATCH"]
        return result
    expected = {key: binding[key] for key in ("pointer", "code_pointer")}
    if declarations.get(source) != expected:
        result["failures"] = ["ARCHIVED_COMPLETE_COLLECTION_DECLARATION_MISMATCH"]
        return result
    result["failures"] = context.source_gaps(source)
    if result["failures"]:
        return result
    try:
        original = codes_from_collection(
            pointer(documents[source], binding["pointer"]), binding["code_pointer"]
        )
        submitted = codes_from_collection(inputs["members"], "/code")
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        result["failures"] = ["CONSTITUENT_COLLECTION_INVALID:" + str(exc)]
        return result
    result.update(
        original_count=len(original),
        missing_codes=sorted(set(original) - set(submitted)),
        added_codes=sorted(set(submitted) - set(original)),
    )
    if original != submitted:
        result["failures"] = ["CONSTITUENT_SET_DIFFERS_FROM_COMPLETE_ORIGINAL"]
    else:
        result["status"] = "MATCHED"
    return result
