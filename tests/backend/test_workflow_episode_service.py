from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest

from src.backend.db.mongo_client import get_db
from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.services import concept_service
from src.backend.services import workflow_episode_service
from src.backend.services.workflow_episode_service import (
    build_workflow_episode_stable_key,
    finalise_workflow_use_episode,
    get_latest_workflow_use_episode,
    get_workflow_episode_counts_for_workflows,
    get_workflow_usage_aggregates_for_workflows,
    list_workflow_use_episodes,
    start_workflow_use_episode,
)


@pytest.fixture(autouse=True)
def reset_episode_state(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    import src.backend.db.mongo_client as mongo_client
    from src.backend.services import workflow_episode_service

    importlib.reload(mongo_client)
    importlib.reload(workflow_episode_service)

    db = mongo_client.get_db()
    if db is not None:
        try:
            db.drop_collection("workflow_use_episodes")
            db.drop_collection("concepts")
            db.drop_collection("text_relations")
            db.drop_collection("text_values")
        except Exception:
            pass
    yield


def _ensure_workflow_concept(workflow_id: str) -> None:
    concept_service.create_concept(
        concept_id=workflow_id,
        name="Workflow Episode Test",
        instance_of_type="#V#type",
    )


def test_workflow_episode_idempotent_start_for_stable_key():
    workflow_id = "#V#workflow_episode_idempotent"
    stable_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-1",
        session_id="session-1",
        stage="tool_calling",
    )

    first = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        turn_id="turn-1",
        session_id="session-1",
    )
    second = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        turn_id="turn-1",
        session_id="session-1",
    )

    assert first is not None
    assert second is not None
    assert first["episode_id"] == second["episode_id"]

    episodes = list_workflow_use_episodes(workflow_id=workflow_id, limit=20)
    assert len(episodes) == 1
    assert episodes[0]["episode_id"] == first["episode_id"]

    aggregate = get_workflow_usage_aggregates_for_workflows([workflow_id])[workflow_id]
    assert aggregate["attempts"] == 1
    assert aggregate["completions"] == 0


def test_workflow_episode_aggregates_sync_to_workflow_concept():
    workflow_id = "#V#workflow_episode_aggregate_sync"
    _ensure_workflow_concept(workflow_id)

    first_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-a",
        session_id="session-a",
        stage="tool_calling",
    )
    second_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-b",
        session_id="session-a",
        stage="tool_calling",
    )

    start_first = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=first_key,
        turn_id="turn-a",
        session_id="session-a",
    )
    assert start_first is not None
    finalise_workflow_use_episode(
        workflow_id=workflow_id,
        stable_key=first_key,
        completed=False,
        terminal_stage="tool_execution",
        termination_code="tool_timeout",
        termination_detail="Tool invocation timed out",
    )

    start_second = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=second_key,
        turn_id="turn-b",
        session_id="session-a",
    )
    assert start_second is not None
    finalise_workflow_use_episode(
        workflow_id=workflow_id,
        stable_key=second_key,
        completed=True,
        terminal_stage="completed",
        termination_code="completed",
        termination_detail=None,
    )

    aggregate = get_workflow_usage_aggregates_for_workflows([workflow_id])[workflow_id]
    assert aggregate["attempts"] == 2
    assert aggregate["completions"] == 1
    assert aggregate["completion_rate"] == pytest.approx(0.5)

    concept_doc = ConceptsRepository.find_one(
        {"concept_id": workflow_id},
        projection={"concept_data.workflow_use_aggregates": 1},
    )
    assert concept_doc is not None
    usage = (concept_doc.get("concept_data") or {}).get("workflow_use_aggregates") or {}
    assert usage.get("attempts") == 2
    assert usage.get("completions") == 1
    assert usage.get("completion_rate") == pytest.approx(0.5)


def test_workflow_episode_hot_path_updates_aggregates_without_log_scan(
    monkeypatch: pytest.MonkeyPatch,
):
    workflow_id = "#V#workflow_episode_delta_sync"
    _ensure_workflow_concept(workflow_id)
    stable_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-delta",
        session_id="session-delta",
        stage="tool_calling",
    )

    monkeypatch.setattr(
        workflow_episode_service,
        "_compute_episode_aggregate",
        lambda *_args, **_kwargs: pytest.fail("hot path should not scan episode log"),
    )

    started = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        turn_id="turn-delta",
        session_id="session-delta",
    )
    finished = finalise_workflow_use_episode(
        workflow_id=workflow_id,
        stable_key=stable_key,
        completed=True,
        terminal_stage="completed",
    )

    assert started is not None
    assert finished is not None
    aggregate = get_workflow_usage_aggregates_for_workflows([workflow_id])[workflow_id]
    assert aggregate["attempts"] == 1
    assert aggregate["completions"] == 1
    assert aggregate["completion_rate"] == pytest.approx(1.0)


