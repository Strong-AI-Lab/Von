"""Durable workflow support for workflow-aware model selection."""

from __future__ import annotations

from typing import Any, Mapping, cast

from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)


WORKFLOW_MODEL_SELECTION_ACTION_ID = "workflow_model.select_stage_model"
WORKFLOW_MODEL_SELECTION_ACTION_CONCEPT_ID = "#V#workflow_model_select_stage_model_action"
WORKFLOW_MODEL_SELECTION_SCHEMA_VERSION = "workflow_model_selection.v1"


class _NoopGateway:
    enabled = False

    def describe_methods(self) -> dict[str, Any]:
        return {}


def _context_string(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _context_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _first_context_string(
    request: WorkflowActionRequest,
    *keys: str,
    include_inputs: bool = True,
    include_data: bool = True,
) -> str | None:
    for key in keys:
        if include_inputs:
            value = _context_string(request.inputs.get(key))
            if value:
                return value
        if include_data:
            value = _context_string(request.data.get(key))
            if value:
                return value
    return None


def _resolve_stage(request: WorkflowActionRequest) -> str:
    stage = _first_context_string(
        request,
        "policy_stage",
        "stage",
        "workflow_stage_id",
        "runtime_stage",
    )
    if stage:
        return stage
    workflow_state_id = _context_string(request.workflow_state_id)
    if workflow_state_id:
        return workflow_state_id
    return "llm_step"


def _resolve_workflow_id(request: WorkflowActionRequest) -> str | None:
    return (
        _first_context_string(request, "workflow_id", "selected_workflow_id")
        or _context_string(request.workflow_id)
        or _context_string(request.data.get("workflow_id"))
        or _context_string(request.data.get("selected_workflow_id"))
    )


def _resolve_user_context_ids(
    request: WorkflowActionRequest,
) -> tuple[str | None, str | None]:
    user_concept_id = (
        _first_context_string(request, "user_concept_id", "namespace")
        or _context_string(request.environment.user_concept_id)
        or _context_string(request.environment.user_namespace)
    )
    org_concept_id = (
        _first_context_string(
            request,
            "org_concept_id",
            "organisation_concept_id",
            "organization_concept_id",
        )
        or _context_string(request.environment.org_concept_id)
    )
    return user_concept_id, org_concept_id


def _enabled_model_pool(
    *,
    user_concept_id: str | None,
    org_concept_id: str | None,
) -> list[dict[str, Any]]:
    try:
        from src.backend.services.settings_service import resolve_enabled_llm_settings

        entries = resolve_enabled_llm_settings(
            user_concept_id=user_concept_id,
            org_concept_id=org_concept_id,
        )
    except Exception:
        return []

    pool: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        provider = _context_string(entry.get("provider"))
        model = _context_string(entry.get("model"))
        host = _context_string(entry.get("host"))
        from ...languagemodels.model_defaults import is_audio_only_model

        if not provider or not model or is_audio_only_model(provider, model):
            continue
        key = (provider.lower(), model, host)
        if key in seen:
            continue
        seen.add(key)
        pool.append(
            {
                "provider": provider.lower(),
                "model": model,
                "host": host,
                "scope": _context_string(entry.get("scope")),
                "source": "enabled_settings",
            }
        )
    return pool


def _candidate_to_dict(candidate: Any) -> dict[str, Any]:
    return {
        "provider": getattr(candidate, "provider", None),
        "model": getattr(candidate, "model", None),
        "raw": getattr(candidate, "raw", None),
        "source": getattr(candidate, "source", None),
        "host": getattr(candidate, "host", None),
    }


def _resolve_active_llm_provider(default_model: str | None) -> str | None:
    if not default_model:
        return None
    try:
        from src.backend.languagemodels.llm_interface import (
            resolve_provider_from_model_concept,
        )

        provider = resolve_provider_from_model_concept(default_model)
    except Exception:
        return None
    return provider.strip().lower() if isinstance(provider, str) and provider.strip() else None


def _select_candidate_model(
    *,
    orchestrator: Any,
    candidate: Any,
    default_model: str | None,
) -> tuple[str | None, str | None]:
    source = getattr(candidate, "source", None)
    if source == "active_llm":
        return default_model, _resolve_active_llm_provider(default_model)
    model = orchestrator._normalise_llm_model_name(getattr(candidate, "model", None))
    provider = getattr(candidate, "provider", None)
    return (
        model.strip() if isinstance(model, str) and model.strip() else None,
        provider.strip().lower() if isinstance(provider, str) and provider.strip() else None,
    )


def _handle_select_stage_model(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )
    from src.backend.services.model_registry_service import get_model_registry_snapshot

    stage = _resolve_stage(request)
    workflow_id = _resolve_workflow_id(request)
    default_model = (
        _first_context_string(request, "default_model", "model")
        or _context_string(request.environment.model)
    )
    prefer_default_model = _context_bool(
        request.inputs.get("prefer_default_model"),
        default=_context_bool(request.data.get("prefer_default_model"), default=False),
    )
    user_concept_id, org_concept_id = _resolve_user_context_ids(request)

    gateway = cast(Any, request.environment.gateway or _NoopGateway())
    orchestrator = InternalMCPChatOrchestrator(gateway=gateway)
    policy_state, policy_telemetry = orchestrator._load_workflow_model_policy(None)
    registry_snapshot = get_model_registry_snapshot()
    candidates = orchestrator._stage_model_candidates(
        stage=stage,
        default_model=default_model,
        policy_state=policy_state,
        registry_snapshot=registry_snapshot,
        workflow_id=workflow_id,
        user_concept_id=user_concept_id,
        org_concept_id=org_concept_id,
        prefer_default_model=prefer_default_model,
    )
    enabled_model_pool = _enabled_model_pool(
        user_concept_id=user_concept_id,
        org_concept_id=org_concept_id,
    )

    selected_candidate = None
    selected_model = None
    selected_provider = None
    for candidate in candidates:
        candidate_model, candidate_provider = _select_candidate_model(
            orchestrator=orchestrator,
            candidate=candidate,
            default_model=default_model,
        )
        if candidate_model:
            selected_candidate = candidate
            selected_model = candidate_model
            selected_provider = candidate_provider
            break

    candidate_pool = [_candidate_to_dict(candidate) for candidate in candidates]
    selected_candidate_payload = (
        _candidate_to_dict(selected_candidate) if selected_candidate is not None else None
    )
    selection_metadata = orchestrator._build_stage_model_selection_metadata(
        stage=stage,
        policy_stage=stage,
        selected_candidate=selected_candidate,
        default_model=default_model,
        policy_state=policy_state,
        registry_snapshot=registry_snapshot,
        workflow_id=workflow_id,
        prefer_default_model=prefer_default_model,
    )
    policy_payload = {
        "enabled": bool(policy_state.enabled),
        "policy_id": policy_state.policy_id,
        "predicate_id": policy_state.predicate_id,
        "errors": list(policy_state.errors or ()),
        "telemetry": dict(policy_telemetry or {}),
    }
    model_selection = {
        "schema_version": WORKFLOW_MODEL_SELECTION_SCHEMA_VERSION,
        "stage": stage,
        "workflow_id": workflow_id,
        "default_model": default_model,
        "prefer_default_model": prefer_default_model,
        "selected_model": selected_model,
        "selected_provider": selected_provider,
        "selected_candidate": selected_candidate_payload,
        "candidate_pool": candidate_pool,
        "enabled_model_pool": enabled_model_pool,
        "workflow_model_policy": policy_payload,
        "registry_source": (
            registry_snapshot.get("source") if isinstance(registry_snapshot, Mapping) else None
        ),
        "selection_metadata": selection_metadata,
    }

    outputs = {
        "model_selection": model_selection,
        "selected_model": selected_model,
        "selected_model_provider": selected_provider,
        "selected_candidate": selected_candidate_payload,
        "model_candidate_pool": candidate_pool,
        "enabled_model_pool": enabled_model_pool,
        "workflow_model_policy": policy_payload,
        "selection_metadata": selection_metadata,
    }
    if not selected_model:
        return WorkflowActionResult(
            status="failed",
            error="workflow_model_selection:no_model_candidate_available",
            outputs=outputs,
        )
    return WorkflowActionResult(status="success", outputs=outputs)


def register_model_selection_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=WORKFLOW_MODEL_SELECTION_ACTION_ID,
            concept_id=WORKFLOW_MODEL_SELECTION_ACTION_CONCEPT_ID,
            handler=_handle_select_stage_model,
            description=(
                "Resolve the enabled model pool and selected model candidate for "
                "a workflow stage from the represented workflow model policy."
            ),
            input_schema={
                "stage": "str?",
                "workflow_id": "str?",
                "default_model": "str?",
                "prefer_default_model": "bool?",
                "user_concept_id": "str?",
                "organisation_concept_id": "str?",
            },
            output_schema={
                "selected_model": "str?",
                "selected_model_provider": "str?",
                "model_selection": "dict",
                "enabled_model_pool": "list",
                "model_candidate_pool": "list",
            },
            side_effects="none",
        )
    )


__all__ = [
    "WORKFLOW_MODEL_SELECTION_ACTION_ID",
    "WORKFLOW_MODEL_SELECTION_ACTION_CONCEPT_ID",
    "WORKFLOW_MODEL_SELECTION_SCHEMA_VERSION",
    "register_model_selection_actions",
]