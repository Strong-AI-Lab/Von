"""Durable workflow for conflation introspection and self-maintenance.

This workflow provides a deterministic maintenance loop for workflow/prompt
quality incidents:

1. gather turn/workflow/prompt evidence via internal MCP tools
2. diagnose conflation/root-cause signals
3. plan bounded repairs
4. optionally apply repairs via MCP write tools
5. verify that repairs are observable

The implementation intentionally prefers MCP tooling pathways over direct data
access so maintenance remains aligned with Vontology-first operations.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
import re
from threading import Lock
from typing import Any, Mapping, Sequence

from ...services.jira_deduplicated_issue_service import (
    upsert_deduplicated_jira_issue,
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

logger = logging.getLogger(__name__)

WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID = (
    "#V#workflow_introspection_maintenance_workflow"
)

_GUARDRAIL_MARKER = "WORKFLOW MAINTENANCE GUARDRAIL"
_DEFAULT_LANGUAGE = "en-NZ"
_DEFAULT_MAX_PROMPT_CHARS = 16_000
_DEFAULT_OPERATION_CAP = 8
_DEFAULT_GITHUB_REPOSITORY = "Strong-AI-Lab/Von"
_DEFAULT_JIRA_PROJECT_KEY = "JVNAUTOSCI"
_DEFAULT_JIRA_ISSUE_TYPE = "Task"
_MAX_GITHUB_EVIDENCE_PATHS = 3
_MAX_GITHUB_EVIDENCE_PREVIEW_CHARS = 1200
_SELF_DIAGNOSIS_LABELS: tuple[str, ...] = (
    "workflow-self-diagnosis",
    "github-readonly",
    "workflow-introspection",
)
_SELF_DIAGNOSIS_TOOL_NAME = "__jira_self_diagnosis_upsert__"
_SOURCE_PATH_PATTERN = re.compile(
    r"((?:src|tests|docs)/[A-Za-z0-9_.\-/]+\.(?:py|md|json|yml|yaml|ts|js|tsx|jsx))"
)

_ABSOLUTE_TERMS: tuple[str, ...] = (
    "always",
    "must",
    "whenever",
    "every time",
)
_OVERREACH_TERMS: tuple[str, ...] = (
    "when the user asks",
    "whenever the user",
    "for any request",
    "for all requests",
    "create, track, or manage",
)
_ONTOLOGY_HINT_TERMS: tuple[str, ...] = (
    "#v#",
    "ontology",
    "concept",
    "predicate",
    "relationship",
    "type",
    "instance",
)
_TOOL_NAME_PATTERN = re.compile(
    r"(?:use|call|invoke)\s+`?([a-z_][a-z0-9_]*)`?",
    flags=re.IGNORECASE,
)

# Legacy/alternate names that may appear in prompts but not in the active
# internal catalogue.
_TOOL_ALIAS_HINTS: dict[str, str] = {
    "create_task": "task_create",
    "get_task": "task_get",
    "update_task_status": "task_update_status",
    "assign_task": "task_assign",
}

_gateway_lock = Lock()
_gateway_singleton: Any | None = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _coerce_mapping_list(value: Any, *, max_items: int = 100) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value[:max_items]:
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return rows


def _append_trace_event(
    context: Mapping[str, Any],
    *,
    stage: str,
    event: str,
    details: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    trace = context.get("maintenance_trace")
    if isinstance(trace, list):
        events = [item for item in trace if isinstance(item, dict)]
    else:
        events = []
    row: dict[str, Any] = {
        "timestamp_utc": _utc_now_iso(),
        "stage": stage,
        "event": event,
    }
    if isinstance(details, Mapping):
        row["details"] = dict(details)
    events.append(row)
    return events


def _normalise_prompt_lookup_candidates(namespace: str | None) -> list[str]:
    cleaned = _normalise_text(namespace)
    if not cleaned:
        return []

    candidates: list[str] = []
    if cleaned.startswith("#V#") and "@" in cleaned:
        user_part = cleaned.split("@", 1)[0].strip()
        if user_part:
            candidates.append(user_part)
    elif "/" in cleaned:
        user_part = cleaned.split("/", 1)[0].strip()
        if user_part:
            if user_part.startswith("#V#"):
                candidates.append(user_part)
            else:
                candidates.append(f"#V#{user_part}")

    candidates.append(cleaned)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _normalise_repository_from_text(value: Any) -> tuple[str | None, str | None, str | None]:
    text = _normalise_text(value)
    if not text:
        return None, None, None
    cleaned = text.strip().strip("/")
    if "/" not in cleaned:
        return None, None, None
    owner, repo = cleaned.split("/", 1)
    owner_clean = _normalise_text(owner)
    repo_clean = _normalise_text(repo)
    if not owner_clean or not repo_clean:
        return None, None, None
    return owner_clean, repo_clean, f"{owner_clean}/{repo_clean}"


def _resolve_github_repository(
    maintenance_context: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
    for owner_key, repo_key in (
        ("github_owner", "github_repo"),
        ("owner", "repo"),
    ):
        owner = _normalise_text(maintenance_context.get(owner_key))
        repo = _normalise_text(maintenance_context.get(repo_key))
        if owner and repo:
            return owner, repo, f"{owner}/{repo}"

    for key in ("github_repository", "repository"):
        owner, repo, repository = _normalise_repository_from_text(maintenance_context.get(key))
        if repository:
            return owner, repo, repository

    incident_text = _incident_text_from_evidence(evidence)
    if incident_text:
        repo_match = re.search(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b", incident_text)
        if repo_match:
            owner, repo, repository = _normalise_repository_from_text(repo_match.group(1))
            if repository:
                return owner, repo, repository

    try:
        import os

        raw_allow_list = os.getenv("VON_GITHUB_REPO_ALLOW_LIST") or _DEFAULT_GITHUB_REPOSITORY
        first_repo = _normalise_text(raw_allow_list.split(",")[0] if raw_allow_list else None)
        owner, repo, repository = _normalise_repository_from_text(first_repo)
        if repository:
            return owner, repo, repository
    except Exception:
        pass

    owner, repo, repository = _normalise_repository_from_text(_DEFAULT_GITHUB_REPOSITORY)
    return owner, repo, repository


def _extract_candidate_source_paths(*texts: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _SOURCE_PATH_PATTERN.finditer(str(text or "")):
            candidate = _normalise_text(match.group(1))
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)
            if len(paths) >= _MAX_GITHUB_EVIDENCE_PATHS:
                return paths
    return paths


def _summarise_github_file_content(payload: Mapping[str, Any]) -> dict[str, Any]:
    text_preview: str | None = None
    if isinstance(payload.get("content"), str):
        text_preview = payload.get("content")
    elif isinstance(payload.get("text"), str):
        text_preview = payload.get("text")
    elif isinstance(payload.get("result"), Mapping):
        result = payload.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("content"), str):
            text_preview = result.get("content")

    preview = ""
    if isinstance(text_preview, str):
        preview = text_preview[:_MAX_GITHUB_EVIDENCE_PREVIEW_CHARS]

    return {
        "path": payload.get("path"),
        "sha": payload.get("sha"),
        "size": payload.get("size"),
        "preview": preview,
    }


def _collect_github_evidence(
    maintenance_context: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    owner, repo, repository = _resolve_github_repository(maintenance_context, evidence)
    payload: dict[str, Any] = {
        "repository": repository,
        "owner": owner,
        "repo": repo,
        "auth": _invoke_mcp_tool("github_get_auth_config", {}),
        "latest_commits": {},
        "open_pull_requests": {},
        "file_evidence": [],
        "errors": [],
    }

    if not owner or not repo:
        payload["errors"].append("repository_resolution_failed")
        payload["success"] = False
        return payload

    commits = _invoke_mcp_tool(
        "github_list_commits",
        {"owner": owner, "repo": repo, "perPage": 5},
    )
    payload["latest_commits"] = commits
    if not bool(commits.get("success")):
        payload["errors"].append("github_list_commits_failed")

    pull_requests = _invoke_mcp_tool(
        "github_list_pull_requests",
        {
            "owner": owner,
            "repo": repo,
            "state": "open",
            "sort": "updated",
            "direction": "desc",
            "perPage": 5,
        },
    )
    payload["open_pull_requests"] = pull_requests
    if not bool(pull_requests.get("success")):
        payload["errors"].append("github_list_pull_requests_failed")

    incident_text = _incident_text_from_evidence(evidence)
    prompt_preview = _normalise_text(
        _coerce_mapping(evidence.get("turn_execution")).get("prompt_preview")
    ) or ""
    paths = _extract_candidate_source_paths(incident_text, prompt_preview)
    for source_path in paths:
        file_result = _invoke_mcp_tool(
            "github_get_file_contents",
            {"owner": owner, "repo": repo, "path": source_path},
        )
        summary = {"path": source_path, "success": bool(file_result.get("success"))}
        if bool(file_result.get("success")):
            summary.update(_summarise_github_file_content(file_result))
        else:
            summary["error"] = file_result.get("error")
            summary["error_code"] = file_result.get("error_code")
            payload["errors"].append(f"github_get_file_contents_failed:{source_path}")
        payload["file_evidence"].append(summary)

    auth_payload = _coerce_mapping(payload.get("auth"))
    auth_success = bool(auth_payload.get("success"))
    tools_available = bool(auth_payload.get("proxy_tools_available", False))
    payload["success"] = auth_success and tools_available and not payload["errors"]
    if not auth_success:
        payload["errors"].append("github_auth_probe_failed")
    elif not tools_available:
        payload["errors"].append("github_tools_unavailable")
    return payload


def _diagnosis_cause_ids(diagnosis: Mapping[str, Any]) -> list[str]:
    cause_ids: list[str] = []
    for row in _coerce_mapping_list(diagnosis.get("root_causes"), max_items=50):
        cause_id = _normalise_text(row.get("cause_id"))
        if cause_id and cause_id not in cause_ids:
            cause_ids.append(cause_id)
    return cause_ids


def _build_self_diagnosis_fingerprint(
    *,
    maintenance_context: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
    github_evidence: Mapping[str, Any],
) -> str:
    fingerprint_parts = [
        _normalise_text(maintenance_context.get("selected_workflow_id")) or "",
        _normalise_text(maintenance_context.get("request_id")) or "",
        _normalise_text(github_evidence.get("repository")) or "",
        ",".join(sorted(_diagnosis_cause_ids(diagnosis))),
    ]
    canonical = "|".join(fingerprint_parts).lower()
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _extract_jira_issue_key(payload: Mapping[str, Any]) -> str | None:
    for key in ("issue_key", "key"):
        value = _normalise_text(payload.get(key))
        if value:
            return value
    result = payload.get("result")
    if isinstance(result, Mapping):
        for key in ("issue_key", "key"):
            value = _normalise_text(result.get(key))
            if value:
                return value
    return None


def _extract_jira_search_issue_keys(payload: Mapping[str, Any]) -> list[str]:
    issues = payload.get("issues")
    if not isinstance(issues, list):
        result = payload.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("issues"), list):
            issues = result.get("issues")
    if not isinstance(issues, list):
        return []
    keys: list[str] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        key = _normalise_text(issue.get("key"))
        if key and key not in keys:
            keys.append(key)
    return keys


def _extract_jira_account_id(payload: Mapping[str, Any]) -> str | None:
    account_id = _normalise_text(payload.get("accountId"))
    if account_id:
        return account_id
    user = payload.get("user")
    if isinstance(user, Mapping):
        return _normalise_text(user.get("accountId"))
    result = payload.get("result")
    if isinstance(result, Mapping):
        account_id = _normalise_text(result.get("accountId"))
        if account_id:
            return account_id
        user = result.get("user")
        if isinstance(user, Mapping):
            return _normalise_text(user.get("accountId"))
    return None


def _build_jira_remediation_summary(
    diagnosis: Mapping[str, Any],
    maintenance_context: Mapping[str, Any],
) -> str:
    cause_ids = _diagnosis_cause_ids(diagnosis)
    cause_fragment = ", ".join(cause_ids[:2]) if cause_ids else "unknown root cause"
    workflow_id = _normalise_text(maintenance_context.get("selected_workflow_id")) or "workflow"
    return f"Workflow self-diagnosis remediation: {cause_fragment} ({workflow_id})"


def _build_jira_remediation_description(
    *,
    maintenance_context: Mapping[str, Any],
    evidence: Mapping[str, Any],
    diagnosis: Mapping[str, Any],
    github_evidence: Mapping[str, Any],
    fingerprint: str,
) -> str:
    incident_text = _incident_text_from_evidence(evidence) or "No incident text provided."
    cause_ids = _diagnosis_cause_ids(diagnosis)
    github_errors: list[str] = []
    errors = github_evidence.get("errors")
    if isinstance(errors, list):
        github_errors = [str(item) for item in errors if isinstance(item, str)]
    lines = [
        "Generated by Codex on behalf of Michael Witbrock.",
        "",
        f"Self-diagnosis fingerprint: {fingerprint}",
        f"Request ID: {_normalise_text(maintenance_context.get('request_id')) or 'unknown'}",
        f"Selected workflow: {_normalise_text(maintenance_context.get('selected_workflow_id')) or 'unknown'}",
        f"Repository (read-only evidence): {_normalise_text(github_evidence.get('repository')) or 'unknown'}",
        "",
        "Incident summary:",
        incident_text[:1200],
        "",
        "Root-cause IDs:",
        ", ".join(cause_ids) if cause_ids else "none",
        "",
        "GitHub evidence status:",
        f"success={bool(github_evidence.get('success'))}; errors={', '.join(github_errors) if github_errors else 'none'}",
        "",
        "Acceptance checks:",
        "1. Reproduce and verify the diagnosis signal.",
        "2. Implement a bounded fix through workflow/tool pathways.",
        "3. Add or update regression tests for the observed failure mode.",
    ]
    return "\n".join(lines)


def _execute_jira_self_diagnosis_upsert(payload: Mapping[str, Any]) -> dict[str, Any]:
    project_key = _normalise_text(payload.get("project_key")) or _DEFAULT_JIRA_PROJECT_KEY
    issue_type = _normalise_text(payload.get("issue_type")) or _DEFAULT_JIRA_ISSUE_TYPE
    summary = _normalise_text(payload.get("summary"))
    description = _normalise_text(payload.get("description"))
    fingerprint = _normalise_text(payload.get("fingerprint"))
    request_id = _normalise_text(payload.get("request_id"))

    if not summary or not description or not fingerprint:
        return {
            "success": False,
            "error": "jira_remediation_payload_invalid",
            "error_code": "jira_remediation_payload_invalid",
        }

    comment = (
        "Generated by Codex on behalf of Michael Witbrock.\n\n"
        "Self-diagnosis rerun found matching fingerprint "
        f"`{fingerprint}`.\n\n"
        f"Latest incident summary:\n{description[:1200]}"
    )
    labels = payload.get("labels")
    final_labels = (
        [str(item) for item in labels if isinstance(item, str) and item.strip()]
        if isinstance(labels, list)
        else list(_SELF_DIAGNOSIS_LABELS)
    )
    return upsert_deduplicated_jira_issue(
        project_key=project_key,
        issue_type=issue_type,
        summary=summary,
        description=description,
        fingerprint=fingerprint,
        search_label=_SELF_DIAGNOSIS_LABELS[0],
        labels=final_labels,
        request_id=request_id or f"diag-{fingerprint}",
        existing_comment=comment,
    )


def _get_gateway():
    global _gateway_singleton
    if _gateway_singleton is not None:
        return _gateway_singleton
    with _gateway_lock:
        if _gateway_singleton is not None:
            return _gateway_singleton
        from ...integrations.internal_mcp.catalogue import build_default_catalogue
        from ...integrations.internal_mcp.gateway import InternalMCPGateway
        from ...integrations.internal_mcp.transport import InternalMCPTransport

        _gateway_singleton = InternalMCPGateway(
            catalogue=build_default_catalogue(),
            transport=InternalMCPTransport(),
            enabled=True,
        )
        return _gateway_singleton


def _available_tool_names() -> list[str]:
    try:
        from ...integrations.internal_mcp.catalogue import build_default_catalogue

        names = build_default_catalogue().list_methods()
        if not isinstance(names, Sequence):
            return []
        return sorted(
            {
                str(name).strip()
                for name in names
                if isinstance(name, str) and str(name).strip()
            }
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[workflow_maintenance] tool list lookup failed: %s", exc)
        return []


def _invoke_mcp_tool(tool_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        gateway = _get_gateway()
        result = gateway.invoke(tool_name, dict(payload))
        response_payload = result.payload if isinstance(result.payload, Mapping) else {}
        if isinstance(response_payload, Mapping):
            return dict(response_payload)
        return {"success": True, "result": result.payload}
    except Exception as exc:
        return {
            "success": False,
            "error_code": "mcp_invoke_failed",
            "error": f"{tool_name}:{exc}",
            "tool": tool_name,
        }


def _tool_family(tool_name: str | None) -> str:
    tool = str(tool_name or "").strip().lower()
    if not tool:
        return "unknown"
    if tool.startswith("task_") or tool.endswith("_task"):
        return "task"
    if tool.startswith("jira_"):
        return "jira"
    if tool.startswith("workflow_") or tool.startswith("turn_execution_"):
        return "workflow"
    if tool.startswith("search_") or tool.startswith("extract_") or tool == "qna_search":
        return "search"
    if tool.startswith("gmail_"):
        return "gmail"
    if tool.startswith("rag_"):
        return "rag"
    if tool.startswith("github_"):
        return "github"
    if tool in {
        "search_concepts",
        "fetch_concept",
        "add_relationship",
        "remove_relationship",
        "upsert_text_relation",
        "upsert_singleton_text_relation",
    }:
        return "vontology"
    return "unknown"


def _tool_domain_description(tool_name: str | None) -> str:
    family = _tool_family(tool_name)
    if family == "task":
        return "task-management operations"
    if family == "jira":
        return "Jira issue operations"
    if family == "workflow":
        return "workflow-orchestration operations"
    if family == "search":
        return "search/retrieval operations"
    if family == "vontology":
        return "Vontology concept/relationship operations"
    if family == "github":
        return "GitHub repository operations"
    if family == "gmail":
        return "Gmail mailbox operations"
    if family == "rag":
        return "RAG indexing/retrieval operations"
    return "the matching operation domain"


def _canonicalise_tool_name(tool_name: str, known_tools: set[str]) -> tuple[str, str]:
    cleaned = str(tool_name or "").strip()
    if not cleaned:
        return "", "empty"
    if cleaned in known_tools:
        return cleaned, "exact"

    hinted = _TOOL_ALIAS_HINTS.get(cleaned)
    if isinstance(hinted, str) and hinted in known_tools:
        return hinted, "alias_map"

    parts = cleaned.split("_")
    if len(parts) == 2:
        swapped = f"{parts[1]}_{parts[0]}"
        if swapped in known_tools:
            return swapped, "token_swap"

    return cleaned, "unknown"


def _extract_prompt_directives(
    *,
    prompt_concept_id: str | None,
    content: str,
    known_tools: set[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        match = _TOOL_NAME_PATTERN.search(line)
        if not match:
            continue
        raw_tool_name = str(match.group(1) or "").strip()
        if not raw_tool_name:
            continue
        canonical_tool_name, canonical_source = _canonicalise_tool_name(
            raw_tool_name, known_tools
        )
        is_absolute = any(term in lower for term in _ABSOLUTE_TERMS)
        scope_overreach = any(term in lower for term in _OVERREACH_TERMS)
        findings.append(
            {
                "prompt_concept_id": prompt_concept_id,
                "line_excerpt": line[:260],
                "tool_name": raw_tool_name,
                "canonical_tool_name": canonical_tool_name,
                "canonical_source": canonical_source,
                "tool_known": canonical_source != "unknown",
                "is_alias": canonical_tool_name != raw_tool_name,
                "is_absolute": is_absolute,
                "scope_overreach": scope_overreach,
                "tool_family": _tool_family(canonical_tool_name or raw_tool_name),
            }
        )
    return findings


def _build_prompt_guardrail_block(findings: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        f"{_GUARDRAIL_MARKER} ({_utc_now_iso()})",
        "- Never apply single-tool directives blindly.",
        "- Before selecting a tool, verify intent and expected effects for this turn.",
        "- If intent is ambiguous or cross-domain, inspect workflow/tool context first.",
    ]
    seen_pairs: set[tuple[str, str]] = set()
    for finding in findings:
        raw_tool = _normalise_text(finding.get("tool_name"))
        canonical_tool = _normalise_text(finding.get("canonical_tool_name")) or raw_tool
        if not raw_tool or not canonical_tool:
            continue
        pair = (raw_tool, canonical_tool)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        domain = _tool_domain_description(canonical_tool)
        if raw_tool != canonical_tool:
            lines.append(
                f"- Canonicalise tool naming: use `{canonical_tool}` (not `{raw_tool}`)."
            )
        lines.append(
            f"- Use `{canonical_tool}` only when the user request is explicitly about {domain}."
        )
    lines.append(
        "- If evidence indicates prompt/workflow conflation, run maintenance introspection before responding."
    )
    return "\n".join(lines)


def _patch_prompt_content(
    *,
    original_content: str,
    findings: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    text = str(original_content or "")
    if not text.strip():
        return text, {"changed": False, "reason": "empty_content"}
    if _GUARDRAIL_MARKER in text:
        return text, {"changed": False, "reason": "guardrail_already_present"}

    patched = text
    replacements: list[dict[str, str]] = []
    for finding in findings:
        raw_tool = _normalise_text(finding.get("tool_name"))
        canonical_tool = _normalise_text(finding.get("canonical_tool_name"))
        if not raw_tool or not canonical_tool or raw_tool == canonical_tool:
            continue
        pattern = re.compile(rf"`?{re.escape(raw_tool)}`?", flags=re.IGNORECASE)
        patched_new = pattern.sub(f"`{canonical_tool}`", patched)
        if patched_new != patched:
            replacements.append({"from": raw_tool, "to": canonical_tool})
            patched = patched_new

    block = _build_prompt_guardrail_block(findings)
    patched = patched.rstrip() + "\n\n" + block.strip() + "\n"
    return patched, {"changed": True, "replacements": replacements}


def _incident_text_from_evidence(evidence: Mapping[str, Any]) -> str:
    explicit_incident = _normalise_text(evidence.get("incident_text"))
    if explicit_incident:
        return explicit_incident
    turn_execution = _coerce_mapping(evidence.get("turn_execution"))
    prompt_preview = _normalise_text(turn_execution.get("prompt_preview"))
    if prompt_preview:
        return prompt_preview
    return ""


def _handle_assess_context(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    namespace = (
        _normalise_text(context.get("namespace"))
        or _normalise_text(getattr(request.environment, "user_namespace", None))
    )
    if not namespace:
        return WorkflowActionResult(
            status="failed",
            error="namespace_required: workflow maintenance needs namespace context",
        )

    request_id = _normalise_text(context.get("request_id")) or _normalise_text(
        context.get("session_id")
    )
    max_prompt_chars = _coerce_int(
        context.get("max_prompt_chars"),
        default=_DEFAULT_MAX_PROMPT_CHARS,
        minimum=500,
        maximum=50_000,
    )
    include_prompt_content = _coerce_bool(
        context.get("include_prompt_content"),
        default=True,
    )
    prompt_lookup_candidates = _normalise_prompt_lookup_candidates(namespace)

    workflow_definitions = _invoke_mcp_tool(
        "workflow_list_definitions",
        {
            "limit": _coerce_int(
                context.get("workflow_limit"),
                default=200,
                minimum=10,
                maximum=500,
            )
        },
    )

    prompt_context: dict[str, Any] = {}
    prompt_lookup_namespace: str | None = None
    for candidate in prompt_lookup_candidates:
        probe = _invoke_mcp_tool(
            "chat_get_prompt_context",
            {
                "namespace": candidate,
                "include_content": include_prompt_content,
                "max_chars": max_prompt_chars,
            },
        )
        if bool(probe.get("success")):
            prompt_context = probe
            prompt_lookup_namespace = candidate
            break
        if not prompt_context:
            prompt_context = probe

    turn_execution: dict[str, Any] = {}
    if request_id:
        turn_execution = _invoke_mcp_tool(
            "turn_execution_get",
            {
                "namespace": namespace,
                "request_id": request_id,
            },
        )

    selected_workflow_id = _normalise_text(context.get("selected_workflow_id"))
    if not selected_workflow_id:
        selected_workflow_id = _normalise_text(turn_execution.get("selected_workflow_id"))

    workflow_concept: dict[str, Any] = {}
    if selected_workflow_id and selected_workflow_id.startswith("#V#"):
        workflow_concept = _invoke_mcp_tool(
            "fetch_concept",
            {
                "concept_id": selected_workflow_id,
                "include_text_relations_arg1": True,
                "include_relations_arg1": True,
                "include_relations_any_arg": True,
                "limit": 200,
            },
        )

    maintenance_context = {
        "namespace": namespace,
        "request_id": request_id,
        "prompt_lookup_namespace": prompt_lookup_namespace,
        "selected_workflow_id": selected_workflow_id,
        "apply_repairs": _coerce_bool(context.get("apply_repairs"), default=True),
        "dry_run": _coerce_bool(context.get("dry_run"), default=False),
        "max_operations": _coerce_int(
            context.get("max_operations"),
            default=_DEFAULT_OPERATION_CAP,
            minimum=1,
            maximum=50,
        ),
        "emit_jira_remediation": _coerce_bool(
            context.get("emit_jira_remediation"),
            default=True,
        ),
        "jira_project_key": _normalise_text(context.get("jira_project_key"))
        or _DEFAULT_JIRA_PROJECT_KEY,
        "jira_issue_type": _normalise_text(context.get("jira_issue_type"))
        or _DEFAULT_JIRA_ISSUE_TYPE,
        "github_owner": _normalise_text(context.get("github_owner")),
        "github_repo": _normalise_text(context.get("github_repo")),
        "github_repository": _normalise_text(context.get("github_repository"))
        or _normalise_text(context.get("repository")),
    }
    known_tools = _available_tool_names()
    github_evidence = _collect_github_evidence(
        maintenance_context,
        {
            "incident_text": _normalise_text(context.get("incident_text")),
            "turn_execution": turn_execution,
        },
    )
    trace = _append_trace_event(
        context,
        stage="assess",
        event="evidence_collected",
        details={
            "namespace": namespace,
            "request_id": request_id,
            "prompt_lookup_namespace": prompt_lookup_namespace,
            "selected_workflow_id": selected_workflow_id,
            "known_tool_count": len(known_tools),
            "workflow_definition_count": int(workflow_definitions.get("count") or 0),
            "github_repository": github_evidence.get("repository"),
            "github_evidence_success": bool(github_evidence.get("success")),
            "github_error_count": len(github_evidence.get("errors", []))
            if isinstance(github_evidence.get("errors"), list)
            else 0,
        },
    )
    maintenance_evidence = {
        "incident_text": _normalise_text(context.get("incident_text")),
        "workflow_definitions": workflow_definitions,
        "prompt_context": prompt_context,
        "turn_execution": turn_execution,
        "workflow_concept": workflow_concept,
        "known_tool_names": known_tools,
        "github_evidence": github_evidence,
    }

    return WorkflowActionResult(
        outputs={
            "maintenance_context": maintenance_context,
            "maintenance_evidence": maintenance_evidence,
            "maintenance_trace": trace,
        }
    )


def _handle_diagnose_conflation(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    maintenance_context = _coerce_mapping(context.get("maintenance_context"))
    evidence = _coerce_mapping(context.get("maintenance_evidence"))
    prompt_context = _coerce_mapping(evidence.get("prompt_context"))
    known_tools = {
        str(item).strip()
        for item in evidence.get("known_tool_names", [])
        if isinstance(item, str) and item.strip()
    }

    prompt_concepts = _coerce_mapping_list(prompt_context.get("behaviour_prompt_concepts"))
    directives: list[dict[str, Any]] = []
    for concept in prompt_concepts:
        concept_id = _normalise_text(concept.get("concept_id"))
        content = _normalise_text(concept.get("content"))
        if not content:
            continue
        directives.extend(
            _extract_prompt_directives(
                prompt_concept_id=concept_id,
                content=content,
                known_tools=known_tools,
            )
        )

    root_causes: list[dict[str, Any]] = []
    seen_causes: set[str] = set()

    def _append_cause(
        cause_id: str,
        *,
        summary: str,
        scope: str,
        evidence_items: Sequence[str],
        severity: str = "medium",
    ) -> None:
        if cause_id in seen_causes:
            return
        seen_causes.add(cause_id)
        root_causes.append(
            {
                "cause_id": cause_id,
                "scope": scope,
                "summary": summary,
                "severity": severity,
                "evidence": [item for item in evidence_items if isinstance(item, str) and item],
            }
        )

    absolute_directives = [row for row in directives if bool(row.get("is_absolute"))]
    overreaching_directives = [row for row in directives if bool(row.get("scope_overreach"))]
    alias_directives = [row for row in directives if bool(row.get("is_alias"))]
    unknown_tool_directives = [row for row in directives if not bool(row.get("tool_known"))]

    if absolute_directives:
        _append_cause(
            "prompt_absolute_tool_directive",
            summary="Prompt contains absolute tool-use directives that can override intent grounding.",
            scope="prompt",
            evidence_items=[str(row.get("line_excerpt") or "") for row in absolute_directives[:3]],
            severity="high",
        )

    if overreaching_directives:
        _append_cause(
            "prompt_scope_overreach",
            summary="Prompt directive scope appears broader than a single operation domain.",
            scope="prompt",
            evidence_items=[str(row.get("line_excerpt") or "") for row in overreaching_directives[:3]],
            severity="high",
        )

    if alias_directives:
        _append_cause(
            "prompt_tool_alias_drift",
            summary="Prompt references tool aliases instead of canonical tool names.",
            scope="prompt",
            evidence_items=[
                (
                    f"{row.get('tool_name')} -> {row.get('canonical_tool_name')}"
                )
                for row in alias_directives[:5]
            ],
        )

    if unknown_tool_directives:
        _append_cause(
            "prompt_unknown_tool_reference",
            summary="Prompt references tools that are not present in the active tool catalogue.",
            scope="prompt",
            evidence_items=[str(row.get("tool_name") or "") for row in unknown_tool_directives[:5]],
        )

    incident_text = _incident_text_from_evidence(evidence).lower()
    if incident_text:
        if any(term in incident_text for term in _ONTOLOGY_HINT_TERMS):
            task_absolute = [
                row
                for row in absolute_directives
                if str(row.get("tool_family") or "") == "task"
            ]
            if task_absolute:
                _append_cause(
                    "cross_domain_conflation_risk",
                    summary=(
                        "Task-tool directives are active while incident signals ontology/concept intent."
                    ),
                    scope="prompt_workflow",
                    evidence_items=[str(row.get("line_excerpt") or "") for row in task_absolute[:3]],
                    severity="high",
                )

    turn_execution = _coerce_mapping(evidence.get("turn_execution"))
    decision = _normalise_text(turn_execution.get("decision")) or ""
    safe_to_claim = bool(turn_execution.get("safe_to_claim_completion", True))
    requires_follow_up = bool(turn_execution.get("requires_follow_up", False))
    critic_payload = _coerce_mapping(turn_execution.get("critic"))
    critic_summary = _coerce_mapping(critic_payload.get("summary"))
    not_verified_count = int(critic_summary.get("not_verified_count") or 0)
    if decision == "completed" and safe_to_claim and not_verified_count > 0:
        _append_cause(
            "completion_gate_false_positive",
            summary="Completion gate marked the turn completed while postconditions remained unverified.",
            scope="workflow",
            evidence_items=[f"not_verified_count={not_verified_count}"],
            severity="high",
        )
    if requires_follow_up and decision:
        _append_cause(
            "completion_gate_follow_up_required",
            summary="Completion gate required follow-up, signalling unresolved effects.",
            scope="workflow",
            evidence_items=[
                f"decision={decision}",
                str(turn_execution.get("decision_reason") or ""),
            ],
        )

    github_evidence = _coerce_mapping(evidence.get("github_evidence"))
    if github_evidence:
        github_success = bool(github_evidence.get("success"))
        if not github_success:
            github_errors = github_evidence.get("errors")
            error_summary = (
                ", ".join([str(item) for item in github_errors if isinstance(item, str)])
                if isinstance(github_errors, list)
                else "unknown"
            )
            _append_cause(
                "github_read_evidence_unavailable",
                summary="Read-only GitHub evidence collection is unavailable; fail closed for repository-grounded diagnosis.",
                scope="github",
                evidence_items=[error_summary],
                severity="high",
            )

    conflation_detected = len(root_causes) > 0
    confidence = "low"
    if len(root_causes) >= 3:
        confidence = "high"
    elif len(root_causes) >= 1:
        confidence = "medium"

    implicated_prompt_ids = sorted(
        {
            str(row.get("prompt_concept_id"))
            for row in directives
            if row.get("prompt_concept_id")
            and (row.get("is_absolute") or row.get("scope_overreach") or row.get("is_alias"))
        }
    )
    diagnosis = {
        "conflation_detected": conflation_detected,
        "confidence": confidence,
        "root_causes": root_causes,
        "directive_findings": directives,
        "implicated_prompt_concept_ids": implicated_prompt_ids,
        "selected_workflow_id": maintenance_context.get("selected_workflow_id"),
        "github_repository": github_evidence.get("repository"),
        "github_evidence_success": bool(github_evidence.get("success")),
    }

    trace = _append_trace_event(
        context,
        stage="diagnose",
        event="diagnosis_ready",
        details={
            "conflation_detected": conflation_detected,
            "root_cause_count": len(root_causes),
            "confidence": confidence,
            "directive_count": len(directives),
        },
    )
    return WorkflowActionResult(
        outputs={
            "maintenance_diagnosis": diagnosis,
            "maintenance_trace": trace,
        }
    )


def _handle_plan_repairs(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    maintenance_context = _coerce_mapping(context.get("maintenance_context"))
    evidence = _coerce_mapping(context.get("maintenance_evidence"))
    diagnosis = _coerce_mapping(context.get("maintenance_diagnosis"))
    github_evidence = _coerce_mapping(evidence.get("github_evidence"))
    prompt_context = _coerce_mapping(evidence.get("prompt_context"))
    prompt_concepts = _coerce_mapping_list(prompt_context.get("behaviour_prompt_concepts"))
    directive_findings = _coerce_mapping_list(diagnosis.get("directive_findings"))
    implicated_prompt_ids = {
        str(item)
        for item in diagnosis.get("implicated_prompt_concept_ids", [])
        if isinstance(item, str) and item.strip()
    }

    operations: list[dict[str, Any]] = []
    for concept in prompt_concepts:
        concept_id = _normalise_text(concept.get("concept_id"))
        content = _normalise_text(concept.get("content"))
        if not concept_id or not content:
            continue
        if implicated_prompt_ids and concept_id not in implicated_prompt_ids:
            continue
        concept_findings = [
            row
            for row in directive_findings
            if _normalise_text(row.get("prompt_concept_id")) == concept_id
        ]
        if not concept_findings:
            continue
        patched_text, patch_meta = _patch_prompt_content(
            original_content=content,
            findings=concept_findings,
        )
        if not bool(patch_meta.get("changed")):
            continue
        operations.append(
            {
                "operation_id": f"prompt_patch:{concept_id}",
                "risk": "low",
                "tool_name": "upsert_singleton_text_relation",
                "reason": "Patch over-broad/alias tool directives with guardrails.",
                "payload": {
                    "concept_id": concept_id,
                    "predicate": "hasContent",
                    "language": _DEFAULT_LANGUAGE,
                    "text": patched_text,
                    "namespace": maintenance_context.get("namespace"),
                    "garbage_collect": True,
                    "provenance": {
                        "source": "workflow_introspection_maintenance_workflow",
                        "request_id": maintenance_context.get("request_id"),
                        "repair_type": "prompt_guardrail_patch",
                    },
                },
            }
        )

    selected_workflow_id = _normalise_text(maintenance_context.get("selected_workflow_id"))
    if selected_workflow_id and selected_workflow_id.startswith("#V#"):
        note_text = (
            f"{_GUARDRAIL_MARKER}: workflow maintenance record\n"
            f"- workflow_id: {selected_workflow_id}\n"
            f"- request_id: {maintenance_context.get('request_id')}\n"
            f"- generated_at_utc: {_utc_now_iso()}\n"
            f"- root_causes: {', '.join([str(item.get('cause_id') or '') for item in diagnosis.get('root_causes', []) if isinstance(item, Mapping)])}"
        )
        operations.append(
            {
                "operation_id": f"workflow_note:{selected_workflow_id}",
                "risk": "low",
                "tool_name": "upsert_text_relation",
                "reason": "Attach a workflow-level maintenance note for traceability.",
                "payload": {
                    "concept_id": selected_workflow_id,
                    "predicate": "hasNote",
                    "language": _DEFAULT_LANGUAGE,
                    "text": note_text,
                    "namespace": maintenance_context.get("namespace"),
                    "provenance": {
                        "source": "workflow_introspection_maintenance_workflow",
                        "request_id": maintenance_context.get("request_id"),
                        "repair_type": "workflow_maintenance_note",
                    },
                },
            }
        )

    jira_remediation_skipped_reason: str | None = None
    if _coerce_bool(maintenance_context.get("emit_jira_remediation"), default=True) and bool(
        diagnosis.get("conflation_detected")
    ):
        if not bool(github_evidence.get("success")):
            jira_remediation_skipped_reason = "github_evidence_unavailable"
        else:
            fingerprint = _build_self_diagnosis_fingerprint(
                maintenance_context=maintenance_context,
                diagnosis=diagnosis,
                github_evidence=github_evidence,
            )
            summary = _build_jira_remediation_summary(diagnosis, maintenance_context)
            description = _build_jira_remediation_description(
                maintenance_context=maintenance_context,
                evidence=evidence,
                diagnosis=diagnosis,
                github_evidence=github_evidence,
                fingerprint=fingerprint,
            )
            operations.append(
                {
                    "operation_id": (
                        f"jira_remediation:{maintenance_context.get('request_id') or fingerprint}"
                    ),
                    "risk": "medium",
                    "tool_name": _SELF_DIAGNOSIS_TOOL_NAME,
                    "reason": (
                        "Create or update a deduplicated Jira remediation issue from workflow self-diagnosis evidence."
                    ),
                    "payload": {
                        "project_key": _normalise_text(
                            maintenance_context.get("jira_project_key")
                        )
                        or _DEFAULT_JIRA_PROJECT_KEY,
                        "issue_type": _normalise_text(
                            maintenance_context.get("jira_issue_type")
                        )
                        or _DEFAULT_JIRA_ISSUE_TYPE,
                        "summary": summary,
                        "description": description,
                        "fingerprint": fingerprint,
                        "labels": list(_SELF_DIAGNOSIS_LABELS),
                        "request_id": maintenance_context.get("request_id"),
                    },
                }
            )

    apply_requested = bool(maintenance_context.get("apply_repairs", True))
    max_operations = _coerce_int(
        maintenance_context.get("max_operations"),
        default=_DEFAULT_OPERATION_CAP,
        minimum=1,
        maximum=50,
    )
    operations = operations[:max_operations]

    repair_plan = {
        "conflation_detected": bool(diagnosis.get("conflation_detected")),
        "apply_requested": apply_requested,
        "dry_run": bool(maintenance_context.get("dry_run", False)),
        "operation_count": len(operations),
        "operations": operations,
        "jira_remediation_skipped_reason": jira_remediation_skipped_reason,
    }

    trace = _append_trace_event(
        context,
        stage="plan",
        event="repair_plan_ready",
        details={
            "apply_requested": apply_requested,
            "operation_count": len(operations),
            "dry_run": bool(maintenance_context.get("dry_run", False)),
        },
    )
    return WorkflowActionResult(
        outputs={
            "maintenance_repair_plan": repair_plan,
            "maintenance_trace": trace,
        }
    )


def _handle_apply_repairs(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    repair_plan = _coerce_mapping(context.get("maintenance_repair_plan"))
    apply_requested = bool(repair_plan.get("apply_requested", False))
    dry_run = bool(repair_plan.get("dry_run", False))
    operations = _coerce_mapping_list(repair_plan.get("operations"), max_items=100)

    report: dict[str, Any] = {
        "applied": False,
        "dry_run": dry_run,
        "requested": apply_requested,
        "operation_count": len(operations),
        "results": [],
    }
    if not apply_requested or not operations:
        report["reason"] = "apply_not_requested_or_no_operations"
        return WorkflowActionResult(outputs={"maintenance_apply_report": report})

    if dry_run:
        report["applied"] = False
        report["reason"] = "dry_run_enabled"
        report["results"] = [
            {
                "operation_id": row.get("operation_id"),
                "tool_name": row.get("tool_name"),
                "status": "planned",
            }
            for row in operations
        ]
        return WorkflowActionResult(outputs={"maintenance_apply_report": report})

    success_count = 0
    for operation in operations:
        tool_name = _normalise_text(operation.get("tool_name"))
        payload = _coerce_mapping(operation.get("payload"))
        if not tool_name:
            report["results"].append(
                {
                    "operation_id": operation.get("operation_id"),
                    "status": "failed",
                    "error": "missing_tool_name",
                }
            )
            continue
        if tool_name == _SELF_DIAGNOSIS_TOOL_NAME:
            result = _execute_jira_self_diagnosis_upsert(payload)
        else:
            result = _invoke_mcp_tool(tool_name, payload)
        ok = bool(result.get("success", False))
        if ok:
            success_count += 1
        report["results"].append(
            {
                "operation_id": operation.get("operation_id"),
                "tool_name": tool_name,
                "status": "success" if ok else "failed",
                "error": result.get("error"),
                "error_code": result.get("error_code"),
                "mode": result.get("mode"),
                "issue_key": result.get("issue_key"),
                "fingerprint": result.get("fingerprint"),
            }
        )

    report["applied"] = success_count > 0
    report["successful_operations"] = success_count
    report["failed_operations"] = len(operations) - success_count
    return WorkflowActionResult(outputs={"maintenance_apply_report": report})


def _handle_verify_repairs(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    maintenance_context = _coerce_mapping(context.get("maintenance_context"))
    apply_report = _coerce_mapping(context.get("maintenance_apply_report"))
    repair_plan = _coerce_mapping(context.get("maintenance_repair_plan"))

    operations = _coerce_mapping_list(repair_plan.get("operations"), max_items=100)
    expected_prompt_ids = [
        _normalise_text(_coerce_mapping(row.get("payload")).get("concept_id"))
        for row in operations
        if _normalise_text(row.get("tool_name")) == "upsert_singleton_text_relation"
    ]
    expected_prompt_ids = [item for item in expected_prompt_ids if item]
    verification: dict[str, Any] = {
        "expected_prompt_updates": expected_prompt_ids,
        "verified_prompt_updates": [],
        "expected_remediation_issue_keys": [],
        "verified_remediation_issue_keys": [],
        "verification_errors": [],
        "verified": True,
    }

    if not bool(apply_report.get("applied", False)):
        verification["verified"] = True
        verification["reason"] = "no_mutations_applied"
        return WorkflowActionResult(outputs={"maintenance_verification": verification})

    prompt_lookup_namespace = _normalise_text(maintenance_context.get("prompt_lookup_namespace"))
    if prompt_lookup_namespace:
        prompt_context = _invoke_mcp_tool(
            "chat_get_prompt_context",
            {
                "namespace": prompt_lookup_namespace,
                "include_content": True,
                "max_chars": _DEFAULT_MAX_PROMPT_CHARS,
            },
        )
        behaviour_prompts = _coerce_mapping_list(
            prompt_context.get("behaviour_prompt_concepts")
        )
        for prompt in behaviour_prompts:
            concept_id = _normalise_text(prompt.get("concept_id"))
            content = _normalise_text(prompt.get("content")) or ""
            if concept_id in expected_prompt_ids and _GUARDRAIL_MARKER in content:
                verification["verified_prompt_updates"].append(concept_id)

    remediation_issue_keys: list[str] = []
    for row in _coerce_mapping_list(apply_report.get("results"), max_items=100):
        if _normalise_text(row.get("tool_name")) != _SELF_DIAGNOSIS_TOOL_NAME:
            continue
        if _normalise_text(row.get("status")) != "success":
            continue
        issue_key = _normalise_text(row.get("issue_key"))
        if issue_key and issue_key not in remediation_issue_keys:
            remediation_issue_keys.append(issue_key)
    verification["expected_remediation_issue_keys"] = remediation_issue_keys

    for issue_key in remediation_issue_keys:
        issue_result = _invoke_mcp_tool(
            "jira_get_issue",
            {"issue_key": issue_key, "fields": ["summary", "status", "labels"]},
        )
        if bool(issue_result.get("success")):
            verification["verified_remediation_issue_keys"].append(issue_key)
        else:
            verification["verification_errors"].append(
                f"jira_issue_not_observable:{issue_key}"
            )

    missing = sorted(set(expected_prompt_ids) - set(verification["verified_prompt_updates"]))
    if missing:
        verification["verified"] = False
        verification["verification_errors"].append(
            f"guardrail_marker_missing_for_prompts:{','.join(missing)}"
        )
    missing_remediation_keys = sorted(
        set(remediation_issue_keys) - set(verification["verified_remediation_issue_keys"])
    )
    if missing_remediation_keys:
        verification["verified"] = False
        verification["verification_errors"].append(
            f"remediation_issue_not_verified:{','.join(missing_remediation_keys)}"
        )
    if verification["verification_errors"]:
        return WorkflowActionResult(
            status="failed",
            error="repair_verification_failed",
            outputs={"maintenance_verification": verification},
        )

    return WorkflowActionResult(outputs={"maintenance_verification": verification})


def _handle_finalise(request: WorkflowActionRequest) -> WorkflowActionResult:
    context = request.data
    result = {
        "success": True,
        "workflow_id": WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
        "generated_at_utc": _utc_now_iso(),
        "maintenance_context": _coerce_mapping(context.get("maintenance_context")),
        "diagnosis": _coerce_mapping(context.get("maintenance_diagnosis")),
        "repair_plan": _coerce_mapping(context.get("maintenance_repair_plan")),
        "apply_report": _coerce_mapping(context.get("maintenance_apply_report")),
        "verification": _coerce_mapping(context.get("maintenance_verification")),
        "trace": _coerce_mapping_list(context.get("maintenance_trace"), max_items=500),
    }
    return WorkflowActionResult(outputs={"workflow_maintenance_result": result})


def build_workflow_introspection_maintenance_workflow_test_definition() -> WorkflowDefinition:
    assess = WorkflowStateSpec(
        state_id="assess",
        actions=(
            WorkflowActionInvocation(
                action_id="workflow_introspection.assess_context",
                description="Collect turn/workflow/prompt evidence for maintenance.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                reason="on_failure",
            ),
            WorkflowTransitionSpec(
                to_state="diagnose",
                condition=lambda ctx: bool(ctx.get("maintenance_evidence")),
                reason="evidence_ready",
            ),
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda _ctx: True,
                reason="missing_evidence",
            ),
        ),
    )

    diagnose = WorkflowStateSpec(
        state_id="diagnose",
        actions=(
            WorkflowActionInvocation(
                action_id="workflow_introspection.diagnose_conflation",
                description="Diagnose conflation/root-cause signals.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                reason="on_failure",
            ),
            WorkflowTransitionSpec(
                to_state="plan",
                condition=lambda ctx: bool(ctx.get("maintenance_diagnosis")),
                reason="diagnosis_ready",
            ),
        ),
    )

    plan = WorkflowStateSpec(
        state_id="plan",
        actions=(
            WorkflowActionInvocation(
                action_id="workflow_introspection.plan_repairs",
                description="Plan bounded prompt/workflow repairs.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                reason="on_failure",
            ),
            WorkflowTransitionSpec(
                to_state="apply",
                condition=lambda _ctx: True,
                reason="planned",
            ),
        ),
    )

    apply_state = WorkflowStateSpec(
        state_id="apply",
        actions=(
            WorkflowActionInvocation(
                action_id="workflow_introspection.apply_repairs",
                description="Apply planned maintenance repairs when requested.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                reason="on_failure",
            ),
            WorkflowTransitionSpec(
                to_state="verify",
                condition=lambda _ctx: True,
                reason="applied_or_skipped",
            ),
        ),
    )

    verify = WorkflowStateSpec(
        state_id="verify",
        actions=(
            WorkflowActionInvocation(
                action_id="workflow_introspection.verify_repairs",
                description="Verify maintenance repairs are now observable.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                reason="on_failure",
            ),
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="verified_or_noop",
            ),
        ),
    )

    complete = WorkflowStateSpec(
        state_id="complete",
        actions=(
            WorkflowActionInvocation(
                action_id="workflow_introspection.finalise",
                description="Publish final workflow-maintenance payload.",
            ),
        ),
        terminal=True,
    )
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
        initial_state="assess",
        states={
            "assess": assess,
            "diagnose": diagnose,
            "plan": plan,
            "apply": apply_state,
            "verify": verify,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Introspect workflow/prompt conflation incidents and perform bounded "
            "self-maintenance repairs via MCP tools."
        ),
    )


def build_workflow_introspection_maintenance_workflow_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
        definition=build_workflow_introspection_maintenance_workflow_test_definition(),
        purpose=(
            "Diagnose workflow/prompt conflation and apply bounded self-maintenance "
            "repairs through MCP tools."
        ),
        source="built_in",
    )


def register_workflow_introspection_maintenance_actions(registry: ActionRegistry) -> None:
    specs = [
        ActionSpec(
            action_id="workflow_introspection.assess_context",
            handler=_handle_assess_context,
            description="Collect workflow/prompt/turn evidence for maintenance.",
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="workflow_introspection.diagnose_conflation",
            handler=_handle_diagnose_conflation,
            description="Diagnose conflation root causes.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="workflow_introspection.plan_repairs",
            handler=_handle_plan_repairs,
            description="Plan bounded prompt/workflow repairs.",
            side_effects="none",
        ),
        ActionSpec(
            action_id="workflow_introspection.apply_repairs",
            handler=_handle_apply_repairs,
            description="Apply maintenance repairs through MCP write tools.",
            side_effects="write",
        ),
        ActionSpec(
            action_id="workflow_introspection.verify_repairs",
            handler=_handle_verify_repairs,
            description="Verify that applied repairs are now visible.",
            side_effects="read_only",
        ),
        ActionSpec(
            action_id="workflow_introspection.finalise",
            handler=_handle_finalise,
            description="Emit final maintenance workflow payload.",
            side_effects="none",
        ),
    ]
    for spec in specs:
        try:
            registry.register(spec)
        except ValueError:
            pass
