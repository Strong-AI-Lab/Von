"""Gateway-level tests for Jira-style task MCP parity operations.

These tests exercise handlers through InternalMCPGateway.invoke() and verify
that both success and error payloads remain compatible with declared output
schemas.
"""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.schemas import validate_payload
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _assert_schema_conformance(gateway: InternalMCPGateway, method: str, payload: dict) -> None:
    definition = gateway.get_method_definition(method)
    assert definition is not None
    assert definition.output_schema is not None
    ok, errors = validate_payload(definition.output_schema, payload)
    assert ok, f"{method} output schema mismatch: {errors}"


def _patch_task_import_write_path(monkeypatch):
    external_mapping: dict[str, str] = {}
    created_counter = {"count": 0}

    def _fake_find(**kwargs):
        issue_key = kwargs.get("external_id")
        if isinstance(issue_key, str):
            task_id = external_mapping.get(issue_key)
            if isinstance(task_id, str) and task_id:
                return {"task_concept_id": task_id}
        return None

    def _fake_create_task(**_kwargs):
        created_counter["count"] += 1
        return {"task_concept_id": f"#V#task_imported_{created_counter['count']}"}

    def _fake_upsert_external_reference(
        task_concept_id: str,
        *,
        external_id: str,
        **_kwargs,
    ):
        external_mapping[external_id] = task_concept_id
        return {"task_concept_id": task_concept_id}

    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.find_task_by_external_reference",
        _fake_find,
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.create_task",
        _fake_create_task,
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.update_task_fields",
        lambda *_args, **_kwargs: {"task": {}},
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.upsert_task_external_reference",
        _fake_upsert_external_reference,
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.set_task_parent",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.set_task_epic",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.link_tasks",
        lambda *_args, **_kwargs: {},
    )
    return external_mapping


def test_task_parity_methods_registered_in_catalogue():
    methods = set(build_default_catalogue().list_methods())
    expected = {
        "task_search",
        "task_import_jira_issues",
        "task_update_fields",
        "task_get_transitions",
        "task_transition",
        "task_unassign",
        "task_set_parent",
        "task_create_subtask",
        "task_link",
        "task_unlink",
        "task_add_comment",
        "task_list_comments",
        "task_add_attachment",
        "task_list_attachments",
        "task_add_worklog",
        "task_list_worklog",
        "task_get_history",
        "task_bulk_update",
    }
    missing = sorted(expected - methods)
    assert not missing, f"Missing expected task parity methods: {missing}"


def test_task_import_jira_issues_gateway_dry_run_and_schema(monkeypatch):
    gateway = _build_gateway()

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Imported issue",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "labels": ["migration"],
                    "issuelinks": [],
                },
            }

        async def search(
            self,
            *,
            jql: str,  # noqa: ARG002
            max_results=None,  # noqa: ARG002
            start_at=None,  # noqa: ARG002
            next_page_token=None,  # noqa: ARG002
            fields=None,  # noqa: ARG002
        ):
            return {"issues": [{"key": "JVNAUTOSCI-3001"}]}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    payload = gateway.invoke(
        "task_import_jira_issues",
        {"issue_keys": ["JVNAUTOSCI-3001"], "dry_run": True},
    ).payload
    assert payload.get("success") is True
    assert payload.get("dry_run") is True
    assert payload.get("summary", {}).get("total_issues") == 1
    _assert_schema_conformance(gateway, "task_import_jira_issues", payload)


def test_task_import_jira_issues_gateway_accepts_issue_objects_from_discovery(monkeypatch):
    gateway = _build_gateway()
    fetched_issue_keys: list[str] = []

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            fetched_issue_keys.append(issue_key)
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Imported issue",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "project": {"key": "JVNAUTOSCI", "name": "JVNAUTOSCI Project"},
                    "issuelinks": [],
                },
            }

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    payload = gateway.invoke(
        "task_import_jira_issues",
        {"issue_keys": [{"key": "JVNAUTOSCI-3002"}], "dry_run": True},
    ).payload
    assert payload.get("success") is True
    assert payload.get("summary", {}).get("total_issues") == 1
    assert fetched_issue_keys == ["JVNAUTOSCI-3002"]
    _assert_schema_conformance(gateway, "task_import_jira_issues", payload)


