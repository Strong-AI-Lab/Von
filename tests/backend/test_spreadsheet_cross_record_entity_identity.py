from __future__ import annotations

from copy import deepcopy
from io import BytesIO

from openpyxl import Workbook

from src.backend.services.kr_materialisation_guard_service import (
    validate_kr_materialisation_guard,
)
from src.backend.services.spreadsheet_materialisation_guard_service import (
    build_spreadsheet_kr_materialisation_guard,
)
from src.backend.services.spreadsheet_record_ingestion_service import (
    compile_spreadsheet_record_plan,
    extract_spreadsheet_evidence,
)


def _source_row(**values: str) -> dict:
    row_identity = "|".join(
        f"{column}={value}" for column, value in values.items()
    )
    return {
        "sheet": "Synthetic assignments",
        "fields": [
            {
                "column": column,
                "value": value,
                "value_type": "s",
                "content_is_untrusted": True,
                "representation_evidence_statement": (
                    f"Synthetic row {row_identity}; {column}={value}."
                ),
            }
            for column, value in values.items()
        ],
        "content_is_untrusted": True,
    }


def _record(
    *,
    record_suffix: str,
    candidate: str,
    supervisor: str,
    logical_dataset_id: str = "spreadsheet-dataset-synthetic",
) -> dict:
    record_id = f"spreadsheet-record-{record_suffix}"
    return {
        "source_record_id": record_id,
        "source_record_version_id": f"{record_id}-version-one",
        "record_fingerprint": f"fingerprint-{record_suffix}",
        "record_processing_fingerprint": f"processing-{record_suffix}",
        "logical_dataset_id": logical_dataset_id,
        "file_copy_concept_id": "#V#synthetic_file_copy",
        "source_groups": {
            "root": [_source_row(Name=candidate)],
            "supervision_assignments": [
                _source_row(Name=candidate, Supervisor=supervisor)
            ],
        },
        "representation_readback_contract": {
            "schema_version": (
                "spreadsheet_representation_readback_contract.v1"
            ),
            "required_concept_type_minimums": [],
            "required_relationship_predicate_minimums": [],
            "source_group_contracts": [
                {
                    "source_group_key": "root",
                    "semantic_role": "doctoral_candidature",
                    "required_concept_type_id": "#V#doctoral_candidature",
                    "identity_columns": ["Name"],
                    "record_link_predicate_id": (
                        "#V#represented_from_source_record_version"
                    ),
                    "record_link_other_concept_type_id": (
                        "#V#spreadsheet_source_record_version"
                    ),
                    "record_link_artefact_argument": "source",
                    "required_per_artefact_relationships": [
                        {
                            "predicate_id": "#V#has_candidate_person",
                            "artefact_argument": "source",
                            "other_concept_type_id": "#V#person",
                        }
                    ],
                    "cardinality": "one_concept_per_source_row",
                    "expected_count": 1,
                },
                {
                    "source_group_key": "supervision_assignments",
                    "semantic_role": "doctoral_supervision_assignment",
                    "required_concept_type_id": (
                        "#V#doctoral_supervision_assignment"
                    ),
                    "identity_columns": ["Name", "Supervisor"],
                    "record_link_predicate_id": "#V#has_supervision_assignment",
                    "record_link_other_concept_type_id": (
                        "#V#doctoral_candidature"
                    ),
                    "record_link_artefact_argument": "target",
                    "required_per_artefact_relationships": [
                        {
                            "predicate_id": "#V#has_doctoral_supervisor",
                            "artefact_argument": "source",
                            "other_concept_type_id": "#V#person",
                            "other_endpoint_identity": {
                                "scope": "logical_dataset",
                                "identity_columns": ["Supervisor"],
                            },
                        }
                    ],
                    "cardinality": "one_concept_per_source_row",
                    "expected_count": 1,
                },
            ],
        },
        "write_authority_contract": {
            "schema_version": "spreadsheet_write_authority_contract.v1",
            "allowed_concept_parent_ids": [
                "#V#spreadsheet_source_record",
                "#V#spreadsheet_source_record_version",
                "#V#person",
                "#V#doctoral_candidature",
                "#V#doctoral_supervision_assignment",
            ],
            "allowed_relationship_predicate_ids": [
                "#V#has_source_record_version",
                "#V#extracted_from_file_copy",
                "#V#represented_from_source_record_version",
                "#V#has_candidate_person",
                "#V#has_supervision_assignment",
                "#V#has_doctoral_supervisor",
            ],
            "source_identity_parent_ids": {
                "source_record": "#V#spreadsheet_source_record",
                "source_record_version": "#V#spreadsheet_source_record_version",
            },
            "structural_relationship_rules": [
                {
                    "predicate": "#V#has_source_record_version",
                    "source_role": "source_record",
                    "target_role": "source_record_version",
                },
                {
                    "predicate": "#V#extracted_from_file_copy",
                    "source_role": "source_record_version",
                    "target_role": "source_file_copy",
                },
            ],
        },
    }


