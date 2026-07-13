from types import SimpleNamespace
from typing import Any

import pytest

from src.backend.workflows import workflow_studio_service as mod
from src.backend.workflows import workflow_concept_authority_service as authority_service
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology


@pytest.fixture(autouse=True)
def _authenticated_workflow_studio_actor(monkeypatch):
    """Existing write-path tests run under explicit ambient actor authority."""

    monkeypatch.setattr(
        mod,
        "get_effective_user_concept_id",
        lambda: "#V#studio_author",
    )
    monkeypatch.setattr(
        mod,
        "get_effective_organisation_concept_id",
        lambda: "#V#studio_org",
    )


@pytest.fixture
def _reset_mock_workflow_studio_graph_db(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db
    from src.backend.services.workflow_discovery_service import (
        invalidate_workflow_discovery_executability_caches,
    )
    from src.backend.workflows.durable.registry_factory import (
        invalidate_shared_workflow_registry_read_only,
    )

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    invalidate_workflow_discovery_executability_caches()
    invalidate_shared_workflow_registry_read_only()
    yield
    invalidate_workflow_discovery_executability_caches()
    invalidate_shared_workflow_registry_read_only()
    authority_service.clear_workflow_type_resolution_cache()


@pytest.mark.parametrize(
    "invoke_mutation",
    [
        lambda: mod.apply_workflow_authoring_spec(
            "#V#new_workflow",
            authoring_spec={},
        ),
        lambda: mod.submit_workflow_authoring_proposal(
            "#V#new_workflow",
            authoring_spec={},
        ),
        lambda: mod.record_workflow_authoring_promotion_evaluation(
            "#V#public_workflow",
            promotion_evaluation={},
        ),
        lambda: mod.review_workflow_authoring_proposal(
            "#V#public_workflow",
            action="approve",
        ),
        lambda: mod.rollback_workflow_authoring_promotion(
            "#V#public_workflow",
        ),
        lambda: mod.demote_workflow_routing("#V#public_workflow"),
        lambda: mod.supersede_workflow_publication(
            "#V#public_workflow",
            replacement_workflow_id="#V#replacement_workflow",
        ),
    ],
)
def test_workflow_studio_mutation_services_require_authenticated_actor(
    monkeypatch,
    invoke_mutation,
) -> None:
    monkeypatch.setattr(mod, "get_effective_user_concept_id", lambda: None)
    monkeypatch.setattr(
        mod,
        "get_effective_organisation_concept_id",
        lambda: None,
    )

    with pytest.raises(
        mod.WorkflowStudioAuthorityError,
        match="workflow_actor_authority_required",
    ) as exc_info:
        invoke_mutation()

    assert exc_info.value.error_code == "workflow_actor_authority_required"


def test_build_workflow_description_proposal_uses_scoped_llm_model(
    monkeypatch,
) -> None:
    captured: dict[str, str | None] = {}

    monkeypatch.setattr(
        mod,
        "build_workflow_studio_detail_payload",
        lambda workflow_id: {
            "workflow_id": workflow_id,
            "summary": {
                "workflow_id": workflow_id,
                "description": "Current workflow description",
            },
        },
    )
    monkeypatch.setattr(
        mod,
        "build_workflow_description_prompt_context",
        lambda workflow_id: {
            "Purpose": f"Purpose for {workflow_id}",
            "Steps": "start -> finish",
        },
    )
    monkeypatch.setattr(
        mod,
        "build_deterministic_workflow_description",
        lambda **_kwargs: "Deterministic description",
    )
    monkeypatch.setattr(
        mod,
        "resolve_workflow_description_prompt_concept_id",
        lambda workflow_id: "#V#workflow_prompt",
    )

    class _PromptBuilder:
        def __init__(self, concept_id: str):
            captured["prompt_concept_id"] = concept_id

        def get_instruction(self) -> str:
            return "Write a concise workflow description."

    class _DummyClient:
        def generate(self, prompt: str, model: str | None = None, **_kwargs) -> str:
            captured["prompt"] = prompt
            captured["model"] = model
            return "AI workflow description"

    monkeypatch.setattr(mod, "AnnotationPromptBuilder", _PromptBuilder)
    monkeypatch.setattr(
        mod,
        "get_active_model_name",
        lambda *, user_concept_id=None, org_concept_id=None: (
            captured.update(
                {
                    "model_user_concept_id": user_concept_id,
                    "model_org_concept_id": org_concept_id,
                }
            )
            or "gpt-5.4-mini"
        ),
    )
    monkeypatch.setattr(
        mod,
        "get_llm_client",
        lambda *, user_concept_id=None, org_concept_id=None: (
            captured.update(
                {
                    "client_user_concept_id": user_concept_id,
                    "client_org_concept_id": org_concept_id,
                }
            )
            or _DummyClient()
        ),
    )

    result = mod.build_workflow_description_proposal(
        "#V#alpha_workflow",
        mode="auto",
        user_concept_id="#V#test_user",
        org_concept_id="#V#test_org",
    )

    assert result["workflow_id"] == "#V#alpha_workflow"
    assert result["proposal"]["text"] == "AI workflow description"
    assert result["proposal"]["source"] == "llm"
    assert captured["prompt_concept_id"] == "#V#workflow_prompt"
    assert captured["client_user_concept_id"] == "#V#test_user"
    assert captured["client_org_concept_id"] == "#V#test_org"
    assert captured["model_user_concept_id"] == "#V#test_user"
    assert captured["model_org_concept_id"] == "#V#test_org"
    assert captured["model"] == "gpt-5.4-mini"
    assert "Workflow ID: #V#alpha_workflow" in str(captured["prompt"])


def test_validate_workflow_candidate_flags_generation_safety_failures(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mod,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: {
            "success": True,
            "workflow_id": workflow_id,
            "preview": {
                "contract_validation": {"valid": True, "errors": []},
                "definition_identity": {"hash": "candidate-hash"},
                "diff_summary": {"changed_state_count": 1},
            },
        },
    )

    result = mod.validate_workflow_candidate(
        "#V#candidate_workflow",
        authoring_spec={
            "workflow_id": "#V#candidate_workflow",
            "publication_spec": {
                "steps": [
                    {
                        "state_id": "generate",
                        "action_id": "llm.action",
                        "execution_mode": "llm",
                    }
                ]
            },
        },
    )

    assert result["success"] is True
    validation = result["candidate_validation"]
    assert validation["valid"] is False
    assert validation["generation_safe_validation"]["valid"] is False
    assert validation["quality_signals"] == {
        "contract_valid": True,
        "generation_safe_valid": False,
        "repair_hint_count": 2,
    }
    assert validation["assertion_classes"] == [
        "workflow_candidate_validation",
        "workflow_generation_safety_failure",
    ]
    assert validation["repair_hints"] == [
        {
            "reason_code": "executable_step_missing_failure_path",
            "repair_hint": "Add an explicit failure path for every executable step.",
            "scope": "generation_safety",
            "state_id": "generate",
        },
        {
            "reason_code": "llm_step_missing_validation_policy",
            "repair_hint": "Add a validation policy to every LLM execution step.",
            "scope": "generation_safety",
            "state_id": "generate",
        },
    ]


def test_validate_workflow_candidate_contract_only_profile_keeps_contract_validity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mod,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: {
            "success": True,
            "workflow_id": workflow_id,
            "preview": {
                "contract_validation": {"valid": True, "errors": []},
                "definition_identity": {"hash": "candidate-hash"},
                "diff_summary": {"changed_state_count": 1},
            },
        },
    )

    result = mod.validate_workflow_candidate(
        "#V#candidate_workflow",
        authoring_spec={
            "workflow_id": "#V#candidate_workflow",
            "publication_spec": {
                "steps": [
                    {
                        "state_id": "generate",
                        "action_id": "llm.action",
                        "execution_mode": "llm",
                    }
                ]
            },
        },
        validation_profile="contract_only",
    )

    assert result["success"] is True
    validation = result["candidate_validation"]
    assert validation["validation_profile"] == "contract_only"
    assert validation["valid"] is True
    assert validation["contract_validation"]["valid"] is True
    assert validation["generation_safe_validation"]["valid"] is False