def test_task_import_jira_issues_gateway_reuses_seeded_issue_documents(monkeypatch):
    gateway = _build_gateway()
    fetched_issue_keys: list[str] = []

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            fetched_issue_keys.append(issue_key)
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Fetched issue",
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "project": {"key": "JVNAUTOSCI", "name": "JVNAUTOSCI Project"},
                    "issuelinks": [],
                },
            }

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    payload = gateway.invoke(
        "task_import_jira_issues",
        {
            "issue_keys": [
                {
                    "key": "JVNAUTOSCI-3004",
                    "fields": {
                        "summary": "Seeded issue",
                        "description": {
                            "type": "doc",
                            "version": 1,
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Desc"}],
                                }
                            ],
                        },
                        "status": {"name": "To Do"},
                        "priority": {"name": "Medium"},
                        "labels": [],
                        "project": {"key": "JVNAUTOSCI", "name": "JVNAUTOSCI Project"},
                        "duedate": None,
                        "startdate": None,
                        "assignee": None,
                        "creator": None,
                        "reporter": None,
                        "parent": None,
                        "issuelinks": [],
                        "components": [],
                        "fixVersions": [],
                        "customfield_10020": None,
                        "customfield_10019": None,
                        "customfield_10027": None,
                        "customfield_10014": None,
                        "customfield_10008": None,
                        "issuetype": {"name": "Task"},
                        "created": "2026-04-01T00:00:00.000+0000",
                        "updated": "2026-04-01T00:00:00.000+0000",
                        "comment": {"comments": []},
                        "attachment": [],
                        "worklog": {"worklogs": []},
                    },
                    "changelog": {"histories": []},
                }
            ],
            "dry_run": True,
        },
    ).payload
    assert payload.get("success") is True
    assert payload.get("summary", {}).get("total_issues") == 1
    assert fetched_issue_keys == []
    _assert_schema_conformance(gateway, "task_import_jira_issues", payload)


def test_task_import_jira_issues_gateway_hydrates_seeded_docs_for_activity_import(
    monkeypatch,
):
    gateway = _build_gateway()
    captured_import_kwargs: dict[str, object] = {}

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None, expand=None):  # noqa: ARG002
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Hydrated issue",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "project": {"key": "JVNAUTOSCI", "name": "JVNAUTOSCI Project"},
                    "comment": {
                        "comments": [
                            {
                                "id": "10001",
                                "body": {
                                    "type": "doc",
                                    "version": 1,
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "Comment"}],
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                    "attachment": [
                        {
                            "id": "20001",
                            "filename": "spec.pdf",
                            "size": 3,
                            "mimeType": "application/pdf",
                        }
                    ],
                    "worklog": {"worklogs": []},
                    "issuelinks": [],
                },
                "changelog": {"histories": []},
            }

        async def get_watchers(self, *, issue_key: str):  # noqa: ARG002
            return {"watchCount": 0, "watchers": []}

        async def get_attachment_content(self, *, attachment_id: str, max_size_bytes=None):  # noqa: ARG002
            return {
                "success": True,
                "attachment_id": attachment_id,
                "content_base64": "cGRm",
                "content_type": "application/pdf",
                "size_bytes": 3,
            }

        async def get_myself(self):
            return {"accountId": "jira-current-user"}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    def _fake_import_jira_issues_to_tasks(**kwargs):
        captured_import_kwargs.update(kwargs)
        issues = kwargs.get("issues")
        issue_count = len(issues) if isinstance(issues, list) else 0
        return {
            "success": True,
            "dry_run": bool(kwargs.get("dry_run")),
            "summary": {"total_issues": issue_count},
            "issues": [
                {
                    "jira_issue_key": "JVNAUTOSCI-3005",
                    "action": "created",
                    "mapped_fields": ["summary->title"],
                    "dropped_fields": [],
                    "relation_results": [],
                }
            ],
        }

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.import_jira_issues_to_tasks",
        _fake_import_jira_issues_to_tasks,
    )

    payload = gateway.invoke(
        "task_import_jira_issues",
        {
            "issue_keys": [{"key": "JVNAUTOSCI-3005", "fields": {"summary": "Seeded issue"}}],
            "dry_run": False,
            "sync_source_labels": False,
            "namespace": "#V#current_user",
        },
    ).payload

    assert payload.get("success") is True
    imported_issues = captured_import_kwargs.get("issues")
    assert isinstance(imported_issues, list)
    attachment_rows = imported_issues[0].get("fields", {}).get("attachment")
    assert isinstance(attachment_rows, list)
    assert attachment_rows[0].get("content_base64") == "cGRm"
    assert imported_issues[0].get("changelog") == {"histories": []}
    _assert_schema_conformance(gateway, "task_import_jira_issues", payload)


