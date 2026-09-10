from __future__ import annotations

import asyncio
from typing import Any, cast

from src.backend.services import jira_task_migration_runner_service as mod


class _FakeInvokeResult:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.duration_ms = 0.0


class _FakeGateway:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = responses
        self.calls: list[dict] = []

    def invoke(self, tool_name: str, payload: dict) -> _FakeInvokeResult:
        self.calls.append({"tool_name": tool_name, "payload": payload})
        return _FakeInvokeResult(self._responses[len(self.calls) - 1])


def test_run_jira_task_migration_filters_recent_catch_up_and_only_missing(
    monkeypatch,
) -> None:
    class _FakeProxy:
        async def search(
            self,
            *,
            jql: str,
            max_results=None,
            next_page_token=None,
            fields=None,
        ):
            assert "statusCategory != Done" in jql
            assert "ORDER BY key ASC" in jql
            assert max_results == 100
            assert next_page_token is None
            assert isinstance(fields, list)
            return {
                "issues": [
                    {"key": "JVNAUTOSCI-1199", "fields": {"summary": "Old"}},
                    {"key": "JVNAUTOSCI-1200", "fields": {"summary": "Imported"}},
                    {"key": "JVNAUTOSCI-1201", "fields": {"summary": "Recent"}},
                    {"key": "JVNAUTOSCI-1407", "fields": {"summary": "Newest"}},
                ]
            }

        async def get_issue(self, *, issue_key: str, fields=None):  # pragma: no cover
            raise AssertionError(f"unexpected get_issue call for {issue_key}")

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(mod, "get_jira_proxy", _fake_get_jira_proxy)
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.list_imported_jira_issue_keys",
        lambda **_kwargs: ["JVNAUTOSCI-1200"],
    )

    gateway = _FakeGateway(
        [
            {
                "success": True,
                "summary": {"total_issues": 2, "created": 2},
                "issues": [],
            }
        ]
    )
    options = mod.JiraTaskMigrationOptions(
        actor_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        only_missing=True,
        min_issue_number=1200,
        passes=1,
        report_path=None,
    )

    report = asyncio.run(
        mod.run_jira_task_migration(options, gateway=cast(Any, gateway))
    )

    assert report["discovery"]["discovered_issue_count"] == 4
    assert report["discovery"]["issue_number_filtered_out_count"] == 1
    assert report["discovery"]["candidate_selected_issue_count"] == 3
    assert report["discovery"]["already_imported_selected_issue_count"] == 1
    assert report["discovery"]["selected_issue_count"] == 2
    assert report["discovery"]["selected_issue_keys_sample"] == [
        "JVNAUTOSCI-1201",
        "JVNAUTOSCI-1407",
    ]
    assert len(gateway.calls) == 1
    assert gateway.calls[0]["tool_name"] == "task_import_jira_issues"
    issue_keys = gateway.calls[0]["payload"]["issue_keys"]
    assert [item["key"] for item in issue_keys] == [
        "JVNAUTOSCI-1201",
        "JVNAUTOSCI-1407",
    ]


