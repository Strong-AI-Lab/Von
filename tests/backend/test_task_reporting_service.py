"""Reporting diagnostics preserve deliberate choices and caller visibility."""

import pytest
from pymongo.errors import PyMongoError

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.integrations.internal_mcp import catalogue
from src.backend.security.access_control import override_current_actor
from src.backend.services import task_management_service as tasks
from src.backend.services.task_reporting_service import task_reporting_projection


@pytest.mark.parametrize(
    "targets,explicit,status,warning",
    [
        (["#V#manager"], None, "resolved", False),
        (["#V#manager"], "#V#manager", "resolved", False),
        (["#V#manager"], "#V#owner", "resolved", True),
        ([], "#V#owner", "unset", False),
        (["#V#manager", "#V#owner"], "#V#owner", "ambiguous", False),
        (["#V#worker"], "#V#owner", "self_reference", False),
        (["#V#hidden"], "#V#owner", "supervisor_inaccessible", False),
        (None, "#V#owner", "assignee_inaccessible", False),
        (RuntimeError("offline"), "#V#owner", "lookup_failed", False),
        (PyMongoError("offline"), "#V#owner", "lookup_failed", False),
    ],
)
def test_default_and_override(monkeypatch, targets, explicit, status, warning):
    def find(query, projection=None):
        cid = query["concept_id"]
        if cid == "#V#worker":
            if isinstance(targets, Exception):
                raise targets
            return (
                None
                if targets is None
                else {"relationships": {"#V#has_supervisor": targets}}
            )
        return {"concept_id": cid} if cid in {"#V#owner", "#V#manager"} else None

    monkeypatch.setattr(ConceptsRepository, "find_one", find)
    task = {"assignee_concept_id": "#V#worker", "report_to_concept_id": explicit}
    result = task_reporting_projection(task)
    assert result["default_reporting_resolution"]["status"] == status
    assert bool(result["reporting_warnings"]) == warning
    assert task["report_to_concept_id"] == explicit
    if explicit:
        assert result["reporting_resolution"]["concept_id"] == explicit
    if status != "resolved":
        assert result["default_reporting_resolution"]["concept_id"] is None
    if warning:
        assert result["reporting_warnings"] == [
            {
                "code": "reporting_override_differs_from_default",
                "supplied_concept_id": "#V#owner",
                "default_concept_id": "#V#manager",
                "supplied_source": "task.report_to_concept_id",
                "default_source": "assignee.#V#has_supervisor",
            }
        ]


@pytest.fixture
def reporting_db(monkeypatch):
    from src.backend.db.mongo_client import close_connection

    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_reporting_projection")
    close_connection()
    for cid in ("#V#thing", "#V#predicate", "#V#task_specification"):
        ConceptsRepository.insert_one({"concept_id": cid, "relationships": {}})
    tasks.ensure_task_ontology()
    monkeypatch.setattr(tasks, "maybe_launch_task_created_workflow", lambda **kw: None)
    for cid in ("#V#owner", "#V#manager"):
        ConceptsRepository.insert_one(
            {
                "concept_id": cid,
                "relationships": (
                    {"#V#has_supervisor": ["#V#manager"]} if cid == "#V#owner" else {}
                ),
            }
        )
    yield
    close_connection()


@pytest.mark.parametrize("explicit", [None, "#V#owner"])
def test_canonical_create_read_and_retry(reporting_db, explicit):
    args = {
        "title": "Reporting diagnostics",
        "description": "Keep the user's explicit choice",
        "acting_user_concept_id": "#V#owner",
        "created_by_concept_id": "#V#owner",
        "organisation_concept_id": "#V#org",
        "namespace": "#V#owner@org",
        "request_id": "reporting-" + str(explicit),
        "report_to_concept_id": explicit,
    }
    with override_current_actor("#V#owner", "#V#org"):
        first = catalogue._task_create(**args)
        read = tasks.get_task(first["task_concept_id"])
        assert first["success"], first
        retry = catalogue._task_create(**args)
    assert retry["success"] and retry["idempotent_replay"]
    for field in (
        "reporting_resolution",
        "default_reporting_resolution",
        "reporting_warnings",
    ):
        assert (
            first[field]
            == read[field]
            == retry[field]
            == first["canonical_read_back"][field]
            == retry["canonical_read_back"][field]
        )
    assert bool(first["reporting_warnings"]) == bool(explicit)
    assert read["report_to_concept_id"] == explicit


def test_explicit_other_target_still_denied(reporting_db):
    with override_current_actor("#V#owner", "#V#org"):
        result = catalogue._task_create(
            title="Denied",
            description="No new reporting authority",
            acting_user_concept_id="#V#owner",
            created_by_concept_id="#V#owner",
            organisation_concept_id="#V#org",
            request_id="denied",
            report_to_concept_id="#V#manager",
        )
    assert result["error_code"] == "task_report_scope_denied"


def test_private_default_is_not_disclosed(reporting_db):
    ConceptsRepository.update_one(
        {"concept_id": "#V#manager"},
        {"$set": {"relationships.#V#specific_to_user": ["#V#other"]}},
    )
    with override_current_actor("#V#owner", "#V#org"):
        result = task_reporting_projection(
            {"assignee_concept_id": "#V#owner", "report_to_concept_id": "#V#owner"}
        )
    assert result["default_reporting_resolution"]["status"] != "resolved"
    assert result["default_reporting_resolution"]["concept_id"] is None
    assert result["reporting_warnings"] == []


def test_create_contract_describes_default_and_advisory():
    definition = catalogue.build_default_catalogue().get("task_create")
    assert "Omit it" in definition.description
    assert "reporting_override_differs_from_default" in definition.description
    assert definition.output_schema.optional["default_reporting_resolution"] is dict
    assert definition.output_schema.optional["reporting_warnings"] is list
    assert "supplied_source" in definition.output_schema.description
