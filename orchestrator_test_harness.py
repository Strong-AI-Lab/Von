from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    _WorkflowModelPolicyState,
)
from src.backend.services.conversation_turn_workflow_vontology_service import (
    _load_expected_outcome_prompt_seed_text,
    _load_synthesiser_context_framing_prompt_seed_text,
    _load_narration_prompt_seed_text,
)
from src.backend.services.synthesiser_context_framing_service import (
    SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
)
from src.backend.services.entity_information_retrieval_workflow_vontology_service import (
    _load_entity_information_retrieval_prompt_seed_text,
)
from src.backend.services.write_tool_request_evidence_vontology_service import (
    _load_write_tool_request_evidence_prompt_seed_text,
)
from src.backend.services.prompt_template_service import (
    PromptTemplateService as _RealPromptTemplateService,
)
from workflow_test_support import (
    TEST_WORKFLOW_SELECTOR_PROMPT_ID,
    TEST_WORKFLOW_SELECTOR_PROMPT_TEMPLATE,
    render_test_prompt_template,
)


class _WorkflowPreludeGatewayProxy:
    """Add canonical prelude read support without touching scenario tool stubs."""

    enabled = True

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def describe_methods(self) -> dict[str, Any]:
        methods: dict[str, Any] = {}
        describe = getattr(self._delegate, "describe_methods", None)
        if callable(describe):
            raw_methods = describe()
            if isinstance(raw_methods, dict):
                methods.update(raw_methods)
        methods.setdefault(
            "get_text_relations",
            {
                "description": (
                    "Read bounded Vontology text relations for workflow guidance."
                ),
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "concept_id": {"type": "string"},
                        "predicate": {"type": "string"},
                        "limit": {"type": "integer"},
                        "sort_recent_first": {"type": "boolean"},
                    },
                },
            },
        )
        methods.setdefault(
            "get_text_relations_summary",
            {
                "description": (
                    "Summarise bounded Vontology text relations for workflow guidance."
                ),
                "category": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "concept_id": {"type": "string"},
                        "predicates": {"type": "array"},
                        "languages": {"type": "array"},
                        "max_relation_ids_per_group": {"type": "integer"},
                    },
                },
            },
        )
        return methods

    def get_method_definition(self, tool_name: str) -> Any:
        getter = getattr(self._delegate, "get_method_definition", None)
        if callable(getter):
            try:
                definition = getter(tool_name)
            except Exception:
                definition = None
            if definition is not None:
                return definition
        return self.describe_methods().get(tool_name)

    def invoke(self, tool_name: str, payload: dict[str, Any]) -> Any:
        if tool_name == "get_text_relations":
            return SimpleNamespace(
                payload={
                    "success": True,
                    "concept_id": payload.get("concept_id"),
                    "predicate": payload.get("predicate"),
                    "relations_found": 0,
                    "relations": [],
                },
                duration_ms=0,
            )
        if tool_name == "get_text_relations_summary":
            return SimpleNamespace(
                payload={
                    "success": True,
                    "concept_id": payload.get("concept_id"),
                    "groups": [],
                    "groups_found": 0,
                    "total_relations_scanned": 0,
                },
                duration_ms=0,
            )
        invoker = getattr(self._delegate, "invoke", None)
        if not callable(invoker):
            raise AttributeError("delegate gateway has no invoke method")
        return invoker(tool_name, payload)


def _stub_stage_model_snapshot() -> dict[str, Any]:
    stages = [
        ("expected_outcome_inference", "Infer expected outcome", None, None),
        ("workflow_discovery", "Workflow discovery", None, None),
        (
            "selector_preparation",
            "Prepare selector context",
            "#V#conversation_turn_execution_workflow",
            "selector_preparation",
        ),
        (
            "selector_decision",
            "Select workflow",
            "#V#conversation_turn_execution_workflow",
            "selector_decision",
        ),
        ("workflow_dispatch", "Workflow dispatch", None, None),
        ("tool_plan", "Plan tool calls", "#V#tool_calling_workflow", "plan"),
        ("tool_execute", "Execute tool calls", "#V#tool_calling_workflow", "execute"),
        (
            "screen_backfill",
            "Summarise/backfill response",
            "#V#tool_calling_workflow",
            "backfill",
        ),
        ("response_finalising", "Finalising response", None, None),
        ("completed", "Completed", None, None),
    ]
    return {
        "schema_version": "conversation_turn_stage_model.v1",
        "workflow_representation_id": "#V#conversation_turn_execution_workflow",
        "stages": [
            {
                "stage_id": stage_id,
                "stage_label": stage_label,
                "runtime_aliases": [stage_id],
                "workflow_id": workflow_id,
                "workflow_state_id": workflow_state_id,
            }
            for stage_id, stage_label, workflow_id, workflow_state_id in stages
        ],
    }