def test_apply_workflow_authoring_spec_updates_existing_workflow_without_root_creation(
    monkeypatch,
) -> None:
    publication_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        mod,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: {
            "workflow_id": workflow_id,
            "preview": {"contract_validation": {"valid": True}},
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_authoring_runtime_definition",
        lambda workflow_id: (SimpleNamespace(workflow_id=workflow_id), "vontology", object()),
    )
    monkeypatch.setattr(mod, "_workflow_concept_exists", lambda _workflow_id: True)
    monkeypatch.setattr(
        mod,
        "build_workflow_definition_from_authoring_spec",
        lambda spec: SimpleNamespace(workflow_id=spec["workflow_id"]),
    )
    monkeypatch.setattr(
        mod,
        "publish_workflow_definition_from_definition",
        lambda **kwargs: publication_calls.append(dict(kwargs)) or {"ok": True},
    )

    result = mod.apply_workflow_authoring_spec(
        "#V#alpha_workflow",
        authoring_spec={
            "workflow_id": "#V#alpha_workflow",
            "description": "Updated description",
            "steps": [{"state_id": "start"}],
        },
    )

    assert result["publication"] == {"ok": True}
    assert publication_calls[0]["create_missing"] is False
    assert publication_calls[0]["create_missing_child_concepts"] is True
    assert publication_calls[0]["purpose"] == "Updated description"


def test_apply_workflow_authoring_spec_creates_missing_workflow_when_absent(
    monkeypatch,
) -> None:
    publication_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        mod,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: {
            "workflow_id": workflow_id,
            "preview": {"contract_validation": {"valid": True}},
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_authoring_runtime_definition",
        lambda _workflow_id: (None, "new_workflow", object()),
    )
    monkeypatch.setattr(mod, "_workflow_concept_exists", lambda _workflow_id: False)
    monkeypatch.setattr(
        mod,
        "build_workflow_definition_from_authoring_spec",
        lambda spec: SimpleNamespace(workflow_id=spec["workflow_id"]),
    )
    monkeypatch.setattr(
        mod,
        "publish_workflow_definition_from_definition",
        lambda **kwargs: publication_calls.append(dict(kwargs)) or {"ok": True},
    )

    mod.apply_workflow_authoring_spec(
        "#V#new_workflow",
        authoring_spec={
            "workflow_id": "#V#new_workflow",
            "description": "Create me",
            "steps": [{"state_id": "start"}],
        },
    )

    assert publication_calls[0]["create_missing"] is True
    assert publication_calls[0]["create_missing_child_concepts"] is True


def test_apply_workflow_authoring_spec_uses_existing_concept_when_runtime_load_is_missing(
    monkeypatch,
) -> None:
    publication_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        mod,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: {
            "workflow_id": workflow_id,
            "preview": {"contract_validation": {"valid": True}},
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_authoring_runtime_definition",
        lambda _workflow_id: (None, "unknown", object()),
    )
    monkeypatch.setattr(mod, "_workflow_concept_exists", lambda _workflow_id: True)
    monkeypatch.setattr(
        mod,
        "build_workflow_definition_from_authoring_spec",
        lambda spec: SimpleNamespace(workflow_id=spec["workflow_id"]),
    )
    monkeypatch.setattr(
        mod,
        "publish_workflow_definition_from_definition",
        lambda **kwargs: publication_calls.append(dict(kwargs)) or {"ok": True},
    )

    mod.apply_workflow_authoring_spec(
        "#V#existing_workflow",
        authoring_spec={
            "workflow_id": "#V#existing_workflow",
            "description": "Repair me",
            "steps": [{"state_id": "start"}],
        },
    )

    assert publication_calls[0]["create_missing"] is False
    assert publication_calls[0]["create_missing_child_concepts"] is True


