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
    creator_account_id: str | None = None,
    reporter_account_id: str | None = None,
    watcher_account_ids: list[str] | None = None,
    project_key: str = "JVNAUTOSCI",
    project_name: str = "JVNAUTOSCI Project",
    extra_fields: dict[str, Any] | None = None,
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
        "project": {"key": project_key, "name": project_name},
    }
    if isinstance(extra_fields, dict):
        fields.update(extra_fields)
    if parent_key:
        fields["parent"] = {"key": parent_key}
    if assignee_account_id:
        fields["assignee"] = {
            "accountId": assignee_account_id,
            "displayName": "Example Person",
        }
    if creator_account_id:
        fields["creator"] = {
            "accountId": creator_account_id,
            "displayName": "Creator Person",
        }
    if reporter_account_id:
        fields["reporter"] = {
            "accountId": reporter_account_id,
            "displayName": "Reporter Person",
        }

    issue: Dict[str, Any] = {"key": key, "fields": fields, "id": key.split("-")[-1]}
    if watcher_account_ids:
        issue["watchers"] = {
            "watchCount": len(watcher_account_ids),
            "watchers": [
                {
                    "accountId": watcher_account_id,
                    "displayName": f"Watcher {idx + 1}",
                }
                for idx, watcher_account_id in enumerate(watcher_account_ids)
            ],
        }
    return issue


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


def test_import_jira_issues_reports_project_level_parity_findings(monkeypatch):
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
        "JVNAUTOSCI-2300",
        status="Custom Workflow State",
        extra_fields={
            "components": [{"name": "Workflow Engine"}],
            "fixVersions": [{"name": "R1"}],
            "customfield_10020": [{"name": "Sprint 6"}],
            "customfield_10019": "0|i000ab:",
        },
    )
    report = import_service.import_jira_issues_to_tasks(
        issues=[issue],
        dry_run=True,
    )

    project_parity = report.get("project_parity") or {}
    assert project_parity.get("summary", {}).get("projects_scanned") == 1
    project_rows = project_parity.get("projects")
    assert isinstance(project_rows, list)
    assert len(project_rows) == 1
    row = project_rows[0]
    assert row.get("project_key") == "JVNAUTOSCI"
    assert row.get("issue_count") == 1
    dropped_fields = row.get("dropped_fields")
    assert isinstance(dropped_fields, list)
    dropped_field_names = {
        item.get("field")
        for item in dropped_fields
        if isinstance(item, dict)
    }
    assert "status_mapping" in dropped_field_names
    assert "components" not in dropped_field_names
    assert "fixVersions" not in dropped_field_names
    assert "sprint" not in dropped_field_names
    assert "rank" not in dropped_field_names
    assert "components" in row.get("mapped_fields", [])
    assert "fixVersions" in row.get("mapped_fields", [])
    assert "sprint" in row.get("mapped_fields", [])
    assert "rank" in row.get("mapped_fields", [])


def test_import_jira_issues_pilot_validation_recommends_go_with_conditions(monkeypatch):
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
        "JVNAUTOSCI-2301",
        status="To Do",
        extra_fields={
            "components": [{"name": "Workflow Engine"}],
            "customfield_10019": "0|i000ac:",
        },
    )
    report = import_service.import_jira_issues_to_tasks(
        issues=[issue],
        dry_run=True,
    )

    pilot_validation = report.get("pilot_validation") or {}
    assert pilot_validation.get("recommendation") == "go_with_conditions"
    summary = pilot_validation.get("summary") or {}
    assert summary.get("must_fix_gap_count") == 0
    assert summary.get("acceptable_defer_gap_count", 0) >= 1


