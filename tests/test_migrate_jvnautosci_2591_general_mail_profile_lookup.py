from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "migrate_jvnautosci_2591_general_mail_profile_lookup.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "migrate_jvnautosci_2591_general_mail_profile_lookup",
    _SCRIPT_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _base_spec() -> dict:
    prefix = "#V#workflow_step_general_mail_review_workflow"
    lookup_arguments = {
        "argument_index": "subject",
        "relation_kind": "any",
    }
    return {
        "workflow_id": mod.WORKFLOW_ID,
        "steps": [
            {
                "state_id": f"{prefix}_lookup_default_mail_profile",
                "action_id": "workflow_mcp.invoke_tool",
                "static_input_bindings": [
                    {
                        "tool_param": "tool_name",
                        "value": "find_relations_with_argument",
                    },
                    {
                        "tool_param": "tool_arguments",
                        "value": dict(lookup_arguments),
                    },
                ],
            },
            {
                "state_id": f"{prefix}_lookup_represented_mail_profiles",
                "action_id": "workflow_mcp.invoke_tool",
                "static_input_bindings": [
                    {
                        "tool_param": "tool_name",
                        "value": "find_relations_with_argument",
                    },
                    {
                        "tool_param": "tool_arguments",
                        "value": dict(lookup_arguments),
                    },
                ],
            },
            {
                "state_id": f"{prefix}_resolve_mail_profile",
                "action_id": "mail_review.resolve_profile",
                "llm_policy": {
                    "tool_argument_defaults": {
                        "find_relations_with_argument": {
                            "relation_kind": "any"
                        }
                    }
                },
            },
        ],
    }


def _relation_kinds(spec: dict) -> list[str]:
    values: list[str] = []
    for step in spec["steps"][:2]:
        binding = next(
            item
            for item in step["static_input_bindings"]
            if item["tool_param"] == "tool_arguments"
        )
        values.append(binding["value"]["relation_kind"])
    values.append(
        spec["steps"][2]["llm_policy"]["tool_argument_defaults"]
        ["find_relations_with_argument"]["relation_kind"]
    )
    return values


def test_rewrite_restricts_profile_lookups_to_binary_relations() -> None:
    rewritten, changed = mod.rewrite_general_mail_profile_lookups(_base_spec())

    assert changed is True
    assert _relation_kinds(rewritten) == ["binary", "binary", "binary"]


def test_rewrite_is_idempotent() -> None:
    first, changed = mod.rewrite_general_mail_profile_lookups(_base_spec())
    second, changed_again = mod.rewrite_general_mail_profile_lookups(first)

    assert changed is True
    assert changed_again is False
    assert second == first
