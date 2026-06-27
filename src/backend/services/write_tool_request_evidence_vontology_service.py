"""Authoritative write-request evidence inference for write-tool policy.

This service keeps write-request interpretation in a Vontology-backed prompt
surface rather than ad-hoc prompt parsing inside Python guardrail code.
Python remains responsible for prompt materialisation, structured-output
validation, and fail-closed defaults when the authority surface is unavailable.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence

from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
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
from ..workflows.definitions import WRITE_TOOL_POLICY_WORKFLOW_ID
from ..workflows.write_tool_policy import (
    CONFIDENCE_EXPLICIT_CONFIRMATION,
    CONFIDENCE_EXPLICIT_DENIAL,
    CONFIDENCE_EXPLICIT_REQUEST,
    CONFIDENCE_LOW,
    CONFIDENCE_RECENT_CONFIRMATION,
    CONFIDENCE_RECENT_DENIAL,
    CONFIDENCE_RECENT_REQUEST,
    WRITE_TOOL_REQUEST_EVIDENCE_SCHEMA_VERSION,
    classify_write_tool_risk,
)

logger = logging.getLogger(__name__)

WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID = (
    "#V#prompt_write_tool_request_evidence_inference"
)
WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_LINK_PREDICATE = (
    "#V#has_write_tool_request_evidence_prompt"
)

_MANAGED_BY = "write_tool_request_evidence_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1918"
_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_LINK_PREDICATE,
    "has_write_tool_request_evidence_prompt",
    "hasWriteToolRequestEvidencePrompt",
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_write_tool_request_evidence_seed.md"
)
_PROMPT_SUPPORT_LOCK = threading.Lock()
_PROMPT_SUPPORT_READY = False


def _load_write_tool_request_evidence_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("write_tool_request_evidence_prompt_seed_missing")
    return prompt_text


def resolve_write_tool_request_evidence_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_WORKFLOW_LINK_TEXT_PREDICATES,
        default_prompt_concept_id=WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID,
    )


def _write_tool_request_evidence_prompt_seed_needs_refresh() -> bool:
    try:
        rows = get_texts_for_concept(
            subject_concept_id=WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID,
            limit=16,
        )
    except Exception:
        return False

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        predicate = safe_str(row.get("predicate"))
        if predicate not in {"hasContent", "#V#hasContent"}:
            continue
        text = safe_str(row.get("text")) or ""
        if text and (
            "denial_state" not in text
            or "external-system side effects" not in text
            or "gmail_send_message" not in text
            or "workflow_execute" not in text
        ):
            return True
    return False


def ensure_write_tool_request_evidence_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID,
                name="Write-tool request evidence inference prompt",
                description=(
                    "Canonical prompt for inferring structured write-request and "
                    "destructive-confirmation evidence for proposed write tools, "
                    "including guarded external side effects."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=WRITE_TOOL_POLICY_WORKFLOW_ID,
                prompt_concept_id=WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID,
                predicate=WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_LINK_PREDICATE,
                context={"jira": _SOURCE_TAG},
                reason="write_tool_request_evidence_prompt_link_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    prompt_has_content = prompt_concept_has_content(
        WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
    )
    if (
        force_prompt_seed
        or not prompt_has_content
        or _write_tool_request_evidence_prompt_seed_needs_refresh()
    ):
        upsert_singleton_text_relation(
            subject_concept_id=WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_write_tool_request_evidence_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    prompt_ready = prompt_concept_has_content(
        WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
    )
    if prompt_ready:
        errors_by_target.pop(WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID)
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
    report["prompt_concept_id"] = WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID
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
        report = ensure_write_tool_request_evidence_prompt_support()
        _PROMPT_SUPPORT_READY = bool(report.get("success"))
        return report


def render_write_tool_request_evidence_prompt(
    *,
    workflow_id: str | None = WRITE_TOOL_POLICY_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 12000,
) -> tuple[Any | None, dict[str, Any]]:
    support_report = _ensure_prompt_support_once()
    resolved_prompt_id = resolve_write_tool_request_evidence_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="write_tool_request_evidence",
    )
    diagnostics["requested_workflow_id"] = safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = safe_str(prompt_concept_id)
    diagnostics["prompt_support"] = dict(support_report)
    return rendered, diagnostics


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


def _clean_tool_names(values: Sequence[str] | None) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(cleaned)
    return output


def _default_evidence_for_tool(tool_name: str) -> dict[str, Any]:
    return {
        "schema_version": WRITE_TOOL_REQUEST_EVIDENCE_SCHEMA_VERSION,
        "tool_name": tool_name,
        "request_state": CONFIDENCE_LOW,
        "confirmation_state": CONFIDENCE_LOW,
        "denial_state": CONFIDENCE_LOW,
        "rationale": None,
    }


def _normalise_string_sequence(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = safe_str(item)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(text)
    return output


def augment_write_tool_request_evidence_with_turn_contract(
    *,
    request_evidence: Mapping[str, Mapping[str, Any] | None] | None,
    requested_tools: Sequence[str] | None,
    requested_tool_payloads: Mapping[str, Mapping[str, Any] | None] | None = None,
    turn_expected_outcome_contract: Mapping[str, Any] | None = None,
    activated_conditional_required_tools: Sequence[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Add exact-symbol request evidence from the represented turn contract.

    This is a fast structural path only. It does not interpret natural language:
    the workflow/prompt-authored expected-outcome contract must already name the
    requested tool, and the workflow_execute payload must name a workflow concept
    already present in that contract.
    """

    requested = _clean_tool_names(requested_tools)
    evidence = {
        str(tool_name): dict(tool_evidence)
        for tool_name, tool_evidence in (request_evidence or {}).items()
        if isinstance(tool_name, str) and isinstance(tool_evidence, Mapping)
    }
    diagnostics: dict[str, Any] = {
        "schema_version": WRITE_TOOL_REQUEST_EVIDENCE_SCHEMA_VERSION,
        "status": "no_contract_evidence_applied",
        "applied_tools": [],
    }
    if not requested or not isinstance(turn_expected_outcome_contract, Mapping):
        return evidence, diagnostics

    required_tools = {
        item.lower()
        for item in _normalise_string_sequence(
            turn_expected_outcome_contract.get("required_tools")
        )
    }
    activated_tools = {
        item.lower()
        for item in _normalise_string_sequence(activated_conditional_required_tools)
    }
    contract_workflow_ids = {
        item.lower()
        for item in _normalise_string_sequence(
            turn_expected_outcome_contract.get("workflow_concept_ids")
        )
    }
    if "workflow_execute" not in required_tools and "workflow_execute" not in activated_tools:
        return evidence, diagnostics
    if not contract_workflow_ids:
        return evidence, diagnostics

    payload_map = (
        dict(requested_tool_payloads)
        if isinstance(requested_tool_payloads, Mapping)
        else {}
    )
    applied_tools: list[str] = []
    for tool_name in requested:
        if tool_name.lower() != "workflow_execute":
            continue
        payload = payload_map.get(tool_name) or payload_map.get(tool_name.lower())
        if not isinstance(payload, Mapping):
            continue
        workflow_id = safe_str(payload.get("workflow_id") or payload.get("workflowId"))
        if not workflow_id or workflow_id.lower() not in contract_workflow_ids:
            continue
        existing = evidence.get(tool_name.lower()) or evidence.get(tool_name) or {}
        if (
            isinstance(existing, Mapping)
            and safe_str(existing.get("denial_state")) == CONFIDENCE_EXPLICIT_DENIAL
        ):
            continue
        evidence[tool_name.lower()] = {
            "schema_version": WRITE_TOOL_REQUEST_EVIDENCE_SCHEMA_VERSION,
            "tool_name": tool_name,
            "request_state": CONFIDENCE_EXPLICIT_REQUEST,
            "confirmation_state": (
                safe_str(existing.get("confirmation_state"))
                if isinstance(existing, Mapping)
                else CONFIDENCE_LOW
            )
            or CONFIDENCE_LOW,
            "denial_state": (
                safe_str(existing.get("denial_state"))
                if isinstance(existing, Mapping)
                else CONFIDENCE_LOW
            )
            or CONFIDENCE_LOW,
            "rationale": (
                "Represented turn expected-outcome contract requires "
                f"workflow_execute for workflow {workflow_id}."
            ),
        }
        applied_tools.append(tool_name)

    if applied_tools:
        diagnostics["status"] = "contract_evidence_applied"
        diagnostics["applied_tools"] = applied_tools
    return evidence, diagnostics


