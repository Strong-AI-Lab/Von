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
