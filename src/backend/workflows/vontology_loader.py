from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import get_texts_for_concept
from .engine import (
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowActionInvocation,
    WorkflowTransitionSpec,
)

logger = logging.getLogger(__name__)

# Canonical workflow graph predicates with legacy-compatible aliases.
# The first entry in each tuple is the preferred canonical concept predicate.
WORKFLOW_GRAPH_PREDICATE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "hasInitialStep": (
        "#V#hasInitialStep",
        "hasInitialStep",
        "#V#has_initial_step",
        "has_initial_step",
    ),
    "hasStep": ("#V#hasStep", "hasStep", "#V#has_step", "has_step"),
    "invokesAction": (
        "#V#invokesAction",
        "invokesAction",
        "#V#invokes_action",
        "invokes_action",
    ),
    "workflowStepInvokesTool": (
        "#V#workflow_step_invokes_tool",
        "workflow_step_invokes_tool",
        "#V#workflowStepInvokesTool",
        "workflowStepInvokesTool",
    ),
    "nextStep": ("#V#nextStep", "nextStep", "#V#next_step", "next_step"),
    "onTrueNextStep": (
        "#V#onTrueNextStep",
        "onTrueNextStep",
        "#V#on_true_next_step",
        "on_true_next_step",
    ),
    "onFalseNextStep": (
        "#V#onFalseNextStep",
        "onFalseNextStep",
        "#V#on_false_next_step",
        "on_false_next_step",
    ),
    "onFailureNextStep": (
        "#V#onFailureNextStep",
        "onFailureNextStep",
        "#V#on_failure_next_step",
        "on_failure_next_step",
    ),
    "hasPrecondition": (
        "#V#hasPrecondition",
        "hasPrecondition",
        "#V#has_precondition",
        "has_precondition",
    ),
    "hasEffect": ("#V#hasEffect", "hasEffect", "#V#has_effect", "has_effect"),
    "readsVariable": (
        "#V#readsVariable",
        "readsVariable",
        "#V#reads_variable",
        "reads_variable",
    ),
    "writesVariable": (
        "#V#writesVariable",
        "writesVariable",
        "#V#writes_variable",
        "writes_variable",
    ),
    "hasInputMap": (
        "#V#hasInputMap",
        "hasInputMap",
        "#V#has_input_map",
        "has_input_map",
    ),
    "workflowStepMapsContextKeyToToolParam": (
        "#V#workflow_step_maps_context_key_to_tool_param",
        "workflow_step_maps_context_key_to_tool_param",
        "#V#workflowStepMapsContextKeyToToolParam",
        "workflowStepMapsContextKeyToToolParam",
    ),
    "workflowStepWritesContextKey": (
        "#V#workflow_step_writes_context_key",
        "workflow_step_writes_context_key",
        "#V#workflowStepWritesContextKey",
        "workflowStepWritesContextKey",
    ),
    "workflowStepMapsToolOutputFieldToContextKey": (
        "#V#workflow_step_maps_tool_output_field_to_context_key",
        "workflow_step_maps_tool_output_field_to_context_key",
        "#V#workflowStepMapsToolOutputFieldToContextKey",
        "workflowStepMapsToolOutputFieldToContextKey",
    ),
}

# Preferred roots for workflow type discovery.
WORKFLOW_DISCOVERY_BASE_TYPE_IDS: Tuple[str, ...] = (
    "#V#ai_workflow",
    "#V#durable_workflow",
    "#V#workflow",
    "#V#llm_workflow",
)

WORKFLOW_STEP_VACUITY_REASON_CODE = "workflow_step_vacuous"