def _normalise_request_state(value: Any) -> str:
    cleaned = str(value or "").strip()
    if cleaned in {CONFIDENCE_EXPLICIT_REQUEST, CONFIDENCE_RECENT_REQUEST}:
        return cleaned
    return CONFIDENCE_LOW


def _normalise_confirmation_state(value: Any) -> str:
    cleaned = str(value or "").strip()
    if cleaned in {CONFIDENCE_EXPLICIT_CONFIRMATION, CONFIDENCE_RECENT_CONFIRMATION}:
        return cleaned
    return CONFIDENCE_LOW


def _normalise_denial_state(value: Any) -> str:
    cleaned = str(value or "").strip()
    if cleaned in {CONFIDENCE_EXPLICIT_DENIAL, CONFIDENCE_RECENT_DENIAL}:
        return cleaned
    return CONFIDENCE_LOW


def _normalise_tool_evidence(
    raw_payload: Any,
    *,
    requested_tools: Sequence[str],
) -> dict[str, dict[str, Any]]:
    requested = _clean_tool_names(requested_tools)
    default_map = {
        tool_name.lower(): _default_evidence_for_tool(tool_name)
        for tool_name in requested
    }
    if not isinstance(raw_payload, Mapping):
        return default_map

    entries = raw_payload.get("tool_evidence")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        entries = []
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            continue
        tool_name = safe_str(raw_entry.get("tool_name"))
        if not tool_name:
            continue
        lowered = tool_name.lower()
        if lowered not in default_map:
            continue
        default_map[lowered] = {
            "schema_version": WRITE_TOOL_REQUEST_EVIDENCE_SCHEMA_VERSION,
            "tool_name": default_map[lowered]["tool_name"],
            "request_state": _normalise_request_state(raw_entry.get("request_state")),
            "confirmation_state": _normalise_confirmation_state(
                raw_entry.get("confirmation_state")
            ),
            "denial_state": _normalise_denial_state(raw_entry.get("denial_state")),
            "rationale": safe_str(raw_entry.get("rationale")),
        }
    return default_map


