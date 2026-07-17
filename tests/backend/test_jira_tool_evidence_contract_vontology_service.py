from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services import (
    jira_tool_evidence_contract_vontology_service as service,
)
from src.backend.services.tool_evidence_projection_service import (
    project_tool_payload_for_llm,
)
from src.backend.services.text_value_service import (
    get_preferred_text_for_concept,
    upsert_singleton_text_relation,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    yield


def _relationships(concept_id: str) -> dict[str, Any]:
    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert concept_doc is not None
    return dict(concept_doc.get("relationships") or {})


def _targets(concept_id: str, predicate: str) -> set[str]:
    values = _relationships(concept_id).get(predicate) or []
    if isinstance(values, str):
        return {values}
    return {str(value) for value in values}


def _attributes(concept_id: str) -> dict[str, Any]:
    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert concept_doc is not None
    return dict(concept_doc.get("attributes") or {})


def test_bootstrap_materialises_and_validates_jira_evidence_graph_from_empty_db(
    _reset_mock_db: Any,
) -> None:
    report = service.bootstrap_jira_tool_evidence_contract()

    assert report["success"] is True
    assert report["schema_version"] == "jira_tool_evidence_contract.v1"
    assert report["contract_concept_id"] == "#V#jira_tool_evidence_contract_v1"
    assert report["vocabulary_report"]["success"] is True
    assert report["counts"]["created_concepts"] == len(
        service.JIRA_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS
    )
    assert report["errors"] == []

    assert _targets(
        service.JIRA_TOOL_EVIDENCE_CONTRACT_ID,
        "#V#tool_contract_applies_to_tool",
    ) == {service.JIRA_GET_ISSUE_TOOL_ID, service.JIRA_SEARCH_TOOL_ID}
    assert _targets(
        service.JIRA_FINAL_ANSWER_VIEW_ID,
        "#V#evidence_view_requires_field",
    ) == set(service.JIRA_REQUIRED_FINAL_ANSWER_FIELD_IDS)
    assert _targets(
        service.JIRA_SEARCH_FINAL_ANSWER_VIEW_ID,
        "#V#evidence_view_requires_field",
    ) == {
        service.JIRA_ISSUES_COLLECTION_FIELD_ID,
        service.JIRA_KEY_FIELD_ID,
        service.JIRA_SUMMARY_FIELD_ID,
        service.JIRA_STATUS_FIELD_ID,
        service.JIRA_IS_LAST_FIELD_ID,
    }

    get_issue_attributes = _attributes(service.JIRA_GET_ISSUE_TOOL_ID)
    assert get_issue_attributes["mcp_tool_name"] == "jira_get_issue"
    assert get_issue_attributes["evidence_role"] == "verification"

    search_attributes = _attributes(service.JIRA_SEARCH_TOOL_ID)
    assert search_attributes["mcp_tool_name"] == "jira_search"
    assert search_attributes["evidence_role"] == "search"

    validation = service.validate_jira_tool_evidence_contract()
    assert validation["success"] is True
    assert validation["missing_concept_ids"] == []
    assert validation["missing_relationships"] == []


def test_bootstrap_is_idempotent_after_clean_environment_materialisation(
    _reset_mock_db: Any,
) -> None:
    first = service.bootstrap_jira_tool_evidence_contract()
    second = service.bootstrap_jira_tool_evidence_contract()

    assert first["success"] is True
    assert second["success"] is True
    assert second["created_concept_ids"] == []
    assert second["repaired_concept_ids"] == []
    assert set(second["existing_concept_ids"]) == set(
        service.canonical_jira_tool_evidence_contract_concept_ids()
    )
    assert second["validation"]["success"] is True
    assert second["errors"] == []


def test_bootstrap_repairs_managed_jira_tool_contract_descriptions(
    _reset_mock_db: Any,
) -> None:
    first = service.bootstrap_jira_tool_evidence_contract()
    assert first["success"] is True

    for concept_id in (service.JIRA_SEARCH_TOOL_ID, service.JIRA_GET_ISSUE_TOOL_ID):
        upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="Stale tool description.",
            lang="en-NZ",
            garbage_collect=True,
        )

    repaired = service.bootstrap_jira_tool_evidence_contract()

    assert repaired["success"] is True
    assert {
        service.JIRA_SEARCH_TOOL_ID,
        service.JIRA_GET_ISSUE_TOOL_ID,
    }.issubset(set(repaired["repaired_concept_ids"]))
    expected_descriptions = {
        service.JIRA_SEARCH_TOOL_ID: service.JIRA_SEARCH_TOOL_DESCRIPTION,
        service.JIRA_GET_ISSUE_TOOL_ID: service.JIRA_GET_ISSUE_TOOL_DESCRIPTION,
    }
    for concept_id, expected_description in expected_descriptions.items():
        preferred = get_preferred_text_for_concept(
            concept_id,
            predicate_precedence=(("hasDescription", "#V#hasDescription"),),
            preferred_languages=("en-NZ", "en"),
        )
        assert preferred is not None
        assert preferred["text"] == expected_description


