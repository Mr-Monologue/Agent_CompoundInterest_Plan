"""One extraction failure policy for live runs, replay and sensitivity history."""

from __future__ import annotations

from typing import Any

from investor_core.r11_macro import stable_state
from investor_core.r11_rules import rules


def apply_extraction(
    output: dict[str, Any],
    extraction: dict[str, Any],
    history: list[dict[str, Any]],
    diagnostic_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output["extraction"] = extraction
    if extraction["status"] != "MATCHED":
        output["gaps"] = sorted(set(output["gaps"] + ["SOURCE_VALUE_BINDING_UNVERIFIED"]))
        output["status"] = "INSUFFICIENT_DATA"
        if output["method"] == "C":
            output["candidate_season"] = "UNKNOWN"
            output.update(stable_state(output, history, diagnostic_rules or rules("C")))
        else:
            output.update(
                leader=None, leading_weeks=0, leading_score_history=[], replacement="BLOCKED"
            )
            output["replacement_blockers"] = sorted(
                set(output["replacement_blockers"] + ["SOURCE_VALUE_BINDING_UNVERIFIED"])
            )
            for row in output["rows"]:
                row["rank"] = None
    return output