def _person_slots(guard: dict) -> list[dict]:
    return [
        slot
        for slot in guard["materialisation_guard"]["concept_slots"]
        if slot["parent_id"] == "#V#person"
    ]


def _supervisor_slot(guard: dict) -> dict:
    return next(
        slot
        for slot in _person_slots(guard)
        if slot["stable_name"].startswith("spreadsheet-dataset-")
    )


def _relationship_specs_for_guard(guard: dict) -> list[dict]:
    specs: list[dict] = []
    for rule in guard["materialisation_guard"]["relationship_rules"]:
        source_slot_keys = rule.get("source_slot_keys") or []
        source_fixed_ids = rule.get("source_fixed_ids") or []
        target_slot_keys = rule.get("target_slot_keys") or []
        target_fixed_ids = rule.get("target_fixed_ids") or []
        spec = {"predicate": rule["predicate"]}
        if source_slot_keys:
            spec["source_key"] = source_slot_keys[0]
        else:
            spec["source_id"] = source_fixed_ids[0]
        if target_slot_keys:
            spec["target_key"] = target_slot_keys[0]
        else:
            spec["target_id"] = target_fixed_ids[0]
        specs.append(spec)
    return specs


def _description_for_slot(slot: dict) -> str:
    fragments = list(slot.get("required_description_fragments") or ())
    return " ".join(["Synthetic evidence.", *fragments])


def _compiled_workbook_bytes() -> bytes:
    book = Workbook()
    candidates = book.active
    candidates.title = "Candidates"
    candidates.append(["Candidate"])
    candidates.append(["Synthetic Candidate One"])
    candidates.append(["Synthetic Candidate Two"])
    assignments = book.create_sheet("Assignments")
    assignments.append(["Candidate", "Supervisor"])
    assignments.append(["Synthetic Candidate One", "Shared Supervisor"])
    assignments.append(["Synthetic Candidate Two", "  shared   supervisor "])
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def _compiled_plan() -> dict:
    return {
        "schema_version": "spreadsheet_record_plan.v1",
        "logical_dataset_key": "synthetic-shared-supervisor",
        "record_kind": "doctoral_candidature",
        "root": {
            "sheet": "Candidates",
            "key_columns": ["Candidate"],
            "include_columns": ["Candidate"],
            "omit_columns": [],
        },
        "joins": [
            {
                "sheet": "Assignments",
                "root_key_columns": ["Candidate"],
                "foreign_key_columns": ["Candidate"],
                "include_columns": ["Candidate", "Supervisor"],
                "omit_columns": [],
                "output_key": "supervision_assignments",
                "allow_unmatched_rows": False,
            }
        ],
        "ignored_sheets": [],
        "expected_record_count": 2,
        "representation_profile": {
            "required_record_concept_type_ids": [
                "#V#doctoral_candidature"
            ],
            "required_record_relationship_predicate_ids": [
                "#V#represented_from_source_record_version"
            ],
            "source_group_contracts": [
                {
                    "source_group_key": "root",
                    "semantic_role": "doctoral_candidature",
                    "required_concept_type_id": "#V#doctoral_candidature",
                    "identity_columns": ["Candidate"],
                    "record_link_predicate_id": (
                        "#V#represented_from_source_record_version"
                    ),
                    "record_link_other_concept_type_id": (
                        "#V#spreadsheet_source_record_version"
                    ),
                    "record_link_artefact_argument": "source",
                    "required_per_artefact_relationships": [],
                    "cardinality": "one_concept_per_source_row",
                },
                {
                    "source_group_key": "supervision_assignments",
                    "semantic_role": "doctoral_supervision_assignment",
                    "required_concept_type_id": (
                        "#V#doctoral_supervision_assignment"
                    ),
                    "identity_columns": ["Candidate", "Supervisor"],
                    "record_link_predicate_id": "#V#has_supervision_assignment",
                    "record_link_other_concept_type_id": (
                        "#V#doctoral_candidature"
                    ),
                    "record_link_artefact_argument": "target",
                    "required_per_artefact_relationships": [
                        {
                            "predicate_id": "#V#has_doctoral_supervisor",
                            "artefact_argument": "source",
                            "other_concept_type_id": "#V#person",
                            "other_endpoint_identity": {
                                "scope": "logical_dataset",
                                "identity_columns": ["Supervisor"],
                            },
                        }
                    ],
                    "cardinality": "one_concept_per_source_row",
                },
            ],
        },
    }