def test_authoring_existence_probe_failure_fails_closed(monkeypatch) -> None:
    registry = SimpleNamespace(all_workflow_ids=lambda: [])
    monkeypatch.setattr(
        mod,
        "get_shared_workflow_registry_read_only",
        lambda **_kwargs: registry,
    )

    def _raise_probe_failure(_workflow_id):
        raise mod.WorkflowStudioAuthorityError(
            "workflow_concept_existence_authority_unavailable"
        )

    monkeypatch.setattr(mod, "_workflow_concept_exists", _raise_probe_failure)

    with pytest.raises(
        mod.WorkflowStudioAuthorityError,
        match="workflow_concept_existence_authority_unavailable",
    ):
        mod._load_authoring_runtime_definition("#V#possibly_existing_workflow")


def test_authoring_runtime_loader_treats_provably_absent_workflow_as_new(
    _reset_mock_workflow_studio_graph_db,
    monkeypatch,
) -> None:
    registry = SimpleNamespace(all_workflow_ids=lambda: [])
    monkeypatch.setattr(
        mod,
        "get_shared_workflow_registry_read_only",
        lambda **_kwargs: registry,
    )

    definition, source, resolved_registry = mod._load_authoring_runtime_definition(
        "#V#provably_absent_studio_workflow"
    )

    assert definition is None
    assert source == "new_workflow"
    assert resolved_registry is registry


def test_workflow_concept_existence_distinguishes_visible_and_hidden(
    monkeypatch,
) -> None:
    from src.backend.services import concept_service

    monkeypatch.setattr(
        concept_service,
        "_find_raw_concept_by_exact_concept_id",
        lambda _concept_id: {"concept_id": "#V#existing_workflow"},
    )
    monkeypatch.setattr(mod, "can_access_concept", lambda _concept_id: True)
    assert mod._workflow_concept_exists("#V#existing_workflow") is True

    monkeypatch.setattr(mod, "can_access_concept", lambda _concept_id: False)
    with pytest.raises(
        mod.WorkflowStudioAuthorityError,
        match="workflow_definition_not_loadable_for_actor",
    ):
        mod._workflow_concept_exists("#V#hidden_workflow")


def test_workflow_concept_existence_authority_failure_is_not_treated_as_absent(
    monkeypatch,
) -> None:
    from src.backend.services import concept_service

    monkeypatch.setattr(
        concept_service,
        "_find_raw_concept_by_exact_concept_id",
        lambda _concept_id: (_ for _ in ()).throw(RuntimeError("store offline")),
    )

    with pytest.raises(
        mod.WorkflowStudioAuthorityError,
        match="workflow_concept_existence_authority_unavailable",
    ):
        mod._workflow_concept_exists("#V#unknown_workflow")


