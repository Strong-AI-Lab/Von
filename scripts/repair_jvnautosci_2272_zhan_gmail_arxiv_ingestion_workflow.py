"""Repair JVNAUTOSCI-2272 Zhan Gmail arXiv ingestion workflow authority.

This is a Vontology-authoring maintenance script. It keeps request-path policy
out of Python and materialises the repair as VWL/Vontology state: partial batch
semantics, represented retry metadata, valid launch contracts, and represented
source-processing markers used to avoid repeating completed work without
mutating Gmail.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys
from typing import Any, Mapping

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.backend.services.text_value_service import (  # noqa: E402
    get_texts_for_concept,
    upsert_singleton_text_relation,
)
from src.backend.workflows import workflow_concept_authority_service  # noqa: E402
from src.backend.workflows.engine import WorkflowDefinition  # noqa: E402
from src.backend.workflows.vontology_loader import (  # noqa: E402
    load_workflow_definition_from_vontology,
    resolve_workflow_launch_input_contract,
)
from src.backend.workflows.workflow_authoring_service import (  # noqa: E402
    build_workflow_definition_from_authoring_spec,
    serialise_workflow_definition_to_authoring_spec,
)
from src.backend.workflows.workflow_launch_input_contracts import (  # noqa: E402
    normalise_workflow_launch_input_contract,
)


ZHAN_GMAIL_ARXIV_WORKFLOW_ID = "#V#zhan_gmail_arxiv_ingestion_workflow"
EMAIL_ARXIV_MESSAGE_WORKFLOW_ID = "#V#email_arxiv_ingestion_from_message_workflow"
GMAIL_GET_MESSAGE_TOOL_CONCEPT_ID = "#V#gmail_get_message_tool"
GMAIL_OUTPUT_FOLLOWUP_HINT_PREDICATE = "#V#output_followup_hint"

PARENT_PROCESS_MESSAGES_STEP_ID = (
    "#V#workflow_step_zhan_gmail_arxiv_ingestion_workflow_process_messages"
)
PARENT_LIST_MESSAGES_STEP_ID = (
    "#V#workflow_step_zhan_gmail_arxiv_ingestion_workflow_list_messages"
)
CHILD_EXTRACT_RESOURCES_STEP_ID = "#V#workflow_step_email_arxiv_ingestion_from_message_workflow_extract_email_resources"
CHILD_INGEST_ARXIV_STEP_ID = "#V#workflow_step_email_arxiv_ingestion_from_message_workflow_ingest_arxiv_resources"
CHILD_MARK_DONE_STEP_ID = (
    "#V#workflow_step_email_arxiv_ingestion_from_message_workflow_mark_done"
)

ALLOW_PARTIAL_SUCCESS_POLICY = "allow_partial"

EXTERNAL_READ_RETRY_POLICY = {
    "schema_version": "workflow_step_retry_policy.v1",
    "max_attempts": 2,
    "backoff_policy": "fixed",
    "initial_delay_ms": 1000,
    "max_delay_ms": 5000,
    "retry_on_outcomes": ["failure", "unknown"],
}
EXTERNAL_WRITE_RETRY_POLICY = {
    "schema_version": "workflow_step_retry_policy.v1",
    "max_attempts": 2,
    "backoff_policy": "fixed",
    "initial_delay_ms": 1000,
    "max_delay_ms": 5000,
    "retry_on_outcomes": ["failure", "unknown"],
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _static_bindings(step: dict[str, Any]) -> list[dict[str, Any]]:
    raw_bindings = step.get("static_input_bindings")
    if not isinstance(raw_bindings, list):
        raw_bindings = []
    bindings = [item for item in raw_bindings if isinstance(item, dict)]
    step["static_input_bindings"] = bindings
    return bindings


def _set_static_binding(step: dict[str, Any], tool_param: str, value: Any) -> bool:
    bindings = _static_bindings(step)
    for item in bindings:
        if _clean_text(item.get("tool_param") or item.get("key")) != tool_param:
            continue
        changed = item.get("value") != value or item.get("tool_param") != tool_param
        item["tool_param"] = tool_param
        item["value"] = copy.deepcopy(value)
        return changed
    bindings.append({"tool_param": tool_param, "value": copy.deepcopy(value)})
    return True


def _ensure_list_item(container: dict[str, Any], key: str, item: str) -> bool:
    raw_values = container.get(key)
    values = raw_values if isinstance(raw_values, list) else []
    if item in values:
        return False
    values.append(item)
    container[key] = values
    return True


def _ensure_tool_output_mapping(
    step: dict[str, Any],
    *,
    context_key: str,
    tool_output_field: str,
    mapping_concept_id: str,
) -> bool:
    raw_mappings = step.get("tool_output_context_mappings")
    mappings = raw_mappings if isinstance(raw_mappings, list) else []
    changed = raw_mappings is not mappings
    for item in mappings:
        if not isinstance(item, dict):
            continue
        if _clean_text(item.get("context_key")) != context_key:
            continue
        desired = {
            "context_key": context_key,
            "tool_output_field": tool_output_field,
            "mapping_concept_id": mapping_concept_id,
        }
        item_changed = any(item.get(key) != value for key, value in desired.items())
        item.update(desired)
        return changed or item_changed
    mappings.append(
        {
            "context_key": context_key,
            "tool_output_field": tool_output_field,
            "mapping_concept_id": mapping_concept_id,
        }
    )
    step["tool_output_context_mappings"] = mappings
    return True


def _metadata(step: dict[str, Any]) -> dict[str, Any]:
    raw_metadata = step.get("metadata")
    if not isinstance(raw_metadata, dict):
        raw_metadata = {}
        step["metadata"] = raw_metadata
    return raw_metadata


def _set_retry_policy(step: dict[str, Any], policy: Mapping[str, Any]) -> bool:
    metadata = _metadata(step)
    desired = copy.deepcopy(dict(policy))
    if metadata.get("retry_policy") == desired:
        return False
    metadata["retry_policy"] = desired
    return True


def _find_step(spec: Mapping[str, Any], state_id: str) -> dict[str, Any]:
    steps = spec.get("steps")
    if not isinstance(steps, list):
        raise ValueError("workflow_authoring_spec_missing_steps")
    for raw_step in steps:
        if not isinstance(raw_step, dict):
            continue
        if (
            _clean_text(raw_step.get("state_id") or raw_step.get("state_key"))
            == state_id
        ):
            return raw_step
    raise ValueError(f"workflow_authoring_spec_missing_step:{state_id}")


def rewrite_zhan_parent_workflow_spec(
    authoring_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Return a repaired parent workflow spec with partial message batches."""

    spec = copy.deepcopy(dict(authoring_spec))
    spec["workflow_description"] = (
        "Top-level workflow for Zhan Gmail arXiv ingestion. It resolves the "
        "Vontology-authored terminal completion hint, lists arXiv-related "
        "messages through read-only Gmail access, processes each message with "
        "partial batch semantics, and relies on the child workflow to record "
        "represented source-processing evidence only for successfully ingested "
        "source messages."
    )
    spec.pop("description", None)

    process_step = _find_step(spec, PARENT_PROCESS_MESSAGES_STEP_ID)
    list_step = _find_step(spec, PARENT_LIST_MESSAGES_STEP_ID)
    changed = {
        "message_batch_allows_partial": _set_static_binding(
            process_step,
            "success_policy",
            ALLOW_PARTIAL_SUCCESS_POLICY,
        ),
        "message_partial_success_output": _ensure_tool_output_mapping(
            process_step,
            context_key="message_partial_success",
            tool_output_field="for_each_partial_success",
            mapping_concept_id=(
                "#V#workflow_mapping_tool_field_zhan_gmail_arxiv_ingestion_workflow_"
                "process_messages_for_each_partial_success_to_message_partial_success"
            ),
        ),
        "message_item_count_output": _ensure_tool_output_mapping(
            process_step,
            context_key="message_item_count",
            tool_output_field="for_each_item_count",
            mapping_concept_id=(
                "#V#workflow_mapping_tool_field_zhan_gmail_arxiv_ingestion_workflow_"
                "process_messages_for_each_item_count_to_message_item_count"
            ),
        ),
        "list_messages_retry_policy": _set_retry_policy(
            list_step,
            EXTERNAL_READ_RETRY_POLICY,
        ),
    }
    changed["message_partial_success_declared"] = _ensure_list_item(
        process_step,
        "writes_context_keys",
        "message_partial_success",
    )
    changed["message_item_count_declared"] = _ensure_list_item(
        process_step,
        "writes_context_keys",
        "message_item_count",
    )
    return spec, changed


