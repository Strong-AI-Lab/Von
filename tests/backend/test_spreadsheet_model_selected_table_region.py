from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook

from src.backend.services.spreadsheet_record_ingestion_service import (
    compile_spreadsheet_record_plan,
    extract_spreadsheet_evidence,
)


def _workbook_bytes() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Candidates"
    sheet.append(["Synthetic programme report"])
    sheet.append(["Prepared for bounded import"])
    sheet.append(["Name", "Stage"])
    sheet.append(["Synthetic Candidate One", "Confirmed"])
    sheet.append(["Synthetic Candidate Two", "Provisional"])
    sheet.append(["End of source table"])
    stream = BytesIO()
    book.save(stream)
    book.close()
    return stream.getvalue()


def _authority() -> dict:
    return {
        "schema_version": "spreadsheet_write_authority_contract.v1",
        "allowed_concept_parent_ids": [
            "#V#spreadsheet_source_record",
            "#V#spreadsheet_source_record_version",
            "#V#doctoral_candidature",
        ],
        "allowed_relationship_predicate_ids": [
            "#V#has_source_record_version",
            "#V#extracted_from_file_copy",
            "#V#represented_from_source_record_version",
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
    }


def _plan() -> dict:
    return {
        "schema_version": "spreadsheet_record_plan.v1",
        "logical_dataset_key": "synthetic-programme",
        "record_kind": "doctoral_candidature",
        "root": {
            "sheet": "Candidates",
            "table_region": {
                "header_row_number": 3,
                "data_start_row_number": 4,
                "data_end_row_number": 5,
                "excluded_rows_reason": (
                    "Title/preamble and footer are not candidate records."
                ),
            },
            "key_columns": ["Name"],
            "include_columns": ["Name", "Stage"],
            "omit_columns": [],
        },
        "joins": [],
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
                    "identity_columns": ["Name"],
                    "record_link_predicate_id": (
                        "#V#represented_from_source_record_version"
                    ),
                    "record_link_other_concept_type_id": (
                        "#V#spreadsheet_source_record_version"
                    ),
                    "record_link_artefact_argument": "source",
                    "required_per_artefact_relationships": [],
                    "cardinality": "one_concept_per_source_row",
                }
            ],
        },
    }


def test_model_selected_table_region_accepts_title_rows_and_footer() -> None:
    evidence = extract_spreadsheet_evidence(_workbook_bytes())

    assert evidence["sheets"][0]["header_row_number"] == 1
    result = compile_spreadsheet_record_plan(
        spreadsheet=evidence,
        plan=_plan(),
        file_copy_concept_id="#V#synthetic_file_copy",
        write_authority_contract=_authority(),
    )

    assert result["success"] is True
    assert result["record_count"] == 2
    assert result["row_counts"] == {"Candidates": 2}
    assert {
        tuple(record["record_key_values"]) for record in result["records"]
    } == {
        ("Synthetic Candidate One",),
        ("Synthetic Candidate Two",),
    }
    assert result["table_regions_by_sheet"] == {
        "Candidates": {
            "header_row_number": 3,
            "data_start_row_number": 4,
            "data_end_row_number": 5,
            "excluded_non_empty_row_numbers": [1, 2, 6],
            "excluded_rows_reason": (
                "Title/preamble and footer are not candidate records."
            ),
        }
    }


def test_selected_region_requires_reason_for_excluded_non_empty_rows() -> None:
    plan = _plan()
    plan["root"]["table_region"].pop("excluded_rows_reason")

    result = compile_spreadsheet_record_plan(
        spreadsheet=extract_spreadsheet_evidence(_workbook_bytes()),
        plan=plan,
        write_authority_contract=_authority(),
    )

    assert result["success"] is False
    assert result["error_code"] == (
        "spreadsheet_plan_table_region_exclusion_reason_missing"
    )