_WORKFLOW_MAPPING_ID_RE = re.compile(
    r"^#V#workflow_mapping_([a-z0-9_]+)_to_([a-z0-9_]+?)(?:_param(?:eter)?)?$",
    re.IGNORECASE,
)
_WORKFLOW_MAPPING_DESCRIPTION_RE = re.compile(
    r"context key\s+['\"]([^'\"]+)['\"].*?['\"]([^'\"]+)['\"]\s+parameter",
    re.IGNORECASE,
)
_WORKFLOW_OUTPUT_MAPPING_ID_RE = re.compile(
    r"^#V#workflow_mapping_tool_field_([a-z0-9_]+)_to_([a-z0-9_]+)$",
    re.IGNORECASE,
)
_WORKFLOW_OUTPUT_MAPPING_DESCRIPTION_RE = re.compile(
    r"tool output field\s+['\"]([^'\"]+)['\"].*?context key\s+['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

_WORKFLOW_MAPPING_SCHEMA_VERSION = 1
_WORKFLOW_MAPPING_SPEC_CANDIDATE_KEYS: Tuple[str, ...] = (
    "workflow_mapping_spec",
    "workflow_mapping_spec_json",
    "workflow_mapping_json",
    "mapping_spec",
    "mapping_spec_json",
    "mapping_json",
)
_WORKFLOW_MAPPING_SPEC_ALLOWED_KEYS: Tuple[str, ...] = (
    "schema_version",
    "mapping_type",
    "workflow_step_id",
    "tool_id",
    "context_key_concept_id",
    "tool_param_name",
    "tool_output_field_name",
    "target_context_key_concept_id",
)
_WORKFLOW_INPUT_MAPPING_TYPES: Tuple[str, ...] = (
    "context_key_to_tool_param",
    "workflow_step_maps_context_key_to_tool_param",
)
_WORKFLOW_OUTPUT_MAPPING_TYPES: Tuple[str, ...] = (
    "tool_output_field_to_context_key",
    "workflow_step_maps_tool_output_field_to_context_key",
)


def _normalise_mapping_type(value: Any) -> str:
    return str(value or "").strip().lower()


def _coerce_mapping_spec_object(value: Any) -> Dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _extract_structured_mapping_spec(
    mapping_doc: Dict[str, Any] | None,
) -> tuple[Dict[str, Any] | None, str | None]:
    """Extract a structured mapping spec from a mapping concept document.

    Preferred path for deterministic runtime behaviour:
    - read explicit schema fields from concept_data/preserved_fields
    - fall back to legacy concept-id and description parsing only when absent
    """

    if not isinstance(mapping_doc, dict):
        return None, None

    concept_data = mapping_doc.get("concept_data")
    preserved_fields: Dict[str, Any] = {}
    if isinstance(concept_data, dict):
        raw_preserved = concept_data.get("preserved_fields")
        if isinstance(raw_preserved, dict):
            preserved_fields = raw_preserved

    candidate_values: List[Any] = []
    for key in _WORKFLOW_MAPPING_SPEC_CANDIDATE_KEYS:
        candidate_values.extend(
            (
                mapping_doc.get(key),
                concept_data.get(key) if isinstance(concept_data, dict) else None,
                preserved_fields.get(key),
            )
        )

    saw_candidate = False
    for raw_value in candidate_values:
        if raw_value is None:
            continue
        saw_candidate = True
        spec_object = _coerce_mapping_spec_object(raw_value)
        if isinstance(spec_object, dict):
            return spec_object, None

    if saw_candidate:
        return None, "schema_invalid_format"
    return None, None


def _validate_mapping_spec_common(
    *,
    spec: Dict[str, Any],
    step_id: str | None,
    action_id: str | None,
) -> str | None:
    unknown_keys = [key for key in spec.keys() if key not in _WORKFLOW_MAPPING_SPEC_ALLOWED_KEYS]
    if unknown_keys:
        return "schema_unknown_fields"

    schema_version = spec.get("schema_version")
    if schema_version is not None:
        version_text = str(schema_version).strip()
        if version_text != str(_WORKFLOW_MAPPING_SCHEMA_VERSION):
            return "schema_version_unsupported"

    spec_step_id = str(spec.get("workflow_step_id") or "").strip()
    if step_id and spec_step_id and spec_step_id != step_id:
        return "schema_step_mismatch"

    spec_tool_id = _normalise_invoked_action_target(str(spec.get("tool_id") or ""))
    expected_action_id = _normalise_invoked_action_target(action_id or "")
    if expected_action_id and spec_tool_id and spec_tool_id != expected_action_id:
        return "schema_tool_mismatch"

    return None


def _normalise_relationship_targets(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def detect_vacuous_workflow_steps(
    *,
    workflow_id: str | None,
    graph: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Find workflow steps that have no executable contract.

    A step is considered vacuous when it contains no executable instruction:
      - no ``invokesAction`` target
      - no preconditions/effects
      - no variable read/write declarations

    This intentionally surfaces steps that appear structurally wired but do not
    declare what they should do, enabling fast diagnosis from previous step
    context before execution is attempted.
    """
    if not isinstance(graph, dict):
        return []

    steps = graph.get("steps")
    edges = graph.get("edges")
    if not isinstance(steps, list) or not steps:
        return []

    initial_step = graph.get("initial_step")
    if not isinstance(initial_step, str) or not initial_step.strip():
        initial_step = None

    step_by_id: Dict[str, Dict[str, Any]] = {}
    step_order: Dict[str, int] = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("step_id", "") or "").strip()
        if step_id:
            step_by_id[step_id] = step
            step_order[step_id] = index

    incoming: Dict[str, List[Dict[str, Any]]] = {key: [] for key in step_by_id}
    if isinstance(edges, list):
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            from_step_id = str(edge.get("from", "") or "").strip()
            to_step_id = str(edge.get("to", "") or "").strip()
            predicate = edge.get("predicate")
            if from_step_id and to_step_id:
                incoming.setdefault(to_step_id, []).append(
                    {
                        "from_step_id": from_step_id,
                        "predicate": predicate,
                    }
                )

    issues: List[Dict[str, Any]] = []

    for step in steps:
        if not isinstance(step, dict):
            continue

        step_id = str(step.get("step_id", "") or "").strip()
        if not step_id:
            continue

        invokes_action = str(step.get("invokes_action", "") or "").strip()
        preconditions = step.get("preconditions") or []
        effects = step.get("effects") or []
        reads_variables = step.get("reads_variables") or []
        writes_variables = step.get("writes_variables") or []
        context_input_mappings = step.get("context_input_mappings") or []
        tool_output_context_mappings = step.get("tool_output_context_mappings") or []
        writes_context_keys = step.get("writes_context_keys") or []

        has_contract = bool(
            invokes_action
            or preconditions
            or effects
            or reads_variables
            or writes_variables
            or context_input_mappings
            or tool_output_context_mappings
            or writes_context_keys
        )
        if has_contract:
            continue

        previous_steps = incoming.get(step_id, [])
        previous_step_context: List[Dict[str, Any]] = []
        if previous_steps:
            for context in previous_steps:
                prev_step_id = str(context.get("from_step_id", "") or "").strip()
                prev_step = step_by_id.get(prev_step_id, {})
                previous_step_context.append(
                    {
                        "step_id": prev_step_id,
                        "step_name": prev_step.get("name"),
                        "invokes_action": str(prev_step.get("invokes_action", "") or "").strip()
                        or None,
                        "link_predicate": context.get("predicate"),
                    }
                )
        elif step_id == initial_step:
            previous_step_context = [
                {
                    "step_id": "__workflow_input__",
                    "step_name": "Workflow input",
                    "invokes_action": None,
                    "link_predicate": None,
                }
            ]

        issues.append(
            {
                "workflow_id": str(workflow_id or "").strip() or None,
                "step_id": step_id,
                "step_name": str(step.get("name", "") or "").strip() or None,
                "step_index": step_order.get(step_id, -1),
                "reason_code": WORKFLOW_STEP_VACUITY_REASON_CODE,
                "cause": (
                    "No executable contract found for step. "
                    "Expected invokesAction, preconditions/effects, "
                    "variable read/write declarations, "
                    "or context mapping declarations."
                ),
                "previous_steps": previous_step_context,
                "previous_step_count": len(previous_step_context),
            }
        )

    return issues


def _first_relationship_target(
    relationships: Dict[str, Any], candidate_predicates: Iterable[str]
) -> Optional[str]:
    target, _ = _first_relationship_target_with_predicate(
        relationships, candidate_predicates
    )
    return target


def _all_relationship_targets(
    relationships: Dict[str, Any], candidate_predicates: Iterable[str]
) -> List[str]:
    targets, _ = _all_relationship_targets_with_predicates(
        relationships, candidate_predicates
    )
    return targets


def _first_relationship_target_with_predicate(
    relationships: Dict[str, Any], candidate_predicates: Iterable[str]
) -> Tuple[Optional[str], Optional[str]]:
    for predicate in candidate_predicates:
        targets = _normalise_relationship_targets(relationships.get(predicate))
        if targets:
            return targets[0], predicate
    return None, None


def _all_relationship_targets_with_predicates(
    relationships: Dict[str, Any], candidate_predicates: Iterable[str]
) -> Tuple[List[str], List[str]]:
    results: List[str] = []
    matched_predicates: List[str] = []
    for predicate in candidate_predicates:
        targets = _normalise_relationship_targets(relationships.get(predicate))
        if targets:
            matched_predicates.append(predicate)
            results.extend(targets)
    # Preserve order but remove duplicates.
    seen: set[str] = set()
    unique: List[str] = []
    for item in results:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique, matched_predicates


def _record_legacy_alias_use(
    *,
    legacy_aliases: set[str],
    canonical_predicate: str,
    matched_predicates: Iterable[Optional[str]],
) -> None:
    for matched in matched_predicates:
        if isinstance(matched, str) and matched and matched != canonical_predicate:
            legacy_aliases.add(matched)


def _normalise_context_key_symbol(raw_symbol: str) -> str:
    symbol = str(raw_symbol or "").strip()
    if symbol.startswith("#V#workflow_context_key_"):
        return symbol[len("#V#workflow_context_key_") :]
    if symbol.startswith("workflow_context_key_"):
        return symbol[len("workflow_context_key_") :]
    if symbol.startswith("#V#"):
        return symbol[3:]
    return symbol


def _mapping_pair_from_concept_id(mapping_concept_id: str) -> tuple[str | None, str | None]:
    if not isinstance(mapping_concept_id, str):
        return None, None
    match = _WORKFLOW_MAPPING_ID_RE.match(mapping_concept_id.strip())
    if not match:
        return None, None
    context_key = _normalise_context_key_symbol(match.group(1))
    tool_param = match.group(2).strip()
    if context_key and tool_param:
        return context_key, tool_param
    return None, None


def _mapping_pair_from_description(description: str) -> tuple[str | None, str | None]:
    if not isinstance(description, str):
        return None, None
    match = _WORKFLOW_MAPPING_DESCRIPTION_RE.search(description)
    if not match:
        return None, None
    context_key = _normalise_context_key_symbol(match.group(1))
    tool_param = match.group(2).strip()
    if context_key and tool_param:
        return context_key, tool_param
    return None, None


def _extract_mapping_pair(
    *,
    mapping_concept_id: str,
    mapping_doc: Dict[str, Any] | None,
    step_id: str | None,
    action_id: str | None,
) -> tuple[str | None, str | None, str | None]:
    structured_spec, structured_error = _extract_structured_mapping_spec(mapping_doc)
    if structured_spec:
        mapping_type = _normalise_mapping_type(structured_spec.get("mapping_type"))
        if mapping_type not in _WORKFLOW_INPUT_MAPPING_TYPES:
            return None, None, "schema_mapping_type_mismatch"
        validation_error = _validate_mapping_spec_common(
            spec=structured_spec,
            step_id=step_id,
            action_id=action_id,
        )
        if validation_error:
            return None, None, validation_error
        context_key = _normalise_context_key_symbol(
            str(structured_spec.get("context_key_concept_id") or "")
        )
        tool_param = str(structured_spec.get("tool_param_name") or "").strip()
        if not context_key or not tool_param:
            return None, None, "schema_required_fields_missing"
        return context_key, tool_param, None
    if structured_error:
        return None, None, structured_error

    context_key, tool_param = _mapping_pair_from_concept_id(mapping_concept_id)
    if context_key and tool_param:
        return context_key, tool_param, None

    concept_data = (
        mapping_doc.get("concept_data", {}) if isinstance(mapping_doc, dict) else {}
    )
    preserved_fields = (
        concept_data.get("preserved_fields", {})
        if isinstance(concept_data, dict)
        else {}
    )
    description = (
        preserved_fields.get("description")
        if isinstance(preserved_fields, dict)
        else None
    )
    context_key, tool_param = _mapping_pair_from_description(str(description or ""))
    if context_key and tool_param:
        return context_key, tool_param, None

    return None, None, "mapping_pattern_not_detected"


def _tool_output_mapping_pair_from_concept_id(
    mapping_concept_id: str,
) -> tuple[str | None, str | None]:
    if not isinstance(mapping_concept_id, str):
        return None, None
    match = _WORKFLOW_OUTPUT_MAPPING_ID_RE.match(mapping_concept_id.strip())
    if not match:
        return None, None
    tool_output_field = match.group(1).strip()
    context_key = _normalise_context_key_symbol(match.group(2))
    if tool_output_field and context_key:
        return tool_output_field, context_key
    return None, None


def _tool_output_mapping_pair_from_description(
    description: str,
) -> tuple[str | None, str | None]:
    if not isinstance(description, str):
        return None, None
    match = _WORKFLOW_OUTPUT_MAPPING_DESCRIPTION_RE.search(description)
    if not match:
        return None, None
    tool_output_field = match.group(1).strip()
    context_key = _normalise_context_key_symbol(match.group(2))
    if tool_output_field and context_key:
        return tool_output_field, context_key
    return None, None


def _extract_tool_output_mapping(
    *,
    mapping_concept_id: str,
    mapping_doc: Dict[str, Any] | None,
    step_id: str | None,
    action_id: str | None,
) -> tuple[str | None, str | None, str | None]:
    structured_spec, structured_error = _extract_structured_mapping_spec(mapping_doc)
    if structured_spec:
        mapping_type = _normalise_mapping_type(structured_spec.get("mapping_type"))
        if mapping_type not in _WORKFLOW_OUTPUT_MAPPING_TYPES:
            return None, None, "schema_mapping_type_mismatch"
        validation_error = _validate_mapping_spec_common(
            spec=structured_spec,
            step_id=step_id,
            action_id=action_id,
        )
        if validation_error:
            return None, None, validation_error
        tool_output_field = str(
            structured_spec.get("tool_output_field_name") or ""
        ).strip()
        context_key = _normalise_context_key_symbol(
            str(structured_spec.get("target_context_key_concept_id") or "")
        )
        if not tool_output_field or not context_key:
            return None, None, "schema_required_fields_missing"
        return tool_output_field, context_key, None
    if structured_error:
        return None, None, structured_error

    tool_output_field, context_key = _tool_output_mapping_pair_from_concept_id(
        mapping_concept_id
    )
    if tool_output_field and context_key:
        return tool_output_field, context_key, None

    concept_data = (
        mapping_doc.get("concept_data", {}) if isinstance(mapping_doc, dict) else {}
    )
    preserved_fields = (
        concept_data.get("preserved_fields", {})
        if isinstance(concept_data, dict)
        else {}
    )
    description = (
        preserved_fields.get("description")
        if isinstance(preserved_fields, dict)
        else None
    )
    tool_output_field, context_key = _tool_output_mapping_pair_from_description(
        str(description or "")
    )
    if tool_output_field and context_key:
        return tool_output_field, context_key, None

    return None, None, "mapping_pattern_not_detected"


def _normalise_invoked_action_target(raw_target: str) -> str | None:
    """Normalise workflow action/tool relation targets into executable action IDs.

    Vontology may encode step actions directly as MCP method names
    (e.g. ``fetch_concept``) or as tool concept IDs
    (e.g. ``#V#fetch_concept_tool``).  The runtime action registry expects
    action IDs/method names, so concept IDs are reduced to their method form.
    """

    if not isinstance(raw_target, str):
        return None
    target = raw_target.strip()
    if not target:
        return None

    if target.startswith("#V#"):
        token = target[3:].strip()
        if token.endswith("_tool"):
            token = token[: -len("_tool")]
        elif token.endswith(" tool"):
            token = token[: -len(" tool")]
        token = token.strip()
        return token or None

    return target


def best_effort_workflow_narrative_text(workflow_id: str) -> Optional[str]:
    """Fetch a stored workflow definition *narrative* text from Vontology.

    Workflows should be represented structurally via explicit relationships
    (steps + control flow). This text is optional and is not machine-parsed.
    """

    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return None

    try:
        texts = get_texts_for_concept(workflow_id)
    except Exception:
        return None

    if not isinstance(texts, list):
        return None

    preferred_predicates = ("hasDefinition", "hasContent", "hasDescription")

    for predicate in preferred_predicates:
        for item in texts:
            if not isinstance(item, dict):
                continue
            if item.get("predicate") != predicate:
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

    return None


def _fetch_concepts_by_id(concept_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not concept_ids:
        return {}

    cursor = ConceptsRepository.find(
        {"concept_id": {"$in": concept_ids}},
        {
            "concept_id": 1,
            "name": 1,
            "relationships": 1,
            "concept_data.preserved_fields": 1,
            "concept_data.preserved_fields.description": 1,
        },
        limit=len(concept_ids),
    )
    docs = list(cursor)
    mapping: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        cid = doc.get("concept_id")
        if isinstance(cid, str) and cid.strip():
            mapping[cid] = doc
    return mapping


def build_workflow_process_graph(
    workflow_id: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Build a workflow definition from explicit Vontology relationships.

    Canonical graph predicates are defined in
    ``WORKFLOW_GRAPH_PREDICATE_ALIASES``. Legacy aliases are still accepted
    for compatibility and are surfaced as warnings.
    """

    warnings: List[str] = []

    workflow_doc = ConceptsRepository.find_one(
        {"concept_id": workflow_id},
        {"concept_id": 1, "name": 1, "relationships": 1},
    )
    if not workflow_doc:
        return None, ["workflow_concept_not_found"]

    relationships = workflow_doc.get("relationships") or {}
    if not isinstance(relationships, dict):
        relationships = {}

    legacy_aliases: set[str] = set()

    initial_step_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInitialStep"]
    initial_step, initial_step_predicate = _first_relationship_target_with_predicate(
        relationships,
        initial_step_candidates,
    )
    _record_legacy_alias_use(
        legacy_aliases=legacy_aliases,
        canonical_predicate=initial_step_candidates[0],
        matched_predicates=(initial_step_predicate,),
    )

    has_step_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["hasStep"]
    step_ids, step_predicates = _all_relationship_targets_with_predicates(
        relationships,
        has_step_candidates,
    )
    _record_legacy_alias_use(
        legacy_aliases=legacy_aliases,
        canonical_predicate=has_step_candidates[0],
        matched_predicates=step_predicates,
    )

    if initial_step and initial_step not in step_ids:
        step_ids.insert(0, initial_step)
    if not initial_step and step_ids:
        initial_step = step_ids[0]
        warnings.append("missing_hasInitialStep_used_first_hasStep")

    if not step_ids:
        return None, ["workflow_has_no_steps"]

    step_docs = _fetch_concepts_by_id(step_ids)
    missing_steps = [sid for sid in step_ids if sid not in step_docs]
    if missing_steps:
        warnings.append(f"missing_step_concepts:{','.join(missing_steps[:25])}")

    step_items: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    def _edge(source_id: str, predicate: str, target_id: Optional[str]) -> None:
        if not target_id:
            return
        edges.append({"from": source_id, "predicate": predicate, "to": target_id})

    for step_id in step_ids:
        doc = step_docs.get(step_id, {})
        step_rels = doc.get("relationships") or {}
        if not isinstance(step_rels, dict):
            step_rels = {}

        invokes_action_candidates = (
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["invokesAction"],
            *WORKFLOW_GRAPH_PREDICATE_ALIASES["workflowStepInvokesTool"],
        )
        invokes_action_raw, invokes_action_predicate = _first_relationship_target_with_predicate(
            step_rels,
            invokes_action_candidates,
        )
        invokes_action = _normalise_invoked_action_target(invokes_action_raw or "")
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=WORKFLOW_GRAPH_PREDICATE_ALIASES["invokesAction"][0],
            matched_predicates=(invokes_action_predicate,),
        )

        next_step_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["nextStep"]
        next_step, next_step_predicate = _first_relationship_target_with_predicate(
            step_rels,
            next_step_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=next_step_candidates[0],
            matched_predicates=(next_step_predicate,),
        )

        on_true_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["onTrueNextStep"]
        on_true, on_true_predicate = _first_relationship_target_with_predicate(
            step_rels,
            on_true_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_true_candidates[0],
            matched_predicates=(on_true_predicate,),
        )

        on_false_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["onFalseNextStep"]
        on_false, on_false_predicate = _first_relationship_target_with_predicate(
            step_rels,
            on_false_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_false_candidates[0],
            matched_predicates=(on_false_predicate,),
        )

        on_failure_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["onFailureNextStep"]
        on_failure, on_failure_predicate = _first_relationship_target_with_predicate(
            step_rels,
            on_failure_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=on_failure_candidates[0],
            matched_predicates=(on_failure_predicate,),
        )

        precondition_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["hasPrecondition"]
        preconditions, precondition_predicates = _all_relationship_targets_with_predicates(
            step_rels,
            precondition_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=precondition_candidates[0],
            matched_predicates=precondition_predicates,
        )

        effect_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["hasEffect"]
        effects, effect_predicates = _all_relationship_targets_with_predicates(
            step_rels,
            effect_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=effect_candidates[0],
            matched_predicates=effect_predicates,
        )

        reads_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["readsVariable"]
        reads_vars, reads_predicates = _all_relationship_targets_with_predicates(
            step_rels,
            reads_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=reads_candidates[0],
            matched_predicates=reads_predicates,
        )

        writes_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES["writesVariable"]
        writes_vars, writes_predicates = _all_relationship_targets_with_predicates(
            step_rels,
            writes_candidates,
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=writes_candidates[0],
            matched_predicates=writes_predicates,
        )

        input_mapping_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES[
            "workflowStepMapsContextKeyToToolParam"
        ]
        context_input_mappings, input_mapping_predicates = (
            _all_relationship_targets_with_predicates(
                step_rels,
                input_mapping_candidates,
            )
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=input_mapping_candidates[0],
            matched_predicates=input_mapping_predicates,
        )

        writes_context_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES[
            "workflowStepWritesContextKey"
        ]
        writes_context_keys, writes_context_predicates = (
            _all_relationship_targets_with_predicates(
                step_rels,
                writes_context_candidates,
            )
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=writes_context_candidates[0],
            matched_predicates=writes_context_predicates,
        )

        output_mapping_candidates = WORKFLOW_GRAPH_PREDICATE_ALIASES[
            "workflowStepMapsToolOutputFieldToContextKey"
        ]
        tool_output_context_mappings, output_mapping_predicates = (
            _all_relationship_targets_with_predicates(
                step_rels,
                output_mapping_candidates,
            )
        )
        _record_legacy_alias_use(
            legacy_aliases=legacy_aliases,
            canonical_predicate=output_mapping_candidates[0],
            matched_predicates=output_mapping_predicates,
        )

        step_items.append(
            {
                "step_id": step_id,
                "name": doc.get("name"),
                "invokes_action": invokes_action,
                "preconditions": preconditions,
                "effects": effects,
                "reads_variables": reads_vars,
                "writes_variables": writes_vars,
                "context_input_mappings": context_input_mappings,
                "tool_output_context_mappings": tool_output_context_mappings,
                "writes_context_keys": writes_context_keys,
                "control_flow": {
                    "next": next_step,
                    "on_true": on_true,
                    "on_false": on_false,
                    "on_failure": on_failure,
                },
            }
        )

        _edge(step_id, "nextStep", next_step)
        _edge(step_id, "onTrueNextStep", on_true)
        _edge(step_id, "onFalseNextStep", on_false)
        _edge(step_id, "onFailureNextStep", on_failure)

    if legacy_aliases:
        warnings.append(
            f"legacy_workflow_predicates_used:{','.join(sorted(legacy_aliases))}"
        )

    definition = {
        "representation": "vontology_process_graph_v1",
        "workflow_id": workflow_id,
        "initial_step": initial_step,
        "steps": step_items,
        "edges": edges,
        "warnings": warnings,
    }
    return definition, warnings


def load_workflow_definition_from_vontology(
    workflow_id: str,
) -> Optional[WorkflowDefinition]:
    """Load an executable WorkflowDefinition from Vontology.

    JVNAUTOSCI-922 Phase 3.2: This function converts the raw process graph
    produced by ``build_workflow_process_graph()`` into an executable
    ``WorkflowDefinition`` for the workflow engine.

    Fixes applied (Phase 3.2):
    - Correct key: reads ``initial_step`` (not ``initial_state``) from graph.
    - Lambda capture: transition conditions capture control-flow values by
      default-argument binding to avoid Python late-binding closure bugs.
    - ``on_failure`` transitions: mapped to a condition checking the
      ``last_action_failed`` context flag (set by the action registry on error).
    - Input mapping: reads ``hasInputMap`` / ``has_input_map`` relationships
      from step concepts to populate ``WorkflowActionInvocation.inputs``.
    - Context mapping: reads ``workflow_step_maps_context_key_to_tool_param``
      mapping concepts and encodes dynamic input bindings resolved at runtime.
    - Output mapping: reads
      ``workflow_step_maps_tool_output_field_to_context_key`` mapping concepts
      and carries structured tool-output→context write contracts.
    - Metadata: preconditions, effects, and variable read/write lists are
      carried through as ``WorkflowStateSpec.metadata`` for introspection, along
      with context read/write contract hints.
    """
    graph, warnings = build_workflow_process_graph(workflow_id)
    if not graph:
        return None

    # Correct key from build_workflow_process_graph output.
    initial_state_id = graph.get("initial_step")
    if not initial_state_id:
        steps = graph.get("steps")
        if steps and len(steps) > 0:
            initial_state_id = steps[0]["step_id"]

    if not initial_state_id:
        return None

    # Pre-fetch step docs for input-map reading.
    step_ids = [s["step_id"] for s in graph.get("steps", []) if s.get("step_id")]
    step_docs = _fetch_concepts_by_id(step_ids) if step_ids else {}
    mapping_ids: list[str] = []
    for step in graph.get("steps", []):
        for mapping_key in (
            "context_input_mappings",
            "tool_output_context_mappings",
        ):
            raw_mapping_ids = step.get(mapping_key)
            if not isinstance(raw_mapping_ids, list):
                continue
            for item in raw_mapping_ids:
                if isinstance(item, str) and item.strip():
                    mapping_ids.append(item.strip())
    mapping_docs = _fetch_concepts_by_id(mapping_ids) if mapping_ids else {}

    states: Dict[str, WorkflowStateSpec] = {}

    for step in graph.get("steps", []):
        step_id = step.get("step_id")
        invokes_action = step.get("invokes_action")
        control_flow = step.get("control_flow", {})

        actions: list[WorkflowActionInvocation] = []
        reads_context_keys: list[str] = []
        unresolved_input_mappings: list[str] = []
        invalid_input_mapping_specs: list[Dict[str, str]] = []
        tool_output_context_mappings: list[Dict[str, str]] = []
        unresolved_output_mappings: list[str] = []
        invalid_output_mapping_specs: list[Dict[str, str]] = []
        if invokes_action:
            # Read input mapping from Vontology (hasInputMap relationships).
            input_map: Dict[str, Any] = {}
            doc = step_docs.get(step_id, {})
            step_rels = doc.get("relationships") or {}
            if isinstance(step_rels, dict):
                raw_inputs = _all_relationship_targets(
                    step_rels,
                    WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInputMap"],
                )
                # Input maps are stored as "key=value" or "key:value" strings.
                for entry in raw_inputs:
                    if "=" in entry:
                        k, _, v = entry.partition("=")
                    elif ":" in entry:
                        k, _, v = entry.partition(":")
                    else:
                        continue
                    k, v = k.strip(), v.strip()
                    if k:
                        input_map[k] = v

            # Read semantic context mappings from dedicated mapping concepts.
            semantic_mapping_ids = step.get("context_input_mappings") or []
            if isinstance(semantic_mapping_ids, list):
                for mapping_concept_id in semantic_mapping_ids:
                    if not isinstance(mapping_concept_id, str) or not mapping_concept_id:
                        continue
                    context_key, tool_param, parse_error = _extract_mapping_pair(
                        mapping_concept_id=mapping_concept_id,
                        mapping_doc=mapping_docs.get(mapping_concept_id),
                        step_id=step_id,
                        action_id=invokes_action,
                    )
                    if not context_key or not tool_param:
                        unresolved_input_mappings.append(mapping_concept_id)
                        if parse_error:
                            invalid_input_mapping_specs.append(
                                {
                                    "mapping_concept_id": mapping_concept_id,
                                    "reason_code": parse_error,
                                }
                            )
                        continue
                    input_map[tool_param] = {
                        "$context_key": context_key,
                        "$mapping_concept_id": mapping_concept_id,
                    }
                    reads_context_keys.append(context_key)

            actions.append(
                WorkflowActionInvocation(
                    action_id=invokes_action,
                    inputs=input_map if input_map else {},
                )
            )

            semantic_output_mapping_ids = step.get("tool_output_context_mappings") or []
            if isinstance(semantic_output_mapping_ids, list):
                for mapping_concept_id in semantic_output_mapping_ids:
                    if not isinstance(mapping_concept_id, str) or not mapping_concept_id:
                        continue
                    tool_output_field, context_key, parse_error = _extract_tool_output_mapping(
                        mapping_concept_id=mapping_concept_id,
                        mapping_doc=mapping_docs.get(mapping_concept_id),
                        step_id=step_id,
                        action_id=invokes_action,
                    )
                    if not tool_output_field or not context_key:
                        unresolved_output_mappings.append(mapping_concept_id)
                        if parse_error:
                            invalid_output_mapping_specs.append(
                                {
                                    "mapping_concept_id": mapping_concept_id,
                                    "reason_code": parse_error,
                                }
                            )
                        continue
                    tool_output_context_mappings.append(
                        {
                            "tool_output_field": tool_output_field,
                            "context_key": context_key,
                            "mapping_concept_id": mapping_concept_id,
                        }
                    )

        transitions: list[WorkflowTransitionSpec] = []

        # --- Build transitions with correct variable capture ---
        # Use default-argument binding (val=val) to capture the current
        # loop iteration's values, avoiding Python's late-binding closure bug.

        on_failure_target = control_flow.get("on_failure")
        on_true_target = control_flow.get("on_true")
        on_false_target = control_flow.get("on_false")
        next_target = control_flow.get("next")

        # Priority: on_failure → on_true/on_false → next (unconditional).

        if on_failure_target:
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=on_failure_target,
                    condition=lambda ctx, _t=on_failure_target: bool(
                        ctx.get("last_action_failed")
                    ),
                    reason="on_failure",
                )
            )

        if on_true_target:
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=on_true_target,
                    condition=lambda ctx, _t=on_true_target: bool(
                        ctx.get("last_step_ok") or ctx.get("result")
                    ),
                    reason="on_true",
                )
            )

        if on_false_target:
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=on_false_target,
                    condition=lambda ctx, _t=on_false_target: not bool(
                        ctx.get("last_step_ok") or ctx.get("result")
                    ),
                    reason="on_false",
                )
            )

        if next_target:
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=next_target,
                    condition=lambda ctx, _t=next_target: True,
                    reason="next_step",
                )
            )

        is_terminal = not transitions and not next_target

        # Carry Vontology metadata through for introspection.
        step_metadata: Dict[str, Any] = {}
        preconditions = step.get("preconditions")
        effects = step.get("effects")
        reads_vars = step.get("reads_variables")
        writes_vars = step.get("writes_variables")
        writes_context_keys = step.get("writes_context_keys")
        if preconditions:
            step_metadata["preconditions"] = preconditions
        if effects:
            step_metadata["effects"] = effects
        if reads_vars:
            step_metadata["reads_variables"] = reads_vars
        if reads_context_keys:
            step_metadata["reads_context_keys"] = reads_context_keys
        if writes_vars:
            step_metadata["writes_variables"] = writes_vars
        if writes_context_keys:
            step_metadata["writes_context_keys"] = writes_context_keys
        if tool_output_context_mappings:
            step_metadata["tool_output_context_mappings"] = tool_output_context_mappings
        if unresolved_input_mappings:
            step_metadata["unresolved_input_mappings"] = unresolved_input_mappings
        if invalid_input_mapping_specs:
            step_metadata["invalid_input_mapping_specs"] = invalid_input_mapping_specs
        if unresolved_output_mappings:
            step_metadata["unresolved_output_mappings"] = unresolved_output_mappings
        if invalid_output_mapping_specs:
            step_metadata["invalid_output_mapping_specs"] = invalid_output_mapping_specs

        states[step_id] = WorkflowStateSpec(
            state_id=step_id,
            actions=actions,
            transitions=transitions,
            terminal=is_terminal,
            metadata=step_metadata,
        )

    purpose = best_effort_workflow_narrative_text(workflow_id)

    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=initial_state_id,
        states=states,
        purpose=purpose,
    )


