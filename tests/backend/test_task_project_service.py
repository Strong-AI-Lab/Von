import copy
import json

import pytest

from src.backend.services import task_project_service as projects


def test_project_catalogue_does_not_grow_with_retained_source_history(monkeypatch):
    record = {
        "name": "Research project",
        "key": "RESEARCH",
        "description": "The complete project description",
        "writer": "jira",
        "source_id": "123",
        "collection_concept_ids": ["#V#research_tasks"],
        "source_archive_history": [{"manifest": "x" * 100_000}],
        "external_document_archives": [{"manifest": "y" * 100_000}],
    }
    query_seen = {}

    def find(query, *, projection, limit, skip):
        query_seen.update(query=query, limit=limit, skip=skip)
        selected = {
            key: value
            for key, value in record.items()
            if projection.get("attributes.task_project")
            or projection.get(f"attributes.task_project.{key}")
        }
        return [
            {
                "concept_id": f"#V#project_{index}",
                "attributes": {"task_project": selected},
            }
            for index in range(skip, min(skip + limit, 43))
        ]

    monkeypatch.setattr(projects.ConceptsRepository, "find", find)
    result = projects.list_task_projects()
    assert len(result) == 43
    assert len(json.dumps({"success": True, "projects": result})) < 100_000
    assert result[0]["description"] == record["description"]
    assert result[0]["collection_concept_ids"] == record["collection_concept_ids"]
    assert result[0]["writer"] == "jira"
    assert result[0]["source_id"] == "123"
    assert projects.list_task_projects(limit=2, offset=41) == result[41:]
    assert query_seen == {
        "query": {"attributes.task_project": {"$exists": True}},
        "limit": 2,
        "skip": 41,
    }

    monkeypatch.setattr(projects, "can_access_concept", lambda key: True)
    monkeypatch.setattr(
        projects,
        "get_concept_by_concept_id_exact",
        lambda key: {"attributes": {"task_project": copy.deepcopy(record)}},
    )
    detail = projects.get_task_project("#V#project_0")
    assert detail["source_archive_history"] == record["source_archive_history"]
    assert detail["external_document_archives"] == record["external_document_archives"]


def test_project_collection_rerun_preserves_identity_and_writer(monkeypatch):
    docs = {}
    publish_during_refresh = False
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
        if publish_during_refresh:
            record = docs[key]["attributes"]["task_project"]
            record.update(writer="von", site_source_archive={"content_sha256": "new"})
            record["collection_concept_ids"].append("#V#new_board_collection")
        for path, value in fields.items():
            target = docs[key]
            parts = path.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = copy.deepcopy(value)

    monkeypatch.setattr(projects, "create_concept", create)
    monkeypatch.setattr(projects, "update_concept", update)
    source = {
        "id": "12",
        "key": "KKAT",
        "name": "KnowKat",
        "description": "The complete project description",
        "self": "https://jira.example/rest/api/3/project/12",
    }
    first = projects.ensure_jira_project(source, actor_concept_id="#V#owner")
    project_id = first["project_concept_id"]
    publish_during_refresh = True
    partial_source = {
        key: value for key, value in source.items() if key != "description"
    }
    second = projects.ensure_jira_project(
        {**partial_source, "name": "KnowKat updated"}, actor_concept_id="#V#owner"
    )
    assert len(docs) == 2
    assert second["project_concept_id"] == project_id
    assert second["collection"]["project_concept_ids"] == [project_id]
    assert second["project"]["writer"] == "von"
    assert second["project"]["name"] == "KnowKat updated"
    assert second["project"]["description"] == "The complete project description"
    assert second["project"]["site_source_archive"] == {"content_sha256": "new"}
    assert "#V#new_board_collection" in second["project"]["collection_concept_ids"]
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