def test_task_import_jira_issues_gateway_syncs_migrated_label_on_write(monkeypatch):
    gateway = _build_gateway()
    _patch_task_import_write_path(monkeypatch)

    jira_labels = {"JVNAUTOSCI-3003": ["jira-migration"]}
    update_calls: list[dict[str, object]] = []

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Imported issue",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "labels": list(jira_labels.get(issue_key, [])),
                    "project": {"key": "JVNAUTOSCI", "name": "JVNAUTOSCI Project"},
                    "issuelinks": [],
                },
            }

        async def update_issue(self, *, issue_key: str, payload):
            update_calls.append({"issue_key": issue_key, "payload": payload})
            labels = (
                payload.get("fields", {}).get("labels")
                if isinstance(payload, dict)
                else None
            )
            if isinstance(labels, list):
                jira_labels[issue_key] = [str(label) for label in labels]
            return {"success": True}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    payload = gateway.invoke(
        "task_import_jira_issues",
        {"issue_keys": ["JVNAUTOSCI-3003"], "dry_run": False},
    ).payload
    assert payload.get("success") is True
    sync_report = payload.get("source_label_sync") or {}
    assert sync_report.get("eligible_issue_count") == 1
    assert sync_report.get("updated_count") == 1
    assert sync_report.get("already_present_count") == 0
    assert jira_labels["JVNAUTOSCI-3003"] == ["jira-migration", "migrated"]
    assert len(update_calls) == 1
    _assert_schema_conformance(gateway, "task_import_jira_issues", payload)


def test_task_import_jira_issues_gateway_migrated_label_sync_is_idempotent(monkeypatch):
    gateway = _build_gateway()
    _patch_task_import_write_path(monkeypatch)

    jira_labels = {"JVNAUTOSCI-3004": ["jira-migration"]}
    update_calls: list[dict[str, object]] = []

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Imported issue",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "In Progress"},
                    "priority": {"name": "Medium"},
                    "labels": list(jira_labels.get(issue_key, [])),
                    "project": {"key": "JVNAUTOSCI", "name": "JVNAUTOSCI Project"},
                    "issuelinks": [],
                },
            }

        async def update_issue(self, *, issue_key: str, payload):
            update_calls.append({"issue_key": issue_key, "payload": payload})
            labels = (
                payload.get("fields", {}).get("labels")
                if isinstance(payload, dict)
                else None
            )
            if isinstance(labels, list):
                jira_labels[issue_key] = [str(label) for label in labels]
            return {"success": True}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    first_payload = gateway.invoke(
        "task_import_jira_issues",
        {"issue_keys": ["JVNAUTOSCI-3004"], "dry_run": False},
    ).payload
    second_payload = gateway.invoke(
        "task_import_jira_issues",
        {"issue_keys": ["JVNAUTOSCI-3004"], "dry_run": False},
    ).payload

    first_sync = first_payload.get("source_label_sync") or {}
    second_sync = second_payload.get("source_label_sync") or {}
    assert first_sync.get("updated_count") == 1
    assert second_sync.get("updated_count") == 0
    assert second_sync.get("already_present_count") == 1
    assert len(update_calls) == 1
    _assert_schema_conformance(gateway, "task_import_jira_issues", second_payload)