def _stub_stage_path(*, runtime_stages: Any, **kwargs) -> dict[str, Any]:
    snapshot = _stub_stage_model_snapshot()
    stage_lookup = {
        str(entry.get("stage_id")): dict(entry)
        for entry in snapshot["stages"]
        if isinstance(entry, dict) and isinstance(entry.get("stage_id"), str)
    }
    workflow_hint = kwargs.get("workflow_id")
    workflow_hint = (
        workflow_hint.strip()
        if isinstance(workflow_hint, str) and workflow_hint.strip()
        else None
    )
    ordered_runtime_stages: list[str] = []
    for item in runtime_stages or []:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned or cleaned in ordered_runtime_stages:
            continue
        if cleaned == "workflow_discovery_complete":
            cleaned = "workflow_discovery"
        elif cleaned in {"orchestrator_start", "orchestrator_end"}:
            cleaned = "workflow_dispatch"
        ordered_runtime_stages.append(cleaned)

    path = []
    observed_workflow_ids: list[str] = []
    observed_workflow_keys: set[str] = set()
    for sequence_no, stage_id in enumerate(ordered_runtime_stages):
        entry: dict[str, Any] = dict(
            stage_lookup.get(stage_id) or {"stage_id": stage_id}
        )
        entry.setdefault("stage_label", stage_id.replace("_", " ").title())
        entry["sequence_no"] = sequence_no
        entry["runtime_stage"] = stage_id
        entry["runtime_stage_normalised"] = stage_id
        entry["mapping_status"] = "mapped"
        path.append(entry)
        mapped_workflow_id = entry.get("workflow_id")
        if isinstance(mapped_workflow_id, str) and mapped_workflow_id.strip():
            dedupe_key = mapped_workflow_id.strip().lower()
            if dedupe_key not in observed_workflow_keys:
                observed_workflow_keys.add(dedupe_key)
                observed_workflow_ids.append(mapped_workflow_id.strip())

    if len(observed_workflow_ids) == 1:
        root_workflow_id = observed_workflow_ids[0]
        workflow_id_source = "mapped_stage_consensus"
    elif observed_workflow_ids:
        root_workflow_id = None
        workflow_id_source = "mixed_stage_membership"
    else:
        root_workflow_id = workflow_hint
        workflow_id_source = "route_hint" if workflow_hint else None

    return {
        "schema_version": "conversation_turn_stage_path.v1",
        "stage_model_schema_version": snapshot["schema_version"],
        "workflow_representation_id": snapshot["workflow_representation_id"],
        "workflow_id": root_workflow_id,
        "workflow_id_source": workflow_id_source,
        "observed_workflow_ids": observed_workflow_ids,
        "has_unmapped_runtime_stages": False,
        "unmapped_runtime_stages": [],
        "path": path,
    }