@pytest.mark.parametrize(
    "hidden_field",
    ("step", "context_mapping", "tool_output_mapping"),
)
def test_apply_preflights_explicit_child_ids_before_any_publication_mutation(
    hidden_field: str,
    monkeypatch,
) -> None:
    from src.backend.services import concept_service

    hidden_id = "#V#hidden_child_concept"
    step: dict[str, Any] = {"state_id": "start", "terminal": True}
    if hidden_field == "step":
        step["concept_id"] = hidden_id
    elif hidden_field == "context_mapping":
        step["context_input_mappings"] = [
            {
                "tool_param": "query",
                "context_key": "request_text",
                "mapping_concept_id": hidden_id,
            }
        ]
    else:
        step["tool_output_context_mappings"] = [
            {
                "tool_output_field": "result",
                "context_key": "result",
                "mapping_concept_id": hidden_id,
            }
        ]
    authoring_spec = {
        "workflow_id": "#V#visible_workflow",
        "steps": [step],
    }
    mutations: list[dict[str, object]] = []

    monkeypatch.setattr(
        mod,
        "preview_workflow_authoring_spec",
        lambda _workflow_id, **_kwargs: {
            "preview": {"contract_validation": {"valid": True}}
        },
    )
    monkeypatch.setattr(
        mod,
        "build_workflow_definition_from_authoring_spec",
        lambda _spec: SimpleNamespace(workflow_id="#V#visible_workflow"),
    )
    monkeypatch.setattr(
        mod,
        "_load_authoring_runtime_definition",
        lambda _workflow_id: (object(), "vontology", object()),
    )
    monkeypatch.setattr(
        concept_service,
        "_find_raw_concept_by_exact_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )
    monkeypatch.setattr(mod, "can_access_concept", lambda _concept_id: False)
    monkeypatch.setattr(
        mod,
        "publish_workflow_definition_from_definition",
        lambda **kwargs: mutations.append(dict(kwargs)),
    )

    with pytest.raises(
        mod.WorkflowStudioAuthorityError,
        match="workflow_definition_not_loadable_for_actor",
    ):
        mod.apply_workflow_authoring_spec(
            "#V#visible_workflow",
            authoring_spec=authoring_spec,
        )

    assert mutations == []


def test_demote_hidden_workflow_fails_before_mutation(monkeypatch) -> None:
    mutations: list[object] = []
    monkeypatch.setattr(
        mod,
        "_load_actor_scoped_runtime_definition",
        lambda _workflow_id: (_ for _ in ()).throw(
            mod.WorkflowStudioAuthorityError(
                "workflow_definition_not_loadable_for_actor"
            )
        ),
    )
    monkeypatch.setattr(
        mod,
        "_set_workflow_runtime_enablement",
        lambda *_args, **_kwargs: mutations.append("runtime"),
    )
    monkeypatch.setattr(
        mod,
        "upsert_workflow_publication_lifecycle",
        lambda **_kwargs: mutations.append("lifecycle"),
    )

    with pytest.raises(mod.WorkflowStudioAuthorityError):
        mod.demote_workflow_routing("#V#hidden_workflow")

    assert mutations == []


def test_supersede_hidden_replacement_leaves_source_untouched(monkeypatch) -> None:
    mutations: list[object] = []

    def _load(workflow_id):
        if workflow_id == "#V#hidden_replacement":
            raise mod.WorkflowStudioAuthorityError(
                "workflow_definition_not_loadable_for_actor"
            )
        return SimpleNamespace(workflow_id=workflow_id), "vontology", object()

    monkeypatch.setattr(mod, "_load_actor_scoped_runtime_definition", _load)
    monkeypatch.setattr(
        mod,
        "_set_workflow_runtime_enablement",
        lambda *_args, **_kwargs: mutations.append("runtime"),
    )
    monkeypatch.setattr(
        mod,
        "upsert_workflow_publication_lifecycle",
        lambda **_kwargs: mutations.append("lifecycle"),
    )

    with pytest.raises(mod.WorkflowStudioAuthorityError):
        mod.supersede_workflow_publication(
            "#V#visible_source",
            replacement_workflow_id="#V#hidden_replacement",
        )

    assert mutations == []


def test_apply_workflow_authoring_spec_materialises_new_steps_for_existing_workflow(
    _reset_mock_workflow_studio_graph_db,
    monkeypatch,
) -> None:
    workflow_id = "#V#workflow_studio_existing_update_workflow"
    start_step_id = authority_service._step_concept_id(
        workflow_id=workflow_id,
        state_id="start",
    )
    collect_step_id = authority_service._step_concept_id(
        workflow_id=workflow_id,
        state_id="collect",
    )
    finish_step_id = authority_service._step_concept_id(
        workflow_id=workflow_id,
        state_id="finish",
    )

    seed_definition = WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id="workflow_control.context_set",
                        inputs={
                            "assignments": [
                                {"key": "seed_seen", "value": True},
                            ],
                        },
                        execution_mode="control",
                    ),
                ),
                terminal=True,
            )
        },
        termination_states=("start",),
    )
    seed_report = authority_service.publish_workflow_definition_from_definition(
        definition=seed_definition,
        create_missing=True,
    )
    assert seed_report["counts"]["errors"] == 0
    assert seed_report["counts"]["workflows_published"] == 1

    class _Registry:
        def all_workflow_ids(self):
            return [workflow_id]

        def get(self, requested_workflow_id: str):
            return load_workflow_definition_from_vontology(requested_workflow_id)

        def get_registration_source(self, _workflow_id: str, **_kwargs):
            return "vontology"

    monkeypatch.setattr(
        mod,
        "get_shared_workflow_registry_read_only",
        lambda **_kwargs: _Registry(),
    )

    authoring_spec = {
        "workflow_id": workflow_id,
        "description": "Updated workflow with a new child step.",
        "steps": [
            {
                "state_id": "start",
                "action_id": "workflow_control.context_set",
                "execution_mode": "control",
                "static_input_bindings": [
                    {
                        "tool_param": "assignments",
                        "value": [{"key": "seed_seen", "value": True}],
                    }
                ],
                "next_state_key": "collect",
            },
            {
                "state_id": "collect",
                "action_id": "workflow_control.context_set",
                "execution_mode": "control",
                "context_input_mappings": [
                    {
                        "tool_param": "assignments",
                        "context_key": "new_assignments",
                        "mapping_concept_id": (
                            "#V#workflow_mapping_studio_existing_update_collect_"
                            "assignments"
                        ),
                    }
                ],
                "next_state_key": "finish",
            },
            {
                "state_id": "finish",
                "terminal": True,
            },
        ],
    }

    apply_result = mod.apply_workflow_authoring_spec(
        workflow_id,
        authoring_spec=authoring_spec,
    )

    publication = apply_result["publication"]
    assert publication["counts"]["errors"] == 0
    assert publication["counts"]["workflows_published"] == 1
    assert set(publication["created_step_concept_ids"]) == {
        collect_step_id,
        finish_step_id,
    }
    assert publication["counts"]["mapping_concepts_created"] == 1

    loaded_definition = load_workflow_definition_from_vontology(workflow_id)
    assert loaded_definition is not None
    assert loaded_definition.initial_state == start_step_id
    assert set(loaded_definition.states) == {
        start_step_id,
        collect_step_id,
        finish_step_id,
    }
    collect_state = loaded_definition.states[collect_step_id]
    assert collect_state.terminal is False
    assert [action.action_id for action in collect_state.actions] == [
        "workflow_control.context_set"
    ]
    assert collect_state.actions[0].inputs["assignments"]["$context_key"] == (
        "new_assignments"
    )
    assert [transition.to_state for transition in collect_state.transitions] == [
        finish_step_id
    ]

    repeat_result = mod.apply_workflow_authoring_spec(
        workflow_id,
        authoring_spec=authoring_spec,
    )
    repeat_publication = repeat_result["publication"]
    assert repeat_publication["counts"]["errors"] == 0
    assert repeat_publication["counts"]["step_concepts_created"] == 0
    assert repeat_publication["counts"]["mapping_concepts_created"] == 0


