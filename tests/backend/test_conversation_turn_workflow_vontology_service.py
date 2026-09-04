from __future__ import annotations

import json

_RETIRED_CONTROLLER_IDS = {
    "#V#conversation_turn_execution_workflow",
    "#V#turn_completion_gate_workflow",
    "#V#turn_prompt_context_adjudication_workflow",
}
_RETIRED_PRELUDE_ID = "#V#workflow_experience_context_prelude"
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

    retained_prompt_ids = {prompt_spec.concept_id for prompt_spec in captured_specs}
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

    report = service._ensure_conversation_turn_prompt_support(force_prompt_seed=True)

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
    monkeypatch.setattr(
        service,
        "_ensure_retired_workflow_tombstones",
        lambda: {"success": True, "workflow_id": _RETIRED_PRELUDE_ID},
    )

    report = service.bootstrap_canonical_conversation_turn_workflows()

    target_ids = set(captured["target_workflow_ids"])
    assert target_ids
    assert target_ids.isdisjoint(_RETIRED_CONTROLLER_IDS)
    assert _RETIRED_PRELUDE_ID in target_ids
    assert set(report["workflow_ids"]) == target_ids
    assert report["retirement"]["workflow_id"] == _RETIRED_PRELUDE_ID
    assert report["success"] is True


def test_seed_bundle_replaces_experience_prelude_with_terminal_tombstone() -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service

    raw_text = service._REPO_SEED_ASSET_PATH.read_text(encoding="utf-8")
    bundle = json.loads(raw_text)
    workflow_ids = {
        item["workflow_id"]
        for item in bundle["workflows"]
        if isinstance(item, dict) and isinstance(item.get("workflow_id"), str)
    }

    assert workflow_ids.isdisjoint(_RETIRED_CONTROLLER_IDS)
    assert _RETIRED_PRELUDE_ID in workflow_ids
    tombstone = next(
        item
        for item in bundle["workflows"]
        if item.get("workflow_id") == _RETIRED_PRELUDE_ID
    )
    assert tombstone["publication_spec"] == {
        "initial_state": "retired",
        "steps": [{"state_id": "retired", "terminal": True}],
    }
    assert "get_text_relations" not in json.dumps(tombstone)
    for retired_prompt_id in _RETIRED_PROMPT_IDS:
        assert retired_prompt_id not in raw_text


def test_retired_prelude_lifecycle_is_idempotent(monkeypatch) -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service
    import src.backend.services.workflow_discovery_service as discovery_service

    expected = dict(service._RETIRED_WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_LIFECYCLE)
    invalidations: list[bool] = []
    monkeypatch.setattr(
        service,
        "resolve_workflow_publication_lifecycle",
        lambda _workflow_id: (expected, "concept_data"),
    )
    monkeypatch.setattr(
        service,
        "upsert_workflow_publication_lifecycle",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected write")),
    )
    monkeypatch.setattr(
        discovery_service,
        "invalidate_workflow_discovery_executability_caches",
        lambda: invalidations.append(True),
    )

    report = service._ensure_retired_workflow_tombstones()

    assert report["success"] is True
    assert report["updated"] is False
    assert report["lifecycle"] == expected
    assert invalidations == [True]


def test_retired_prelude_lifecycle_is_written_and_read_back(monkeypatch) -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service
    import src.backend.services.workflow_discovery_service as discovery_service

    expected = dict(service._RETIRED_WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_LIFECYCLE)
    resolved = iter(
        [
            ({"phase": "published", "published": True}, "concept_data"),
            (expected, "concept_data"),
        ]
    )
    writes: list[dict[str, object]] = []
    invalidations: list[bool] = []
    monkeypatch.setattr(
        service,
        "resolve_workflow_publication_lifecycle",
        lambda _workflow_id: next(resolved),
    )
    monkeypatch.setattr(
        service,
        "upsert_workflow_publication_lifecycle",
        lambda **kwargs: writes.append(kwargs) or expected,
    )
    monkeypatch.setattr(
        discovery_service,
        "invalidate_workflow_discovery_executability_caches",
        lambda: invalidations.append(True),
    )

    report = service._ensure_retired_workflow_tombstones()

    assert report["success"] is True
    assert report["updated"] is True
    assert writes == [{"workflow_id": _RETIRED_PRELUDE_ID, **expected}]
    assert invalidations == [True]
    assert report["lifecycle"] == expected


def test_bootstrap_fails_closed_when_retirement_readback_fails(monkeypatch) -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service

    monkeypatch.setattr(
        service,
        "_ensure_conversation_turn_prompt_support",
        lambda **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        service,
        "bootstrap_repo_seed_workflow_bundle",
        lambda **_kwargs: {
            "publication": {"counts": {"errors": 0}},
            "typed_workflow_ids": [],
            "typed_step_ids": [],
            "validation_by_workflow_id": {},
        },
    )
    monkeypatch.setattr(
        service,
        "_ensure_retired_workflow_tombstones",
        lambda: {"success": False, "error": "readback mismatch"},
    )

    report = service.bootstrap_canonical_conversation_turn_workflows()

    assert report["success"] is False
    assert report["retirement"]["error"] == "readback mismatch"


def test_bootstrap_does_not_retire_unknown_live_prelude_drift(monkeypatch) -> None:
    import src.backend.services.conversation_turn_workflow_vontology_service as service

    monkeypatch.setattr(
        service,
        "_ensure_conversation_turn_prompt_support",
        lambda **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        service,
        "bootstrap_repo_seed_workflow_bundle",
        lambda **_kwargs: {
            "publication": {
                "counts": {"errors": 1},
                "errors_by_workflow_id": {
                    _RETIRED_PRELUDE_ID: (
                        "live_authority_requires_explicit_migration"
                    )
                },
            },
            "typed_workflow_ids": [],
            "typed_step_ids": [],
            "validation_by_workflow_id": {},
        },
    )
    monkeypatch.setattr(
        service,
        "_ensure_retired_workflow_tombstones",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected lifecycle write")),
    )

    report = service.bootstrap_canonical_conversation_turn_workflows()

    assert report["success"] is False
    assert report["retirement"] == {
        "success": False,
        "workflow_id": _RETIRED_PRELUDE_ID,
        "updated": False,
        "skipped": True,
        "error": "retired_workflow_tombstone_not_materialised",
        "publication_error": "live_authority_requires_explicit_migration",
    }