def build_db_independent_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gateway: Any,
    selector_enabled: bool = False,
    max_tool_invocations: int = 1,
    tool_batch_cap: int = 3,
) -> InternalMCPChatOrchestrator:
    """Build an orchestrator without DB/model-registry dependencies.

    These tests target routing and write-policy behaviour, not Mongo-backed
    workflow discovery or model registry resolution. Keep the harness minimal
    so orchestration-path regressions fail fast instead of hanging on startup.
    When tests need to suppress selector-driven routing, do that by patching
    the selector object directly rather than relying on removed runtime env
    toggles.
    """

    from src.backend.workflows import WorkflowRegistry
    from src.backend.workflows.action_registry import ActionRegistry
    from src.backend.workflows.durable.control_flow_actions import (
        register_control_flow_actions,
    )
    from src.backend.workflows.durable.subworkflow_actions import (
        register_subworkflow_actions,
    )
    from src.backend.workflows.durable.synthesiser_context_prep_actions import (
        register_synthesiser_context_prep_actions,
    )
    from src.backend.workflows.durable.turn_execution_actions import (
        register_turn_execution_actions,
    )
    from src.backend.workflows.definitions import CONVERSATION_TURN_WORKFLOW_IDS
    from src.backend.workflows.workflow_registry import (
        LazyWorkflowRegistration,
        WorkflowRegistration,
    )
    from src.backend.workflows import (
        workflow_concept_authority_service as authority_service,
    )

    seed_specs = authority_service.seed_canonical_workflow_publication_specs()
    publication_specs = dict(seed_specs)
    publication_purposes: dict[str, str | None] = {
        workflow_id: getattr(spec, "purpose", None)
        for workflow_id, spec in seed_specs.items()
    }

    bundle_dir = getattr(
        authority_service,
        "REPO_SEED_WORKFLOW_BUNDLE_DIR",
        None,
    )
    if isinstance(bundle_dir, Path) and bundle_dir.exists():
        for asset_path in sorted(bundle_dir.glob("*_seed_bundle.json")):
            try:
                bundle = authority_service.load_repo_seed_workflow_bundle(
                    asset_path
                )
            except Exception:
                continue
            bundle_specs = bundle.get("publication_specs")
            bundle_purposes = bundle.get("publication_purposes")
            if isinstance(bundle_specs, dict):
                for workflow_id, spec in bundle_specs.items():
                    if workflow_id not in publication_specs:
                        publication_specs[str(workflow_id)] = spec
            if isinstance(bundle_purposes, dict):
                for workflow_id, purpose in bundle_purposes.items():
                    publication_purposes.setdefault(
                        str(workflow_id),
                        (
                            str(purpose).strip()
                            if isinstance(purpose, str) and str(purpose).strip()
                            else None
                        ),
                    )

    def _load_seed_workflow_definition(workflow_id: str):
        spec = publication_specs.get(workflow_id)
        if spec is None:
            return None
        return authority_service._build_definition_from_publication_spec(
            workflow_id=workflow_id,
            spec=spec,
        )

    gateway_for_orchestrator = _WorkflowPreludeGatewayProxy(gateway)

    shared_test_registry: WorkflowRegistry | None = None

    def _build_test_registry(*, defer_parity_work: bool = True) -> WorkflowRegistry:
        nonlocal shared_test_registry
        assert defer_parity_work is True
        if shared_test_registry is not None:
            return shared_test_registry

        registry = WorkflowRegistry(definition_loader=_load_seed_workflow_definition)
        for workflow_id in CONVERSATION_TURN_WORKFLOW_IDS:
            spec = publication_specs.get(workflow_id)
            if spec is None:
                continue
            registry.register(
                WorkflowRegistration(
                    workflow_id=workflow_id,
                    definition=authority_service._build_definition_from_publication_spec(
                        workflow_id=workflow_id,
                        spec=spec,
                    ),
                    purpose=workflow_id,
                    source="vontology",
                )
            )
        eager_ids = {
            str(workflow_id).strip()
            for workflow_id in CONVERSATION_TURN_WORKFLOW_IDS
            if isinstance(workflow_id, str) and str(workflow_id).strip()
        }
        for workflow_id in sorted(publication_specs):
            if workflow_id in eager_ids:
                continue
            registry.register_lazy(
                LazyWorkflowRegistration(
                    workflow_id=workflow_id,
                    purpose=publication_purposes.get(workflow_id),
                    source="vontology",
                )
            )
        shared_test_registry = registry
        return registry

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_shared_workflow_registry_read_only",
        _build_test_registry,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        _build_test_registry,
    )

    def _build_test_action_registry() -> ActionRegistry:
        from src.backend.workflows.mcp_tool_bridge import (
            workflow_action_result_from_mcp_payload,
        )

        def _gateway_fallback_action(request: Any):
            payload = dict(getattr(request, "inputs", {}) or {})
            environment = getattr(request, "environment", None)
            user_namespace = getattr(environment, "user_namespace", None)
            if isinstance(user_namespace, str) and user_namespace.strip():
                payload.setdefault("namespace", user_namespace.strip())
            result = gateway_for_orchestrator.invoke(request.action_id, payload)
            return workflow_action_result_from_mcp_payload(
                tool_name=request.action_id,
                payload=result.payload,
                duration_ms=result.duration_ms,
            )

        registry = ActionRegistry()
        register_control_flow_actions(
            registry,
            definition_loader=_load_seed_workflow_definition,
        )
        register_subworkflow_actions(
            registry,
            definition_loader=_load_seed_workflow_definition,
        )
        register_turn_execution_actions(registry)
        register_synthesiser_context_prep_actions(registry)
        registry.set_fallback_handler(_gateway_fallback_action)
        return registry

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_shared_durable_action_registry",
        _build_test_action_registry,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_shared_durable_action_registry",
        _build_test_action_registry,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_tool_metadata",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_tool_salience",
        lambda *_a, **_kw: "medium",
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.is_tool_visible",
        lambda *_a, **_kw: True,
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, gateway_for_orchestrator),
        max_tool_invocations=max_tool_invocations,
        tool_batch_cap=tool_batch_cap,
    )

    monkeypatch.setattr(
        orchestrator,
        "_load_workflow_model_policy",
        lambda *_a, **_kw: (
            _WorkflowModelPolicyState(
                enabled=False,
                policy=None,
                policy_id=None,
                predicate_id=None,
                errors=[],
            ),
            None,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_ontology_preflight",
        lambda *_a, **_kw: type(
            "_Preflight", (), {"telemetry": None, "message": None}
        )(),
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.load_minimal_imposition_runtime_profile",
        lambda **_kw: (
            None,
            {"status": "skipped_db_independent_test_harness"},
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_resolve_concept_id_by_name",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_get_missing_tool_call_detector",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda *_a, **_kw: (
            "You are Von. Prefer direct answers and tool execution when needed.",
            "#V#test_base_system_prompt",
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "policy_active": False,
            "guidance_mode": "none",
            "candidate_scores": [],
            "ranked_candidate_ids": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.build_conversation_turn_stage_model_snapshot",
        _stub_stage_model_snapshot,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.build_conversation_turn_stage_path",
        _stub_stage_path,
    )
    monkeypatch.setattr(
        orchestrator._workflow_selector,
        "enabled",
        lambda: selector_enabled,
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.build_conversation_turn_stage_model_snapshot",
        _stub_stage_model_snapshot,
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.build_conversation_turn_stage_path",
        _stub_stage_path,
    )

    original_render_prompt = orchestrator._prompt_templates.render_prompt
    selector_prompt_ids = {
        str(item).strip()
        for item in (orchestrator._TURN_SELECTOR_PROMPTS or ())
        if isinstance(item, str) and str(item).strip()
    }
    expected_outcome_prompt_id = "#V#prompt_turn_execution_expected_outcome_inference"
    narration_prompt_id = "#V#prompt_turn_execution_narrate_completion_report"
    entity_information_retrieval_prompt_id = (
        "#V#entity_information_retrieval_prompt"
    )
    write_tool_request_evidence_prompt_id = (
        "#V#prompt_write_tool_request_evidence_inference"
    )
    synthesiser_context_framing_prompt_id = (
        SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID
    )
    turn_current_request_prompt_id = "#V#turn_current_request_stage_prompt"

    class _HarnessPromptTemplateService:
        def __init__(self, default_max_chars: int = 24000):
            self._delegate = _RealPromptTemplateService(
                default_max_chars=default_max_chars
            )

        def render_prompt(
            self,
            concept_ids: Any,
            *,
            variables: Any = None,
            fallback: Any = None,
            max_chars: Any = None,
        ) -> Any:
            requested_prompt_ids = [
                str(item).strip()
                for item in (concept_ids or ())
                if isinstance(item, str) and str(item).strip()
            ]
            if expected_outcome_prompt_id in requested_prompt_ids:
                return SimpleNamespace(
                    text=_load_expected_outcome_prompt_seed_text(),
                    prompt_id=expected_outcome_prompt_id,
                    variables=dict(variables or {}),
                    truncated=False,
                )
            if narration_prompt_id in requested_prompt_ids:
                return SimpleNamespace(
                    text=_load_narration_prompt_seed_text(),
                    prompt_id=narration_prompt_id,
                    variables=dict(variables or {}),
                    truncated=False,
                )
            if entity_information_retrieval_prompt_id in requested_prompt_ids:
                return SimpleNamespace(
                    text=_load_entity_information_retrieval_prompt_seed_text(),
                    prompt_id=entity_information_retrieval_prompt_id,
                    variables=dict(variables or {}),
                    truncated=False,
                )
            if write_tool_request_evidence_prompt_id in requested_prompt_ids:
                return SimpleNamespace(
                    text=_load_write_tool_request_evidence_prompt_seed_text(),
                    prompt_id=write_tool_request_evidence_prompt_id,
                    variables=dict(variables or {}),
                    truncated=False,
                )
            if turn_current_request_prompt_id in requested_prompt_ids:
                rendered = render_test_prompt_template(
                    (
                        "Current turn request to route:\n{turn_text}\n\n"
                        "Use the surrounding turn context to resolve references "
                        "and continuity, but keep this request as the immediate "
                        "routing objective unless explicit continuation context "
                        "requires otherwise."
                    ),
                    dict(variables or {}),
                )
                return SimpleNamespace(
                    text=rendered,
                    prompt_id=turn_current_request_prompt_id,
                    variables=dict(variables or {}),
                    truncated=False,
                )
            return self._delegate.render_prompt(
                concept_ids,
                variables=variables,
                fallback=fallback,
                max_chars=max_chars,
            )

        def resolve_prompt_text(
            self,
            concept_ids: Any,
            *,
            fallback: Any = None,
            max_chars: Any = None,
        ) -> Any:
            requested_prompt_ids = [
                str(item).strip()
                for item in (concept_ids or ())
                if isinstance(item, str) and str(item).strip()
            ]
            if synthesiser_context_framing_prompt_id in requested_prompt_ids:
                return (
                    synthesiser_context_framing_prompt_id,
                    _load_synthesiser_context_framing_prompt_seed_text(),
                )
            return self._delegate.resolve_prompt_text(
                concept_ids,
                fallback=fallback,
                max_chars=max_chars,
            )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.PromptTemplateService",
        _HarnessPromptTemplateService,
    )
    monkeypatch.setattr(
        "src.backend.services.synthesiser_context_framing_service.PromptTemplateService",
        _HarnessPromptTemplateService,
    )

    def _render_prompt_with_narration_fallback(
        prompt_ids: Any,
        *,
        fallback: Any = None,
        variables: Any = None,
        max_chars: Any = None,
    ) -> Any:
        requested_prompt_ids = [
            str(item).strip()
            for item in (prompt_ids or ())
            if isinstance(item, str) and str(item).strip()
        ]
        if requested_prompt_ids and any(
            item in selector_prompt_ids for item in requested_prompt_ids
        ):
            return SimpleNamespace(
                text=render_test_prompt_template(
                    TEST_WORKFLOW_SELECTOR_PROMPT_TEMPLATE,
                    dict(variables or {}),
                ),
                prompt_id=requested_prompt_ids[0] or TEST_WORKFLOW_SELECTOR_PROMPT_ID,
                variables=dict(variables or {}),
                truncated=False,
            )
        if turn_current_request_prompt_id in requested_prompt_ids:
            return SimpleNamespace(
                text=render_test_prompt_template(
                    (
                        "Current turn request to route:\n{turn_text}\n\n"
                        "Use the surrounding turn context to resolve references "
                        "and continuity, but keep this request as the immediate "
                        "routing objective unless explicit continuation context "
                        "requires otherwise."
                    ),
                    dict(variables or {}),
                ),
                prompt_id=turn_current_request_prompt_id,
                variables=dict(variables or {}),
                truncated=False,
            )
        if write_tool_request_evidence_prompt_id in requested_prompt_ids:
            return SimpleNamespace(
                text=_load_write_tool_request_evidence_prompt_seed_text(),
                prompt_id=write_tool_request_evidence_prompt_id,
                variables=dict(variables or {}),
                truncated=False,
            )
        if not prompt_ids and not fallback:
            return SimpleNamespace(
                text=(
                    "Produce a concise spoken summary of the on-screen content. "
                    "Return only the spoken talk track."
                ),
                prompt_id="#V#test_narration_prompt",
            )
        return original_render_prompt(
            prompt_ids,
            fallback=fallback,
            variables=variables,
            max_chars=max_chars,
        )

    monkeypatch.setattr(
        orchestrator._prompt_templates,
        "render_prompt",
        _render_prompt_with_narration_fallback,
    )

    return orchestrator
