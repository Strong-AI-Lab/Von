from __future__ import annotations

import json

_RETIRED_WORKFLOW_IDS = {
    "#V#conversation_turn_execution_workflow",
    "#V#turn_completion_gate_workflow",
    "#V#turn_prompt_context_adjudication_workflow",
}
_RETAINED_EXPLICIT_SUPPORT_WORKFLOW_ID = "#V#workflow_experience_context_prelude"
_RETIRED_PROMPT_IDS = {
    "#V#prompt_turn_execution_expected_outcome_inference",
    "#V#turn_prompt_context_adjudication_prompt",
    "#V#chat_turn_classifier_prompt",
    "#V#prompt_turn_execution_narrate_completion_report",
    "#V#prompt_turn_execution_recovery_decision",
}


def test_support_prompt_bootstrap_excludes_universal_controller_prompts(
    monkeypatch,
) -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service

    captured_specs = []

    def ensure_prompt_concept_support(*, prompt_specs, provenance_source):
        captured_specs.extend(prompt_specs)
        assert provenance_source
        return {
            "created_prompt_ids": [],
            "validated_prompt_ids": [
                prompt_spec.concept_id for prompt_spec in prompt_specs
            ],
            "linked_workflow_ids": [],
            "missing_content_prompt_ids": [],
            "errors_by_target": {},
        }

    monkeypatch.setattr(
        service,
        "ensure_prompt_concept_support",
        ensure_prompt_concept_support,
    )
    monkeypatch.setattr(
        service,
        "prompt_concept_has_content",
        lambda _prompt_id: True,
    )

    report = service._ensure_conversation_turn_prompt_support()

    retained_prompt_ids = {
        prompt_spec.concept_id for prompt_spec in captured_specs
    }
    assert retained_prompt_ids
    assert retained_prompt_ids.isdisjoint(_RETIRED_PROMPT_IDS)
    assert report["success"] is True
    assert report["seeded_prompt_ids"] == []


def test_forced_support_seed_publishes_only_independently_useful_prompts(
    monkeypatch,
) -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service

    seeded_ids: list[str] = []
    monkeypatch.setattr(
        service,
        "ensure_prompt_concept_support",
        lambda **_kwargs: {
            "created_prompt_ids": [],
            "validated_prompt_ids": [],
            "linked_workflow_ids": [],
            "missing_content_prompt_ids": [],
            "errors_by_target": {},
        },
    )
    monkeypatch.setattr(
        service,
        "prompt_concept_has_content",
        lambda _prompt_id: True,
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: seeded_ids.append(kwargs["subject_concept_id"]),
    )

    report = service._ensure_conversation_turn_prompt_support(
        force_prompt_seed=True
    )

    assert seeded_ids
    assert set(seeded_ids).isdisjoint(_RETIRED_PROMPT_IDS)
    assert report["seeded_prompt_ids"] == seeded_ids


def test_bootstrap_targets_explicit_workflows_not_the_master_controller(
    monkeypatch,
) -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        service,
        "_ensure_conversation_turn_prompt_support",
        lambda **_kwargs: {"success": True},
    )

    def bootstrap_repo_seed_workflow_bundle(**kwargs):
        captured.update(kwargs)
        return {
            "publication": {"counts": {"errors": 0}},
            "typed_workflow_ids": [],
            "typed_step_ids": [],
            "validation_by_workflow_id": {},
        }

    monkeypatch.setattr(
        service,
        "bootstrap_repo_seed_workflow_bundle",
        bootstrap_repo_seed_workflow_bundle,
    )

    report = service.bootstrap_canonical_conversation_turn_workflows()

    target_ids = set(captured["target_workflow_ids"])
    assert target_ids
    assert target_ids.isdisjoint(_RETIRED_WORKFLOW_IDS)
    assert _RETAINED_EXPLICIT_SUPPORT_WORKFLOW_ID in target_ids
    assert set(report["workflow_ids"]) == target_ids
    assert report["success"] is True


def test_seed_bundle_no_longer_contains_the_controller_tower() -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service

    raw_text = service._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8")
    bundle = json.loads(raw_text)
    workflow_ids = {
        item["workflow_id"]
        for item in bundle["workflows"]
        if isinstance(item, dict) and isinstance(item.get("workflow_id"), str)
    }

    assert workflow_ids.isdisjoint(_RETIRED_WORKFLOW_IDS)
    assert _RETAINED_EXPLICIT_SUPPORT_WORKFLOW_ID in workflow_ids
    for retired_prompt_id in _RETIRED_PROMPT_IDS:
        assert retired_prompt_id not in raw_text


def test_retained_experience_prelude_is_an_explicit_bounded_read_workflow() -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service

    bundle = json.loads(service._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8"))
    prelude = next(
        item
        for item in bundle["workflows"]
        if item.get("workflow_id") == _RETAINED_EXPLICIT_SUPPORT_WORKFLOW_ID
    )
    spec = prelude["publication_spec"]
    steps = {
        step["state_id"]: step
        for step in spec["steps"]
        if isinstance(step, dict) and isinstance(step.get("state_id"), str)
    }

    assert spec["initial_state"] == "prepare_profile"
    assert {
        steps[state_id]["action_id"]
        for state_id in (
            "read_success_guidance",
            "read_failure_guidance",
            "read_exploration_guidance",
        )
    } == {"get_text_relations"}
    assert all(
        steps[state_id]["static_input_bindings"][1] == {
            "key": "limit",
            "value": 5,
        }
        for state_id in (
            "read_success_guidance",
            "read_failure_guidance",
            "read_exploration_guidance",
        )
    )