def _compiled_authority() -> dict:
    return _record(
        record_suffix="authority",
        candidate="Synthetic Candidate",
        supervisor="Synthetic Supervisor",
    )["write_authority_contract"]


def test_compiler_preserves_declared_cross_record_entity_identity() -> None:
    batch = compile_spreadsheet_record_plan(
        spreadsheet=extract_spreadsheet_evidence(_compiled_workbook_bytes()),
        plan=_compiled_plan(),
        file_copy_concept_id="#V#synthetic_file_copy",
        source_filename="synthetic.xlsx",
        user_concept_id="#V#synthetic_user",
        write_authority_contract=_compiled_authority(),
    )

    assert batch["success"] is True
    assert batch["record_count"] == 2
    contracts = [
        record["representation_readback_contract"]["source_group_contracts"][1]
        for record in batch["records"]
    ]
    assert all(
        contract["required_per_artefact_relationships"][0][
            "other_endpoint_identity"
        ]
        == {
            "scope": "logical_dataset",
            "identity_columns": ["Supervisor"],
        }
        for contract in contracts
    )
    guards = [
        build_spreadsheet_kr_materialisation_guard(record=record)
        for record in batch["records"]
    ]
    assert all(guard["success"] is True for guard in guards)
    assert _supervisor_slot(guards[0])["stable_name"] == _supervisor_slot(
        guards[1]
    )["stable_name"]


def test_compiler_rejects_missing_declared_entity_identity_column() -> None:
    plan = _compiled_plan()
    plan["representation_profile"]["source_group_contracts"][1][
        "required_per_artefact_relationships"
    ][0]["other_endpoint_identity"]["identity_columns"] = ["Unknown identifier"]

    result = compile_spreadsheet_record_plan(
        spreadsheet=extract_spreadsheet_evidence(_compiled_workbook_bytes()),
        plan=plan,
        write_authority_contract=_compiled_authority(),
    )

    assert result["success"] is False
    assert result["error_code"] == (
        "spreadsheet_source_group_entity_identity_columns_missing"
    )