def test_build_workflow_catalogue_payload_uses_fast_listing_mode(monkeypatch) -> None:
    class _Registry:
        def all_workflow_ids(self):
            return ["#V#alpha_workflow"]

    seen: dict[str, object] = {}

    monkeypatch.setattr(
        mod,
        "get_shared_workflow_registry_read_only",
        lambda **_kwargs: _Registry(),
    )
    monkeypatch.setattr(
        mod,
        "get_or_build_workflow_registry_inventory_snapshot",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        mod,
        "get_workflow_usage_aggregates_for_workflows",
        lambda _workflow_ids, **_kwargs: {},
    )
    monkeypatch.setattr(
        mod,
        "get_workflow_episode_counts_for_workflows",
        lambda _workflow_ids, **_kwargs: {},
    )

    def _fake_listing_entry(**kwargs):
        seen.update(kwargs)
        return {
            "workflow_id": "#V#alpha_workflow",
            "description": "Alpha workflow",
            "source": "vontology",
            "definition_loaded": False,
            "definition_identity": {"build_state": "pending_lazy_definition"},
        }

    monkeypatch.setattr(mod, "build_workflow_listing_entry", _fake_listing_entry)

    payload = mod.build_workflow_catalogue_payload(
        limit=10,
        namespace=None,
        session_id=None,
        turn_id=None,
        include_designs=True,
    )

    assert payload["count"] == 1
    assert payload["items"][0]["workflow_id"] == "#V#alpha_workflow"
    assert seen["resolve_vontology_metadata"] is False


def test_workflow_studio_operations_are_strictly_scoped_to_ambient_actor(
    monkeypatch,
) -> None:
    actor_user = "#V#cohort_member"
    actor_org = "#V#trusted_org"
    actor_namespace = "#V#cohort_member@trusted_org"
    calls: dict[str, dict[str, object]] = {}

    class _Schedule:
        def __init__(
            self,
            schedule_id: str,
            *,
            user_id: str,
            org_id: str,
            namespace: str,
        ) -> None:
            self.schedule_id = schedule_id
            self.user_id = user_id
            self.org_id = org_id
            self.namespace = namespace

        def to_status_dict(self) -> dict[str, str]:
            return {"schedule_id": self.schedule_id}

    class _Manager:
        def list_schedules(self, **kwargs):
            calls["schedules"] = dict(kwargs)
            return [
                _Schedule(
                    "own-schedule",
                    user_id=actor_user,
                    org_id=actor_org,
                    namespace=actor_namespace,
                ),
                _Schedule(
                    "other-org-schedule",
                    user_id=actor_user,
                    org_id="#V#other_org",
                    namespace="#V#cohort_member@other_org",
                ),
                _Schedule(
                    "other-user-schedule",
                    user_id="#V#other_user",
                    org_id=actor_org,
                    namespace="#V#other_user@trusted_org",
                ),
            ]

        def list_instance_status_dicts(self, **kwargs):
            calls["instances"] = dict(kwargs)
            return [{"instance_id": "own-instance", "status": "running"}]

        def list_event_bindings(self, **_kwargs):
            raise AssertionError("ordinary Studio detail must not read global bindings")

    def _list_traces(**kwargs):
        calls["executions"] = dict(kwargs)
        return [{"execution_id": "own-execution"}]

    def _list_episodes(**kwargs):
        calls["episodes"] = dict(kwargs)
        return [{"episode_id": "own-episode"}]

    monkeypatch.setattr(mod, "get_effective_user_concept_id", lambda: actor_user)
    monkeypatch.setattr(
        mod,
        "get_effective_organisation_concept_id",
        lambda: actor_org,
    )
    monkeypatch.setattr(mod, "get_instance_manager", lambda: _Manager())
    monkeypatch.setattr(mod, "list_recent_workflow_execution_traces", _list_traces)
    monkeypatch.setattr(mod, "list_workflow_use_episodes", _list_episodes)

    payload = mod._build_operations_payload("#V#visible_workflow")

    assert payload["schedules"]["items"] == [{"schedule_id": "own-schedule"}]
    assert payload["instances"]["items"] == [
        {"instance_id": "own-instance", "status": "running"}
    ]
    assert payload["executions"]["items"] == [
        {"execution_id": "own-execution"}
    ]
    assert payload["episodes"]["items"] == [{"episode_id": "own-episode"}]
    assert payload["bindings"] == {
        "items": [],
        "count": 0,
        "diagnostics": [],
        "available": False,
        "reason": "trusted_operator_surface_required",
    }
    assert calls["schedules"] == {
        "user_id": actor_user,
        "workflow_id": "#V#visible_workflow",
        "limit": 200,
    }
    assert calls["instances"] == {
        "user_id": actor_user,
        "org_id": actor_org,
        "namespace": actor_namespace,
        "workflow_id": "#V#visible_workflow",
        "limit": 30,
    }
    assert calls["executions"] == {
        "limit": 12,
        "namespace": actor_namespace,
        "workflow_id": "#V#visible_workflow",
    }
    assert calls["episodes"] == {
        "workflow_id": "#V#visible_workflow",
        "namespace": actor_namespace,
        "strict_namespace_scope": True,
        "limit": 12,
    }


