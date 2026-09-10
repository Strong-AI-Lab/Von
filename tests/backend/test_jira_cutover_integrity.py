"""Outcome tests for interrupted migration, attachment integrity and cutover."""

import base64
import copy
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.backend.services import jira_source_retention_service as retention
from src.backend.services import jira_task_import_service as importer
from src.backend.services import jira_task_reconciliation_service as reconciliation
from src.backend.services import task_management_service as tasks
from src.backend.services import task_project_service as projects


def test_interrupted_activity_append_returns_original_without_duplicate_history(
    monkeypatch,
):
    original = {
        "comment_id": "original",
        "body": "preserve me",
        "source": {"source_system": "jira", "external_id": "123"},
    }
    doc = {"metadata": {"comments": [original]}}
    monkeypatch.setattr(tasks, "_get_task_doc", lambda key: (key, doc))
    queries = []
    monkeypatch.setattr(
        tasks.ConceptsRepository,
        "update_one",
        lambda query, update: (
            queries.append(query) or SimpleNamespace(matched_count=0)
        ),
    )
    monkeypatch.setattr(
        tasks,
        "_append_task_history_event",
        lambda **kwargs: pytest.fail("duplicate event"),
    )
    result = tasks.add_task_comment(
        "#V#task", body="preserve me", source=original["source"]
    )
    assert result == original
    assert queries[0]["metadata.comments"]["$not"]["$elemMatch"] == {
        "source.source_system": "jira",
        "source.external_id": "123",
    }


def test_dates_compare_by_instant_and_cross_batch_relations_do_not_fetch_jira(
    monkeypatch,
):
    assert importer._projection_value(
        "due_date", datetime(2026, 9, 10, tzinfo=UTC)
    ) == importer._projection_value("due_date", "2026-09-10T12:00:00+12:00")
    calls = []
    monkeypatch.setattr(
        importer,
        "set_task_parent",
        lambda child, parent, **kw: calls.append((child, parent)),
    )
    monkeypatch.setattr(
        importer,
        "find_task_by_external_reference",
        lambda **kw: pytest.fail("target should be in retained rows"),
    )
    rows = [
        {
            "jira_issue_key": "KKAT-2",
            "task_concept_id": "#V#child",
            "parent_issue_key": "KKAT-1",
            "action": "created",
        },
        {
            "jira_issue_key": "KKAT-1",
            "task_concept_id": "#V#parent",
            "action": "created",
        },
    ]
    report = importer.repair_jira_task_relations(rows, actor_concept_id="#V#owner")
    assert calls == [("#V#child", "#V#parent")]
    assert report == {"mapped_relations": 1, "dropped_relations": 0}


def test_refresh_preserves_native_hierarchy_edits(monkeypatch):
    monkeypatch.setattr(
        importer, "get_task", lambda key: {"parent_task_concept_id": "#V#native_parent"}
    )
    monkeypatch.setattr(
        importer,
        "set_task_parent",
        lambda *args, **kw: pytest.fail("Native parent must survive"),
    )
    rows = [
        {
            "jira_issue_key": "KKAT-2",
            "task_concept_id": "#V#child",
            "parent_issue_key": "KKAT-1",
            "guard_existing_hierarchy": True,
            "hierarchy_baseline": {"parent": "KKAT-1"},
            "action": "updated",
        },
        {
            "jira_issue_key": "KKAT-1",
            "task_concept_id": "#V#source_parent",
            "action": "updated",
        },
    ]
    result = importer.repair_jira_task_relations(rows, actor_concept_id="#V#owner")
    assert result["dropped_relations"] == 1
    assert "Native parent edit preserved" in rows[0]["relation_results"][0]["reason"]