def test_conversation_turn_support_bootstrap_materialises_jira_contract(
    _reset_mock_db: Any,
) -> None:
    from src.backend.services.conversation_turn_workflow_vontology_service import (
        _ensure_conversation_turn_prompt_support,
    )

    report = _ensure_conversation_turn_prompt_support(
        ensure_tool_evidence_contracts=True
    )

    jira_bootstrap = report["support_bootstraps"]["jira_tool_evidence_contract"]
    assert jira_bootstrap["success"] is True
    assert jira_bootstrap["validation"]["success"] is True
    assert (
        concept_service.get_concept_by_concept_id(
            service.JIRA_TOOL_EVIDENCE_CONTRACT_ID
        )
        is not None
    )


def test_jira_get_issue_projection_preserves_required_answer_fields_and_omits_raw(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_jira_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "jira_get_issue",
        {
            "key": "JVNAUTOSCI-150",
            "fields": {
                "summary": "Describe publication-topic salience",
                "status": {"name": "Done"},
                "assignee": {"displayName": "Michael Witbrock"},
                "issuetype": {"name": "Task"},
                "parent": {
                    "key": "JVNAUTOSCI-144",
                    "fields": {"summary": "Parent"},
                },
                "created": "2025-02-04T00:00:00.000+0000",
                "updated": "2025-02-05T00:00:00.000+0000",
                "description": "Represent publication-topic salience.",
            },
            "raw": {"large": "not selected by the represented view"},
        },
    )

    assert projected is not None
    assert projected["key"] == "JVNAUTOSCI-150"
    assert projected["summary"] == "Describe publication-topic salience"
    assert projected["status"] == "Done"
    assert projected["assignee"] == "Michael Witbrock"
    assert projected["issue_type"] == "Task"
    assert projected["parent"]["key"] == "JVNAUTOSCI-144"
    assert "raw" not in projected

    telemetry = projected["_tool_evidence_projection"]
    preserved_ids = {
        entry["field_concept_id"] for entry in telemetry["preserved_fields"]
    }
    assert set(service.JIRA_REQUIRED_FINAL_ANSWER_FIELD_IDS).issubset(preserved_ids)
    assert telemetry["missing_required_fields"] == []