def test_workflow_episode_duplicate_finalise_does_not_double_count_completion():
    workflow_id = "#V#workflow_episode_duplicate_finalise"
    _ensure_workflow_concept(workflow_id)
    stable_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-duplicate-finalise",
        session_id="session-duplicate-finalise",
        stage="tool_calling",
    )

    started = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        turn_id="turn-duplicate-finalise",
        session_id="session-duplicate-finalise",
    )
    first_finish = finalise_workflow_use_episode(
        workflow_id=workflow_id,
        stable_key=stable_key,
        completed=True,
        terminal_stage="completed",
    )
    second_finish = finalise_workflow_use_episode(
        workflow_id=workflow_id,
        stable_key=stable_key,
        completed=True,
        terminal_stage="completed",
    )

    assert started is not None
    assert first_finish is not None
    assert second_finish is not None
    aggregate = get_workflow_usage_aggregates_for_workflows([workflow_id])[workflow_id]
    assert aggregate["attempts"] == 1
    assert aggregate["completions"] == 1


def test_workflow_episode_can_skip_concept_aggregate_sync():
    workflow_id = "#V#workflow_episode_skip_aggregate_sync"
    _ensure_workflow_concept(workflow_id)
    stable_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-skip-sync",
        session_id="session-skip-sync",
        stage="tool_calling",
    )

    started = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        turn_id="turn-skip-sync",
        session_id="session-skip-sync",
        sync_aggregates=False,
    )
    finished = finalise_workflow_use_episode(
        workflow_id=workflow_id,
        stable_key=stable_key,
        completed=True,
        terminal_stage="completed",
        termination_code="completed",
        termination_detail=None,
        sync_aggregates=False,
    )

    assert started is not None
    assert started["aggregate"] is None
    assert finished is not None
    assert finished["aggregate"] is None

    concept_doc = ConceptsRepository.find_one(
        {"concept_id": workflow_id},
        projection={"concept_data.workflow_use_aggregates": 1},
    )
    assert concept_doc is not None
    usage = (concept_doc.get("concept_data") or {}).get("workflow_use_aggregates") or {}
    assert usage == {}


def test_list_workflow_use_episodes_matches_equivalent_namespace_forms():
    workflow_id = "#V#workflow_episode_namespace_equivalence"
    stable_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-ns",
        session_id="session-ns",
        stage="tool_calling",
    )

    created = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        namespace="#V#michael_witbrock/#V#university_of_auckland_strong_ai_lab",
        turn_id="turn-ns",
        session_id="session-ns",
    )
    assert created is not None

    # Query using the modern user@org form should still match the stored slash form.
    episodes = list_workflow_use_episodes(
        workflow_id=workflow_id,
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        limit=20,
    )
    assert len(episodes) == 1
    assert episodes[0]["episode_id"] == created["episode_id"]


def test_actor_scoped_episode_query_excludes_legacy_and_other_org_namespaces():
    workflow_id = "#V#workflow_episode_strict_actor_scope"
    namespaces = (
        "#V#scope_user/#V#org_a",
        "#V#scope_user/#V#org_b",
        "#V#scope_user",
        "#V#scope_user/default",
    )
    for index, namespace in enumerate(namespaces, start=1):
        created = start_workflow_use_episode(
            workflow_id=workflow_id,
            source="chat_turn_workflow",
            stable_key=build_workflow_episode_stable_key(
                workflow_id=workflow_id,
                source="chat_turn_workflow",
                turn_id=f"turn-strict-{index}",
                stage="tool_calling",
            ),
            namespace=namespace,
            turn_id=f"turn-strict-{index}",
        )
        assert created is not None

    episodes = list_workflow_use_episodes(
        workflow_ids=[workflow_id],
        namespace="#V#scope_user@org_a",
        strict_namespace_scope=True,
        limit=20,
    )
    assert [episode["namespace"] for episode in episodes] == [
        "#V#scope_user/#V#org_a"
    ]
    assert (
        list_workflow_use_episodes(
            workflow_ids=[],
            namespace="#V#scope_user@org_a",
            strict_namespace_scope=True,
            limit=20,
        )
        == []
    )