def test_import_jira_issues_preserves_participant_concepts_from_account_map(monkeypatch):
    created_calls: list[dict[str, Any]] = []
    update_calls: list[dict[str, Any]] = []
    external_refs: list[dict[str, Any]] = []

    monkeypatch.setattr(
        import_service,
        "find_task_by_external_reference",
        lambda **_kwargs: None,
    )

    def _fake_create_task(**kwargs):
        created_calls.append(kwargs)
        return {"task_concept_id": "#V#task_imported_participants"}

    def _fake_update_task_fields(task_id: str, *, fields: dict[str, Any], **_kwargs):
        update_calls.append({"task_id": task_id, "fields": fields})
        return {"task": {"task_concept_id": task_id}}

    def _fake_upsert_task_external_reference(
        task_concept_id: str,
        *,
        reference_payload: dict[str, Any],
        **_kwargs,
    ):
        external_refs.append(
            {"task_concept_id": task_concept_id, "reference_payload": reference_payload}
        )
        return {"task_concept_id": task_concept_id}

    monkeypatch.setattr(import_service, "create_task", _fake_create_task)
    monkeypatch.setattr(import_service, "update_task_fields", _fake_update_task_fields)
    monkeypatch.setattr(
        import_service,
        "upsert_task_external_reference",
        _fake_upsert_task_external_reference,
    )
    monkeypatch.setattr(import_service, "set_task_parent", lambda *_a, **_k: {})
    monkeypatch.setattr(import_service, "set_task_epic", lambda *_a, **_k: {})
    monkeypatch.setattr(import_service, "link_tasks", lambda *_a, **_k: {})

    issue = _jira_issue(
        "JVNAUTOSCI-2601",
        assignee_account_id="jira-assignee-1",
        creator_account_id="jira-creator-1",
        reporter_account_id="jira-reporter-1",
        watcher_account_ids=["jira-watcher-1", "jira-watcher-2"],
    )
    report = import_service.import_jira_issues_to_tasks(
        issues=[issue],
        dry_run=False,
        auto_resolve_participants=False,
        create_missing_participant_concepts=False,
        jira_account_id_to_concept_id={
            "jira-assignee-1": "#V#person_assignee",
            "jira-creator-1": "#V#person_creator",
            "jira-reporter-1": "#V#person_reporter",
            "jira-watcher-1": "#V#person_watcher_1",
            "jira-watcher-2": "#V#person_watcher_2",
        },
    )

    assert report.get("success") is True
    assert created_calls
    assert created_calls[0].get("created_by_concept_id") == "#V#person_creator"
    assert created_calls[0].get("assignee_concept_id") == "#V#person_assignee"

    assert update_calls
    updated_fields = update_calls[0]["fields"]
    assert updated_fields.get("assignee_concept_id") == "#V#person_assignee"
    assert updated_fields.get("created_by_concept_id") == "#V#person_creator"
    assert updated_fields.get("reporter_concept_id") == "#V#person_reporter"
    assert updated_fields.get("watcher_concept_ids") == [
        "#V#person_watcher_1",
        "#V#person_watcher_2",
    ]

    issue_row = report["issues"][0]
    assert "assignee" in issue_row["mapped_fields"]
    assert "creator" in issue_row["mapped_fields"]
    assert "reporter" in issue_row["mapped_fields"]
    assert "watchers" in issue_row["mapped_fields"]
    assert issue_row.get("participant_mappings", {}).get("creator", {}).get("concept_id") == (
        "#V#person_creator"
    )

    assert external_refs
    participants_payload = (
        external_refs[0]["reference_payload"].get("participants")
        if isinstance(external_refs[0]["reference_payload"], dict)
        else {}
    )
    assert isinstance(participants_payload, dict)
    assert participants_payload.get("assignee", {}).get("concept_id") == "#V#person_assignee"
    assert participants_payload.get("creator", {}).get("concept_id") == "#V#person_creator"
    assert participants_payload.get("reporter", {}).get("concept_id") == "#V#person_reporter"
    watcher_payload = participants_payload.get("watchers")
    assert isinstance(watcher_payload, list)
    assert len(watcher_payload) == 2


def test_list_imported_jira_issue_keys_scans_repository_with_org_and_legacy_scope(monkeypatch):
    raw_keys = [
        "JVNAUTOSCI-301",
        "JVNAUTOSCI-302",
        "jvnautosci-301",
        "  JVNAUTOSCI-303  ",
        None,
    ]
    captured_queries: list[dict[str, Any]] = []

    def _fake_distinct(
        field: str,
        query: dict[str, Any] | None = None,
    ):
        assert field == "metadata.external_references.jira.external_id"
        captured_queries.append(query or {})
        return list(raw_keys)

    monkeypatch.setattr(import_service.ConceptsRepository, "distinct", _fake_distinct)

    keys = import_service.list_imported_jira_issue_keys(
        organisation_concept_id="#V#sail_org",
        limit=10,
    )

    assert keys == ["JVNAUTOSCI-301", "JVNAUTOSCI-302", "JVNAUTOSCI-303"]
    assert captured_queries
    assert "$or" in captured_queries[0]
    assert {
        "metadata.organisation_concept_id": "#V#sail_org"
    } in captured_queries[0]["$or"]
    assert {"metadata.organisation_concept_id": None} in captured_queries[0]["$or"]
