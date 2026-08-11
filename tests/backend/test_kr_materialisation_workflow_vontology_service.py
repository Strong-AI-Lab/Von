from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from scripts import publish_kr_materialisation_workflows as publish_cli
from src.backend.services import kr_materialisation_workflow_vontology_service as mod
from src.backend.services import workflow_repo_seed_bootstrap as seed_bootstrap
from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.engine import _normalise_retry_policy_spec
from src.backend.workflows.execution_contracts import (
    WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
)
from src.backend.workflows.metadata_validation import (
    validate_state_metadata_post_action,
)
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows import workflow_repo_seed_export_service as export_service

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SEED_BUNDLE_PATH = (
    _PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "kr_materialisation_workflow_seed_bundle.json"
)
_PLAN_PROMPT_SEED_PATH = (
    _PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_kr_design_materialisation_plan_seed.md"
)
_RELATIONSHIP_PROMPT_SEED_PATH = (
    _PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_kr_relationship_endpoint_resolution_seed.md"
)


def _iter_publication_steps(bundle: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for workflow in bundle.get("workflows") or []:
        if not isinstance(workflow, Mapping):
            continue
        publication_spec = workflow.get("publication_spec")
        if not isinstance(publication_spec, Mapping):
            continue
        for step in publication_spec.get("steps") or []:
            if isinstance(step, Mapping):
                yield step


def _publication_step(
    bundle: Mapping[str, Any],
    *,
    workflow_id: str,
    state_id: str,
) -> Mapping[str, Any]:
    workflow = next(
        row
        for row in bundle.get("workflows") or []
        if isinstance(row, Mapping) and row.get("workflow_id") == workflow_id
    )
    publication_spec = workflow.get("publication_spec")
    assert isinstance(publication_spec, Mapping)
    return next(
        row
        for row in publication_spec.get("steps") or []
        if isinstance(row, Mapping) and row.get("state_id") == state_id
    )


def _validate_context_set_writes(
    *,
    step: Mapping[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    bindings = dict(step.get("static_input_bindings") or [])
    assignments = bindings.get("assignments")
    assert isinstance(assignments, list)
    registry = ActionRegistry()
    register_control_flow_actions(
        registry,
        definition_loader=lambda _workflow_id: None,
    )
    context_before = dict(context)
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={"assignments": assignments},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )
    assert result.status == "success"
    context.update(result.outputs)
    validation = validate_state_metadata_post_action(
        state_id=str(step.get("state_id") or ""),
        metadata={"writes_context_keys": step.get("writes_context_keys") or []},
        context_before=context_before,
        context_after=context,
    )
    assert validation.ok is True
    return context


def _prompt_seed_text_by_id() -> dict[str, str]:
    return {
        mod.PROMPT_KR_DESIGN_MATERIALISATION_PLAN_ID: (
            _PLAN_PROMPT_SEED_PATH.read_text(encoding="utf-8").strip()
        ),
        mod.PROMPT_KR_RELATIONSHIP_ENDPOINT_RESOLUTION_ID: (
            _RELATIONSHIP_PROMPT_SEED_PATH.read_text(encoding="utf-8").strip()
        ),
    }


def _base_prompt_support_report() -> dict[str, Any]:
    return {
        "success": True,
        "created_prompt_ids": [],
        "linked_workflow_ids": [],
        "validated_prompt_ids": [],
        "missing_content_prompt_ids": [],
        "errors_by_target": {},
    }


def test_kr_seed_bundle_uses_prompt_concepts_not_inline_prompt_text() -> None:
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    rendered_bundle = json.dumps(bundle, sort_keys=True)

    assert "prompt_text" not in rendered_bundle
    assert "response_contract_text" not in rendered_bundle

    llm_steps = [
        step
        for step in _iter_publication_steps(bundle)
        if step.get("action_id") == "llm.action"
    ]
    assert len(llm_steps) == 1
    prompt_ids_by_state = {
        step.get("state_id"): tuple(step.get("prompt_concept_ids") or ())
        for step in llm_steps
    }
    llm_policy_by_state = {
        step.get("state_id"): step.get("llm_policy") or {} for step in llm_steps
    }
    assert prompt_ids_by_state["extract_kr_materialisation_plan"] == (
        mod.PROMPT_KR_DESIGN_MATERIALISATION_PLAN_ID,
    )
    assert all(
        policy.get("suppress_raw_io_logging") is True
        for policy in llm_policy_by_state.values()
    )
    resolution_step = _publication_step(
        bundle,
        workflow_id=mod.KR_DESIGN_MATERIALISATION_WORKFLOW_ID,
        state_id="resolve_relationship_specs",
    )
    assert resolution_step["action_id"] == (
        "workflow_control.kr_relationship_resolution"
    )
    assert resolution_step["execution_mode"] == "deterministic"
    assert "prompt_concept_ids" not in resolution_step
    assert "llm_policy" not in resolution_step


def test_kr_prompt_seed_assets_hold_operational_prompt_content() -> None:
    plan_prompt = _PLAN_PROMPT_SEED_PATH.read_text(encoding="utf-8")
    relationship_prompt = _RELATIONSHIP_PROMPT_SEED_PATH.read_text(encoding="utf-8")

    assert "materialise bounded knowledge representation designs" in plan_prompt
    assert (
        "Return JSON only with keys: decision ('materialise' or 'block')" in plan_prompt
    )
    assert "`required_description_fragments` verbatim exactly once" in plan_prompt
    assert "copy it into no other slot" in plan_prompt
    assert "Resolve KR relationship endpoint references" in relationship_prompt
    assert "use concept_exists for the bounded existence and access check" in (
        relationship_prompt
    )
    assert "request no incoming structural relations, text relations, or concept previews" in (
        relationship_prompt
    )
    assert "Do not use broad lexical search in this stage" in relationship_prompt
    assert "Block and report that exact-ID lookup failure instead" in (
        relationship_prompt
    )
    assert (
        "Return JSON only with keys: decision ('assert', 'skip', or 'block')"
        in relationship_prompt
    )


def test_kr_seed_applies_optional_guard_before_each_write_phase() -> None:
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    assert bundle["seed_version"] == "9"
    assert bundle["known_legacy_authority_payload_sha256_by_seed_version"] == {
        mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID: {
            "6": [
                "c027e848fe7860016bdc91fd1efa5809c3cfb57e667eb021bc638d1cfb34d8d8"
            ]
        },
        mod.KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID: {
            "8": [
                "69904fc11dd094666fc5c429329b923e834d18cce51ae099b541765cdd5352d7"
            ]
        },
        mod.KR_DESIGN_MATERIALISATION_WORKFLOW_ID: {
            "7": [
                "217607c88af869b891bba73bd4f776bec1297df91445c71ebff8aa84cbd41f1e"
            ],
            "8": [
                "032a62452010744f5988f1e66e8781ff05032b1a0e537db62e7f3864489a16b5"
            ]
        }
    }
    assert (
        mod._REVIEWED_LEGACY_PROMPT_CONTENT_SHA256_BY_TARGET_SEED_VERSION
        == {
            "9": {
                mod.PROMPT_KR_DESIGN_MATERIALISATION_PLAN_ID: {
                    "7": (
                        "cfadd26376e176bbd87bffce74cab180edcd12b03d1e10ca9ba2bfb7501ed8a2",
                    )
                },
                mod.PROMPT_KR_RELATIONSHIP_ENDPOINT_RESOLUTION_ID: {
                    "7": (
                        "1600a90774e9a39d13d88809e581c631ac99f1e62b5dc16d2743027b692db6e5",
                    )
                }
            }
        }
    )
    workflow = next(
        row
        for row in bundle["workflows"]
        if row["workflow_id"] == mod.KR_DESIGN_MATERIALISATION_WORKFLOW_ID
    )
    states = {row["state_id"]: row for row in workflow["publication_spec"]["steps"]}

    assert workflow["launch_input_contract"]["excluded_ambient_input_keys"] == [
        "invocations"
    ]
    assert any(
        row["target_context_key"] == "kr_materialisation_guard"
        and row["required"] is False
        for row in workflow["launch_input_contract"]["input_mappings"]
    )
    assert (
        states["extract_kr_materialisation_plan"]["conditional_transitions"][0][
            "to_state"
        ]
        == "validate_materialisation_plan"
    )
    assert states["validate_materialisation_plan"]["action_id"] == (
        "workflow_control.kr_materialisation_guard"
    )
    assert states["validate_materialisation_plan"]["static_input_bindings"] == [
        ["phase", "plan"]
    ]
    guard_outputs = {
        row["tool_output_field"]: row["context_key"]
        for row in states["validate_materialisation_plan"]["tool_output_mapping_specs"]
    }
    assert guard_outputs["duplicate_resolution_mode"] == (
        "kr_concept_duplicate_resolution_mode"
    )
    assert (
        "kr_concept_duplicate_resolution_mode"
        in states["validate_materialisation_plan"]["writes_context_keys"]
    )
    concept_item = next(
        row
        for row in bundle["workflows"]
        if row["workflow_id"] == mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID
    )
    concept_item_states = {
        row["state_id"]: row for row in concept_item["publication_spec"]["steps"]
    }
    duplicate_resolution_mapping = next(
        row
        for row in concept_item_states["create_concept"]["context_input_mapping_specs"]
        if row["tool_param"] == "duplicate_resolution_mode"
    )
    assert duplicate_resolution_mapping == {
        "concept_id": (
            "#V#workflow_mapping_kr_design_concept_materialisation_item_workflow_"
            "create_concept_kr_concept_duplicate_resolution_mode_to_"
            "duplicate_resolution_mode_parameter"
        ),
        "context_key": "kr_concept_duplicate_resolution_mode",
        "required": False,
        "tool_param": "duplicate_resolution_mode",
    }
    assert not any(
        (
            isinstance(binding, list)
            and len(binding) == 2
            and binding[0] == "duplicate_resolution_mode"
        )
        for binding in concept_item_states["create_concept"]["static_input_bindings"]
    )
    description_outputs = {
        row["context_key"]: row["tool_output_field"]
        for row in concept_item_states["attach_description"][
            "tool_output_mapping_specs"
        ]
    }
    assert description_outputs["kr_description_relation_id"] == "kept_relation_id"
    assert description_outputs["kr_description_text_value_id"] == "text_value_id"
    expected_readback_retry = {
        "backoff_policy": "fixed",
        "initial_delay_ms": 1000,
        "max_attempts": 2,
        "max_delay_ms": 1000,
        "retry_on_outcomes": ["failure"],
        "schema_version": "workflow_step_retry_policy.v1",
    }
    assert (
        concept_item_states["read_back_concept"]["retry_policy"]
        == expected_readback_retry
    )
    assert (
        concept_item_states["read_back_text_relations"]["retry_policy"]
        == expected_readback_retry
    )
    relationship_item = next(
        row
        for row in bundle["workflows"]
        if row["workflow_id"] == mod.KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID
    )
    relationship_item_states = {
        row["state_id"]: row
        for row in relationship_item["publication_spec"]["steps"]
    }
    assert (
        relationship_item_states["read_back_source"]["retry_policy"]
        == expected_readback_retry
    )
    assert (
        relationship_item_states["read_back_target"]["retry_policy"]
        == expected_readback_retry
    )
    concept_iteration_bindings = dict(
        states["materialise_concepts"]["static_input_bindings"]
    )
    assert concept_iteration_bindings["max_concurrency"] == 1
    assert concept_iteration_bindings["stop_on_error"] is True
    assert (
        states["resolve_relationship_specs"]["conditional_transitions"][0]["to_state"]
        == "validate_resolved_relationships"
    )
    assert states["validate_resolved_relationships"]["action_id"] == (
        "workflow_control.kr_materialisation_guard"
    )
    assert states["validate_resolved_relationships"]["static_input_bindings"] == [
        ["phase", "resolved_relationships"]
    ]


def test_main_kr_materialisation_workflow_is_explicit_only() -> None:
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    workflow = next(
        row
        for row in bundle["workflows"]
        if row["workflow_id"] == mod.KR_DESIGN_MATERIALISATION_WORKFLOW_ID
    )
    text_relations = {
        row["predicate"]: json.loads(row["text"])
        for row in workflow["text_relations"]
    }

    lifecycle = text_relations["#V#hasWorkflowLifecycleJson"]
    routing = text_relations["#V#hasWorkflowRoutingProfileJson"]
    assert lifecycle["routing_eligible"] is False
    assert lifecycle["reviewed_by"] == "Codex"
    assert lifecycle["review_reason"] == (
        "2026-08-10 deterministic relationship resolution and exact read-back repair"
    )
    assert routing["routing_eligible"] is False
    assert routing["explicit_workflow_context_required"] is True


def test_kr_relationship_child_requires_exact_readback_before_completion() -> None:
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    relationship_workflow = next(
        row
        for row in bundle["workflows"]
        if row["workflow_id"]
        == mod.KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID
    )
    states = {
        row["state_id"]: row
        for row in relationship_workflow["publication_spec"]["steps"]
    }

    assert states["read_back_target"]["next_state"] == (
        "validate_relationship_readback"
    )
    verifier = states["validate_relationship_readback"]
    assert verifier["action_id"] == "workflow_control.kr_relationship_readback"
    assert verifier["conditional_transitions"][0] == {
        "condition_spec": {
            "key": "kr_relationship_readback_verified",
            "kind": "context_flag",
            "expected": True,
        },
        "reason": "exact_relationship_readback_verified",
        "to_state": "emit_relationship_payload",
    }
    assert verifier["conditional_transitions"][1]["to_state"] == (
        "summarise_blocker"
    )
    emitted_keys = {
        row["key"]
        for row in dict(states["emit_relationship_payload"]["static_input_bindings"])[
            "assignments"
        ]
    }
    assert {
        "kr_relationship_readback_verified",
        "kr_relationship_verified_relationship",
    }.issubset(emitted_keys)


def test_kr_relationship_resolution_is_a_bounded_deterministic_join() -> None:
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    plan_step = _publication_step(
        bundle,
        workflow_id=mod.KR_DESIGN_MATERIALISATION_WORKFLOW_ID,
        state_id="extract_kr_materialisation_plan",
    )
    resolution_step = _publication_step(
        bundle,
        workflow_id=mod.KR_DESIGN_MATERIALISATION_WORKFLOW_ID,
        state_id="resolve_relationship_specs",
    )

    plan_policy = plan_step["llm_policy"]
    assert plan_policy["allowed_tools"] == ["search_concepts", "fetch_concept"]
    assert plan_policy["required_tools"] == ["search_concepts"]
    assert plan_policy["tool_argument_defaults"]["fetch_concept"] == {
        "include_relations_any_arg": True,
        "include_text_relations_arg1": True,
        "limit": 50,
    }
    plan_context_fields = {
        row["context_key"] for row in plan_policy["context_fields"]
    }
    assert "kr_materialisation_guard" in plan_context_fields

    assert resolution_step["action_id"] == (
        "workflow_control.kr_relationship_resolution"
    )
    assert resolution_step["execution_mode"] == "deterministic"
    assert "llm_policy" not in resolution_step
    assert "validation_policy" not in resolution_step
    assert {
        row["tool_output_field"]
        for row in resolution_step["tool_output_mapping_specs"]
    } == {"blocking_reason", "decision", "resolved_relationship_specs"}


def test_kr_relationship_resolution_action_round_trips_and_detects_v8_drift() -> None:
    raw_bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    bundle = authority_service.load_repo_seed_workflow_bundle(_SEED_BUNDLE_PATH)
    workflow_id = mod.KR_DESIGN_MATERIALISATION_WORKFLOW_ID
    expected_spec = bundle["publication_specs"][workflow_id]
    definition = authority_service._build_definition_from_publication_spec(
        workflow_id=workflow_id,
        spec=expected_spec,
    )
    exported = export_service._build_publication_spec_payload_from_definition(
        workflow_id=workflow_id,
        definition=definition,
    )
    exported_steps = {step["state_id"]: step for step in exported["steps"]}
    raw_step = _publication_step(
        raw_bundle,
        workflow_id=workflow_id,
        state_id="resolve_relationship_specs",
    )

    assert exported_steps["resolve_relationship_specs"]["action_id"] == (
        raw_step["action_id"]
    )
    assert exported_steps["resolve_relationship_specs"]["execution_mode"] == (
        "deterministic"
    )
    assert "llm_policy" not in exported_steps["resolve_relationship_specs"]

    legacy_steps = tuple(
        replace(
            step,
            action_id="llm.action",
            execution_mode="llm",
            prompt_concept_ids=(
                mod.PROMPT_KR_RELATIONSHIP_ENDPOINT_RESOLUTION_ID,
            ),
            llm_policy={"tool_mode": "allowed"},
        )
        if step.state_id == "resolve_relationship_specs"
        else step
        for step in expected_spec.steps
    )
    legacy_definition = authority_service._build_definition_from_publication_spec(
        workflow_id=workflow_id,
        spec=replace(expected_spec, steps=legacy_steps),
    )

    assert not seed_bootstrap._materialisation_matches_publication_spec(
        loaded_definition=legacy_definition,
        workflow_id=workflow_id,
        publication_spec=expected_spec,
    )


def test_kr_seed_suppresses_event_fan_out_only_for_owned_mutations() -> None:
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    suppression_states: set[tuple[str, str]] = set()

    for workflow in bundle["workflows"]:
        for step in workflow["publication_spec"]["steps"]:
            bindings = dict(step.get("static_input_bindings") or [])
            if "suppress_event_workflow_launches" not in bindings:
                continue
            assert bindings["suppress_event_workflow_launches"] is True
            suppression_states.add((workflow["workflow_id"], step["state_id"]))

    assert suppression_states == {
        (
            mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
            "create_concept",
        ),
        (
            mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
            "assert_type_parent_relationship",
        ),
        (
            mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
            "assert_instance_parent_relationship",
        ),
        (
            mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
            "attach_description",
        ),
        (
            mod.KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID,
            "assert_relationship",
        ),
    }


def test_kr_readbacks_avoid_expensive_relation_expansion() -> None:
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    concept_readback = _publication_step(
        bundle,
        workflow_id=mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
        state_id="read_back_concept",
    )
    text_readback = _publication_step(
        bundle,
        workflow_id=mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
        state_id="read_back_text_relations",
    )

    assert dict(concept_readback["static_input_bindings"]) == {
        "tool_name": "fetch_concept"
    }
    assert {
        mapping["tool_output_field"]
        for mapping in concept_readback["tool_output_mapping_specs"]
    } == {"concept_id", "relationships"}
    assert concept_readback["next_state"] == "read_back_text_relations"
    assert dict(text_readback["static_input_bindings"]) == {
        "language": "en-NZ",
        "limit": 10,
        "predicate": "hasDescription",
        "tool_name": "get_text_relations",
    }
    for state_id in ("read_back_source", "read_back_target"):
        relationship_readback = _publication_step(
            bundle,
            workflow_id=mod.KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID,
            state_id=state_id,
        )
        assert dict(relationship_readback["static_input_bindings"]) == {
            "tool_name": "fetch_concept"
        }
        assert {
            mapping["tool_output_field"]
            for mapping in relationship_readback["tool_output_mapping_specs"]
        } == {"concept_id", "relationships"}


def test_kr_retry_policy_round_trips_in_canonical_loader_shape() -> None:
    raw_bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    bundle = authority_service.load_repo_seed_workflow_bundle(_SEED_BUNDLE_PATH)
    readback_states_by_workflow = {
        mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID: (
            "read_back_concept",
            "read_back_text_relations",
        ),
        mod.KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID: (
            "read_back_source",
            "read_back_target",
        ),
    }

    for workflow_id, state_ids in readback_states_by_workflow.items():
        publication_spec = bundle["publication_specs"][workflow_id]
        definition = authority_service._build_definition_from_publication_spec(
            workflow_id=workflow_id,
            spec=publication_spec,
        )
        exported = export_service._build_publication_spec_payload_from_definition(
            workflow_id=workflow_id,
            definition=definition,
        )
        exported_steps = {step["state_id"]: step for step in exported["steps"]}

        for state_id in state_ids:
            raw_policy = _publication_step(
                raw_bundle,
                workflow_id=workflow_id,
                state_id=state_id,
            )["retry_policy"]
            parsed_policy = definition.states[state_id].metadata["retry_policy"]
            canonical_policy = _normalise_retry_policy_spec(parsed_policy)
            assert raw_policy == canonical_policy
            assert parsed_policy == canonical_policy
            assert exported_steps[state_id]["retry_policy"] == canonical_policy


def test_kr_seed_bundle_validates_as_workflow_contracts() -> None:
    report = mod.validate_kr_materialisation_seed_bundle()

    assert report["success"] is True
    assert report["workflow_ids"] == list(mod.KR_MATERIALISATION_WORKFLOW_IDS)
    assert report["invalid_workflow_ids"] == []
    for validation in report["validation_by_workflow_id"].values():
        assert validation["valid"] is True


def test_normal_bootstrap_migrates_exact_reviewed_v7_prompt_content(
    monkeypatch,
) -> None:
    desired_by_id = _prompt_seed_text_by_id()
    legacy_by_id = {
        prompt_id: f"reviewed v7 content for {prompt_id}"
        for prompt_id in desired_by_id
    }
    live_by_id = {
        prompt_id: (legacy_content,)
        for prompt_id, legacy_content in legacy_by_id.items()
    }
    writes: list[dict[str, Any]] = []
    publication_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        mod,
        "ensure_prompt_concept_support",
        lambda **_kwargs: _base_prompt_support_report(),
    )
    monkeypatch.setattr(mod, "_load_repo_seed_version", lambda: "9")
    monkeypatch.setattr(
        mod,
        "_REVIEWED_LEGACY_PROMPT_CONTENT_SHA256_BY_TARGET_SEED_VERSION",
        {
            "9": {
                prompt_id: {
                    "7": (mod._prompt_content_sha256(legacy_content),)
                }
                for prompt_id, legacy_content in legacy_by_id.items()
            }
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_prompt_content_values",
        lambda prompt_id: live_by_id.get(prompt_id, ()),
    )

    def fake_upsert(**kwargs: Any) -> None:
        writes.append(dict(kwargs))
        live_by_id[str(kwargs["subject_concept_id"])] = (
            str(kwargs["text"]).strip(),
        )

    def fake_bootstrap(**kwargs: Any) -> dict[str, Any]:
        publication_calls.append(dict(kwargs))
        return {
            "publication": {
                "counts": {
                    "errors": 0,
                    "workflows_published": len(
                        kwargs.get("target_workflow_ids") or ()
                    ),
                }
            }
        }

    monkeypatch.setattr(mod, "upsert_singleton_text_relation", fake_upsert)
    monkeypatch.setattr(
        mod,
        "bootstrap_repo_seed_workflow_bundle",
        fake_bootstrap,
    )

    report = mod.bootstrap_canonical_kr_materialisation_workflows()

    assert report["success"] is True
    assert len(publication_calls) == 1
    assert publication_calls[0]["force_republish"] is False
    assert {
        write["subject_concept_id"] for write in writes
    } == set(desired_by_id)
    assert all(
        write["context"]["refresh_reason"]
        == "exact_reviewed_legacy_migration"
        for write in writes
    )
    prompt_support = report["prompt_support"]
    assert set(prompt_support["migrated_prompt_ids"]) == set(desired_by_id)
    assert prompt_support["migration_source_seed_version_by_prompt_id"] == {
        prompt_id: "7" for prompt_id in desired_by_id
    }
    assert live_by_id == {
        prompt_id: (desired_content,)
        for prompt_id, desired_content in desired_by_id.items()
    }


def test_arbitrary_prompt_edit_is_preserved_and_blocks_workflow_publication(
    monkeypatch,
) -> None:
    desired_by_id = _prompt_seed_text_by_id()
    edited_prompt_id = mod.PROMPT_KR_RELATIONSHIP_ENDPOINT_RESOLUTION_ID
    live_by_id = {
        prompt_id: (desired_content,)
        for prompt_id, desired_content in desired_by_id.items()
    }
    live_by_id[edited_prompt_id] = ("human-authored live endpoint policy",)
    writes: list[dict[str, Any]] = []

    monkeypatch.setattr(
        mod,
        "ensure_prompt_concept_support",
        lambda **_kwargs: _base_prompt_support_report(),
    )
    monkeypatch.setattr(mod, "_load_repo_seed_version", lambda: "9")
    monkeypatch.setattr(
        mod,
        "_load_prompt_content_values",
        lambda prompt_id: live_by_id.get(prompt_id, ()),
    )
    monkeypatch.setattr(
        mod,
        "upsert_singleton_text_relation",
        lambda **kwargs: writes.append(dict(kwargs)),
    )

    def unexpected_publication(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("workflow publication must remain blocked")

    monkeypatch.setattr(
        mod,
        "bootstrap_repo_seed_workflow_bundle",
        unexpected_publication,
    )

    report = mod.bootstrap_canonical_kr_materialisation_workflows()

    assert report["success"] is False
    assert writes == []
    assert live_by_id[edited_prompt_id] == (
        "human-authored live endpoint policy",
    )
    assert report["publication"]["skipped"] is True
    assert report["publication"]["skip_reason"] == (
        "kr_materialisation_prompt_authority_blocked"
    )
    prompt_support = report["prompt_support"]
    assert prompt_support["preserved_drift_prompt_ids"] == [edited_prompt_id]
    assert prompt_support["errors_by_target"][edited_prompt_id] == (
        "kr_materialisation_prompt_authority_requires_explicit_migration"
    )


def test_prompt_migration_fails_when_canonical_readback_does_not_match(
    monkeypatch,
) -> None:
    desired_by_id = _prompt_seed_text_by_id()
    legacy_by_id = {
        prompt_id: f"reviewed v7 content for {prompt_id}"
        for prompt_id in desired_by_id
    }
    live_by_id = {
        prompt_id: (legacy_content,)
        for prompt_id, legacy_content in legacy_by_id.items()
    }

    monkeypatch.setattr(
        mod,
        "ensure_prompt_concept_support",
        lambda **_kwargs: _base_prompt_support_report(),
    )
    monkeypatch.setattr(mod, "_load_repo_seed_version", lambda: "9")
    monkeypatch.setattr(
        mod,
        "_REVIEWED_LEGACY_PROMPT_CONTENT_SHA256_BY_TARGET_SEED_VERSION",
        {
            "9": {
                prompt_id: {
                    "7": (mod._prompt_content_sha256(legacy_content),)
                }
                for prompt_id, legacy_content in legacy_by_id.items()
            }
        },
    )
    monkeypatch.setattr(
        mod,
        "_load_prompt_content_values",
        lambda prompt_id: live_by_id.get(prompt_id, ()),
    )
    monkeypatch.setattr(
        mod,
        "upsert_singleton_text_relation",
        lambda **_kwargs: None,
    )

    report = mod._ensure_kr_materialisation_prompt_support()

    assert report["success"] is False
    assert set(report["seeded_prompt_ids"]) == set(desired_by_id)
    assert set(report["migrated_prompt_ids"]) == set(desired_by_id)
    assert report["validated_prompt_ids"] == []
    assert report["errors_by_target"] == {
        prompt_id: "kr_materialisation_prompt_authority_readback_mismatch"
        for prompt_id in desired_by_id
    }


def test_kr_optional_initialiser_fields_satisfy_enforced_write_metadata() -> None:
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    concept_step = _publication_step(
        bundle,
        workflow_id=mod.KR_DESIGN_CONCEPT_MATERIALISATION_ITEM_WORKFLOW_ID,
        state_id="initialise_from_concept_spec",
    )
    relationship_step = _publication_step(
        bundle,
        workflow_id=mod.KR_DESIGN_RELATIONSHIP_ASSERTION_ITEM_WORKFLOW_ID,
        state_id="initialise_from_relationship_spec",
    )

    reuse_context = _validate_context_set_writes(
        step=concept_step,
        context={
            "current_kr_concept_spec": {
                "key": "candidate",
                "decision": "reuse_existing",
                "target_name": "Stable candidate",
                "target_kind": "instance",
                "parent_id": "#V#doctoral_candidate",
                "existing_concept_id": "#V#stable_candidate",
                "description_text": "Bounded provenance-bearing candidate.",
            }
        },
    )
    assert reuse_context["kr_concept_create_concepts"] is None
    assert reuse_context["kr_concept_parent_rationale"] is None
    assert reuse_context["kr_concept_blocking_reason"] is None

    create_context = _validate_context_set_writes(
        step=concept_step,
        context={
            "current_kr_concept_spec": {
                "key": "candidate",
                "decision": "create",
                "target_name": "Stable candidate",
                "target_kind": "instance",
                "parent_id": "#V#doctoral_candidate",
                "concepts": [
                    {
                        "name": "Stable candidate",
                        "kind": "instance",
                        "description": "Bounded provenance-bearing candidate.",
                    }
                ],
                "description_text": "Bounded provenance-bearing candidate.",
            }
        },
    )
    assert create_context["kr_concept_id"] is None

    relationship_context = _validate_context_set_writes(
        step=relationship_step,
        context={
            "current_kr_relationship_spec": {
                "source_id": "#V#stable_candidate",
                "predicate": "#V#has_doctoral_programme",
                "target_id": "#V#stable_programme",
            }
        },
    )
    assert relationship_context["kr_relationship_key"] is None
    assert relationship_context["kr_relationship_rationale"] is None
    assert relationship_context["kr_relationship_blocking_reason"] is None


def test_publish_script_validate_all_uses_seed_bundle(capsys) -> None:
    exit_code = publish_cli.main(["validate-all"])
    output = capsys.readouterr().out

    assert exit_code == 0
    payload = json.loads(output)
    assert payload["success"] is True
    assert payload["workflow_ids"] == list(mod.KR_MATERIALISATION_WORKFLOW_IDS)