def test_build_workflow_studio_detail_payload_includes_improvement_guidance(
    monkeypatch,
) -> None:
    seen_scopes: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(
        mod,
        "get_effective_user_concept_id",
        lambda: "#V#cohort_member",
    )
    monkeypatch.setattr(
        mod,
        "get_effective_organisation_concept_id",
        lambda: "#V#trusted_org",
    )
    monkeypatch.setattr(
        mod,
        "_load_actor_scoped_runtime_definition",
        lambda _workflow_id: (None, "vontology", object()),
    )
    monkeypatch.setattr(
        mod,
        "build_workflow_process_graph",
        lambda _workflow_id: (
            {"steps": [], "edges": [], "warnings": []},
            [],
        ),
    )
    monkeypatch.setattr(mod, "resolve_workflow_narrative_text", lambda _workflow_id: ("Narrative", "vontology"))
    monkeypatch.setattr(
        mod,
        "build_workflow_listing_entry",
        lambda **_kwargs: {
            "workflow_id": "#V#alpha_workflow",
            "description": "Alpha workflow",
            "source": "vontology",
        },
    )
    monkeypatch.setattr(
        mod,
        "get_workflow_usage_aggregates_for_workflows",
        lambda _ids, **_kwargs: {},
    )
    monkeypatch.setattr(
        mod,
        "classify_workflow_concept_executability",
        lambda _workflow_id: (True, "ok", None),
    )
    monkeypatch.setattr(
        mod,
        "_build_operations_payload",
        lambda _workflow_id: {"instances": {"active_count": 0}},
    )
    def _count_episodes(**kwargs):
        seen_scopes["episode_count"] = dict(kwargs)
        return 2

    monkeypatch.setattr(mod, "count_workflow_use_episodes", _count_episodes)
    monkeypatch.setattr(
        mod,
        "_build_current_policy_payload",
        lambda _workflow_id: {
            "publication_lifecycle": {"phase": "published"},
            "routing_profile": {"role": "execution"},
        },
    )
    monkeypatch.setattr(mod, "_load_workflow_authoring_proposal", lambda _workflow_id: None)
    monkeypatch.setattr(
        mod,
        "_build_authoring_payload",
        lambda **_kwargs: {"available": True, "validation": {"valid": True}},
    )
    def _list_improvements(workflow_id, **kwargs):
        seen_scopes["improvements"] = dict(kwargs)
        return [
            {
                "suggestion_id": "workflow_change_alpha",
                "category": "workflow_change",
                "priority": "high",
                "target_surface": "workflow",
                "target_workflow_id": workflow_id,
                "title": "Repair routing policy",
                "rationale": "Another eligible route existed.",
                "suggested_change": "Tighten routing exemplars.",
                "evidence_refs": ["expected_context.routing_quality_signals"],
                "recursion_level": 0,
                "memory_id": "#V#episode_critique_memory_1",
                "request_id": "req-1838",
                "verdict": "fail",
            }
        ]

    monkeypatch.setattr(
        mod,
        "list_recent_workflow_improvement_suggestions",
        _list_improvements,
    )

    payload = mod.build_workflow_studio_detail_payload(
        "#V#alpha_workflow",
        namespace="#V#cohort_member@trusted_org",
    )

    assert payload["summary"]["improvement_suggestion_count"] == 1
    guidance = payload["improvement_guidance"]
    assert guidance["available"] is True
    assert guidance["count"] == 1
    assert guidance["high_priority_count"] == 1
    assert guidance["items"][0]["category"] == "workflow_change"
    assert seen_scopes["episode_count"]["namespace"] == (
        "#V#cohort_member@trusted_org"
    )
    assert seen_scopes["episode_count"]["strict_namespace_scope"] is True
    assert seen_scopes["improvements"]["namespace"] == (
        "#V#cohort_member@trusted_org"
    )


def test_workflow_studio_detail_fails_closed_when_actor_graph_is_incomplete(
    monkeypatch,
) -> None:
    registry = object()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        mod,
        "get_shared_workflow_registry_read_only",
        lambda **_kwargs: registry,
    )
    monkeypatch.setattr(
        mod,
        "get_effective_user_concept_id",
        lambda: "#V#cohort_member",
    )
    monkeypatch.setattr(
        mod,
        "get_effective_organisation_concept_id",
        lambda: "#V#trusted_org",
    )

    def _resolve(workflow_id: str, **kwargs: object) -> SimpleNamespace:
        calls.append({"workflow_id": workflow_id, **kwargs})
        return SimpleNamespace(
            definition=None,
            error_code="workflow_definition_not_loadable_for_actor",
            registration_source="unknown",
            registry=registry,
        )

    monkeypatch.setattr(mod, "resolve_workflow_definition_from_authority", _resolve)

    with pytest.raises(
        mod.WorkflowStudioAuthorityError,
        match="workflow_definition_not_loadable_for_actor",
    ):
        mod.build_workflow_studio_detail_payload("#V#alpha_workflow")

    assert calls[0]["actor_user_id"] == "#V#cohort_member"
    assert calls[0]["actor_org_id"] == "#V#trusted_org"
    assert calls[0]["use_current_shared_registry"] is False


