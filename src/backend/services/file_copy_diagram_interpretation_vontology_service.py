"""Vontology-backed file-copy diagram interpretation support.

This service keeps file-copy diagram organisation/relation interpretation in an
authoritative prompt surface rather than Python regex heuristics. Python keeps
only prompt support, LLM invocation, structured-output validation, and fail-
closed defaults when the authority surface is unavailable.
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

FILE_COPY_DIAGRAM_INTERPRETATION_SCHEMA_VERSION = (
    "file_copy_diagram_interpretation.v1"
)
FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID = (
    "#V#prompt_file_copy_diagram_interpretation"
)
FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_LINK_PREDICATE = (
    "#V#has_file_copy_diagram_interpretation_prompt"
)

_MANAGED_BY = "file_copy_diagram_interpretation_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1916"
_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_LINK_PREDICATE,
    "has_file_copy_diagram_interpretation_prompt",
    "hasFileCopyDiagramInterpretationPrompt",
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_file_copy_diagram_interpretation_seed.md"
)
_PROMPT_SUPPORT_LOCK = threading.Lock()
_PROMPT_SUPPORT_READY = False


def _load_file_copy_diagram_interpretation_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("file_copy_diagram_interpretation_prompt_seed_missing")
    return prompt_text


def resolve_file_copy_diagram_interpretation_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_WORKFLOW_LINK_TEXT_PREDICATES,
        default_prompt_concept_id=FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID,
    )


def ensure_file_copy_diagram_interpretation_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID,
                name="File-copy diagram interpretation prompt",
                description=(
                    "Canonical prompt for interpreting OCR/prose file-copy "
                    "segments into candidate organisations and relation hints "
                    "that remain explicitly subject to human confirmation."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=FILE_COPY_INTERPRETATION_WORKFLOW_ID,
                prompt_concept_id=FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID,
                predicate=FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_LINK_PREDICATE,
                context={"jira": _SOURCE_TAG},
                reason="file_copy_diagram_interpretation_prompt_link_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_file_copy_diagram_interpretation_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    prompt_ready = prompt_concept_has_content(
        FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
    )
    if prompt_ready:
        errors_by_target.pop(FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if (
            FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
            not in validated_prompt_ids
        ):
            validated_prompt_ids.append(
                FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
            )
        report["validated_prompt_ids"] = validated_prompt_ids

    report["errors_by_target"] = errors_by_target
    report["missing_content_prompt_ids"] = missing_content_prompt_ids
    report["counts"] = {
        "created_prompts": len(report.get("created_prompt_ids") or []),
        "validated_prompts": len(report.get("validated_prompt_ids") or []),
        "missing_content_prompts": len(missing_content_prompt_ids),
        "linked_workflows": len(report.get("linked_workflow_ids") or []),
        "errors": len(errors_by_target),
    }
    report["source"] = _SOURCE_TAG
    report["managed_by"] = _MANAGED_BY
    report["prompt_concept_id"] = FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["success"] = not errors_by_target and prompt_ready
    return report


def _ensure_prompt_support_once() -> dict[str, Any]:
    global _PROMPT_SUPPORT_READY
    if _PROMPT_SUPPORT_READY:
        return {"success": True, "cached": True}
    with _PROMPT_SUPPORT_LOCK:
        if _PROMPT_SUPPORT_READY:
            return {"success": True, "cached": True}
        report = ensure_file_copy_diagram_interpretation_prompt_support()
        _PROMPT_SUPPORT_READY = bool(report.get("success"))
        return report


def render_file_copy_diagram_interpretation_prompt(
    *,
    workflow_id: str | None = FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 16000,
) -> tuple[Any | None, dict[str, Any]]:
    support_report = _ensure_prompt_support_once()
    resolved_prompt_id = resolve_file_copy_diagram_interpretation_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="file_copy_diagram_interpretation",
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


def _clean_string_list(values: Sequence[Any] | None) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for raw in values or []:
        cleaned = safe_str(raw)
        if not cleaned:
            continue
        lowered = cleaned.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(cleaned)
    return output


def _normalise_segment_refs(
    values: Sequence[Any] | None,
    *,
    known_segment_ids: set[str],
) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        cleaned = safe_str(raw)
        if not cleaned or cleaned not in known_segment_ids or cleaned in seen:
            continue
        seen.add(cleaned)
        refs.append(cleaned)
    return refs


def _normalise_relation_hint(value: Any) -> str:
    cleaned = safe_str(value)
    if not cleaned:
        return "unknown"
    lowered = cleaned.casefold()
    if lowered in {"directed_link", "directed", "arrow"}:
        return "directed_link"
    if lowered in {"association", "undirected", "related"}:
        return "association"
    return "unknown"


def _default_payload() -> dict[str, Any]:
    return {
        "schema_version": FILE_COPY_DIAGRAM_INTERPRETATION_SCHEMA_VERSION,
        "organisation_candidates": [],
        "relationship_candidates": [],
    }


def _normalise_payload(
    raw_payload: Any,
    *,
    known_segment_ids: set[str],
    max_candidates: int,
) -> dict[str, Any]:
    payload = _default_payload()
    if not isinstance(raw_payload, Mapping):
        return payload

    organisation_candidates: list[dict[str, Any]] = []
    seen_org_names: set[str] = set()
    raw_orgs = raw_payload.get("organisation_candidates")
    if isinstance(raw_orgs, Sequence) and not isinstance(raw_orgs, (str, bytes)):
        for raw_entry in raw_orgs:
            if not isinstance(raw_entry, Mapping):
                continue
            name = safe_str(raw_entry.get("name"))
            if not name:
                continue
            lowered = name.casefold()
            if lowered in seen_org_names:
                continue
            segment_refs = _normalise_segment_refs(
                raw_entry.get("segment_refs")
                if isinstance(raw_entry.get("segment_refs"), Sequence)
                else [],
                known_segment_ids=known_segment_ids,
            )
            if not segment_refs:
                continue
            seen_org_names.add(lowered)
            organisation_candidates.append(
                {
                    "name": name,
                    "segment_refs": segment_refs,
                    "evidence_excerpt": safe_str(raw_entry.get("evidence_excerpt")),
                }
            )
            if len(organisation_candidates) >= max_candidates:
                break
    payload["organisation_candidates"] = organisation_candidates

    relationship_candidates: list[dict[str, Any]] = []
    seen_relation_keys: set[tuple[str, str, str]] = set()
    raw_relations = raw_payload.get("relationship_candidates")
    if isinstance(raw_relations, Sequence) and not isinstance(
        raw_relations, (str, bytes)
    ):
        for raw_entry in raw_relations:
            if not isinstance(raw_entry, Mapping):
                continue
            source_name = safe_str(raw_entry.get("source_name"))
            target_name = safe_str(raw_entry.get("target_name"))
            if not source_name or not target_name:
                continue
            relation_hint = _normalise_relation_hint(raw_entry.get("relation_hint"))
            key = (
                source_name.casefold(),
                target_name.casefold(),
                relation_hint,
            )
            if key in seen_relation_keys:
                continue
            segment_refs = _normalise_segment_refs(
                raw_entry.get("segment_refs")
                if isinstance(raw_entry.get("segment_refs"), Sequence)
                else [],
                known_segment_ids=known_segment_ids,
            )
            if not segment_refs:
                continue
            seen_relation_keys.add(key)
            relationship_candidates.append(
                {
                    "source_name": source_name,
                    "target_name": target_name,
                    "relation_hint": relation_hint,
                    "segment_refs": segment_refs,
                    "evidence_excerpt": safe_str(raw_entry.get("evidence_excerpt")),
                }
            )
            if len(relationship_candidates) >= max_candidates:
                break
    payload["relationship_candidates"] = relationship_candidates
    return payload


def infer_file_copy_diagram_semantics(
    *,
    segments: Sequence[Mapping[str, Any]] | None,
    max_candidates: int = 40,
    allowed_organisation_names: Sequence[str] | None = None,
    llm_client: Any | None = None,
    model: str | None = None,
    workflow_id: str | None = FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prepared_segments: list[dict[str, Any]] = []
    known_segment_ids: set[str] = set()
    for raw_segment in segments or []:
        if not isinstance(raw_segment, Mapping):
            continue
        segment_id = safe_str(raw_segment.get("segment_id"))
        text = safe_str(raw_segment.get("text"))
        if not segment_id or not text:
            continue
        if segment_id in known_segment_ids:
            continue
        known_segment_ids.add(segment_id)
        prepared_segments.append(
            {
                "segment_id": segment_id,
                "source_type": safe_str(raw_segment.get("source_type")),
                "page_number": raw_segment.get("page_number"),
                "figure_id": safe_str(raw_segment.get("figure_id")),
                "extraction_method": safe_str(raw_segment.get("extraction_method")),
                "text": text,
            }
        )

    diagnostics: dict[str, Any] = {
        "schema_version": FILE_COPY_DIAGRAM_INTERPRETATION_SCHEMA_VERSION,
        "status": "skipped_no_segments",
        "segment_count": len(prepared_segments),
        "allowed_organisation_name_count": len(
            _clean_string_list(allowed_organisation_names)
        ),
    }
    if not prepared_segments:
        return _default_payload(), diagnostics

    rendered, render_diagnostics = render_file_copy_diagram_interpretation_prompt(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        variables={
            "segments_json": json.dumps(
                prepared_segments,
                ensure_ascii=True,
                sort_keys=True,
            ),
            "allowed_organisation_names_json": json.dumps(
                _clean_string_list(allowed_organisation_names),
                ensure_ascii=True,
                sort_keys=True,
            ),
            "max_candidates": int(max(1, max_candidates)),
        },
        max_chars=16000,
    )
    diagnostics.update(render_diagnostics)
    if rendered is None or not getattr(rendered, "text", None):
        diagnostics["status"] = "prompt_unavailable"
        return _default_payload(), diagnostics

    effective_llm_client = llm_client
    if effective_llm_client is None:
        try:
            effective_llm_client = _get_llm_client()
        except Exception as exc:
            diagnostics["status"] = "llm_unavailable"
            diagnostics["error"] = f"{type(exc).__name__}:{exc}"
            return _default_payload(), diagnostics

    if model is None:
        try:
            model = _get_active_model_name()
        except Exception:
            model = None
    diagnostics["model"] = model

    try:
        raw_response = effective_llm_client.generate(
            prompt="Infer file-copy diagram interpretation candidates",
            context=[{"role": "system", "content": str(rendered.text)}],
            model=model,
        )
    except Exception as exc:
        diagnostics["status"] = "llm_error"
        diagnostics["error"] = f"{type(exc).__name__}:{exc}"
        return _default_payload(), diagnostics

    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    diagnostics["parse_mode"] = parse_mode
    if not isinstance(parsed_payload, Mapping):
        diagnostics["status"] = "parse_failed"
        diagnostics["error"] = "structured_file_copy_diagram_interpretation_parse_failed"
        return _default_payload(), diagnostics

    diagnostics["status"] = "ok"
    payload = _normalise_payload(
        parsed_payload,
        known_segment_ids=known_segment_ids,
        max_candidates=max(1, int(max_candidates)),
    )
    return payload, diagnostics


__all__ = [
    "FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_CONCEPT_ID",
    "FILE_COPY_DIAGRAM_INTERPRETATION_PROMPT_LINK_PREDICATE",
    "FILE_COPY_DIAGRAM_INTERPRETATION_SCHEMA_VERSION",
    "_load_file_copy_diagram_interpretation_prompt_seed_text",
    "ensure_file_copy_diagram_interpretation_prompt_support",
    "infer_file_copy_diagram_semantics",
    "render_file_copy_diagram_interpretation_prompt",
    "resolve_file_copy_diagram_interpretation_prompt_concept_id",
]
