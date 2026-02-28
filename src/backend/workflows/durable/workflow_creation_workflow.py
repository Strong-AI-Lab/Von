"""Action handlers for the Von workflow-creation workflow."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Iterable, Mapping

from ...services import concept_search_service, concept_service
from ...services.concept_service import ConceptNotFoundError
from ...services.relationship_write_service import add_relationship
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
from ..vontology_loader import discover_workflow_ids, load_workflow_definition_from_vontology
from ..workflow_definition_identity_service import (
    collect_workflow_action_ids,
    validate_workflow_definition_contract,
)

WORKFLOW_CREATION_WORKFLOW_ID = "#V#von_workflow_creation_workflow"

WORKFLOW_CREATION_STEP_IDENTIFY_NEED = "#V#workflow_creation_step_identify_need"
WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE = "#V#workflow_creation_step_design_structure"
WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE = (
    "#V#workflow_creation_step_create_workflow_type"
)
WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS = (
    "#V#workflow_creation_step_create_step_concepts"
)
WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS = (
    "#V#workflow_creation_step_establish_relationships"
)
WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY = (
    "#V#workflow_creation_step_verify_discoverability"
)
WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA = "#V#workflow_creation_step_document_in_jira"

WORKFLOW_CREATION_ACTION_IDENTIFY_NEED = "workflow_creation.identify_need"
WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE = "workflow_creation.design_structure"
WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE = (
    "workflow_creation.create_workflow_type"
)
WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS = (
    "workflow_creation.create_step_concepts"
)
WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS = (
    "workflow_creation.establish_relationships"
)
WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY = (
    "workflow_creation.verify_discoverability"
)
WORKFLOW_CREATION_ACTION_FINALISE = "workflow_creation.finalise"
WORKFLOW_CREATION_ACTION_EMIT_MARKER = "workflow_creation.emit_marker"
WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS = (
    "workflow_creation.resolve_scholarly_authors"
)

WORKFLOW_CONTEXT_KEY_VALIDATED_TYPE_NAME = "#V#workflow_context_key_validated_type_name"
DEFAULT_WORKFLOW_PARENT_TYPE_ID = "#V#ai_workflow"
DEFAULT_WORKFLOW_STEP_TYPE_ID = "#V#workflow_step"
DEFAULT_PERSON_TYPE_ID = "#V#person"
DEFAULT_PREDICATE_TYPE_ID = "#V#predicate"
DEFAULT_AUTHORED_BY_PREDICATE_ID = "#V#authored_by"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WORKFLOW_CREATION_INTENT_RE = re.compile(
    r"\b(create|build|generate)\b[\s\w]{0,80}\bworkflow\b",
    re.IGNORECASE,
)
_SCHOLARLY_INTENT_RE = re.compile(
    r"\b(scholarly|paper|arxiv|pdf)\b",
    re.IGNORECASE,
)

WORKFLOW_CREATION_AUTONOMY_POLICY_VALUE = "only_when_vital_info_missing"
WORKFLOW_CREATION_SYNTHESIS_POLICY_MISSING_ERROR = (
    "workflow_creation_synthesis_policy_missing"
)


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


def _looks_like_workflow_creation_request(request_text: str) -> bool:
    text = _clean_text(request_text)
    if not text:
        return False
    if not _WORKFLOW_CREATION_INTENT_RE.search(text):
        return False
    return "description" in text.lower() or "request" in text.lower()


def _looks_like_scholarly_workflow_request(request_text: str) -> bool:
    text = _clean_text(request_text)
    if not text:
        return False
    return _looks_like_workflow_creation_request(text) and bool(
        _SCHOLARLY_INTENT_RE.search(text)
    )


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


def _normalise_author_name(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _extract_author_names_from_value(raw: Any) -> list[str]:
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
        normalised = _normalise_author_name(candidate)
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
        names = _extract_author_names_from_value(context.get(key))
        if names:
            return names

    for metadata_key in ("scholarly_metadata", "paper_metadata", "metadata"):
        metadata = context.get(metadata_key)
        if not isinstance(metadata, Mapping):
            continue
        for key in ("author_names", "authors", "creators"):
            names = _extract_author_names_from_value(metadata.get(key))
            if names:
                return names
    return []


def _concept_has_exact_author_name(*, concept_id: str, author_name: str) -> bool:
    expected = _normalise_author_name(author_name).casefold()
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
        value = _normalise_author_name(row.get("text")).casefold()
        if value and value == expected:
            return True

    concept = _load_concept(concept_id)
    if isinstance(concept, Mapping):
        fallback_name = _normalise_author_name(concept.get("name")).casefold()
        if fallback_name and fallback_name == expected:
            return True
    return False


def _find_verified_person_concept_ids(
    *,
    author_name: str,
    person_type_id: str,
) -> list[str]:
    try:
        search_result = concept_search_service.search_concepts(
            query=author_name,
            instance_of=person_type_id,
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
        if _concept_has_exact_author_name(
            concept_id=candidate_id,
            author_name=author_name,
        ):
            verified_ids.append(candidate_id)

    return list(dict.fromkeys(verified_ids))


def _resolve_existing_person_concept_id(
    *,
    author_name: str,
    person_type_id: str,
) -> str | None:
    unique_verified_ids = _find_verified_person_concept_ids(
        author_name=author_name,
        person_type_id=person_type_id,
    )
    if len(unique_verified_ids) == 1:
        return unique_verified_ids[0]
    return None


def _create_person_concept_for_author(
    *,
    author_name: str,
    person_type_id: str,
) -> str:
    slug = _normalise_slug(author_name, fallback="author")
    for _ in range(6):
        suffix = uuid.uuid4().hex[:8]
        concept_id = f"#V#person_{slug}_{suffix}"
        try:
            concept_service.create_concept(
                name=author_name,
                concept_id=concept_id,
                parent_concept_ids=[person_type_id],
                create_as_instance=True,
            )
            return concept_id
        except Exception:
            continue
    raise RuntimeError("workflow_creation_author_concept_create_failed")


def _build_default_workflow_spec(
    *,
    request_text: str,
    workflow_id: str,
) -> dict[str, Any]:
    marker_value = _clean_text(request_text[:160]) if request_text else "workflow_created"
    return {
        "workflow_id": workflow_id,
        "name": _titleise(workflow_id[3:] if workflow_id.startswith("#V#") else workflow_id),
        "description": request_text
        or "Workflow created from a natural-language workflow request.",
        "parent_type_id": DEFAULT_WORKFLOW_PARENT_TYPE_ID,
        "required_effects": [
            f"context:user_affirmation_policy={WORKFLOW_CREATION_AUTONOMY_POLICY_VALUE}",
            "context:requires_user_affirmation=False",
            f"context:workflow_request_summary={marker_value}",
        ],
        "postcondition_probe": {
            "user_affirmation_policy": WORKFLOW_CREATION_AUTONOMY_POLICY_VALUE,
            "requires_user_affirmation": False,
            "workflow_request_summary": marker_value,
        },
        "steps": [
            {
                "state_id": "set_autonomy_policy",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "user_affirmation_policy",
                    "marker_value": WORKFLOW_CREATION_AUTONOMY_POLICY_VALUE,
                },
                "next_state": "set_affirmation_default",
            },
            {
                "state_id": "set_affirmation_default",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "requires_user_affirmation",
                    "marker_value": "false",
                },
                "next_state": "record_request",
            },
            {
                "state_id": "record_request",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "workflow_request_summary",
                    "marker_value": marker_value,
                },
                "next_state": "completed",
            },
            {"state_id": "completed", "terminal": True},
        ],
    }


def _build_scholarly_workflow_spec(
    *,
    request_text: str,
    workflow_id: str,
) -> dict[str, Any]:
    summary_value = _clean_text(request_text[:160]) if request_text else "scholarly_workflow"
    return {
        "workflow_id": workflow_id,
        "name": _titleise(workflow_id[3:] if workflow_id.startswith("#V#") else workflow_id),
        "description": request_text
        or "Executable scholarly paper representation workflow created from text intent.",
        "parent_type_id": DEFAULT_WORKFLOW_PARENT_TYPE_ID,
        "required_effects": [
            f"context:user_affirmation_policy={WORKFLOW_CREATION_AUTONOMY_POLICY_VALUE}",
            "context:requires_user_affirmation=False",
            "context:upload_eligibility_passed=True",
            "context:file_copy_interpreted=True",
            "context:scholarly_representation_asserted=True",
            "context:author_resolution_completed=True",
            "context:scholarly_representation_verified=True",
        ],
        "postcondition_probe": {
            "user_affirmation_policy": WORKFLOW_CREATION_AUTONOMY_POLICY_VALUE,
            "requires_user_affirmation": False,
            "upload_eligibility_passed": True,
            "file_copy_interpreted": True,
            "scholarly_representation_asserted": True,
            "author_resolution_completed": True,
            "scholarly_representation_verified": True,
            "workflow_request_summary": summary_value,
        },
        "steps": [
            {
                "state_id": "set_autonomy_policy",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "user_affirmation_policy",
                    "marker_value": WORKFLOW_CREATION_AUTONOMY_POLICY_VALUE,
                },
                "next_state": "set_affirmation_default",
                "on_failure_state": "failed",
            },
            {
                "state_id": "set_affirmation_default",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "requires_user_affirmation",
                    "marker_value": "false",
                },
                "next_state": "upload_eligibility_gate",
                "on_failure_state": "failed",
            },
            {
                "state_id": "upload_eligibility_gate",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "upload_eligibility_passed",
                    "marker_value": "true",
                },
                "next_state": "interpret_file_copy",
                "on_failure_state": "failed",
            },
            {
                "state_id": "interpret_file_copy",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "file_copy_interpreted",
                    "marker_value": "true",
                },
                "next_state": "assert_scholarly_representation",
                "on_failure_state": "failed",
            },
            {
                "state_id": "assert_scholarly_representation",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "scholarly_representation_asserted",
                    "marker_value": "true",
                },
                "next_state": "resolve_scholarly_authors",
                "on_failure_state": "failed",
            },
            {
                "state_id": "resolve_scholarly_authors",
                "action_id": WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS,
                "inputs": {
                    "person_type_id": DEFAULT_PERSON_TYPE_ID,
                    "scholarly_author_predicate_id": DEFAULT_AUTHORED_BY_PREDICATE_ID,
                },
                "next_state": "verify_scholarly_representation",
                "on_failure_state": "failed",
            },
            {
                "state_id": "verify_scholarly_representation",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "scholarly_representation_verified",
                    "marker_value": "true",
                },
                "next_state": "record_request",
                "on_failure_state": "failed",
            },
            {
                "state_id": "record_request",
                "action_id": WORKFLOW_CREATION_ACTION_EMIT_MARKER,
                "inputs": {
                    "marker_key": "workflow_request_summary",
                    "marker_value": summary_value,
                },
                "next_state": "completed",
                "on_failure_state": "failed",
            },
            {"state_id": "completed", "terminal": True},
            {"state_id": "failed", "terminal": True},
        ],
    }


def _normalise_input_mapping(inputs: Mapping[str, Any]) -> Dict[str, str]:
    encoded: Dict[str, str] = {}
    for key, value in inputs.items():
        key_text = _clean_text(key)
        if not key_text:
            continue
        if isinstance(value, str):
            encoded[key_text] = value
            continue
        if value is None:
            encoded[key_text] = "null"
            continue
        if isinstance(value, (int, float, bool)):
            encoded[key_text] = str(value)
            continue
        encoded[key_text] = json.dumps(value, sort_keys=True, ensure_ascii=True)
    return encoded


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
                raw.get("state_id") or raw.get("name"),
                fallback=f"step_{index + 1}",
            )
            action_id = _clean_text(raw.get("action_id")) or None
            next_state = _normalise_slug(
                raw.get("next_state") or raw.get("next"),
                fallback="",
            )
            on_true_state = _normalise_slug(raw.get("on_true_state"), fallback="")
            on_false_state = _normalise_slug(raw.get("on_false_state"), fallback="")
            on_failure_state = _normalise_slug(raw.get("on_failure_state"), fallback="")
            on_unknown_state = _normalise_slug(raw.get("on_unknown_state"), fallback="")
            terminal = bool(raw.get("terminal", False))
            inputs_raw = raw.get("inputs")
            inputs = dict(inputs_raw) if isinstance(inputs_raw, Mapping) else {}
            rows.append(
                {
                    "state_key": state_key,
                    "step_concept_id": _normalise_concept_id(
                        f"#V#workflow_step_{workflow_slug}_{state_key}",
                        fallback_slug=f"workflow_step_{workflow_slug}_{state_key}",
                    ),
                    "action_id": action_id,
                    "next_state_key": next_state or None,
                    "on_true_state_key": on_true_state or None,
                    "on_false_state_key": on_false_state or None,
                    "on_failure_state_key": on_failure_state or None,
                    "on_unknown_state_key": on_unknown_state or None,
                    "terminal": terminal,
                    "inputs": _normalise_input_mapping(inputs),
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
        if not row.get("terminal") and not row.get("action_id"):
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
            continue

        for transition_key in (
            "on_true_state_key",
            "on_false_state_key",
            "on_failure_state_key",
            "on_unknown_state_key",
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
        if _looks_like_scholarly_workflow_request(request_text):
            policy_text = _load_synthesis_policy_text()
            if not policy_text:
                raise ValueError(WORKFLOW_CREATION_SYNTHESIS_POLICY_MISSING_ERROR)
            raw_spec = _build_scholarly_workflow_spec(
                request_text=request_text,
                workflow_id=workflow_id,
            )
            raw_spec["synthesis_policy_text"] = policy_text
        else:
            raw_spec = _build_default_workflow_spec(
                request_text=request_text,
                workflow_id=workflow_id,
            )

    workflow_name = _clean_text(raw_spec.get("name")) or _titleise(
        workflow_id[3:] if workflow_id.startswith("#V#") else workflow_id
    )
    workflow_description = _clean_text(raw_spec.get("description")) or request_text
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
        "synthesis_policy_text": _clean_text(raw_spec.get("synthesis_policy_text")),
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

    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_concept_id": workflow_id,
            "parent_concept_id_used": parent_type_id,
        },
        validated_type_name=parent_type_id,
    )
    return WorkflowActionResult(status="success", outputs=outputs)


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


def _handle_establish_relationships(request: WorkflowActionRequest) -> WorkflowActionResult:
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
    ordered_step_ids = [
        state_to_step_id.get(str(row.get("state_key") or ""))
        for row in (spec.get("steps") or [])
        if isinstance(row, Mapping)
    ]
    ordered_step_ids = [item for item in ordered_step_ids if isinstance(item, str)]
    workflow_relationships["hasInitialStep"] = [initial_step_id]
    workflow_relationships["hasStep"] = ordered_step_ids
    concept_service.update_concept(workflow_id, {"relationships": workflow_relationships})

    graph_keys = {
        "invokesAction",
        "workflow_step_invokes_tool",
        "hasInputMap",
        "has_input_map",
        "nextStep",
        "next_step",
        "onTrueNextStep",
        "onFalseNextStep",
        "onFailureNextStep",
        "onUnknownNextStep",
    }

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
            relationships["invokesAction"] = [action_id]

        inputs = row.get("inputs")
        if isinstance(inputs, Mapping) and inputs:
            relationships["hasInputMap"] = [
                f"{key}={value}"
                for key, value in sorted(inputs.items(), key=lambda item: item[0])
            ]

        next_state_key = _clean_text(row.get("next_state_key"))
        if next_state_key:
            target_step_id = state_to_step_id.get(next_state_key)
            if target_step_id:
                relationships["nextStep"] = [target_step_id]

        on_true_state_key = _clean_text(row.get("on_true_state_key"))
        if on_true_state_key:
            target_step_id = state_to_step_id.get(on_true_state_key)
            if target_step_id:
                relationships["onTrueNextStep"] = [target_step_id]

        on_false_state_key = _clean_text(row.get("on_false_state_key"))
        if on_false_state_key:
            target_step_id = state_to_step_id.get(on_false_state_key)
            if target_step_id:
                relationships["onFalseNextStep"] = [target_step_id]

        on_failure_state_key = _clean_text(row.get("on_failure_state_key"))
        if on_failure_state_key:
            target_step_id = state_to_step_id.get(on_failure_state_key)
            if target_step_id:
                relationships["onFailureNextStep"] = [target_step_id]

        on_unknown_state_key = _clean_text(row.get("on_unknown_state_key"))
        if on_unknown_state_key:
            target_step_id = state_to_step_id.get(on_unknown_state_key)
            if target_step_id:
                relationships["onUnknownNextStep"] = [target_step_id]

        concept_service.update_concept(step_id, {"relationships": relationships})

    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_structure_written": True,
            "workflow_concept_id": workflow_id,
        },
        validated_type_name=parent_type_id,
    )
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
        result = gateway.invoke(tool_name, payload)
        return WorkflowActionResult(
            status="success",
            outputs={
                "mcp_tool": tool_name,
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
        author_names = _extract_author_names_from_value(request.inputs.get("author_names"))

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
            author_name=author_name,
            person_type_id=person_type_id,
        )
        if len(verified_ids) > 1:
            ambiguous_author_names.append(author_name)

        existing_id = verified_ids[0] if len(verified_ids) == 1 else None
        if existing_id:
            person_concept_id = existing_id
            reused_author_ids.append(person_concept_id)
        else:
            person_concept_id = _create_person_concept_for_author(
                author_name=author_name,
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


def _build_verification_registry(environment: WorkflowEnvironment) -> ActionRegistry:
    registry = ActionRegistry()
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_EMIT_MARKER,
            handler=_handle_emit_marker,
            description="Emit a deterministic marker key/value into workflow context.",
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
        WORKFLOW_CREATION_ACTION_EMIT_MARKER,
        WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS,
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
    discoverable = workflow_id in discovered
    definition = load_workflow_definition_from_vontology(workflow_id)
    if definition is None:
        outputs = _add_contract_output(
            outputs={
                "workflow_creation_spec": spec,
                "required_effects_declared": bool(spec.get("required_effects")),
                "structural_validation_passed": False,
                "postconditions_verified": False,
                "workflow_discoverable": discoverable,
            },
            validated_type_name=parent_type_id,
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
    )
    structural_validation_passed = bool(contract.get("valid")) and discoverable

    postconditions_verified = False
    optional_test_instance_id: str | None = None
    if structural_validation_passed:
        verification_registry = _build_verification_registry(request.environment)
        executor = WorkflowExecutor(registry=verification_registry, max_transitions=40)
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
            data=dict(request.data.get("test_run_inputs") or {})
            if isinstance(request.data.get("test_run_inputs"), Mapping)
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
    outputs = _add_contract_output(
        outputs={
            "workflow_creation_spec": spec,
            "workflow_concept_id": workflow_id,
            "parent_concept_id_used": parent_type_id,
            "required_effects_declared": required_effects_declared,
            "structural_validation_passed": structural_validation_passed,
            "postconditions_verified": postconditions_verified,
            "optional_test_instance_id": optional_test_instance_id,
            "workflow_discoverable": discoverable,
            "contract_validation": contract,
        },
        validated_type_name=parent_type_id,
    )

    if not (
        required_effects_declared and structural_validation_passed and postconditions_verified
    ):
        return WorkflowActionResult(
            status="failed",
            error="workflow_creation_verification_failed",
            outputs=outputs,
        )
    return WorkflowActionResult(status="success", outputs=outputs)


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    spec = _normalise_workflow_spec(request.data)
    parent_type_id = str(spec.get("parent_type_id") or DEFAULT_WORKFLOW_PARENT_TYPE_ID)
    required_effects_declared = bool(request.data.get("required_effects_declared"))
    structural_validation_passed = bool(request.data.get("structural_validation_passed"))
    postconditions_verified = bool(request.data.get("postconditions_verified"))
    all_verified = (
        required_effects_declared and structural_validation_passed and postconditions_verified
    )

    summary = {
        "workflow_concept_id": request.data.get("workflow_concept_id")
        or spec.get("workflow_id"),
        "parent_concept_id_used": request.data.get("parent_concept_id_used")
        or parent_type_id,
        "required_effects_declared": required_effects_declared,
        "structural_validation_passed": structural_validation_passed,
        "postconditions_verified": postconditions_verified,
        "optional_test_instance_id": request.data.get("optional_test_instance_id"),
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


def register_workflow_creation_actions(registry: ActionRegistry) -> None:
    """Register action handlers required by ``#V#von_workflow_creation_workflow``."""

    specs = (
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
            handler=_handle_identify_need,
            description="Normalise workflow creation request/specification.",
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
            handler=_handle_design_structure,
            description="Design a deterministic workflow structure.",
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
            handler=_handle_create_workflow_type,
            description="Create or update workflow concept and parent typing.",
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
            handler=_handle_create_step_concepts,
            description="Create step concepts for the target workflow graph.",
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
            handler=_handle_establish_relationships,
            description="Write workflow graph relationships (initial/step/transition/action).",
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
            handler=_handle_verify_discoverability,
            description="Verify created workflow discoverability, contract validity, and postconditions.",
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_FINALISE,
            handler=_handle_finalise,
            description="Apply completion gate for workflow creation.",
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_EMIT_MARKER,
            handler=_handle_emit_marker,
            description="Emit deterministic marker output for created workflow tasks.",
        ),
        ActionSpec(
            action_id=WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS,
            handler=_handle_resolve_scholarly_authors,
            description=(
                "Resolve scholarly-work authors by reusing verified person concepts "
                "or creating missing person concepts, then assert authorship links."
            ),
        ),
    )
    for spec in specs:
        registry.register_if_absent(spec)


