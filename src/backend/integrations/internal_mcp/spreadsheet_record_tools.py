"""Internal MCP adapters for generic spreadsheet-record processing tools.

The handlers in this module expose deterministic support operations. Durable
spreadsheet interpretation and programme semantics remain represented workflow
and prompt authority rather than catalogue policy.
"""

from __future__ import annotations

from typing import Any, Mapping

from .gateway import MethodDefinition
from .schemas import Schema, make_error_response


def _compile_spreadsheet_record_plan(**kwargs):
    from ...services.spreadsheet_record_ingestion_service import (
        compile_spreadsheet_record_plan,
    )

    spreadsheet = kwargs.get("spreadsheet")
    plan = kwargs.get("plan")
    if not isinstance(spreadsheet, Mapping):
        return make_error_response(
            "missing_parameter",
            "Missing structured spreadsheet evidence.",
            details={"missing": ["spreadsheet"]},
        )
    if not isinstance(plan, Mapping):
        return make_error_response(
            "missing_parameter",
            "Missing model-authored spreadsheet record plan.",
            details={"missing": ["plan"]},
        )
    return compile_spreadsheet_record_plan(
        spreadsheet=spreadsheet,
        plan=plan,
        file_copy_concept_id=kwargs.get("file_copy_concept_id"),
        source_filename=kwargs.get("source_filename"),
        user_concept_id=kwargs.get("user_concept_id"),
        write_authority_contract=kwargs.get("write_authority_contract"),
        organisation_concept_id=(
            kwargs.get("organisation_concept_id") or kwargs.get("org_concept_id")
        ),
    )


def _build_spreadsheet_record_materialisation_request(**kwargs):
    from ...services.spreadsheet_record_ingestion_service import (
        build_spreadsheet_record_materialisation_request,
    )
    from ...services.source_processing_marker_service import (
        find_existing_accessible_concept_ids,
    )
    from ...services.spreadsheet_materialisation_guard_service import (
        build_spreadsheet_kr_materialisation_guard,
    )

    record = kwargs.get("record")
    if not isinstance(record, Mapping):
        return make_error_response(
            "missing_parameter",
            "Missing validated spreadsheet record evidence.",
            details={"missing": ["record"]},
        )
    guard_preview = build_spreadsheet_kr_materialisation_guard(record=record)
    if not guard_preview.get("success"):
        return guard_preview
    guard_contract = guard_preview.get("materialisation_guard")
    slots = (
        guard_contract.get("concept_slots")
        if isinstance(guard_contract, Mapping)
        else None
    )
    candidate_ids = sorted(
        {
            str(concept_id).strip()
            for slot in (slots if isinstance(slots, list) else [])
            if isinstance(slot, Mapping)
            for concept_id in (slot.get("allowed_existing_concept_ids") or [])
            if isinstance(concept_id, str) and concept_id.strip()
        }
    )
    try:
        reusable_ids = find_existing_accessible_concept_ids(candidate_ids)
    except Exception:
        return make_error_response(
            "spreadsheet_reuse_authority_read_failed",
            "Could not bind idempotent reuse authority from canonical read-back.",
            details={"candidate_count": len(candidate_ids)},
        )
    return build_spreadsheet_record_materialisation_request(
        record=record,
        reusable_existing_concept_ids=sorted(reusable_ids),
    )


def _compare_spreadsheet_record_batch(**kwargs):
    from ...services.spreadsheet_record_ingestion_service import (
        compare_spreadsheet_record_batch,
    )

    previous_evidence = kwargs.get("previous_evidence")
    return compare_spreadsheet_record_batch(
        current_manifest=kwargs.get("current_manifest"),
        previous_evidence=(
            previous_evidence if isinstance(previous_evidence, Mapping) else None
        ),
    )


def _build_spreadsheet_record_completion_evidence(**kwargs):
    from ...services.spreadsheet_record_ingestion_service import (
        build_spreadsheet_record_completion_evidence,
    )

    record = kwargs.get("record")
    if not isinstance(record, Mapping):
        return make_error_response(
            "missing_parameter",
            "Missing validated spreadsheet record evidence.",
            details={"missing": ["record"]},
        )
    return build_spreadsheet_record_completion_evidence(
        record=record,
        materialisation_result=(
            kwargs.get("materialisation_result")
            if isinstance(kwargs.get("materialisation_result"), Mapping)
            else None
        ),
        concept_iteration_results=kwargs.get("concept_iteration_results"),
        relationship_iteration_results=kwargs.get("relationship_iteration_results"),
        previous_evidence=(
            kwargs.get("previous_evidence")
            if isinstance(kwargs.get("previous_evidence"), Mapping)
            else None
        ),
    )


