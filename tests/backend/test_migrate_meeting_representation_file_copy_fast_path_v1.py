from __future__ import annotations

import copy
import hashlib
from contextlib import contextmanager, nullcontext

import pytest

from scripts import migrate_meeting_representation_file_copy_fast_path_v1 as migration
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
)
from src.backend.workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)


def _sample_authoring_spec() -> dict:
    current_prompt = "Legacy meeting representation prompt."
    current_policy = {
        "allowed_tools": [
            "search_concepts",
            "fetch_concept",
            "get_predicate_incidence",
            "find_relations_with_argument",
            "create_concepts",
            "add_relationship",
            "upsert_text_relation",
            "upsert_singleton_text_relation",
        ],
        "context_fields": [
            {"context_key": "prompt", "label": "Current turn request"},
            {
                "context_key": "meeting_source_text",
                "label": "Partially published source text",
            },
            {
                "context_key": "meeting_source_filename",
                "label": "Partially published source filename",
            },
        ],
        "max_tool_invocations": 24,
        "prompt_candidates": [migration.LEGACY_PRIVATE_PROMPT_CONCEPT_ID],
        "prompt_text": current_prompt,
        "required_tools": [
            "search_concepts",
            "fetch_concept",
            "get_predicate_incidence",
            "create_concepts",
            "add_relationship",
        ],
        "selected_prompt_id": migration.LEGACY_PRIVATE_PROMPT_CONCEPT_ID,
        "tool_argument_defaults": {
            "fetch_concept": {
                "include_concept_preview": True,
                "include_relations_any_arg": True,
                "include_relations_arg1": True,
                "include_text_relations_arg1": True,
                "limit": 80,
            },
            "get_predicate_incidence": {
                "argument_index": "subject",
                "limit": 40,
            },
        },
        "tool_mode": "allowed",
    }
    current_prompt_contract = {
        "resolved_prompt_concept_id": migration.LEGACY_PRIVATE_PROMPT_CONCEPT_ID,
        "requested_prompt_concept_ids": [migration.LEGACY_PRIVATE_PROMPT_CONCEPT_ID],
        "prompt_text": current_prompt,
        "validation_policy": "fail",
    }
    return {
        "workflow_id": migration.WORKFLOW_ID,
        "description": "Represent meetings.",
        "initial_state_key": migration.ROUTE_SOURCE_STATE_ID,
        "steps": [
            {
                "state_id": migration.ROUTE_SOURCE_STATE_ID,
                "concept_id": migration.ROUTE_SOURCE_STATE_ID,
                "terminal": False,
                "conditional_transitions": [
                    {
                        "to_state": migration.READ_FILE_COPY_STATE_ID,
                        "reason": "uploaded_file_copy_available",
                        "condition_spec": {
                            "kind": "context_exists",
                            "key": "file_copy_concept_id",
                            "expected": True,
                        },
                    }
                ],
                "next_state_key": "represent_meeting",
            },
            {
                "state_id": migration.READ_FILE_COPY_STATE_ID,
                "concept_id": migration.READ_FILE_COPY_STATE_ID,
                "action_id": "workflow_mcp.invoke_tool",
                "execution_mode": "deterministic",
                "static_input_bindings": [
                    {"tool_param": "tool_name", "value": "read_file_copy"},
                ],
                "next_state_key": "represent_meeting",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "represent_meeting",
                "action_id": "llm.action",
                "execution_mode": "llm",
                "llm_policy": current_policy,
                "prompt_contract": current_prompt_contract,
                "metadata": {
                    "llm_policy": copy.deepcopy(current_policy),
                    "llm_policies": [copy.deepcopy(current_policy)],
                    "prompt_contract": copy.deepcopy(current_prompt_contract),
                },
                "next_state_key": "completed",
                "on_failure_state_key": "failed",
            },
            {"state_id": "completed", "terminal": True},
            {"state_id": "failed", "terminal": True},
        ],
        "workflow_metadata": {
            "launch_input_contract": {
                "schema_version": "workflow_launch_input_contract.v1",
                "required_inputs": ["prompt"],
                "input_mappings": [
                    {
                        "target_context_key": "prompt",
                        "source_expression": "inputs.prompt",
                        "extractor": "identity",
                        "required": True,
                    }
                ],
            },
            "required_effects_contract": {
                "schema_version": "workflow_required_effects_contract.v1",
                "contract_id": "meeting_representation_materialisation",
                "required_effects": [
                    {
                        "effect_id": "meeting_representation_mutation",
                        "effect_type": "meeting_representation",
                        "required_tools": ["create_concepts", "add_relationship"],
                        "required_tools_match": "all",
                        "activation_required_tools": [
                            "search_concepts",
                            "fetch_concept",
                            "get_predicate_incidence",
                        ],
                        "activation_required_tools_match": "all",
                    }
                ],
            },
        },
    }


