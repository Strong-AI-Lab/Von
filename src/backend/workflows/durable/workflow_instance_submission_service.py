"""Canonical durable workflow instance submission with runnable verification.

This module is the single authoritative pathway for creating durable workflow
instances from user-facing surfaces (MCP tools, REST routes, scheduler
triggers). It prevents optimistic "started/running" claims when a workflow is
not actually runnable in the current process.

JVNAUTOSCI-1106:
- Require preflight runnable verification before instance creation.
- Re-check runnability post-create before returning success.
- Return structured verification telemetry for conceptual/executable/runnable
  states so callers can present accurate status.

JVNAUTOSCI-1308:
- Add bounded runnable-verification caching keyed by definition identity +
  feature/runtime signatures.
- Keep safety checks fail-closed when cache/check state is ambiguous.
- Surface low-overhead timing telemetry for launch-time verification paths.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import json
import os
import threading
from time import monotonic, perf_counter
from typing import Any, Callable, Dict, List, Mapping, Sequence

from ...services.feature_flags import (
    get_durable_workflows_enabled,
    get_event_workflow_integration_enabled,
)
from ...services.namespace_service import resolve_canonical_namespace
from ..engine import WorkflowDefinition
from ..mcp_tool_bridge import candidate_internal_mcp_tool_names
from ..vontology_loader import (
    build_workflow_process_graph,
    build_workflow_process_graph_from_definition,
    detect_vacuous_workflow_steps,
    load_workflow_definition_from_vontology,
)
from ..workflow_definition_identity_service import (
    build_workflow_definition_identity,
    collect_workflow_action_ids,
    validate_workflow_definition_contract,
)
from ..workflow_launch_input_contracts import (
    WorkflowLaunchInputResolution,
    resolve_workflow_launch_inputs,
)
from .instance_manager import WorkflowInstanceManager

_RUNNABLE_CACHE_TTL_ENV = "VON_WORKFLOW_RUNNABLE_CACHE_TTL_SECONDS"
_RUNNABLE_CACHE_MAX_ENTRIES_ENV = "VON_WORKFLOW_RUNNABLE_CACHE_MAX_ENTRIES"
_DEFAULT_RUNNABLE_CACHE_TTL_SECONDS = 45.0
_DEFAULT_RUNNABLE_CACHE_MAX_ENTRIES = 256
_MAX_RUNNABLE_CACHE_ENTRIES = 2048
_MIN_RUNNABLE_CACHE_ENTRIES = 8


@dataclass(frozen=True)
class WorkflowRunnableVerification:
    workflow_id: str
    conceptual_representation_success: bool
    executable_registration_success: bool
    runnable_verification_success: bool
    fallback_action_routing_enabled: bool
    discovered_action_ids: tuple[str, ...]
    unsupported_action_ids: tuple[str, ...]
    integrity_issues: tuple[Dict[str, Any], ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    definition_identity: Mapping[str, Any] | None = None
    contract_validation: Mapping[str, Any] | None = None
    verification_telemetry: Mapping[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "workflow_id": self.workflow_id,
            "conceptual_representation_success": self.conceptual_representation_success,
            "executable_registration_success": self.executable_registration_success,
            "runnable_verification_success": self.runnable_verification_success,
            "fallback_action_routing_enabled": self.fallback_action_routing_enabled,
            "discovered_action_ids": list(self.discovered_action_ids),
            "unsupported_action_ids": list(self.unsupported_action_ids),
            "integrity_issues": [dict(item) for item in self.integrity_issues],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }
        if isinstance(self.definition_identity, Mapping):
            payload["definition_identity"] = dict(self.definition_identity)
        if isinstance(self.contract_validation, Mapping):
            payload["contract_validation"] = dict(self.contract_validation)
        if isinstance(self.verification_telemetry, Mapping):
            payload["verification_telemetry"] = dict(self.verification_telemetry)
        return payload


@dataclass(frozen=True)
class WorkflowInstanceSubmissionResult:
    success: bool
    workflow_id: str
    status: str
    instance_id: str | None
    verification: Mapping[str, Any]
    created_new: bool | None = None
    error_code: str | None = None
    error: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "success": self.success,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "verification": dict(self.verification),
        }
        if isinstance(self.instance_id, str) and self.instance_id:
            payload["instance_id"] = self.instance_id
        if isinstance(self.created_new, bool):
            payload["created_new"] = self.created_new
        if isinstance(self.error_code, str) and self.error_code:
            payload["error_code"] = self.error_code
        if isinstance(self.error, str) and self.error:
            payload["error"] = self.error
        return payload


def build_verified_instance_launch_payload(
    submission: WorkflowInstanceSubmissionResult,
    *,
    workflow_inputs: Mapping[str, Any] | None = None,
    launch_mode: str = "durable_instance",
) -> Dict[str, Any]:
    """Add stable workflow-execution payloads without direct instance launches.

    Callers outside the durable submission layer should not need to construct
    their own instance-launch envelopes, because that encourages bypassing the
    verified submission pathway and regressing workflow-purity invariants.
    """

    payload = submission.to_dict()
    instance_id = (
        submission.instance_id
        if isinstance(submission.instance_id, str) and submission.instance_id.strip()
        else None
    )
    if not submission.success or not instance_id:
        return payload

    payload["workflow_execution"] = {
        "workflow_id": submission.workflow_id,
        "instance_id": instance_id,
        "launch_mode": str(launch_mode or "durable_instance").strip()
        or "durable_instance",
        "workflow_inputs": dict(workflow_inputs or {}),
    }
    return payload


@dataclass(frozen=True)
class _RunnableVerificationCacheEntry:
    cache_key: str
    workflow_id: str
    definition_hash: str
    created_at_monotonic: float
    expires_at_monotonic: float
    verification: WorkflowRunnableVerification


_RUNNABLE_CACHE_LOCK = threading.RLock()
_RUNNABLE_CACHE: dict[str, _RunnableVerificationCacheEntry] = {}
_RUNNABLE_CACHE_GENERATION = 0
_RUNNABLE_LAST_FEATURE_SIGNATURE_HASH: str | None = None


def _normalise_warning_items(items: Sequence[Any] | None) -> List[str]:
    return [
        str(item).strip()
        for item in (items or [])
        if isinstance(item, str) and str(item).strip()
    ]


def _read_float_env(
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _read_int_env(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _read_runnable_cache_ttl_seconds() -> float:
    return _read_float_env(
        _RUNNABLE_CACHE_TTL_ENV,
        default=_DEFAULT_RUNNABLE_CACHE_TTL_SECONDS,
        minimum=0.0,
        maximum=3600.0,
    )


def _read_runnable_cache_max_entries() -> int:
    return _read_int_env(
        _RUNNABLE_CACHE_MAX_ENTRIES_ENV,
        default=_DEFAULT_RUNNABLE_CACHE_MAX_ENTRIES,
        minimum=_MIN_RUNNABLE_CACHE_ENTRIES,
        maximum=_MAX_RUNNABLE_CACHE_ENTRIES,
    )


def _stable_json_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _truthy_env(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalised = str(raw).strip().lower()
    if normalised in {"1", "true", "yes", "on", "y"}:
        return True
    if normalised in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _build_runnable_feature_signature() -> dict[str, Any]:
    return {
        "durable_workflows_enabled": get_durable_workflows_enabled(default=False),
        "event_workflow_integration_enabled": get_event_workflow_integration_enabled(
            default=True
        ),
        "internal_mcp_enabled": _truthy_env("VON_INTERNAL_MCP_ENABLE", default=False),
    }


def _resolve_registered_workflow_runtime(
    workflow_id: str,
) -> tuple[Any, WorkflowDefinition | None, str, tuple[str, ...]]:
    from .registry_factory import (
        get_shared_workflow_registry_read_only,
        register_workflow_from_vontology,
    )

    registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
    definition = registry.get(workflow_id)
    if definition is None:
        try:
            registered, _error_code = register_workflow_from_vontology(
                registry=registry,
                workflow_id=workflow_id,
            )
            if registered:
                definition = registry.get(workflow_id)
        except Exception:
            # Keep submission/verification fail-closed. Callers handle a missing
            # definition explicitly after the shared lookup path completes.
            definition = None
    registration = getattr(registry, "get_registration", lambda _wid: None)(workflow_id)
    registration_source = (
        str(getattr(registration, "source", "") or "").strip()
        if registration
        else "unknown"
    )
    known_workflow_ids = tuple(
        sorted(
            {
                str(item).strip()
                for item in getattr(registry, "all_workflow_ids", lambda: [])()
                if isinstance(item, str) and str(item).strip()
            }
        )
    )
    return registry, definition, registration_source, known_workflow_ids


def _resolve_submission_launch_inputs(
    *,
    workflow_id: str,
    inputs: Mapping[str, Any],
) -> tuple[WorkflowDefinition | None, WorkflowLaunchInputResolution]:
    workflow_definition: WorkflowDefinition | None = None
    try:
        _registry, workflow_definition, _source, _known_workflow_ids = (
            _resolve_registered_workflow_runtime(workflow_id)
        )
    except Exception:
        workflow_definition = None

    if workflow_definition is None:
        diagnostics = {
            "schema_version": "workflow_launch_input_resolution.v1",
            "workflow_id": str(workflow_id or "").strip(),
            "status": "definition_unavailable",
            "contract_source": None,
            "required_inputs": [],
            "resolved_inputs": [],
            "unresolved_required_inputs": [],
            "unresolved_optional_inputs": [],
            "mappings": [],
        }
        return workflow_definition, WorkflowLaunchInputResolution(
            resolved_inputs={},
            unresolved_required_inputs=(),
            unresolved_optional_inputs=(),
            diagnostics=diagnostics,
        )

    workflow_metadata = getattr(workflow_definition, "metadata", None)
    launch_contract = (
        workflow_metadata.get("launch_input_contract")
        if isinstance(workflow_metadata, Mapping)
        else None
    )
    launch_contract_source = (
        workflow_metadata.get("launch_input_contract_source")
        if isinstance(workflow_metadata, Mapping)
        else None
    )
    resolution = resolve_workflow_launch_inputs(
        workflow_id=workflow_id,
        contract=launch_contract if isinstance(launch_contract, Mapping) else None,
        inputs=inputs,
        contract_source=(
            str(launch_contract_source).strip()
            if isinstance(launch_contract_source, str) and launch_contract_source.strip()
            else None
        ),
    )
    return workflow_definition, resolution


def _with_verification_telemetry(
    verification: WorkflowRunnableVerification,
    telemetry: Mapping[str, Any],
) -> WorkflowRunnableVerification:
    return replace(verification, verification_telemetry=dict(telemetry))


def _current_runnable_cache_generation() -> int:
    with _RUNNABLE_CACHE_LOCK:
        return int(_RUNNABLE_CACHE_GENERATION)


def _increment_runnable_cache_generation_locked() -> int:
    global _RUNNABLE_CACHE_GENERATION
    _RUNNABLE_CACHE_GENERATION += 1
    return _RUNNABLE_CACHE_GENERATION


def _prune_runnable_cache_locked(now_monotonic: float, *, max_entries: int) -> None:
    expired_keys = [
        key
        for key, entry in _RUNNABLE_CACHE.items()
        if entry.expires_at_monotonic <= now_monotonic
    ]
    for key in expired_keys:
        _RUNNABLE_CACHE.pop(key, None)

    overflow = len(_RUNNABLE_CACHE) - max_entries
    if overflow <= 0:
        return

    oldest_entries = sorted(
        _RUNNABLE_CACHE.values(),
        key=lambda item: item.created_at_monotonic,
    )[:overflow]
    for entry in oldest_entries:
        _RUNNABLE_CACHE.pop(entry.cache_key, None)


def invalidate_workflow_runnable_verification_cache(
    *,
    reason: str = "unspecified",
    workflow_id: str | None = None,
) -> dict[str, Any]:
    """Invalidate cached runnable verification entries.

    ``workflow_id`` narrows invalidation to one workflow. Omit it to clear all.
    """

    workflow_id_clean = str(workflow_id or "").strip() or None
    reason_clean = str(reason or "").strip() or "unspecified"

    with _RUNNABLE_CACHE_LOCK:
        previous_size = len(_RUNNABLE_CACHE)
        if workflow_id_clean:
            keys_to_remove = [
                key
                for key, entry in _RUNNABLE_CACHE.items()
                if entry.workflow_id == workflow_id_clean
            ]
            for key in keys_to_remove:
                _RUNNABLE_CACHE.pop(key, None)
            removed_count = len(keys_to_remove)
        else:
            _RUNNABLE_CACHE.clear()
            removed_count = previous_size
        generation = _increment_runnable_cache_generation_locked()
        cache_size_after = len(_RUNNABLE_CACHE)

    return {
        "success": True,
        "reason": reason_clean,
        "workflow_id": workflow_id_clean,
        "removed_count": removed_count,
        "cache_size_after": cache_size_after,
        "cache_generation": generation,
    }


def _invalidate_cache_if_feature_signature_changed(
    feature_signature: Mapping[str, Any],
) -> None:
    signature_hash = _stable_json_hash(dict(feature_signature))
    with _RUNNABLE_CACHE_LOCK:
        global _RUNNABLE_LAST_FEATURE_SIGNATURE_HASH
        if _RUNNABLE_LAST_FEATURE_SIGNATURE_HASH is None:
            _RUNNABLE_LAST_FEATURE_SIGNATURE_HASH = signature_hash
            return
        if _RUNNABLE_LAST_FEATURE_SIGNATURE_HASH == signature_hash:
            return
        _RUNNABLE_CACHE.clear()
        _increment_runnable_cache_generation_locked()
        _RUNNABLE_LAST_FEATURE_SIGNATURE_HASH = signature_hash


def _evict_stale_workflow_entries(
    *,
    workflow_id: str,
    definition_hash: str,
) -> int:
    if not workflow_id or not definition_hash:
        return 0
    with _RUNNABLE_CACHE_LOCK:
        stale_keys = [
            key
            for key, entry in _RUNNABLE_CACHE.items()
            if entry.workflow_id == workflow_id and entry.definition_hash != definition_hash
        ]
        for key in stale_keys:
            _RUNNABLE_CACHE.pop(key, None)
        return len(stale_keys)


def _read_cached_runnable_verification(
    *,
    cache_key: str,
    now_monotonic: float,
) -> WorkflowRunnableVerification | None:
    if not cache_key:
        return None
    with _RUNNABLE_CACHE_LOCK:
        entry = _RUNNABLE_CACHE.get(cache_key)
        if entry is None:
            return None
        if entry.expires_at_monotonic <= now_monotonic:
            _RUNNABLE_CACHE.pop(cache_key, None)
            return None
        return entry.verification


def _write_cached_runnable_verification(
    *,
    cache_key: str,
    workflow_id: str,
    definition_hash: str,
    verification: WorkflowRunnableVerification,
    now_monotonic: float,
    ttl_seconds: float,
) -> None:
    if not cache_key:
        return
    if ttl_seconds <= 0.0:
        return

    max_entries = _read_runnable_cache_max_entries()
    with _RUNNABLE_CACHE_LOCK:
        _prune_runnable_cache_locked(now_monotonic, max_entries=max_entries)
        _RUNNABLE_CACHE[cache_key] = _RunnableVerificationCacheEntry(
            cache_key=cache_key,
            workflow_id=workflow_id,
            definition_hash=definition_hash,
            created_at_monotonic=now_monotonic,
            expires_at_monotonic=now_monotonic + ttl_seconds,
            verification=verification,
        )
        _prune_runnable_cache_locked(now_monotonic, max_entries=max_entries)


def _build_runnable_cache_key(
    *,
    workflow_id: str,
    definition_identity: Mapping[str, Any] | None,
    fallback_enabled: bool,
    fallback_tool_names: Sequence[str],
    feature_signature: Mapping[str, Any],
) -> tuple[str | None, str]:
    definition_hash = ""
    if isinstance(definition_identity, Mapping):
        definition_hash = str(definition_identity.get("definition_hash") or "").strip()
    if not definition_hash:
        definition_hash = "__missing_definition__"

    payload = {
        "schema_version": "workflow_runnable_cache_key.v1",
        "workflow_id": workflow_id,
        "definition_hash": definition_hash,
        "fallback_enabled": bool(fallback_enabled),
        "fallback_tool_hash": _stable_json_hash(
            {"tools": sorted(str(name) for name in fallback_tool_names if str(name))}
        ),
        "feature_signature": dict(feature_signature),
        "cache_generation": _current_runnable_cache_generation(),
    }
    return _stable_json_hash(payload), definition_hash


@lru_cache(maxsize=1)
def _internal_mcp_method_names() -> frozenset[str]:
    """Return available internal MCP tool names (best effort, cached)."""

    try:
        from ...integrations.internal_mcp.catalogue import build_default_catalogue

        return frozenset(build_default_catalogue().list_methods())
    except Exception:
        return frozenset()


def _build_fail_closed_verification(
    *,
    workflow_id: str,
    error_code: str,
    additional_errors: Sequence[str] | None = None,
    warnings: Sequence[str] | None = None,
    definition_identity: Mapping[str, Any] | None = None,
    telemetry: Mapping[str, Any] | None = None,
) -> WorkflowRunnableVerification:
    errors: list[str] = [error_code]
    for item in additional_errors or ():
        text = str(item or "").strip()
        if text and text not in errors:
            errors.append(text)

    warning_items = _normalise_warning_items(list(warnings or ()))
    contract_errors = list(errors)

    return WorkflowRunnableVerification(
        workflow_id=workflow_id,
        conceptual_representation_success=False,
        executable_registration_success=False,
        runnable_verification_success=False,
        fallback_action_routing_enabled=False,
        discovered_action_ids=(),
        unsupported_action_ids=(),
        integrity_issues=(),
        warnings=tuple(warning_items),
        errors=tuple(contract_errors),
        definition_identity=definition_identity,
        contract_validation={"valid": False, "errors": list(contract_errors)},
        verification_telemetry=dict(telemetry or {}),
    )


def _verify_workflow_runnable_uncached(
    *,
    workflow_id: str,
    definition: WorkflowDefinition | None,
    registration_source: str,
    fallback_enabled: bool,
    fallback_tool_names: Sequence[str],
    action_registry: Any,
    known_workflow_ids: Sequence[str],
    workflow_definition_loader: Callable[[str], WorkflowDefinition | None] | None,
    cache_generation: int,
    feature_signature: Mapping[str, Any],
) -> WorkflowRunnableVerification:
    uncached_started = perf_counter()
    stage_timings_ms: dict[str, float] = {}

    graph_started = perf_counter()
    graph: Mapping[str, Any] | None = None
    graph_warnings: Sequence[Any] = ()
    graph_error: str | None = None
    try:
        if definition is not None:
            graph = build_workflow_process_graph_from_definition(definition)
            graph_warnings = ()
        else:
            graph, graph_warnings = build_workflow_process_graph(workflow_id)
    except Exception as exc:  # pragma: no cover - defensive
        graph_error = f"workflow_graph_build_failed:{type(exc).__name__}"
        graph = None
        graph_warnings = (graph_error,)
    stage_timings_ms["graph_build_ms"] = round(
        (perf_counter() - graph_started) * 1000.0,
        3,
    )
    warnings = _normalise_warning_items(graph_warnings)
    conceptual_representation_success = isinstance(graph, Mapping)

    authoritative_started = perf_counter()
    authoritative_definition = (
        definition if str(registration_source or "").strip().lower() == "vontology" else None
    )
    stage_timings_ms["authoritative_definition_load_ms"] = round(
        (perf_counter() - authoritative_started) * 1000.0,
        3,
    )

    identity_started = perf_counter()
    definition_identity = build_workflow_definition_identity(
        workflow_id=workflow_id,
        source=registration_source or "unknown",
        definition=definition,
        authoritative_definition=authoritative_definition,
    )
    stage_timings_ms["definition_identity_ms"] = round(
        (perf_counter() - identity_started) * 1000.0,
        3,
    )
    executable_registration_success = definition is not None

    if definition is None:
        error_items = ("workflow_definition_not_registered",)
        if graph_error and graph_error not in error_items:
            error_items = (*error_items, graph_error)
        return WorkflowRunnableVerification(
            workflow_id=workflow_id,
            conceptual_representation_success=conceptual_representation_success,
            executable_registration_success=False,
            runnable_verification_success=False,
            fallback_action_routing_enabled=False,
            discovered_action_ids=(),
            unsupported_action_ids=(),
            integrity_issues=(),
            warnings=tuple(warnings),
            errors=error_items,
            definition_identity=definition_identity,
            contract_validation={"valid": False, "errors": list(error_items)},
            verification_telemetry={
                "cache_hit": False,
                "cache_generation": cache_generation,
                "feature_signature": dict(feature_signature),
                "timings_ms": {
                    **stage_timings_ms,
                    "uncached_total_ms": round(
                        (perf_counter() - uncached_started) * 1000.0,
                        3,
                    ),
                },
            },
        )

    integrity_started = perf_counter()
    integrity_issues = (
        tuple(detect_vacuous_workflow_steps(workflow_id=workflow_id, graph=graph))
        if isinstance(graph, Mapping)
        else ()
    )
    stage_timings_ms["integrity_check_ms"] = round(
        (perf_counter() - integrity_started) * 1000.0,
        3,
    )
    if integrity_issues:
        warnings.append("workflow_step_contract_integrity_issue")

    action_ids = collect_workflow_action_ids(definition)
    supported_actions: set[str] = set()

    if fallback_enabled and not fallback_tool_names:
        warnings.append("internal_tool_catalogue_unavailable")

    for action_id in action_ids:
        if action_registry.has(action_id):
            supported_actions.add(action_id)
            continue
        if fallback_enabled and (
            not fallback_tool_names
            or any(
                candidate in fallback_tool_names
                for candidate in candidate_internal_mcp_tool_names(action_id)
            )
        ):
            supported_actions.add(action_id)

    contract_started = perf_counter()
    contract_validation = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids=supported_actions,
        enforce_supported_actions=True,
        known_workflow_ids=known_workflow_ids,
        workflow_definition_loader=workflow_definition_loader,
    )
    stage_timings_ms["contract_validation_ms"] = round(
        (perf_counter() - contract_started) * 1000.0,
        3,
    )

    unsupported = [
        item
        for item in contract_validation.get("unsupported_action_ids", [])
        if isinstance(item, str) and item.strip()
    ]
    errors = [
        code
        for code in contract_validation.get("errors", [])
        if isinstance(code, str) and code.strip()
    ]
    if integrity_issues and "workflow_step_contract_integrity_issue" not in errors:
        errors.append("workflow_step_contract_integrity_issue")
    if graph_error and graph_error not in errors:
        errors.append(graph_error)

    return WorkflowRunnableVerification(
        workflow_id=workflow_id,
        conceptual_representation_success=conceptual_representation_success,
        executable_registration_success=executable_registration_success,
        runnable_verification_success=len(errors) == 0,
        fallback_action_routing_enabled=fallback_enabled,
        discovered_action_ids=action_ids,
        unsupported_action_ids=tuple(sorted(set(unsupported))),
        integrity_issues=integrity_issues,
        warnings=tuple(warnings),
        errors=tuple(errors),
        definition_identity=definition_identity,
        contract_validation=contract_validation,
        verification_telemetry={
            "cache_hit": False,
            "cache_generation": cache_generation,
            "feature_signature": dict(feature_signature),
            "timings_ms": {
                **stage_timings_ms,
                "uncached_total_ms": round(
                    (perf_counter() - uncached_started) * 1000.0,
                    3,
                ),
            },
        },
    )


def verify_workflow_runnable(workflow_id: str) -> WorkflowRunnableVerification:
    """Evaluate whether a workflow is runnable in the current runtime context."""

    verify_started = perf_counter()
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return _build_fail_closed_verification(
            workflow_id="",
            error_code="invalid_workflow_id",
            telemetry={
                "cache_hit": False,
                "reason": "invalid_workflow_id",
                "timings_ms": {
                    "total_ms": round((perf_counter() - verify_started) * 1000.0, 3),
                },
            },
        )

    try:
        feature_signature = _build_runnable_feature_signature()
        _invalidate_cache_if_feature_signature_changed(feature_signature)

        prep_started = perf_counter()
        from .registry_factory import get_shared_durable_action_registry

        # Reuse the startup/shared registry rather than rebuilding the lazy
        # registry graph on every launch verification. This keeps verified
        # submission aligned with the authoritative runtime registry that the
        # worker itself will use once the instance starts executing.
        registry, definition, registration_source, known_workflow_ids = (
            _resolve_registered_workflow_runtime(workflow_id)
        )

        def _resolve_workflow_definition_for_validation(
            candidate_workflow_id: str,
        ) -> WorkflowDefinition | None:
            candidate_id = str(candidate_workflow_id or "").strip()
            if not candidate_id:
                return None
            try:
                registered_definition = registry.get(candidate_id)
                if registered_definition is not None:
                    return registered_definition
            except Exception:
                # Keep validation conservative when registry lookups fail.
                pass
            try:
                return load_workflow_definition_from_vontology(candidate_id)
            except Exception:
                return None

        definition_identity = build_workflow_definition_identity(
            workflow_id=workflow_id,
            source=registration_source or "unknown",
            definition=definition,
            authoritative_definition=None,
        )

        action_registry = get_shared_durable_action_registry()
        fallback_enabled = action_registry.has_fallback_handler()
        fallback_tool_names = (
            _internal_mcp_method_names() if fallback_enabled else frozenset()
        )
        prep_ms = round((perf_counter() - prep_started) * 1000.0, 3)

        cache_lookup_started = perf_counter()
        cache_key, definition_hash = _build_runnable_cache_key(
            workflow_id=workflow_id,
            definition_identity=definition_identity,
            fallback_enabled=fallback_enabled,
            fallback_tool_names=tuple(sorted(fallback_tool_names)),
            feature_signature=feature_signature,
        )
        stale_evicted = _evict_stale_workflow_entries(
            workflow_id=workflow_id,
            definition_hash=definition_hash,
        )
        now_monotonic = monotonic()
        cached = _read_cached_runnable_verification(
            cache_key=cache_key or "",
            now_monotonic=now_monotonic,
        )
        cache_lookup_ms = round((perf_counter() - cache_lookup_started) * 1000.0, 3)
        cache_generation = _current_runnable_cache_generation()

        if cached is not None:
            existing_telemetry = (
                dict(cached.verification_telemetry)
                if isinstance(cached.verification_telemetry, Mapping)
                else {}
            )
            merged_telemetry = {
                **existing_telemetry,
                "cache_hit": True,
                "cache_generation": cache_generation,
                "cache_stale_entries_evicted": stale_evicted,
                "feature_signature": dict(feature_signature),
                "timings_ms": {
                    **dict(existing_telemetry.get("timings_ms") or {}),
                    "cache_prepare_ms": prep_ms,
                    "cache_lookup_ms": cache_lookup_ms,
                    "total_ms": round((perf_counter() - verify_started) * 1000.0, 3),
                },
            }
            return _with_verification_telemetry(cached, merged_telemetry)

        verification = _verify_workflow_runnable_uncached(
            workflow_id=workflow_id,
            definition=definition,
            registration_source=registration_source,
            fallback_enabled=fallback_enabled,
            fallback_tool_names=tuple(sorted(fallback_tool_names)),
            action_registry=action_registry,
            known_workflow_ids=known_workflow_ids,
            workflow_definition_loader=_resolve_workflow_definition_for_validation,
            cache_generation=cache_generation,
            feature_signature=feature_signature,
        )

        telemetry = (
            dict(verification.verification_telemetry)
            if isinstance(verification.verification_telemetry, Mapping)
            else {}
        )
        merged_telemetry = {
            **telemetry,
            "cache_hit": False,
            "cache_generation": cache_generation,
            "cache_stale_entries_evicted": stale_evicted,
            "feature_signature": dict(feature_signature),
            "timings_ms": {
                **dict(telemetry.get("timings_ms") or {}),
                "cache_prepare_ms": prep_ms,
                "cache_lookup_ms": cache_lookup_ms,
                "total_ms": round((perf_counter() - verify_started) * 1000.0, 3),
            },
        }
        verification_with_telemetry = _with_verification_telemetry(
            verification,
            merged_telemetry,
        )

        ttl_seconds = _read_runnable_cache_ttl_seconds()
        _write_cached_runnable_verification(
            cache_key=cache_key or "",
            workflow_id=workflow_id,
            definition_hash=definition_hash,
            verification=verification_with_telemetry,
            now_monotonic=now_monotonic,
            ttl_seconds=ttl_seconds,
        )
        return verification_with_telemetry
    except Exception as exc:  # pragma: no cover - defensive
        return _build_fail_closed_verification(
            workflow_id=workflow_id,
            error_code="workflow_runnable_check_failed",
            additional_errors=(f"{type(exc).__name__}",),
            telemetry={
                "cache_hit": False,
                "reason": "verification_exception",
                "exception_type": type(exc).__name__,
                "exception": str(exc),
                "timings_ms": {
                    "total_ms": round((perf_counter() - verify_started) * 1000.0, 3),
                },
            },
        )


def _build_submission_verification_payload(
    *,
    preflight: WorkflowRunnableVerification,
    postflight: WorkflowRunnableVerification | None,
) -> Dict[str, Any]:
    postflight_payload = postflight.to_dict() if postflight is not None else None
    postflight_passed = bool(
        postflight is not None and postflight.runnable_verification_success
    )

    return {
        "workflow_id": preflight.workflow_id,
        "conceptual_representation_success": preflight.conceptual_representation_success,
        "executable_registration_success": preflight.executable_registration_success,
        "runnable_verification_success": (
            postflight.runnable_verification_success
            if postflight is not None
            else preflight.runnable_verification_success
        ),
        "preflight_passed": preflight.runnable_verification_success,
        "postflight_passed": postflight_passed,
        "preflight": preflight.to_dict(),
        "postflight": postflight_payload,
    }


def submit_verified_workflow_instance(
    *,
    manager: WorkflowInstanceManager,
    workflow_id: str,
    user_id: str,
    org_id: str,
    namespace: str,
    inputs: Mapping[str, Any] | None = None,
    schedule_id: str | None = None,
    max_retries: int = 3,
    source_event_type: str | None = None,
    source_event_id: str | None = None,
    event_idempotency_key: str | None = None,
) -> WorkflowInstanceSubmissionResult:
    """Create a durable workflow instance only when runnable verification passes."""

    workflow_id = str(workflow_id or "").strip()
    inputs_payload = dict(inputs or {})
    canonical_namespace = resolve_canonical_namespace(namespace, user_id, org_id)
    if not canonical_namespace:
        return WorkflowInstanceSubmissionResult(
            success=False,
            workflow_id=workflow_id,
            status="rejected_preflight",
            instance_id=None,
            error_code="invalid_namespace",
            error=(
                "Workflow instance namespace could not be resolved to canonical "
                "Vontology form."
            ),
            verification={
                "runnable_verification_success": False,
                "preflight_passed": False,
                "postflight_passed": False,
                "preflight": {
                    "workflow_id": workflow_id,
                    "runnable_verification_success": False,
                    "errors": ["invalid_namespace"],
                    "warnings": [],
                },
                "postflight": None,
                "namespace_resolution_error": "invalid_namespace",
            },
            created_new=None,
        )
    preflight = verify_workflow_runnable(workflow_id)
    verification_payload = _build_submission_verification_payload(
        preflight=preflight,
        postflight=None,
    )

    if not preflight.runnable_verification_success:
        return WorkflowInstanceSubmissionResult(
            success=False,
            workflow_id=workflow_id,
            status="rejected_preflight",
            instance_id=None,
            error_code="workflow_not_runnable",
            error=(
                f"Workflow '{workflow_id}' is not runnable; "
                "instance was not created."
            ),
            verification=verification_payload,
            created_new=None,
        )

    workflow_definition, launch_resolution = _resolve_submission_launch_inputs(
        workflow_id=workflow_id,
        inputs=inputs_payload,
    )
    launch_diagnostics = dict(launch_resolution.diagnostics)
    verification_payload["workflow_launch_input_resolution"] = launch_diagnostics
    inputs_payload["workflow_launch_input_resolution"] = dict(launch_diagnostics)

    if workflow_definition is None:
        return WorkflowInstanceSubmissionResult(
            success=False,
            workflow_id=workflow_id,
            status="rejected_launch_input_contract",
            instance_id=None,
            error_code="workflow_launch_input_resolution_failed",
            error=(
                f"Workflow '{workflow_id}' could not resolve launch inputs because "
                "its executable definition was unavailable."
            ),
            verification=verification_payload,
            created_new=None,
        )

    for key, value in launch_resolution.resolved_inputs.items():
        inputs_payload.setdefault(key, value)

    unresolved_required_inputs = tuple(
        item
        for item in launch_resolution.unresolved_required_inputs
        if isinstance(item, str) and item.strip()
    )
    if unresolved_required_inputs:
        return WorkflowInstanceSubmissionResult(
            success=False,
            workflow_id=workflow_id,
            status="rejected_launch_input_contract",
            instance_id=None,
            error_code="workflow_launch_input_resolution_failed",
            error=(
                f"Workflow '{workflow_id}' could not start because required launch "
                "inputs were unresolved: "
                + ", ".join(unresolved_required_inputs)
                + "."
            ),
            verification=verification_payload,
            created_new=None,
        )

    use_event_idempotency_submission = all(
        isinstance(value, str) and value.strip()
        for value in (
            source_event_type,
            source_event_id,
            event_idempotency_key,
        )
    )
    created_new = True
    if use_event_idempotency_submission:
        instance_id, created_new = manager.create_instance_for_event(
            workflow_id=workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=canonical_namespace,
            event_idempotency_key=str(event_idempotency_key).strip(),
            source_event_type=str(source_event_type).strip(),
            source_event_id=str(source_event_id).strip(),
            inputs=inputs_payload,
            schedule_id=schedule_id,
            max_retries=max_retries,
        )
    else:
        instance_id = manager.create_instance(
            workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=canonical_namespace,
            inputs=inputs_payload,
            schedule_id=schedule_id,
            max_retries=max_retries,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            event_idempotency_key=event_idempotency_key,
        )

    # Idempotent event reuse should not retroactively fail a previously created
    # instance if the workflow definition drifts after the original launch.
    postflight = preflight if not created_new else verify_workflow_runnable(workflow_id)
    verification_payload = _build_submission_verification_payload(
        preflight=preflight,
        postflight=postflight,
    )
    verification_payload["workflow_launch_input_resolution"] = launch_diagnostics
    if created_new and not postflight.runnable_verification_success:
        manager.mark_failed(
            instance_id,
            error=f"workflow_postflight_not_runnable:{workflow_id}",
            increment_retry=False,
        )
        return WorkflowInstanceSubmissionResult(
            success=False,
            workflow_id=workflow_id,
            status="rejected_postflight",
            instance_id=instance_id,
            error_code="workflow_not_runnable_postflight",
            error=(
                f"Workflow '{workflow_id}' failed postflight runnability check; "
                "instance marked failed."
            ),
            verification=verification_payload,
            created_new=True,
        )

    return WorkflowInstanceSubmissionResult(
        success=True,
        workflow_id=workflow_id,
        status="pending" if created_new else "reused",
        instance_id=instance_id,
        verification=verification_payload,
        created_new=created_new,
    )


__all__ = [
    "build_verified_instance_launch_payload",
    "WorkflowRunnableVerification",
    "WorkflowInstanceSubmissionResult",
    "verify_workflow_runnable",
    "submit_verified_workflow_instance",
    "invalidate_workflow_runnable_verification_cache",
]
