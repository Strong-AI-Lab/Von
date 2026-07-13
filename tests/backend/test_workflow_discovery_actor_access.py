"""Domain-agnostic actor-isolation tests for workflow discovery surfaces."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.backend.security import access_control
import src.backend.services.workflow_capability_service as capability_service
import src.backend.services.workflow_discovery_memo_service as memo_service
import src.backend.services.workflow_discovery_service as discovery_service
from src.backend.services.workflow_capability_service import (
    WorkflowCapabilityIndex,
    resolve_workflow_capabilities_for_contract,
)
from src.backend.services.workflow_discovery_service import (
    WorkflowDiscoveryResult,
    WorkflowMatch,
    _filter_actor_accessible_workflow_matches,
    _resolve_contract_direct_workflow_candidates,
    discover_workflows_for_turn,
)


GLOBAL_WORKFLOW_ID = "#V#synthetic_global_workflow"
COHORT_WORKFLOW_ID = "#V#synthetic_cohort_workflow"
TRUSTED_ORG_ID = "#V#synthetic_trusted_org"
OUTSIDER_ORG_ID = "#V#synthetic_outsider_org"


@pytest.fixture()
def visibility_collection(monkeypatch: pytest.MonkeyPatch):
    mongomock = pytest.importorskip("mongomock")
    collection = mongomock.MongoClient().db.concepts
    collection.insert_many(
        [
            {"concept_id": GLOBAL_WORKFLOW_ID, "relationships": {}},
            {
                "concept_id": COHORT_WORKFLOW_ID,
                "relationships": {
                    "#V#specific_to_organisation": [TRUSTED_ORG_ID],
                },
            },
        ]
    )
    monkeypatch.setattr(
        access_control,
        "get_concepts_collection",
        lambda: collection,
    )
    return collection


def _capability_entry(workflow_id: str, name: str):
    return capability_service._CapabilityEntry(
        workflow_id=workflow_id,
        doc_id=f"workflow_capability:{workflow_id}",
        text="Shared workflow routing through a represented fetch action.",
        metadata={
            "name": name,
            "summary_text": "Shared workflow routing and represented retrieval.",
            "required_tools": ["synthetic_fetch"],
            "workflow_action_ids": ["synthetic_fetch"],
        },
    )


def _capability_index() -> WorkflowCapabilityIndex:
    index = WorkflowCapabilityIndex()
    with index._lock:
        index._entries = {
            GLOBAL_WORKFLOW_ID: _capability_entry(
                GLOBAL_WORKFLOW_ID,
                "Synthetic Global Workflow",
            ),
            COHORT_WORKFLOW_ID: _capability_entry(
                COHORT_WORKFLOW_ID,
                "Synthetic Cohort Workflow",
            ),
        }
    return index


def test_global_capability_index_filters_before_limit_without_actor_poisoning(
    monkeypatch: pytest.MonkeyPatch,
    visibility_collection,
) -> None:
    del visibility_collection
    index = _capability_index()
    retrieval_backend = SimpleNamespace(
        query=lambda **_kwargs: [
            {
                "metadata": {"workflow_id": COHORT_WORKFLOW_ID},
                "score": 1.0,
            }
        ]
    )
    monkeypatch.setattr(
        capability_service,
        "_get_workflow_capability_rag_service",
        lambda: retrieval_backend,
    )

    with access_control.override_current_actor(
        "#V#synthetic_member",
        TRUSTED_ORG_ID,
    ):
        trusted_first = index.search("shared workflow routing", max_results=1)
    with access_control.override_current_actor(
        "#V#synthetic_outsider",
        OUTSIDER_ORG_ID,
    ):
        outsider = index.search("shared workflow routing", max_results=1)
    with access_control.override_current_actor(
        "#V#synthetic_member",
        TRUSTED_ORG_ID,
    ):
        trusted_again = index.search("shared workflow routing", max_results=1)

    assert [match.workflow_id for match in trusted_first] == [COHORT_WORKFLOW_ID]
    # The inaccessible top retrieval result is removed before the result limit;
    # the existing memory fallback can still expose the lower-ranked global row.
    assert [match.workflow_id for match in outsider] == [GLOBAL_WORKFLOW_ID]
    assert [match.workflow_id for match in trusted_again] == [COHORT_WORKFLOW_ID]


def test_contract_capability_resolution_filters_shared_entries_for_each_actor(
    monkeypatch: pytest.MonkeyPatch,
    visibility_collection,
) -> None:
    del visibility_collection
    index = _capability_index()
    monkeypatch.setattr(
        capability_service,
        "get_workflow_capability_index",
        lambda: index,
    )

    with access_control.override_current_actor(
        "#V#synthetic_outsider",
        OUTSIDER_ORG_ID,
    ):
        outsider = resolve_workflow_capabilities_for_contract(
            required_tools=["synthetic_fetch"],
            max_results=5,
        )
    with access_control.override_current_actor(
        "#V#synthetic_member",
        TRUSTED_ORG_ID,
    ):
        trusted = resolve_workflow_capabilities_for_contract(
            required_tools=["synthetic_fetch"],
            max_results=5,
        )

    assert [match.workflow_id for match in outsider] == [GLOBAL_WORKFLOW_ID]
    assert {match.workflow_id for match in trusted} == {
        GLOBAL_WORKFLOW_ID,
        COHORT_WORKFLOW_ID,
    }


def test_exact_registry_resolution_checks_access_before_reading_purpose(
    monkeypatch: pytest.MonkeyPatch,
    visibility_collection,
) -> None:
    del visibility_collection
    registry = SimpleNamespace(
        all_workflow_ids=lambda: [COHORT_WORKFLOW_ID],
    )
    metadata_lookup = MagicMock(
        return_value=("Restricted represented purpose", "vontology")
    )
    monkeypatch.setattr(
        discovery_service,
        "_peek_registry_workflow_metadata",
        metadata_lookup,
    )

    with access_control.override_current_actor(
        "#V#synthetic_outsider",
        OUTSIDER_ORG_ID,
    ):
        outsider = _resolve_contract_direct_workflow_candidates(
            COHORT_WORKFLOW_ID,
            workflow_registry=registry,
            limit=1,
        )
    metadata_lookup.assert_not_called()

    with access_control.override_current_actor(
        "#V#synthetic_member",
        TRUSTED_ORG_ID,
    ):
        trusted = _resolve_contract_direct_workflow_candidates(
            COHORT_WORKFLOW_ID,
            workflow_registry=registry,
            limit=1,
        )

    assert outsider == []
    assert [match.concept_id for match in trusted] == [COHORT_WORKFLOW_ID]
    assert trusted[0].description == "Restricted represented purpose"
    metadata_lookup.assert_called_once_with(
        COHORT_WORKFLOW_ID,
        workflow_registry=registry,
    )


def test_outer_discovery_guard_removes_restricted_metadata_from_future_sources(
    visibility_collection,
) -> None:
    del visibility_collection
    matches = [
        WorkflowMatch(
            concept_id=COHORT_WORKFLOW_ID,
            name="Synthetic Cohort Workflow",
            description="Restricted represented purpose",
            routing_index_metadata={"restricted_projection": "must not escape"},
        ),
        WorkflowMatch(
            concept_id=GLOBAL_WORKFLOW_ID,
            name="Synthetic Global Workflow",
            description="Global represented purpose",
        ),
    ]

    with access_control.override_current_actor(
        "#V#synthetic_outsider",
        OUTSIDER_ORG_ID,
    ):
        filtered = _filter_actor_accessible_workflow_matches(matches)

    assert [match.concept_id for match in filtered] == [GLOBAL_WORKFLOW_ID]


@pytest.mark.parametrize(
    "authority_failure",
    ("collection_unavailable", "collection_acquisition_raises", "query_raises"),
)
def test_actor_scoped_discovery_hides_warm_matches_when_authority_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    authority_failure: str,
) -> None:
    matches = [
        WorkflowMatch(
            concept_id=GLOBAL_WORKFLOW_ID,
            name="Synthetic Global Workflow",
            description="Warm process-global discovery metadata.",
        ),
        WorkflowMatch(
            concept_id=COHORT_WORKFLOW_ID,
            name="Synthetic Cohort Workflow",
            description="Warm restricted discovery metadata.",
        ),
    ]

    if authority_failure == "collection_unavailable":
        monkeypatch.setattr(
            access_control,
            "get_concepts_collection",
            lambda: None,
        )
    elif authority_failure == "collection_acquisition_raises":
        def _raise_on_acquisition():
            raise RuntimeError("synthetic concept authority acquisition failure")

        monkeypatch.setattr(
            access_control,
            "get_concepts_collection",
            _raise_on_acquisition,
        )
    else:
        collection = MagicMock()
        collection.find.side_effect = RuntimeError(
            "synthetic concept visibility query failure"
        )
        monkeypatch.setattr(
            access_control,
            "get_concepts_collection",
            lambda: collection,
        )

    with access_control.override_current_actor(
        "#V#synthetic_member",
        TRUSTED_ORG_ID,
    ):
        filtered = _filter_actor_accessible_workflow_matches(matches)

    assert filtered == []


def test_deliberately_unscoped_internal_discovery_retains_trusted_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        access_control,
        "get_concepts_collection",
        lambda: None,
    )
    matches = [
        WorkflowMatch(
            concept_id=GLOBAL_WORKFLOW_ID,
            name="Synthetic Global Workflow",
            description="Trusted internal projection.",
        ),
        WorkflowMatch(
            concept_id=COHORT_WORKFLOW_ID,
            name="Synthetic Cohort Workflow",
            description="Trusted internal projection.",
        ),
    ]

    filtered = _filter_actor_accessible_workflow_matches(matches)

    assert [match.concept_id for match in filtered] == [
        GLOBAL_WORKFLOW_ID,
        COHORT_WORKFLOW_ID,
    ]


def test_turn_discovery_binds_namespace_actor_and_rejects_ambient_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_actors: list[tuple[str | None, str | None]] = []

    def _discover(*_args, **_kwargs) -> WorkflowDiscoveryResult:
        observed_actors.append(
            (
                access_control.get_effective_user_concept_id(),
                access_control.get_effective_organisation_concept_id(),
            )
        )
        return WorkflowDiscoveryResult()

    monkeypatch.setattr(discovery_service, "discover_workflows", _discover)
    monkeypatch.setattr(
        discovery_service,
        "_attach_selector_fast_path_metadata",
        lambda payload: payload,
    )

    bound = discover_workflows_for_turn(
        "Route this represented turn",
        namespace="#V#synthetic_member@synthetic_trusted_org",
    )
    with access_control.override_current_actor(
        "#V#ambient_actor",
        "#V#ambient_org",
    ):
        rejected = discover_workflows_for_turn(
            "Route this represented turn",
            namespace="#V#different_actor@different_org",
        )

    assert observed_actors == [
        ("#V#synthetic_member", TRUSTED_ORG_ID),
    ]
    assert bound is not None
    assert rejected is not None
    assert rejected["matches"] == []
    assert rejected["match_absence_reason"] == (
        "workflow_discovery_actor_namespace_mismatch"
    )


def test_turn_discovery_rejects_namespace_org_escalation_for_user_only_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discover = MagicMock(return_value=WorkflowDiscoveryResult())
    monkeypatch.setattr(discovery_service, "discover_workflows", discover)

    with access_control.override_current_actor("#V#synthetic_outsider", None):
        rejected = discover_workflows_for_turn(
            "Route this represented turn",
            namespace="#V#synthetic_outsider@synthetic_trusted_org",
        )

    assert rejected is not None
    assert rejected["matches"] == []
    assert rejected["match_absence_reason"] == (
        "workflow_discovery_actor_namespace_mismatch"
    )
    discover.assert_not_called()


def test_turn_memo_is_actor_bound_before_cache_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memo_service.clear_turn_workflow_discovery_memo()
    monkeypatch.setattr(memo_service, "_capability_index_version", lambda: "v1")
    observed_actors: list[str | None] = []

    def _discover(user_input: str, **_kwargs):
        actor = access_control.get_effective_user_concept_id()
        observed_actors.append(actor)
        return {
            "query": user_input,
            "matches": [{"concept_id": f"{actor}_workflow"}],
            "candidates": [{"concept_id": f"{actor}_workflow"}],
            "candidate_count": 1,
            "match_count": 1,
        }

    owner_namespace = "#V#synthetic_owner@synthetic_trusted_org"
    owner_first = memo_service.discover_workflows_for_turn_memoized(
        "Route this represented turn",
        namespace=owner_namespace,
        turn_scope="turn-actor-cache",
        discovery_func=_discover,
    )
    owner_cached = memo_service.discover_workflows_for_turn_memoized(
        "Route this represented turn",
        namespace=owner_namespace,
        turn_scope="turn-actor-cache",
        discovery_func=_discover,
    )
    outsider = memo_service.discover_workflows_for_turn_memoized(
        "Route this represented turn",
        namespace="#V#synthetic_outsider@synthetic_outsider_org",
        turn_scope="turn-actor-cache",
        discovery_func=_discover,
    )
    with access_control.override_current_actor(
        "#V#synthetic_outsider",
        OUTSIDER_ORG_ID,
    ):
        forged_owner_scope = memo_service.discover_workflows_for_turn_memoized(
            "Route this represented turn",
            namespace=owner_namespace,
            turn_scope="turn-actor-cache",
            discovery_func=_discover,
        )

    assert observed_actors == ["#V#synthetic_owner", "#V#synthetic_outsider"]
    assert owner_first is not None
    assert owner_cached is not None
    assert outsider is not None
    assert forged_owner_scope is not None
    assert owner_cached["workflow_discovery_cache"]["cache_hit"] is True
    assert outsider["matches"] == [{"concept_id": "#V#synthetic_outsider_workflow"}]
    assert forged_owner_scope["matches"] == []
    assert (
        forged_owner_scope["workflow_discovery_cache"]["cache_skipped_reason"]
        == "actor_scope_rejected"
    )


def _stale_discovery_payload() -> dict[str, object]:
    restricted = {
        "concept_id": COHORT_WORKFLOW_ID,
        "name": "Synthetic Cohort Workflow",
        "description": "Restricted metadata must disappear after revocation.",
        "routing_index_metadata": {"private_marker": "restricted-routing-data"},
    }
    global_entry = {
        "concept_id": GLOBAL_WORKFLOW_ID,
        "name": "Synthetic Global Workflow",
        "description": "Global represented purpose.",
    }
    return {
        "query": "Route this represented turn",
        "requested_query": "Route this represented turn",
        "matches": [restricted, global_entry],
        "candidates": [restricted, global_entry],
        "routing_matches": [restricted, global_entry],
        "routing_readiness_diagnostics": [
            {
                "workflow_id": COHORT_WORKFLOW_ID,
                "detail": "restricted-readiness-data",
            },
            {"workflow_id": GLOBAL_WORKFLOW_ID, "detail": "global-ready"},
        ],
        "selected_workflow_id": COHORT_WORKFLOW_ID,
        "contract_projection": {
            "workflow_concept_ids": [COHORT_WORKFLOW_ID, GLOBAL_WORKFLOW_ID],
            "required_tools": ["synthetic_fetch"],
        },
        "candidate_count": 2,
        "match_count": 2,
        "discovery_payload_origin": "synthetic_discovery",
    }


def test_turn_memo_reprojects_cached_metadata_after_visibility_revocation(
    monkeypatch: pytest.MonkeyPatch,
    visibility_collection,
) -> None:
    memo_service.clear_turn_workflow_discovery_memo()
    monkeypatch.setattr(memo_service, "_capability_index_version", lambda: "v1")
    discover = MagicMock(return_value=_stale_discovery_payload())
    namespace = "#V#synthetic_member@synthetic_trusted_org"

    with access_control.override_current_actor(
        "#V#synthetic_member",
        TRUSTED_ORG_ID,
    ):
        first = memo_service.discover_workflows_for_turn_memoized(
            "Route this represented turn",
            namespace=namespace,
            turn_scope="turn-live-revocation",
            discovery_func=discover,
        )
        assert COHORT_WORKFLOW_ID in access_control.filter_accessible_concept_ids(
            [COHORT_WORKFLOW_ID]
        )
        visibility_collection.update_one(
            {"concept_id": COHORT_WORKFLOW_ID},
            {
                "$set": {
                    "relationships": {
                        "#V#specific_to_organisation": [OUTSIDER_ORG_ID],
                    }
                }
            },
        )
        second = memo_service.discover_workflows_for_turn_memoized(
            "Route this represented turn",
            namespace=namespace,
            turn_scope="turn-live-revocation",
            discovery_func=discover,
        )

    discover.assert_called_once()
    assert first is not None
    assert second is not None
    assert second["workflow_discovery_cache"]["cache_hit"] is True
    assert second["workflow_discovery_cache"]["candidate_count"] == 1
    assert [row["concept_id"] for row in second["matches"]] == [
        GLOBAL_WORKFLOW_ID
    ]
    assert [row["concept_id"] for row in second["candidates"]] == [
        GLOBAL_WORKFLOW_ID
    ]
    assert [row["concept_id"] for row in second["routing_matches"]] == [
        GLOBAL_WORKFLOW_ID
    ]
    assert second["routing_readiness_diagnostics"] == [
        {"workflow_id": GLOBAL_WORKFLOW_ID, "detail": "global-ready"}
    ]
    assert "selected_workflow_id" not in second
    assert second["contract_projection"]["workflow_concept_ids"] == [
        GLOBAL_WORKFLOW_ID
    ]
    assert second["candidate_count"] == 1
    assert second["match_count"] == 1
    assert second["workflow_discovery_visibility_projection"]["status"] == (
        "filtered"
    )
    assert COHORT_WORKFLOW_ID not in repr(second)
    assert "restricted-routing-data" not in repr(second)
    assert "restricted-readiness-data" not in repr(second)


def test_turn_memo_cache_hit_fails_closed_when_visibility_authority_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    visibility_collection,
) -> None:
    del visibility_collection
    memo_service.clear_turn_workflow_discovery_memo()
    monkeypatch.setattr(memo_service, "_capability_index_version", lambda: "v1")
    discover = MagicMock(return_value=_stale_discovery_payload())
    namespace = "#V#synthetic_member@synthetic_trusted_org"

    with access_control.override_current_actor(
        "#V#synthetic_member",
        TRUSTED_ORG_ID,
    ):
        first = memo_service.discover_workflows_for_turn_memoized(
            "Route this represented turn",
            namespace=namespace,
            turn_scope="turn-authority-unavailable",
            discovery_func=discover,
        )
        monkeypatch.setattr(access_control, "get_concepts_collection", lambda: None)
        second = memo_service.discover_workflows_for_turn_memoized(
            "Route this represented turn",
            namespace=namespace,
            turn_scope="turn-authority-unavailable",
            discovery_func=discover,
        )

    assert first is not None
    assert second is not None
    discover.assert_called_once()
    assert second["matches"] == []
    assert second["candidates"] == []
    assert second["routing_matches"] == []
    assert second["routing_readiness_diagnostics"] == []
    assert "selected_workflow_id" not in second
    assert second["contract_projection"]["workflow_concept_ids"] == []
    assert second["match_absence_reason"] == (
        "workflow_discovery_visibility_authority_unavailable"
    )
    assert second["workflow_discovery_visibility_projection"]["status"] == (
        "authority_unavailable"
    )
    assert COHORT_WORKFLOW_ID not in repr(second)
    assert GLOBAL_WORKFLOW_ID not in repr(second)


def test_durable_carried_discovery_is_reprojected_before_reuse(
    visibility_collection,
) -> None:
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.durable.turn_execution_actions import (
        _discover_turn_workflows_for_durable_action,
    )

    visibility_collection.update_one(
        {"concept_id": COHORT_WORKFLOW_ID},
        {
            "$set": {
                "relationships": {
                    "#V#specific_to_organisation": [OUTSIDER_ORG_ID],
                }
            }
        },
    )
    request = WorkflowActionRequest(
        action_id="turn_execution.prepare_selector_context",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=None,
            user_namespace="#V#synthetic_member@synthetic_trusted_org",
        ),
        data={
            "user_prompt": "Route this represented turn",
            "workflow_discovery_result": _stale_discovery_payload(),
        },
    )

    projected = _discover_turn_workflows_for_durable_action(request)

    assert projected["discovery_payload_origin"] == (
        "durable_action_reused_cached_workflow_discovery"
    )
    assert [row["concept_id"] for row in projected["matches"]] == [
        GLOBAL_WORKFLOW_ID
    ]
    assert [row["concept_id"] for row in projected["candidates"]] == [
        GLOBAL_WORKFLOW_ID
    ]
    assert projected["routing_readiness_diagnostics"] == [
        {"workflow_id": GLOBAL_WORKFLOW_ID, "detail": "global-ready"}
    ]
    assert "selected_workflow_id" not in projected
    assert projected["contract_projection"]["workflow_concept_ids"] == [
        GLOBAL_WORKFLOW_ID
    ]
    assert COHORT_WORKFLOW_ID not in repr(projected)
    assert "restricted-routing-data" not in repr(projected)