def test_get_workflow_episode_counts_for_workflows_matches_namespace_equivalents():
    workflow_id = "#V#workflow_episode_counts_namespace_equivalence"
    stable_key = build_workflow_episode_stable_key(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        turn_id="turn-counts",
        session_id="session-counts",
        stage="tool_calling",
    )

    created = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=stable_key,
        namespace="#V#michael_witbrock/#V#university_of_auckland_strong_ai_lab",
        turn_id="turn-counts",
        session_id="session-counts",
    )
    assert created is not None

    counts = get_workflow_episode_counts_for_workflows(
        [workflow_id, "#V#missing_workflow_id"],
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    )
    assert counts[workflow_id] == 1
    assert counts["#V#missing_workflow_id"] == 0


def test_actor_scoped_usage_aggregates_do_not_use_global_workflow_totals():
    workflow_id = "#V#workflow_actor_scoped_metrics"
    episodes: list[tuple[str, str, bool]] = [
        ("#V#same_user@org_a", "turn-org-a", True),
        ("#V#same_user@org_b", "turn-org-b", False),
    ]
    for namespace, turn_id, completed in episodes:
        stable_key = build_workflow_episode_stable_key(
            workflow_id=workflow_id,
            source="chat_turn_workflow",
            turn_id=turn_id,
            session_id=f"session-{turn_id}",
            stage="tool_calling",
        )
        created = start_workflow_use_episode(
            workflow_id=workflow_id,
            source="chat_turn_workflow",
            stable_key=stable_key,
            namespace=namespace,
            turn_id=turn_id,
            session_id=f"session-{turn_id}",
        )
        assert created is not None
        finalise_workflow_use_episode(
            workflow_id=workflow_id,
            stable_key=stable_key,
            completed=completed,
            terminal_stage="done",
        )

    aggregates = get_workflow_usage_aggregates_for_workflows(
        [workflow_id],
        namespace="#V#same_user@org_a",
        strict_namespace_scope=True,
    )
    assert aggregates[workflow_id]["attempts"] == 1
    assert aggregates[workflow_id]["completions"] == 1
    assert aggregates[workflow_id]["completion_rate"] == 1.0

    counts = get_workflow_episode_counts_for_workflows(
        [workflow_id],
        namespace="#V#same_user@org_a",
        strict_namespace_scope=True,
    )
    assert counts[workflow_id] == 1

    unscoped_strict = get_workflow_usage_aggregates_for_workflows(
        [workflow_id],
        strict_namespace_scope=True,
    )
    assert unscoped_strict[workflow_id]["attempts"] == 0


def test_namespace_equivalence_includes_user_only_and_default_legacy_forms():
    workflow_id = "#V#workflow_episode_legacy_namespace_forms"

    episode_one = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=build_workflow_episode_stable_key(
            workflow_id=workflow_id,
            source="chat_turn_workflow",
            turn_id="turn-legacy-user",
            session_id="session-legacy",
            stage="tool_calling",
        ),
        namespace="#V#michael_witbrock",
        turn_id="turn-legacy-user",
        session_id="session-legacy",
    )
    episode_two = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=build_workflow_episode_stable_key(
            workflow_id=workflow_id,
            source="chat_turn_workflow",
            turn_id="turn-legacy-default",
            session_id="session-legacy",
            stage="tool_calling",
        ),
        namespace="#V#michael_witbrock/default",
        turn_id="turn-legacy-default",
        session_id="session-legacy",
    )
    assert episode_one is not None
    assert episode_two is not None

    episodes = list_workflow_use_episodes(
        workflow_id=workflow_id,
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        limit=20,
    )
    assert len(episodes) == 2

    counts = get_workflow_episode_counts_for_workflows(
        [workflow_id],
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    )
    assert counts[workflow_id] == 2