def _prompt_snapshot(
    text: str,
    relation_count: int = 1,
    *,
    concept_exists: bool = True,
    is_prompt_instance: bool = True,
    is_global_general: bool = True,
) -> dict:
    return {
        "concept_exists": concept_exists,
        "is_prompt_instance": is_prompt_instance if concept_exists else False,
        "is_global_general": is_global_general if concept_exists else False,
        "specific_to_user": [],
        "specific_to_organisation": [],
        "relation_count": relation_count,
        "relation_id": "prompt-relation" if relation_count else None,
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _step_with_action(spec: dict, action_id: str) -> dict:
    return next(step for step in spec["steps"] if step.get("action_id") == action_id)


def test_rewrite_adds_bounded_optional_file_copy_path_and_is_idempotent() -> None:
    original = _sample_authoring_spec()
    before = copy.deepcopy(original)

    candidate, changed = migration.rewrite_meeting_representation_workflow(original)

    assert changed is True
    assert original == before
    launch = candidate["workflow_metadata"]["launch_input_contract"]
    assert launch["required_inputs"] == ["prompt"]
    assert {
        "target_context_key": "file_copy_concept_id",
        "source_expression": "inputs.file_copy_concept_id",
        "extractor": "identity",
        "required": False,
        "description": (
            "Pass through the optional authenticated file-copy concept ID "
            "created for an uploaded meeting source."
        ),
    } in launch["input_mappings"]

    assert candidate["initial_state_key"] == "represent_meeting"
    candidate_state_ids = {step.get("state_id") for step in candidate["steps"]}
    assert migration.ROUTE_SOURCE_STATE_ID not in candidate_state_ids
    assert migration.READ_FILE_COPY_STATE_ID not in candidate_state_ids
    assert len(candidate["steps"]) == 3
    step = _step_with_action(candidate, "llm.action")

    policy = step["llm_policy"]
    assert "read_file_copy" in policy["allowed_tools"]
    assert "get_predicate_incidence" not in policy["allowed_tools"]
    assert policy["required_tools"] == ["search_concepts"]
    assert policy["tool_argument_defaults"]["read_file_copy"] == {
        "as_text": True,
        "allow_large": False,
        "max_bytes": migration.READ_FILE_COPY_MAX_BYTES,
    }
    assert policy["tool_argument_defaults"]["fetch_concept"] == {
        "include_concept_preview": True,
        "include_relations_arg1": False,
        "include_relations_any_arg": False,
        "include_text_relations_arg1": False,
        "limit": 8,
    }
    assert "get_predicate_incidence" not in policy["tool_argument_defaults"]
    assert policy["tool_argument_defaults"]["find_relations_with_argument"] == {
        "argument_index": "subject",
        "relation_kind": "binary",
        "include_concept_preview": False,
        "include_text_snippets": False,
        "limit": 8,
    }
    context_keys = {field.get("context_key") for field in policy["context_fields"]}
    assert "file_copy_concept_id" in context_keys
    assert "meeting_source_text" not in context_keys
    assert "meeting_source_filename" not in context_keys
    assert policy["prompt_text"].startswith(
        f"[Versioned migration: {migration.MIGRATION_ID}]"
    )
    assert step["prompt_contract"]["prompt_text"] == (
        migration.MEETING_REPRESENTATION_PROMPT
    )
    assert step["metadata"]["llm_policy"] == policy
    assert step["metadata"]["llm_policies"] == [policy]
    assert "#V#documentary_evidence_for" in policy["prompt_text"]
    assert "read back that exact" in policy["prompt_text"]
    assert "your first source action must be exactly one" in policy["prompt_text"]
    assert f"max_bytes={migration.READ_FILE_COPY_MAX_BYTES}" in policy["prompt_text"]
    assert "deterministic predecessor" not in policy["prompt_text"]

    effect = candidate["workflow_metadata"]["required_effects_contract"][
        "required_effects"
    ][0]
    assert effect["required_tools_match"] == "any"
    assert effect["required_tools"] == [
        "add_relationship",
        "upsert_text_relation",
        "upsert_singleton_text_relation",
    ]
    assert effect["activation_required_tools"] == []
    assert "fetch_concept" not in effect["required_tools"]
    assert "get_predicate_incidence" not in effect["required_tools"]
    assert "create_concepts" not in effect["required_tools"]

    definition = build_workflow_definition_from_authoring_spec(candidate)
    validation = validate_workflow_definition_contract(definition=definition)
    assert validation["valid"] is True
    assert definition.initial_state == "represent_meeting"
    assert migration.ROUTE_SOURCE_STATE_ID not in definition.states
    assert migration.READ_FILE_COPY_STATE_ID not in definition.states
    assert definition.states["represent_meeting"].actions[0].action_id == "llm.action"

    second_candidate, second_changed = (
        migration.rewrite_meeting_representation_workflow(candidate)
    )
    assert second_changed is False
    assert second_candidate == candidate


def test_preview_is_default_read_only_and_uses_actor_scoped_workflow_studio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_definition = object()
    previewed: dict = {}

    monkeypatch.setattr(
        migration,
        "override_current_actor",
        lambda **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        migration,
        "load_workflow_definition_from_vontology",
        lambda workflow_id: (
            before_definition if workflow_id == migration.WORKFLOW_ID else None
        ),
    )
    monkeypatch.setattr(
        migration,
        "serialise_workflow_definition_to_authoring_spec",
        lambda definition: copy.deepcopy(_sample_authoring_spec()),
    )
    monkeypatch.setattr(
        migration,
        "_definition_identity",
        lambda definition: {"definition_hash": "before-hash"},
    )
    monkeypatch.setattr(
        migration,
        "_prompt_snapshot",
        lambda: _prompt_snapshot(
            "",
            relation_count=0,
            concept_exists=False,
        ),
    )

    def _preview(workflow_id, *, authoring_spec, base_definition_hash):
        previewed.update(
            {
                "workflow_id": workflow_id,
                "authoring_spec": copy.deepcopy(authoring_spec),
                "base_definition_hash": base_definition_hash,
            }
        )
        return {
            "preview": {
                "definition_identity": {"definition_hash": "candidate-hash"},
                "contract_validation": {"valid": True},
                "diff_summary": {"changed": True},
            }
        }

    monkeypatch.setattr(migration, "preview_workflow_authoring_spec", _preview)
    monkeypatch.setattr(
        migration,
        "apply_workflow_authoring_spec",
        lambda *_args, **_kwargs: pytest.fail("preview must not publish workflow"),
    )
    monkeypatch.setattr(
        migration,
        "upsert_singleton_text_relation",
        lambda **_kwargs: pytest.fail("preview must not publish prompt"),
    )
    monkeypatch.setattr(
        migration,
        "_create_public_prompt_concept",
        lambda: pytest.fail("preview must not create prompt concept"),
    )
    monkeypatch.setattr(
        migration,
        "suppress_event_workflow_launches",
        lambda _reason: pytest.fail("preview must not enter mutation scope"),
    )

    result = migration.run_migration(apply=False)

    assert result["mode"] == "preview"
    assert result["publishes_to_vontology"] is False
    assert result["contract_valid"] is True
    assert result["workflow_changed"] is True
    assert result["prompt_changed"] is True
    assert result["prompt_concept_creation_required"] is True
    assert previewed["workflow_id"] == migration.WORKFLOW_ID
    assert previewed["base_definition_hash"] == "before-hash"
    preview_llm_step = _step_with_action(previewed["authoring_spec"], "llm.action")
    assert "read_file_copy" in preview_llm_step["llm_policy"]["allowed_tools"]
    assert previewed["authoring_spec"]["initial_state_key"] == "represent_meeting"
    preview_state_ids = {
        step.get("state_id") for step in previewed["authoring_spec"]["steps"]
    }
    assert migration.ROUTE_SOURCE_STATE_ID not in preview_state_ids
    assert migration.READ_FILE_COPY_STATE_ID not in preview_state_ids


def test_apply_previews_then_publishes_prompt_and_verifies_canonical_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before_definition = object()
    refreshed_definition = object()
    after_definition = object()
    before_spec = _sample_authoring_spec()
    candidate_spec, _changed = migration.rewrite_meeting_representation_workflow(
        before_spec
    )
    refreshed_spec = copy.deepcopy(before_spec)
    refreshed_spec["description"] = "Canonical workflow after prompt publication."
    refreshed_candidate_spec, refreshed_changed = (
        migration.rewrite_meeting_representation_workflow(refreshed_spec)
    )
    assert refreshed_changed is True
    load_results = iter(
        [
            ("load_before", before_definition),
            ("load_refreshed", refreshed_definition),
            ("load_after", after_definition),
        ]
    )
    prompt_results = iter(
        [
            _prompt_snapshot("", relation_count=0, concept_exists=False),
            _prompt_snapshot(migration.MEETING_REPRESENTATION_PROMPT),
        ]
    )
    events: list[str] = []
    actor_context: dict = {}
    prompt_upsert: dict = {}
    prompt_create: dict = {}
    apply_call: dict = {}

    def _actor(**kwargs):
        actor_context.update(kwargs)
        return nullcontext()

    monkeypatch.setattr(migration, "override_current_actor", _actor)

    def _load(_workflow_id):
        event, definition = next(load_results)
        events.append(event)
        return definition

    monkeypatch.setattr(
        migration,
        "load_workflow_definition_from_vontology",
        _load,
    )
    monkeypatch.setattr(
        migration,
        "serialise_workflow_definition_to_authoring_spec",
        lambda definition: copy.deepcopy(
            before_spec
            if definition is before_definition
            else refreshed_spec
            if definition is refreshed_definition
            else refreshed_candidate_spec
        ),
    )
    monkeypatch.setattr(
        migration,
        "_definition_identity",
        lambda definition: {
            "definition_hash": (
                "before-hash"
                if definition is before_definition
                else "refreshed-base-hash"
                if definition is refreshed_definition
                else "after-hash"
            )
        },
    )
    monkeypatch.setattr(migration, "_prompt_snapshot", lambda: next(prompt_results))

    preview_calls: list[dict] = []

    def _preview(workflow_id, *, authoring_spec, base_definition_hash):
        preview_calls.append(
            {
                "workflow_id": workflow_id,
                "authoring_spec": copy.deepcopy(authoring_spec),
                "base_definition_hash": base_definition_hash,
            }
        )
        is_refreshed = len(preview_calls) == 2
        events.append("refreshed_preview" if is_refreshed else "preview")
        return {
            "preview": {
                "definition_identity": {
                    "definition_hash": (
                        "refreshed-candidate-hash" if is_refreshed else "candidate-hash"
                    )
                },
                "contract_validation": {"valid": True},
                "diff_summary": {"changed": True},
            }
        }

    def _apply(workflow_id, *, authoring_spec, base_definition_hash):
        events.append("apply_workflow")
        apply_call.update(
            {
                "workflow_id": workflow_id,
                "authoring_spec": copy.deepcopy(authoring_spec),
                "base_definition_hash": base_definition_hash,
            }
        )
        return {
            "publication": {
                "counts": {"errors": 0},
                "published_workflow_ids": [migration.WORKFLOW_ID],
            }
        }

    def _upsert(**kwargs):
        events.append("upsert_prompt")
        prompt_upsert.update(kwargs)
        return {"relation_id": "prompt-relation"}

    @contextmanager
    def _suppression(reason):
        events.append(f"suppress_enter:{reason}")
        try:
            yield
        finally:
            events.append("suppress_exit")

    def _create_prompt():
        events.append("create_prompt")
        prompt_create.update(
            {
                "name": migration.PROMPT_CONCEPT_NAME,
                "concept_id": migration.PROMPT_CONCEPT_ID,
                "parent_concept_ids": [migration.PROMPT_TYPE_CONCEPT_ID],
                "create_as_instance": True,
                "visibility_scope_mode": "global_general",
            }
        )

    monkeypatch.setattr(migration, "preview_workflow_authoring_spec", _preview)
    monkeypatch.setattr(migration, "apply_workflow_authoring_spec", _apply)
    monkeypatch.setattr(migration, "upsert_singleton_text_relation", _upsert)
    monkeypatch.setattr(migration, "_create_public_prompt_concept", _create_prompt)
    monkeypatch.setattr(migration, "suppress_event_workflow_launches", _suppression)

    result = migration.run_migration(apply=True)

    assert events == [
        "load_before",
        "preview",
        f"suppress_enter:{migration.MIGRATION_ID}",
        "create_prompt",
        "upsert_prompt",
        "load_refreshed",
        "refreshed_preview",
        "apply_workflow",
        "suppress_exit",
        "load_after",
    ]
    assert actor_context == {
        "user_concept_id": migration.DEFAULT_ACTOR_USER_ID,
        "organisation_concept_id": (migration.DEFAULT_ACTOR_ORGANISATION_ID),
    }
    assert prompt_upsert["subject_concept_id"] == migration.PROMPT_CONCEPT_ID
    assert prompt_upsert["predicate"] == migration.PROMPT_PREDICATE
    assert prompt_upsert["text"] == migration.MEETING_REPRESENTATION_PROMPT
    assert prompt_upsert["context"]["migration_id"] == migration.MIGRATION_ID
    assert prompt_create == {
        "name": migration.PROMPT_CONCEPT_NAME,
        "concept_id": migration.PROMPT_CONCEPT_ID,
        "parent_concept_ids": [migration.PROMPT_TYPE_CONCEPT_ID],
        "create_as_instance": True,
        "visibility_scope_mode": "global_general",
    }
    assert result["prompt_concept_creation"] == {
        "concept_id": migration.PROMPT_CONCEPT_ID,
        "created": True,
        "parent_concept_id": migration.PROMPT_TYPE_CONCEPT_ID,
        "visibility_scope_mode": "global_general",
    }
    assert result["mode"] == "apply"
    assert result["refreshed_base_definition_hash"] == "refreshed-base-hash"
    assert result["refreshed_candidate_definition_hash"] == "refreshed-candidate-hash"
    assert result["refreshed_contract_valid"] is True
    assert preview_calls[1] == {
        "workflow_id": migration.WORKFLOW_ID,
        "authoring_spec": refreshed_candidate_spec,
        "base_definition_hash": "refreshed-base-hash",
    }
    assert apply_call == preview_calls[1]
    assert apply_call["authoring_spec"] != candidate_spec
    assert result["canonical_readback_verified"] is True
    assert result["readback_definition_hash"] == "after-hash"


def test_apply_rejects_invalid_preview_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migration,
        "override_current_actor",
        lambda **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        migration,
        "load_workflow_definition_from_vontology",
        lambda _workflow_id: object(),
    )
    monkeypatch.setattr(
        migration,
        "serialise_workflow_definition_to_authoring_spec",
        lambda _definition: _sample_authoring_spec(),
    )
    monkeypatch.setattr(
        migration,
        "_definition_identity",
        lambda _definition: {"definition_hash": "before-hash"},
    )
    monkeypatch.setattr(
        migration,
        "_prompt_snapshot",
        lambda: _prompt_snapshot("", relation_count=0, concept_exists=False),
    )
    monkeypatch.setattr(
        migration,
        "preview_workflow_authoring_spec",
        lambda *_args, **_kwargs: {
            "preview": {
                "definition_identity": {"definition_hash": "candidate-hash"},
                "contract_validation": {
                    "valid": False,
                    "errors": ["invalid candidate"],
                },
            }
        },
    )
    monkeypatch.setattr(
        migration,
        "apply_workflow_authoring_spec",
        lambda *_args, **_kwargs: pytest.fail("invalid preview must not publish"),
    )
    monkeypatch.setattr(
        migration,
        "upsert_singleton_text_relation",
        lambda **_kwargs: pytest.fail("invalid preview must not update prompt"),
    )
    monkeypatch.setattr(
        migration,
        "_create_public_prompt_concept",
        lambda: pytest.fail("invalid preview must not create prompt"),
    )
    monkeypatch.setattr(
        migration,
        "suppress_event_workflow_launches",
        lambda _reason: pytest.fail("invalid preview must not enter mutation scope"),
    )

    with pytest.raises(ValueError, match="workflow_authoring_preview_invalid"):
        migration.run_migration(apply=True)


