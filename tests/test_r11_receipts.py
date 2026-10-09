"""Receipt contract adversarial tests, no remote CI claims or real observations."""

import hashlib
import json

import pytest

from investor_core.ledger import LedgerError
from investor_core.r11_receipts import original_json, verify_artifacts


def archive(body, name):
    raw = json.dumps(body)
    return dict(
        id=name,
        source_lineage=name,
        source_ref="https://github.com/Mr-Monologue/Agent_CompoundInterest_Plan/actions/runs/123",
        facts_json=json.dumps(
            dict(
                excerpt=raw,
                original_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                quality="OFFICIAL",
            )
        ),
    )


def engineering():
    names = [
        "full_regression",
        "ubuntu_ci",
        "windows_ci",
        "migration",
        "idempotency",
        "six_step_isolation",
    ]
    bodies = {
        name: dict(
            artifact_type=name,
            engine_hash="fixture-engine",
            dataset_kind="REAL",
            result="PASS",
            exit_code=0,
            passed=1,
            failed=0,
            unexpected_skips=0,
            command="isolated fixture",
            repository="Mr-Monologue/Agent_CompoundInterest_Plan",
            platform=name.removesuffix("_ci"),
            head_sha="a" * 40,
        )
        for name in names
    }
    return bodies


def test_engineering_requires_originals_for_every_declared_check():
    bodies = engineering()
    artifacts = {name: archive(value, name) for name, value in bodies.items()}
    result = verify_artifacts(
        dict(receipt_type="ENGINEERING"), dict(engine_hash="fixture-engine"), artifacts, [], None
    )
    assert result == artifacts
    # This is contract acceptance of fixtures, not CI execution or actual qualification.
    stored = json.loads(artifacts["ubuntu_ci"]["facts_json"])
    original = json.loads(stored["excerpt"])
    original["result"] = "FAIL"
    stored["excerpt"] = json.dumps(original)
    corrupt = dict(artifacts["ubuntu_ci"], facts_json=json.dumps(stored))
    with pytest.raises(LedgerError):
        original_json(corrupt)


@pytest.mark.parametrize(
    "field,value",
    [
        ("passed", True),
        ("exit_code", 1),
        ("unexpected_skips", 1),
        ("dataset_kind", "SYNTHETIC"),
        ("engine_hash", "other"),
        ("platform", "ubuntu"),
        ("head_sha", "b" * 40),
    ],
)
def test_failed_mismatched_or_synthetic_ci_receipt_cannot_pass(field, value):
    bodies = engineering()
    bodies["windows_ci"][field] = value
    artifacts = {name: archive(v, name) for name, v in bodies.items()}
    with pytest.raises(LedgerError):
        verify_artifacts(
            dict(receipt_type="ENGINEERING"),
            dict(engine_hash="fixture-engine"),
            artifacts,
            [],
            None,
        )


def test_independent_review_cannot_omit_recomputed_observation():
    names = ["source_independence", "source_value_reconciliation", "recomputed_observations"]
    artifacts = {
        name: archive(
            dict(
                artifact_type=name,
                engine_hash="fixture-engine",
                dataset_kind="REAL",
                result="PASS",
                observations=[],
                pairs=[],
            ),
            name,
        )
        for name in names
    }
    receipt = dict(
        receipt_type="INDEPENDENT_REVIEW",
        reviewed_forward_ids=["f1"],
        reviewed_history_ids=[],
        reviewer="reviewer",
    )
    with pytest.raises(LedgerError) as missing:
        verify_artifacts(
            receipt, dict(engine_hash="fixture-engine"), artifacts, [dict(id="f1")], None
        )
    assert missing.value.code == "R11_REVIEW_ARTIFACT_COVERAGE"