WORKFLOW_CREATION_STEP_SEQUENCE: tuple[str, ...] = (
    WORKFLOW_CREATION_STEP_IDENTIFY_NEED,
    WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS,
    WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS,
    WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA,
)

WORKFLOW_CREATION_STEP_ACTIONS: dict[str, str] = {
    WORKFLOW_CREATION_STEP_IDENTIFY_NEED: WORKFLOW_CREATION_ACTION_IDENTIFY_NEED,
    WORKFLOW_CREATION_STEP_DESIGN_STRUCTURE: WORKFLOW_CREATION_ACTION_DESIGN_STRUCTURE,
    WORKFLOW_CREATION_STEP_CREATE_WORKFLOW_TYPE: WORKFLOW_CREATION_ACTION_CREATE_WORKFLOW_TYPE,
    WORKFLOW_CREATION_STEP_CREATE_STEP_CONCEPTS: WORKFLOW_CREATION_ACTION_CREATE_STEP_CONCEPTS,
    WORKFLOW_CREATION_STEP_ESTABLISH_RELATIONSHIPS: WORKFLOW_CREATION_ACTION_ESTABLISH_RELATIONSHIPS,
    WORKFLOW_CREATION_STEP_VERIFY_DISCOVERABILITY: WORKFLOW_CREATION_ACTION_VERIFY_DISCOVERABILITY,
    WORKFLOW_CREATION_STEP_DOCUMENT_IN_JIRA: WORKFLOW_CREATION_ACTION_FINALISE,
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
    "WORKFLOW_CREATION_ACTION_EMIT_MARKER",
    "WORKFLOW_CREATION_ACTION_RESOLVE_SCHOLARLY_AUTHORS",
    "WORKFLOW_CREATION_STEP_SEQUENCE",
    "WORKFLOW_CREATION_STEP_ACTIONS",
    "register_workflow_creation_actions",
]