def test_task_import_jira_issues_gateway_backfill_and_namespace_identity_mapping(
    monkeypatch,
):
    gateway = _build_gateway()
    captured_import_kwargs: dict[str, object] = {}

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Imported issue",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "labels": [],
                    "issuelinks": [],
                },
            }

        async def get_myself(self):
            return {"accountId": "jira-current-user"}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    def _fake_import_jira_issues_to_tasks(**kwargs):
        captured_import_kwargs.update(kwargs)
        issues = kwargs.get("issues")
        issue_count = len(issues) if isinstance(issues, list) else 0
        return {
            "success": True,
            "dry_run": bool(kwargs.get("dry_run")),
            "summary": {"total_issues": issue_count},
            "issues": [
                {
                    "jira_issue_key": "JVNAUTOSCI-3999",
                    "action": "would_update",
                    "mapped_fields": ["summary->title", "status", "priority"],
                    "dropped_fields": [],
                    "relation_results": [],
                }
            ],
        }

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.list_imported_jira_issue_keys",
        lambda **_kwargs: ["JVNAUTOSCI-3999"],
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.import_jira_issues_to_tasks",
        _fake_import_jira_issues_to_tasks,
    )

    payload = gateway.invoke(
        "task_import_jira_issues",
        {
            "backfill_existing_imports": True,
            "dry_run": True,
            "namespace": "#V#current_user",
        },
    ).payload

    assert payload.get("success") is True
    assert payload.get("fetch", {}).get("backfill_discovered_issue_count") == 1
    assert payload.get("summary", {}).get("total_issues") == 1
    imported_issues = captured_import_kwargs.get("issues")
    assert isinstance(imported_issues, list)
    assert imported_issues and imported_issues[0].get("key") == "JVNAUTOSCI-3999"
    participant_map = captured_import_kwargs.get("jira_account_id_to_concept_id")
    assert isinstance(participant_map, dict)
    assert participant_map.get("jira-current-user") == "#V#current_user"
    _assert_schema_conformance(gateway, "task_import_jira_issues", payload)


def test_task_import_jira_issues_gateway_resolves_org_scope_for_backfill_and_import(
    monkeypatch,
):
    gateway = _build_gateway()
    captured_import_kwargs: dict[str, object] = {}
    captured_backfill_kwargs: dict[str, object] = {}

    class _FakeProxy:
        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            return {
                "key": issue_key,
                "fields": {
                    "summary": "Imported issue",
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "Desc"}],
                            }
                        ],
                    },
                    "status": {"name": "To Do"},
                    "priority": {"name": "Medium"},
                    "labels": [],
                    "issuelinks": [],
                },
            }

        async def get_myself(self):
            return {"accountId": "jira-current-user"}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    def _fake_list_imported_jira_issue_keys(**kwargs):
        captured_backfill_kwargs.update(kwargs)
        return ["JVNAUTOSCI-4111"]

    def _fake_import_jira_issues_to_tasks(**kwargs):
        captured_import_kwargs.update(kwargs)
        issues = kwargs.get("issues")
        issue_count = len(issues) if isinstance(issues, list) else 0
        return {
            "success": True,
            "dry_run": bool(kwargs.get("dry_run")),
            "summary": {"total_issues": issue_count},
            "issues": [
                {
                    "jira_issue_key": "JVNAUTOSCI-4111",
                    "action": "would_update",
                    "mapped_fields": ["summary->title", "status", "priority"],
                    "dropped_fields": [],
                    "relation_results": [],
                }
            ],
        }

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.list_imported_jira_issue_keys",
        _fake_list_imported_jira_issue_keys,
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.import_jira_issues_to_tasks",
        _fake_import_jira_issues_to_tasks,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.resolve_event_actor_context",
        lambda **_kwargs: ("#V#current_user", "#V#sail_org"),
    )

    payload = gateway.invoke(
        "task_import_jira_issues",
        {
            "backfill_existing_imports": True,
            "dry_run": True,
            "namespace": "#V#current_user@sail_org",
        },
    ).payload

    assert payload.get("success") is True
    assert captured_backfill_kwargs.get("organisation_concept_id") == "#V#sail_org"
    assert captured_import_kwargs.get("organisation_concept_id") == "#V#sail_org"
    assert captured_import_kwargs.get("actor_concept_id") == "#V#current_user"
    participant_map = captured_import_kwargs.get("jira_account_id_to_concept_id")
    assert isinstance(participant_map, dict)
    assert participant_map.get("jira-current-user") == "#V#current_user"
    _assert_schema_conformance(gateway, "task_import_jira_issues", payload)