def test_declared_dataset_entity_identity_reuses_only_generated_stable_person() -> None:
    first = build_spreadsheet_kr_materialisation_guard(
        record=_record(
            record_suffix="one",
            candidate="Synthetic Candidate One",
            supervisor="Shared Supervisor",
        )
    )
    second = build_spreadsheet_kr_materialisation_guard(
        record=_record(
            record_suffix="two",
            candidate="Synthetic Candidate Two",
            supervisor="  shared   supervisor ",
        )
    )

    assert first["success"] is True
    assert second["success"] is True
    first_supervisor = _supervisor_slot(first)
    second_supervisor = _supervisor_slot(second)
    assert first_supervisor["stable_name"] == second_supervisor["stable_name"]
    assert (
        first_supervisor["allowed_existing_concept_ids"]
        == second_supervisor["allowed_existing_concept_ids"]
    )
    assert len(first_supervisor["allowed_existing_concept_ids"]) == 1

    candidate_one = next(
        slot
        for slot in _person_slots(first)
        if slot is not first_supervisor
    )
    candidate_two = next(
        slot
        for slot in _person_slots(second)
        if slot is not second_supervisor
    )
    assert candidate_one["stable_name"] != candidate_two["stable_name"]

    arbitrary_existing = first_supervisor["allowed_existing_concept_ids"][0]
    concept_specs = [
        {
            "key": slot["key"],
            "decision": "create",
            "target_name": slot["stable_name"],
            "target_kind": slot["target_kind"],
            "parent_id": slot["parent_id"],
            "description_text": _description_for_slot(slot),
            "concepts": [
                {
                    "name": slot["stable_name"],
                    "kind": "instance",
                    "description": _description_for_slot(slot),
                }
            ],
        }
        for slot in first["materialisation_guard"]["concept_slots"]
    ]
    supervisor_spec = next(
        row for row in concept_specs if row["key"] == first_supervisor["key"]
    )
    supervisor_spec.update(
        {
            "decision": "reuse_existing",
            "existing_concept_id": arbitrary_existing,
        }
    )
    supervisor_spec.pop("concepts")
    accepted = validate_kr_materialisation_guard(
        guard_contract=first["materialisation_guard"],
        phase="plan",
        concept_specs=concept_specs,
        relationship_specs=_relationship_specs_for_guard(first),
    )
    assert accepted["guard_passed"] is True

    unsafe_specs = deepcopy(concept_specs)
    unsafe_supervisor = next(
        row for row in unsafe_specs if row["key"] == first_supervisor["key"]
    )
    unsafe_supervisor["existing_concept_id"] = "#V#arbitrary_existing_person"
    rejected = validate_kr_materialisation_guard(
        guard_contract=first["materialisation_guard"],
        phase="plan",
        concept_specs=unsafe_specs,
        relationship_specs=_relationship_specs_for_guard(first),
    )
    assert rejected["guard_passed"] is False
    assert rejected["error_code"] == "kr_materialisation_guard_reuse_payload_rejected"


def test_dataset_entity_identity_isolated_by_logical_dataset() -> None:
    first = build_spreadsheet_kr_materialisation_guard(
        record=_record(
            record_suffix="one",
            candidate="Synthetic Candidate One",
            supervisor="Shared Supervisor",
            logical_dataset_id="spreadsheet-dataset-one",
        )
    )
    neighbour = build_spreadsheet_kr_materialisation_guard(
        record=_record(
            record_suffix="two",
            candidate="Synthetic Candidate Two",
            supervisor="Shared Supervisor",
            logical_dataset_id="spreadsheet-dataset-two",
        )
    )

    assert _supervisor_slot(first)["stable_name"] != _supervisor_slot(neighbour)[
        "stable_name"
    ]


def test_identity_formatting_drift_reuses_assignment_and_entity_slots() -> None:
    baseline = build_spreadsheet_kr_materialisation_guard(
        record=_record(
            record_suffix="one",
            candidate="Synthetic Candidate One",
            supervisor="Shared Supervisor",
        )
    )
    formatting_only_change = build_spreadsheet_kr_materialisation_guard(
        record=_record(
            record_suffix="one",
            candidate="  synthetic   candidate one ",
            supervisor="  shared   supervisor ",
        )
    )

    assert baseline["success"] is True
    assert formatting_only_change["success"] is True
    baseline_slots = {
        (slot["parent_id"], slot["stable_name"])
        for slot in baseline["materialisation_guard"]["concept_slots"]
    }
    changed_slots = {
        (slot["parent_id"], slot["stable_name"])
        for slot in formatting_only_change["materialisation_guard"][
            "concept_slots"
        ]
    }
    assert changed_slots == baseline_slots
    assert (
        formatting_only_change["materialisation_guard"]["relationship_rules"]
        == baseline["materialisation_guard"]["relationship_rules"]
    )


def test_missing_declared_entity_identity_fails_closed() -> None:
    record = _record(
        record_suffix="one",
        candidate="Synthetic Candidate One",
        supervisor="Shared Supervisor",
    )
    record["source_groups"]["supervision_assignments"][0]["fields"] = [
        field
        for field in record["source_groups"]["supervision_assignments"][0]["fields"]
        if field["column"] != "Supervisor"
    ]

    result = build_spreadsheet_kr_materialisation_guard(record=record)

    assert result["success"] is False
    assert result["error_code"] == (
        "spreadsheet_materialisation_guard_entity_identity_missing"
    )
