"""Support for bounded, provenance-bound workflow authority snapshots.

This module keeps exact-output projection, durable worker attestation, and
awaited child-workflow read-back separate from the chat orchestrator. It
provides generic execution and telemetry support only; represented workflows
and Vontology artefacts remain authoritative for release policy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ...workflows.engine import WorkflowResult
from ...workflows.execution_contracts import (
    LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
    WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
)

_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_KEY = "workflow_authority_output"
_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_METADATA_KEY = "workflow_authority_output_snapshot"
_WORKFLOW_EXECUTION_IDENTITY_SCHEMA_VERSION = "workflow_execution_identity.v1"
_WORKFLOW_EXECUTION_IDENTITY_ATTRIBUTE = "_von_workflow_execution_identity"
_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_DEPTH = 12
_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_ITEMS_PER_CONTAINER = 64
_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_TOTAL_ITEMS = 512
_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_STRING_CHARS = 8_000
_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_TOTAL_STRING_CHARS = 64_000
_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_RECORDED_PATHS = 32
_WORKFLOW_AUTHORITY_OUTPUT_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "encrypted_content",
    "id_token",
    "oauth",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "set_cookie",
    "token",
)


def _workflow_authority_output_key_is_sensitive(key: str) -> bool:
    lowered = key.strip().lower()
    return bool(lowered) and any(
        marker in lowered for marker in _WORKFLOW_AUTHORITY_OUTPUT_SENSITIVE_KEY_PARTS
    )


def _build_workflow_authority_output_snapshot(
    value: Any,
) -> tuple[Any, dict[str, Any]]:
    """Build a bounded, secret-aware projection for represented authority output.

    Registration consumers require ``exact`` to remain true.  A workflow can
    therefore expose rich represented output to its persisted Turn Execution
    Record without allowing an untrusted response to grow telemetry without
    bounds or to smuggle credential-shaped values into diagnostics.
    """

    total_items = 0
    total_string_chars = 0
    redacted_count = 0
    truncated_count = 0
    redacted_paths: list[str] = []
    truncated_paths: list[str] = []

    def _record_path(paths: list[str], path: str) -> None:
        if len(paths) < _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_RECORDED_PATHS:
            paths.append(path)

    def _redact(path: str) -> str:
        nonlocal redacted_count
        redacted_count += 1
        _record_path(redacted_paths, path)
        return "[redacted]"

    def _truncate(path: str, replacement: Any) -> Any:
        nonlocal truncated_count
        truncated_count += 1
        _record_path(truncated_paths, path)
        return replacement

    def _walk(current: Any, *, depth: int, path: str) -> Any:
        nonlocal total_items, total_string_chars
        if current is None or isinstance(current, (bool, int)):
            return current
        if isinstance(current, float):
            try:
                json.dumps(current, allow_nan=False)
            except (TypeError, ValueError):
                return _truncate(path, str(current))
            return current
        if isinstance(current, str):
            remaining = max(
                0,
                _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_TOTAL_STRING_CHARS
                - total_string_chars,
            )
            permitted = min(
                _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_STRING_CHARS,
                remaining,
            )
            if len(current) > permitted:
                projected = current[:permitted]
                total_string_chars += len(projected)
                return _truncate(path, projected)
            total_string_chars += len(current)
            return current
        if depth >= _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_DEPTH:
            kind = "mapping" if isinstance(current, Mapping) else "sequence"
            return _truncate(path, {"_snapshot_truncated": f"{kind}_depth_limit"})
        if isinstance(current, Mapping):
            projected_mapping: dict[str, Any] = {}
            for index, (key, item) in enumerate(current.items()):
                item_path = f"{path}.{str(key)[:120]}"
                if index >= (
                    _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_ITEMS_PER_CONTAINER
                ):
                    projected_mapping["_snapshot_truncated_items"] = _truncate(
                        path,
                        max(1, len(current) - index),
                    )
                    break
                if total_items >= _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_TOTAL_ITEMS:
                    projected_mapping["_snapshot_truncated_items"] = _truncate(
                        path,
                        max(1, len(current) - index),
                    )
                    break
                total_items += 1
                if not isinstance(key, str):
                    _truncate(item_path, None)
                    continue
                if len(key) > 120:
                    _truncate(item_path, None)
                    continue
                if callable(item):
                    projected_mapping[key] = _truncate(
                        item_path,
                        f"<{type(item).__name__}>",
                    )
                    continue
                if _workflow_authority_output_key_is_sensitive(key):
                    projected_mapping[key] = _redact(item_path)
                    continue
                projected_mapping[key] = _walk(
                    item,
                    depth=depth + 1,
                    path=item_path,
                )
            return projected_mapping
        if isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            projected_items: list[Any] = []
            for index, item in enumerate(current):
                item_path = f"{path}[{index}]"
                if index >= (
                    _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_ITEMS_PER_CONTAINER
                ) or total_items >= (
                    _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_TOTAL_ITEMS
                ):
                    projected_items.append(
                        _truncate(item_path, "<snapshot item limit reached>")
                    )
                    break
                total_items += 1
                projected_items.append(_walk(item, depth=depth + 1, path=item_path))
            return projected_items
        return _truncate(path, str(current))

    projected = _walk(value, depth=0, path="$")
    exact = redacted_count == 0 and truncated_count == 0
    output_sha256: str | None = None
    if exact:
        try:
            encoded = json.dumps(
                projected,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            exact = False
            truncated_count += 1
            _record_path(truncated_paths, "$")
        else:
            output_sha256 = hashlib.sha256(encoded).hexdigest()
    metadata = {
        "schema_version": "workflow_authority_output_snapshot.v1",
        "exact": exact,
        "output_sha256": output_sha256,
        "redacted_count": redacted_count,
        "redacted_paths": redacted_paths,
        "truncated_count": truncated_count,
        "truncated_paths": truncated_paths,
        "projected_item_count": total_items,
        "projected_string_char_count": total_string_chars,
        "limits": {
            "max_depth": _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_DEPTH,
            "max_items_per_container": (
                _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_ITEMS_PER_CONTAINER
            ),
            "max_total_items": _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_TOTAL_ITEMS,
            "max_string_chars": (_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_STRING_CHARS),
            "max_total_string_chars": (
                _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_MAX_TOTAL_STRING_CHARS
            ),
        },
    }
    return projected, metadata


def _workflow_authority_prompt_lineage(
    result_data: Mapping[str, Any],
) -> dict[str, Any]:
    """Project hash-only prompt and rendered-context lineage from step surfaces."""

    prompt_hashes: list[str] = []
    context_fields_hashes: list[str] = []
    prompt_ids: list[str] = []
    pending: list[tuple[Any, int]] = []
    for key in (
        "prompt_context_diagnostics",
        LAST_WORKFLOW_STEP_RESULT_ENVELOPE_KEY,
        WORKFLOW_STEP_RESULT_ENVELOPES_KEY,
    ):
        pending.append((result_data.get(key), 0))
    visited = 0
    while pending and visited < 256:
        current, depth = pending.pop()
        visited += 1
        if depth > 8:
            continue
        if isinstance(current, Mapping):
            prompt_hash = current.get("prompt_content_sha256")
            if (
                isinstance(prompt_hash, str)
                and len(prompt_hash) == 64
                and all(character in "0123456789abcdef" for character in prompt_hash)
                and prompt_hash not in prompt_hashes
            ):
                prompt_hashes.append(prompt_hash)
            context_fields_hash = current.get("llm_context_fields_sha256")
            if (
                isinstance(context_fields_hash, str)
                and len(context_fields_hash) == 64
                and all(
                    character in "0123456789abcdef" for character in context_fields_hash
                )
                and context_fields_hash not in context_fields_hashes
            ):
                context_fields_hashes.append(context_fields_hash)
            prompt_id = current.get("resolved_prompt_concept_id")
            if (
                isinstance(prompt_id, str)
                and prompt_id.strip()
                and prompt_id.strip() not in prompt_ids
            ):
                prompt_ids.append(prompt_id.strip())
            for index, item in enumerate(current.values()):
                if index >= 64:
                    break
                pending.append((item, depth + 1))
        elif isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            for index, item in enumerate(current):
                if index >= 64:
                    break
                pending.append((item, depth + 1))

    lineage: dict[str, Any] = {
        "prompt_lineage_observed": bool(prompt_hashes),
        "prompt_lineage_ambiguous": len(prompt_hashes) > 1,
        "llm_context_fields_lineage_observed": bool(context_fields_hashes),
        "llm_context_fields_lineage_ambiguous": len(context_fields_hashes) > 1,
    }
    if len(prompt_hashes) == 1:
        lineage["prompt_content_sha256"] = prompt_hashes[0]
    elif prompt_hashes:
        lineage["prompt_content_sha256s"] = prompt_hashes
    if len(context_fields_hashes) == 1:
        lineage["llm_context_fields_sha256"] = context_fields_hashes[0]
    elif context_fields_hashes:
        lineage["llm_context_fields_sha256s"] = context_fields_hashes
    if len(prompt_ids) == 1:
        lineage["resolved_prompt_concept_id"] = prompt_ids[0]
    elif prompt_ids:
        lineage["resolved_prompt_concept_ids"] = prompt_ids
    return lineage


def _workflow_execute_snapshot_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _workflow_execute_vontology_definition_identity(
    value: Any,
    *,
    workflow_id: str,
) -> dict[str, Any] | None:
    """Return a definition identity suitable for exact authority binding.

    The bridge below is deliberately stricter than ordinary workflow launch
    telemetry.  A represented authority output can influence a release only
    when the exact definition executed by the durable worker is also proven to
    be the Vontology-authoritative definition.
    """

    if not isinstance(value, Mapping):
        return None
    identity = dict(value)
    definition_hash = _workflow_execute_snapshot_text(identity.get("definition_hash"))
    authoritative_hash = _workflow_execute_snapshot_text(
        identity.get("authoritative_definition_hash")
    )
    runtime_hash = _workflow_execute_snapshot_text(
        identity.get("runtime_definition_hash")
    )
    if (
        identity.get("schema_version") != "workflow_definition_identity.v1"
        or identity.get("workflow_id") != workflow_id
        or str(identity.get("source") or "").strip().lower() != "vontology"
        or not definition_hash
        or len(definition_hash) != 64
        or any(character not in "0123456789abcdef" for character in definition_hash)
        or authoritative_hash != definition_hash
        or identity.get("hash_mismatch") is not False
        or runtime_hash != definition_hash
    ):
        return None
    return {
        key: identity[key]
        for key in (
            "schema_version",
            "version",
            "workflow_id",
            "source",
            "definition_hash",
            "runtime_definition_hash",
            "authoritative_definition_hash",
            "hash_mismatch",
            "state_count",
            "action_count",
        )
        if key in identity
    }


@dataclass(frozen=True)
class _AwaitedWorkflowExecuteEnvelope:
    workflow_id: str
    instance_id: str


@dataclass(frozen=True)
class _WorkflowExecuteActorScope:
    namespace: str
    user_id: str
    org_id: str | None


def _workflow_execute_snapshot_failure(
    *,
    workflow_id: str | None,
    reason_code: str,
    instance_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "workflow_execute_result_snapshot",
        "source": "workflow_execute_tool",
        "status": "not_recorded",
        "workflow_id": workflow_id,
        "reason_code": reason_code,
    }
    if instance_id:
        payload["workflow_instance_id"] = instance_id
    return payload


def _verified_awaited_workflow_execute_envelope(
    *,
    tool_payload: Mapping[str, Any],
    tool_result_payload: Any,
) -> tuple[_AwaitedWorkflowExecuteEnvelope | None, str | None]:
    workflow_id = _workflow_execute_snapshot_text(tool_payload.get("workflow_id"))
    if not workflow_id or not isinstance(tool_result_payload, Mapping):
        return None, "workflow_execute_result_missing"
    if tool_result_payload.get("success") is not True:
        return None, "workflow_execute_not_successful"
    execution = tool_result_payload.get("workflow_execution")
    if not isinstance(execution, Mapping):
        return None, "workflow_execute_execution_envelope_missing"
    instance_id = _workflow_execute_snapshot_text(execution.get("instance_id"))
    if not instance_id:
        return None, "workflow_execute_instance_id_missing"
    envelope = _AwaitedWorkflowExecuteEnvelope(workflow_id, instance_id)
    if (
        execution.get("workflow_id") != workflow_id
        or execution.get("await_terminal") is not True
        or execution.get("timed_out") is not False
        or tool_result_payload.get("timed_out") is True
    ):
        return envelope, "workflow_execute_not_awaited_terminal_snapshot"
    final_status = (
        _workflow_execute_snapshot_text(execution.get("final_status"))
        or _workflow_execute_snapshot_text(execution.get("current_status"))
        or _workflow_execute_snapshot_text(tool_result_payload.get("final_status"))
    )
    if str(final_status or "").lower() != "completed":
        return envelope, "workflow_execute_not_completed"
    return envelope, None


def _verified_workflow_execute_submission_identity(
    *,
    tool_result_payload: Mapping[str, Any],
    workflow_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    verification = tool_result_payload.get("verification")
    if not isinstance(verification, Mapping):
        return None, "workflow_execute_submission_identity_missing"
    preflight = verification.get("preflight")
    postflight = verification.get("postflight")
    preflight_identity = _workflow_execute_vontology_definition_identity(
        preflight.get("definition_identity")
        if isinstance(preflight, Mapping)
        else None,
        workflow_id=workflow_id,
    )
    postflight_identity = _workflow_execute_vontology_definition_identity(
        postflight.get("definition_identity")
        if isinstance(postflight, Mapping)
        else None,
        workflow_id=workflow_id,
    )
    if (
        verification.get("preflight_passed") is not True
        or verification.get("postflight_passed") is not True
        or preflight_identity is None
        or postflight_identity is None
        or preflight_identity.get("definition_hash")
        != postflight_identity.get("definition_hash")
    ):
        return None, "workflow_execute_submission_identity_unverified"
    return postflight_identity, None


def _verified_workflow_execute_actor_scope(
    *,
    tool_payload: Mapping[str, Any],
    parent_data: Mapping[str, Any],
    user_namespace: str | None,
) -> tuple[_WorkflowExecuteActorScope | None, str | None]:
    # The environment namespace is established before tool arguments exist.
    namespace = _workflow_execute_snapshot_text(user_namespace)
    try:
        from ...services.namespace_service import derive_actor_context_from_namespace

        user_id, org_id = derive_actor_context_from_namespace(namespace or "")
    except Exception:
        user_id, org_id = None, None
    user_id = _workflow_execute_snapshot_text(user_id)
    org_id = _workflow_execute_snapshot_text(org_id)
    if not namespace or not user_id:
        return None, "workflow_execute_parent_actor_scope_missing"

    expected = _WorkflowExecuteActorScope(namespace, user_id, org_id)
    namespace_hints = (
        parent_data.get("namespace"),
        parent_data.get("user_namespace"),
        tool_payload.get("namespace"),
    )
    user_hints = (
        parent_data.get("user_concept_id"),
        tool_payload.get("user_id"),
        tool_payload.get("user_concept_id"),
    )
    org_hints = (
        parent_data.get("org_concept_id"),
        parent_data.get("organisation_concept_id"),
        tool_payload.get("org_id"),
        tool_payload.get("org_concept_id"),
        tool_payload.get("organisation_concept_id"),
    )
    mismatched = (
        any(
            (hint := _workflow_execute_snapshot_text(value)) is not None
            and hint != expected.namespace
            for value in namespace_hints
        )
        or any(
            (hint := _workflow_execute_snapshot_text(value)) is not None
            and hint != expected.user_id
            for value in user_hints
        )
        or any(
            (hint := _workflow_execute_snapshot_text(value)) is not None
            and hint != expected.org_id
            for value in org_hints
        )
    )
    if mismatched:
        return None, "workflow_execute_requested_actor_scope_mismatch"
    return expected, None


def _workflow_execute_instance_matches_scope(
    *,
    instance: Any,
    envelope: _AwaitedWorkflowExecuteEnvelope,
    actor_scope: _WorkflowExecuteActorScope,
) -> bool:
    status_raw = getattr(instance, "status", None)
    status = _workflow_execute_snapshot_text(getattr(status_raw, "value", status_raw))
    return bool(
        getattr(instance, "instance_id", None) == envelope.instance_id
        and getattr(instance, "workflow_id", None) == envelope.workflow_id
        and str(status or "").lower() == "completed"
        and getattr(instance, "user_id", None) == actor_scope.user_id
        and getattr(instance, "namespace", None) == actor_scope.namespace
        and getattr(instance, "org_id", None) == actor_scope.org_id
    )


def _workflow_execute_claim_provenance_projection(instance: Any) -> dict[str, Any]:
    """Return a bounded audit projection without exposing the opaque claim token."""

    claimed_by_build = getattr(instance, "claimed_by_build", None)
    checkpoint_attestation = getattr(instance, "authority_checkpoint_attestation", None)
    claim = claimed_by_build if isinstance(claimed_by_build, Mapping) else {}
    attestation = (
        checkpoint_attestation if isinstance(checkpoint_attestation, Mapping) else {}
    )
    claim_token = _workflow_execute_snapshot_text(
        getattr(instance, "claim_token", None)
    )
    capabilities = claim.get("capabilities")
    projected_capabilities = (
        sorted(
            {
                str(item).strip()
                for item in capabilities
                if isinstance(item, str) and item.strip()
            }
        )[:16]
        if isinstance(capabilities, Sequence)
        and not isinstance(capabilities, (str, bytes, bytearray))
        else []
    )
    projection = {
        "schema_version": "workflow_execute_claim_provenance.v1",
        "worker_claim_schema_version": _workflow_execute_snapshot_text(
            claim.get("schema_version")
        ),
        "worker_id": _workflow_execute_snapshot_text(claim.get("worker_id")),
        "version": _workflow_execute_snapshot_text(claim.get("version")),
        "version_base": _workflow_execute_snapshot_text(claim.get("version_base")),
        "git_commit": _workflow_execute_snapshot_text(claim.get("git_commit")),
        "git_short_commit": _workflow_execute_snapshot_text(
            claim.get("git_short_commit")
        ),
        "capabilities": projected_capabilities,
        "claim_token_sha256": (
            hashlib.sha256(claim_token.encode("utf-8")).hexdigest()
            if claim_token
            else None
        ),
        "checkpoint_attestation_schema_version": (
            _workflow_execute_snapshot_text(attestation.get("schema_version"))
        ),
        "authority_payload_sha256": _workflow_execute_snapshot_text(
            attestation.get("authority_payload_sha256")
        ),
        "exact_snapshot_eligible": attestation.get("exact_snapshot_eligible"),
    }
    return {key: value for key, value in projection.items() if value is not None}


def _exact_workflow_execute_child_result(
    *,
    instance: Any,
    envelope: _AwaitedWorkflowExecuteEnvelope,
    submission_identity: Mapping[str, Any],
) -> tuple[WorkflowResult | None, dict[str, Any] | None, str | None]:
    workflow_data = getattr(instance, "workflow_data", None)
    if not isinstance(workflow_data, Mapping):
        return None, None, "workflow_execute_instance_context_missing"
    try:
        from ...workflows.durable.authority_snapshot_attestation import (
            validate_authority_checkpoint_attestation,
            worker_claim_supports_exact_authority_snapshot,
        )
        from ...workflows.durable.checkpoint_context_projection import (
            CHECKPOINT_CONTEXT_PROJECTION_KEY,
            CHECKPOINT_CONTEXT_PROJECTION_SCHEMA_VERSION,
        )
        from ...workflows.durable.durable_executor import (
            DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY,
        )
    except Exception:
        return None, None, "workflow_execute_executed_identity_support_unavailable"

    claimed_by_build = getattr(instance, "claimed_by_build", None)
    if not worker_claim_supports_exact_authority_snapshot(claimed_by_build):
        return None, None, "workflow_execute_worker_capability_unverified"
    claimed_worker_id = (
        _workflow_execute_snapshot_text(claimed_by_build.get("worker_id"))
        if isinstance(claimed_by_build, Mapping)
        else None
    )
    current_claim_token = _workflow_execute_snapshot_text(
        getattr(instance, "claim_token", None)
    )
    if current_claim_token is None:
        return None, None, "workflow_execute_current_claim_token_missing"
    checkpoint_attestation, attestation_error = (
        validate_authority_checkpoint_attestation(
            getattr(instance, "authority_checkpoint_attestation", None),
            instance_id=envelope.instance_id,
            workflow_id=envelope.workflow_id,
            current_state=(
                _workflow_execute_snapshot_text(
                    getattr(instance, "current_state", None)
                )
                or "completed"
            ),
            step_index=int(getattr(instance, "step_index", 0) or 0),
            workflow_data=workflow_data,
            expected_claim_token=current_claim_token,
            expected_worker_id=claimed_worker_id,
            require_exact_eligible=True,
        )
    )
    if checkpoint_attestation is None:
        return (
            None,
            None,
            attestation_error
            or "workflow_execute_authority_checkpoint_attestation_unverified",
        )

    projection = workflow_data.get(CHECKPOINT_CONTEXT_PROJECTION_KEY)
    projected_keys = (
        projection.get("projected_keys") if isinstance(projection, Mapping) else None
    )
    if (
        not isinstance(projection, Mapping)
        or projection.get("schema_version")
        != CHECKPOINT_CONTEXT_PROJECTION_SCHEMA_VERSION
        or not isinstance(projected_keys, Sequence)
        or isinstance(projected_keys, (str, bytes, bytearray))
    ):
        return None, None, "workflow_execute_checkpoint_projection_unverified"
    authority_lineage_keys = {
        _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_KEY,
        "prompt_context_diagnostics",
        DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY,
    }
    if int(projection.get("omitted_projected_key_count") or 0) > 0 or any(
        isinstance(record, Mapping)
        and str(record.get("key") or "").strip() in authority_lineage_keys
        for record in projected_keys
    ):
        return (
            None,
            None,
            "workflow_execute_authority_lineage_was_checkpoint_projected",
        )

    executed_identity = _workflow_execute_vontology_definition_identity(
        workflow_data.get(DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY),
        workflow_id=envelope.workflow_id,
    )
    if executed_identity is None or executed_identity.get(
        "definition_hash"
    ) != submission_identity.get("definition_hash"):
        return None, None, "workflow_execute_executed_identity_unverified"
    authority_output = workflow_data.get(_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_KEY)
    prompt_diagnostics = workflow_data.get("prompt_context_diagnostics")
    if not isinstance(authority_output, Mapping):
        return None, None, "workflow_execute_exact_authority_snapshot_unavailable"
    if not isinstance(prompt_diagnostics, Mapping):
        return None, None, "workflow_execute_exact_prompt_lineage_missing"
    return (
        WorkflowResult(
            data={
                _WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_KEY: dict(authority_output),
                "prompt_context_diagnostics": dict(prompt_diagnostics),
            },
            completed=True,
            final_state=(
                _workflow_execute_snapshot_text(
                    getattr(instance, "current_state", None)
                )
                or "completed"
            ),
            error=None,
        ),
        executed_identity,
        None,
    )


def _build_awaited_workflow_execute_aux_entry(
    *,
    tool_payload: Mapping[str, Any],
    tool_result_payload: Any,
    parent_data: Mapping[str, Any],
    user_namespace: str | None,
    aux_entry_builder: Callable[..., dict[str, Any]],
    instance_loader: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Project exact awaited child authority into the parent TER, fail closed."""

    envelope, reason = _verified_awaited_workflow_execute_envelope(
        tool_payload=tool_payload,
        tool_result_payload=tool_result_payload,
    )
    workflow_id = (
        envelope.workflow_id
        if envelope is not None
        else _workflow_execute_snapshot_text(tool_payload.get("workflow_id"))
    )
    instance_id = envelope.instance_id if envelope is not None else None

    def _fail(failure_reason: str) -> dict[str, Any]:
        return _workflow_execute_snapshot_failure(
            workflow_id=workflow_id,
            reason_code=failure_reason,
            instance_id=instance_id,
        )

    if envelope is None or reason:
        return _fail(reason or "workflow_execute_result_missing")
    assert isinstance(tool_result_payload, Mapping)
    submission_identity, reason = _verified_workflow_execute_submission_identity(
        tool_result_payload=tool_result_payload,
        workflow_id=envelope.workflow_id,
    )
    if submission_identity is None:
        return _fail(reason or "workflow_execute_submission_identity_unverified")
    actor_scope, reason = _verified_workflow_execute_actor_scope(
        tool_payload=tool_payload,
        parent_data=parent_data,
        user_namespace=user_namespace,
    )
    if actor_scope is None:
        return _fail(reason or "workflow_execute_parent_actor_scope_missing")

    if instance_loader is None:
        try:
            from ...workflows.durable import WorkflowInstanceManager

            instance_loader = WorkflowInstanceManager().get_instance
        except Exception:
            return _fail("workflow_execute_instance_loader_unavailable")
    try:
        instance = instance_loader(envelope.instance_id)
    except Exception:
        return _fail("workflow_execute_instance_read_failed")
    if instance is None:
        return _fail("workflow_execute_instance_not_found")
    if not _workflow_execute_instance_matches_scope(
        instance=instance,
        envelope=envelope,
        actor_scope=actor_scope,
    ):
        return _fail("workflow_execute_completed_instance_scope_mismatch")

    workflow_result, executed_identity, reason = _exact_workflow_execute_child_result(
        instance=instance,
        envelope=envelope,
        submission_identity=submission_identity,
    )
    if workflow_result is None or executed_identity is None:
        return _fail(reason or "workflow_execute_exact_authority_snapshot_unavailable")
    execution_request_id = _workflow_execute_snapshot_text(parent_data.get("turn_id"))
    if not execution_request_id:
        return _fail("workflow_execute_exact_authority_snapshot_unavailable")
    claim_provenance = _workflow_execute_claim_provenance_projection(instance)
    setattr(
        workflow_result,
        _WORKFLOW_EXECUTION_IDENTITY_ATTRIBUTE,
        {
            "schema_version": _WORKFLOW_EXECUTION_IDENTITY_SCHEMA_VERSION,
            "workflow_id": envelope.workflow_id,
            "execution_request_id": execution_request_id,
            "conversation_session_id": _workflow_execute_snapshot_text(
                parent_data.get("conversation_session_id")
            ),
            "workflow_instance_id": envelope.instance_id,
            "episode_source": "workflow_execute_tool",
            "workflow_definition_identity": dict(executed_identity),
            "durable_claim_provenance": claim_provenance,
        },
    )
    entry = aux_entry_builder(
        workflow_id=envelope.workflow_id,
        workflow_result=workflow_result,
    )
    result_snapshot = entry.get("result_snapshot")
    metadata = (
        result_snapshot.get(_WORKFLOW_AUTHORITY_OUTPUT_SNAPSHOT_METADATA_KEY)
        if isinstance(result_snapshot, Mapping)
        else None
    )
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("exact") is not True
        or metadata.get("prompt_lineage_observed") is not True
        or metadata.get("prompt_lineage_ambiguous") is not False
    ):
        return _fail("workflow_execute_exact_authority_snapshot_unavailable")
    context_fields_sha256 = metadata.get("llm_context_fields_sha256")
    if (
        metadata.get("llm_context_fields_lineage_observed") is not True
        or metadata.get("llm_context_fields_lineage_ambiguous") is not False
        or not isinstance(context_fields_sha256, str)
        or len(context_fields_sha256) != 64
        or any(
            character not in "0123456789abcdef" for character in context_fields_sha256
        )
    ):
        return _fail("workflow_execute_exact_llm_context_fields_lineage_unavailable")
    entry.update(
        {
            "source": "workflow_execute_tool",
            "instance_id": envelope.instance_id,
            "workflow_instance_id": envelope.instance_id,
            "execution_trace_id": _workflow_execute_snapshot_text(
                getattr(instance, "execution_trace_id", None)
            ),
            "workflow_definition_identity": dict(executed_identity),
            "durable_claim_provenance": claim_provenance,
        }
    )
    return entry
