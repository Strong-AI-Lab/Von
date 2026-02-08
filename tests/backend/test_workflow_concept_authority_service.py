from types import SimpleNamespace
from typing import Any, cast

from src.backend.workflows import workflow_concept_authority_service as authority_service


class _DummyRegistry:
    def __init__(self, workflow_ids: list[str]) -> None:
        self._workflow_ids = workflow_ids
        self._registrations = {
            workflow_id: SimpleNamespace(
                workflow_id=workflow_id,
                source="built_in",
                purpose=f"Purpose for {workflow_id}",
            )
            for workflow_id in workflow_ids
        }

    def all_workflow_ids(self):
        return list(self._workflow_ids)

    def get_registration(self, workflow_id: str):
        return self._registrations.get(workflow_id)


def test_workflow_authority_report_classifies_missing_and_untyped(monkeypatch):
    registry = _DummyRegistry(
        workflow_ids=["#V#wf_missing", "#V#wf_untyped", "#V#wf_valid"]
    )

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow", "#V#durable_workflow"),
    )

    def _mock_load_concept(concept_id: str):
        if concept_id == "#V#wf_missing":
            return None, None
        if concept_id == "#V#wf_untyped":
            return {
                "concept_id": concept_id,
                "relationships": {"is_an_instance_of": ["#V#other_type"]},
            }, None
        if concept_id == "#V#wf_valid":
            return {
                "concept_id": concept_id,
                "relationships": {"is_an_instance_of": ["#V#durable_workflow"]},
            }, None
        return None, None

    monkeypatch.setattr(authority_service, "_load_concept", _mock_load_concept)

    report = authority_service.build_workflow_concept_authority_report(
        registry=cast(Any, registry)
    )

    assert report["drift_detected"] is True
    assert report["counts"]["missing_concepts"] == 1
    assert report["counts"]["missing_required_type"] == 1
    assert report["counts"]["valid"] == 1
    assert "#V#wf_missing" in report["missing_concept_workflow_ids"]
    assert "#V#wf_untyped" in report["missing_required_type_by_workflow_id"]


def test_bootstrap_workflow_concepts_creates_missing(monkeypatch):
    registry = _DummyRegistry(workflow_ids=["#V#wf_create"])

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow",),
    )
    monkeypatch.setattr(authority_service, "_load_concept", lambda _cid: (None, None))

    created_payloads: list[dict] = []

    def _mock_create_concept(**kwargs):
        created_payloads.append(kwargs)
        return {"concept_id": kwargs.get("concept_id")}

    monkeypatch.setattr(
        authority_service.concept_service,
        "create_concept",
        _mock_create_concept,
    )

    report = authority_service.bootstrap_workflow_concepts(
        registry=cast(Any, registry)
    )

    assert report["counts"]["created"] == 1
    assert report["created_workflow_ids"] == ["#V#wf_create"]
    assert created_payloads
    payload = created_payloads[0]
    assert payload["concept_id"] == "#V#wf_create"
    assert payload["parent_concept_ids"] == ["#V#ai_workflow"]
    assert payload["create_as_instance"] is True


def test_bootstrap_workflow_concepts_enforces_required_type(monkeypatch):
    registry = _DummyRegistry(workflow_ids=["#V#wf_retype"])

    monkeypatch.setattr(
        authority_service,
        "resolve_available_workflow_type_ids",
        lambda: ("#V#ai_workflow",),
    )
    monkeypatch.setattr(
        authority_service,
        "_load_concept",
        lambda _cid: (
            {
                "concept_id": "#V#wf_retype",
                "relationships": {"is_an_instance_of": ["#V#legacy_type"]},
            },
            None,
        ),
    )

    update_payloads: list[dict] = []

    def _mock_update_concept(concept_id: str, payload: dict):
        update_payloads.append({"concept_id": concept_id, "payload": payload})
        return {"concept_id": concept_id, "relationships": payload["relationships"]}

    monkeypatch.setattr(
        authority_service.concept_service,
        "update_concept",
        _mock_update_concept,
    )

    report = authority_service.bootstrap_workflow_concepts(
        registry=cast(Any, registry)
    )

    assert report["counts"]["updated"] == 1
    assert report["updated_workflow_ids"] == ["#V#wf_retype"]
    assert update_payloads
    payload = update_payloads[0]["payload"]
    assert "#V#ai_workflow" in payload["relationships"]["is_an_instance_of"]