def _get_recursive_subtypes(root_type_id: str, max_depth: int = 3) -> set[str]:
    """Find all concept IDs that are subtypes of the given root type (transitively)."""
    found = set()
    queue = [root_type_id]
    depth = 0

    while queue and depth < max_depth:
        # Find immediate subtypes: concepts where (is_a_type_of == current OR has_subtype contains current?? No.
        # Subtype relationship: Child -> is_a_type_of -> Parent
        # OR Parent -> has_subtype -> Child.
        # We should check both, but efficiently.

        # Query: relationships.is_a_type_of IN queue
        # AND relationships.is_a_type_of is a list of strings

        parents = list(queue)
        queue = []

        # Find children via is_a_type_of (most common)
        cursor = ConceptsRepository.find(
            {"relationships.is_a_type_of": {"$in": parents}}, {"concept_id": 1}
        )
        for doc in cursor:
            cid = doc.get("concept_id")
            if cid and cid not in found and cid != root_type_id:
                found.add(cid)
                queue.append(cid)

        # Find children via has_subtype (less common but possible)
        # We need to query concepts that have has_subtype pointing to... no...
        # We need to query the PARENT documents to see their has_subtype.

        parent_docs = ConceptsRepository.find(
            {"concept_id": {"$in": parents}},
            {"relationships.has_subtype": 1, "relationships.#V#has_subtype": 1},
        )
        for pdoc in parent_docs:
            rels = pdoc.get("relationships") or {}
            targets = _all_relationship_targets(rels, ("has_subtype", "#V#has_subtype"))
            for t in targets:
                if t and t not in found and t != root_type_id:
                    # Verify target exists and is a concept? optional
                    found.add(t)
                    queue.append(t)

        depth += 1

    return found