def _build_spreadsheet_batch_completion_evidence(**kwargs):
    from ...services.spreadsheet_record_ingestion_service import (
        build_spreadsheet_batch_completion_evidence,
    )

    batch = kwargs.get("batch")
    if not isinstance(batch, Mapping):
        return make_error_response(
            "missing_parameter",
            "Missing compiled spreadsheet record batch.",
            details={"missing": ["batch"]},
        )
    reconciliation = kwargs.get("reconciliation")
    return build_spreadsheet_batch_completion_evidence(
        batch=batch,
        reconciliation=(
            reconciliation if isinstance(reconciliation, Mapping) else None
        ),
        iteration_results=kwargs.get("iteration_results"),
    )


def _compile_spreadsheet_record_plan_input_schema() -> Schema:
    return Schema(
        required={
            "spreadsheet": dict,
            "plan": dict,
            "write_authority_contract": dict,
        },
        optional={
            "file_copy_concept_id": (str, type(None)),
            "source_filename": (str, type(None)),
            "user_concept_id": (str, type(None)),
            "organisation_concept_id": (str, type(None)),
            "org_concept_id": (str, type(None)),
        },
        allow_unknown=False,
        description=(
            "Validate and execute a model-authored spreadsheet root/join/coverage "
            "plan against structured untrusted workbook evidence."
        ),
    )


def _compile_spreadsheet_record_plan_output_schema() -> Schema:
    return Schema(
        required={"success": bool, "schema_version": str, "records": list},
        optional={
            "error": (str, type(None)),
            "error_code": (str, type(None)),
            "error_details": (dict, type(None)),
            "logical_dataset_key": (str, type(None)),
            "proposed_logical_dataset_key": (str, type(None)),
            "logical_dataset_binding_source": (str, type(None)),
            "logical_dataset_id": (str, type(None)),
            "dataset_source_item_id": (str, type(None)),
            "record_kind": (str, type(None)),
            "plan_digest": (str, type(None)),
            "batch_fingerprint": (str, type(None)),
            "record_count": (int, type(None)),
            "ready_record_count": (int, type(None)),
            "blocked_record_count": (int, type(None)),
            "row_counts": (dict, type(None)),
            "record_manifest": (list, type(None)),
            "source_group_manifest": (list, type(None)),
            "omissions_by_sheet": (dict, type(None)),
            "ignored_sheets": (list, type(None)),
            "unmatched_join_rows": (list, type(None)),
            "file_copy_concept_id": (str, type(None)),
            "file_sha256": (str, type(None)),
            "logical_content_sha256": (str, type(None)),
            "content_is_untrusted": (bool, type(None)),
        },
        allow_unknown=True,
        description=(
            "Compiled record batch with stable opaque record keys, reorder-stable "
            "fingerprints, bounded source evidence, omissions, and counts."
        ),
    )


def _spreadsheet_record_materialisation_request_input_schema() -> Schema:
    return Schema(
        required={"record": dict},
        optional={},
        allow_unknown=False,
        description=(
            "Build a bounded KR request from one validated spreadsheet record; "
            "cell content remains explicitly untrusted."
        ),
    )


def _spreadsheet_record_materialisation_request_output_schema() -> Schema:
    return Schema(
        required={"success": bool},
        optional={
            "schema_version": (str, type(None)),
            "prompt": (str, type(None)),
            "request_payload": (dict, type(None)),
            "source_item_id": (str, type(None)),
            "source_fingerprint": (str, type(None)),
            "source_record_id": (str, type(None)),
            "source_record_version_id": (str, type(None)),
            "file_copy_concept_id": (str, type(None)),
            "blocking_reasons": (list, type(None)),
            "error_code": (str, type(None)),
        },
        allow_unknown=True,
        description=(
            "Bounded per-record KR materialisation request and stable source identity."
        ),
    )


def _compare_spreadsheet_record_batch_input_schema() -> Schema:
    return Schema(
        required={"current_manifest": list},
        optional={"previous_evidence": (dict, type(None))},
        allow_unknown=False,
        description=(
            "Compare current and previous opaque spreadsheet record manifests. "
            "Missing records require review and never authorise deletion."
        ),
    )


def _compare_spreadsheet_record_batch_output_schema() -> Schema:
    return Schema(
        required={
            "success": bool,
            "schema_version": str,
            "missing_record_review_required": bool,
            "delete_authorised": bool,
        },
        optional={
            "previous_batch_seen": (bool, type(None)),
            "current_record_count": (int, type(None)),
            "previous_record_count": (int, type(None)),
            "added_source_item_ids": (list, type(None)),
            "changed_source_item_ids": (list, type(None)),
            "unchanged_source_item_ids": (list, type(None)),
            "missing_source_item_ids": (list, type(None)),
            "added_source_group_row_ids": (list, type(None)),
            "changed_source_group_row_ids": (list, type(None)),
            "unchanged_source_group_row_ids": (list, type(None)),
            "missing_source_group_row_ids": (list, type(None)),
            "missing_source_group_row_review_required": (bool, type(None)),
            "source_group_reconciliation": (dict, type(None)),
        },
        allow_unknown=True,
        description="Privacy-safe spreadsheet batch delta; deletion is always false.",
    )