def test_task_transition_gateway_success_and_error_schema(monkeypatch):
    gateway = _build_gateway()

    def _fake_transition_task(
        task_concept_id: str,
        *,
        transition_id=None,
        to_status=None,
        actor_concept_id=None,
    ):
        return {
            "task_concept_id": task_concept_id,
            "from_status": "pending",
            "to_status": to_status or "in_progress",
            "transition": {
                "transition_id": transition_id or "start_progress",
                "to_status": to_status or "in_progress",
            },
            "task": {"task_concept_id": task_concept_id, "status": to_status or "in_progress"},
            "actor": actor_concept_id,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.transition_task",
        _fake_transition_task,
    )

    success_payload = gateway.invoke(
        "task_transition",
        {"task_concept_id": "#V#task_1", "to_status": "in_progress"},
    ).payload
    assert success_payload.get("success") is True
    assert success_payload.get("task", {}).get("status") == "in_progress"
    _assert_schema_conformance(gateway, "task_transition", success_payload)

    error_payload = gateway.invoke("task_transition", {"task_concept_id": "#V#task_1"}).payload
    assert error_payload.get("success") is False
    assert error_payload.get("error_code") == "MISSING_PARAM"
    _assert_schema_conformance(gateway, "task_transition", error_payload)


def test_task_link_gateway_success_and_error_schema(monkeypatch):
    gateway = _build_gateway()
    from src.backend.services.task_management_service import InvalidTaskDataError

    def _fake_link_tasks(
        source_task_concept_id: str,
        target_task_concept_id: str,
        *,
        link_type: str,
        actor_concept_id=None,
    ):
        if link_type == "unsupported":
            raise InvalidTaskDataError("Unsupported link_type")
        return {
            "source_task_concept_id": source_task_concept_id,
            "target_task_concept_id": target_task_concept_id,
            "link_type": link_type,
            "linked": True,
            "actor": actor_concept_id,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.link_tasks",
        _fake_link_tasks,
    )

    success_payload = gateway.invoke(
        "task_link",
        {
            "source_task_concept_id": "#V#task_1",
            "target_task_concept_id": "#V#task_2",
            "link_type": "depends_on",
        },
    ).payload
    assert success_payload.get("success") is True
    assert success_payload.get("linked") is True
    _assert_schema_conformance(gateway, "task_link", success_payload)

    error_payload = gateway.invoke(
        "task_link",
        {
            "source_task_concept_id": "#V#task_1",
            "target_task_concept_id": "#V#task_2",
            "link_type": "unsupported",
        },
    ).payload
    assert error_payload.get("success") is False
    assert error_payload.get("error_code") == "INVALID_DATA"
    _assert_schema_conformance(gateway, "task_link", error_payload)