@pytest.mark.asyncio
async def test_enhanced_board_pagination_requires_progress_and_terminal_evidence():
    tokens = []

    class Source:
        async def get_migration_resource(self, **kwargs):
            tokens.append(kwargs["next_page_token"])
            if len(tokens) == 1:
                return {
                    "issues": [{"id": "1"}],
                    "nextPageToken": "next",
                    "isLast": False,
                }
            return {"issues": [{"id": "2"}], "isLast": True}

    result = await retention.capture_resource(
        Source(), "board_issues", "12", item_key="issues"
    )
    assert result["complete"] and result["count"] == 2
    assert tokens == [None, "next"]

    class Stalled:
        async def get_migration_resource(self, **kwargs):
            return {"issues": [{"id": "1"}], "nextPageToken": "same", "isLast": False}

    result = await retention.capture_resource(
        Stalled(), "board_issues", "12", item_key="issues"
    )
    assert not result["complete"]


def test_attachment_upload_is_stored_and_read_back_before_linking(monkeypatch):
    from src.backend.services import computer_file_copy_service as files

    monkeypatch.setattr(tasks, "_get_task_doc", lambda key: (key, {"metadata": {}}))
    writes = []
    monkeypatch.setattr(
        files,
        "import_bytes_file_copy",
        lambda **kw: (writes.append(kw) or {"success": True, "concept_id": "#V#file"}),
    )
    monkeypatch.setattr(
        files,
        "fetch_file_copy_bytes",
        lambda **kw: {"success": True, "data": b"original"},
    )
    monkeypatch.setattr(tasks, "add_task_attachment", lambda key, **kw: kw)
    saved = tasks.add_task_attachment_bytes(
        "#V#task",
        data=b"original",
        filename="evidence.txt",
        actor_concept_id="#V#owner",
    )
    assert saved["file_copy_concept_id"] == "#V#file"
    assert saved["source"]["sha256"] == hashlib.sha256(b"original").hexdigest()
    assert writes[0]["visibility_scope_mode"] == "user_only_default"
    monkeypatch.setattr(
        files,
        "fetch_file_copy_bytes",
        lambda **kw: {"success": True, "data": b"corrupted"},
    )
    with pytest.raises(tasks.TaskManagementError, match="read-back"):
        tasks.add_task_attachment_bytes(
            "#V#task",
            data=b"original",
            filename="evidence.txt",
            actor_concept_id="#V#owner",
        )


def test_imported_assignment_is_provenance_without_a_new_audience(monkeypatch):
    monkeypatch.setattr(
        tasks, "_get_task_doc", lambda key: (key, {"relationships": {}})
    )
    edges = []
    monkeypatch.setattr(
        tasks.ConceptsRepository,
        "mutate_relationship_edge",
        lambda **kw: edges.append(kw),
    )
    monkeypatch.setattr(tasks.ConceptsRepository, "update_one", lambda *args: None)
    monkeypatch.setattr(tasks, "_append_task_history_event", lambda **kw: None)
    monkeypatch.setattr(tasks, "get_task", lambda key: {"task_concept_id": key})
    tasks.assign_task(
        "#V#task", "#V#historical_source_assignee", update_visibility=False
    )
    assert edges and {edge["kind"] for edge in edges} == {tasks.PREDICATE_HAS_ASSIGNEE}


def test_native_subtask_stays_in_its_parents_project_and_collections(monkeypatch):
    metadata = {
        "project_concept_id": "#V#project",
        "collection_concept_ids": ["#V#collection"],
        "organisation_concept_id": None,
    }
    monkeypatch.setattr(
        tasks, "_get_task_doc", lambda key: (key, {"metadata": metadata})
    )
    created = []
    monkeypatch.setattr(
        tasks,
        "create_task",
        lambda **kwargs: (created.append(kwargs) or {"task_concept_id": "#V#child"}),
    )
    monkeypatch.setattr(
        tasks,
        "set_task_parent",
        lambda child, parent, **kwargs: {
            "task_concept_id": child,
            "parent_task_concept_id": parent,
        },
    )
    result = tasks.create_subtask(
        "#V#parent", title="Child", description="Native project task"
    )
    assert created[0]["project_concept_id"] == "#V#project"
    assert created[0]["collection_concept_ids"] == ["#V#collection"]
    assert result["task"]["parent_task_concept_id"] == "#V#parent"