def test_submit_workflow_authoring_proposal_sets_pending_review_lifecycle(
    monkeypatch,
) -> None:
    stored_payloads: list[dict[str, Any]] = []
    lifecycle_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        mod,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: {
            "success": True,
            "workflow_id": workflow_id,
            "preview": {
                "definition_identity": {"definition_hash": "candidate-hash"},
                "diff_summary": {"changed_state_count": 1},
                "contract_validation": {"valid": True, "errors": []},
            },
        },
    )
    monkeypatch.setattr(
        mod,
        "validate_workflow_candidate",
        lambda workflow_id, **_kwargs: {
            "success": True,
            "candidate_validation": {
                "workflow_id": workflow_id,
                "valid": True,
                "repair_hints": [],
            },
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_authoring_runtime_definition",
        lambda workflow_id: (SimpleNamespace(), "vontology", object()),
    )
    monkeypatch.setattr(
        mod,
        "serialise_workflow_definition_to_authoring_spec",
        lambda _definition: {"workflow_id": "#V#alpha_workflow", "steps": [{"state_id": "start"}]},
    )
    monkeypatch.setattr(mod, "_load_workflow_authoring_proposal", lambda _workflow_id: None)
    monkeypatch.setattr(
        mod,
        "build_workflow_definition_from_authoring_spec",
        lambda spec: SimpleNamespace(workflow_id=spec.get("workflow_id")),
    )
    monkeypatch.setattr(
        mod,
        "build_workflow_authoring_prompt_contract",
        lambda: {"schema_version": "workflow_authoring_prompt_contract.v1"},
    )
    monkeypatch.setattr(
        mod,
        "get_workflow_authoring_prompt_health_status",
        lambda: {"healthy": True},
    )
    monkeypatch.setattr(
        mod,
        "_store_workflow_authoring_proposal",
        lambda workflow_id, proposal_payload, **_kwargs: (
            stored_payloads.append(dict(proposal_payload)) or dict(proposal_payload)
        ),
    )
    monkeypatch.setattr(
        mod,
        "resolve_workflow_publication_lifecycle",
        lambda _workflow_id: ({"phase": "published", "published": True, "routing_eligible": True}, "vontology"),
    )
    monkeypatch.setattr(
        mod,
        "upsert_workflow_publication_lifecycle",
        lambda **kwargs: lifecycle_calls.append(dict(kwargs)) or dict(kwargs),
    )

    result = mod.submit_workflow_authoring_proposal(
        "#V#alpha_workflow",
        authoring_spec={"workflow_id": "#V#alpha_workflow", "steps": [{"state_id": "start"}]},
        base_definition_hash="base-hash",
        session_id="session-1",
        turn_id="turn-1",
        proposed_by="#V#test_user",
        proposal_context={
            "schema_version": "episode_self_improvement_proposal_context.v1",
            "source": "episode_self_improvement_workflow",
        },
    )

    assert result["success"] is True
    assert result["proposal"]["status"] == "pending_review"
    assert result["next_step"] == "approval"
    assert stored_payloads[0]["created_by"] == "#V#test_user"
    assert stored_payloads[0]["proposal_context"]["source"] == "episode_self_improvement_workflow"
    assert lifecycle_calls[0]["review_state"] == "pending_review"
    assert lifecycle_calls[0]["approval_required"] is True
    assert lifecycle_calls[0]["proposal_source_session_id"] == "session-1"


def test_submit_workflow_authoring_proposal_supersedes_previous_pending_review_proposal(
    monkeypatch,
) -> None:
    stored_payloads: list[tuple[dict[str, Any], bool | None]] = []

    monkeypatch.setattr(
        mod,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: {
            "success": True,
            "workflow_id": workflow_id,
            "preview": {
                "definition_identity": {"definition_hash": "candidate-hash"},
                "diff_summary": {"changed_state_count": 1},
                "contract_validation": {"valid": True, "errors": []},
            },
        },
    )
    monkeypatch.setattr(
        mod,
        "validate_workflow_candidate",
        lambda workflow_id, **_kwargs: {
            "success": True,
            "candidate_validation": {
                "workflow_id": workflow_id,
                "valid": True,
                "repair_hints": [],
            },
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_authoring_runtime_definition",
        lambda workflow_id: (SimpleNamespace(), "vontology", object()),
    )
    monkeypatch.setattr(
        mod,
        "serialise_workflow_definition_to_authoring_spec",
        lambda _definition: {"workflow_id": "#V#alpha_workflow", "steps": [{"state_id": "old"}]},
    )
    monkeypatch.setattr(
        mod,
        "_load_workflow_authoring_proposal",
        lambda _workflow_id: {
            "proposal_id": "proposal-1",
            "status": "pending_review",
            "created_at_utc": "2026-04-23T00:00:00+00:00",
            "authoring_spec": {"workflow_id": "#V#alpha_workflow", "steps": [{"state_id": "old"}]},
        },
    )
    monkeypatch.setattr(
        mod,
        "build_workflow_definition_from_authoring_spec",
        lambda spec: SimpleNamespace(workflow_id=spec.get("workflow_id")),
    )
    monkeypatch.setattr(
        mod,
        "build_workflow_authoring_prompt_contract",
        lambda: {"schema_version": "workflow_authoring_prompt_contract.v1"},
    )
    monkeypatch.setattr(
        mod,
        "get_workflow_authoring_prompt_health_status",
        lambda: {"healthy": True},
    )
    monkeypatch.setattr(
        mod,
        "_store_workflow_authoring_proposal",
        lambda workflow_id, proposal_payload, **kwargs: (
            stored_payloads.append((dict(proposal_payload), kwargs.get("update_active_pointer")))
            or dict(proposal_payload)
        ),
    )
    monkeypatch.setattr(
        mod,
        "resolve_workflow_publication_lifecycle",
        lambda _workflow_id: (
            {"phase": "published", "published": True, "routing_eligible": True},
            "vontology",
        ),
    )
    monkeypatch.setattr(
        mod,
        "upsert_workflow_publication_lifecycle",
        lambda **kwargs: dict(kwargs),
    )

    result = mod.submit_workflow_authoring_proposal(
        "#V#alpha_workflow",
        authoring_spec={"workflow_id": "#V#alpha_workflow", "steps": [{"state_id": "start"}]},
    )

    assert result["success"] is True
    assert len(stored_payloads) == 2
    assert stored_payloads[0][0]["proposal_id"] == "proposal-1"
    assert stored_payloads[0][0]["status"] == "superseded"
    assert stored_payloads[0][1] is False
    assert stored_payloads[1][0]["status"] == "pending_review"
    assert stored_payloads[1][0]["proposal_id"] != "proposal-1"
    assert stored_payloads[1][1] is True


def test_record_workflow_authoring_promotion_evaluation_updates_proposal_and_lifecycle(
    monkeypatch,
) -> None:
    lifecycle_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        mod,
        "_load_workflow_authoring_proposal_by_id",
        lambda _workflow_id, _proposal_id: {
            "proposal_id": "proposal-1",
            "status": "pending_review",
            "proposal_source_session_id": "session-1",
            "proposal_source_turn_id": "turn-1",
        },
    )
    monkeypatch.setattr(
        mod,
        "_store_workflow_authoring_proposal",
        lambda _workflow_id, proposal_payload, **_kwargs: dict(proposal_payload),
    )
    monkeypatch.setattr(
        mod,
        "resolve_workflow_publication_lifecycle",
        lambda _workflow_id: (
            {
                "phase": "published",
                "published": True,
                "review_state": "pending_review",
                "approval_required": True,
                "routing_eligible": True,
            },
            "vontology",
        ),
    )
    monkeypatch.setattr(
        mod,
        "resolve_workflow_routing_profile",
        lambda _workflow_id: ({"role": "authoring", "routing_eligible": True}, "vontology"),
    )
    monkeypatch.setattr(
        mod,
        "upsert_workflow_publication_lifecycle",
        lambda **kwargs: lifecycle_calls.append(dict(kwargs)) or dict(kwargs),
    )

    result = mod.record_workflow_authoring_promotion_evaluation(
        "#V#alpha_workflow",
        proposal_id="proposal-1",
        promotion_evaluation={
            "promotion_recommendation": "ready_for_review",
            "summary": "The candidate is structurally valid and reviewable.",
        },
    )

    assert result["success"] is True
    assert result["proposal"]["promotion_evaluation"]["promotion_recommendation"] == (
        "ready_for_review"
    )
    assert lifecycle_calls[0]["promotion_decision"] == "ready_for_review"
    assert lifecycle_calls[0]["review_state"] == "pending_review"


def test_review_workflow_authoring_proposal_approve_publishes_and_updates_lifecycle(
    monkeypatch,
) -> None:
    lifecycle_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        mod,
        "_load_workflow_authoring_proposal",
        lambda _workflow_id: {
            "proposal_id": "proposal-1",
            "status": "pending_review",
            "authoring_spec": {"workflow_id": "#V#alpha_workflow", "steps": [{"state_id": "start"}]},
            "base_definition_hash": "base-hash",
            "proposal_source_session_id": "session-1",
            "proposal_source_turn_id": "turn-1",
        },
    )
    monkeypatch.setattr(
        mod,
        "resolve_workflow_publication_lifecycle",
        lambda _workflow_id: ({"phase": "published", "published": True}, "vontology"),
    )
    monkeypatch.setattr(
        mod,
        "_load_authoring_runtime_definition",
        lambda workflow_id: (SimpleNamespace(), "vontology", object()),
    )
    monkeypatch.setattr(
        mod,
        "serialise_workflow_definition_to_authoring_spec",
        lambda _definition: {"workflow_id": "#V#alpha_workflow", "steps": [{"state_id": "old"}]},
    )
    monkeypatch.setattr(
        mod,
        "apply_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: {
            "workflow_id": workflow_id,
            "publication": {"summary": {"workflows_published": 1}},
            "preview": {"contract_validation": {"valid": True}},
        },
    )
    monkeypatch.setattr(
        mod,
        "build_workflow_definition_from_authoring_spec",
        lambda _spec: SimpleNamespace(
            metadata={
                "routing_profile": {"role": "authoring"},
                "event_bindings": [{"event_type": "concept.created"}],
                "schedule_specs": [{"schedule_type": "interval", "interval_seconds": 300}],
            }
        ),
    )
    monkeypatch.setattr(
        mod,
        "_apply_workflow_policy_metadata",
        lambda **_kwargs: {
            "routing_profile": {"role": "authoring"},
            "event_binding_ids": ["binding-1"],
            "schedule_ids": ["schedule-1"],
        },
    )
    monkeypatch.setattr(
        mod,
        "_store_workflow_authoring_proposal",
        lambda _workflow_id, proposal_payload, **_kwargs: dict(proposal_payload),
    )
    monkeypatch.setattr(
        mod,
        "upsert_workflow_publication_lifecycle",
        lambda **kwargs: lifecycle_calls.append(dict(kwargs)) or dict(kwargs),
    )

    result = mod.review_workflow_authoring_proposal(
        "#V#alpha_workflow",
        action="approve",
        review_reason="Looks safe",
        reviewed_by="#V#reviewer",
        user_id="#V#reviewer",
        org_id="#V#org",
        namespace="#V#reviewer@org",
    )

    assert result["success"] is True
    assert result["review_action"] == "approve"
    assert result["proposal"]["status"] == "approved"
    assert result["metadata_sync"]["event_binding_ids"] == ["binding-1"]
    assert lifecycle_calls[0]["phase"] == "published"
    assert lifecycle_calls[0]["review_state"] == "approved"
    assert lifecycle_calls[0]["schedule_ids"] == ["schedule-1"]


