from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import mongomock

from src.backend.integrations.internal_mcp import catalogue
from src.backend.security.access_control import override_current_actor
from src.backend.services import task_management_service


def _task_doc(
    concept_id: str,
    *,
    user_id: str | None = None,
    organisation_id: str | None = None,
) -> dict[str, Any]:
    relationships: dict[str, Any] = {
        "is_an_instance_of": ["#V#task_specification"],
    }
    if user_id is not None:
        relationships["#V#specific_to_user"] = [user_id]
        relationships["#V#hasAssignee"] = [user_id]
    if organisation_id is not None:
        relationships["#V#specific_to_organisation"] = [organisation_id]
    return {
        "concept_id": concept_id,
        "relationships": relationships,
        "metadata": {
            "comments": [{"body": f"private comment for {concept_id}"}],
            "attachments": [],
            "worklog": [],
            "task_history": [],
        },
    }


def _task_ids(payload: dict[str, Any]) -> set[str]:
    return {
        str(task["task_concept_id"])
        for task in payload.get("tasks", [])
        if isinstance(task, dict) and task.get("task_concept_id")
    }


def test_task_reads_inherit_repository_actor_visibility(
    monkeypatch,
) -> None:
    concepts = mongomock.MongoClient().von_test.concepts
    concepts.insert_many(
        [
            _task_doc(
                "#V#task_actor_a",
                user_id="#V#actor_a",
                organisation_id="#V#org_a",
            ),
            _task_doc(
                "#V#task_actor_b",
                user_id="#V#actor_b",
                organisation_id="#V#org_b",
            ),
            _task_doc("#V#task_global"),
        ]
    )
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.get_concepts_collection",
        lambda: concepts,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_concepts_collection",
        lambda: concepts,
    )
    monkeypatch.setattr(
        task_management_service,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [],
    )

    with override_current_actor("#V#actor_a", "#V#org_a"):
        exact_other_actor = catalogue._task_get(
            task_concept_id="#V#task_actor_b"
        )
        unfiltered_list = catalogue._task_list()
        other_actor_list = catalogue._task_list(
            user_concept_id="#V#actor_b"
        )
        unfiltered_search = catalogue._task_search()
        other_actor_comments = catalogue._task_list_comments(
            task_concept_id="#V#task_actor_b"
        )

    assert exact_other_actor["success"] is False
    assert exact_other_actor["error_code"] == "NOT_FOUND"
    assert _task_ids(unfiltered_list) == {
        "#V#task_actor_a",
        "#V#task_global",
    }
    assert _task_ids(other_actor_list) == set()
    assert _task_ids(unfiltered_search) == {
        "#V#task_actor_a",
        "#V#task_global",
    }
    assert other_actor_comments["success"] is False
    assert other_actor_comments["error_code"] == "NOT_FOUND"


def test_task_list_cap_is_applied_after_actor_visibility(
    monkeypatch,
) -> None:
    """An inaccessible created task cannot consume the caller's task page."""
    concepts = mongomock.MongoClient().von_test.concepts
    now = datetime.now(timezone.utc)
    inaccessible = _task_doc(
        "#V#task_private_to_actor_b",
        user_id="#V#actor_b",
        organisation_id="#V#org_b",
    )
    inaccessible["relationships"]["#V#hasCreatedBy"] = ["#V#actor_a"]
    inaccessible["updated_at"] = now + timedelta(minutes=1)
    visible = _task_doc(
        "#V#task_visible_to_actor_a",
        user_id="#V#actor_a",
        organisation_id="#V#org_a",
    )
    visible["updated_at"] = now
    concepts.insert_many([inaccessible, visible])
    monkeypatch.setattr(
        "src.backend.db.repositories.concepts_repository.get_concepts_collection",
        lambda: concepts,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_concepts_collection",
        lambda: concepts,
    )
    monkeypatch.setattr(
        task_management_service,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: {},
    )

    with override_current_actor("#V#actor_a", "#V#org_a"):
        payload = catalogue._task_list(
            user_concept_id="#V#actor_a",
            include_created=True,
            limit=1,
        )

    assert _task_ids(payload) == {"#V#task_visible_to_actor_a"}
    assert payload["count"] == 1
    assert payload["total"] == 1
    assert payload["has_more"] is False
