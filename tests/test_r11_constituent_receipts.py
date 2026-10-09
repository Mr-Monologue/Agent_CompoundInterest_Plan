"""Independent review compares complete original collections, including their identities."""

import hashlib
import json

import pytest

from investor_core.ledger import LedgerError
from investor_core.r11_receipts import verify_artifacts
from investor_core.scheduler import digest


def archive(key, value):
    text = json.dumps(value)
    return dict(
        id=key,
        source_ref="https://example.test/" + key,
        source_lineage=key,
        facts_json=json.dumps(
            dict(
                excerpt=text,
                original_sha256=hashlib.sha256(text.encode()).hexdigest(),
                quality="OFFICIAL",
            )
        ),
    )


@pytest.mark.parametrize(
    "secondary_codes, valid",
    [
        (["B", "A"], True),
        (["A"], False),
        (["A", "B", "C"], False),
        (["A", "A"], False),
        (["A", "C"], False),
    ],
)
def test_independent_original_requires_whole_same_set(secondary_codes, valid):
    primary = archive("primary", {"all": ["A", "B"], "publisher": "primary"})
    secondary = archive("secondary", {"all": secondary_codes, "publisher": "secondary"})
    proof = archive(
        "proof",
        dict(
            primary_lineage="primary",
            secondary_lineage="secondary",
            independent_upstream=True,
            reviewer="SYNTHETIC",
        ),
    )
    originals = {row["id"]: row for row in [primary, secondary, proof]}
    output = dict(
        extraction=dict(
            status="MATCHED",
            matched=[
                dict(
                    path="/members",
                    source="official",
                    pointer="/all",
                    code_pointer="",
                    kind="CONSTITUENT_SET",
                )
            ],
        )
    )
    run = dict(
        id="synthetic-run",
        input={},
        output=output,
        output_hash=digest(output),
        evidence={"sources": {"official": primary}},
    )
    artifacts = {
        name: archive(
            name,
            dict(
                artifact_type=name,
                engine_hash="synthetic-engine",
                dataset_kind="SYNTHETIC",
                result="PASS",
                observations=[
                    dict(
                        run_id=run["id"],
                        input_hash=digest(run["input"]),
                        output_hash=run["output_hash"],
                        result="PASS",
                    )
                ],
                pairs=[
                    dict(
                        primary_id="primary",
                        secondary_id="secondary",
                        origin_review_id="proof",
                        fields="IDENTICAL_POINTERS",
                    )
                ],
            ),
        )
        for name in [
            "source_value_reconciliation",
            "recomputed_observations",
            "source_independence",
        ]
    }
    args = (
        dict(
            receipt_type="INDEPENDENT",
            reviewer="SYNTHETIC",
            reviewed_forward_ids=[run["id"]],
            reviewed_history_ids=[],
        ),
        dict(engine_hash="synthetic-engine", dataset_kind="SYNTHETIC"),
        artifacts,
        [run],
        originals.__getitem__,
    )
    if valid:
        assert "source:primary" in verify_artifacts(*args)
    else:
        with pytest.raises(LedgerError, match="Independent original values"):
            verify_artifacts(*args)


def test_unverified_collection_clears_breadth_and_season_without_smaller_denominator():
    from test_r11_calculators import macro

    from investor_core.r11_macro import MacroInput, calculate_macro
    from investor_core.r11_quality import apply_extraction

    calculated = calculate_macro(MacroInput.model_validate(macro()))
    assert calculated["dimension_scores"]["B"] is not None and calculated["axes"]
    restricted = apply_extraction(
        calculated,
        dict(
            status="UNVERIFIED",
            constituent_set=dict(status="UNVERIFIED", input_count=1, original_count=2),
        ),
        [],
    )
    assert restricted["dimension_scores"]["B"] is None
    assert restricted["axes"] == {}
    assert restricted["candidate_season"] == "UNKNOWN"
    assert restricted["breadth"]["total"] == 2
    assert restricted["breadth"]["eligible"] is None
    assert restricted["status"] == "INSUFFICIENT_DATA"
    assert "CONSTITUENT_SET_UNVERIFIED" in restricted["dimension_gaps"]["B"]
