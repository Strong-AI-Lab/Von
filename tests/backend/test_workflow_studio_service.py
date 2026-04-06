from src.backend.workflows import workflow_studio_service as mod


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
