"""A source's complete constituent set must not be reduced before breadth scoring."""

import hashlib
import json
from copy import deepcopy

import pytest
from test_r11_calculators import macro
from test_r11_second_review import new_bundle
from test_r11_service import archive_bundle, isolated  # noqa: F401

from investor_core.execution import ExecutionService, SourceArchive
from investor_core.r11_service import R11Request


def test_omitting_an_archived_constituent_cannot_improve_breadth(isolated):  # noqa: F811
    _, _, service, _ = isolated
    body = macro()
    other = deepcopy(body["members"][0])
    other["code"] = "SYNTHETIC_DECLINING"
    other["wealth"]["identity"] = other["code"]
    for i, point in enumerate(other["wealth"]["points"]):
        point["value"] = str(60 - i)
    body["members"].append(other)
    eid = archive_bundle(service, body)
    with service.research._connect() as c:
        archived = json.loads(
            service._evidence(c, body["context"]["sources"]["official"]["archive_id"])["facts_json"]
        )
    document = json.loads(archived["excerpt"])
    document["complete_constituents"] = [member["code"] for member in body["members"]]
    archived["excerpt"] = json.dumps(document, separators=(",", ":"))
    archived["original_sha256"] = hashlib.sha256(archived["excerpt"].encode()).hexdigest()
    archived["source_ref"] += "/complete-constituents"
    source = ExecutionService(service.research).archive(
        SourceArchive.model_validate(
            {k: v for k, v in archived.items() if k in SourceArchive.model_fields}
        )
    )

    def full_source(facts):
        metadata = facts["r11_input"]["context"]["sources"]["official"]
        metadata.update(
            archive_id=source["id"],
            url=archived["source_ref"],
            document_hash=archived["original_sha256"],
        )

    complete = new_bundle(service, eid, "complete", full_source)
    full = service.observe(
        R11Request(method="C", bundle_evidence_id=complete, idempotency_key="full")
    )
    assert full["output"]["status"] == "CALCULATED_SHADOW"
    assert full["output"]["breadth"]["total"] == 2

    def omit(facts):
        facts["r11_input"]["members"].pop()
        facts["r11_bindings"] = {
            k: v for k, v in facts["r11_bindings"].items() if not k.startswith("/members/1/")
        }

    incomplete = new_bundle(service, complete, "omitted", omit)
    selected = service.observe(
        R11Request(
            method="C",
            bundle_evidence_id=incomplete,
            idempotency_key="selected",
            supersedes=full["id"],
        )
    )
    print(
        "FULL",
        full["output"]["status"],
        full["output"]["breadth"],
        full["output"]["dimension_scores"],
    )
    print(
        "OMITTED",
        selected["output"]["status"],
        selected["output"]["breadth"],
        selected["output"]["dimension_scores"],
        selected["output"]["extraction"]["status"],
    )
    assert selected["output"]["status"] != "CALCULATED_SHADOW"


def collection_case(codes, original=None):
    from test_r11_calculators import fixture_context

    from investor_core.r11_inputs import Context

    inputs = dict(members=[dict(code=code) for code in codes], constituents_source="official")
    document = {"all": original if original is not None else ["A", "B"]}
    binding = dict(source="official", pointer="/all", code_pointer="")
    return (
        inputs,
        {"official": document},
        binding,
        Context.model_validate(fixture_context()),
        {"official": {"pointer": "/all", "code_pointer": ""}},
    )


@pytest.mark.parametrize("codes", [["A"], ["A", "B", "C"], ["A", "A"], ["A", "C"]])
def test_membership_mutations_fail_against_complete_original(codes):
    from investor_core.r11_constituents import reconcile_constituents

    result = reconcile_constituents(*collection_case(codes))
    assert result["status"] == "UNVERIFIED"


@pytest.mark.parametrize("original", [["A", "A"], [], ["A", None], ["A", " "]])
def test_invalid_original_collection_is_not_a_denominator(original):
    from investor_core.r11_constituents import reconcile_constituents

    result = reconcile_constituents(*collection_case(["A", "B"], original))
    assert result["status"] == "UNVERIFIED"
    assert result["original_count"] is None


@pytest.mark.parametrize(
    "binding",
    [
        None,
        {"total": 2},
        dict(source="other", pointer="/all", code_pointer=""),
        dict(source="official", pointer="/all/0", code_pointer=""),
        dict(source="official", pointer="/all", code_pointer="/missing"),
    ],
)
def test_count_claim_or_invalid_collection_binding_does_not_verify(binding):
    from investor_core.r11_constituents import reconcile_constituents

    args = list(collection_case(["A", "B"]))
    args[2] = binding
    assert reconcile_constituents(*args)["status"] == "UNVERIFIED"


