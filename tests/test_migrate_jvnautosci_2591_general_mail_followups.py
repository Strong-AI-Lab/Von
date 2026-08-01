from __future__ import annotations

import copy
import importlib.util
from pathlib import Path


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "migrate_jvnautosci_2591_general_mail_followups.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "migrate_jvnautosci_2591_general_mail_followups",
    _SCRIPT_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _step(suffix: str, **fields: object) -> dict:
    return {
        "state_id": f"#V#workflow_step_general_mail_review_workflow_{suffix}",
        **fields,
    }


def _desired_spec() -> dict:
    mapping = {
        "tool_output_field": "runtime_profile_alias",
        "context_key": "mail_review_profile_id",
        "mapping_concept_id": (
            "#V#workflow_mapping_mail_profile_status_runtime_alias_to_"
            "mail_review_profile_id"
        ),
    }
    return {
        "workflow_id": mod.GENERAL_MAIL_WORKFLOW_ID,
        "steps": [
            _step("prepare_mail_review_tool_prompt", static_input_bindings=[]),
            _step("record_mail_list_invocation", static_input_bindings=[]),
            _step("render_mail_review_response", llm_policy={}),
            _step(
                "extract_mail_review_request_parameters",
                llm_policy={},
                validation_policy={},
                tool_output_context_mappings=[],
                writes_context_keys=[],
                conditional_transitions=[
                    {"to_state": "fetch_mail_profile_status"}
                ],
            ),
            _step(
                "fetch_mail_profile_status",
                tool_output_context_mappings=[mapping],
                writes_context_keys=["mail_review_profile_id"],
                conditional_transitions=[
                    {"to_state": "render_mail_profile_status"}
                ],
            ),
            _step(
                "render_mail_profile_status",
                conditional_transitions=[{"to_state": "completed"}],
            ),
            _step("completed"),
        ],
    }


def _current_spec() -> dict:
    current = copy.deepcopy(_desired_spec())
    fetch = next(
        step
        for step in current["steps"]
        if step["state_id"].endswith("fetch_mail_profile_status")
    )
    fetch["tool_output_context_mappings"][0]["tool_output_field"] = (
        "attributes.runtime_profile_alias"
    )
    return current


def test_rewrite_updates_existing_profile_status_projection_mapping() -> None:
    rewritten, changed = mod.rewrite_general_mail_followups(
        _current_spec(), _desired_spec()
    )

    assert changed is True
    fetch = next(
        step
        for step in rewritten["steps"]
        if step["state_id"].endswith("fetch_mail_profile_status")
    )
    assert fetch["tool_output_context_mappings"][0]["tool_output_field"] == (
        "runtime_profile_alias"
    )


def test_rewrite_is_idempotent() -> None:
    first, changed = mod.rewrite_general_mail_followups(
        _current_spec(), _desired_spec()
    )
    second, changed_again = mod.rewrite_general_mail_followups(
        first, _desired_spec()
    )

    assert changed is True
    assert changed_again is False
    assert second == first


def test_rewrite_repairs_dangling_profile_status_branch() -> None:
    current = _current_spec()
    current["steps"] = [
        step
        for step in current["steps"]
        if not step["state_id"].endswith(
            ("fetch_mail_profile_status", "render_mail_profile_status")
        )
    ]

    rewritten, changed = mod.rewrite_general_mail_followups(
        current,
        _desired_spec(),
    )

    assert changed is True
    state_ids = {step["state_id"] for step in rewritten["steps"]}
    assert (
        "#V#workflow_step_general_mail_review_workflow_fetch_mail_profile_status"
        in state_ids
    )
    assert (
        "#V#workflow_step_general_mail_review_workflow_render_mail_profile_status"
        in state_ids
    )


def test_visibility_repair_copies_parent_scope_and_preserves_other_relations(
    monkeypatch,
) -> None:
    workflow_id = mod.GENERAL_MAIL_WORKFLOW_ID
    child_id = "#V#stale_mail_child"
    docs = {
        workflow_id: {
            "concept_id": workflow_id,
            "relationships": {"#V#is_an_instance_of": ["#V#ai_workflow"]},
        },
        child_id: {
            "concept_id": child_id,
            "relationships": {
                "#V#specific_to_user": ["#V#zhan_von_witbrock"],
                "#V#is_an_instance_of": ["#V#workflow_step"],
            },
        },
    }
    monkeypatch.setitem(
        mod.VISIBILITY_REPAIR_CHILD_IDS_BY_WORKFLOW,
        workflow_id,
        (child_id,),
    )
    monkeypatch.setattr(
        mod,
        "_find_raw_concept_by_exact_concept_id",
        lambda concept_id: copy.deepcopy(docs.get(concept_id)),
    )

    def update_concept(
        concept_id: str,
        update_data: dict,
        *,
        defer_side_effects: bool,
    ) -> None:
        assert defer_side_effects is True
        docs[concept_id]["relationships"] = copy.deepcopy(
            update_data["relationships"]
        )

    monkeypatch.setattr(mod.concept_service, "update_concept", update_concept)

    preview = mod.repair_mail_visibility_closure(workflow_id, apply=False)
    applied = mod.repair_mail_visibility_closure(workflow_id, apply=True)

    assert preview["repair_required_ids"] == [child_id]
    assert applied["closure_verified"] is True
    assert "#V#specific_to_user" not in docs[child_id]["relationships"]
    assert docs[child_id]["relationships"]["#V#is_an_instance_of"] == [
        "#V#workflow_step"
    ]