def test_task_comment_gateway_success_and_error_schema(monkeypatch):
    gateway = _build_gateway()
    from src.backend.security.access_control import override_current_actor

    def _fake_add_task_comment(
        task_concept_id: str,
        *,
        body: str,
        author_concept_id=None,
        source=None,
    ):
        return {
            "comment_id": "comment_abc123",
            "body": body,
            "author_concept_id": author_concept_id,
            "task_concept_id": task_concept_id,
            "source": source,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.add_task_comment",
        _fake_add_task_comment,
    )
    monkeypatch.setattr(
        "src.backend.services.task_management_service.get_task",
        lambda task_concept_id: {
            "task_concept_id": task_concept_id,
            "assignee_concept_id": "#V#user_alice",
            "created_by_concept_id": "#V#user_alice",
            "organisation_concept_id": "#V#org_test",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.task_management_service.find_task_comment_by_effect_fingerprint",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.task_management_service.get_task_comment",
        lambda _task_concept_id, _comment_id: {
            "comment_id": "comment_abc123",
            "body": "Looks good",
            "author_concept_id": "#V#user_alice",
        },
    )

    actor_payload = {
        "acting_user_concept_id": "#V#user_alice",
        "organisation_concept_id": "#V#org_test",
        "request_id": "turn-1",
        "namespace": "#V#user_alice@org_test",
    }
    with override_current_actor("#V#user_alice", "#V#org_test"):
        success_payload = gateway.invoke(
            "task_add_comment",
            {
                "task_concept_id": "#V#task_1",
                "body": "Looks good",
                **actor_payload,
            },
        ).payload
    assert success_payload.get("success") is True
    assert success_payload.get("comment", {}).get("comment_id") == "comment_abc123"
    _assert_schema_conformance(gateway, "task_add_comment", success_payload)

    with override_current_actor("#V#user_alice", "#V#org_test"):
        error_payload = gateway.invoke(
            "task_add_comment",
            {"task_concept_id": "#V#task_1", "body": "", **actor_payload},
        ).payload
    assert error_payload.get("success") is False
    assert error_payload.get("error_code") == "MISSING_PARAM"
    _assert_schema_conformance(gateway, "task_add_comment", error_payload)


def test_task_attachment_gateway_success_and_error_schema(monkeypatch):
    gateway = _build_gateway()

    def _fake_add_task_attachment(
        task_concept_id: str,
        *,
        filename: str,
        uri: str,
        media_type=None,
        size_bytes=None,
        added_by_concept_id=None,
        note=None,
    ):
        return {
            "attachment_id": "attachment_abc123",
            "filename": filename,
            "uri": uri,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "added_by_concept_id": added_by_concept_id,
            "note": note,
            "task_concept_id": task_concept_id,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.add_task_attachment",
        _fake_add_task_attachment,
    )

    success_payload = gateway.invoke(
        "task_add_attachment",
        {
            "task_concept_id": "#V#task_1",
            "filename": "spec.pdf",
            "uri": "blob://spec.pdf",
        },
    ).payload
    assert success_payload.get("success") is True
    assert success_payload.get("attachment", {}).get("attachment_id") == "attachment_abc123"
    _assert_schema_conformance(gateway, "task_add_attachment", success_payload)

    error_payload = gateway.invoke(
        "task_add_attachment",
        {"task_concept_id": "#V#task_1", "filename": "spec.pdf"},
    ).payload
    assert error_payload.get("success") is False
    assert error_payload.get("error_code") == "MISSING_PARAM"
    _assert_schema_conformance(gateway, "task_add_attachment", error_payload)


def test_task_create_gateway_supports_start_date_and_epic(monkeypatch):
    gateway = _build_gateway()
    from src.backend.security.access_control import override_current_actor

    def _fake_create_task(
        title: str,
        description: str,
        *,
        assignee_concept_id=None,
        originating_session_id=None,
        created_by_concept_id=None,
        start_date=None,
        due_date=None,
        epic_task_concept_id=None,
        components=None,
        fix_versions=None,
        sprint_values=None,
        backlog_rank=None,
        priority="medium",
        organisation_concept_id=None,
        task_type_ids=None,
        task_source_id=None,
        report_to_concept_id=None,
        task_role=None,
        next_checkpoint=None,
        progress_signal=None,
        evidence=None,
        notes=None,
        reference_code=None,
        agent_creation_fingerprint=None,
        agent_creation_request_id=None,
    ):
        return {
            "task_concept_id": "#V#task_123",
            "title": title,
            "description": description,
            "status": "pending",
            "priority": priority,
            "assignee_concept_id": assignee_concept_id,
            "created_by_concept_id": created_by_concept_id,
            "originating_conversation_id": originating_session_id,
            "start_date": start_date.isoformat() if start_date is not None else None,
            "due_date": due_date.isoformat() if due_date is not None else None,
            "epic_task_concept_id": epic_task_concept_id,
            "components": components,
            "fix_versions": fix_versions,
            "sprint_values": sprint_values,
            "backlog_rank": backlog_rank,
            "organisation_concept_id": organisation_concept_id,
            "task_type_ids": task_type_ids,
            "task_source_id": task_source_id,
            "report_to_concept_id": report_to_concept_id,
            "task_role": task_role,
            "next_checkpoint": next_checkpoint,
            "progress_signal": progress_signal,
            "evidence": evidence,
            "notes": notes,
            "reference_code": reference_code,
            "agent_creation_fingerprint": agent_creation_fingerprint,
            "agent_creation_request_id": agent_creation_request_id,
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.create_task",
        _fake_create_task,
    )
    monkeypatch.setattr(
        "src.backend.services.task_management_service.get_task",
        lambda task_concept_id: {
            "task_concept_id": task_concept_id,
            "title": "Task with dates",
            "description": "Details",
            "status": "pending",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.task_management_service.find_task_by_agent_creation_fingerprint",
        lambda **_kwargs: None,
    )

    with override_current_actor("#V#user_alice", "#V#org_test"):
        payload = gateway.invoke(
            "task_create",
            {
                "title": "Task with dates",
                "description": "Details",
                "start_date": "2026-03-01T10:00:00Z",
                "due_date": "2026-03-05T10:00:00Z",
                "epic_task_concept_id": "#V#task_epic_1",
                "task_type_ids": ["#V#delegated_task_specification"],
                "task_source_id": "#V#jira_imported_task_source",
                "report_to_concept_id": "#V#user_manager",
                "task_role": "Communicator",
                "next_checkpoint": "Tomorrow morning",
                "progress_signal": "Confirmed by chat",
                "evidence": "Printed document",
                "notes": "Needs a coloured copy",
                "reference_code": "TASK-001",
                "assignee_id": "#V#user_alice",
                "created_by_concept_id": "#V#user_alice",
                "acting_user_concept_id": "#V#user_alice",
                "organisation_concept_id": "#V#org_test",
                "request_id": "turn-1",
                "namespace": "#V#user_alice@org_test",
            },
        ).payload
    assert payload.get("success") is True
    assert payload.get("start_date") == "2026-03-01T10:00:00+00:00"
    assert payload.get("due_date") == "2026-03-05T10:00:00+00:00"
    assert payload.get("epic_task_concept_id") == "#V#task_epic_1"
    assert payload.get("task_type_ids") == ["#V#delegated_task_specification"]
    assert payload.get("task_source_id") == "#V#jira_imported_task_source"
    assert payload.get("report_to_concept_id") == "#V#user_manager"
    assert payload.get("reference_code") == "TASK-001"
    _assert_schema_conformance(gateway, "task_create", payload)


def test_task_search_gateway_supports_start_and_epic_filters(monkeypatch):
    gateway = _build_gateway()
    definition = gateway.get_method_definition("task_search")
    assert definition is not None
    assert definition.input_schema is not None
    assert "start_from" in definition.input_schema.optional
    assert "start_to" in definition.input_schema.optional
    assert "epic_task_concept_id" in definition.input_schema.optional
    assert "has_epic" in definition.input_schema.optional
    assert "components" in definition.input_schema.optional
    assert "fix_versions" in definition.input_schema.optional
    assert "sprint_values" in definition.input_schema.optional
    assert "backlog_rank" in definition.input_schema.optional
    assert "has_backlog_rank" in definition.input_schema.optional
    assert "task_type_ids" in definition.input_schema.optional
    assert "task_source_id" in definition.input_schema.optional
    assert "task_source_ids" in definition.input_schema.optional
    assert "created_by_concept_id" in definition.input_schema.optional
    assert "report_to_concept_id" in definition.input_schema.optional

    captured: dict = {}

    def _fake_search_tasks(**kwargs):
        captured.update(kwargs)
        return {
            "tasks": [],
            "total": 0,
            "count": 0,
            "offset": kwargs.get("offset", 0),
            "limit": kwargs.get("limit", 50),
        }

    monkeypatch.setattr(
        "src.backend.services.task_management_service.search_tasks",
        _fake_search_tasks,
    )

    payload = gateway.invoke(
        "task_search",
        {
            "start_from": "2026-03-01T00:00:00Z",
            "start_to": "2026-03-15T00:00:00Z",
            "epic_task_concept_id": "#V#task_epic_1",
            "has_epic": True,
            "components": ["Workflow Engine"],
            "fix_versions": ["R1"],
            "sprint_values": ["Sprint 6"],
            "backlog_rank": "0|i00123:",
            "has_backlog_rank": True,
            "task_type_ids": ["#V#delegated_task_specification"],
            "task_source_id": "#V#jira_imported_task_source",
            "task_source_ids": ["#V#jira_imported_task_source"],
            "created_by_concept_id": "#V#user_creator",
            "report_to_concept_id": "#V#user_manager",
        },
    ).payload
    assert payload.get("success") is True
    assert captured.get("start_from") == "2026-03-01T00:00:00Z"
    assert captured.get("start_to") == "2026-03-15T00:00:00Z"
    assert captured.get("epic_task_concept_id") == "#V#task_epic_1"
    assert captured.get("has_epic") is True
    assert captured.get("components") == ["Workflow Engine"]
    assert captured.get("fix_versions") == ["R1"]
    assert captured.get("sprint_values") == ["Sprint 6"]
    assert captured.get("backlog_rank") == "0|i00123:"
    assert captured.get("has_backlog_rank") is True
    assert captured.get("task_type_ids") == ["#V#delegated_task_specification"]
    assert captured.get("task_source_id") == "#V#jira_imported_task_source"
    assert captured.get("task_source_ids") == ["#V#jira_imported_task_source"]
    assert captured.get("created_by_concept_id") == "#V#user_creator"
    assert captured.get("report_to_concept_id") == "#V#user_manager"
    _assert_schema_conformance(gateway, "task_search", payload)
