"""Reusable durable workflow for authoritative file-copy typing.

This workflow turns lightweight file metadata into persisted Vontology file-copy
typing so other workflows can route from typed artefact context instead of
repeating filename/MIME heuristics independently.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...services.file_copy_typing_service import (
    FILE_COPY_TYPING_PREDICATE,
    FILE_COPY_TYPING_SCHEMA_VERSION,
    infer_file_copy_typing,
    persist_file_copy_typing,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..workflow_registry import WorkflowRegistration

FILE_COPY_TYPING_WORKFLOW_ID = "#V#file_copy_typing_workflow"
FILE_COPY_TYPING_WORKFLOW_VERSION = "file_copy_typing_workflow.v1"

_FILE_COPY_CONTEXT_INPUTS = {
    "concept_id": {
        "$context_key": "concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_concept_id_to_concept_id_parameter",
    },
    "file_copy_concept_id": {
        "$context_key": "file_copy_concept_id",
        "$mapping_concept_id": "#V#workflow_mapping_file_copy_concept_id_to_file_copy_concept_id_parameter",
    },
    "content_type": {"$context_key": "content_type"},
    "original_filename": {"$context_key": "original_filename"},
    "size_bytes": {"$context_key": "size_bytes"},
}


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _normalise_typing_result(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _handle_infer_typing(request: WorkflowActionRequest) -> WorkflowActionResult:
    combined: dict[str, Any] = dict(request.data or {})
    combined.update(dict(request.inputs or {}))

    typing_result = infer_file_copy_typing(
        content_type=_clean_text(combined.get("content_type")) or None,
        original_filename=_clean_text(combined.get("original_filename")) or None,
        size_bytes=_coerce_optional_int(combined.get("size_bytes")),
    )
    typing_result = _normalise_typing_result(typing_result)

    return WorkflowActionResult(
        status="success",
        outputs={
            "typing_version": FILE_COPY_TYPING_WORKFLOW_VERSION,
            "typing_schema_version": _clean_text(
                typing_result.get("schema_version")
            )
            or FILE_COPY_TYPING_SCHEMA_VERSION,
            "typing_result": typing_result,
            "typing_determinable": bool(typing_result.get("determinable")),
            "primary_type_concept_id": typing_result.get("primary_type_concept_id"),
            "semantic_type_concept_id": typing_result.get("semantic_type_concept_id"),
            "format_type_concept_id": typing_result.get("format_type_concept_id"),
            "asserted_type_concept_ids": list(
                typing_result.get("asserted_type_concept_ids") or []
            ),
            "route_hint": typing_result.get("route_hint"),
            "route_confidence": typing_result.get("route_confidence"),
            "route_scores": dict(typing_result.get("route_scores") or {}),
            "arxiv_id": typing_result.get("arxiv_id"),
            "arxiv_ids": list(typing_result.get("arxiv_ids") or []),
            "matched_signals": list(typing_result.get("matched_signals") or []),
            "matched_rule_ids": list(typing_result.get("matched_rule_ids") or []),
            "content_type_token": typing_result.get("content_type_token"),
            "filename_extension": typing_result.get("filename_extension"),
        },
    )


def _handle_persist_typing(request: WorkflowActionRequest) -> WorkflowActionResult:
    concept_id = _clean_text(request.data.get("file_copy_concept_id")) or _clean_text(
        request.data.get("concept_id")
    )
    if not concept_id:
        return WorkflowActionResult(
            status="success",
            outputs={
                "typing_persisted": False,
                "typing_persist_error": "missing_file_copy_concept_id",
            },
        )

    typing_result = _normalise_typing_result(request.data.get("typing_result"))
    if not typing_result:
        typing_result = infer_file_copy_typing(
            content_type=_clean_text(request.data.get("content_type")) or None,
            original_filename=_clean_text(request.data.get("original_filename")) or None,
            size_bytes=_coerce_optional_int(request.data.get("size_bytes")),
        )

    persist_result = persist_file_copy_typing(
        file_copy_concept_id=concept_id,
        typing_result=typing_result,
    )
    persist_payload = dict(persist_result) if isinstance(persist_result, Mapping) else {}

    outputs = {
        "typing_version": FILE_COPY_TYPING_WORKFLOW_VERSION,
        "typing_schema_version": _clean_text(typing_result.get("schema_version"))
        or FILE_COPY_TYPING_SCHEMA_VERSION,
        "typing_result": typing_result,
        "typing_persisted": bool(persist_payload.get("typing_persisted")),
        "typing_predicate": _clean_text(persist_payload.get("typing_predicate"))
        or FILE_COPY_TYPING_PREDICATE,
        "typing_relation_id": persist_payload.get("typing_relation_id"),
        "typing_replaced_count": persist_payload.get("typing_replaced_count"),
        "primary_type_concept_id": typing_result.get("primary_type_concept_id"),
        "semantic_type_concept_id": typing_result.get("semantic_type_concept_id"),
        "format_type_concept_id": typing_result.get("format_type_concept_id"),
        "asserted_type_concept_ids": list(
            persist_payload.get("asserted_type_concept_ids")
            or typing_result.get("asserted_type_concept_ids")
            or []
        ),
        "route_hint": typing_result.get("route_hint"),
        "route_confidence": typing_result.get("route_confidence"),
        "route_scores": dict(typing_result.get("route_scores") or {}),
        "arxiv_id": typing_result.get("arxiv_id"),
        "arxiv_ids": list(typing_result.get("arxiv_ids") or []),
        "typing_structural_relations": list(
            persist_payload.get("structural_relations") or []
        ),
        "typing_relation_errors": list(persist_payload.get("relation_errors") or []),
    }
    if persist_payload.get("success") is False:
        outputs["typing_persist_error"] = _clean_text(persist_payload.get("error"))
    return WorkflowActionResult(status="success", outputs=outputs)


def build_file_copy_typing_workflow_test_definition() -> WorkflowDefinition:
    infer_writes_context_keys = [
        "typing_version",
        "typing_schema_version",
        "typing_result",
        "typing_determinable",
        "primary_type_concept_id",
        "semantic_type_concept_id",
        "format_type_concept_id",
        "asserted_type_concept_ids",
        "route_hint",
        "route_confidence",
        "route_scores",
        "arxiv_id",
        "arxiv_ids",
        "matched_signals",
        "matched_rule_ids",
        "content_type_token",
        "filename_extension",
    ]
    persist_writes_context_keys = [
        "typing_version",
        "typing_schema_version",
        "typing_result",
        "typing_persisted",
        "typing_predicate",
        "typing_relation_id",
        "typing_replaced_count",
        "primary_type_concept_id",
        "semantic_type_concept_id",
        "format_type_concept_id",
        "asserted_type_concept_ids",
        "route_hint",
        "route_confidence",
        "route_scores",
        "arxiv_id",
        "arxiv_ids",
        "typing_structural_relations",
        "typing_relation_errors",
        "typing_persist_error",
    ]
    infer = WorkflowStateSpec(
        state_id="infer",
        actions=(
            WorkflowActionInvocation(
                action_id="file_copy_typing.infer",
                inputs=dict(_FILE_COPY_CONTEXT_INPUTS),
                description=(
                    "Infer authoritative file-copy taxonomy and route hints from "
                    "lightweight upload metadata."
                ),
            ),
        ),
        metadata={"writes_context_keys": infer_writes_context_keys},
        transitions=(
            WorkflowTransitionSpec(
                to_state="persist",
                condition=lambda _ctx: True,
                reason="typing_inferred",
            ),
        ),
    )

    persist = WorkflowStateSpec(
        state_id="persist",
        actions=(
            WorkflowActionInvocation(
                action_id="file_copy_typing.persist",
                description=(
                    "Persist inferred file-copy typing into Vontology so later "
                    "routing/discovery can reuse typed artefact context."
                ),
            ),
        ),
        metadata={"writes_context_keys": persist_writes_context_keys},
        transitions=(
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="typing_persisted",
            ),
        ),
    )

    return WorkflowDefinition(
        workflow_id=FILE_COPY_TYPING_WORKFLOW_ID,
        initial_state="infer",
        states={
            "infer": infer,
            "persist": persist,
            "complete": WorkflowStateSpec(state_id="complete", terminal=True),
            "failed": WorkflowStateSpec(state_id="failed", terminal=True),
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Infer and persist authoritative file-copy typing so downstream "
            "workflows can route from Vontology-backed artefact taxonomy."
        ),
    )


def build_file_copy_typing_workflow_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=FILE_COPY_TYPING_WORKFLOW_ID,
        definition=build_file_copy_typing_workflow_test_definition(),
        purpose=(
            "Persist authoritative file-copy typing and route hints for upload, "
            "discovery, and continuation workflows."
        ),
        source="built_in",
    )


def register_file_copy_typing_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id="file_copy_typing.infer",
            handler=_handle_infer_typing,
            description="Infer file-copy typing from upload metadata.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id="file_copy_typing.persist",
            handler=_handle_persist_typing,
            description="Persist authoritative file-copy typing into Vontology.",
        )
    )


__all__ = [
    "FILE_COPY_TYPING_PREDICATE",
    "FILE_COPY_TYPING_SCHEMA_VERSION",
    "FILE_COPY_TYPING_WORKFLOW_ID",
    "FILE_COPY_TYPING_WORKFLOW_VERSION",
    "build_file_copy_typing_workflow_test_definition",
    "build_file_copy_typing_workflow_test_registration",
    "register_file_copy_typing_actions",
]
