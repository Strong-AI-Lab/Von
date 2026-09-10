import copy

import pytest

from src.backend.services import task_project_service as projects


def test_project_collection_rerun_preserves_identity_and_writer(monkeypatch):
    docs = {}
    monkeypatch.setattr(projects, "can_access_concept", lambda key: True)
    monkeypatch.setattr(
        projects,
        "get_concept_by_concept_id_exact",
        lambda key: copy.deepcopy(docs.get(key)),
    )

    def create(**kwargs):
        key = kwargs["concept_id"]
        assert key not in docs
        assert kwargs["created_by_concept_id"] == "#V#owner"
        assert not kwargs["maintain_relationship_inverses"]
        docs[key] = copy.deepcopy(kwargs)
        return docs[key]

    def update(key, fields):
        for path, value in fields.items():
            docs[key]["attributes"][path.split(".", 1)[1]] = copy.deepcopy(value)

    monkeypatch.setattr(projects, "create_concept", create)
    monkeypatch.setattr(projects, "update_concept", update)
    source = {
        "id": "12",
        "key": "KKAT",
        "name": "KnowKat",
        "self": "https://jira.example/rest/api/3/project/12",
    }
    first = projects.ensure_jira_project(source, actor_concept_id="#V#owner")
    project_id = first["project_concept_id"]
    docs[project_id]["attributes"]["task_project"]["writer"] = "von"
    second = projects.ensure_jira_project(
        {**source, "name": "KnowKat updated"}, actor_concept_id="#V#owner"
    )
    assert len(docs) == 2
    assert second["project_concept_id"] == project_id
    assert second["collection"]["project_concept_ids"] == [project_id]
    assert second["project"]["writer"] == "von"
    assert (
        projects.validate_task_membership(project_id, second["collection_concept_ids"])[
            1
        ]
        == second["collection_concept_ids"]
    )


def test_membership_cannot_select_inaccessible_or_wrong_project(monkeypatch):
    monkeypatch.setattr(projects, "can_access_concept", lambda key: key != "#V#private")
    monkeypatch.setattr(
        projects,
        "get_concept_by_concept_id_exact",
        lambda key: {
            "attributes": {
                "task_project": {},
                "task_collection": {"project_concept_ids": ["#V#different"]},
            }
        },
    )
    with pytest.raises(ValueError, match="not accessible"):
        projects.validate_task_membership("#V#private", [])
    with pytest.raises(ValueError, match="does not cover"):
        projects.validate_task_membership("#V#project", ["#V#collection"])


def test_private_view_does_not_break_shared_project_navigation(monkeypatch):
    monkeypatch.setattr(projects, "can_access_concept", lambda key: key == "#V#shared")
    monkeypatch.setattr(
        projects, "get_task_collection", lambda key: {"concept_id": key}
    )
    assert projects.list_project_collections(
        {"collection_concept_ids": ["#V#private", "#V#shared"]}
    ) == [{"concept_id": "#V#shared"}]
