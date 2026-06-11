"""Tests for represented tool/action projection into routing metadata
(JVNAUTOSCI-2501).

batch_fetch_workflow_routing_metadata must project per-workflow
workflow_action_ids and required_tools from the represented step graph
(#V#invokesAction / #V#workflow_step_invokes_tool targets) so capability-index
candidates expose them as routing_index_metadata. The projection is purely
structural: engine control actions are excluded from tool requirements, and
tool names follow the same identity/dots-to-underscores rule the runtime MCP
bridge uses.
"""

from __future__ import annotations

from unittest.mock import patch

_LOADER = "src.backend.workflows.vontology_loader"

_WORKFLOW_ID = "#V#test_projection_workflow"
_BARE_WORKFLOW_ID = "#V#test_bare_workflow"

_WORKFLOW_DOCS = {
    _WORKFLOW_ID: {
        "concept_id": _WORKFLOW_ID,
        "relationships": {
            "#V#hasInitialStep": [f"{_WORKFLOW_ID}_step_fetch"],
            "#V#hasStep": [
                f"{_WORKFLOW_ID}_step_fetch",
                f"{_WORKFLOW_ID}_step_control",
                f"{_WORKFLOW_ID}_step_tool_link",
                f"{_WORKFLOW_ID}_step_missing_doc",
            ],
        },
        "concept_data": {},
    },
    _BARE_WORKFLOW_ID: {
        "concept_id": _BARE_WORKFLOW_ID,
        "relationships": {},
        "concept_data": {},
    },
}

_STEP_DOCS = {
    f"{_WORKFLOW_ID}_step_fetch": {
        "concept_id": f"{_WORKFLOW_ID}_step_fetch",
        "relationships": {"#V#invokesAction": ["arxiv.fetch_metadata"]},
    },
    f"{_WORKFLOW_ID}_step_control": {
        "concept_id": f"{_WORKFLOW_ID}_step_control",
        "relationships": {
            "#V#invokesAction": ["workflow_control.context_set"]
        },
    },
    f"{_WORKFLOW_ID}_step_tool_link": {
        "concept_id": f"{_WORKFLOW_ID}_step_tool_link",
        "relationships": {
            "#V#workflow_step_invokes_tool": ["download_paper"]
        },
    },
}


class _FakeConceptsRepository:
    @staticmethod
    def find(query, projection=None):
        requested_ids = (query or {}).get("concept_id", {}).get("$in", [])
        docs = {**_WORKFLOW_DOCS, **_STEP_DOCS}
        return [docs[cid] for cid in requested_ids if cid in docs]


def _fetch_metadata():
    from src.backend.workflows.vontology_loader import (
        batch_fetch_workflow_routing_metadata,
    )

    with (
        patch(f"{_LOADER}.ConceptsRepository", _FakeConceptsRepository),
        patch(
            f"{_LOADER}.get_preferred_texts_for_concepts",
            return_value={},
        ),
    ):
        return batch_fetch_workflow_routing_metadata(
            [_WORKFLOW_ID, _BARE_WORKFLOW_ID]
        )


def test_projects_action_ids_and_tools_from_represented_steps():
    metadata = _fetch_metadata()
    workflow_metadata = metadata[_WORKFLOW_ID]

    assert workflow_metadata["workflow_action_ids"] == [
        "arxiv.fetch_metadata",
        "download_paper",
        "workflow_control.context_set",
    ]
    assert workflow_metadata["workflow_action_ids_source"] == (
        "vontology_workflow_graph:invokesAction"
    )

    # Control actions are excluded from tool requirements; tool names include
    # the bridge's identity and underscored candidates.
    assert workflow_metadata["required_tools"] == [
        "arxiv.fetch_metadata",
        "arxiv_fetch_metadata",
        "download_paper",
    ]
    assert workflow_metadata["required_tools_source"] == (
        "vontology_workflow_graph:invokesAction:internal_mcp_tool_name_candidates"
    )


def test_workflow_without_steps_gets_no_tool_claims():
    metadata = _fetch_metadata()
    bare_metadata = metadata[_BARE_WORKFLOW_ID]
    assert "workflow_action_ids" not in bare_metadata
    assert "required_tools" not in bare_metadata


def test_projection_supports_selector_fast_path_coverage():
    """The projected metadata drives the JVNAUTOSCI-2406 coverage annotator."""

    from src.backend.services.workflow_discovery_service import (
        _annotate_selector_fast_path_coverage,
    )

    metadata = _fetch_metadata()[_WORKFLOW_ID]
    candidate = {
        "concept_id": _WORKFLOW_ID,
        "is_executable": True,
        "routing_index_metadata": {
            "required_tools": metadata["required_tools"],
            "workflow_action_ids": metadata["workflow_action_ids"],
        },
    }
    payload = {
        "matches": [candidate],
        "contract_projection": {
            "required_tools": ["download_paper", "arxiv_fetch_metadata"],
            "required_actions": [],
        },
    }
    _annotate_selector_fast_path_coverage(
        payload,
        policy={
            "authority_concept_id": "#V#selector_fast_path_policy",
            "treat_missing_required_actions_as_covered": True,
        },
    )
    assert candidate["covers_expected_tool_set"] is True
    assert candidate["covers_success_contract"] is True