def rewrite_email_message_workflow_spec(
    authoring_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Return a repaired child workflow spec with collectable arXiv outcomes."""

    spec = copy.deepcopy(dict(authoring_spec))
    spec["workflow_description"] = (
        "Processes one Gmail message: extract resource links, ingest each arXiv "
        "reference through represented subworkflows with collectable partial "
        "outcomes, and record a represented source-processing marker only when "
        "all discovered arXiv resources for the message succeed. Messages with "
        "failed arXiv items remain without a marker for later repair or retry."
    )
    spec.pop("description", None)

    extract_step = _find_step(spec, CHILD_EXTRACT_RESOURCES_STEP_ID)
    ingest_step = _find_step(spec, CHILD_INGEST_ARXIV_STEP_ID)
    mark_done_step = _find_step(spec, CHILD_MARK_DONE_STEP_ID)
    marker_args = {
        "source_system": "gmail",
        "source_profile": {"$context_key": "gmail_profile"},
        "source_item_id": {"$context_key": "message_id"},
        "represented_outputs": {"$context_key": "arxiv_iteration_results"},
        "processing_status": "processed",
        "workflow_id": EMAIL_ARXIV_MESSAGE_WORKFLOW_ID,
    }
    transition_changed = False
    for transition in ingest_step.get("conditional_transitions") or []:
        if not isinstance(transition, dict):
            continue
        if _clean_text(transition.get("to_state")) != "resolve_done_hint":
            continue
        transition["to_state"] = CHILD_MARK_DONE_STEP_ID
        transition_changed = True
    changed = {
        "arxiv_batch_allows_partial": _set_static_binding(
            ingest_step,
            "success_policy",
            ALLOW_PARTIAL_SUCCESS_POLICY,
        ),
        "arxiv_partial_success_output": _ensure_tool_output_mapping(
            ingest_step,
            context_key="arxiv_partial_success",
            tool_output_field="for_each_partial_success",
            mapping_concept_id=(
                "#V#workflow_mapping_tool_field_email_arxiv_ingestion_from_message_"
                "workflow_ingest_arxiv_resources_for_each_partial_success_to_"
                "arxiv_partial_success"
            ),
        ),
        "arxiv_item_count_output": _ensure_tool_output_mapping(
            ingest_step,
            context_key="arxiv_item_count",
            tool_output_field="for_each_item_count",
            mapping_concept_id=(
                "#V#workflow_mapping_tool_field_email_arxiv_ingestion_from_message_"
                "workflow_ingest_arxiv_resources_for_each_item_count_to_arxiv_item_count"
            ),
        ),
        "extract_resources_retry_policy": _set_retry_policy(
            extract_step,
            EXTERNAL_READ_RETRY_POLICY,
        ),
        "mark_done_retry_policy": _set_retry_policy(
            mark_done_step,
            EXTERNAL_WRITE_RETRY_POLICY,
        ),
        "successful_arxiv_batch_records_marker": transition_changed,
        "mark_done_uses_source_processing_marker_tool": _set_static_binding(
            mark_done_step,
            "tool_name",
            "record_source_processing_marker",
        ),
        "mark_done_marker_arguments": _set_static_binding(
            mark_done_step,
            "tool_arguments",
            marker_args,
        ),
        "mark_done_marker_output": _ensure_tool_output_mapping(
            mark_done_step,
            context_key="message_processing_marker",
            tool_output_field="result.message_processing_marker",
            mapping_concept_id=(
                "#V#workflow_mapping_tool_field_email_arxiv_ingestion_from_message_"
                "workflow_mark_done_result_message_processing_marker_to_"
                "message_processing_marker"
            ),
        ),
    }
    changed["arxiv_partial_success_declared"] = _ensure_list_item(
        ingest_step,
        "writes_context_keys",
        "arxiv_partial_success",
    )
    changed["arxiv_item_count_declared"] = _ensure_list_item(
        ingest_step,
        "writes_context_keys",
        "arxiv_item_count",
    )
    changed["message_processing_marker_declared"] = _ensure_list_item(
        mark_done_step,
        "writes_context_keys",
        "message_processing_marker",
    )
    return spec, changed


def rewrite_gmail_completion_hint_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Represent completion with source-processing markers, not Gmail labels."""

    hint = copy.deepcopy(dict(payload))
    entries = hint.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("gmail_completion_hint_entries_missing")

    changed = False
    desired_description = (
        "After a message's arXiv resources have been successfully ingested, "
        "record durable represented source-processing evidence for the source "
        "Gmail message."
    )
    desired_action = {
        "type": "workflow_mcp.invoke_tool",
        "tool_name": "record_source_processing_marker",
        "tool_concept_id": "#V#record_source_processing_marker_tool",
    }
    desired_tool_arguments = {
        "source_system": "gmail",
        "source_profile_context_key": "gmail_profile",
        "source_item_id_context_key": "message_id",
        "represented_outputs_context_key": "arxiv_iteration_results",
        "processing_status": "processed",
    }
    desired_effects = [
        {
            "effect_kind": "record_source_processing_marker",
            "marker_type_concept_id": "#V#source_processing_marker",
            "evidence_predicate_concept_id": "#V#hasSourceProcessingEvidenceJson",
        }
    ]
    desired_upstream_filter = {
        "query_fragment": "",
        "rationale": (
            "Gmail listing remains read-only; child workflows check represented "
            "source-processing markers before repeating source-message work."
        ),
    }
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        if _clean_text(raw_entry.get("action_kind")) != "terminal_completion":
            continue
        desired_values = {
            "description": desired_description,
            "action": desired_action,
            "tool_arguments": desired_tool_arguments,
            "represented_effects": desired_effects,
            "upstream_filter": desired_upstream_filter,
        }
        for key, desired in desired_values.items():
            if raw_entry.get(key) == desired:
                continue
            raw_entry[key] = copy.deepcopy(desired)
            changed = True
    return hint, changed


def build_zhan_launch_input_contract() -> dict[str, Any]:
    return {
        "schema_version": "workflow_launch_input_contract.v1",
        "required_inputs": [],
        "input_mappings": [
            {
                "target_context_key": "gmail_profile",
                "source_expression": "inputs.gmail_profile",
                "required": False,
                "description": "Optional caller override for the Gmail profile.",
            },
            {
                "target_context_key": "base_gmail_query",
                "source_expression": "inputs.base_gmail_query",
                "required": False,
                "description": "Optional caller override for the base Gmail query.",
            },
            {
                "target_context_key": "base_gmail_query",
                "source_expression": "inputs.arxiv_id",
                "extractor": "arxiv_id",
                "required": False,
                "description": (
                    "Optional prior-evidence arXiv identifier used as a narrow "
                    "Gmail query when no explicit base Gmail query is supplied."
                ),
            },
            {
                "target_context_key": "arxiv_id",
                "source_expression": "inputs.arxiv_id",
                "extractor": "arxiv_id",
                "required": False,
                "description": (
                    "Optional prior-evidence arXiv identifier preserved for "
                    "downstream workflow context."
                ),
            },
            {
                "target_context_key": "gmail_max_results",
                "source_expression": "inputs.gmail_max_results",
                "required": False,
                "description": "Optional caller override for maximum Gmail messages.",
            },
            {
                "target_context_key": "gmail_max_results",
                "source_expression": "inputs.max_results",
                "required": False,
                "description": "Alias for callers that supply max_results.",
            },
        ],
    }


def build_email_message_launch_input_contract() -> dict[str, Any]:
    return {
        "schema_version": "workflow_launch_input_contract.v1",
        "required_inputs": ["gmail_profile", "current_message"],
        "input_mappings": [
            {
                "target_context_key": "gmail_profile",
                "source_expression": "inputs.gmail_profile",
                "required": True,
                "description": "Gmail profile inherited from the parent workflow.",
            },
            {
                "target_context_key": "current_message",
                "source_expression": "inputs.current_message",
                "required": True,
                "description": "The Gmail list item being processed by the parent for-each step.",
            },
        ],
    }


def _validate_launch_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    contract, error = normalise_workflow_launch_input_contract(payload)
    if contract is None:
        raise ValueError(f"workflow_launch_input_contract_invalid:{error}")
    return contract


def _publish_definition(definition: WorkflowDefinition) -> dict[str, Any]:
    publication = (
        workflow_concept_authority_service.publish_workflow_definition_from_definition(
            definition=definition,
            create_missing=False,
            purpose=definition.purpose,
        )
    )
    errors = publication.get("errors_by_workflow_id") or {}
    validation = publication.get("validation_failures_by_workflow_id") or {}
    if errors.get(definition.workflow_id) or validation.get(definition.workflow_id):
        raise RuntimeError(
            json.dumps(publication, indent=2, sort_keys=True, default=str)
        )
    workflow_concept_authority_service.upsert_workflow_publication_lifecycle(
        workflow_id=definition.workflow_id,
        phase="published",
        published=True,
        validation_passed=True,
        postconditions_verified=False,
        routing_eligible=(definition.workflow_id == ZHAN_GMAIL_ARXIV_WORKFLOW_ID),
        rollout_state=(
            "published"
            if definition.workflow_id == ZHAN_GMAIL_ARXIV_WORKFLOW_ID
            else "support_subworkflow"
        ),
    )
    return publication


def _load_authoring_spec(workflow_id: str) -> dict[str, Any]:
    definition = load_workflow_definition_from_vontology(workflow_id)
    if definition is None:
        raise ValueError(f"workflow_not_found:{workflow_id}")
    return serialise_workflow_definition_to_authoring_spec(definition)


def _load_gmail_completion_hint() -> dict[str, Any]:
    rows = get_texts_for_concept(GMAIL_GET_MESSAGE_TOOL_CONCEPT_ID, limit=50)
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        predicate = _clean_text(row.get("predicate") or row.get("predicate_id"))
        if predicate != GMAIL_OUTPUT_FOLLOWUP_HINT_PREDICATE:
            continue
        text = _clean_text(row.get("text"))
        if not text:
            continue
        return json.loads(text)
    raise ValueError("gmail_completion_hint_missing")


def publish_jvnautosci_2272_repair(*, dry_run: bool = False) -> dict[str, Any]:
    parent_spec, parent_changes = rewrite_zhan_parent_workflow_spec(
        _load_authoring_spec(ZHAN_GMAIL_ARXIV_WORKFLOW_ID)
    )
    child_spec, child_changes = rewrite_email_message_workflow_spec(
        _load_authoring_spec(EMAIL_ARXIV_MESSAGE_WORKFLOW_ID)
    )
    parent_definition = build_workflow_definition_from_authoring_spec(parent_spec)
    child_definition = build_workflow_definition_from_authoring_spec(child_spec)

    zhan_contract = _validate_launch_contract(build_zhan_launch_input_contract())
    child_contract = _validate_launch_contract(
        build_email_message_launch_input_contract()
    )
    hint_payload, hint_changed = rewrite_gmail_completion_hint_payload(
        _load_gmail_completion_hint()
    )

    result: dict[str, Any] = {
        "success": True,
        "dry_run": dry_run,
        "parent_changes": parent_changes,
        "child_changes": child_changes,
        "gmail_completion_hint_changed": hint_changed,
        "launch_contracts": {
            ZHAN_GMAIL_ARXIV_WORKFLOW_ID: zhan_contract,
            EMAIL_ARXIV_MESSAGE_WORKFLOW_ID: child_contract,
        },
    }
    if dry_run:
        return result

    result["parent_publication"] = _publish_definition(parent_definition)
    result["child_publication"] = _publish_definition(child_definition)
    result["zhan_launch_input_contract"] = (
        workflow_concept_authority_service.upsert_workflow_json_policy_text(
            workflow_id=ZHAN_GMAIL_ARXIV_WORKFLOW_ID,
            predicate=workflow_concept_authority_service.WORKFLOW_LAUNCH_INPUT_CONTRACT_TEXT_PREDICATE,
            payload=zhan_contract,
            context={"source": "jvnautosci_2272_repair"},
        )
    )
    result["child_launch_input_contract"] = (
        workflow_concept_authority_service.upsert_workflow_json_policy_text(
            workflow_id=EMAIL_ARXIV_MESSAGE_WORKFLOW_ID,
            predicate=workflow_concept_authority_service.WORKFLOW_LAUNCH_INPUT_CONTRACT_TEXT_PREDICATE,
            payload=child_contract,
            context={"source": "jvnautosci_2272_repair"},
        )
    )
    result["gmail_completion_hint"] = upsert_singleton_text_relation(
        subject_concept_id=GMAIL_GET_MESSAGE_TOOL_CONCEPT_ID,
        predicate=GMAIL_OUTPUT_FOLLOWUP_HINT_PREDICATE,
        text=json.dumps(hint_payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={"source": "jvnautosci_2272_repair"},
        garbage_collect=True,
    )
    result["resolved_launch_contract_sources"] = {
        workflow_id: resolve_workflow_launch_input_contract(workflow_id)[1]
        for workflow_id in (
            ZHAN_GMAIL_ARXIV_WORKFLOW_ID,
            EMAIL_ARXIV_MESSAGE_WORKFLOW_ID,
        )
    }
    try:
        from src.backend.workflows.durable import registry_factory

        registry_factory._resolve_subworkflow_definition.cache_clear()
    except Exception:
        pass
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and validate the repaired specs without mutating Vontology.",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            publish_jvnautosci_2272_repair(dry_run=args.dry_run),
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
