"""Authority-backed file-copy entity representation interpretation support.

This service keeps file-copy person/company/meeting candidate interpretation in
an authoritative prompt surface rather than Python regex heuristics. Python
keeps only prompt support, LLM invocation, structured-output validation, and
fail-closed defaults when the authority surface is unavailable.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from .text_value_service import upsert_singleton_text_relation
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
from ..workflows.durable.file_copy_interpretation_workflow import (
    FILE_COPY_INTERPRETATION_WORKFLOW_ID,
)

FILE_COPY_ENTITY_REPRESENTATION_SCHEMA_VERSION = (
    "file_copy_entity_representation_interpretation.v1"
)
FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID = (
    "#V#prompt_file_copy_entity_representation_interpretation"
)
FILE_COPY_ENTITY_REPRESENTATION_PROMPT_LINK_PREDICATE = (
    "#V#has_file_copy_entity_representation_prompt"
)

_MANAGED_BY = "file_copy_entity_representation_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1952"
_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    FILE_COPY_ENTITY_REPRESENTATION_PROMPT_LINK_PREDICATE,
    "has_file_copy_entity_representation_prompt",
    "hasFileCopyEntityRepresentationPrompt",
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_file_copy_entity_representation_seed.md"
)
_PROMPT_SUPPORT_LOCK = threading.Lock()
_PROMPT_SUPPORT_READY = False


def _load_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("file_copy_entity_representation_prompt_seed_missing")
    return prompt_text


def resolve_file_copy_entity_representation_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_WORKFLOW_LINK_TEXT_PREDICATES,
        default_prompt_concept_id=FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID,
    )


def ensure_file_copy_entity_representation_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID,
                name="File-copy entity representation interpretation prompt",
                description=(
                    "Canonical prompt for interpreting uploaded file-copy text and "
                    "metadata into candidate person, company, and meeting "
                    "representations without Python semantic heuristics."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=FILE_COPY_INTERPRETATION_WORKFLOW_ID,
                prompt_concept_id=FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID,
                predicate=FILE_COPY_ENTITY_REPRESENTATION_PROMPT_LINK_PREDICATE,
                context={"jira": _SOURCE_TAG},
                reason="file_copy_entity_representation_prompt_link_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
    prompt_ready = prompt_concept_has_content(
        FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID
    )
    if prompt_ready:
        errors_by_target.pop(FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID
        ]
        if (
            FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID
            not in validated_prompt_ids
        ):
            validated_prompt_ids.append(
                FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID
            )

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
    report["prompt_concept_id"] = FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID
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
        report = ensure_file_copy_entity_representation_prompt_support()
        _PROMPT_SUPPORT_READY = bool(report.get("success"))
        return report


def render_file_copy_entity_representation_prompt(
    *,
    workflow_id: str | None = FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 16000,
) -> tuple[Any | None, dict[str, Any]]:
    support_report = _ensure_prompt_support_once()
    resolved_prompt_id = resolve_file_copy_entity_representation_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="file_copy_entity_representation",
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


def default_person_representation_candidate() -> dict[str, Any]:
    return {
        "applicable": False,
        "representation_mode": None,
        "person_name": "",
        "emails": [],
        "phone_numbers": [],
        "affiliations": [],
        "roles": [],
        "evidence_fragments": [],
    }


def default_company_representation_candidate() -> dict[str, Any]:
    return {
        "applicable": False,
        "representation_mode": None,
        "company_name": "",
        "aliases": [],
        "urls": [],
        "descriptors": [],
        "evidence_fragments": [],
    }


def default_meeting_representation_candidate() -> dict[str, Any]:
    return {
        "applicable": False,
        "representation_mode": None,
        "meeting_name": "",
        "datetime_candidates": [],
        "participants": [],
        "outcomes": [],
        "evidence_fragments": [],
    }


def default_file_copy_entity_representation_payload() -> dict[str, Any]:
    return {
        "schema_version": FILE_COPY_ENTITY_REPRESENTATION_SCHEMA_VERSION,
        "person_candidate": default_person_representation_candidate(),
        "company_candidate": default_company_representation_candidate(),
        "meeting_candidate": default_meeting_representation_candidate(),
    }


def normalise_file_copy_person_candidate(raw_payload: Any) -> dict[str, Any]:
    payload = default_person_representation_candidate()
    if not isinstance(raw_payload, Mapping):
        return payload
    payload["applicable"] = bool(raw_payload.get("applicable"))
    representation_mode = _clean_string(raw_payload.get("representation_mode"))
    payload["representation_mode"] = representation_mode or None
    payload["person_name"] = _clean_string(
        raw_payload.get("person_name") or raw_payload.get("name")
    )
    payload["emails"] = _clean_string_list(raw_payload.get("emails"))
    payload["phone_numbers"] = _clean_string_list(
        raw_payload.get("phone_numbers") or raw_payload.get("phones")
    )
    payload["affiliations"] = _clean_string_list(raw_payload.get("affiliations"))
    payload["roles"] = _clean_string_list(raw_payload.get("roles"))
    payload["evidence_fragments"] = _clean_string_list(
        raw_payload.get("evidence_fragments") or raw_payload.get("evidence")
    )
    return payload


def normalise_file_copy_company_candidate(raw_payload: Any) -> dict[str, Any]:
    payload = default_company_representation_candidate()
    if not isinstance(raw_payload, Mapping):
        return payload
    payload["applicable"] = bool(raw_payload.get("applicable"))
    representation_mode = _clean_string(raw_payload.get("representation_mode"))
    payload["representation_mode"] = representation_mode or None
    payload["company_name"] = _clean_string(
        raw_payload.get("company_name") or raw_payload.get("name")
    )
    payload["aliases"] = _clean_string_list(raw_payload.get("aliases"))
    payload["urls"] = _clean_string_list(
        raw_payload.get("urls")
        or raw_payload.get("website_urls")
        or raw_payload.get("source_urls")
    )
    payload["descriptors"] = _clean_string_list(raw_payload.get("descriptors"))
    payload["evidence_fragments"] = _clean_string_list(
        raw_payload.get("evidence_fragments") or raw_payload.get("evidence")
    )
    return payload


def normalise_file_copy_meeting_candidate(raw_payload: Any) -> dict[str, Any]:
    payload = default_meeting_representation_candidate()
    if not isinstance(raw_payload, Mapping):
        return payload
    payload["applicable"] = bool(raw_payload.get("applicable"))
    representation_mode = _clean_string(raw_payload.get("representation_mode"))
    payload["representation_mode"] = representation_mode or None
    payload["meeting_name"] = _clean_string(
        raw_payload.get("meeting_name") or raw_payload.get("name")
    )
    payload["datetime_candidates"] = _clean_string_list(
        raw_payload.get("datetime_candidates") or raw_payload.get("datetimes")
    )
    payload["participants"] = _clean_string_list(raw_payload.get("participants"))
    payload["outcomes"] = _clean_string_list(raw_payload.get("outcomes"))
    payload["evidence_fragments"] = _clean_string_list(
        raw_payload.get("evidence_fragments") or raw_payload.get("evidence")
    )
    return payload


def normalise_file_copy_entity_representation_payload(
    raw_payload: Any,
) -> dict[str, Any]:
    payload = default_file_copy_entity_representation_payload()
    if not isinstance(raw_payload, Mapping):
        return payload
    payload["person_candidate"] = normalise_file_copy_person_candidate(
        raw_payload.get("person_candidate")
    )
    payload["company_candidate"] = normalise_file_copy_company_candidate(
        raw_payload.get("company_candidate")
    )
    payload["meeting_candidate"] = normalise_file_copy_meeting_candidate(
        raw_payload.get("meeting_candidate")
    )
    return payload


def _summarise_interpretation(interpretation: Mapping[str, Any] | None) -> str:
    if not isinstance(interpretation, Mapping):
        return "{}"
    summary: dict[str, Any] = {}
    for key in (
        "kind",
        "description",
        "source_url",
        "canonical_url",
        "url",
        "content_length",
        "content_type",
    ):
        value = interpretation.get(key)
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            summary[key] = value
    subject_tags = interpretation.get("subject_tags")
    if isinstance(subject_tags, Sequence) and not isinstance(subject_tags, str):
        summary["subject_tags"] = _clean_string_list(subject_tags)[:12]
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)


def infer_file_copy_entity_representation_candidates(
    *,
    extracted_text: str,
    original_filename: str | None = None,
    content_type: str | None = None,
    interpretation: Mapping[str, Any] | None = None,
    llm_client: Any | None = None,
    model: str | None = None,
    workflow_id: str | None = FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    cleaned_text = _clean_string(extracted_text)
    diagnostics: dict[str, Any] = {
        "schema_version": FILE_COPY_ENTITY_REPRESENTATION_SCHEMA_VERSION,
        "status": "ok",
        "extracted_text_length": len(cleaned_text),
        "original_filename": _clean_string(original_filename),
        "content_type": _clean_string(content_type),
    }
    if not cleaned_text:
        diagnostics["status"] = "missing_extracted_text"
        return default_file_copy_entity_representation_payload(), diagnostics

    rendered, render_diagnostics = render_file_copy_entity_representation_prompt(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        variables={
            "original_filename": _clean_string(original_filename),
            "content_type": _clean_string(content_type),
            "interpretation_json": _summarise_interpretation(interpretation),
            "extracted_text": cleaned_text,
        },
        max_chars=16000,
    )
    diagnostics.update(render_diagnostics)
    if rendered is None or not getattr(rendered, "text", None):
        diagnostics["status"] = "prompt_unavailable"
        return default_file_copy_entity_representation_payload(), diagnostics

    effective_llm_client = llm_client
    if effective_llm_client is None:
        try:
            effective_llm_client = _get_llm_client()
        except Exception as exc:
            diagnostics["status"] = "llm_unavailable"
            diagnostics["error"] = f"{type(exc).__name__}:{exc}"
            return default_file_copy_entity_representation_payload(), diagnostics

    if model is None:
        try:
            model = _get_active_model_name()
        except Exception:
            model = None
    diagnostics["model"] = model

    try:
        raw_response = effective_llm_client.generate(
            prompt="Interpret file-copy entity representation candidates",
            context=[{"role": "system", "content": str(rendered.text)}],
            model=model,
        )
    except Exception as exc:
        diagnostics["status"] = "llm_error"
        diagnostics["error"] = f"{type(exc).__name__}:{exc}"
        return default_file_copy_entity_representation_payload(), diagnostics

    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    diagnostics["parse_mode"] = parse_mode
    if not isinstance(parsed_payload, Mapping):
        diagnostics["status"] = "parse_failed"
        diagnostics["error"] = "file_copy_entity_representation_parse_failed"
        return default_file_copy_entity_representation_payload(), diagnostics

    payload = normalise_file_copy_entity_representation_payload(parsed_payload)
    if not any(
        payload[candidate_key].get("applicable")
        for candidate_key in (
            "person_candidate",
            "company_candidate",
            "meeting_candidate",
        )
    ):
        diagnostics["status"] = "no_candidates"
    return payload, diagnostics


__all__ = [
    "FILE_COPY_ENTITY_REPRESENTATION_PROMPT_CONCEPT_ID",
    "FILE_COPY_ENTITY_REPRESENTATION_PROMPT_LINK_PREDICATE",
    "FILE_COPY_ENTITY_REPRESENTATION_SCHEMA_VERSION",
    "default_company_representation_candidate",
    "default_file_copy_entity_representation_payload",
    "default_meeting_representation_candidate",
    "default_person_representation_candidate",
    "ensure_file_copy_entity_representation_prompt_support",
    "infer_file_copy_entity_representation_candidates",
    "normalise_file_copy_company_candidate",
    "normalise_file_copy_entity_representation_payload",
    "normalise_file_copy_meeting_candidate",
    "normalise_file_copy_person_candidate",
    "render_file_copy_entity_representation_prompt",
    "resolve_file_copy_entity_representation_prompt_concept_id",
]
