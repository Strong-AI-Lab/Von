from src.backend.workflows import workflow_studio_service as mod
from types import SimpleNamespace


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


def test_apply_workflow_authoring_spec_updates_existing_workflow_without_create_missing(
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
        "_load_runtime_definition",
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
        "_load_runtime_definition",
        lambda _workflow_id: (None, "unknown", object()),
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
        "_load_runtime_definition",
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
        lambda _workflow_ids: {},
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


def test_build_workflow_studio_detail_payload_includes_improvement_guidance(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mod,
        "_load_runtime_definition",
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
    monkeypatch.setattr(mod, "get_workflow_usage_aggregates_for_workflows", lambda _ids: {})
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
    monkeypatch.setattr(mod, "count_workflow_use_episodes", lambda **_kwargs: 2)
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
    monkeypatch.setattr(
        mod,
        "list_recent_workflow_improvement_suggestions",
        lambda workflow_id, **_kwargs: [
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
        ],
    )

    payload = mod.build_workflow_studio_detail_payload("#V#alpha_workflow")

    assert payload["summary"]["improvement_suggestion_count"] == 1
    guidance = payload["improvement_guidance"]
    assert guidance["available"] is True
    assert guidance["count"] == 1
    assert guidance["high_priority_count"] == 1
    assert guidance["items"][0]["category"] == "workflow_change"


def test_submit_workflow_authoring_proposal_sets_pending_review_lifecycle(
    monkeypatch,
) -> None:
    stored_payloads: list[dict[str, object]] = []
    lifecycle_calls: list[dict[str, object]] = []

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
        "_load_runtime_definition",
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
        lambda workflow_id, proposal_payload: (
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
    )

    assert result["success"] is True
    assert result["proposal"]["status"] == "pending_review"
    assert result["next_step"] == "approval"
    assert stored_payloads[0]["created_by"] == "#V#test_user"
    assert lifecycle_calls[0]["review_state"] == "pending_review"
    assert lifecycle_calls[0]["approval_required"] is True
    assert lifecycle_calls[0]["proposal_source_session_id"] == "session-1"


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
        "_load_runtime_definition",
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
        lambda _workflow_id, proposal_payload: dict(proposal_payload),
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
