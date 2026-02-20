from __future__ import annotations

from typing import Any, Dict

from src.backend.services import jira_task_import_service as import_service


def _jira_issue(
    key: str,
    *,
    summary: str = "Example summary",
    description: str = "Example description",
    status: str = "To Do",
    priority: str = "Medium",
    labels: list[str] | None = None,
    parent_key: str | None = None,
    links: list[dict[str, Any]] | None = None,
    assignee_account_id: str | None = None,
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "summary": summary,
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description}],
                }
            ],
        },
        "status": {"name": status},
        "priority": {"name": priority},
        "labels": labels or [],
        "issuelinks": links or [],
    }
    if parent_key:
        fields["parent"] = {"key": parent_key}
    if assignee_account_id:
        fields["assignee"] = {
            "accountId": assignee_account_id,
            "displayName": "Example Person",
        }
    return {"key": key, "fields": fields, "id": key.split("-")[-1]}


def test_import_jira_issues_dry_run_reports_mapping_and_drops(monkeypatch):
    monkeypatch.setattr(
        import_service,
        "find_task_by_external_reference",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        import_service,
        "create_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("dry_run should not write")),
    )

    issue = _jira_issue(
        "JVNAUTOSCI-2001",
        status="In Progress",
        priority="High",
        labels=["alpha", "beta"],
        assignee_account_id="jira-user-1",
    )
    report = import_service.import_jira_issues_to_tasks(
        issues=[issue],
        dry_run=True,
    )

    assert report["success"] is True
    assert report["dry_run"] is True
    assert report["summary"]["total_issues"] == 1
    assert report["summary"]["would_create"] == 1
    issue_row = report["issues"][0]
    assert issue_row["jira_issue_key"] == "JVNAUTOSCI-2001"
    assert issue_row["action"] == "would_create"
    assert "summary->title" in issue_row["mapped_fields"]
    assert "status" in issue_row["mapped_fields"]
    assert any(
        item.get("reason") == "jira_assignee_mapping_missing"
        for item in issue_row["dropped_fields"]
        if isinstance(item, dict)
    )


def test_import_jira_issues_idempotent_rerun_updates_existing(monkeypatch):
    state: dict[str, str] = {}
    create_calls = {"count": 0}
    update_calls = {"count": 0}

    def _fake_find(**kwargs):
        issue_key = kwargs.get("external_id")
        if isinstance(issue_key, str) and issue_key in state:
            return {"task_concept_id": state[issue_key]}
        return None

    def _fake_create_task(**_kwargs):
        create_calls["count"] += 1
        return {"task_concept_id": "#V#task_imported_1"}

    def _fake_upsert_external_ref(
        task_concept_id: str,
        *,
        external_id: str,
        **_kwargs,
    ):
        state[external_id] = task_concept_id
        return {"task_concept_id": task_concept_id}

    def _fake_update_task_fields(*_args, **_kwargs):
        update_calls["count"] += 1
        return {"task": {"task_concept_id": "#V#task_imported_1"}}

    monkeypatch.setattr(import_service, "find_task_by_external_reference", _fake_find)
    monkeypatch.setattr(import_service, "create_task", _fake_create_task)
    monkeypatch.setattr(
        import_service,
        "upsert_task_external_reference",
        _fake_upsert_external_ref,
    )
    monkeypatch.setattr(import_service, "update_task_fields", _fake_update_task_fields)
    monkeypatch.setattr(import_service, "set_task_parent", lambda *_a, **_k: {})
    monkeypatch.setattr(import_service, "set_task_epic", lambda *_a, **_k: {})
    monkeypatch.setattr(import_service, "link_tasks", lambda *_a, **_k: {})

    issue = _jira_issue("JVNAUTOSCI-2002", status="Done")
    first_report = import_service.import_jira_issues_to_tasks(
        issues=[issue],
        dry_run=False,
    )
    second_report = import_service.import_jira_issues_to_tasks(
        issues=[issue],
        dry_run=False,
    )

    assert first_report["issues"][0]["action"] == "created"
    assert second_report["issues"][0]["action"] == "updated"
    assert create_calls["count"] == 1
    assert update_calls["count"] >= 2


def test_import_jira_issues_maps_parent_and_links_when_targets_available(monkeypatch):
    created_ids = iter(["#V#task_parent", "#V#task_child"])
    mappings: dict[str, str] = {}
    parent_calls: list[tuple[str, str]] = []
    link_calls: list[tuple[str, str, str]] = []

    def _fake_find(**kwargs):
        issue_key = kwargs.get("external_id")
        if isinstance(issue_key, str) and issue_key in mappings:
            return {"task_concept_id": mappings[issue_key]}
        return None

    def _fake_create_task(**_kwargs):
        return {"task_concept_id": next(created_ids)}

    def _fake_upsert_external_ref(
        task_concept_id: str,
        *,
        external_id: str,
        **_kwargs,
    ):
        mappings[external_id] = task_concept_id
        return {"task_concept_id": task_concept_id}

    def _fake_set_parent(source_id: str, target_id: str, **_kwargs):
        parent_calls.append((source_id, target_id))
        return {}

    def _fake_link(source_id: str, target_id: str, *, link_type: str, **_kwargs):
        link_calls.append((source_id, target_id, link_type))
        return {}

    monkeypatch.setattr(import_service, "find_task_by_external_reference", _fake_find)
    monkeypatch.setattr(import_service, "create_task", _fake_create_task)
    monkeypatch.setattr(
        import_service,
        "upsert_task_external_reference",
        _fake_upsert_external_ref,
    )
    monkeypatch.setattr(
        import_service,
        "update_task_fields",
        lambda *_args, **_kwargs: {"task": {}},
    )
    monkeypatch.setattr(import_service, "set_task_parent", _fake_set_parent)
    monkeypatch.setattr(import_service, "set_task_epic", lambda *_a, **_k: {})
    monkeypatch.setattr(import_service, "link_tasks", _fake_link)

    parent_issue = _jira_issue("JVNAUTOSCI-2100")
    child_issue = _jira_issue(
        "JVNAUTOSCI-2101",
        parent_key="JVNAUTOSCI-2100",
        links=[
            {
                "type": {"outward": "relates to"},
                "outwardIssue": {"key": "JVNAUTOSCI-2100"},
            }
        ],
    )

    report = import_service.import_jira_issues_to_tasks(
        issues=[parent_issue, child_issue],
        dry_run=False,
    )

    assert report["success"] is True
    assert ("#V#task_child", "#V#task_parent") in parent_calls
    assert ("#V#task_child", "#V#task_parent", "relates_to") in link_calls
