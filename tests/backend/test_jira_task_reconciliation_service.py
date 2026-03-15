from __future__ import annotations

import asyncio

from src.backend.services import jira_task_reconciliation_service as mod


def test_scan_next_jira_task_gap_block_selects_earliest_bounded_block(
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
            assert "project = JVNAUTOSCI" in jql
            assert "statusCategory != Done" not in jql
            assert "ORDER BY key ASC" in jql
            assert max_results == 100
            assert next_page_token is None
            assert fields == ["summary"]
            return {
                "issues": [
                    {"key": "JVNAUTOSCI-1001"},
                    {"key": "JVNAUTOSCI-1002"},
                    {"key": "JVNAUTOSCI-1003"},
                    {"key": "JVNAUTOSCI-1004"},
                    {"key": "JVNAUTOSCI-1005"},
                    {"key": "JVNAUTOSCI-1006"},
                    {"key": "JVNAUTOSCI-1007"},
                    {"key": "JVNAUTOSCI-1008"},
                ]
            }

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.list_imported_jira_issue_keys",
        lambda **_kwargs: [
            "JVNAUTOSCI-1001",
            "JVNAUTOSCI-1002",
            "JVNAUTOSCI-1007",
            "JVNAUTOSCI-1008",
            "OTHER-99",
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_migration_runner_service.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    result = asyncio.run(
        mod.scan_next_jira_task_gap_block(
            mod.JiraTaskGapScanOptions(
                organisation_concept_id="#V#sail_org",
                include_done=True,
                gap_block_size=2,
            )
        )
    )

    assert result["gap_block_found"] is True
    assert result["imported_project_issue_count"] == 4
    assert result["missing_issue_count"] == 4
    assert result["missing_block_count"] == 1
    assert result["remaining_block_count_after_selected_block"] == 1
    assert result["remaining_missing_issue_count_after_selected_block"] == 2
    assert result["highest_observed_issue_number"] == 1008
    assert result["selected_gap_block"] == {
        "min_issue_number": 1003,
        "max_issue_number": 1004,
        "selected_from_block_max_issue_number": 1006,
        "issue_count": 2,
        "issue_keys": ["JVNAUTOSCI-1003", "JVNAUTOSCI-1004"],
    }


def test_scan_next_jira_task_gap_block_reports_complete_coverage(monkeypatch) -> None:
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
                    {"key": "JVNAUTOSCI-2001"},
                    {"key": "JVNAUTOSCI-2002"},
                ]
            }

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.services.jira_task_import_service.list_imported_jira_issue_keys",
        lambda **_kwargs: [
            "JVNAUTOSCI-2001",
            "JVNAUTOSCI-2002",
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.jira_task_migration_runner_service.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    result = asyncio.run(
        mod.scan_next_jira_task_gap_block(mod.JiraTaskGapScanOptions())
    )

    assert result["gap_block_found"] is False
    assert result["missing_issue_count"] == 0
    assert result["missing_block_count"] == 0
    assert result["selected_gap_block"] is None