def test_reconciliation_detects_wrong_relation_and_corrupt_retained_binary(monkeypatch):
    source = [
        {
            "key": "KKAT-1",
            "id": "1",
            "fields": {
                "project": {"key": "KKAT"},
                "updated": "now",
                "parent": {"key": "OTHER-1"},
                "issuetype": {"name": "Document"},
            },
        }
    ]
    project = {
        "concept_id": "#V#project",
        "key": "KKAT",
        "source_id": "12",
        "collection_concept_ids": ["#V#collection"],
        "source_archive": {"complete": True, "file_copy_concept_id": "project"},
    }
    task = {
        "concept_id": "#V#task",
        "relationships": {},
        "metadata": {
            "project_concept_id": "#V#project",
            "collection_concept_ids": ["#V#collection"],
            "external_references": {
                "jira": {
                    "external_id": "KKAT-1",
                    "issue_id": "1",
                    "updated": "now",
                    "source_archive": {
                        "complete": True,
                        "file_copy_concept_id": "issue",
                    },
                }
            },
        },
    }
    target = {
        "concept_id": "#V#other",
        "metadata": {"external_references": {"jira": {"external_id": "OTHER-1"}}},
    }
    from src.backend.db.repositories.concepts_repository import ConceptsRepository

    monkeypatch.setattr(
        ConceptsRepository,
        "find",
        lambda query, **kw: [task] if "$or" in query else [target],
    )
    monkeypatch.setattr(projects, "get_task_project", lambda key: project)
    monkeypatch.setattr(
        projects,
        "get_task_collection",
        lambda key: {
            "concept_id": key,
            "project_concept_ids": ["#V#project"],
            "selection": {"kind": "project_membership"},
        },
    )

    def archive(ref, **kwargs):
        if ref["file_copy_concept_id"] == "project":
            return {"complete": True, "source": {"id": "12"}}
        return {
            "complete": True,
            "source": source[0],
            "binaries": [
                {
                    "id": "file",
                    "complete": True,
                    "source": {"content_base64": base64.b64encode(b"wrong").decode()},
                    "size_bytes": 5,
                    "sha256": hashlib.sha256(b"right").hexdigest(),
                }
            ],
        }

    monkeypatch.setattr(retention, "read_source_archive", archive)
    result = reconciliation.reconcile_retained_project(
        source_issues=source,
        project_concept_id="#V#project",
        actor_concept_id="#V#owner",
    )
    assert not result["retention_reconciled"]
    assert {p["kind"] for p in result["problems"]} == {
        "archive_readback_failed",
        "relation_mismatch",
    }


def test_cutover_requires_all_material_receipts_and_readback(monkeypatch):
    project = {"concept_id": "#V#project", "writer": "jira"}
    monkeypatch.setattr(
        projects, "get_task_project", lambda key: copy.deepcopy(project)
    )

    def update(key, fields):
        for name, value in fields.items():
            project[name.rsplit(".", 1)[-1]] = value

    monkeypatch.setattr(projects, "update_concept", update)
    evidence = {
        "reconciliation": {
            "project_concept_id": "#V#project",
            "retention_reconciled": True,
        }
    }
    with pytest.raises(ValueError, match="receipts"):
        projects.set_task_project_writer(
            "#V#project", writer="von", actor_concept_id="#V#owner", evidence=evidence
        )
    assert project["writer"] == "jira"
    evidence.update(
        {
            key: {"receipt": key}
            for key in (
                "site_retention_receipt",
                "ordinary_use_receipt",
                "restore_receipt",
                "final_delta_receipt",
            )
        }
    )
    saved = projects.set_task_project_writer(
        "#V#project", writer="von", actor_concept_id="#V#owner", evidence=evidence
    )
    assert saved["writer"] == "von" and saved["writer_history"][0]["from"] == "jira"