def test_get_workflow_authoring_proposal_by_id_preserves_exact_candidate(
    monkeypatch,
) -> None:
    proposal_one = {
        "proposal_id": "proposal-1",
        "workflow_id": "#V#alpha_workflow",
        "status": "superseded",
        "updated_at_utc": "2026-04-23T00:00:00+00:00",
    }
    proposal_two = {
        "proposal_id": "proposal-2",
        "workflow_id": "#V#alpha_workflow",
        "status": "pending_review",
        "updated_at_utc": "2026-04-23T01:00:00+00:00",
    }

    def _fake_get_texts_for_concept(subject_concept_id, predicate=None, **_kwargs):
        if predicate == "#V#hasActiveWorkflowAuthoringProposalId":
            return [
                {
                    "text": "proposal-2",
                    "relation_id": "pointer-1",
                    "relation_updated_at": "2026-04-23T01:00:00+00:00",
                }
            ]
        if predicate == "#V#hasWorkflowAuthoringProposalJson":
            return [
                {
                    "text": mod.json.dumps(proposal_one, sort_keys=True),
                    "predicate": predicate,
                    "relation_id": "relation-1",
                    "relation_updated_at": "2026-04-23T00:00:00+00:00",
                    "context": {"proposal_id": "proposal-1"},
                },
                {
                    "text": mod.json.dumps(proposal_two, sort_keys=True),
                    "predicate": predicate,
                    "relation_id": "relation-2",
                    "relation_updated_at": "2026-04-23T01:00:00+00:00",
                    "context": {"proposal_id": "proposal-2"},
                },
            ]
        return []

    monkeypatch.setattr(mod, "get_texts_for_concept", _fake_get_texts_for_concept)

    active = mod.get_workflow_authoring_proposal("#V#alpha_workflow")
    exact = mod.get_workflow_authoring_proposal_by_id(
        "#V#alpha_workflow",
        "proposal-1",
    )

    assert active is not None
    assert active["proposal_id"] == "proposal-2"
    assert exact is not None
    assert exact["proposal_id"] == "proposal-1"
    assert exact["status"] == "superseded"