def test_apply_already_current_is_idempotent_without_mutation_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_definition = object()
    current_spec, changed = migration.rewrite_meeting_representation_workflow(
        _sample_authoring_spec()
    )
    assert changed is True
    load_calls: list[str] = []
    preview_calls: list[str] = []

    monkeypatch.setattr(
        migration,
        "override_current_actor",
        lambda **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        migration,
        "load_workflow_definition_from_vontology",
        lambda workflow_id: load_calls.append(workflow_id) or current_definition,
    )
    monkeypatch.setattr(
        migration,
        "serialise_workflow_definition_to_authoring_spec",
        lambda _definition: copy.deepcopy(current_spec),
    )
    monkeypatch.setattr(
        migration,
        "_definition_identity",
        lambda _definition: {"definition_hash": "current-hash"},
    )
    monkeypatch.setattr(
        migration,
        "_prompt_snapshot",
        lambda: _prompt_snapshot(migration.MEETING_REPRESENTATION_PROMPT),
    )
    monkeypatch.setattr(
        migration,
        "preview_workflow_authoring_spec",
        lambda workflow_id, **_kwargs: (
            preview_calls.append(workflow_id)
            or {
                "preview": {
                    "definition_identity": {"definition_hash": "current-hash"},
                    "contract_validation": {"valid": True},
                    "diff_summary": {"changed": False},
                }
            }
        ),
    )
    monkeypatch.setattr(
        migration,
        "apply_workflow_authoring_spec",
        lambda *_args, **_kwargs: pytest.fail("current workflow must not publish"),
    )
    monkeypatch.setattr(
        migration,
        "upsert_singleton_text_relation",
        lambda **_kwargs: pytest.fail("current prompt must not upsert"),
    )
    monkeypatch.setattr(
        migration,
        "_create_public_prompt_concept",
        lambda: pytest.fail("current prompt concept must not be created"),
    )
    monkeypatch.setattr(
        migration,
        "suppress_event_workflow_launches",
        lambda _reason: pytest.fail("current apply must not enter mutation scope"),
    )

    result = migration.run_migration(apply=True)

    assert result["changed"] is False
    assert result["workflow_publication_skipped"] == "already_current"
    assert result["prompt_publication_skipped"] == "already_current"
    assert result["prompt_concept_creation"]["created"] is False
    assert result["canonical_readback_verified"] is True
    assert load_calls == [migration.WORKFLOW_ID, migration.WORKFLOW_ID]
    assert preview_calls == [migration.WORKFLOW_ID]