def test_run_jira_task_migration_repairs_missing_referenced_targets(
    monkeypatch,
) -> None:
    get_issue_calls: list[str] = []

    class _FakeProxy:
        async def search(
            self,
            *,
            jql: str,  # noqa: ARG002
            max_results=None,  # noqa: ARG002
            next_page_token=None,  # noqa: ARG002
            fields=None,  # noqa: ARG002
        ):
            return {
                "issues": [
                    {"key": "JVNAUTOSCI-1407", "fields": {"summary": "Needs target"}}
                ]
            }

        async def get_issue(self, *, issue_key: str, fields=None):  # noqa: ARG002
            get_issue_calls.append(issue_key)
            return {"key": issue_key, "fields": {"summary": f"Fetched {issue_key}"}}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(mod, "get_jira_proxy", _fake_get_jira_proxy)
    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.list_imported_jira_issue_keys",
        lambda **_kwargs: [],
    )

    gateway = _FakeGateway(
        [
            {
                "success": True,
                "summary": {"total_issues": 1, "updated": 1},
                "issues": [
                    {
                        "relation_results": [
                            {
                                "reason": "target_issue_not_imported",
                                "target_issue_key": "JVNAUTOSCI-1199",
                            }
                        ]
                    }
                ],
            },
            {
                "success": True,
                "summary": {"total_issues": 1, "created": 1},
                "issues": [],
            },
            {
                "success": True,
                "summary": {"total_issues": 1, "updated": 1},
                "issues": [],
            },
        ]
    )
    options = mod.JiraTaskMigrationOptions(
        actor_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        import_referenced_targets=True,
        report_path=None,
        passes=1,
    )

    report = asyncio.run(
        mod.run_jira_task_migration(options, gateway=cast(Any, gateway))
    )

    assert report["current_scope"]["missing_target_issue_keys"] == ["JVNAUTOSCI-1199"]
    assert report["referenced_targets"]["input_issue_count"] == 1
    assert report["current_scope_repair"]["input_issue_count"] == 1
    assert get_issue_calls == ["JVNAUTOSCI-1199"]
    assert len(gateway.calls) == 3
    assert [item["key"] for item in gateway.calls[0]["payload"]["issue_keys"]] == [
        "JVNAUTOSCI-1407"
    ]
    assert [item["key"] for item in gateway.calls[1]["payload"]["issue_keys"]] == [
        "JVNAUTOSCI-1199"
    ]
    assert [item["key"] for item in gateway.calls[2]["payload"]["issue_keys"]] == [
        "JVNAUTOSCI-1407"
    ]


def test_failed_batch_is_checkpointed_and_resume_reuses_completed_work(tmp_path):
    import json
    import pytest
    from dataclasses import replace

    docs = [
        {"id": str(i), "key": f"KKAT-{i}", "fields": {"updated": "v1"}} for i in (1, 2)
    ]
    options = mod.JiraTaskMigrationOptions(
        actor_concept_id="#V#owner",
        project_key="KKAT",
        batch_size=1,
        passes=1,
        preserve_source=True,
        report_path=tmp_path / "report.json",
    )
    first = {"success": True, "summary": {"created": 1}, "issues": []}
    failed = {
        "success": False,
        "fetch": {"fetch_errors": [{"issue_key": "KKAT-2", "error": "unavailable"}]},
    }
    with pytest.raises(RuntimeError, match="failed"):
        mod._run_import_batches(
            gateway=_FakeGateway([first, failed]),
            issue_docs=docs,
            options=options,
            run_label="current_scope",
        )
    checkpoint = json.loads(
        (tmp_path / "report.current_scope.checkpoint.json").read_text()
    )
    assert checkpoint["completed_batches"] == [first]
    assert checkpoint["last_batch"] == failed
    retry = _FakeGateway([first])
    report = mod._run_import_batches(
        gateway=retry,
        issue_docs=docs,
        options=replace(options, resume=True),
        run_label="current_scope",
    )
    assert len(retry.calls) == 1
    assert retry.calls[0]["payload"]["issue_keys"][0]["key"] == "KKAT-2"
    assert report["summary"]["created"] == 2
    changed = [{**docs[0], "fields": {"updated": "v2"}}, docs[1]]
    with pytest.raises(ValueError, match="scope/source changed"):
        mod._run_import_batches(
            gateway=_FakeGateway([]),
            issue_docs=changed,
            options=replace(options, resume=True),
            run_label="current_scope",
        )


def test_discovery_errors_cannot_be_reported_as_empty_project(monkeypatch):
    import pytest

    class Proxy:
        async def search(self, **kwargs):
            return {"success": False, "status_code": 403}

    async def get_proxy():
        return Proxy()

    monkeypatch.setattr(mod, "get_jira_proxy", get_proxy)
    with pytest.raises(RuntimeError, match="not an empty project"):
        asyncio.run(
            mod.discover_jira_issue_docs(
                jql="project = KKAT", fields=["key"], page_size=100
            )
        )
