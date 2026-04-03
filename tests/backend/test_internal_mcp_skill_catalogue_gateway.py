"""Gateway-level coverage for skill catalogue MCP tools."""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_skill_catalogue_list_gateway_invoke(monkeypatch):
    gateway = _build_gateway()
    captured: dict[str, object] = {}

    def _fake_list_skill_catalogue(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "roots_by_scope": {"project": ["C:\\repo\\.agents\\skills"]},
            "skills": [
                {
                    "name": "triage-issue",
                    "workflow_id": "#V#skill_triage_issue_workflow",
                }
            ],
            "count": 1,
        }

    monkeypatch.setattr(
        "src.backend.services.skill_catalogue_service.list_skill_catalogue",
        _fake_list_skill_catalogue,
    )

    payload = gateway.invoke(
        "skill_catalogue_list",
        {
            "include_default_roots": False,
            "project_roots": ["C:\\repo\\.agents\\skills"],
            "include_body": True,
        },
    ).payload

    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["skills"][0]["workflow_id"] == "#V#skill_triage_issue_workflow"
    assert captured == {
        "include_default_roots": False,
        "project_roots": ["C:\\repo\\.agents\\skills"],
        "personal_roots": [],
        "extension_roots": [],
        "shared_roots": [],
        "include_body": True,
    }


def test_skill_catalogue_sync_gateway_invoke(monkeypatch):
    gateway = _build_gateway()
    captured: dict[str, object] = {}

    def _fake_sync_skill_catalogue_to_vontology(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "dry_run": True,
            "ensured_concept_ids": ["#V#agent_skill"],
            "synced_skills": [],
            "count": 0,
        }

    monkeypatch.setattr(
        "src.backend.services.skill_catalogue_service.sync_skill_catalogue_to_vontology",
        _fake_sync_skill_catalogue_to_vontology,
    )

    payload = gateway.invoke(
        "skill_catalogue_sync",
        {
            "shared_roots": ["C:\\shared\\skills"],
            "dry_run": True,
        },
    ).payload

    assert payload["success"] is True
    assert payload["dry_run"] is True
    assert captured == {
        "include_default_roots": True,
        "project_roots": [],
        "personal_roots": [],
        "extension_roots": [],
        "shared_roots": ["C:\\shared\\skills"],
        "dry_run": True,
    }
