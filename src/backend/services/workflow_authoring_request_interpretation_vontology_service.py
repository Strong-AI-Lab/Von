"""Authoritative workflow-authoring request interpretation support.

This service keeps workflow naming and text-profile interpretation in
Vontology-backed prompt surfaces rather than English lexical heuristics in
Python. Python remains responsible for prompt support, structured-output
validation, and fail-closed defaults when the authority surface is unavailable.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    WorkflowPromptLinkSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
    render_authoritative_prompt,
    resolve_linked_prompt_concept_id,
    safe_str,
)
from .text_value_service import upsert_singleton_text_relation
from ..workflows.workflow_creation_contracts import WORKFLOW_CREATION_WORKFLOW_ID

WORKFLOW_AUTHORING_IDENTITY_SCHEMA_VERSION = "workflow_authoring_identity_inference.v1"
WORKFLOW_AUTHORING_PROFILE_SCHEMA_VERSION = (
    "workflow_authoring_profile_interpretation.v1"
)
WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID = (
    "#V#prompt_workflow_authoring_identity_inference"
)
WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID = (
    "#V#prompt_workflow_authoring_profile_interpretation"
)
WORKFLOW_AUTHORING_IDENTITY_PROMPT_LINK_PREDICATE = (
    "#V#has_workflow_authoring_identity_prompt"
)
WORKFLOW_AUTHORING_PROFILE_PROMPT_LINK_PREDICATE = (
    "#V#has_workflow_authoring_profile_interpretation_prompt"
)

_MANAGED_BY = "workflow_authoring_request_interpretation_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1917"
_IDENTITY_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    WORKFLOW_AUTHORING_IDENTITY_PROMPT_LINK_PREDICATE,
    "has_workflow_authoring_identity_prompt",
    "hasWorkflowAuthoringIdentityPrompt",
)
_PROFILE_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    WORKFLOW_AUTHORING_PROFILE_PROMPT_LINK_PREDICATE,
    "has_workflow_authoring_profile_interpretation_prompt",
    "hasWorkflowAuthoringProfileInterpretationPrompt",
)
_IDENTITY_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_workflow_authoring_identity_inference_seed.md"
)
_PROFILE_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_workflow_authoring_profile_interpretation_seed.md"
)
_PROMPT_SUPPORT_LOCK = threading.Lock()
_PROMPT_SUPPORT_READY = False


def _load_prompt_seed_text(path: Path, *, error_code: str) -> str:
    prompt_text = path.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError(error_code)
    return prompt_text


def resolve_workflow_authoring_identity_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_IDENTITY_WORKFLOW_LINK_TEXT_PREDICATES,
        default_prompt_concept_id=WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID,
    )


def resolve_workflow_authoring_profile_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_PROFILE_WORKFLOW_LINK_TEXT_PREDICATES,
        default_prompt_concept_id=WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID,
    )


def ensure_workflow_authoring_request_interpretation_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID,
                name="Workflow authoring identity inference prompt",
                description=(
                    "Canonical prompt for inferring durable workflow names and "
                    "optional explicit workflow IDs from workflow-authoring requests."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID,
                name="Workflow authoring profile interpretation prompt",
                description=(
                    "Canonical prompt for extracting PhD-student workflow profile "
                    "fields from free text without English label heuristics."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
                prompt_concept_id=WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID,
                predicate=WORKFLOW_AUTHORING_IDENTITY_PROMPT_LINK_PREDICATE,
                context={"jira": _SOURCE_TAG},
                reason="workflow_authoring_identity_prompt_link_bootstrap",
            ),
            WorkflowPromptLinkSpec(
                workflow_id=WORKFLOW_CREATION_WORKFLOW_ID,
                prompt_concept_id=WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID,
                predicate=WORKFLOW_AUTHORING_PROFILE_PROMPT_LINK_PREDICATE,
                context={"jira": _SOURCE_TAG},
                reason="workflow_authoring_profile_prompt_link_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    seed_specs = (
        (
            WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID,
            _IDENTITY_PROMPT_SEED_ASSET_PATH,
            "workflow_authoring_identity_prompt_seed_missing",
        ),
        (
            WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID,
            _PROFILE_PROMPT_SEED_ASSET_PATH,
            "workflow_authoring_profile_prompt_seed_missing",
        ),
    )
    for prompt_concept_id, asset_path, error_code in seed_specs:
        if force_prompt_seed or not prompt_concept_has_content(prompt_concept_id):
            upsert_singleton_text_relation(
                subject_concept_id=prompt_concept_id,
                predicate="hasContent",
                text=_load_prompt_seed_text(asset_path, error_code=error_code),
                lang="en-NZ",
                context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
                garbage_collect=True,
            )
            seeded_prompt_ids.append(prompt_concept_id)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
    for prompt_concept_id in (
        WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID,
        WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID,
    ):
        if prompt_concept_has_content(prompt_concept_id):
            errors_by_target.pop(prompt_concept_id, None)
            missing_content_prompt_ids = [
                current
                for current in missing_content_prompt_ids
                if current != prompt_concept_id
            ]
            if prompt_concept_id not in validated_prompt_ids:
                validated_prompt_ids.append(prompt_concept_id)

    report["errors_by_target"] = errors_by_target
    report["missing_content_prompt_ids"] = missing_content_prompt_ids
    report["validated_prompt_ids"] = validated_prompt_ids
    report["counts"] = {
        "created_prompts": len(report.get("created_prompt_ids") or []),
        "validated_prompts": len(validated_prompt_ids),
        "missing_content_prompts": len(missing_content_prompt_ids),
        "linked_workflows": len(report.get("linked_workflow_ids") or []),
        "errors": len(errors_by_target),
    }
    report["source"] = _SOURCE_TAG
    report["managed_by"] = _MANAGED_BY
    report["prompt_concept_ids"] = [
        WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID,
        WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID,
    ]
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["success"] = not errors_by_target and not missing_content_prompt_ids
    return report


def _ensure_prompt_support_once() -> dict[str, Any]:
    global _PROMPT_SUPPORT_READY
    if _PROMPT_SUPPORT_READY:
        return {"success": True, "cached": True}
    with _PROMPT_SUPPORT_LOCK:
        if _PROMPT_SUPPORT_READY:
            return {"success": True, "cached": True}
        report = ensure_workflow_authoring_request_interpretation_prompt_support()
        _PROMPT_SUPPORT_READY = bool(report.get("success"))
        return report


def render_workflow_authoring_identity_prompt(
    *,
    workflow_id: str | None = WORKFLOW_CREATION_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 12000,
) -> tuple[Any | None, dict[str, Any]]:
    support_report = _ensure_prompt_support_once()
    resolved_prompt_id = resolve_workflow_authoring_identity_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="workflow_authoring_identity",
    )
    diagnostics["requested_workflow_id"] = safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = safe_str(prompt_concept_id)
    diagnostics["prompt_support"] = dict(support_report)
    return rendered, diagnostics


def render_workflow_authoring_profile_prompt(
    *,
    workflow_id: str | None = WORKFLOW_CREATION_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 12000,
) -> tuple[Any | None, dict[str, Any]]:
    support_report = _ensure_prompt_support_once()
    resolved_prompt_id = resolve_workflow_authoring_profile_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="workflow_authoring_profile",
    )
    diagnostics["requested_workflow_id"] = safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = safe_str(prompt_concept_id)
    diagnostics["prompt_support"] = dict(support_report)
    return rendered, diagnostics


def _get_llm_client() -> Any:
    from ..languagemodels.llm_interface import get_llm_client

    return get_llm_client()


def _get_active_model_name() -> str | None:
    from ..languagemodels.llm_interface import get_active_model_name

    return get_active_model_name()


def _extract_json_payload(raw_text: str) -> tuple[Any, str]:
    cleaned = str(raw_text or "").strip()
    if not cleaned:
        return None, "empty"

    try:
        return json.loads(cleaned), "strict_json"
    except Exception:
        pass

    if "```" in cleaned:
        start = cleaned.find("```")
        end = cleaned.rfind("```")
        if end > start:
            fenced = cleaned[start + 3 : end].strip()
            if fenced.lower().startswith("json"):
                fenced = fenced[4:].strip()
            try:
                return json.loads(fenced), "fenced_json"
            except Exception:
                pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            return json.loads(candidate), "braced_json"
        except Exception:
            pass

    return None, "unparsed"


def _clean_string(value: Any) -> str:
    return safe_str(value) or ""


def _clean_string_list(values: Sequence[Any] | None) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for raw in values or []:
        cleaned = _clean_string(raw)
        if not cleaned:
            continue
        lowered = cleaned.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(cleaned)
    return output


def _default_identity_payload() -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_AUTHORING_IDENTITY_SCHEMA_VERSION,
        "target_workflow_name": "",
        "target_workflow_id": None,
        "workflow_description": "",
    }


def _normalise_identity_payload(raw_payload: Any) -> dict[str, Any]:
    payload = _default_identity_payload()
    if not isinstance(raw_payload, Mapping):
        return payload
    payload["target_workflow_name"] = _clean_string(
        raw_payload.get("target_workflow_name")
        or raw_payload.get("workflow_name")
        or raw_payload.get("name")
    )
    target_workflow_id = _clean_string(
        raw_payload.get("target_workflow_id") or raw_payload.get("workflow_id")
    )
    payload["target_workflow_id"] = target_workflow_id or None
    payload["workflow_description"] = _clean_string(
        raw_payload.get("workflow_description") or raw_payload.get("description")
    )
    return payload


def _default_profile_payload(*, source_text: str) -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_AUTHORING_PROFILE_SCHEMA_VERSION,
        "student_name": "",
        "supervisor_names": [],
        "research_topic": "",
        "institution": "",
        "source_text": _clean_string(source_text),
    }


def _normalise_profile_payload(raw_payload: Any, *, source_text: str) -> dict[str, Any]:
    payload = _default_profile_payload(source_text=source_text)
    if not isinstance(raw_payload, Mapping):
        return payload
    payload["student_name"] = _clean_string(
        raw_payload.get("student_name") or raw_payload.get("phd_student_name")
    )
    payload["supervisor_names"] = _clean_string_list(
        raw_payload.get("supervisor_names") or raw_payload.get("supervisors")
    )
    payload["research_topic"] = _clean_string(
        raw_payload.get("research_topic") or raw_payload.get("research_area")
    )
    payload["institution"] = _clean_string(
        raw_payload.get("institution")
        or raw_payload.get("university")
        or raw_payload.get("department")
    )
    return payload


def infer_workflow_authoring_identity(
    *,
    request_text: str,
    llm_client: Any | None = None,
    model: str | None = None,
    workflow_id: str | None = WORKFLOW_CREATION_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "schema_version": WORKFLOW_AUTHORING_IDENTITY_SCHEMA_VERSION,
        "status": "ok",
        "request_text_length": len(_clean_string(request_text)),
    }
    cleaned_request_text = _clean_string(request_text)
    if not cleaned_request_text:
        diagnostics["status"] = "missing_request_text"
        return _default_identity_payload(), diagnostics

    rendered, render_diagnostics = render_workflow_authoring_identity_prompt(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        variables={
            "request_text": cleaned_request_text,
        },
        max_chars=12000,
    )
    diagnostics.update(render_diagnostics)
    if rendered is None or not getattr(rendered, "text", None):
        diagnostics["status"] = "prompt_unavailable"
        return _default_identity_payload(), diagnostics

    effective_llm_client = llm_client
    if effective_llm_client is None:
        try:
            effective_llm_client = _get_llm_client()
        except Exception as exc:
            diagnostics["status"] = "llm_unavailable"
            diagnostics["error"] = f"{type(exc).__name__}:{exc}"
            return _default_identity_payload(), diagnostics

    if model is None:
        try:
            model = _get_active_model_name()
        except Exception:
            model = None
    diagnostics["model"] = model

    try:
        raw_response = effective_llm_client.generate(
            prompt="Infer workflow authoring identity",
            context=[{"role": "system", "content": str(rendered.text)}],
            model=model,
        )
    except Exception as exc:
        diagnostics["status"] = "llm_error"
        diagnostics["error"] = f"{type(exc).__name__}:{exc}"
        return _default_identity_payload(), diagnostics

    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    diagnostics["parse_mode"] = parse_mode
    if not isinstance(parsed_payload, Mapping):
        diagnostics["status"] = "parse_failed"
        diagnostics["error"] = "workflow_authoring_identity_parse_failed"
        return _default_identity_payload(), diagnostics

    payload = _normalise_identity_payload(parsed_payload)
    if not payload["target_workflow_name"] and not payload["target_workflow_id"]:
        diagnostics["status"] = "missing_identity_fields"
    return payload, diagnostics


def infer_workflow_authoring_profile(
    *,
    source_text: str,
    llm_client: Any | None = None,
    model: str | None = None,
    workflow_id: str | None = WORKFLOW_CREATION_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cleaned_source_text = _clean_string(source_text)
    diagnostics: dict[str, Any] = {
        "schema_version": WORKFLOW_AUTHORING_PROFILE_SCHEMA_VERSION,
        "status": "ok",
        "source_text_length": len(cleaned_source_text),
    }
    if not cleaned_source_text:
        diagnostics["status"] = "missing_source_text"
        return _default_profile_payload(source_text=""), diagnostics

    rendered, render_diagnostics = render_workflow_authoring_profile_prompt(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        variables={
            "source_text": cleaned_source_text,
        },
        max_chars=12000,
    )
    diagnostics.update(render_diagnostics)
    if rendered is None or not getattr(rendered, "text", None):
        diagnostics["status"] = "prompt_unavailable"
        return _default_profile_payload(source_text=cleaned_source_text), diagnostics

    effective_llm_client = llm_client
    if effective_llm_client is None:
        try:
            effective_llm_client = _get_llm_client()
        except Exception as exc:
            diagnostics["status"] = "llm_unavailable"
            diagnostics["error"] = f"{type(exc).__name__}:{exc}"
            return _default_profile_payload(source_text=cleaned_source_text), diagnostics

    if model is None:
        try:
            model = _get_active_model_name()
        except Exception:
            model = None
    diagnostics["model"] = model

    try:
        raw_response = effective_llm_client.generate(
            prompt="Interpret workflow authoring profile fields",
            context=[{"role": "system", "content": str(rendered.text)}],
            model=model,
        )
    except Exception as exc:
        diagnostics["status"] = "llm_error"
        diagnostics["error"] = f"{type(exc).__name__}:{exc}"
        return _default_profile_payload(source_text=cleaned_source_text), diagnostics

    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    diagnostics["parse_mode"] = parse_mode
    if not isinstance(parsed_payload, Mapping):
        diagnostics["status"] = "parse_failed"
        diagnostics["error"] = "workflow_authoring_profile_parse_failed"
        return _default_profile_payload(source_text=cleaned_source_text), diagnostics

    payload = _normalise_profile_payload(
        parsed_payload,
        source_text=cleaned_source_text,
    )
    if not payload["student_name"]:
        diagnostics["status"] = "student_name_missing"
    return payload, diagnostics


__all__ = [
    "WORKFLOW_AUTHORING_IDENTITY_PROMPT_CONCEPT_ID",
    "WORKFLOW_AUTHORING_IDENTITY_PROMPT_LINK_PREDICATE",
    "WORKFLOW_AUTHORING_IDENTITY_SCHEMA_VERSION",
    "WORKFLOW_AUTHORING_PROFILE_PROMPT_CONCEPT_ID",
    "WORKFLOW_AUTHORING_PROFILE_PROMPT_LINK_PREDICATE",
    "WORKFLOW_AUTHORING_PROFILE_SCHEMA_VERSION",
    "ensure_workflow_authoring_request_interpretation_prompt_support",
    "infer_workflow_authoring_identity",
    "infer_workflow_authoring_profile",
    "render_workflow_authoring_identity_prompt",
    "render_workflow_authoring_profile_prompt",
    "resolve_workflow_authoring_identity_prompt_concept_id",
    "resolve_workflow_authoring_profile_prompt_concept_id",
]
