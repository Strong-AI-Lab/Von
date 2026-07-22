from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "migrate_jvnautosci_2591_arxiv_degraded_contract.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "migrate_jvnautosci_2591_arxiv_degraded_contract",
    _SCRIPT_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _spec() -> dict:
    assignments = [
        {"key": "pdf_acquisition_status", "value": "skipped"},
        *[
            {
                "key": key,
                "value_from_context": f"last_action_outputs.result.{key}",
                "skip_if_unresolved": True,
            }
            for key in sorted(mod.DIAGNOSTIC_KEYS)
        ],
    ]
    return {
        "workflow_id": mod.WORKFLOW_ID,
        "steps": [
            {
                "state_id": (
                    "#V#workflow_step_arxiv_paper_representation_workflow"
                    f"{mod.STATE_SUFFIX}"
                ),
                "action_id": "workflow_control.context_set",
                "static_input_bindings": [
                    {"tool_param": "assignments", "value": assignments}
                ],
            },
            {
                "state_id": (
                    "#V#workflow_step_arxiv_paper_representation_workflow"
                    f"{mod.DELEGATE_STATE_SUFFIX}"
                ),
                "action_id": "workflow_invoke_subworkflow",
                "static_input_bindings": [
                    {
                        "tool_param": "workflow_id",
                        "value": mod.DELEGATED_WORKFLOW_ID,
                    }
                ],
            },
        ],
    }


def test_rewrite_makes_declared_diagnostics_explicit_nullable_outputs() -> None:
    rewritten, changed = mod.rewrite_degraded_pdf_contract(_spec())

    assert changed is True
    assignments = rewritten["steps"][0]["static_input_bindings"][0]["value"]
    diagnostic_assignments = [
        item for item in assignments if item["key"] in mod.DIAGNOSTIC_KEYS
    ]
    assert len(diagnostic_assignments) == len(mod.DIAGNOSTIC_KEYS)
    assert all("skip_if_unresolved" not in item for item in diagnostic_assignments)
    delegate = next(
        step
        for step in rewritten["steps"]
        if step["state_id"].endswith(mod.DELEGATE_STATE_SUFFIX)
    )
    assert delegate["subworkflow_id"] == mod.DELEGATED_WORKFLOW_ID


def test_rewrite_is_idempotent() -> None:
    first, changed = mod.rewrite_degraded_pdf_contract(_spec())
    second, changed_again = mod.rewrite_degraded_pdf_contract(first)

    assert changed is True
    assert changed_again is False
    assert second == first
