from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.workflow_discovery_memo_service import (
    clear_turn_workflow_discovery_memo,
    discover_workflows_for_turn_memoized,
)


@pytest.fixture(autouse=True)
def _clear_memo() -> None:
    clear_turn_workflow_discovery_memo()
    yield
    clear_turn_workflow_discovery_memo()


def _runtime_state(version: str) -> dict[str, Any]:
    return {
        "surface": "workflow_retrieval",
        "namespace": "workflow_capabilities",
        "size": 3,
        "ready": True,
        "last_manifest_digest": version,
        "last_success_monotonic": 100.0,
        "last_invalidated_at_utc": None,
        "last_mode": "test",
    }


def test_turn_workflow_discovery_memo_reuses_same_turn_query(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.get_workflow_capability_index_runtime_state",
        lambda *, latency_sensitive=False: _runtime_state("v1"),
    )
    calls: list[str] = []

    def _discover(user_input: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(user_input)
        return {
            "query": user_input,
            "requested_query": "Find represented papers",
            "matches": [{"concept_id": "#V#represented_fact_workflow"}],
            "candidates": [{"concept_id": "#V#represented_fact_workflow"}],
            "candidate_count": 1,
            "match_count": 1,
            "discovery_payload_origin": "test_discovery",
        }

    first = discover_workflows_for_turn_memoized(
        "Find represented papers",
        namespace="#V#user@org",
        turn_scope="turn-1",
        requested_query="Find represented papers",
        expected_outcome_contract={"summary": "Grounded represented answer"},
        discovery_func=_discover,
    )
    second = discover_workflows_for_turn_memoized(
        "Find represented papers",
        namespace="#V#user@org",
        turn_scope="turn-1",
        requested_query="Find represented papers",
        expected_outcome_contract={"summary": "Grounded represented answer"},
        discovery_func=_discover,
    )

    assert calls == ["Find represented papers"]
    assert first is not None
    assert second is not None
    assert first["workflow_discovery_cache"]["cache_hit"] is False
    assert second["workflow_discovery_cache"]["cache_hit"] is True
    assert second["workflow_discovery_cache"]["candidate_count"] == 1
    assert (
        second["workflow_discovery_cache"]["cache_key_digest"]
        == first["workflow_discovery_cache"]["cache_key_digest"]
    )


def test_turn_workflow_discovery_memo_separates_namespace(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.get_workflow_capability_index_runtime_state",
        lambda *, latency_sensitive=False: _runtime_state("v1"),
    )
    calls: list[str] = []

    def _discover(user_input: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs.get("namespace")))
        return {
            "query": user_input,
            "matches": [],
            "candidates": [],
            "candidate_count": 0,
        }

    discover_workflows_for_turn_memoized(
        "Route this turn",
        namespace="#V#user@org-a",
        turn_scope="turn-1",
        discovery_func=_discover,
    )
    discover_workflows_for_turn_memoized(
        "Route this turn",
        namespace="#V#user@org-b",
        turn_scope="turn-1",
        discovery_func=_discover,
    )

    assert calls == ["#V#user@org-a", "#V#user@org-b"]


def test_turn_workflow_discovery_memo_invalidates_on_contract_change(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.get_workflow_capability_index_runtime_state",
        lambda *, latency_sensitive=False: _runtime_state("v1"),
    )
    calls = 0

    def _discover(user_input: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"query": user_input, "matches": [], "candidates": []}

    discover_workflows_for_turn_memoized(
        "Route this turn",
        namespace="#V#user@org",
        turn_scope="turn-1",
        expected_outcome_contract={"summary": "Read represented facts"},
        discovery_func=_discover,
    )
    discover_workflows_for_turn_memoized(
        "Route this turn",
        namespace="#V#user@org",
        turn_scope="turn-1",
        expected_outcome_contract={"summary": "Create represented facts"},
        discovery_func=_discover,
    )

    assert calls == 2


