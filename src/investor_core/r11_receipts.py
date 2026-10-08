"""Structural and numerical checks on accountable external review originals.

Archive ingestion remains the existing trusted local research boundary. These
checks do not authenticate a remote publisher or infer independence from domains.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from investor_core.ledger import LedgerError
from investor_core.r11_provenance import pointer
from investor_core.scheduler import digest


def original_json(archive: dict[str, Any]) -> dict[str, Any]:
    body = json.loads(archive["facts_json"])
    text = body.get("excerpt", "")
    if (
        not text
        or hashlib.sha256(text.encode()).hexdigest() != body.get("original_sha256")
        or body.get("quality") not in {"OFFICIAL", "ACCOUNT_OBSERVATION"}
    ):
        raise LedgerError(
            "R11_ARTIFACT_ORIGINAL_REQUIRED", "Exact complete artifact original required"
        )
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise LedgerError(
            "R11_ARTIFACT_FORMAT_UNSUPPORTED", "Native JSON artifact required"
        ) from exc
    if not isinstance(value, dict):
        raise LedgerError("R11_ARTIFACT_INVALID", "Structured artifact object required")
    return value


def verify_artifacts(
    receipt: dict[str, Any],
    report: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
    runs: list[dict[str, Any]],
    lookup: Any,
) -> dict[str, dict[str, Any]]:
    # Save every dependency, not just the top-level reviewer statement.
    dependencies = dict(artifacts)
    values = {name: original_json(row) for name, row in artifacts.items()}
    for name, value in values.items():
        if (
            value.get("artifact_type") != name
            or value.get("engine_hash") != report["engine_hash"]
            or value.get("dataset_kind") != "REAL"
            or value.get("result") != "PASS"
        ):
            raise LedgerError(
                "R11_ARTIFACT_SCOPE_MISMATCH", "Artifact type/code/result/class mismatch"
            )
    if receipt["receipt_type"] == "ENGINEERING":
        for name, value in values.items():
            if (
                value.get("exit_code") != 0
                or isinstance(value.get("exit_code"), bool)
                or not isinstance(value.get("passed"), int)
                or isinstance(value["passed"], bool)
                or value["passed"] < 1
                or value.get("failed") != 0
                or value.get("unexpected_skips") != 0
                or not value.get("command")
            ):
                raise LedgerError(
                    "R11_ENGINEERING_ARTIFACT_INCOMPLETE",
                    "Actual check counts/command/exit required",
                )
            if name in {"ubuntu_ci", "windows_ci"} and (
                value.get("repository") != "Mr-Monologue/Agent_CompoundInterest_Plan"
                or value.get("platform") != name.removesuffix("_ci")
                or not value.get("head_sha")
                or len(value["head_sha"]) != 40
                or not artifacts[name]["source_ref"].startswith(
                    "https://github.com/Mr-Monologue/Agent_CompoundInterest_Plan/actions/runs/"
                )
            ):
                raise LedgerError(
                    "R11_CI_ARTIFACT_MISMATCH",
                    "Exact repository, platform and run evidence required",
                )
        if values["ubuntu_ci"]["head_sha"] != values["windows_ci"]["head_sha"]:
            raise LedgerError(
                "R11_CI_COMMIT_MISMATCH", "Both platforms must verify the same source commit"
            )
        return dependencies
    checked = set(receipt["reviewed_forward_ids"] + receipt["reviewed_history_ids"])
    by_id = {r["id"]: r for r in runs}
    if not checked <= by_id.keys():
        raise LedgerError("R11_REVIEW_RUN_MISSING", "Reviewed observations must exist")
    for name in ("source_value_reconciliation", "recomputed_observations"):
        records = values[name].get("observations", [])
        if not isinstance(records, list) or len(records) != len(checked):
            raise LedgerError(
                "R11_REVIEW_ARTIFACT_COVERAGE", "One exact result for each reviewed observation"
            )
        if {r.get("run_id") for r in records} != checked:
            raise LedgerError("R11_REVIEW_ARTIFACT_COVERAGE", "Missing or duplicate recomputation")
        for item in records:
            run = by_id[item["run_id"]]
            if (
                item.get("input_hash") != digest(run["input"])
                or item.get("output_hash") != run["output_hash"]
                or item.get("result") != "PASS"
            ):
                raise LedgerError(
                    "R11_REVIEW_RECOMPUTATION_MISMATCH", "Independent input/output differs"
                )
    required: dict[str, set[str]] = {}
    for run_id in checked:
        run = by_id[run_id]
        extraction = run["output"].get("extraction", {})
        if extraction.get("status") != "MATCHED":
            raise LedgerError(
                "R11_REVIEW_EXTRACTION_MISSING", "Every reviewed original must reconcile"
            )
        for match in extraction["matched"]:
            source = run["evidence"]["sources"][match["source"]]
            required.setdefault(source["id"], set()).add(match["pointer"])
    pairs = values["source_independence"].get("pairs", [])
    if (
        not isinstance(pairs, list)
        or len(pairs) != len(required)
        or {p.get("primary_id") for p in pairs} != required.keys()
    ):
        raise LedgerError("R11_INDEPENDENT_SOURCE_COVERAGE", "Cover each used primary source")
    for pair in pairs:
        primary, secondary, proof = [
            lookup(pair[name]) for name in ("primary_id", "secondary_id", "origin_review_id")
        ]
        for original in (primary, secondary, proof):
            dependencies["source:" + original["id"]] = original
        a, b, review = original_json(primary), original_json(secondary), original_json(proof)
        if (
            primary["source_lineage"] == secondary["source_lineage"]
            or primary["source_ref"] == secondary["source_ref"]
            or digest(a) == digest(b)
            or review.get("primary_lineage") != primary["source_lineage"]
            or review.get("secondary_lineage") != secondary["source_lineage"]
            or review.get("independent_upstream") is not True
            or review.get("reviewer") != receipt["reviewer"]
        ):
            raise LedgerError(
                "R11_UPSTREAM_INDEPENDENCE_UNPROVEN",
                "Different URLs alone are not independent origins",
            )
        fields = pair.get("fields", {})
        if not isinstance(fields, dict) or set(fields) != required[primary["id"]]:
            raise LedgerError(
                "R11_INDEPENDENT_VALUE_COVERAGE",
                "Reconcile every used value, date, unit and identity",
            )
        try:
            if any(
                digest(pointer(a, left)) != digest(pointer(b, right))
                for left, right in fields.items()
            ):
                raise ValueError("different values")
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise LedgerError(
                "R11_INDEPENDENT_VALUES_DIFFER", "Independent original values do not agree"
            ) from exc
    return dependencies