def infer_write_tool_request_evidence(
    *,
    llm_client: Any,
    model: str | None,
    prompt: str,
    recent_user_prompts: Sequence[str] | None,
    requested_tools: Sequence[str] | None,
    requested_tool_payloads: Mapping[str, Mapping[str, Any] | None] | None = None,
    workflow_id: str | None = WRITE_TOOL_POLICY_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    requested = _clean_tool_names(requested_tools)
    diagnostics: dict[str, Any] = {
        "schema_version": WRITE_TOOL_REQUEST_EVIDENCE_SCHEMA_VERSION,
        "status": "skipped_no_requested_tools",
        "requested_tool_count": len(requested),
    }
    if not requested:
        return {}, diagnostics

    requested_payload_summary = []
    payload_map = (
        dict(requested_tool_payloads)
        if isinstance(requested_tool_payloads, Mapping)
        else {}
    )
    for tool_name in requested:
        payload = payload_map.get(tool_name) or payload_map.get(tool_name.lower())
        requested_payload_summary.append(
            {
                "tool_name": tool_name,
                "risk_class": classify_write_tool_risk(tool_name),
                "payload": dict(payload) if isinstance(payload, Mapping) else {},
            }
        )

    rendered, render_diagnostics = render_write_tool_request_evidence_prompt(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        variables={
            "current_prompt": str(prompt or "").strip(),
            "recent_user_prompts_json": json.dumps(
                [
                    str(item).strip()
                    for item in (recent_user_prompts or [])
                    if str(item).strip()
                ],
                ensure_ascii=True,
                sort_keys=True,
            ),
            "requested_tools_json": json.dumps(
                requested_payload_summary,
                ensure_ascii=True,
                sort_keys=True,
            ),
        },
        max_chars=12000,
    )
    diagnostics.update(render_diagnostics)
    if rendered is None or not getattr(rendered, "text", None):
        diagnostics["status"] = "prompt_unavailable"
        return (
            _normalise_tool_evidence({}, requested_tools=requested),
            diagnostics,
        )
    if llm_client is None or not hasattr(llm_client, "generate"):
        diagnostics["status"] = "llm_unavailable"
        return (
            _normalise_tool_evidence({}, requested_tools=requested),
            diagnostics,
        )

    try:
        raw_response = llm_client.generate(
            prompt="Infer write-tool request evidence",
            context=[{"role": "system", "content": str(rendered.text)}],
            model=model,
        )
    except Exception as exc:
        diagnostics["status"] = "llm_error"
        diagnostics["error"] = f"{type(exc).__name__}:{exc}"
        return (
            _normalise_tool_evidence({}, requested_tools=requested),
            diagnostics,
        )

    parsed_payload, parse_mode = _extract_json_payload(str(raw_response or ""))
    diagnostics["parse_mode"] = parse_mode
    diagnostics["status"] = (
        "ok" if isinstance(parsed_payload, Mapping) else "parse_failed"
    )
    normalised = _normalise_tool_evidence(
        parsed_payload if isinstance(parsed_payload, Mapping) else {},
        requested_tools=requested,
    )
    if diagnostics["status"] != "ok":
        diagnostics["error"] = "structured_request_evidence_parse_failed"
    return normalised, diagnostics


__all__ = [
    "WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_CONCEPT_ID",
    "WRITE_TOOL_REQUEST_EVIDENCE_PROMPT_LINK_PREDICATE",
    "_load_write_tool_request_evidence_prompt_seed_text",
    "augment_write_tool_request_evidence_with_turn_contract",
    "ensure_write_tool_request_evidence_prompt_support",
    "infer_write_tool_request_evidence",
    "render_write_tool_request_evidence_prompt",
    "resolve_write_tool_request_evidence_prompt_concept_id",
]
