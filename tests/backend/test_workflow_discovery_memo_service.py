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
    assert second["workflow_discovery_cache"]["cache_key_digest"] == first[
        "workflow_discovery_cache"
    ]["cache_key_digest"]


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
