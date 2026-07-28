from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "migrate_jvnautosci_2591_arxiv_identity_workflow.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "migrate_jvnautosci_2591_arxiv_identity_workflow",
    _SCRIPT_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _base_spec() -> dict:
    prefix = (
        "#V#workflow_step_scholarly_article_metadata_representation_workflow"
    )
    return {
        "workflow_id": mod.WORKFLOW_ID,
        "initial_state_key": f"{prefix}_normalise_metadata_context",
        "steps": [
            {
                "state_id": f"{prefix}_normalise_metadata_context",
                "action_id": "workflow_control.context_set",
                "conditional_transitions": [],
                "next_state_key": f"{prefix}_create_article_concept",
            },
            {
                "state_id": f"{prefix}_create_article_concept",
                "action_id": "create_concepts",
                "next_state_key": f"{prefix}_attach_metadata",
            },
            {
                "state_id": f"{prefix}_attach_metadata",
                "action_id": "add_relationships",
                "next_state_key": f"{prefix}_completed",
            },
            {"state_id": f"{prefix}_completed", "terminal": True},
            {"state_id": f"{prefix}_failed", "terminal": True},
        ],
    }


def test_rewrite_adds_canonical_arxiv_identity_state_and_transition() -> None:
    rewritten, changed = mod.rewrite_metadata_workflow_for_canonical_arxiv_identity(
        _base_spec()
    )

    assert changed is True
    ensure_step = next(
        step
        for step in rewritten["steps"]
        if step["state_id"].endswith(mod.ENSURE_STATE_SUFFIX)
    )
    assert ensure_step["action_id"] == mod.ENSURE_ACTION_ID
    assert ensure_step["mutation_authority"]["maximum_level"] == (
        "additive_vontology"
    )
    assert ensure_step["next_state_key"].endswith(mod.ATTACH_STATE_SUFFIX)

    normalise_step = next(
        step
        for step in rewritten["steps"]
        if step["state_id"].endswith(mod.NORMALISE_STATE_SUFFIX)
    )
    transition = next(
        item
        for item in normalise_step["conditional_transitions"]
        if item["reason"] == "canonical_arxiv_identity_available"
    )
    assert transition["to_state"] == ensure_step["state_id"]


def test_rewrite_is_idempotent() -> None:
    first, changed = mod.rewrite_metadata_workflow_for_canonical_arxiv_identity(
        _base_spec()
    )
    second, changed_again = (
        mod.rewrite_metadata_workflow_for_canonical_arxiv_identity(first)
    )

    assert changed is True
    assert changed_again is False
    assert second == first


def test_visibility_repair_aligns_ensure_step_and_mappings_with_root(
    monkeypatch,
) -> None:
    rewritten, _changed = (
        mod.rewrite_metadata_workflow_for_canonical_arxiv_identity(_base_spec())
    )
    child_ids = mod._ensure_visibility_child_ids(rewritten)
    documents = {
        mod.WORKFLOW_ID: {
            "concept_id": mod.WORKFLOW_ID,
            "relationships": {},
        },
        **{
            child_id: {
                "concept_id": child_id,
                "relationships": {
                    "#V#specific_to_user": [mod.DEFAULT_ACTOR_USER_ID]
                },
            }
            for child_id in child_ids
        },
    }
    monkeypatch.setattr(
        mod,
        "_find_raw_concept_by_exact_concept_id",
        lambda concept_id: documents.get(concept_id),
    )

    def _update(concept_id, payload, **_kwargs):
        documents[concept_id]["relationships"] = payload["relationships"]

    monkeypatch.setattr(mod.concept_service, "update_concept", _update)

    preview = mod.repair_ensure_arxiv_visibility_closure(
        rewritten,
        apply=False,
    )
    applied = mod.repair_ensure_arxiv_visibility_closure(
        rewritten,
        apply=True,
    )

    assert set(preview["repair_required_ids"]) == set(child_ids)
    assert preview["closure_verified"] is False
    assert set(applied["repair_required_ids"]) == set(child_ids)
    assert applied["closure_verified"] is True
    assert all(documents[child_id]["relationships"] == {} for child_id in child_ids)