def test_usage_aggregates_skip_episode_log_fallback_when_concept_aggregates_exist(
    monkeypatch: pytest.MonkeyPatch,
):
    workflow_ids = ["#V#workflow_a", "#V#workflow_b"]

    monkeypatch.setattr(
        workflow_episode_service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "concept_id": workflow_ids[0],
                "concept_data": {
                    "workflow_use_aggregates": {
                        "attempts": 3,
                        "completions": 2,
                        "completion_rate": 2 / 3,
                        "last_episode_at": datetime(
                            2026, 3, 5, 12, 0, tzinfo=timezone.utc
                        ),
                        "updated_at": datetime(2026, 3, 5, 12, 5, tzinfo=timezone.utc),
                    }
                },
            },
            {
                "concept_id": workflow_ids[1],
                "concept_data": {
                    "workflow_use_aggregates": {
                        "attempts": 1,
                        "completions": 1,
                        "completion_rate": 1.0,
                    }
                },
            },
        ],
    )
    monkeypatch.setattr(
        workflow_episode_service,
        "_get_collection",
        lambda: pytest.fail("episode log aggregate fallback should not run"),
    )

    aggregates = get_workflow_usage_aggregates_for_workflows(workflow_ids)

    assert aggregates[workflow_ids[0]]["attempts"] == 3
    assert aggregates[workflow_ids[0]]["completions"] == 2
    assert aggregates[workflow_ids[1]]["attempts"] == 1


def test_usage_aggregates_fallback_queries_only_missing_concept_aggregates(
    monkeypatch: pytest.MonkeyPatch,
):
    populated_id = "#V#workflow_populated"
    missing_id = "#V#workflow_missing"
    captured_pipeline = {}

    class FakeCollection:
        def aggregate(self, pipeline):
            captured_pipeline["pipeline"] = pipeline
            return [
                {
                    "_id": missing_id,
                    "attempts": 4,
                    "completions": 1,
                    "last_episode_at": datetime(
                        2026, 3, 5, 12, 10, tzinfo=timezone.utc
                    ),
                }
            ]

    monkeypatch.setattr(
        workflow_episode_service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "concept_id": populated_id,
                "concept_data": {
                    "workflow_use_aggregates": {
                        "attempts": 8,
                        "completions": 8,
                        "completion_rate": 1.0,
                    }
                },
            }
        ],
    )
    monkeypatch.setattr(
        workflow_episode_service, "_get_collection", lambda: FakeCollection()
    )

    aggregates = get_workflow_usage_aggregates_for_workflows([populated_id, missing_id])

    assert captured_pipeline["pipeline"][0]["$match"] == {
        "workflow_id": {"$in": [missing_id]}
    }
    assert aggregates[populated_id]["attempts"] == 8
    assert aggregates[missing_id]["attempts"] == 4
    assert aggregates[missing_id]["completion_rate"] == 0.25


def test_get_latest_workflow_use_episode_returns_newest_session_episode():
    workflow_id = "#V#workflow_episode_latest_lookup"
    namespace = "#V#michael_witbrock/#V#university_of_auckland_strong_ai_lab"

    first = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=build_workflow_episode_stable_key(
            workflow_id=workflow_id,
            source="chat_turn_workflow",
            turn_id="turn-old",
            session_id="session-latest",
            stage="tool_calling",
        ),
        namespace=namespace,
        turn_id="turn-old",
        session_id="session-latest",
    )
    second = start_workflow_use_episode(
        workflow_id=workflow_id,
        source="chat_turn_workflow",
        stable_key=build_workflow_episode_stable_key(
            workflow_id=workflow_id,
            source="chat_turn_workflow",
            turn_id="turn-new",
            session_id="session-latest",
            stage="tool_calling",
        ),
        namespace=namespace,
        turn_id="turn-new",
        session_id="session-latest",
    )

    assert first is not None
    assert second is not None

    db = get_db()
    assert db is not None
    coll = db["workflow_use_episodes"]
    coll.update_one(
        {"episode_id": first["episode_id"]},
        {
            "$set": {
                "attempt_started_at": datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)
            }
        },
    )
    coll.update_one(
        {"episode_id": second["episode_id"]},
        {
            "$set": {
                "attempt_started_at": datetime(2026, 3, 5, 12, 5, tzinfo=timezone.utc)
            }
        },
    )

    latest = get_latest_workflow_use_episode(
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        session_id="session-latest",
    )
    assert latest is not None
    assert latest["episode_id"] == second["episode_id"]
    assert latest["turn_id"] == "turn-new"
    assert latest["workflow_id"] == workflow_id
