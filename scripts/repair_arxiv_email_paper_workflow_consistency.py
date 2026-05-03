"""Repair and audit arXiv email paper-ingestion workflow consistency.

This is a Vontology-authoring maintenance script. It does not add request-path
fallback behaviour to the arXiv MCP tool. Instead it updates the email arXiv
resource workflow so post-representation paper reading consumes the
``file_copy_concept_id`` produced by ``#V#arxiv_paper_representation_workflow``.
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

from src.backend.services.output_hint_contracts import (  # noqa: E402
    OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
)
from src.backend.services import concept_service  # noqa: E402
from src.backend.services.text_value_service import (  # noqa: E402
    upsert_singleton_text_relation,
)
from src.backend.workflows import workflow_concept_authority_service  # noqa: E402
from src.backend.workflows.engine import (  # noqa: E402
    WorkflowActionInvocation,
    WorkflowDefinition,
)
from src.backend.workflows.vontology_loader import (  # noqa: E402
    discover_workflow_ids,
    load_workflow_definition_from_vontology,
)
from src.backend.workflows.workflow_authoring_service import (  # noqa: E402
    build_workflow_definition_from_authoring_spec,
    serialise_workflow_definition_to_authoring_spec,
)


ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#arxiv_paper_representation_workflow"
ARXIV_RESOURCE_EMAIL_WORKFLOW_ID = (
    "#V#arxiv_resource_ingestion_from_email_reference_workflow"
)
EMAIL_ARXIV_MESSAGE_WORKFLOW_ID = "#V#email_arxiv_ingestion_from_message_workflow"
ZHAN_GMAIL_ARXIV_WORKFLOW_ID = "#V#zhan_gmail_arxiv_ingestion_workflow"

READ_FILE_COPY_TOOL_CONCEPT_ID = "#V#read_file_copy_tool"
READ_FILE_COPY_TOOL_NAME = "read_file_copy"

_ARXIV_WRAPPER_FILE_COPY_PRODUCER_STATE_IDS = frozenset(
    {
        "finalise_cached_pdf",
        "download_or_finalise",
        "import_arxiv_pdf_from_url",
    }
)

_READ_FILE_COPY_HINT_SEED_PATH = (
    _PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "read_file_copy_scholarly_signal_extraction_hint_seed.md"
)

_DIRECT_ARXIV_MCP_TOOL_NAMES = frozenset(
    {
        "download_paper",
        "get_paper_metadata",
        "read_paper",
        "search_papers",
    }
)
_DIRECT_ARXIV_MCP_ALLOWED_WORKFLOW_IDS = frozenset(
    {
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
    }
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip() if isinstance(value, str) else ""


def _logical_suffix(value: Any, suffix: str) -> bool:
    return _clean_text(value).endswith(suffix)


def _is_arxiv_wrapper_file_copy_producer_state(value: Any) -> bool:
    state_id = _clean_text(value)
    if not state_id:
        return False
    return any(
        state_id == producer_state_id or state_id.endswith(f"_{producer_state_id}")
        for producer_state_id in _ARXIV_WRAPPER_FILE_COPY_PRODUCER_STATE_IDS
    )


def _static_bindings(step: dict[str, Any]) -> list[dict[str, Any]]:
    value = step.get("static_input_bindings")
    if not isinstance(value, list):
        value = []
        step["static_input_bindings"] = value
    return [item for item in value if isinstance(item, dict)]


def _set_static_binding(step: dict[str, Any], tool_param: str, value: Any) -> None:
    bindings = _static_bindings(step)
    for item in bindings:
        if _clean_text(item.get("tool_param") or item.get("key")) == tool_param:
            item["tool_param"] = tool_param
            item["value"] = copy.deepcopy(value)
            return
    bindings.append({"tool_param": tool_param, "value": copy.deepcopy(value)})
    step["static_input_bindings"] = bindings


def _ensure_list_item(container: dict[str, Any], key: str, item: Any) -> None:
    values = container.get(key)
    if not isinstance(values, list):
        values = []
        container[key] = values
    if item not in values:
        values.append(item)


def _remove_list_item(container: dict[str, Any], key: str, item: Any) -> bool:
    values = container.get(key)
    if values is None:
        return False
    values_list = values if isinstance(values, list) else [values]
    filtered = [value for value in values_list if value != item]
    if len(filtered) == len(values_list):
        return False
    if filtered:
        container[key] = filtered
    else:
        container.pop(key, None)
    return True


def _ensure_tool_output_mapping(
    step: dict[str, Any],
    *,
    context_key: str,
    tool_output_field: str,
    mapping_concept_id: str,
) -> None:
    mappings = step.get("tool_output_context_mappings")
    if not isinstance(mappings, list):
        mappings = []
        step["tool_output_context_mappings"] = mappings
    for item in mappings:
        if not isinstance(item, dict):
            continue
        if _clean_text(item.get("context_key")) == context_key:
            item["tool_output_field"] = tool_output_field
            item["mapping_concept_id"] = mapping_concept_id
            return
    mappings.append(
        {
            "context_key": context_key,
            "tool_output_field": tool_output_field,
            "mapping_concept_id": mapping_concept_id,
        }
    )


def load_read_file_copy_signal_hint_seed() -> str:
    text = _READ_FILE_COPY_HINT_SEED_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("read_file_copy_signal_hint_seed_empty")
    return text


def ensure_read_file_copy_signal_extraction_hint() -> dict[str, Any]:
    """Materialise the Vontology-authored extraction hint for read_file_copy."""

    return upsert_singleton_text_relation(
        subject_concept_id=READ_FILE_COPY_TOOL_CONCEPT_ID,
        predicate=OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
        text=load_read_file_copy_signal_hint_seed(),
        lang="en-NZ",
        context={
            "source": "repair_arxiv_email_paper_workflow_consistency",
            "authority_role": "seed_to_vontology_hint",
        },
        garbage_collect=True,
    )


def rewrite_arxiv_resource_ingestion_spec(
    authoring_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Return a repaired authoring spec for the arXiv email-resource workflow."""

    spec = copy.deepcopy(dict(authoring_spec))
    spec["workflow_description"] = (
        "Ingests one arXiv resource reference discovered from an email by "
        "delegating paper representation to #V#arxiv_paper_representation_workflow, "
        "reading the represented file copy produced by that wrapper, extracting "
        "high-confidence Vontology claim candidates using the "
        "#V#read_file_copy_tool hint, creating #V#claim instances, and linking "
        "them to the represented paper."
    )
    spec.pop("description", None)

    found = {
        "representation_step": False,
        "read_step": False,
        "extract_step": False,
    }
    steps = spec.get("steps")
    if not isinstance(steps, list):
        raise ValueError("arxiv_resource_ingestion_spec_missing_steps")

    for raw_step in steps:
        if not isinstance(raw_step, dict):
            continue
        state_id = raw_step.get("state_id") or raw_step.get("state_key")

        if _logical_suffix(state_id, "_represent_arxiv_paper"):
            found["representation_step"] = True
            _ensure_tool_output_mapping(
                raw_step,
                context_key="file_copy_concept_id",
                tool_output_field="result.file_copy_concept_id",
                mapping_concept_id=(
                    "#V#workflow_mapping_tool_field_"
                    "arxiv_resource_ingestion_from_email_reference_workflow_"
                    "represent_arxiv_paper_result_file_copy_concept_id_to_"
                    "file_copy_concept_id"
                ),
            )
            _ensure_list_item(raw_step, "writes_context_keys", "file_copy_concept_id")

        if _logical_suffix(state_id, "_read_paper"):
            found["read_step"] = True
            _set_static_binding(raw_step, "tool_name", READ_FILE_COPY_TOOL_NAME)
            _set_static_binding(
                raw_step,
                "tool_arguments",
                {
                    "concept_id": {"$context_key": "file_copy_concept_id"},
                    "as_text": True,
                    "allow_large": True,
                    "max_bytes": 20_000_000,
                },
            )
            _ensure_tool_output_mapping(
                raw_step,
                context_key="paper_payload",
                tool_output_field="result",
                mapping_concept_id=(
                    "#V#workflow_mapping_tool_field_"
                    "arxiv_resource_ingestion_from_email_reference_workflow_"
                    "read_file_copy_result_to_paper_payload"
                ),
            )

        if _logical_suffix(state_id, "_extract_paper_signals"):
            found["extract_step"] = True
            _set_static_binding(
                raw_step,
                "source_tool_concept",
                READ_FILE_COPY_TOOL_CONCEPT_ID,
            )

    if not all(found.values()):
        missing = [key for key, value in found.items() if not value]
        raise ValueError(
            "arxiv_resource_ingestion_spec_missing_expected_steps:"
            + ",".join(missing)
        )
    return spec, found


