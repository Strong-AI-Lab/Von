"""Action handlers for the Von workflow-creation workflow."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Iterable, Mapping

from ...services import concept_search_service, concept_service
from ...services.concept_service import ConceptNotFoundError
from ...services.relationship_write_service import add_relationship
from ...services.workflow_discovery_service import discover_workflows
from ...services.workflow_authoring_vontology_service import (
    build_workflow_authoring_prompt_contract,
    get_workflow_authoring_prompt_health_status,
)
from ...services.text_value_service import (
    get_texts_for_concept,
    upsert_text_for_concept,
)
from ...utils.concept_id_utils import canonicalise_vontology_concept_id
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import WorkflowEnvironment, WorkflowExecutor
from ..mcp_tool_bridge import resolve_internal_mcp_tool_name
from ..vontology_loader import discover_workflow_ids, load_workflow_definition_from_vontology
from ..workflow_creation_contracts import (
    WORKFLOW_AUTHORING_ACTION_DECIDE_REPAIR_OR_CREATE,
    WORKFLOW_AUTHORING_ACTION_DESIGN_REPAIR_SPEC,
    WORKFLOW_AUTHORING_ACTION_DISCOVER_EXISTING_WORKFLOWS,
    WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY,
    WORKFLOW_AUTHORING_ACTION_EXTRACT_EXISTING_WORKFLOW_SPEC,
    WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION,
    WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION,
    WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS,
    WORKFLOW_CREATION_ACTION_CONTRACT_BY_ACTION_ID,
    WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
    WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_ACTION_EMIT_MARKER,
    WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
    WORKFLOW_CREATION_ACTION_FINALISE,
    WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT,
    WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
    WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE,
    WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS,
    WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
    WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
    WORKFLOW_CREATION_STEP_MATERIALISE_WORKFLOW_DEFINITION,
    WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_WORKFLOW_ID,
    workflow_creation_action_target,
)
from ..workflow_definition_identity_service import (
    collect_workflow_action_ids,
    validate_workflow_definition_contract,
)
from ..workflow_side_effect_guardrails import enforce_workflow_mcp_write_guardrails
from ..workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
    serialise_workflow_definition_to_authoring_spec,
)
from ..workflow_template_profile_service import (
    resolve_workflow_spec_template,
)
from .entity_representation_workflow import (
    ENTITY_REPRESENTATION_MATERIALISE_ACTION_ID,
    register_entity_representation_actions,
)
from .workflow_gap_recovery_workflow import register_workflow_gap_recovery_actions

WORKFLOW_CONTEXT_KEY_VALIDATED_TYPE_NAME = "#V#workflow_context_key_validated_type_name"
DEFAULT_WORKFLOW_PARENT_TYPE_ID = "#V#ai_workflow"
DEFAULT_WORKFLOW_STEP_TYPE_ID = "#V#workflow_step"
DEFAULT_PERSON_TYPE_ID = "#V#person"
DEFAULT_STUDENT_TYPE_ID = "#V#student"
DEFAULT_PHD_STUDENT_TYPE_ID = "#V#phd_student"
DEFAULT_RESEARCH_TOPIC_TYPE_ID = "#V#research_topic"
DEFAULT_PREDICATE_TYPE_ID = "#V#predicate"
DEFAULT_AUTHORED_BY_PREDICATE_ID = "#V#authored_by"
DEFAULT_SUPERVISED_BY_PREDICATE_ID = "#V#supervised_by"
DEFAULT_RESEARCHES_PREDICATE_ID = "#V#researches"
WORKFLOW_GRAPH_PREDICATE_HAS_INITIAL_STEP = "#V#hasInitialStep"
WORKFLOW_GRAPH_PREDICATE_HAS_STEP = "#V#hasStep"
WORKFLOW_GRAPH_PREDICATE_INVOKES_ACTION = "#V#invokesAction"
WORKFLOW_GRAPH_PREDICATE_HAS_INPUT_MAP = "#V#hasInputMap"
WORKFLOW_GRAPH_PREDICATE_NEXT_STEP = "#V#nextStep"
WORKFLOW_GRAPH_PREDICATE_ON_TRUE_NEXT_STEP = "#V#onTrueNextStep"
WORKFLOW_GRAPH_PREDICATE_ON_FALSE_NEXT_STEP = "#V#onFalseNextStep"
WORKFLOW_GRAPH_PREDICATE_ON_FAILURE_NEXT_STEP = "#V#onFailureNextStep"
WORKFLOW_GRAPH_PREDICATE_ON_UNKNOWN_NEXT_STEP = "#V#onUnknownNextStep"
_SLUG_RE = re.compile(r"[^a-z0-9]+")

WORKFLOW_CREATION_AUTONOMY_POLICY_VALUE = "only_when_vital_info_missing"
WORKFLOW_CREATION_SYNTHESIS_POLICY_MISSING_ERROR = (
    "workflow_creation_synthesis_policy_missing"
)
WORKFLOW_PUBLICATION_LIFECYCLE_SCHEMA_VERSION = "workflow_publication_lifecycle.v1"
WORKFLOW_PUBLICATION_LIFECYCLE_TEXT_PREDICATE = "#V#hasWorkflowLifecycleJson"


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _normalise_slug(value: Any, *, fallback: str) -> str:
    raw = _clean_text(value).lower()
    if raw.startswith("#v#"):
        raw = raw[3:]
    slug = _SLUG_RE.sub("_", raw).strip("_")
    return slug or fallback


def _titleise(slug: str) -> str:
    words = [part for part in slug.replace("-", "_").split("_") if part]
    if not words:
        return "Workflow"
    return " ".join(word.capitalize() for word in words)


def _normalise_concept_id(value: Any, *, fallback_slug: str) -> str:
    candidate = _clean_text(value)
    if not candidate:
        candidate = f"#V#{fallback_slug}"
    canonical = canonicalise_vontology_concept_id(candidate)
    if canonical:
        return canonical
    return f"#V#{fallback_slug}"


def _extract_request_text(context: Mapping[str, Any]) -> str:
    for key in (
        "workflow_request",
        "workflow_description",
        "request",
        "description",
        "prompt",
    ):
        value = _clean_text(context.get(key))
        if value:
            return value
    return ""


def _extract_workflow_spec(context: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = context.get("workflow_spec")
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _load_synthesis_policy_text() -> str | None:
    rows = get_texts_for_concept(
        subject_concept_id=WORKFLOW_CREATION_WORKFLOW_ID,
        predicate="hasContent",
        limit=200,
    )
    if not isinstance(rows, list) or not rows:
        return None

    preferred: str | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = _clean_text(row.get("text"))
        if not text:
            continue
        lang = _clean_text(row.get("lang")).lower()
        if lang == "en-nz":
            return text
        if preferred is None:
            preferred = text
    return preferred


def _normalise_person_name(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _extract_person_names_from_value(raw: Any) -> list[str]:
    candidates: list[str] = []
    if isinstance(raw, str):
        parts = re.split(r"[,\n;]+", raw)
        candidates.extend(parts)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                candidates.append(item)
                continue
            if isinstance(item, Mapping):
                candidate = _clean_text(
                    item.get("name")
                    or item.get("full_name")
                    or item.get("display_name")
                    or item.get("author")
                )
                if candidate:
                    candidates.append(candidate)
    elif isinstance(raw, Mapping):
        candidate = _clean_text(
            raw.get("name")
            or raw.get("full_name")
            or raw.get("display_name")
            or raw.get("author")
        )
        if candidate:
            candidates.append(candidate)

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalised = _normalise_person_name(candidate)
        if not normalised:
            continue
        fingerprint = normalised.casefold()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(normalised)
    return deduped


def _extract_author_names(context: Mapping[str, Any]) -> list[str]:
    for key in (
        "author_names",
        "authors",
        "paper_author_names",
        "scholarly_author_names",
    ):
        names = _extract_person_names_from_value(context.get(key))
        if names:
            return names

    for metadata_key in ("scholarly_metadata", "paper_metadata", "metadata"):
        metadata = context.get(metadata_key)
        if not isinstance(metadata, Mapping):
            continue
        for key in ("author_names", "authors", "creators"):
            names = _extract_person_names_from_value(metadata.get(key))
            if names:
                return names
    return []


def _extract_labeled_value(*, source_text: str, labels: tuple[str, ...]) -> str:
    text = _clean_text(source_text)
    if not text:
        return ""
    for label in labels:
        pattern = re.compile(
            rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+)$",
        )
        match = pattern.search(text)
        if match:
            return _clean_text(match.group(1))
    return ""


def _extract_phd_student_profile(context: Mapping[str, Any]) -> dict[str, Any]:
    source_text = _clean_text(
        context.get("phd_student_description")
        or context.get("student_description")
        or context.get("student_profile_text")
        or context.get("source_text")
        or context.get("description")
        or context.get("text")
    )
    if not source_text:
        source_text = _extract_request_text(context)

    student_name = _clean_text(
        context.get("phd_student_name")
        or context.get("student_name")
        or context.get("name")
    )
    if not student_name:
        student_name = _extract_labeled_value(
            source_text=source_text,
            labels=(
                "phd student name",
                "doctoral student name",
                "student name",
                "student",
                "name",
            ),
        )
    student_name = _normalise_person_name(student_name)

    supervisor_names = _extract_person_names_from_value(
        context.get("supervisor_names")
        or context.get("supervisors")
        or context.get("advisor_names")
        or context.get("advisors")
    )
    if not supervisor_names:
        supervisor_text = _extract_labeled_value(
            source_text=source_text,
            labels=("supervisors", "supervisor", "advisors", "advisor"),
        )
        if supervisor_text:
            supervisor_names = _extract_person_names_from_value(supervisor_text)

    research_topic = _clean_text(
        context.get("research_topic")
        or context.get("research_area")
        or context.get("topic")
    )
    if not research_topic:
        research_topic = _extract_labeled_value(
            source_text=source_text,
            labels=(
                "research topic",
                "research area",
                "research focus",
                "topic",
            ),
        )
    research_topic = _clean_text(research_topic)

    institution = _clean_text(
        context.get("institution")
        or context.get("university")
        or context.get("department")
    )
    if not institution:
        institution = _extract_labeled_value(
            source_text=source_text,
            labels=("institution", "university", "department"),
        )

    return {
        "student_name": student_name,
        "supervisor_names": supervisor_names,
        "research_topic": research_topic,
        "institution": institution,
        "source_text": source_text,
    }


def _concept_has_exact_name(*, concept_id: str, concept_name: str) -> bool:
    expected = _normalise_person_name(concept_name).casefold()
    if not expected:
        return False

    name_rows = get_texts_for_concept(
        subject_concept_id=concept_id,
        predicate="hasName",
        limit=200,
    )
    for row in name_rows:
        if not isinstance(row, Mapping):
            continue
        value = _normalise_person_name(row.get("text")).casefold()
        if value and value == expected:
            return True

    concept = _load_concept(concept_id)
    if isinstance(concept, Mapping):
        fallback_name = _normalise_person_name(concept.get("name")).casefold()
        if fallback_name and fallback_name == expected:
            return True
    return False


def _find_verified_named_instance_concept_ids(
    *,
    concept_name: str,
    instance_of_type_id: str,
) -> list[str]:
    try:
        search_result = concept_search_service.search_concepts(
            query=concept_name,
            instance_of=instance_of_type_id,
            match_type="exact",
            include_description=False,
            limit=25,
        )
    except Exception:
        return []

    rows = search_result.get("results") if isinstance(search_result, Mapping) else None
    if not isinstance(rows, list):
        return []

    verified_ids: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        candidate_id = _clean_text(row.get("concept_id"))
        if not candidate_id:
            continue
        if _concept_has_exact_name(
            concept_id=candidate_id,
            concept_name=concept_name,
        ):
            verified_ids.append(candidate_id)

    return list(dict.fromkeys(verified_ids))


def _find_verified_person_concept_ids(*, person_name: str, person_type_id: str) -> list[str]:
    return _find_verified_named_instance_concept_ids(
        concept_name=person_name,
        instance_of_type_id=person_type_id,
    )


def _resolve_existing_person_concept_id(
    *,
    person_name: str,
    person_type_id: str,
) -> str | None:
    unique_verified_ids = _find_verified_person_concept_ids(
        person_name=person_name,
        person_type_id=person_type_id,
    )
    if len(unique_verified_ids) == 1:
        return unique_verified_ids[0]
    return None


def _create_person_concept(
    *,
    person_name: str,
    person_type_id: str,
) -> str:
    slug = _normalise_slug(person_name, fallback="person")
    for _ in range(6):
        suffix = uuid.uuid4().hex[:8]
        concept_id = f"#V#person_{slug}_{suffix}"
        try:
            concept_service.create_concept(
                name=person_name,
                concept_id=concept_id,
                parent_concept_ids=[person_type_id],
                create_as_instance=True,
            )
            return concept_id
        except Exception:
            continue
    raise RuntimeError("workflow_creation_person_concept_create_failed")


def _create_research_topic_concept(
    *,
    research_topic: str,
    research_topic_type_id: str,
) -> str:
    slug = _normalise_slug(research_topic, fallback="research_topic")
    for _ in range(6):
        suffix = uuid.uuid4().hex[:8]
        concept_id = f"#V#research_topic_{slug}_{suffix}"
        try:
            concept_service.create_concept(
                name=research_topic,
                concept_id=concept_id,
                parent_concept_ids=[research_topic_type_id],
                create_as_instance=True,
            )
            return concept_id
        except Exception:
            continue
    raise RuntimeError("workflow_creation_research_topic_concept_create_failed")


def _resolve_or_create_research_topic_concept_id(
    *,
    research_topic: str,
    research_topic_type_id: str,
) -> tuple[str, bool]:
    verified_ids = _find_verified_named_instance_concept_ids(
        concept_name=research_topic,
        instance_of_type_id=research_topic_type_id,
    )
    if len(verified_ids) > 1:
        raise RuntimeError("workflow_creation_research_topic_ambiguous")
    if len(verified_ids) == 1:
        return verified_ids[0], True
    return (
        _create_research_topic_concept(
            research_topic=research_topic,
            research_topic_type_id=research_topic_type_id,
        ),
        False,
    )


def _workflow_template_id_from_context(context: Mapping[str, Any]) -> str | None:
    for key in (
        "workflow_template_id",
        "workflow_template_profile_id",
        "template_id",
    ):
        value = _clean_text(context.get(key))
        if value:
            return value
    return None


def _resolve_workflow_template_spec(
    *,
    context: Mapping[str, Any],
    request_text: str,
    workflow_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    explicit_template_id = _workflow_template_id_from_context(context)
    workflow_name = _titleise(
        workflow_id[3:] if workflow_id.startswith("#V#") else workflow_id
    )
    request_summary = _clean_text(request_text[:160]) if request_text else "workflow_created"
    workflow_description = request_text
    rendered_spec, template_resolution = resolve_workflow_spec_template(
        request_text=request_text,
        explicit_template_id=explicit_template_id,
        variables={
            "workflow_id": workflow_id,
            "workflow_name": workflow_name,
            "workflow_description": workflow_description,
            "request_summary": request_summary,
        },
    )
    profile = (
        dict(template_resolution.get("profile") or {})
        if isinstance(template_resolution.get("profile"), Mapping)
        else {}
    )
    fallback_description = _clean_text(
        rendered_spec.get("description") or workflow_description
    )
    if not fallback_description:
        fallback_description = _clean_text(
            template_resolution.get("default_workflow_description")
        ) or "Workflow created from a natural-language workflow request."
    rendered_spec["description"] = fallback_description

    if bool(profile.get("requires_synthesis_policy")):
        policy_text = _load_synthesis_policy_text()
        if not policy_text:
            raise ValueError(WORKFLOW_CREATION_SYNTHESIS_POLICY_MISSING_ERROR)
        rendered_spec["synthesis_policy_text"] = policy_text

    return rendered_spec, template_resolution


def _normalise_input_mapping(inputs: Mapping[str, Any]) -> Dict[str, Any]:
    encoded: Dict[str, Any] = {}
    for key, value in inputs.items():
        key_text = _clean_text(key)
        if not key_text:
            continue
        if isinstance(value, Mapping):
            encoded[key_text] = {
                str(child_key): child_value
                for child_key, child_value in value.items()
                if isinstance(child_key, str) and str(child_key).strip()
            }
            continue
        if isinstance(value, list):
            encoded[key_text] = list(value)
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            encoded[key_text] = value
            continue
        encoded[key_text] = json.loads(
            json.dumps(value, sort_keys=True, ensure_ascii=True)
        )
    return encoded


def _normalise_text_relation_specs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    relation_specs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        predicate = _clean_text(item.get("predicate"))
        text = _clean_text(item.get("text"))
        lang = _clean_text(item.get("lang")) or "en-NZ"
        if not predicate or not text:
            continue
        relation_specs.append(
            {
                "predicate": predicate,
                "text": text,
                "lang": lang,
            }
        )
    return relation_specs


def _build_step_rows(
    *,
    workflow_id: str,
    raw_steps: Any,
    request_text: str,
) -> list[dict[str, Any]]:
    workflow_slug = _normalise_slug(workflow_id, fallback="generated_workflow")
    rows: list[dict[str, Any]] = []
    if isinstance(raw_steps, list):
        for index, raw in enumerate(raw_steps):
            if not isinstance(raw, Mapping):
                continue
            state_key = _normalise_slug(
                raw.get("state_key") or raw.get("state_id") or raw.get("name"),
                fallback=f"step_{index + 1}",
            )
            action_id = _clean_text(raw.get("action_id")) or None
            subworkflow_id = _clean_text(raw.get("subworkflow_id")) or None
            execution_mode = _clean_text(raw.get("execution_mode")) or None
            next_state = _normalise_slug(
                raw.get("next_state")
                or raw.get("next_state_key")
                or raw.get("next"),
                fallback="",
            )
            on_true_state = _normalise_slug(
                raw.get("on_true_state") or raw.get("on_true_state_key"),
                fallback="",
            )
            on_false_state = _normalise_slug(
                raw.get("on_false_state") or raw.get("on_false_state_key"),
                fallback="",
            )
            on_failure_state = _normalise_slug(
                raw.get("on_failure_state") or raw.get("on_failure_state_key"),
                fallback="",
            )
            on_unknown_state = _normalise_slug(
                raw.get("on_unknown_state") or raw.get("on_unknown_state_key"),
                fallback="",
            )
            on_approval_required_state = _normalise_slug(
                raw.get("on_approval_required_state")
                or raw.get("on_approval_required_state_key"),
                fallback="",
            )
            on_break_state = _normalise_slug(
                raw.get("on_break_state") or raw.get("on_break_state_key"),
                fallback="",
            )
            on_continue_state = _normalise_slug(
                raw.get("on_continue_state") or raw.get("on_continue_state_key"),
                fallback="",
            )
            terminal = bool(raw.get("terminal", False))
            inputs_raw = raw.get("inputs")
            inputs = dict(inputs_raw) if isinstance(inputs_raw, Mapping) else {}
            prompt_contract_raw = raw.get("prompt_contract")
            prompt_contract = (
                dict(prompt_contract_raw)
                if isinstance(prompt_contract_raw, Mapping)
                else None
            )
            llm_policy_raw = raw.get("llm_policy")
            llm_policy = (
                dict(llm_policy_raw) if isinstance(llm_policy_raw, Mapping) else None
            )
            validation_policy_raw = raw.get("validation_policy")
            validation_policy = (
                dict(validation_policy_raw)
                if isinstance(validation_policy_raw, Mapping)
                else None
            )
            mutation_authority_raw = raw.get("mutation_authority")
            mutation_authority = (
                dict(mutation_authority_raw)
                if isinstance(mutation_authority_raw, Mapping)
                else None
            )
            action_concept_id = _clean_text(raw.get("action_concept_id")) or None
            writes_context_keys = [
                str(item).strip()
                for item in (raw.get("writes_context_keys") or [])
                if isinstance(item, str) and str(item).strip()
            ]
            tool_output_context_mappings = [
                {
                    str(key): value
                    for key, value in item.items()
                    if isinstance(key, str) and str(key).strip()
                }
                for item in (raw.get("tool_output_context_mappings") or [])
                if isinstance(item, Mapping)
            ]
            conditional_transitions: list[dict[str, Any]] = []
            raw_conditional_transitions = raw.get("conditional_transitions")
            if isinstance(raw_conditional_transitions, list):
                for transition in raw_conditional_transitions:
                    if not isinstance(transition, Mapping):
                        continue
                    target_state = _normalise_slug(
                        transition.get("to_state")
                        or transition.get("to_state_key")
                        or transition.get("to")
                        or transition.get("next"),
                        fallback="",
                    )
                    if not target_state:
                        continue
                    transition_row: dict[str, Any] = {"to_state": target_state}
                    reason = _clean_text(transition.get("reason"))
                    if reason:
                        transition_row["reason"] = reason
                    condition_spec = transition.get("condition_spec") or transition.get(
                        "condition"
                    )
                    if isinstance(condition_spec, Mapping):
                        transition_row["condition_spec"] = dict(condition_spec)
                    conditional_transitions.append(transition_row)
            rows.append(
                {
                    "state_key": state_key,
                    "step_concept_id": _normalise_concept_id(
                        f"#V#workflow_step_{workflow_slug}_{state_key}",
                        fallback_slug=f"workflow_step_{workflow_slug}_{state_key}",
                    ),
                    "action_id": action_id,
                    "action_concept_id": action_concept_id,
                    "subworkflow_id": subworkflow_id,
                    "execution_mode": execution_mode,
                    "next_state_key": next_state or None,
                    "on_true_state_key": on_true_state or None,
                    "on_false_state_key": on_false_state or None,
                    "on_failure_state_key": on_failure_state or None,
                    "on_unknown_state_key": on_unknown_state or None,
                    "on_approval_required_state_key": on_approval_required_state or None,
                    "on_break_state_key": on_break_state or None,
                    "on_continue_state_key": on_continue_state or None,
                    "terminal": terminal,
                    "inputs": _normalise_input_mapping(inputs),
                    "prompt_contract": prompt_contract,
                    "llm_policy": llm_policy,
                    "validation_policy": validation_policy,
                    "mutation_authority": mutation_authority,
                    "writes_context_keys": writes_context_keys,
                    "tool_output_context_mappings": tool_output_context_mappings,
                    "conditional_transitions": conditional_transitions,
                }
            )

    if not rows:
        marker_value = _clean_text(request_text[:160]) if request_text else "workflow_created"
        rows = [
            {
                "state_key": "record_request",
                "step_concept_id": _normalise_concept_id(
                    f"#V#workflow_step_{workflow_slug}_record_request",
                    fallback_slug=f"workflow_step_{workflow_slug}_record_request",
                ),
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "next_state_key": "completed",
                "on_true_state_key": None,
                "on_false_state_key": None,
                "on_failure_state_key": None,
                "on_unknown_state_key": None,
                "terminal": False,
                "inputs": {
                    "marker_key": "workflow_request_summary",
                    "marker_value": marker_value,
                },
            },
            {
                "state_key": "completed",
                "step_concept_id": _normalise_concept_id(
                    f"#V#workflow_step_{workflow_slug}_completed",
                    fallback_slug=f"workflow_step_{workflow_slug}_completed",
                ),
                "action_id": None,
                "next_state_key": None,
                "on_true_state_key": None,
                "on_false_state_key": None,
                "on_failure_state_key": None,
                "on_unknown_state_key": None,
                "terminal": True,
                "inputs": {},
            },
        ]

    state_keys = [row["state_key"] for row in rows]
    key_set = set(state_keys)
    for index, row in enumerate(rows):
        if (
            not row.get("terminal")
            and not row.get("action_id")
            and not row.get("subworkflow_id")
        ):
            row["action_id"] = WORKFLOW_CREATION_ACTION_EMIT_MARKER
            row["inputs"] = row.get("inputs") or {
                "marker_key": f"{row['state_key']}_marker",
                "marker_value": "completed",
            }
        if row.get("terminal"):
            row["next_state_key"] = None
            row["on_true_state_key"] = None
            row["on_false_state_key"] = None
            row["on_failure_state_key"] = None
            row["on_unknown_state_key"] = None
            row["on_approval_required_state_key"] = None
            row["on_break_state_key"] = None
            row["on_continue_state_key"] = None
            continue

        for transition_key in (
            "on_true_state_key",
            "on_false_state_key",
            "on_failure_state_key",
            "on_unknown_state_key",
            "on_approval_required_state_key",
            "on_break_state_key",
            "on_continue_state_key",
        ):
            target = row.get(transition_key)
            if not isinstance(target, str) or target not in key_set:
                row[transition_key] = None

        next_state_key = row.get("next_state_key")
        if isinstance(next_state_key, str) and next_state_key in key_set:
            continue
        if any(
            bool(row.get(transition_key))
            for transition_key in (
                "on_true_state_key",
                "on_false_state_key",
                "on_failure_state_key",
                "on_unknown_state_key",
                "on_approval_required_state_key",
                "on_break_state_key",
                "on_continue_state_key",
            )
        ):
            row["next_state_key"] = None
            continue
        if index + 1 < len(rows):
            row["next_state_key"] = rows[index + 1]["state_key"]
        else:
            row["terminal"] = True
            row["next_state_key"] = None

    if not any(bool(row.get("terminal")) for row in rows):
        rows[-1]["terminal"] = True
        rows[-1]["next_state_key"] = None

    return rows


def _infer_postcondition_probe(step_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    for row in step_rows:
        action_id = _clean_text(row.get("action_id"))
        if action_id != WORKFLOW_CREATION_ACTION_EMIT_MARKER:
            continue
        inputs = row.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        marker_key = _clean_text(inputs.get("marker_key"))
        marker_value = _clean_text(inputs.get("marker_value"))
        if marker_key and marker_value:
            return {marker_key: marker_value}
    return {}


def _normalise_workflow_spec(context: Mapping[str, Any]) -> dict[str, Any]:
    request_text = _extract_request_text(context)
    raw_spec = _extract_workflow_spec(context)
    template_resolution: dict[str, Any] = {}
    candidate_workflow_id = (
        _clean_text(raw_spec.get("workflow_id"))
        or _clean_text(context.get("target_workflow_id"))
    )
    if not candidate_workflow_id:
        stem = _normalise_slug(request_text, fallback="generated")
        if not stem.endswith("workflow"):
            stem = f"{stem}_workflow"
        candidate_workflow_id = f"#V#{stem}"
    workflow_id = _normalise_concept_id(
        candidate_workflow_id,
        fallback_slug="generated_workflow",
    )

    if not raw_spec:
        raw_spec, template_resolution = _resolve_workflow_template_spec(
            context=context,
            request_text=request_text,
            workflow_id=workflow_id,
        )

    workflow_name = _clean_text(
        raw_spec.get("name") or raw_spec.get("workflow_name")
    ) or _titleise(
        workflow_id[3:] if workflow_id.startswith("#V#") else workflow_id
    )
    workflow_description = _clean_text(
        raw_spec.get("description") or raw_spec.get("workflow_description")
    ) or request_text
    parent_type_id = _normalise_concept_id(
        raw_spec.get("parent_type_id")
        or context.get("parent_type_id")
        or DEFAULT_WORKFLOW_PARENT_TYPE_ID,
        fallback_slug="ai_workflow",
    )
    step_rows = _build_step_rows(
        workflow_id=workflow_id,
        raw_steps=raw_spec.get("steps"),
        request_text=request_text,
    )
    state_to_step_id = {
        str(row["state_key"]): str(row["step_concept_id"]) for row in step_rows
    }
    initial_state_key = str(step_rows[0]["state_key"])
    postcondition_probe_raw = raw_spec.get("postcondition_probe")
    postcondition_probe = (
        dict(postcondition_probe_raw)
        if isinstance(postcondition_probe_raw, Mapping)
        else {}
    )
    if not postcondition_probe:
        postcondition_probe = _infer_postcondition_probe(step_rows)

    required_effects_raw = raw_spec.get("required_effects")
    required_effects = [
        _clean_text(item)
        for item in (
            required_effects_raw if isinstance(required_effects_raw, list) else []
        )
        if _clean_text(item)
    ]
    if not required_effects and postcondition_probe:
        required_effects = [
            f"context:{key}={value}" for key, value in sorted(postcondition_probe.items())
        ]

    verification_inputs_raw = raw_spec.get("verification_inputs")
    verification_inputs: dict[str, Any] = {}
    if isinstance(verification_inputs_raw, Mapping):
        verification_inputs = {
            str(key): value
            for key, value in verification_inputs_raw.items()
            if isinstance(key, str)
        }
    text_relations = _normalise_text_relation_specs(raw_spec.get("text_relations"))

    return {
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "workflow_description": workflow_description,
        "parent_type_id": parent_type_id,
        "steps": step_rows,
        "state_to_step_id": state_to_step_id,
        "initial_state_key": initial_state_key,
        "required_effects": required_effects,
        "postcondition_probe": postcondition_probe,
        "verification_inputs": verification_inputs,
        "text_relations": text_relations,
        "synthesis_policy_text": _clean_text(raw_spec.get("synthesis_policy_text")),
        "template_resolution": template_resolution,
    }


def _load_concept(concept_id: str) -> dict[str, Any] | None:
    try:
        return concept_service.get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None


def _ensure_type_concept(concept_id: str, *, name: str) -> None:
    if _load_concept(concept_id) is not None:
        return
    concept_service.create_concept(
        name=name,
        concept_id=concept_id,
        create_as_instance=False,
    )


def _ensure_predicate_concept(predicate_id: str) -> None:
    if not predicate_id or not predicate_id.startswith("#V#"):
        return
    if _load_concept(predicate_id) is not None:
        return

    _ensure_type_concept(DEFAULT_PREDICATE_TYPE_ID, name="Predicate")
    predicate_name = _titleise(
        predicate_id[3:] if predicate_id.startswith("#V#") else predicate_id
    )
    concept_service.create_concept(
        name=predicate_name,
        concept_id=predicate_id,
        parent_concept_ids=[DEFAULT_PREDICATE_TYPE_ID],
        create_as_instance=True,
    )


def _ensure_instance_concept(
    *,
    concept_id: str,
    name: str,
    parent_type_id: str,
    description: str | None = None,
) -> None:
    existing = _load_concept(concept_id)
    if existing is None:
        concept_service.create_concept(
            name=name,
            concept_id=concept_id,
            parent_concept_ids=[parent_type_id],
            create_as_instance=True,
        )
    else:
        rels = dict(existing.get("relationships") or {})
        existing_types = rels.get("is_an_instance_of")
        if isinstance(existing_types, list):
            type_ids = [str(item) for item in existing_types if isinstance(item, str)]
        elif isinstance(existing_types, str):
            type_ids = [existing_types]
        else:
            type_ids = []
        if parent_type_id not in type_ids:
            type_ids.append(parent_type_id)
            rels["is_an_instance_of"] = type_ids
            concept_service.update_concept(concept_id, {"relationships": rels})

    if isinstance(description, str) and description.strip():
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text=description.strip(),
            lang="en-NZ",
        )


def _add_contract_output(
    *,
    outputs: Mapping[str, Any] | None,
    validated_type_name: str,
) -> dict[str, Any]:
    payload = dict(outputs) if isinstance(outputs, Mapping) else {}
    payload[WORKFLOW_CONTEXT_KEY_VALIDATED_TYPE_NAME] = validated_type_name
    return payload


def _write_workflow_publication_lifecycle(
    *,
    workflow_id: str,
    phase: str,
    published: bool,
    validation_passed: bool | None = None,
    postconditions_verified: bool | None = None,
    optional_test_instance_id: str | None = None,
    last_error: str | None = None,
) -> None:
    phase_text = _clean_text(phase) or "draft"
    payload: dict[str, Any] = {
        "schema_version": WORKFLOW_PUBLICATION_LIFECYCLE_SCHEMA_VERSION,
        "phase": phase_text,
        "published": bool(published),
    }
    if validation_passed is not None:
        payload["validation_passed"] = bool(validation_passed)
    if postconditions_verified is not None:
        payload["postconditions_verified"] = bool(postconditions_verified)
    if isinstance(optional_test_instance_id, str) and optional_test_instance_id.strip():
        payload["optional_test_instance_id"] = optional_test_instance_id.strip()
    if isinstance(last_error, str) and last_error.strip():
        payload["last_error"] = last_error.strip()

    concept_service.update_concept(
        workflow_id,
        {"concept_data.workflow_publication_lifecycle": payload},
    )
    upsert_text_for_concept(
        subject_concept_id=workflow_id,
        predicate=WORKFLOW_PUBLICATION_LIFECYCLE_TEXT_PREDICATE,
        text=json.dumps(payload, sort_keys=True),
        lang="en-NZ",
    )


def _coerce_string_sequence(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = _clean_text(value)
        if not cleaned:
            return []
        if cleaned.startswith("["):
            try:
                parsed = json.loads(cleaned)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                return _coerce_string_sequence(parsed)
        if "," in cleaned:
            return _coerce_string_sequence(
                [item.strip() for item in cleaned.split(",") if item.strip()]
            )
        return [cleaned]
    if not isinstance(value, Iterable) or isinstance(value, (bytes, bytearray, Mapping)):
        return []
    results: list[str] = []
    seen: set[str] = set()
    for item in value:
        cleaned = _clean_text(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        results.append(cleaned)
    return results


def _normalise_workflow_candidate_rows(
    rows: Any,
    *,
    exclude_ids: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    payload: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        workflow_id = _clean_text(item.get("concept_id") or item.get("workflow_id"))
        if not workflow_id or workflow_id in exclude_ids:
            continue
        row = {str(key): value for key, value in item.items() if isinstance(key, str)}
        row["concept_id"] = workflow_id
        payload.append(row)
    return payload


def _handle_discover_existing_workflows(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs) if isinstance(request.inputs, Mapping) else {}
    request_text = _extract_request_text({**request.data, **inputs})
    if not request_text:
        return WorkflowActionResult(
            status="failed",
            error="workflow_authoring_request_text_missing",
        )

    max_results_raw = inputs.get("max_results", request.data.get("max_results", 8))
    try:
        max_results = max(1, min(12, int(max_results_raw)))
    except (TypeError, ValueError):
        max_results = 8

    exclude_ids = set(
        _coerce_string_sequence(
            inputs.get("exclude_workflow_ids") or request.data.get("exclude_workflow_ids")
        )
    )
    exclude_ids.update(
        _coerce_string_sequence(
            request.data.get("workflow_authoring_exclude_workflow_ids")
        )
    )

    discovery_result = discover_workflows(
        request_text,
        max_results=max_results,
        allow_non_executable=True,
    )
    discovery_payload = discovery_result.to_dict()
    candidate_rows = _normalise_workflow_candidate_rows(
        discovery_payload.get("candidates") or discovery_payload.get("matches"),
        exclude_ids=exclude_ids,
    )
    routing_rows = _normalise_workflow_candidate_rows(
        discovery_payload.get("matches"),
        exclude_ids=exclude_ids,
    )
    discovery_payload["candidates"] = candidate_rows
    discovery_payload["matches"] = routing_rows
    discovery_payload["candidate_count"] = len(candidate_rows)
    discovery_payload["match_count"] = len(routing_rows)
    discovery_payload["excluded_workflow_ids"] = sorted(exclude_ids)

    outputs = {
        "workflow_authoring_request_text": request_text,
        "workflow_authoring_discovery_result": discovery_payload,
        "workflow_authoring_candidate_workflows": candidate_rows,
        "workflow_authoring_candidate_workflow_ids": [
            str(item.get("concept_id") or "").strip()
            for item in candidate_rows
            if isinstance(item, Mapping)
            and isinstance(item.get("concept_id"), str)
            and str(item.get("concept_id")).strip()
        ],
        "workflow_authoring_candidate_count": len(candidate_rows),
        "workflow_authoring_prompt_contract": build_workflow_authoring_prompt_contract(),
        "workflow_authoring_prompt_health": (
            get_workflow_authoring_prompt_health_status()
        ),
    }
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_identify_need(request: WorkflowActionRequest) -> WorkflowActionResult:
    spec = _normalise_workflow_spec(request.data)
    parent_type_id = str(spec.get("parent_type_id") or DEFAULT_WORKFLOW_PARENT_TYPE_ID)
    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_request_text": _extract_request_text(request.data),
            "required_effects_declared": bool(spec.get("required_effects")),
            "parent_concept_id_used": parent_type_id,
        },
        validated_type_name=parent_type_id,
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_design_structure(request: WorkflowActionRequest) -> WorkflowActionResult:
    spec = _normalise_workflow_spec(request.data)
    parent_type_id = str(spec.get("parent_type_id") or DEFAULT_WORKFLOW_PARENT_TYPE_ID)
    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_creation_step_count": len(spec.get("steps") or []),
        },
        validated_type_name=parent_type_id,
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_create_workflow_type(request: WorkflowActionRequest) -> WorkflowActionResult:
    spec = _normalise_workflow_spec(request.data)
    parent_type_id = str(spec.get("parent_type_id") or DEFAULT_WORKFLOW_PARENT_TYPE_ID)
    workflow_id = str(spec["workflow_id"])
    workflow_name = str(spec["workflow_name"])
    workflow_description = _clean_text(spec.get("workflow_description"))

    _ensure_type_concept(parent_type_id, name=_titleise(parent_type_id))
    _ensure_instance_concept(
        concept_id=workflow_id,
        name=workflow_name,
        parent_type_id=parent_type_id,
        description=workflow_description or None,
    )
    _write_workflow_publication_lifecycle(
        workflow_id=workflow_id,
        phase="draft",
        published=False,
        validation_passed=False,
        postconditions_verified=False,
    )

    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_concept_id": workflow_id,
            "parent_concept_id_used": parent_type_id,
        },
        validated_type_name=parent_type_id,
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_ensure_workflow_identity(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    return _handle_create_workflow_type(request)


def _handle_create_step_concepts(request: WorkflowActionRequest) -> WorkflowActionResult:
    spec = _normalise_workflow_spec(request.data)
    parent_type_id = str(spec.get("parent_type_id") or DEFAULT_WORKFLOW_PARENT_TYPE_ID)
    workflow_name = _clean_text(spec.get("workflow_name")) or "Generated Workflow"
    workflow_id = str(spec["workflow_id"])

    _ensure_type_concept(DEFAULT_WORKFLOW_STEP_TYPE_ID, name="Workflow Step")
    created_step_ids: list[str] = []
    for row in spec.get("steps") or []:
        if not isinstance(row, Mapping):
            continue
        step_id = str(row.get("step_concept_id") or "").strip()
        state_key = str(row.get("state_key") or "step")
        if not step_id:
            continue
        _ensure_instance_concept(
            concept_id=step_id,
            name=f"{workflow_name} {state_key}",
            parent_type_id=DEFAULT_WORKFLOW_STEP_TYPE_ID,
            description=f"Step '{state_key}' in workflow {workflow_id}.",
        )
        created_step_ids.append(step_id)

    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_step_concept_ids": created_step_ids,
        },
        validated_type_name=parent_type_id,
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_materialise_workflow_definition(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    from .. import workflow_concept_authority_service as workflow_authority_service

    spec = _normalise_workflow_spec(request.data)
    parent_type_id = str(spec.get("parent_type_id") or DEFAULT_WORKFLOW_PARENT_TYPE_ID)
    workflow_id = str(spec["workflow_id"])
    workflow_name = str(spec["workflow_name"])
    workflow_description = _clean_text(spec.get("workflow_description"))

    _ensure_type_concept(parent_type_id, name=_titleise(parent_type_id))
    _ensure_instance_concept(
        concept_id=workflow_id,
        name=workflow_name,
        parent_type_id=parent_type_id,
        description=workflow_description or None,
    )
    _write_workflow_publication_lifecycle(
        workflow_id=workflow_id,
        phase="draft",
        published=False,
        validation_passed=False,
        postconditions_verified=False,
    )

    try:
        definition = build_workflow_definition_from_authoring_spec(spec)
        publication_report = workflow_authority_service.publish_workflow_definition_from_definition(
            definition=definition,
            create_missing=True,
            purpose=workflow_description or workflow_name,
        )
    except Exception as exc:
        return WorkflowActionResult(
            status="failed",
            error=f"workflow_definition_materialisation_failed:{workflow_id}:{exc}",
            outputs={
                "workflow_creation_spec": spec,
                "workflow_concept_id": workflow_id,
                "workflow_structure_written": False,
            },
        )

    errors_by_workflow_id = publication_report.get("errors_by_workflow_id") or {}
    workflow_error = (
        str(errors_by_workflow_id.get(workflow_id) or "").strip()
        if isinstance(errors_by_workflow_id, Mapping)
        else ""
    )
    validation_failures = publication_report.get("validation_failures_by_workflow_id") or {}
    validation_failure = (
        validation_failures.get(workflow_id)
        if isinstance(validation_failures, Mapping)
        else None
    )
    if workflow_error or validation_failure:
        outputs = _add_contract_output(
            outputs={
                "workflow_creation_spec": spec,
                "workflow_concept_id": workflow_id,
                "workflow_structure_written": False,
                "workflow_graph_publication_report": publication_report,
            },
            validated_type_name=parent_type_id,
        )
        return WorkflowActionResult(
            status="failed",
            error=workflow_error
            or f"workflow_definition_materialisation_validation_failed:{workflow_id}",
            outputs=outputs,
        )

    relation_specs = tuple(spec.get("text_relations") or ())
    if relation_specs:
        try:
            workflow_authority_service.upsert_seed_bundle_text_relations(
                subject_concept_id=workflow_id,
                relation_specs=relation_specs,
                workflow_id=workflow_id,
                source_tag="JVNAUTOSCI-1704",
                managed_by="workflow_creation_workflow",
            )
        except Exception as exc:
            outputs = _add_contract_output(
                outputs={
                    "workflow_creation_spec": spec,
                    "workflow_concept_id": workflow_id,
                    "workflow_structure_written": False,
                    "workflow_graph_publication_report": publication_report,
                },
                validated_type_name=parent_type_id,
            )
            return WorkflowActionResult(
                status="failed",
                error=(
                    "workflow_definition_text_relation_materialisation_failed:"
                    f"{workflow_id}:{exc}"
                ),
                outputs=outputs,
            )

    step_ids = [
        str(row.get("step_concept_id") or "").strip()
        for row in (spec.get("steps") or [])
        if isinstance(row, Mapping) and str(row.get("step_concept_id") or "").strip()
    ]
    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_structure_written": True,
            "workflow_concept_id": workflow_id,
            "workflow_step_concept_ids": step_ids,
            "workflow_action_contract_concept_ids": list(
                dict.fromkeys(
                    [
                        str(item).strip()
                        for item in (
                            publication_report.get("created_action_concept_ids") or []
                        )
                        if isinstance(item, str) and str(item).strip()
                    ]
                )
            ),
            "workflow_graph_publication_report": publication_report,
        },
        validated_type_name=parent_type_id,
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_establish_relationships(request: WorkflowActionRequest) -> WorkflowActionResult:
    from .. import workflow_concept_authority_service as workflow_authority_service

    spec = _normalise_workflow_spec(request.data)
    parent_type_id = str(spec.get("parent_type_id") or DEFAULT_WORKFLOW_PARENT_TYPE_ID)
    workflow_id = str(spec["workflow_id"])
    state_to_step_id = {
        str(key): str(value)
        for key, value in dict(spec.get("state_to_step_id") or {}).items()
        if isinstance(key, str) and isinstance(value, str)
    }
    initial_state_key = str(spec.get("initial_state_key") or "")
    initial_step_id = state_to_step_id.get(initial_state_key)
    if not initial_step_id:
        return WorkflowActionResult(
            status="failed",
            error="workflow_creation_missing_initial_step",
        )

    workflow_doc = _load_concept(workflow_id)
    if workflow_doc is None:
        return WorkflowActionResult(
            status="failed",
            error=f"workflow_creation_missing_workflow_concept:{workflow_id}",
        )

    workflow_relationships = dict(workflow_doc.get("relationships") or {})
    for key in (
        WORKFLOW_GRAPH_PREDICATE_HAS_INITIAL_STEP,
        "hasInitialStep",
        "has_initial_step",
        WORKFLOW_GRAPH_PREDICATE_HAS_STEP,
        "hasStep",
        "has_step",
    ):
        workflow_relationships.pop(key, None)
    ordered_step_ids = [
        state_to_step_id.get(str(row.get("state_key") or ""))
        for row in (spec.get("steps") or [])
        if isinstance(row, Mapping)
    ]
    ordered_step_ids = [item for item in ordered_step_ids if isinstance(item, str)]
    workflow_relationships[WORKFLOW_GRAPH_PREDICATE_HAS_INITIAL_STEP] = [
        initial_step_id
    ]
    workflow_relationships[WORKFLOW_GRAPH_PREDICATE_HAS_STEP] = ordered_step_ids
    concept_service.update_concept(workflow_id, {"relationships": workflow_relationships})

    graph_keys = {
        WORKFLOW_GRAPH_PREDICATE_INVOKES_ACTION,
        "invokesAction",
        "workflow_step_invokes_tool",
        WORKFLOW_GRAPH_PREDICATE_HAS_INPUT_MAP,
        "hasInputMap",
        "has_input_map",
        WORKFLOW_GRAPH_PREDICATE_NEXT_STEP,
        "nextStep",
        "next_step",
        WORKFLOW_GRAPH_PREDICATE_ON_TRUE_NEXT_STEP,
        "onTrueNextStep",
        WORKFLOW_GRAPH_PREDICATE_ON_FALSE_NEXT_STEP,
        "onFalseNextStep",
        WORKFLOW_GRAPH_PREDICATE_ON_FAILURE_NEXT_STEP,
        "onFailureNextStep",
        WORKFLOW_GRAPH_PREDICATE_ON_UNKNOWN_NEXT_STEP,
        "onUnknownNextStep",
    }
    action_contract_concept_ids: list[str] = []

    for row in spec.get("steps") or []:
        if not isinstance(row, Mapping):
            continue
        step_id = str(row.get("step_concept_id") or "").strip()
        if not step_id:
            continue
        step_doc = _load_concept(step_id)
        if step_doc is None:
            return WorkflowActionResult(
                status="failed",
                error=f"workflow_creation_missing_step_concept:{step_id}",
            )
        relationships = dict(step_doc.get("relationships") or {})
        for key in graph_keys:
            relationships.pop(key, None)

        action_id = _clean_text(row.get("action_id"))
        if action_id:
            action_target = action_id
            if action_id in WORKFLOW_CREATION_ACTION_CONTRACT_BY_ACTION_ID:
                (
                    resolved_action_concept_id,
                    _created_action_concept,
                    action_concept_error,
                ) = workflow_authority_service.ensure_workflow_action_contract_concept(
                    action_id=action_id
                )
                if action_concept_error:
                    return WorkflowActionResult(
                        status="failed",
                        error=(
                            "workflow_creation_action_contract_failed:"
                            f"{step_id}:{action_id}:{action_concept_error}"
                        ),
                    )
                if resolved_action_concept_id:
                    action_target = resolved_action_concept_id
                    action_contract_concept_ids.append(resolved_action_concept_id)
            relationships[WORKFLOW_GRAPH_PREDICATE_INVOKES_ACTION] = [
                workflow_creation_action_target(action_target) or action_target
            ]

        inputs = row.get("inputs")
        if isinstance(inputs, Mapping) and inputs:
            relationships[WORKFLOW_GRAPH_PREDICATE_HAS_INPUT_MAP] = [
                f"{key}={value}"
                for key, value in sorted(inputs.items(), key=lambda item: item[0])
            ]

        next_state_key = _clean_text(row.get("next_state_key"))
        if next_state_key:
            target_step_id = state_to_step_id.get(next_state_key)
            if target_step_id:
                relationships[WORKFLOW_GRAPH_PREDICATE_NEXT_STEP] = [target_step_id]

        on_true_state_key = _clean_text(row.get("on_true_state_key"))
        if on_true_state_key:
            target_step_id = state_to_step_id.get(on_true_state_key)
            if target_step_id:
                relationships[WORKFLOW_GRAPH_PREDICATE_ON_TRUE_NEXT_STEP] = [
                    target_step_id
                ]

        on_false_state_key = _clean_text(row.get("on_false_state_key"))
        if on_false_state_key:
            target_step_id = state_to_step_id.get(on_false_state_key)
            if target_step_id:
                relationships[WORKFLOW_GRAPH_PREDICATE_ON_FALSE_NEXT_STEP] = [
                    target_step_id
                ]

        on_failure_state_key = _clean_text(row.get("on_failure_state_key"))
        if on_failure_state_key:
            target_step_id = state_to_step_id.get(on_failure_state_key)
            if target_step_id:
                relationships[WORKFLOW_GRAPH_PREDICATE_ON_FAILURE_NEXT_STEP] = [
                    target_step_id
                ]

        on_unknown_state_key = _clean_text(row.get("on_unknown_state_key"))
        if on_unknown_state_key:
            target_step_id = state_to_step_id.get(on_unknown_state_key)
            if target_step_id:
                relationships[WORKFLOW_GRAPH_PREDICATE_ON_UNKNOWN_NEXT_STEP] = [
                    target_step_id
                ]

        concept_service.update_concept(step_id, {"relationships": relationships})

    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_structure_written": True,
            "workflow_concept_id": workflow_id,
            "workflow_action_contract_concept_ids": list(
                dict.fromkeys(action_contract_concept_ids)
            ),
        },
        validated_type_name=parent_type_id,
    )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_validate_workflow_definition(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    return _handle_verify_discoverability(request)


def _handle_extract_existing_workflow_spec(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs) if isinstance(request.inputs, Mapping) else {}
    target_workflow_id = _clean_text(
        inputs.get("target_workflow_id")
        or request.data.get("target_workflow_id")
        or request.data.get("workflow_authoring_target_workflow_id")
    )
    if not target_workflow_id:
        return WorkflowActionResult(
            status="failed",
            error="workflow_authoring_target_workflow_id_missing",
        )

    definition = load_workflow_definition_from_vontology(target_workflow_id)
    if definition is None:
        return WorkflowActionResult(
            status="failed",
            error=f"workflow_authoring_existing_workflow_not_found:{target_workflow_id}",
        )

    try:
        authoring_spec = serialise_workflow_definition_to_authoring_spec(definition)
    except Exception as exc:
        return WorkflowActionResult(
            status="failed",
            error=(
                "workflow_authoring_existing_workflow_serialisation_failed:"
                f"{target_workflow_id}:{exc}"
            ),
        )

    outputs = {
        "target_workflow_id": target_workflow_id,
        "existing_workflow_spec": authoring_spec,
        "existing_workflow_state_count": len(definition.states or {}),
        "existing_workflow_action_ids": list(collect_workflow_action_ids(definition)),
        "workflow_authoring_prompt_contract": build_workflow_authoring_prompt_contract(),
        "workflow_authoring_prompt_health": (
            get_workflow_authoring_prompt_health_status()
        ),
    }
    return WorkflowActionResult(status="success", outputs=outputs)


def _gateway_fallback_action(request: WorkflowActionRequest) -> WorkflowActionResult:
    gateway = request.environment.gateway
    tool_name = _clean_text(request.action_id)
    if gateway is None or not getattr(gateway, "enabled", False):
        return WorkflowActionResult(
            status="failed",
            error=f"gateway_unavailable_for:{tool_name}",
        )
    payload = dict(request.inputs) if isinstance(request.inputs, Mapping) else {}
    user_namespace = _clean_text(request.environment.user_namespace)
    if user_namespace:
        payload.setdefault("namespace", user_namespace)
    try:
        available_tool_names = tuple(gateway.describe_methods().keys())
        resolved_tool_name = (
            resolve_internal_mcp_tool_name(
                tool_name,
                available_tool_names=available_tool_names,
            )
            or tool_name
        )
        method_definition = gateway.get_method_definition(resolved_tool_name)
        blocked_result = enforce_workflow_mcp_write_guardrails(
            request=request,
            resolved_tool_name=resolved_tool_name,
            method_definition=method_definition,
        )
        if blocked_result is not None:
            return blocked_result
        result = gateway.invoke(resolved_tool_name, payload)
        return WorkflowActionResult(
            status="success",
            outputs={
                "mcp_tool": resolved_tool_name,
                "mcp_result": result.payload,
                "mcp_duration_ms": getattr(result, "duration_ms", None),
                "result": result.payload,
            },
            duration_ms=getattr(result, "duration_ms", None),
        )
    except Exception as exc:
        return WorkflowActionResult(
            status="failed",
            error=f"mcp_invoke_failed:{tool_name}:{exc}",
        )


def _handle_resolve_scholarly_authors(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    paper_concept_id = _clean_text(
        request.inputs.get("paper_concept_id")
        if isinstance(request.inputs, Mapping)
        else ""
    ) or _clean_text(request.data.get("paper_concept_id"))

    person_type_id = _clean_text(
        request.inputs.get("person_type_id")
        if isinstance(request.inputs, Mapping)
        else ""
    ) or _clean_text(request.data.get("person_type_id"))
    if not person_type_id:
        person_type_id = DEFAULT_PERSON_TYPE_ID

    authored_by_predicate_id = _clean_text(
        request.inputs.get("scholarly_author_predicate_id")
        if isinstance(request.inputs, Mapping)
        else ""
    ) or _clean_text(request.data.get("scholarly_author_predicate_id"))
    if not authored_by_predicate_id:
        authored_by_predicate_id = DEFAULT_AUTHORED_BY_PREDICATE_ID

    author_names = _extract_author_names(request.data)
    if (
        not author_names
        and isinstance(request.inputs, Mapping)
        and "author_names" in request.inputs
    ):
        author_names = _extract_person_names_from_value(request.inputs.get("author_names"))

    _ensure_type_concept(
        person_type_id,
        name="Person" if person_type_id == DEFAULT_PERSON_TYPE_ID else _titleise(person_type_id),
    )
    _ensure_predicate_concept(authored_by_predicate_id)

    paper_exists = bool(_load_concept(paper_concept_id)) if paper_concept_id else False
    reused_author_ids: list[str] = []
    created_author_ids: list[str] = []
    resolved_author_ids: list[str] = []
    ambiguous_author_names: list[str] = []
    links_written = 0

    for author_name in author_names:
        verified_ids = _find_verified_person_concept_ids(
            person_name=author_name,
            person_type_id=person_type_id,
        )
        if len(verified_ids) > 1:
            ambiguous_author_names.append(author_name)

        existing_id = verified_ids[0] if len(verified_ids) == 1 else None
        if existing_id:
            person_concept_id = existing_id
            reused_author_ids.append(person_concept_id)
        else:
            person_concept_id = _create_person_concept(
                person_name=author_name,
                person_type_id=person_type_id,
            )
            created_author_ids.append(person_concept_id)

        resolved_author_ids.append(person_concept_id)
        if paper_exists:
            relationship_result = add_relationship(
                source_id=paper_concept_id,
                predicate=authored_by_predicate_id,
                target=person_concept_id,
            )
            if bool(relationship_result.get("success")):
                links_written += 1

    outputs: dict[str, Any] = {
        "author_names_processed": author_names,
        "resolved_author_concept_ids": list(dict.fromkeys(resolved_author_ids)),
        "reused_author_concept_ids": list(dict.fromkeys(reused_author_ids)),
        "created_author_concept_ids": list(dict.fromkeys(created_author_ids)),
        "ambiguous_author_names": ambiguous_author_names,
        "author_resolution_completed": True,
        "author_resolution_mode": "exact_name_unique_match_or_create",
        "scholarly_author_predicate_id": authored_by_predicate_id,
        "paper_authorship_links_written": links_written,
    }
    if paper_concept_id and not paper_exists:
        outputs["paper_authorship_links_skipped_reason"] = (
            "paper_concept_not_found"
        )
    elif not paper_concept_id:
        outputs["paper_authorship_links_skipped_reason"] = (
            "paper_concept_id_missing"
        )

    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_resolve_phd_student_candidate(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    person_type_id = _clean_text(
        request.inputs.get("person_type_id") if isinstance(request.inputs, Mapping) else ""
    ) or _clean_text(request.data.get("person_type_id"))
    if not person_type_id:
        person_type_id = DEFAULT_PERSON_TYPE_ID
    _ensure_type_concept(
        person_type_id,
        name="Person" if person_type_id == DEFAULT_PERSON_TYPE_ID else _titleise(person_type_id),
    )

    merged_context = dict(request.data)
    if isinstance(request.inputs, Mapping):
        for key, value in request.inputs.items():
            if key not in merged_context:
                merged_context[key] = value

    profile = _extract_phd_student_profile(merged_context)
    student_name = _normalise_person_name(profile.get("student_name"))
    if not student_name:
        return WorkflowActionResult(
            status="failed",
            error="phd_student_candidate_name_missing:expected_phd_student_name_or_description",
            outputs={
                "phd_student_candidate_resolved": False,
                "phd_student_failure_diagnostics": {
                    "error_code": "phd_student_candidate_name_missing",
                    "reason": "No PhD student name could be resolved from workflow inputs.",
                    "expected_fields": [
                        "phd_student_name",
                        "student_name",
                        "phd_student_description",
                    ],
                },
            },
        )

    verified_ids = _find_verified_person_concept_ids(
        person_name=student_name,
        person_type_id=person_type_id,
    )
    if len(verified_ids) > 1:
        return WorkflowActionResult(
            status="failed",
            error=(
                f"phd_student_candidate_ambiguous:{student_name}:"
                f"{','.join(verified_ids)}"
            ),
            outputs={
                "phd_student_candidate_resolved": False,
                "phd_student_name": student_name,
                "phd_student_ambiguous_candidate_ids": verified_ids,
                "phd_student_failure_diagnostics": {
                    "error_code": "phd_student_candidate_ambiguous",
                    "reason": "Multiple existing person concepts match the requested PhD student name.",
                    "student_name": student_name,
                    "ambiguous_candidate_ids": verified_ids,
                    "resolution_hint": (
                        "Provide phd_student_concept_id or a more specific student name."
                    ),
                },
            },
        )

    reused_existing = len(verified_ids) == 1
    student_concept_id = (
        verified_ids[0]
        if reused_existing
        else _create_person_concept(
            person_name=student_name,
            person_type_id=person_type_id,
        )
    )
    return WorkflowActionResult(
        status="success",
        outputs={
            "phd_student_candidate_resolved": True,
            "phd_student_name": student_name,
            "phd_student_concept_id": student_concept_id,
            "phd_student_candidate_reused": reused_existing,
            "supervisor_names": list(profile.get("supervisor_names") or []),
            "research_topic": _clean_text(profile.get("research_topic")),
            "institution": _clean_text(profile.get("institution")),
            "phd_student_source_text": _clean_text(profile.get("source_text")),
            "phd_student_resolution_mode": "exact_name_unique_match_or_create",
        },
    )


def _handle_assert_phd_student_relationships(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    student_concept_id = _clean_text(request.data.get("phd_student_concept_id"))
    student_name = _normalise_person_name(request.data.get("phd_student_name"))
    if not student_concept_id:
        return WorkflowActionResult(
            status="failed",
            error="phd_student_relationships_missing_student_concept_id",
            outputs={
                "phd_student_relationships_asserted": False,
                "phd_student_failure_diagnostics": {
                    "error_code": "phd_student_relationships_missing_student_concept_id",
                    "reason": "PhD student relationship assertion requires phd_student_concept_id.",
                },
            },
        )

    person_type_id = _clean_text(
        request.inputs.get("person_type_id") if isinstance(request.inputs, Mapping) else ""
    ) or _clean_text(request.data.get("person_type_id"))
    if not person_type_id:
        person_type_id = DEFAULT_PERSON_TYPE_ID

    student_type_id = _clean_text(
        request.inputs.get("student_type_id") if isinstance(request.inputs, Mapping) else ""
    ) or _clean_text(request.data.get("student_type_id"))
    if not student_type_id:
        student_type_id = DEFAULT_STUDENT_TYPE_ID

    phd_student_type_id = _clean_text(
        request.inputs.get("phd_student_type_id")
        if isinstance(request.inputs, Mapping)
        else ""
    ) or _clean_text(request.data.get("phd_student_type_id"))
    if not phd_student_type_id:
        phd_student_type_id = DEFAULT_PHD_STUDENT_TYPE_ID

    research_topic_type_id = _clean_text(
        request.inputs.get("research_topic_type_id")
        if isinstance(request.inputs, Mapping)
        else ""
    ) or _clean_text(request.data.get("research_topic_type_id"))
    if not research_topic_type_id:
        research_topic_type_id = DEFAULT_RESEARCH_TOPIC_TYPE_ID

    supervised_by_predicate_id = _clean_text(
        request.inputs.get("supervised_by_predicate_id")
        if isinstance(request.inputs, Mapping)
        else ""
    ) or _clean_text(request.data.get("supervised_by_predicate_id"))
    if not supervised_by_predicate_id:
        supervised_by_predicate_id = DEFAULT_SUPERVISED_BY_PREDICATE_ID

    researches_predicate_id = _clean_text(
        request.inputs.get("researches_predicate_id")
        if isinstance(request.inputs, Mapping)
        else ""
    ) or _clean_text(request.data.get("researches_predicate_id"))
    if not researches_predicate_id:
        researches_predicate_id = DEFAULT_RESEARCHES_PREDICATE_ID

    _ensure_type_concept(
        person_type_id,
        name="Person" if person_type_id == DEFAULT_PERSON_TYPE_ID else _titleise(person_type_id),
    )
    _ensure_type_concept(
        student_type_id,
        name="Student" if student_type_id == DEFAULT_STUDENT_TYPE_ID else _titleise(student_type_id),
    )
    _ensure_type_concept(
        phd_student_type_id,
        name=(
            "PhD Student"
            if phd_student_type_id == DEFAULT_PHD_STUDENT_TYPE_ID
            else _titleise(phd_student_type_id)
        ),
    )
    _ensure_type_concept(
        research_topic_type_id,
        name=(
            "Research Topic"
            if research_topic_type_id == DEFAULT_RESEARCH_TOPIC_TYPE_ID
            else _titleise(research_topic_type_id)
        ),
    )
    _ensure_predicate_concept(supervised_by_predicate_id)
    _ensure_predicate_concept(researches_predicate_id)

    student_display_name = student_name or _titleise(student_concept_id)
    _ensure_instance_concept(
        concept_id=student_concept_id,
        name=student_display_name,
        parent_type_id=person_type_id,
    )
    _ensure_instance_concept(
        concept_id=student_concept_id,
        name=student_display_name,
        parent_type_id=student_type_id,
    )
    _ensure_instance_concept(
        concept_id=student_concept_id,
        name=student_display_name,
        parent_type_id=phd_student_type_id,
    )

    supervisor_names = _extract_person_names_from_value(request.data.get("supervisor_names"))
    if not supervisor_names and isinstance(request.inputs, Mapping):
        supervisor_names = _extract_person_names_from_value(
            request.inputs.get("supervisor_names")
        )
    supervisor_matches: dict[str, list[str]] = {}
    ambiguous_supervisor_names: list[str] = []
    for supervisor_name in supervisor_names:
        matches = _find_verified_person_concept_ids(
            person_name=supervisor_name,
            person_type_id=person_type_id,
        )
        supervisor_matches[supervisor_name] = matches
        if len(matches) > 1:
            ambiguous_supervisor_names.append(supervisor_name)
    if ambiguous_supervisor_names:
        return WorkflowActionResult(
            status="failed",
            error=(
                "phd_student_supervisor_ambiguous:"
                + ";".join(
                    f"{name}=>{','.join(supervisor_matches.get(name) or [])}"
                    for name in ambiguous_supervisor_names
                )
            ),
            outputs={
                "phd_student_relationships_asserted": False,
                "phd_student_supervisor_ambiguities": ambiguous_supervisor_names,
                "phd_student_failure_diagnostics": {
                    "error_code": "phd_student_supervisor_ambiguous",
                    "reason": (
                        "Supervisor resolution returned multiple matching person concepts."
                    ),
                    "ambiguous_supervisors": ambiguous_supervisor_names,
                    "matches_by_supervisor": {
                        key: value for key, value in supervisor_matches.items() if len(value) > 1
                    },
                    "resolution_hint": (
                        "Provide explicit supervisor concept IDs or unique supervisor names."
                    ),
                },
            },
        )

    reused_supervisor_ids: list[str] = []
    created_supervisor_ids: list[str] = []
    supervisor_concept_ids: list[str] = []
    supervisor_links_written = 0
    for supervisor_name in supervisor_names:
        matches = supervisor_matches.get(supervisor_name) or []
        supervisor_concept_id = (
            matches[0]
            if len(matches) == 1
            else _create_person_concept(
                person_name=supervisor_name,
                person_type_id=person_type_id,
            )
        )
        if len(matches) == 1:
            reused_supervisor_ids.append(supervisor_concept_id)
        else:
            created_supervisor_ids.append(supervisor_concept_id)
        supervisor_concept_ids.append(supervisor_concept_id)
        relation_result = add_relationship(
            source_id=student_concept_id,
            predicate=supervised_by_predicate_id,
            target=supervisor_concept_id,
        )
        if bool(relation_result.get("success")):
            supervisor_links_written += 1

    research_topic = _clean_text(request.data.get("research_topic"))
    research_topic_concept_id = ""
    research_topic_reused_existing = False
    research_topic_link_written = False
    if research_topic:
        try:
            (
                research_topic_concept_id,
                research_topic_reused_existing,
            ) = _resolve_or_create_research_topic_concept_id(
                research_topic=research_topic,
                research_topic_type_id=research_topic_type_id,
            )
        except RuntimeError:
            return WorkflowActionResult(
                status="failed",
                error=f"phd_student_research_topic_ambiguous:{research_topic}",
                outputs={
                    "phd_student_relationships_asserted": False,
                    "phd_student_failure_diagnostics": {
                        "error_code": "phd_student_research_topic_ambiguous",
                        "reason": (
                            "Research topic name matched multiple existing topic concepts."
                        ),
                        "research_topic": research_topic,
                        "resolution_hint": (
                            "Provide a unique research topic concept ID in the workflow input."
                        ),
                    },
                },
            )

        relation_result = add_relationship(
            source_id=student_concept_id,
            predicate=researches_predicate_id,
            target=research_topic_concept_id,
        )
        research_topic_link_written = bool(relation_result.get("success"))

    return WorkflowActionResult(
        status="success",
        outputs={
            "phd_student_relationships_asserted": True,
            "phd_student_concept_id": student_concept_id,
            "phd_student_supervisor_concept_ids": list(dict.fromkeys(supervisor_concept_ids)),
            "phd_student_supervisor_links_written": supervisor_links_written,
            "reused_supervisor_concept_ids": list(dict.fromkeys(reused_supervisor_ids)),
            "created_supervisor_concept_ids": list(dict.fromkeys(created_supervisor_ids)),
            "research_topic": research_topic,
            "phd_student_research_topic_concept_id": research_topic_concept_id or None,
            "phd_student_research_topic_reused_existing": research_topic_reused_existing,
            "phd_student_research_topic_link_written": research_topic_link_written,
            "phd_student_supervised_by_predicate_id": supervised_by_predicate_id,
            "phd_student_researches_predicate_id": researches_predicate_id,
        },
    )


def _handle_ground_phd_student_text(request: WorkflowActionRequest) -> WorkflowActionResult:
    student_concept_id = _clean_text(request.data.get("phd_student_concept_id"))
    source_text = _clean_text(
        request.data.get("phd_student_source_text")
        or request.data.get("student_description")
        or request.data.get("phd_student_description")
        or request.data.get("source_text")
    )
    if not source_text:
        source_text = _extract_request_text(request.data)

    if not student_concept_id:
        return WorkflowActionResult(
            status="failed",
            error="phd_student_text_grounding_missing_student_concept_id",
            outputs={
                "phd_student_text_grounded": False,
                "phd_student_failure_diagnostics": {
                    "error_code": "phd_student_text_grounding_missing_student_concept_id",
                    "reason": "Text grounding requires phd_student_concept_id.",
                },
            },
        )
    if not source_text:
        return WorkflowActionResult(
            status="failed",
            error="phd_student_text_source_missing:expected_source_text_or_description",
            outputs={
                "phd_student_text_grounded": False,
                "phd_student_failure_diagnostics": {
                    "error_code": "phd_student_text_source_missing",
                    "reason": "No source text was provided for PhD student representation grounding.",
                    "expected_fields": [
                        "phd_student_source_text",
                        "phd_student_description",
                        "student_description",
                    ],
                },
            },
        )

    upsert_text_for_concept(
        subject_concept_id=student_concept_id,
        predicate="hasDescription",
        text=source_text,
        lang="en-NZ",
    )
    provenance_payload = {
        "source": "text_driven_workflow_creation",
        "workflow": WORKFLOW_CREATION_WORKFLOW_ID,
        "institution": _clean_text(request.data.get("institution")),
        "research_topic": _clean_text(request.data.get("research_topic")),
    }
    upsert_text_for_concept(
        subject_concept_id=student_concept_id,
        predicate="hasNote",
        text=json.dumps(provenance_payload, sort_keys=True, ensure_ascii=True),
        lang="en-NZ",
    )
    return WorkflowActionResult(
        status="success",
        outputs={
            "phd_student_text_grounded": True,
            "phd_student_concept_id": student_concept_id,
            "phd_student_grounding_text_length": len(source_text),
            "phd_student_grounding_provenance": provenance_payload,
        },
    )


def _build_verification_registry(environment: WorkflowEnvironment) -> ActionRegistry:
    registry = ActionRegistry()
    # Candidate workflows created by workflow-gap recovery must verify through
    # the same action registry they will later execute under, or verification
    # can incorrectly reject otherwise runnable workflows.
    register_workflow_gap_recovery_actions(registry)
    register_entity_representation_actions(registry)
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_EMIT_MARKER,
            handler=_handle_emit_marker,
            description="Emit a deterministic marker key/value into workflow context.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_EMIT_MARKER
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS,
            handler=_handle_resolve_scholarly_authors,
            description=(
                "Resolve scholarly-work authors against existing person concepts "
                "and create/link missing people."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE,
            handler=_handle_resolve_phd_student_candidate,
            description=(
                "Resolve a PhD-student candidate concept from text/profile data "
                "with fail-closed ambiguity diagnostics."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS,
            handler=_handle_assert_phd_student_relationships,
            description=(
                "Assert core person/student/research relationships for the resolved "
                "PhD student concept."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS
            ),
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT,
            handler=_handle_ground_phd_student_text,
            description=(
                "Ground source text and provenance on the resolved PhD student concept."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT
            ),
        )
    )
    if environment.gateway is not None and getattr(environment.gateway, "enabled", False):
        registry.set_fallback_handler(_gateway_fallback_action)
    return registry


def _supported_action_ids_for_verification(
    *,
    action_ids: Iterable[str],
    environment: WorkflowEnvironment,
) -> set[str]:
    supported: set[str] = set()
    local_actions = {
        "workflow_gap.execute_candidate",
        ENTITY_REPRESENTATION_MATERIALISE_ACTION_ID,
        WORKFLOW_CREATION_ACTION_EMIT_MARKER,
        WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS,
        WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE,
        WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS,
        WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT,
    }
    for action_id in action_ids:
        if action_id in local_actions:
            supported.add(action_id)

    gateway = environment.gateway
    if gateway is not None and getattr(gateway, "enabled", False):
        try:
            methods = gateway.describe_methods()
            if isinstance(methods, Mapping):
                for key in methods.keys():
                    key_text = _clean_text(key)
                    if key_text:
                        supported.add(key_text)
            elif isinstance(methods, list):
                for item in methods:
                    item_text = _clean_text(item)
                    if item_text:
                        supported.add(item_text)
        except Exception:
            # Keep verification conservative when method introspection fails.
            pass
    return supported


def _verify_postconditions(
    *,
    workflow_data: Mapping[str, Any],
    probe: Mapping[str, Any],
) -> bool:
    if not probe:
        return True
    for key, expected in probe.items():
        if workflow_data.get(key) != expected:
            return False
    return True


def _handle_verify_discoverability(request: WorkflowActionRequest) -> WorkflowActionResult:
    spec = _normalise_workflow_spec(request.data)
    parent_type_id = str(spec.get("parent_type_id") or DEFAULT_WORKFLOW_PARENT_TYPE_ID)
    workflow_id = str(spec["workflow_id"])
    discovered = set(discover_workflow_ids())
    discoverable_before_publish = workflow_id in discovered
    definition = load_workflow_definition_from_vontology(workflow_id)
    if definition is None:
        outputs = _add_contract_output(
            outputs={
                "workflow_creation_spec": spec,
                "required_effects_declared": bool(spec.get("required_effects")),
                "structural_validation_passed": False,
                "postconditions_verified": False,
                "workflow_discoverable_before_publish": discoverable_before_publish,
            },
            validated_type_name=parent_type_id,
        )
        _write_workflow_publication_lifecycle(
            workflow_id=workflow_id,
            phase="validation_failed",
            published=False,
            validation_passed=False,
            postconditions_verified=False,
            last_error=f"workflow_definition_not_loadable:{workflow_id}",
        )
        return WorkflowActionResult(
            status="failed",
            error=f"workflow_definition_not_loadable:{workflow_id}",
            outputs=outputs,
        )

    action_ids = collect_workflow_action_ids(definition)
    supported_actions = _supported_action_ids_for_verification(
        action_ids=action_ids,
        environment=request.environment,
    )
    contract = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids=supported_actions,
        enforce_supported_actions=True,
        known_workflow_ids=discovered,
        workflow_definition_loader=load_workflow_definition_from_vontology,
    )
    structural_validation_passed = bool(contract.get("valid"))

    postconditions_verified = False
    optional_test_instance_id: str | None = None
    if structural_validation_passed:
        verification_registry = _build_verification_registry(request.environment)
        executor = WorkflowExecutor(registry=verification_registry, max_transitions=40)
        verification_inputs_raw = request.data.get("test_run_inputs")
        if not isinstance(verification_inputs_raw, Mapping):
            verification_inputs_raw = spec.get("verification_inputs")
        verification_result = executor.run(
            definition,
            environment=WorkflowEnvironment(
                llm_client=request.environment.llm_client,
                gateway=request.environment.gateway,
                model=request.environment.model,
                user_namespace=request.environment.user_namespace,
                auxiliary_system_prompt=request.environment.auxiliary_system_prompt,
                max_tool_invocations=request.environment.max_tool_invocations,
                default_gmail_profile=request.environment.default_gmail_profile,
            ),
            data=dict(verification_inputs_raw or {})
            if isinstance(verification_inputs_raw, Mapping)
            else {},
        )
        optional_test_instance_id = f"local_test_{uuid.uuid4()}"
        probe: dict[str, Any] = {}
        probe_raw = spec.get("postcondition_probe")
        if isinstance(probe_raw, Mapping):
            for key, value in probe_raw.items():
                key_text = _clean_text(key)
                if key_text:
                    probe[key_text] = value
        postconditions_verified = verification_result.completed and _verify_postconditions(
            workflow_data=verification_result.data,
            probe=probe,
        )

    required_effects_declared = bool(spec.get("required_effects"))
    all_verified = (
        required_effects_declared and structural_validation_passed and postconditions_verified
    )
    _write_workflow_publication_lifecycle(
        workflow_id=workflow_id,
        phase="validated" if all_verified else "validation_failed",
        published=False,
        validation_passed=structural_validation_passed,
        postconditions_verified=postconditions_verified,
        optional_test_instance_id=optional_test_instance_id,
        last_error=None if all_verified else "workflow_creation_verification_failed",
    )
    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_concept_id": workflow_id,
            "parent_concept_id_used": parent_type_id,
            "required_effects_declared": required_effects_declared,
            "structural_validation_passed": structural_validation_passed,
            "postconditions_verified": postconditions_verified,
            "optional_test_instance_id": optional_test_instance_id,
            "workflow_discoverable_before_publish": discoverable_before_publish,
            "contract_validation": contract,
        },
        validated_type_name=parent_type_id,
    )

    if not all_verified:
        return WorkflowActionResult(
            status="failed",
            error="workflow_creation_verification_failed",
            outputs=outputs,
        )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    spec = _normalise_workflow_spec(request.data)
    parent_type_id = str(spec.get("parent_type_id") or DEFAULT_WORKFLOW_PARENT_TYPE_ID)
    workflow_id = str(
        request.data.get("workflow_concept_id") or spec.get("workflow_id") or ""
    ).strip()
    required_effects_declared = bool(request.data.get("required_effects_declared"))
    structural_validation_passed = bool(request.data.get("structural_validation_passed"))
    postconditions_verified = bool(request.data.get("postconditions_verified"))
    all_verified = (
        required_effects_declared and structural_validation_passed and postconditions_verified
    )
    if workflow_id:
        _write_workflow_publication_lifecycle(
            workflow_id=workflow_id,
            phase="published" if all_verified else "draft_failed_completion_gate",
            published=all_verified,
            validation_passed=structural_validation_passed,
            postconditions_verified=postconditions_verified,
            optional_test_instance_id=(
                str(request.data.get("optional_test_instance_id") or "").strip() or None
            ),
            last_error=(
                None if all_verified else "workflow_creation_completion_gate_failed"
            ),
        )
    discoverable = workflow_id in set(discover_workflow_ids()) if workflow_id else False

    summary = {
        "workflow_concept_id": workflow_id or spec.get("workflow_id"),
        "parent_concept_id_used": request.data.get("parent_concept_id_used")
        or parent_type_id,
        "required_effects_declared": required_effects_declared,
        "structural_validation_passed": structural_validation_passed,
        "postconditions_verified": postconditions_verified,
        "optional_test_instance_id": request.data.get("optional_test_instance_id"),
        "workflow_discoverable": discoverable,
    }
    outputs = _add_contract_output(outputs=summary, validated_type_name=parent_type_id)
    outputs["response_text"] = (
        "Workflow creation completed and verified."
        if all_verified
        else "Workflow creation did not satisfy completion gate checks."
    )

    if not all_verified:
        return WorkflowActionResult(
            status="failed",
            error="workflow_creation_completion_gate_failed",
            outputs=outputs,
        )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_publish_workflow_definition(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    return _handle_finalise(request)


def _handle_emit_marker(request: WorkflowActionRequest) -> WorkflowActionResult:
    marker_key = _clean_text(
        request.inputs.get("marker_key") if isinstance(request.inputs, Mapping) else ""
    )
    if not marker_key:
        marker_key = "workflow_creation_marker"
    marker_value_raw = (
        request.inputs.get("marker_value") if isinstance(request.inputs, Mapping) else ""
    )
    marker_value: Any
    if isinstance(marker_value_raw, bool):
        marker_value = marker_value_raw
    elif isinstance(marker_value_raw, (int, float)) and not isinstance(
        marker_value_raw, bool
    ):
        marker_value = marker_value_raw
    else:
        marker_value_text = _clean_text(marker_value_raw)
        lowered = marker_value_text.lower()
        if lowered == "true":
            marker_value = True
        elif lowered == "false":
            marker_value = False
        else:
            marker_value = marker_value_text or "done"
    return WorkflowActionResult(
        status="success",
        outputs={marker_key: marker_value},
    )


def _workflow_creation_action_spec_kwargs(action_id: str) -> dict[str, Any]:
    definition = WORKFLOW_CREATION_ACTION_CONTRACT_BY_ACTION_ID.get(
        str(action_id or "").strip()
    )
    if definition is None:
        return {}
    return {
        "concept_id": definition.concept_id,
        "input_schema": definition.input_schema,
        "output_schema": definition.output_schema,
        "side_effects": definition.side_effects,
        "postconditions": definition.postconditions,
    }


def register_workflow_creation_actions(registry: ActionRegistry) -> None:
    """Register action handlers required by ``#V#von_workflow_creation_workflow``."""

    specs = (
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
            handler=_handle_identify_need,
            description="Normalise workflow creation request/specification.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_IDENTIFY_NEED
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
            handler=_handle_design_structure,
            description="Design a deterministic workflow structure.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
            handler=_handle_create_workflow_type,
            description="Create or update workflow concept and parent typing.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
            handler=_handle_create_step_concepts,
            description="Create step concepts for the target workflow graph.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
            handler=_handle_establish_relationships,
            description="Write workflow graph relationships (initial/step/transition/action).",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
            handler=_handle_verify_discoverability,
            description="Verify created workflow discoverability, contract validity, and postconditions.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_FINALISE,
            handler=_handle_finalise,
            description="Apply completion gate for workflow creation.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_FINALISE
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY,
            handler=_handle_ensure_workflow_identity,
            description="Ensure workflow identity, typing, and draft lifecycle metadata for an authored workflow.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_AUTHORING_ACTION_DISCOVER_EXISTING_WORKFLOWS,
            handler=_handle_discover_existing_workflows,
            description=(
                "Discover semantically relevant existing workflows so authoring "
                "workflows can decide whether to reuse, repair, or create."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_AUTHORING_ACTION_DISCOVER_EXISTING_WORKFLOWS
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_AUTHORING_ACTION_EXTRACT_EXISTING_WORKFLOW_SPEC,
            handler=_handle_extract_existing_workflow_spec,
            description=(
                "Load an existing workflow definition and render it as a "
                "declarative authoring spec for repair/improvement workflows."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_AUTHORING_ACTION_EXTRACT_EXISTING_WORKFLOW_SPEC
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION,
            handler=_handle_materialise_workflow_definition,
            description="Materialise a declarative workflow definition into authoritative workflow graph concepts and bindings.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION,
            handler=_handle_validate_workflow_definition,
            description="Validate an authored draft workflow definition structurally and by bounded execution.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION,
            handler=_handle_publish_workflow_definition,
            description="Publish a validated draft workflow definition when the completion gate passes.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_EMIT_MARKER,
            handler=_handle_emit_marker,
            description="Emit deterministic marker output for created workflow tasks.",
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_EMIT_MARKER
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS,
            handler=_handle_resolve_scholarly_authors,
            description=(
                "Resolve scholarly-work authors by reusing verified person concepts "
                "or creating missing person concepts, then assert authorship links."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE,
            handler=_handle_resolve_phd_student_candidate,
            description=(
                "Resolve a PhD-student candidate concept from text/profile data "
                "with fail-closed ambiguity diagnostics."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS,
            handler=_handle_assert_phd_student_relationships,
            description=(
                "Assert core person/student/research relationships for the resolved "
                "PhD student concept."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS
            ),
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT,
            handler=_handle_ground_phd_student_text,
            description=(
                "Ground source text and provenance on the resolved PhD student concept."
            ),
            **_workflow_creation_action_spec_kwargs(
                WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT
            ),
        ),
    )
    for spec in specs:
        registry.register_if_absent(spec)


WORKFLOW_CREATION_STEP_SEQUENCE: tuple[str, ...] = (
    WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
    WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_STEP_MATERIALISE_WORKFLOW_DEFINITION,
    WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
)

WORKFLOW_CREATION_STEP_ACTIONS: dict[str, str] = {
    WORKFLOW_CREATION_STEP_IDENTIFY_NEED: WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
    WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE: WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE: WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY,
    WORKFLOW_CREATION_STEP_MATERIALISE_WORKFLOW_DEFINITION: WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION,
    WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY: WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION,
    WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA: WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION,
}


__all__ = [
    "WORKFLOW_CREATION_WORKFLOW_ID",
    "WORKFLOW_CREATION_ACTION_IDENTIFY_NEED",
    "WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE",
    "WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE",
    "WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS",
    "WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS",
    "WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY",
    "WORKFLOW_CREATION_ACTION_FINALISE",
    "WORKFLOW_AUTHORING_ACTION_ENSURE_WORKFLOW_IDENTITY",
    "WORKFLOW_AUTHORING_ACTION_DISCOVER_EXISTING_WORKFLOWS",
    "WORKFLOW_AUTHORING_ACTION_EXTRACT_EXISTING_WORKFLOW_SPEC",
    "WORKFLOW_AUTHORING_ACTION_MATERIALISE_WORKFLOW_DEFINITION",
    "WORKFLOW_AUTHORING_ACTION_VALIDATE_WORKFLOW_DEFINITION",
    "WORKFLOW_AUTHORING_ACTION_PUBLISH_WORKFLOW_DEFINITION",
    "WORKFLOW_AUTHORING_ACTION_DECIDE_REPAIR_OR_CREATE",
    "WORKFLOW_AUTHORING_ACTION_DESIGN_REPAIR_SPEC",
    "WORKFLOW_CREATION_ACTION_EMIT_MARKER",
    "WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS",
    "WORKFLOW_CREATION_ACTION_RESOLVE_PHD_STUDENT_CANDIDATE",
    "WORKFLOW_CREATION_ACTION_ASSERT_PHD_STUDENT_RELATIONSHIPS",
    "WORKFLOW_CREATION_ACTION_GROUND_PHD_STUDENT_TEXT",
    "WORKFLOW_CREATION_STEP_SEQUENCE",
    "WORKFLOW_CREATION_STEP_ACTIONS",
    "register_workflow_creation_actions",
]