def _discover_workflow_type_family_ids() -> set[str]:
    """Return workflow base types plus discovered subtypes."""

    type_ids: set[str] = set()
    for base_type_id in WORKFLOW_DISCOVERY_BASE_TYPE_IDS:
        if isinstance(base_type_id, str) and base_type_id:
            type_ids.add(base_type_id)
            type_ids.update(_get_recursive_subtypes(base_type_id))
    return type_ids


def discover_workflow_ids() -> List[str]:
    """Find all potential Vontology-defined workflows.

    Heuristic:
    1. Concepts that define a workflow structure (have hasInitialStep aliases).
    2. Concepts typed under known workflow roots (instance or subtype paths)
       that define an initial step.
    """
    candidates = set()

    # 1. Direct property search (best effort, includes canonical and legacy aliases)
    try:
        direct_initial_step_query = [
            {f"relationships.{predicate}": {"$exists": True}}
            for predicate in WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInitialStep"]
        ]
        cursor = ConceptsRepository.find(
            {"$or": direct_initial_step_query},
            {"concept_id": 1},
        )
        for doc in cursor:
            concept_id = doc.get("concept_id")
            if isinstance(concept_id, str) and concept_id.strip():
                candidates.add(concept_id.strip())
    except Exception as e:
        logger.warning(f"Error querying workflows by property: {e}")

    # 2. Type-based discovery over workflow type families. This catches
    # instance-typed workflow concepts that use canonical #V# graph predicates.
    workflow_type_ids = sorted(_discover_workflow_type_family_ids())
    if workflow_type_ids:
        try:
            typed_cursor = ConceptsRepository.find(
                {
                    "$or": [
                        {"relationships.is_an_instance_of": {"$in": workflow_type_ids}},
                        {"relationships.is_a_type_of": {"$in": workflow_type_ids}},
                    ]
                },
                {"concept_id": 1, "relationships": 1},
            )
            for doc in typed_cursor:
                concept_id = doc.get("concept_id")
                if not isinstance(concept_id, str) or not concept_id.strip():
                    continue
                rels = doc.get("relationships") or {}
                if not isinstance(rels, dict):
                    continue
                initial_step = _first_relationship_target(
                    rels,
                    WORKFLOW_GRAPH_PREDICATE_ALIASES["hasInitialStep"],
                )
                if initial_step:
                    candidates.add(concept_id.strip())
        except Exception as e:
            logger.warning(f"Error querying workflows by type: {e}")

    return sorted(candidates)