def rewrite_arxiv_wrapper_file_copy_output_spec(
    authoring_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    """Ensure the arXiv wrapper declares file_copy_concept_id as an output."""

    spec = copy.deepcopy(dict(authoring_spec))
    steps = spec.get("steps")
    if not isinstance(steps, list):
        raise ValueError("arxiv_wrapper_spec_missing_steps")

    updated_count = 0
    for raw_step in steps:
        if not isinstance(raw_step, dict):
            continue
        state_id = raw_step.get("state_id")
        is_file_copy_producer = _is_arxiv_wrapper_file_copy_producer_state(state_id)
        mappings = raw_step.get("tool_output_context_mappings")
        if not isinstance(mappings, list):
            continue
        if not any(
            isinstance(item, Mapping)
            and _clean_text(item.get("context_key")) == "file_copy_concept_id"
            for item in mappings
        ):
            continue
        if not is_file_copy_producer:
            if _remove_list_item(
                raw_step, "writes_context_keys", "file_copy_concept_id"
            ):
                updated_count += 1
            continue
        before = list(raw_step.get("writes_context_keys") or [])
        _ensure_list_item(raw_step, "writes_context_keys", "file_copy_concept_id")
        after = raw_step.get("writes_context_keys") or []
        if before != after:
            updated_count += 1

    if updated_count == 0:
        already_declared = any(
            isinstance(raw_step, dict)
            and _is_arxiv_wrapper_file_copy_producer_state(raw_step.get("state_id"))
            and "file_copy_concept_id" in (raw_step.get("writes_context_keys") or [])
            for raw_step in steps
        )
        if not already_declared:
            raise ValueError("arxiv_wrapper_file_copy_output_mapping_missing")
    return spec, updated_count


def build_repaired_arxiv_wrapper_definition() -> tuple[WorkflowDefinition, int]:
    definition = load_workflow_definition_from_vontology(ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID)
    if definition is None:
        raise ValueError(
            "arxiv_paper_representation_workflow_not_found:"
            f"{ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID}"
        )
    spec = serialise_workflow_definition_to_authoring_spec(definition)
    repaired_spec, updated_count = rewrite_arxiv_wrapper_file_copy_output_spec(spec)
    return build_workflow_definition_from_authoring_spec(repaired_spec), updated_count


def build_repaired_arxiv_resource_ingestion_definition() -> WorkflowDefinition:
    definition = load_workflow_definition_from_vontology(ARXIV_RESOURCE_EMAIL_WORKFLOW_ID)
    if definition is None:
        raise ValueError(
            "arxiv_resource_ingestion_workflow_not_found:"
            f"{ARXIV_RESOURCE_EMAIL_WORKFLOW_ID}"
        )
    spec = serialise_workflow_definition_to_authoring_spec(definition)
    repaired_spec, _found = rewrite_arxiv_resource_ingestion_spec(spec)
    return build_workflow_definition_from_authoring_spec(repaired_spec)


def _ensure_step_writes_context_key(
    *,
    step_concept_id: str,
    context_key: str,
) -> bool:
    concept = concept_service.get_concept_by_concept_id(step_concept_id)
    if not isinstance(concept, Mapping):
        raise ValueError(f"workflow_step_concept_missing:{step_concept_id}")
    relationships = dict(concept.get("relationships") or {})
    values_raw = relationships.get("#V#workflow_step_writes_context_key")
    values = (
        list(values_raw)
        if isinstance(values_raw, list)
        else ([values_raw] if isinstance(values_raw, str) and values_raw.strip() else [])
    )
    if context_key in values:
        return False
    values.append(context_key)
    relationships["#V#workflow_step_writes_context_key"] = values
    concept_service.update_concept(step_concept_id, {"relationships": relationships})
    return True


def _remove_step_writes_context_key(
    *,
    step_concept_id: str,
    context_key: str,
) -> bool:
    concept = concept_service.get_concept_by_concept_id(step_concept_id)
    if not isinstance(concept, Mapping):
        raise ValueError(f"workflow_step_concept_missing:{step_concept_id}")
    relationships = dict(concept.get("relationships") or {})
    values_raw = relationships.get("#V#workflow_step_writes_context_key")
    values = (
        list(values_raw)
        if isinstance(values_raw, list)
        else ([values_raw] if isinstance(values_raw, str) and values_raw.strip() else [])
    )
    filtered = [value for value in values if value != context_key]
    if len(filtered) == len(values):
        return False
    if filtered:
        relationships["#V#workflow_step_writes_context_key"] = filtered
    else:
        relationships.pop("#V#workflow_step_writes_context_key", None)
    concept_service.update_concept(step_concept_id, {"relationships": relationships})
    return True


def publish_arxiv_wrapper_output_contract_repair() -> dict[str, Any]:
    definition = load_workflow_definition_from_vontology(
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )
    if definition is None:
        raise ValueError(
            "arxiv_paper_representation_workflow_not_found:"
            f"{ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID}"
        )
    updated_step_ids: list[str] = []
    removed_step_ids: list[str] = []
    for state_id, state in definition.states.items():
        mappings = (state.metadata or {}).get("tool_output_context_mappings")
        if not isinstance(mappings, list):
            continue
        if not any(
            isinstance(item, Mapping)
            and _clean_text(item.get("context_key")) == "file_copy_concept_id"
            for item in mappings
        ):
            continue
        step_id = _clean_text(state_id)
        if not step_id:
            continue
        if not _is_arxiv_wrapper_file_copy_producer_state(step_id):
            if _remove_step_writes_context_key(
                step_concept_id=step_id,
                context_key="file_copy_concept_id",
            ):
                removed_step_ids.append(step_id)
            continue
        if _ensure_step_writes_context_key(
            step_concept_id=step_id,
            context_key="file_copy_concept_id",
        ):
            updated_step_ids.append(step_id)
    return {
        "updated_file_copy_output_step_count": len(updated_step_ids),
        "updated_step_ids": updated_step_ids,
        "removed_non_producer_file_copy_output_step_count": len(removed_step_ids),
        "removed_step_ids": removed_step_ids,
    }


def publish_arxiv_resource_ingestion_repair() -> dict[str, Any]:
    ensure_hint_result = ensure_read_file_copy_signal_extraction_hint()
    wrapper_repair = publish_arxiv_wrapper_output_contract_repair()
    definition = build_repaired_arxiv_resource_ingestion_definition()
    publication = workflow_concept_authority_service.publish_workflow_definition_from_definition(
        definition=definition,
        create_missing=True,
        purpose=definition.purpose,
    )
    errors = publication.get("errors_by_workflow_id") or {}
    validation = publication.get("validation_failures_by_workflow_id") or {}
    if errors.get(ARXIV_RESOURCE_EMAIL_WORKFLOW_ID) or validation.get(
        ARXIV_RESOURCE_EMAIL_WORKFLOW_ID
    ):
        raise RuntimeError(json.dumps(publication, indent=2, sort_keys=True, default=str))

    workflow_concept_authority_service.upsert_workflow_publication_lifecycle(
        workflow_id=ARXIV_RESOURCE_EMAIL_WORKFLOW_ID,
        phase="published",
        published=True,
        validation_passed=True,
        postconditions_verified=True,
    )
    try:
        from src.backend.workflows.durable import registry_factory

        registry_factory._resolve_subworkflow_definition.cache_clear()
    except Exception:
        pass
    return {
        "success": True,
        "hint": ensure_hint_result,
        "wrapper_output_contract_repair": wrapper_repair,
        "publication": publication,
    }


def _tool_names_for_action(action: WorkflowActionInvocation) -> list[str]:
    names: list[str] = []
    action_id = _clean_text(getattr(action, "action_id", None))
    if action_id:
        names.append(action_id)
    inputs = getattr(action, "inputs", None)
    if isinstance(inputs, Mapping):
        tool_name = _clean_text(inputs.get("tool_name"))
        if tool_name:
            names.append(tool_name)
    return names


def _scan_definition_for_direct_arxiv_tools(
    definition: WorkflowDefinition,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if definition.workflow_id in _DIRECT_ARXIV_MCP_ALLOWED_WORKFLOW_IDS:
        return findings
    for state_id, state in (definition.states or {}).items():
        for action in state.actions or ():
            for tool_name in _tool_names_for_action(action):
                if tool_name not in _DIRECT_ARXIV_MCP_TOOL_NAMES:
                    continue
                findings.append(
                    {
                        "workflow_id": definition.workflow_id,
                        "state_id": state_id,
                        "tool_name": tool_name,
                        "reason": "direct_arxiv_mcp_tool_outside_wrapper",
                    }
                )
    return findings


def audit_live_paper_workflow_consistency() -> dict[str, Any]:
    workflow_ids = [
        workflow_id
        for workflow_id in discover_workflow_ids()
        if any(
            token in workflow_id.lower()
            for token in ("arxiv", "paper", "scholarly", "gmail", "email")
        )
    ]
    for workflow_id in (
        ZHAN_GMAIL_ARXIV_WORKFLOW_ID,
        EMAIL_ARXIV_MESSAGE_WORKFLOW_ID,
        ARXIV_RESOURCE_EMAIL_WORKFLOW_ID,
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
    ):
        if workflow_id not in workflow_ids:
            workflow_ids.append(workflow_id)

    findings: list[dict[str, Any]] = []
    loaded: list[str] = []
    missing: list[str] = []
    for workflow_id in sorted(dict.fromkeys(workflow_ids)):
        definition = load_workflow_definition_from_vontology(workflow_id)
        if definition is None:
            missing.append(workflow_id)
            continue
        loaded.append(workflow_id)
        findings.extend(_scan_definition_for_direct_arxiv_tools(definition))

    return {
        "success": not findings,
        "loaded_workflow_ids": loaded,
        "missing_workflow_ids": missing,
        "direct_arxiv_tool_findings": findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Audit live Vontology workflows without applying the repair.",
    )
    args = parser.parse_args()

    before = audit_live_paper_workflow_consistency()
    if args.check_only:
        print(json.dumps({"before": before}, indent=2, sort_keys=True, default=str))
        return

    repair = publish_arxiv_resource_ingestion_repair()
    after = audit_live_paper_workflow_consistency()
    print(
        json.dumps(
            {
                "before": before,
                "repair": repair,
                "after": after,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