def test_jira_search_projection_preserves_required_rows_and_omits_detail_only_fields(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_jira_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "jira_search",
        {
            "jql": "project = JVNAUTOSCI ORDER BY created DESC",
            "total": 1,
            "issues": [
                {
                    "key": "JVNAUTOSCI-2535",
                    "fields": {
                        "summary": "Strengthen answer-grounding checks",
                        "status": {"name": "In Progress"},
                        "assignee": {"displayName": "Michael Witbrock"},
                        "issuetype": {"name": "Task"},
                        "created": "2026-06-20T00:00:00.000+0000",
                        "updated": "2026-06-21T00:00:00.000+0000",
                        "description": "Detail-only field must not leak into search rows.",
                    },
                    "raw": "not selected by the represented view",
                }
            ],
            "isLast": True,
            "raw": "not selected by the represented view",
        },
    )

    assert projected is not None
    assert projected["issues_count"] == 1
    assert projected["total"] == 1
    assert projected["jql"] == "project = JVNAUTOSCI ORDER BY created DESC"
    assert projected["is_last"] is True
    assert projected["issues"] == [
        {
            "key": "JVNAUTOSCI-2535",
            "summary": "Strengthen answer-grounding checks",
            "status": "In Progress",
            "assignee": "Michael Witbrock",
            "issue_type": "Task",
            "created": "2026-06-20T00:00:00.000+0000",
            "updated": "2026-06-21T00:00:00.000+0000",
        }
    ]
    assert "description" not in projected["issues"][0]
    assert "raw" not in projected

    telemetry = projected["_tool_evidence_projection"]
    assert telemetry["missing_required_fields"] == []


def test_jira_search_empty_page_does_not_invent_missing_row_fields(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_jira_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "jira_search",
        {"issues": [], "isLast": True},
    )

    assert projected is not None
    assert projected["issues"] == []
    assert projected["issues_count"] == 0
    assert projected["is_last"] is True
    assert projected["_tool_evidence_projection"]["missing_required_fields"] == []


def test_jira_search_missing_pagination_finality_fails_visible(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_jira_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "jira_search",
        {
            "issues": [
                {
                    "key": "JVNAUTOSCI-2575",
                    "fields": {
                        "summary": "Operational reliability",
                        "status": {"name": "In Progress"},
                    },
                }
            ]
        },
    )

    assert projected is not None
    assert "is_last" not in projected
    assert projected["_tool_evidence_projection"]["missing_required_fields"] == [
        {
            "field_concept_id": service.JIRA_IS_LAST_FIELD_ID,
            "output_key": "is_last",
        }
    ]


def test_jira_search_reports_required_fields_per_collection_row(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_jira_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "jira_search",
        {
            "issues": [
                {
                    "key": "JVNAUTOSCI-1",
                    "fields": {"summary": "First", "status": {"name": "Done"}},
                },
                {
                    "key": "JVNAUTOSCI-2",
                    "fields": {"summary": "Second"},
                },
            ],
            "nextPageToken": "opaque-next-page",
            "isLast": False,
        },
    )

    assert projected is not None
    assert projected["next_page_token"] == "opaque-next-page"
    assert projected["is_last"] is False
    assert projected["_tool_evidence_projection"]["missing_required_fields"] == [
        {
            "field_concept_id": service.JIRA_STATUS_FIELD_ID,
            "output_key": "status",
            "location": "issues",
            "row_index": 1,
        }
    ]


def test_projection_reports_missing_required_fields_without_fabricating_values(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_jira_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "jira_get_issue",
        {
            "key": "JVNAUTOSCI-2535",
            "fields": {"summary": "Strengthen answer-grounding checks"},
        },
    )

    assert projected is not None
    assert "status" not in projected
    assert projected["_tool_evidence_projection"]["missing_required_fields"] == [
        {
            "field_concept_id": service.JIRA_STATUS_FIELD_ID,
            "output_key": "status",
        }
    ]


def test_jira_contract_uses_graph_kr_instead_of_json_text_contracts() -> None:
    concept_ids = service.canonical_jira_tool_evidence_contract_concept_ids()
    relationship_specs = service.canonical_jira_tool_evidence_contract_relationships()

    assert all("json" not in concept_id.lower() for concept_id in concept_ids)
    assert all("json" not in spec.predicate.lower() for spec in relationship_specs)
    assert all(spec.predicate != "hasText" for spec in relationship_specs)