def test_order_independent_complete_object_collection():
    from investor_core.r11_constituents import reconcile_constituents

    inputs, documents, binding, context, declarations = collection_case(
        ["B", "A"], [{"code": "A"}, {"code": "B"}]
    )
    binding["code_pointer"] = "/code"
    declarations["official"]["code_pointer"] = "/code"
    first = reconcile_constituents(inputs, documents, binding, context, declarations)
    inputs["members"].reverse()
    documents["official"]["all"].reverse()
    assert reconcile_constituents(inputs, documents, binding, context, declarations) == first
    assert first["status"] == "MATCHED" and first["original_count"] == 2


@pytest.mark.parametrize("count, calculated", [(10, True), (2, False)])
def test_legitimate_new_listing_keeps_full_denominator(count, calculated):
    from investor_core.r11_constituents import reconcile_constituents
    from investor_core.r11_inputs import Context
    from investor_core.r11_macro import MacroInput, calculate_macro
    from investor_core.r11_quality import apply_extraction

    body = macro()
    member = body["members"][0]
    body["members"] = [deepcopy(member) for _ in range(count)]
    for index, row in enumerate(body["members"]):
        row["code"] = row["wealth"]["identity"] = f"SYNTH_{index}"
    body["members"][-1]["listed_on"] = body["calendar"]["dates"][-5]
    codes = [row["code"] for row in body["members"]]
    collection = reconcile_constituents(
        body,
        {"official": codes},
        dict(source="official", pointer="", code_pointer=""),
        Context.model_validate(body["context"]),
        {"official": {"pointer": "", "code_pointer": ""}},
    )
    output = apply_extraction(
        calculate_macro(MacroInput.model_validate(body)),
        dict(status="MATCHED", constituent_set=collection),
        [],
    )
    assert output["breadth"]["total"] == count
    assert output["breadth"]["eligible"] == count - 1
    assert output["breadth"]["new_listing_exclusions"] == [codes[-1]]
    assert (output["status"] == "CALCULATED_SHADOW") is calculated
    assert ("BREADTH_COVERAGE_BELOW_90_PERCENT" in output["dimension_gaps"]["B"]) is not calculated


@pytest.mark.parametrize("method", ["C", "D"])
def test_valid_v3_snapshot_replays_immutably_but_cannot_qualify_current(method):
    from test_r11_calculators import candidates

    from investor_core.r11_inputs import DEFINITION
    from investor_core.r11_legacy.v3.candidates import CandidateInput, calculate_candidates
    from investor_core.r11_legacy.v3.macro import MacroInput, calculate_macro
    from investor_core.r11_legacy.v3.provenance import leaves, reconcile
    from investor_core.r11_legacy.v3.quality import apply_extraction
    from investor_core.r11_service import R11Service
    from investor_core.scheduler import digest

    data = (
        MacroInput.model_validate(macro())
        if method == "C"
        else CandidateInput.model_validate(candidates())
    )
    inputs = data.model_dump(mode="json")
    values = leaves(inputs)
    original = json.dumps(list(values.values()))
    sources = {
        "official": {
            "facts_json": json.dumps(
                dict(
                    excerpt=original,
                    original_sha256=hashlib.sha256(original.encode()).hexdigest(),
                    facts={"r11_original_format": "JSON_UTF8"},
                )
            )
        }
    }
    bindings = {path: dict(source="official", pointer=f"/{i}") for i, path in enumerate(values)}
    extraction = reconcile(inputs, sources, bindings)
    assert extraction["status"] == "MATCHED"
    output = calculate_macro(data) if method == "C" else calculate_candidates(data)
    output = apply_extraction(output, extraction, [])
    assert output["status"] == "CALCULATED_SHADOW"
    evidence = dict(
        sources=sources, bundle={"facts_json": json.dumps({"facts": {"r11_bindings": bindings}})}
    )
    record = dict(
        id="synthetic-v3",
        input=inputs,
        previous_outputs=[],
        output=output,
        output_hash=digest(output),
        evidence=evidence,
        evidence_hash=digest(evidence),
        definition_hash=digest(DEFINITION),
    )
    saved = deepcopy(record)
    replay = R11Service.replay_record(record)
    assert replay["result"] == "PASS"
    assert replay["historical_evaluator"] and not replay["qualifies_current_version"]
    assert record == saved


def test_bundle_cannot_redirect_to_selected_subcollection_in_same_original():
    from investor_core.r11_constituents import reconcile_constituents

    inputs, documents, binding, context, declarations = collection_case(["A"])
    documents["official"]["selected"] = ["A"]
    binding["pointer"] = "/selected"
    result = reconcile_constituents(inputs, documents, binding, context, declarations)
    assert result["status"] == "UNVERIFIED"
    assert result["failures"] == ["ARCHIVED_COMPLETE_COLLECTION_DECLARATION_MISMATCH"]
