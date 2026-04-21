"""Bounded read-only repository dossier surfaces for critic-grounded diagnosis.

These helpers provide evidence-oriented access to the checked-out repository
without exposing arbitrary shell execution or unrestricted filesystem walking.
The surface is intentionally limited to:

- tracked-file snapshots
- bounded search over tracked files
- authoritative workflow/prompt definition lookup
- bounded git metadata
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .text_value_service import get_texts_for_concept
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from ..workflows.durable.registry_factory import (
    build_durable_workflow_registry_read_only,
)
from ..workflows.engine import WorkflowDefinition
from ..workflows.workflow_definition_identity_service import (
    build_workflow_definition_identity,
)

logger = logging.getLogger(__name__)

REPO_DOSSIER_RECEIPT_SCHEMA_VERSION = "repo_dossier_receipt.v1"
REPO_DOSSIER_FILE_SNAPSHOT_SCHEMA_VERSION = "repo_dossier_file_snapshot.v1"
REPO_DOSSIER_SEARCH_SCHEMA_VERSION = "repo_dossier_search.v1"
REPO_DOSSIER_WORKFLOW_SCHEMA_VERSION = "repo_dossier_workflow_definition.v1"
REPO_DOSSIER_PROMPT_SCHEMA_VERSION = "repo_dossier_prompt_definition.v1"
REPO_DOSSIER_GIT_METADATA_SCHEMA_VERSION = "repo_dossier_git_metadata.v1"
REPO_DOSSIER_SOURCE_SYSTEM = "local.git_repository"

_DEFAULT_FILE_MAX_CHARS = 8000
_DEFAULT_FILE_MAX_LINES = 200
_DEFAULT_SEARCH_LIMIT = 20
_DEFAULT_GIT_COMMIT_LIMIT = 5

_MAX_FILE_MAX_CHARS = 20000
_MAX_FILE_MAX_LINES = 400
_MAX_SEARCH_LIMIT = 50
_MAX_GIT_COMMIT_LIMIT = 10

_SECRET_FILE_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
}
_SECRET_FILE_BASENAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
}
_ENV_SAFE_MARKERS = ("example", "sample", "template")
_PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PRIVATE_KEY_END_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>^|\s|['\"])"
    r"(?P<key>[A-Za-z0-9_.-]*(?:secret|token|password|api[_-]?key|private[_-]?key)[A-Za-z0-9_.-]*)"
    r"(?P<separator>['\"]?\s*[:=]\s*)(?P<quote>['\"]?)(?P<value>.+?)(?P=quote)\s*$",
    re.IGNORECASE,
)
_PROMPT_PREDICATE_PREFERENCE: tuple[tuple[str, ...], ...] = (
    ("hasContent", "#V#hasContent"),
    ("hasDefinition", "#V#hasDefinition"),
    ("hasDescription", "#V#hasDescription"),
)


class RepoDossierError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, coerced))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _error_payload(
    code: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "error": code,
        "message": message,
    }
    if details:
        payload["details"] = dict(details)
    return payload


def _json_default(value: Any) -> Any:
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass
    return str(value)


def _payload_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            default=_json_default,
        )
    except Exception:
        return str(value)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_receipt(
    *,
    source_system: str,
    locator: Mapping[str, Any],
    authoritative_payload: Any,
    bounded_payload: Any,
    sanitised: bool = False,
) -> dict[str, Any]:
    authoritative_text = _payload_text(authoritative_payload)
    bounded_text = _payload_text(bounded_payload)
    return {
        "schema_version": REPO_DOSSIER_RECEIPT_SCHEMA_VERSION,
        "source_system": source_system,
        "locator": dict(locator),
        "authoritative_sha256": _sha256_text(authoritative_text),
        "authoritative_char_count": len(authoritative_text),
        "bounded_sha256": _sha256_text(bounded_text),
        "bounded_char_count": len(bounded_text),
        "truncated": authoritative_text != bounded_text,
        "sanitised": bool(sanitised),
    }


def _placeholder_like(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return True
    return any(
        marker in lowered
        for marker in (
            "<your",
            "<insert",
            "placeholder",
            "example",
            "sample",
            "redacted",
        )
    )


def _is_denied_repo_path(relative_path: str) -> bool:
    lowered = relative_path.replace("\\", "/").lower()
    parts = [part for part in lowered.split("/") if part]
    if any(part == ".git" for part in parts):
        return True
    basename = parts[-1] if parts else lowered
    if basename in _SECRET_FILE_BASENAMES:
        return True
    if basename.startswith(".env") and not any(
        marker in basename for marker in _ENV_SAFE_MARKERS
    ):
        return True
    if any(basename.endswith(suffix) for suffix in _SECRET_FILE_SUFFIXES):
        return True
    return False


def _normalise_repo_relative_path(value: Any, *, field_name: str) -> str:
    path_text = _safe_str(value)
    if not path_text:
        raise RepoDossierError(
            "missing_parameter",
            f"Missing required parameter: {field_name}",
            {"missing": [field_name]},
        )
    if Path(path_text).is_absolute() or re.match(r"^[A-Za-z]:", path_text):
        raise RepoDossierError(
            "absolute_path_not_allowed",
            "Only repo-relative paths are allowed.",
            {"path": path_text},
        )
    repo_root = _repo_root().resolve()
    candidate = (repo_root / path_text).resolve()
    try:
        relative = candidate.relative_to(repo_root).as_posix()
    except ValueError as exc:
        raise RepoDossierError(
            "path_outside_repo",
            "Path must stay within the repository root.",
            {"path": path_text},
        ) from exc
    if not relative or relative == ".":
        raise RepoDossierError(
            "invalid_path",
            "Path must refer to a tracked file or repo-relative prefix.",
            {"path": path_text},
        )
    if _is_denied_repo_path(relative):
        raise RepoDossierError(
            "path_blocked",
            "Path is blocked from repo dossier access.",
            {"path": relative},
        )
    return relative


def _normalise_repo_relative_paths(
    value: Any,
    *,
    field_name: str,
) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list) else [value]
    results: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        relative = _normalise_repo_relative_path(item, field_name=field_name)
        if relative in seen:
            continue
        seen.add(relative)
        results.append(relative)
    return results


def _run_git(
    args: Sequence[str],
    *,
    allowed_returncodes: Sequence[int] = (0,),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode not in set(allowed_returncodes):
        raise RepoDossierError(
            "git_command_failed",
            "Git command failed while building repo dossier evidence.",
            {
                "args": list(args),
                "returncode": completed.returncode,
                "stderr": (completed.stderr or "").strip()[:400],
            },
        )
    return completed


def _stream_git_lines(
    args: Sequence[str],
    *,
    allowed_returncodes: Sequence[int] = (0,),
    max_lines: int | None = None,
) -> tuple[list[str], bool]:
    process = subprocess.Popen(
        ["git", *args],
        cwd=_repo_root(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: list[str] = []
    truncated = False
    assert process.stdout is not None
    for raw_line in process.stdout:
        lines.append(raw_line.rstrip("\r\n"))
        if max_lines is not None and len(lines) >= max_lines:
            truncated = True
            process.terminate()
            break
    stdout_remainder, stderr_text = process.communicate()
    if stdout_remainder:
        for raw_line in stdout_remainder.splitlines():
            if max_lines is not None and len(lines) >= max_lines:
                truncated = True
                break
            lines.append(raw_line.rstrip("\r\n"))
    if not truncated and process.returncode not in set(allowed_returncodes):
        raise RepoDossierError(
            "git_command_failed",
            "Git command failed while streaming repo dossier evidence.",
            {
                "args": list(args),
                "returncode": process.returncode,
                "stderr": (stderr_text or "").strip()[:400],
            },
        )
    return lines, truncated


def _git_rev_parse(ref: str) -> str | None:
    ref_text = _safe_str(ref)
    if not ref_text:
        return None
    completed = _run_git(
        ["rev-parse", "--verify", "--quiet", ref_text],
        allowed_returncodes=(0, 1),
    )
    resolved = _safe_str(completed.stdout)
    return resolved


def _get_tracked_paths(*, prefixes: Sequence[str] = ()) -> list[str]:
    args = ["ls-files", "-z"]
    if prefixes:
        args.extend(["--", *prefixes])
    completed = _run_git(args)
    raw = completed.stdout or ""
    items = [item for item in raw.split("\0") if item]
    return [item.replace("\\", "/") for item in items if not _is_denied_repo_path(item)]


def _get_tracked_blob_map(paths: Sequence[str]) -> dict[str, str]:
    if not paths:
        return {}
    completed = _run_git(["ls-files", "--stage", "--", *paths], allowed_returncodes=(0,))
    results: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if len(parts) < 3:
            continue
        blob_sha = parts[1].strip()
        repo_path = path.strip().replace("\\", "/")
        if repo_path:
            results[repo_path] = blob_sha
    return results


def _ensure_tracked_file(path: str) -> str | None:
    tracked_map = _get_tracked_blob_map([path])
    if path not in tracked_map:
        raise RepoDossierError(
            "tracked_file_required",
            "Repo dossier file access is limited to tracked files.",
            {"path": path},
        )
    return tracked_map.get(path)


def _read_worktree_text(path: str) -> str:
    _ensure_tracked_file(path)
    file_path = _repo_root() / path
    try:
        return file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RepoDossierError(
            "file_read_failed",
            "Could not read tracked file from the working tree.",
            {"path": path},
        ) from exc


def _read_revision_text(path: str, revision: str) -> str:
    _ensure_tracked_file(path)
    completed = _run_git(
        ["show", f"{revision}:{path}"],
        allowed_returncodes=(0,),
    )
    return completed.stdout


def _sanitise_text(text: str) -> tuple[str, bool]:
    if not text:
        return "", False
    lines = text.splitlines()
    in_private_key_block = False
    sanitised_any = False
    output_lines: list[str] = []
    for line in lines:
        if _PRIVATE_KEY_BEGIN_RE.search(line):
            in_private_key_block = True
            sanitised_any = True
            output_lines.append("[REDACTED PRIVATE KEY BLOCK]")
            continue
        if in_private_key_block:
            sanitised_any = True
            if _PRIVATE_KEY_END_RE.search(line):
                in_private_key_block = False
            continue
        match = _SECRET_ASSIGNMENT_RE.search(line)
        if match:
            value = match.group("value").strip()
            if value and not _placeholder_like(value):
                sanitised_any = True
                line = (
                    f"{match.group('prefix')}{match.group('key')}"
                    f"{match.group('separator')}{match.group('quote')}"
                    f"[REDACTED]{match.group('quote')}"
                )
        output_lines.append(line)
    return "\n".join(output_lines), sanitised_any


def _numbered_excerpt(
    text: str,
    *,
    max_lines: int,
    max_chars: int,
) -> tuple[str, bool, int]:
    lines = text.splitlines()
    line_count = len(lines)
    displayed = lines[:max_lines]
    truncated = line_count > max_lines
    excerpt = "\n".join(
        f"{index + 1:04d}: {line}" for index, line in enumerate(displayed)
    )
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars]
        truncated = True
    return excerpt, truncated, line_count


def _parse_git_grep_count_output(output: str) -> tuple[int, int]:
    total_matches = 0
    file_match_count = 0
    for line in output.splitlines():
        if ":" not in line:
            continue
        _path, raw_count = line.rsplit(":", 1)
        try:
            count = int(raw_count.strip())
        except ValueError:
            continue
        total_matches += count
        if count > 0:
            file_match_count += 1
    return total_matches, file_match_count


def _parse_git_grep_line(line: str) -> tuple[str, int, str] | None:
    match = re.match(r"^(.*?):(\d+):(.*)$", line)
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3)


def _select_prompt_row(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for aliases in _PROMPT_PREDICATE_PREFERENCE:
        for row in rows:
            predicate = _safe_str(row.get("predicate"))
            text = _safe_str(row.get("text"))
            if text and predicate in aliases:
                return row
    for row in rows:
        text = _safe_str(row.get("text"))
        if text:
            return row
    return None


def _summarise_workflow_definition(definition: WorkflowDefinition) -> dict[str, Any]:
    states: list[dict[str, Any]] = []
    for state_id, state_spec in sorted(definition.states.items(), key=lambda item: item[0]):
        metadata = (
            dict(state_spec.metadata)
            if isinstance(state_spec.metadata, Mapping)
            else {}
        )
        prompt_contract = metadata.get("prompt_contract")
        state_summary: dict[str, Any] = {
            "state_id": state_id,
            "terminal": bool(state_spec.terminal),
            "action_ids": [
                str(getattr(action, "action_id", "") or "").strip()
                for action in state_spec.actions
                if str(getattr(action, "action_id", "") or "").strip()
            ],
            "transitions": [
                {
                    "to_state": str(transition.to_state),
                    "reason": _safe_str(getattr(transition, "reason", None)),
                    "description": _safe_str(
                        getattr(transition, "description", None)
                    ),
                    "condition_spec": (
                        dict(transition.condition_spec)
                        if isinstance(transition.condition_spec, Mapping)
                        else None
                    ),
                }
                for transition in state_spec.transitions[:20]
            ],
        }
        if metadata:
            metadata_summary: dict[str, Any] = {}
            for key in (
                "preconditions",
                "effects",
                "reads_context_keys",
                "writes_context_keys",
                "reads_variables",
                "writes_variables",
            ):
                value = metadata.get(key)
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    metadata_summary[key] = list(value)
            if isinstance(prompt_contract, Mapping):
                metadata_summary["prompt_contract"] = dict(prompt_contract)
            if metadata_summary:
                state_summary["metadata"] = metadata_summary
        states.append(state_summary)
    return {
        "workflow_id": definition.workflow_id,
        "purpose": _safe_str(definition.purpose),
        "initial_state": definition.initial_state,
        "termination_states": list(definition.termination_states),
        "metadata": dict(definition.metadata)
        if isinstance(definition.metadata, Mapping)
        else {},
        "state_count": len(states),
        "states": states,
    }


def repo_dossier_file_snapshot(**kwargs: Any) -> dict[str, Any]:
    try:
        path = _normalise_repo_relative_path(kwargs.get("path"), field_name="path")
        max_chars = _coerce_int(
            kwargs.get("max_chars"),
            default=_DEFAULT_FILE_MAX_CHARS,
            minimum=200,
            maximum=_MAX_FILE_MAX_CHARS,
        )
        max_lines = _coerce_int(
            kwargs.get("max_lines"),
            default=_DEFAULT_FILE_MAX_LINES,
            minimum=20,
            maximum=_MAX_FILE_MAX_LINES,
        )
        revision = _safe_str(kwargs.get("revision")) or "WORKTREE"
        tracked_blob_sha = _ensure_tracked_file(path)
        if revision.upper() == "WORKTREE":
            raw_text = _read_worktree_text(path)
            source_kind = "worktree_tracked_file"
        else:
            if _git_rev_parse(revision) is None:
                raise RepoDossierError(
                    "invalid_revision",
                    "Revision could not be resolved for repo dossier snapshot.",
                    {"revision": revision},
                )
            raw_text = _read_revision_text(path, revision)
            source_kind = "git_tracked_blob"
        sanitised_text, sanitised = _sanitise_text(raw_text)
        excerpt, truncated, line_count = _numbered_excerpt(
            sanitised_text,
            max_lines=max_lines,
            max_chars=max_chars,
        )
        locator = {
            "path": path,
            "revision": revision,
            "source_kind": source_kind,
            "tracked_blob_sha": tracked_blob_sha,
        }
        bounded_payload = {
            "path": path,
            "revision": revision,
            "content": excerpt,
        }
        return {
            "success": True,
            "schema_version": REPO_DOSSIER_FILE_SNAPSHOT_SCHEMA_VERSION,
            "path": path,
            "revision": revision,
            "source_kind": source_kind,
            "tracked_blob_sha": tracked_blob_sha,
            "line_count": line_count,
            "content": excerpt,
            "truncated": bool(truncated),
            "sanitised": bool(sanitised),
            "receipt": _build_receipt(
                source_system=REPO_DOSSIER_SOURCE_SYSTEM,
                locator=locator,
                authoritative_payload={"path": path, "revision": revision, "text": raw_text},
                bounded_payload=bounded_payload,
                sanitised=sanitised,
            ),
        }
    except RepoDossierError as exc:
        return _error_payload(exc.code, exc.message, **dict(exc.details or {}))


def repo_dossier_search(**kwargs: Any) -> dict[str, Any]:
    try:
        query = _safe_str(kwargs.get("query"))
        if not query:
            raise RepoDossierError(
                "missing_parameter",
                "Missing required parameter: query",
                {"missing": ["query"]},
            )
        limit = _coerce_int(
            kwargs.get("limit"),
            default=_DEFAULT_SEARCH_LIMIT,
            minimum=1,
            maximum=_MAX_SEARCH_LIMIT,
        )
        path_prefixes = _normalise_repo_relative_paths(
            kwargs.get("path_prefixes"),
            field_name="path_prefixes",
        )
        regex = bool(kwargs.get("regex", False))
        tracked_paths = _get_tracked_paths(prefixes=path_prefixes)
        grep_base = ["grep", "--full-name", "--no-color", "-I"]
        grep_base.append("-E" if regex else "-F")
        grep_args = [*grep_base, "--count", "-e", query, "--", *path_prefixes]
        count_result = _run_git(grep_args, allowed_returncodes=(0, 1))
        total_matches, matched_file_count = _parse_git_grep_count_output(
            count_result.stdout or ""
        )
        line_args = [*grep_base, "-n", "-e", query, "--", *path_prefixes]
        raw_lines, truncated_stream = _stream_git_lines(
            line_args,
            allowed_returncodes=(0, 1),
            max_lines=limit,
        )
        blob_map = _get_tracked_blob_map(tracked_paths)
        matches: list[dict[str, Any]] = []
        sanitised_any = False
        for raw_line in raw_lines:
            parsed = _parse_git_grep_line(raw_line)
            if parsed is None:
                continue
            path, line_number, line_text = parsed
            if _is_denied_repo_path(path):
                continue
            sanitised_line, line_sanitised = _sanitise_text(line_text)
            sanitised_any = sanitised_any or line_sanitised
            matches.append(
                {
                    "path": path,
                    "line_number": line_number,
                    "line_text": sanitised_line,
                    "tracked_blob_sha": blob_map.get(path),
                }
            )
        truncated = bool(total_matches > len(matches) or truncated_stream)
        locator = {
            "query": query,
            "path_prefixes": list(path_prefixes),
            "regex": regex,
        }
        bounded_payload = {
            "query": query,
            "matches": matches,
            "match_count_returned": len(matches),
        }
        return {
            "success": True,
            "schema_version": REPO_DOSSIER_SEARCH_SCHEMA_VERSION,
            "query": query,
            "regex": regex,
            "path_prefixes": list(path_prefixes),
            "searched_file_count": len(tracked_paths),
            "matched_file_count": matched_file_count,
            "match_count_total": total_matches,
            "match_count_returned": len(matches),
            "truncated": truncated,
            "sanitised": bool(sanitised_any),
            "matches": matches,
            "receipt": _build_receipt(
                source_system=REPO_DOSSIER_SOURCE_SYSTEM,
                locator=locator,
                authoritative_payload={
                    "query": query,
                    "regex": regex,
                    "path_prefixes": list(path_prefixes),
                    "match_count_total": total_matches,
                    "matches": matches,
                },
                bounded_payload=bounded_payload,
                sanitised=sanitised_any,
            ),
        }
    except RepoDossierError as exc:
        return _error_payload(exc.code, exc.message, **dict(exc.details or {}))


def repo_dossier_workflow_definition_get(**kwargs: Any) -> dict[str, Any]:
    try:
        workflow_id = _safe_str(kwargs.get("workflow_id"))
        if not workflow_id:
            raise RepoDossierError(
                "missing_parameter",
                "Missing required parameter: workflow_id",
                {"missing": ["workflow_id"]},
            )
        registry = build_durable_workflow_registry_read_only(defer_parity_work=True)
        registration = registry.get_registration(workflow_id)
        if registration is None:
            raise RepoDossierError(
                "workflow_not_found",
                "Workflow definition was not found in the authoritative registry.",
                {"workflow_id": workflow_id},
            )
        definition = registration.definition
        definition_summary = _summarise_workflow_definition(definition)
        definition_identity = build_workflow_definition_identity(
            workflow_id=workflow_id,
            source=getattr(registration, "source", None),
            definition=definition,
        )
        locator = {
            "workflow_id": workflow_id,
            "registration_source": getattr(registration, "source", None),
        }
        bounded_payload = {
            "workflow_id": workflow_id,
            "definition_identity": definition_identity,
            "definition_summary": definition_summary,
        }
        return {
            "success": True,
            "schema_version": REPO_DOSSIER_WORKFLOW_SCHEMA_VERSION,
            "workflow_id": workflow_id,
            "registration_source": getattr(registration, "source", None),
            "definition_identity": definition_identity,
            "definition_summary": definition_summary,
            "receipt": _build_receipt(
                source_system="workflow.registry",
                locator=locator,
                authoritative_payload=bounded_payload,
                bounded_payload=bounded_payload,
            ),
        }
    except RepoDossierError as exc:
        return _error_payload(exc.code, exc.message, **dict(exc.details or {}))
    except Exception as exc:
        logger.warning(
            "[repo_dossier] workflow definition lookup failed for %s: %s",
            kwargs.get("workflow_id"),
            exc,
        )
        return _error_payload(
            "workflow_lookup_failed",
            "Workflow definition lookup failed.",
            workflow_id=kwargs.get("workflow_id"),
        )


def repo_dossier_prompt_definition_get(**kwargs: Any) -> dict[str, Any]:
    try:
        prompt_concept_id = _safe_str(kwargs.get("prompt_concept_id"))
        if not prompt_concept_id:
            raise RepoDossierError(
                "missing_parameter",
                "Missing required parameter: prompt_concept_id",
                {"missing": ["prompt_concept_id"]},
            )
        max_chars = _coerce_int(
            kwargs.get("max_chars"),
            default=_DEFAULT_FILE_MAX_CHARS,
            minimum=200,
            maximum=_MAX_FILE_MAX_CHARS,
        )
        try:
            prompt_concept = get_concept_by_concept_id(prompt_concept_id)
        except ConceptNotFoundError:
            prompt_concept = None
        if not isinstance(prompt_concept, Mapping):
            raise RepoDossierError(
                "prompt_concept_not_found",
                "Prompt concept was not found in Vontology.",
                {"prompt_concept_id": prompt_concept_id},
            )
        rows = get_texts_for_concept(prompt_concept_id, limit=20)
        if not isinstance(rows, list):
            rows = []
        selected_row = _select_prompt_row([row for row in rows if isinstance(row, Mapping)])
        if selected_row is None:
            raise RepoDossierError(
                "prompt_content_missing",
                "Prompt concept exists but did not resolve to any authoritative prompt text.",
                {"prompt_concept_id": prompt_concept_id},
            )
        raw_text = str(selected_row.get("text") or "")
        sanitised_text, sanitised = _sanitise_text(raw_text)
        truncated = len(sanitised_text) > max_chars
        bounded_text = sanitised_text[:max_chars] if truncated else sanitised_text
        available_relations = [
            {
                "predicate": _safe_str(row.get("predicate")),
                "language": _safe_str(row.get("lang") or row.get("language")),
                "char_count": len(str(row.get("text") or "")),
            }
            for row in rows
            if isinstance(row, Mapping)
        ]
        locator = {
            "prompt_concept_id": prompt_concept_id,
            "selected_predicate": _safe_str(selected_row.get("predicate")),
        }
        bounded_payload = {
            "prompt_concept_id": prompt_concept_id,
            "text": bounded_text,
            "selected_predicate": _safe_str(selected_row.get("predicate")),
        }
        return {
            "success": True,
            "schema_version": REPO_DOSSIER_PROMPT_SCHEMA_VERSION,
            "prompt_concept_id": prompt_concept_id,
            "selected_predicate": _safe_str(selected_row.get("predicate")),
            "language": _safe_str(
                selected_row.get("lang") or selected_row.get("language")
            ),
            "text": bounded_text,
            "truncated": truncated,
            "sanitised": bool(sanitised),
            "available_relations": available_relations,
            "receipt": _build_receipt(
                source_system="vontology.prompt_text",
                locator=locator,
                authoritative_payload={
                    "prompt_concept_id": prompt_concept_id,
                    "selected_row": dict(selected_row),
                    "available_relations": available_relations,
                },
                bounded_payload=bounded_payload,
                sanitised=sanitised,
            ),
        }
    except RepoDossierError as exc:
        return _error_payload(exc.code, exc.message, **dict(exc.details or {}))


def _parse_status_porcelain(output: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        entries.append(
            {
                "index_status": line[0],
                "worktree_status": line[1],
                "path": line[3:].strip().replace("\\", "/"),
            }
        )
    return entries


def _parse_git_log(output: str) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        commits.append(
            {
                "commit_sha": parts[0],
                "short_commit_sha": parts[1],
                "author": parts[2],
                "committed_at": parts[3],
                "subject": parts[4],
            }
        )
    return commits


def _parse_numstat(output: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        files.append(
            {
                "path": path.replace("\\", "/"),
                "insertions": 0 if added == "-" else int(added),
                "deletions": 0 if deleted == "-" else int(deleted),
            }
        )
    return files


def repo_dossier_git_metadata(**kwargs: Any) -> dict[str, Any]:
    try:
        paths = _normalise_repo_relative_paths(kwargs.get("paths"), field_name="paths")
        commit_limit = _coerce_int(
            kwargs.get("commit_limit"),
            default=_DEFAULT_GIT_COMMIT_LIMIT,
            minimum=1,
            maximum=_MAX_GIT_COMMIT_LIMIT,
        )
        compare_base = _safe_str(kwargs.get("compare_base")) or "HEAD"
        if _git_rev_parse(compare_base) is None:
            raise RepoDossierError(
                "invalid_revision",
                "compare_base could not be resolved.",
                {"compare_base": compare_base},
            )
        tracked_blob_map = _get_tracked_blob_map(paths) if paths else {}
        if paths:
            missing = [path for path in paths if path not in tracked_blob_map]
            if missing:
                raise RepoDossierError(
                    "tracked_file_required",
                    "Repo dossier git metadata is limited to tracked paths.",
                    {"paths": missing},
                )
        branch = _safe_str(_run_git(["branch", "--show-current"]).stdout) or "HEAD"
        head_commit = _safe_str(_run_git(["rev-parse", "HEAD"]).stdout)
        short_head_commit = _safe_str(_run_git(["rev-parse", "--short", "HEAD"]).stdout)
        subject = _safe_str(_run_git(["log", "-1", "--pretty=%s"]).stdout)
        status_output = _run_git(
            ["status", "--short", "--", *paths] if paths else ["status", "--short"]
        ).stdout
        status_entries = _parse_status_porcelain(status_output)
        log_args = [
            "log",
            f"-n{commit_limit}",
            "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s",
        ]
        if paths:
            log_args.extend(["--", *paths])
        commits = _parse_git_log(_run_git(log_args, allowed_returncodes=(0, 1)).stdout)
        diff_args = ["diff", "--numstat", compare_base]
        if paths:
            diff_args.extend(["--", *paths])
        diff_files = _parse_numstat(_run_git(diff_args, allowed_returncodes=(0, 1)).stdout)
        locator = {
            "paths": list(paths),
            "compare_base": compare_base,
            "branch": branch,
        }
        bounded_payload = {
            "branch": branch,
            "head_commit": head_commit,
            "paths": list(paths),
            "recent_commits": commits,
            "diff_files": diff_files,
            "status_entries": status_entries,
        }
        return {
            "success": True,
            "schema_version": REPO_DOSSIER_GIT_METADATA_SCHEMA_VERSION,
            "branch": branch,
            "head_commit": {
                "commit_sha": head_commit,
                "short_commit_sha": short_head_commit,
                "subject": subject,
            },
            "compare_base": compare_base,
            "paths": list(paths),
            "path_receipts": [
                {"path": path, "tracked_blob_sha": tracked_blob_map.get(path)}
                for path in paths
            ],
            "dirty": bool(status_entries),
            "status_entries": status_entries,
            "recent_commits": commits,
            "diff_files": diff_files,
            "receipt": _build_receipt(
                source_system=REPO_DOSSIER_SOURCE_SYSTEM,
                locator=locator,
                authoritative_payload=bounded_payload,
                bounded_payload=bounded_payload,
            ),
        }
    except RepoDossierError as exc:
        return _error_payload(exc.code, exc.message, **dict(exc.details or {}))


__all__ = [
    "repo_dossier_file_snapshot",
    "repo_dossier_git_metadata",
    "repo_dossier_prompt_definition_get",
    "repo_dossier_search",
    "repo_dossier_workflow_definition_get",
]