def test_turn_workflow_discovery_memo_ranks_with_requested_query(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.get_workflow_capability_index_runtime_state",
        lambda *, latency_sensitive=False: _runtime_state("v1"),
    )
    calls: list[str] = []
    raw_query = "Represent this paper: https://arxiv.org/abs/2106.03245"
    enriched_query = (
        raw_query + "\n\nTurn-intent routing guidance:\n"
        "- Routing guidance: Prefer the most specific represented workflow.\n"
        "- Required tools: get_paper_metadata"
    )

    def _discover(user_input: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(user_input)
        assert _kwargs["requested_query"] == raw_query
        return {
            "query": user_input,
            "requested_query": raw_query,
            "matches": [{"concept_id": "#V#arxiv_paper_representation_workflow"}],
            "candidates": [{"concept_id": "#V#arxiv_paper_representation_workflow"}],
            "candidate_count": 1,
            "match_count": 1,
        }

    result = discover_workflows_for_turn_memoized(
        enriched_query,
        namespace="#V#user@org",
        turn_scope="turn-1",
        requested_query=raw_query,
        expected_outcome_contract={"required_tools": ["get_paper_metadata"]},
        discovery_func=_discover,
    )

    assert calls == [enriched_query]
    assert result is not None
    assert result["query"] == enriched_query
    assert result["ranking_query_input"] == enriched_query
    assert result["ranking_query_input_source"] == "expected_outcome_contract"
    assert result["matches"][0]["concept_id"] == (
        "#V#arxiv_paper_representation_workflow"
    )


def test_turn_workflow_discovery_memo_passes_structured_contract_to_discovery(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.get_workflow_capability_index_runtime_state",
        lambda *, latency_sensitive=False: _runtime_state("v1"),
    )
    captured_kwargs: dict[str, Any] = {}
    raw_query = "Represent metadata for #V#benchmark_report."
    enriched_query = (
        raw_query
        + "\n\nTurn-intent routing guidance:\n- Required tools: workflow_execute"
    )
    contract = {
        "schema_version": "turn_expected_outcome_contract.v1",
        "fields": {"summary": "Represent metadata for the benchmark report."},
        "required_tools": ["workflow_execute"],
        "target_workflow_id": "#V#generic_metadata_representation_workflow",
    }

    def _discover(user_input: str, **kwargs: Any) -> dict[str, Any]:
        captured_kwargs.update(kwargs)
        return {
            "query": (
                user_input + "\n\nTurn-intent routing guidance:\n"
                "- Workflow concept IDs: #V#generic_metadata_representation_workflow"
            ),
            "requested_query": raw_query,
            "matches": [],
            "candidates": [],
            "candidate_count": 0,
            "match_count": 0,
            "contract_projection": {
                "schema_version": "workflow_discovery_contract_projection.v1",
                "fields_used": ["summary", "required_tools", "workflow_concept_ids"],
            },
        }

    result = discover_workflows_for_turn_memoized(
        enriched_query,
        namespace="#V#user@org",
        turn_scope="turn-contract",
        requested_query=raw_query,
        expected_outcome_contract=contract,
        discovery_func=_discover,
    )

    assert captured_kwargs["expected_outcome_contract"] == contract
    assert captured_kwargs["requested_query"] == raw_query
    assert result is not None
    assert result["ranking_query_input_source"] == "expected_outcome_contract"
    assert (
        "#V#generic_metadata_representation_workflow" in result["ranking_query_input"]
    )


def test_turn_workflow_discovery_memo_invalidates_on_capability_index_version(
    monkeypatch,
) -> None:
    versions = iter(("v1", "v2"))
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service.get_workflow_capability_index_runtime_state",
        lambda *, latency_sensitive=False: _runtime_state(next(versions)),
    )
    calls = 0

    def _discover(user_input: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"query": user_input, "matches": [], "candidates": []}

    discover_workflows_for_turn_memoized(
        "Route this turn",
        namespace="#V#user@org",
        turn_scope="turn-1",
        discovery_func=_discover,
    )
    discover_workflows_for_turn_memoized(
        "Route this turn",
        namespace="#V#user@org",
        turn_scope="turn-1",
        discovery_func=_discover,
    )

    assert calls == 2