def _spreadsheet_completion_evidence_input_schema(*, batch: bool) -> Schema:
    if batch:
        return Schema(
            required={"batch": dict},
            optional={
                "reconciliation": (dict, type(None)),
                "iteration_results": (list, type(None)),
            },
            allow_unknown=False,
            description="Build a compact terminal spreadsheet batch receipt.",
        )
    return Schema(
        required={"record": dict},
        optional={
            "materialisation_result": (dict, type(None)),
            "concept_iteration_results": (list, type(None)),
            "relationship_iteration_results": (list, type(None)),
            "previous_evidence": (dict, type(None)),
        },
        allow_unknown=False,
        description=(
            "Build compact per-record source-marker evidence from verified KR results."
        ),
    )


def _spreadsheet_completion_evidence_output_schema(*, batch: bool) -> Schema:
    optional: dict[str, Any] = {
        "processing_evidence": (dict, type(None)),
        "source_item_id": (str, type(None)),
        "source_fingerprint": (str, type(None)),
        "file_copy_concept_ids": (list, type(None)),
        "represented_outputs": (dict, type(None)),
        "batch_receipt": (dict, type(None)),
        "batch_complete": (bool, type(None)),
        "partial_success": (bool, type(None)),
        "processing_status": (str, type(None)),
        "record_change_kind": (str, type(None)),
        "record_outcome_counts": (dict, type(None)),
        "effect_counts": (dict, type(None)),
        "represented_concept_ids": (list, type(None)),
        "record_marker_ids": (list, type(None)),
        "blocked_records": (list, type(None)),
        "source_group_review_items": (list, type(None)),
        "missing_source_group_row_review_required": (bool, type(None)),
    }
    return Schema(
        required={"success": bool},
        optional=optional,
        allow_unknown=True,
        description=(
            "Compact batch completion receipt."
            if batch
            else "Compact record completion evidence."
        ),
    )


def build_spreadsheet_record_tool_definitions() -> list[MethodDefinition]:
    """Build the generic spreadsheet-record internal MCP definitions."""

    return [
        MethodDefinition(
            name="compile_spreadsheet_record_plan",
            handler=_compile_spreadsheet_record_plan,
            input_schema=_compile_spreadsheet_record_plan_input_schema(),
            output_schema=_compile_spreadsheet_record_plan_output_schema(),
            category="read",
            description=(
                "Validate and execute a model-authored spreadsheet record plan. "
                "The tool performs only deterministic sheet/column/join validation, "
                "coverage accounting, and stable fingerprinting; semantic meaning "
                "remains workflow and prompt authority."
            ),
        ),
        MethodDefinition(
            name="build_spreadsheet_record_materialisation_request",
            handler=_build_spreadsheet_record_materialisation_request,
            input_schema=_spreadsheet_record_materialisation_request_input_schema(),
            output_schema=_spreadsheet_record_materialisation_request_output_schema(),
            category="read",
            description=(
                "Serialise one validated spreadsheet record into a bounded KR "
                "materialisation request while preserving untrusted-content labels, "
                "source coordinates, stable record identity, and version fingerprint."
            ),
        ),
        MethodDefinition(
            name="compare_spreadsheet_record_batch",
            handler=_compare_spreadsheet_record_batch,
            input_schema=_compare_spreadsheet_record_batch_input_schema(),
            output_schema=_compare_spreadsheet_record_batch_output_schema(),
            category="read",
            description=(
                "Compare opaque current and previous spreadsheet record manifests. "
                "Returns additions, changes, unchanged records, and review-only "
                "missing records; it never authorises deletion."
            ),
        ),
        MethodDefinition(
            name="build_spreadsheet_record_completion_evidence",
            handler=_build_spreadsheet_record_completion_evidence,
            input_schema=_spreadsheet_completion_evidence_input_schema(batch=False),
            output_schema=_spreadsheet_completion_evidence_output_schema(batch=False),
            category="read",
            description=(
                "Build compact source-marker evidence after a per-record KR "
                "materialisation has been read back successfully."
            ),
        ),
        MethodDefinition(
            name="build_spreadsheet_batch_completion_evidence",
            handler=_build_spreadsheet_batch_completion_evidence,
            input_schema=_spreadsheet_completion_evidence_input_schema(batch=True),
            output_schema=_spreadsheet_completion_evidence_output_schema(batch=True),
            category="read",
            description=(
                "Build a privacy-safe terminal batch receipt from the compiled "
                "record manifest, reconciliation result, and child outcomes."
            ),
        ),
    ]


__all__ = ["build_spreadsheet_record_tool_definitions"]
