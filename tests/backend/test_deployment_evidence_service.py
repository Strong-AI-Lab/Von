"""Receipt lifecycle against an isolated canonical-service fixture; no live writes."""

import copy

import pytest
from flask import Flask

from src.backend.services import deployment_evidence_service as service
from src.backend.services import task_management_service as tasks
from src.backend.services import task_work_product_service as products
from src.backend.server.routes import task_routes as routes


@pytest.fixture
def records(monkeypatch):
    docs = {cid: {"concept_id": cid} for cid in service.VOCABULARY}
    hidden = set()

    def matches(doc, query):
        if "$or" in query:
            return any(matches(doc, part) for part in query["$or"])
        for key, value in query.items():
            current = doc
            for part in key.split("."):
                current = current.get(part, {}) if isinstance(current, dict) else None
            if not (
                value in current if isinstance(current, list) else value == current
            ):
                return False
        return True

    def find(query, **_):
        return [
            copy.deepcopy(doc)
            for cid, doc in docs.items()
            if cid not in hidden and matches(doc, query)
        ]

    def create(**kwargs):
        cid = kwargs["concept_id"]
        assert cid not in docs
        docs[cid] = {
            "concept_id": cid,
            "attributes": copy.deepcopy(kwargs.get("attributes", {})),
            "relationships": {
                (
                    "is_an_instance_of"
                    if kwargs.get("create_as_instance", True)
                    else "is_a_type_of"
                ): kwargs["parent_concept_ids"]
            },
        }
        return copy.deepcopy(docs[cid])

    def link(source, predicate, target, **_):
        values = docs[source]["relationships"].setdefault(predicate, [])
        if target not in values:
            values.append(target)
        return {"success": True}

    def task(cid):
        if cid in hidden or not cid.startswith("#V#task_"):
            raise tasks.TaskNotFoundError("Unavailable")
        return cid, {"concept_id": cid}

    monkeypatch.setattr(service.ConceptsRepository, "find", find)
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda query, **kwargs: next(iter(find(query)), None),
    )
    monkeypatch.setattr(service.concept_service, "create_concept", create)
    monkeypatch.setattr(service, "add_relationship", link)
    monkeypatch.setattr(service, "can_access_concept", lambda cid: cid not in hidden)
    monkeypatch.setattr(products, "can_access_concept", lambda cid: cid not in hidden)
    monkeypatch.setattr(tasks, "_get_task_doc", task)
    return docs, hidden


def receipt(**changes):
    return {
        "build_id": "sha256:artefact-a",
        "source_revision": "ab10a303e",
        "deployment_id": "dgx-20260912-1",
        "environment": "production",
        "target": "Von service and PWA",
        "status": "deployed",
        "observed_at": "2026-09-12T22:00:00Z",
        "deployed_at": "2026-09-12T21:59:00Z",
        "evidence": "Fixture: health revision and PWA asset manifest observed",
        "receipt_id": "ci-run-1-deployed",
        "implements_tasks": ["#V#task_a"],
        "verifies_tasks": ["#V#task_b"],
        **changes,
    }


def ingest(**changes):
    return service.ingest_deployment(
        receipt(**changes), actor="#V#alice", organisation="#V#lab"
    )


def test_ingestion_retry_bidirectional_reads_and_multiple_builds(records):
    first = ingest()
    count = len(records[0])
    assert ingest() == first
    assert len(records[0]) == count
    assert first["implements_tasks"] == ["#V#task_a"]
    assert first["verifies_tasks"] == ["#V#task_b"]
    assert service.list_build_deployments(first["build"]["concept_id"]) == [first]
    second = ingest(
        build_id="sha256:artefact-b", deployment_id="dgx-2", receipt_id="ci-2"
    )
    assert {d["concept_id"] for d in service.list_task_deployments("#V#task_a")} == {
        first["concept_id"],
        second["concept_id"],
    }
    assert len(service.list_task_deployments("#V#task_b")) == 2


def test_status_changes_retain_provenance_and_late_receipts(records):
    first = ingest()
    verified = ingest(
        status="verified", receipt_id="verify-1", observed_at="2026-09-12T22:05:00Z"
    )
    assert verified["status"] == "verified"
    assert len(verified["observations"]) == 2
    assert verified["observations"][-1]["recorded_by"] == "#V#alice"
    assert ingest() == verified
    assert first["build"] == verified["build"]
    result = ingest(
        status="failed", receipt_id="conflict", observed_at="2026-09-12T22:05:00Z"
    )
    assert result["status"] == "ambiguous"


def test_rollback_reuses_build_but_has_new_attempt_and_environment_boundary(records):
    first = ingest()
    rollback = ingest(
        deployment_id="rollback-1",
        receipt_id="rollback-1",
        supersedes=first["concept_id"],
    )
    assert rollback["build"] == first["build"]
    assert rollback["concept_id"] != first["concept_id"]
    assert rollback["supersedes"] == first["concept_id"]
    with pytest.raises(service.DeploymentEvidenceError, match="same environment"):
        ingest(
            deployment_id="staging-1",
            receipt_id="staging-1",
            environment="staging",
            supersedes=first["concept_id"],
        )


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_planned_and_built_do_not_assert_deployment(records, environment):
    result = ingest(environment=environment, status="planned", deployed_at=None)
    assert result["deployed_at"] is None
    assert (
        ingest(
            environment=environment,
            status="built",
            deployed_at=None,
            receipt_id="built",
            observed_at="2026-09-12T22:01:00Z",
        )["status"]
        == "built"
    )


@pytest.mark.parametrize(
    "changes",
    [{"source_revision": "other"}, {"target": "other"}, {"status": "verified"}],
)
def test_immutable_id_conflict_precedes_writes(records, changes):
    ingest()
    before = copy.deepcopy(records[0])
    with pytest.raises(service.DeploymentEvidenceError, match="Immutable"):
        ingest(**changes)
    assert records[0] == before


@pytest.mark.parametrize(
    "changes",
    [
        {"environment": "unknown"},
        {"deployed_at": None},
        {"observed_at": "2026-09-12"},
        {"implements_tasks": "#V#task_a"},
        {"evidence": ""},
        {"actor": "forged"},
    ],
)
def test_invalid_receipts_cannot_write(records, changes):
    before = copy.deepcopy(records[0])
    with pytest.raises(service.DeploymentEvidenceError):
        ingest(**changes)
    assert records[0] == before


def test_hidden_tasks_and_products_are_not_exposed(records):
    first = ingest()
    records[1].add("#V#task_b")
    result = service.get_deployment(first["concept_id"])
    assert result["verifies_tasks"] == []
    assert result["observations"][0]["task_links"][service.VERIFIES] == []
    with pytest.raises(tasks.TaskNotFoundError):
        service.list_task_deployments("#V#task_b")
    records[1].add(first["concept_id"])
    with pytest.raises(service.DeploymentEvidenceError):
        service.get_deployment(first["concept_id"])


def test_selected_deployment_work_product_exposes_unverified_state(records):
    first = ingest()
    result = products.resolve_task_work_product(
        {
            "relationships": {
                products.PREDICATE_HAS_CURRENT_WORK_PRODUCT: [first["concept_id"]]
            }
        },
        include_content=True,
    )
    assert result["kind"] == "deployment"
    assert result["deployment"]["status"] == "deployed"
    assert '"status": "deployed"' in result["content"]


def test_routes_bind_actor_and_read_back(records, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(routes.task_bp, url_prefix="/api/tasks")
    monkeypatch.setattr(routes, "_get_current_user_concept_id", lambda: "#V#alice")
    monkeypatch.setattr(routes, "_get_current_org_concept_id", lambda: "#V#lab")
    client = app.test_client()
    result = client.post("/api/tasks/deployments", json=receipt())
    assert result.status_code == 200
    cid = result.json["concept_id"].replace("#", "%23")
    assert client.get(f"/api/tasks/deployments/{cid}").json == result.json
    assert client.get("/api/tasks/%23V%23task_a/deployments").json["deployments"] == [
        result.json
    ]
    bid = result.json["build"]["concept_id"].replace("#", "%23")
    assert client.get(f"/api/tasks/builds/{bid}/deployments").json["deployments"] == [
        result.json
    ]
    monkeypatch.setattr(routes, "_get_current_user_concept_id", lambda: None)
    assert client.post("/api/tasks/deployments", json=receipt()).status_code == 401
    assert client.get(f"/api/tasks/deployments/{cid}").status_code == 401


def test_partial_ingestion_can_be_retried_without_duplicate_records(
    records, monkeypatch
):
    link = service.add_relationship
    failed = False

    def fail_once(source, predicate, target, **kwargs):
        nonlocal failed
        if predicate == service.OBSERVES and not failed:
            failed = True
            return {"success": False}
        return link(source, predicate, target, **kwargs)

    monkeypatch.setattr(service, "add_relationship", fail_once)
    with pytest.raises(service.DeploymentEvidenceError, match="retry"):
        ingest()
    count = len(records[0])
    result = ingest()
    assert len(records[0]) == count
    assert result["status"] == "deployed"
    assert len(result["observations"]) == 1


def test_missing_vocabulary_fails_before_creating_receipt(records):
    records[0].pop(service.BUILD)
    before = copy.deepcopy(records[0])
    with pytest.raises(service.DeploymentEvidenceError, match="unavailable"):
        ingest()
    assert records[0] == before


def test_explicit_vocabulary_creation_is_scoped_idempotent_release_input(
    records, monkeypatch
):
    records[0].clear()
    create = service.concept_service.create_concept
    calls = []

    def tracked(**kwargs):
        calls.append(kwargs)
        return create(**kwargs)

    monkeypatch.setattr(service.concept_service, "create_concept", tracked)
    service.bootstrap_vocabulary(actor="#V#alice", organisation="#V#lab")
    service.bootstrap_vocabulary(actor="#V#alice", organisation="#V#lab")
    assert set(records[0]) == set(service.VOCABULARY)
    assert len(calls) == len(service.VOCABULARY)
    assert all(call["created_by_concept_id"] == "#V#alice" for call in calls)
    assert all(call["organisation_concept_id"] == "#V#lab" for call in calls)
    assert all(call["maintain_relationship_inverses"] is False for call in calls)
    assert all("visibility_scope_mode" not in call for call in calls)

    from src.backend.vontology.utils_vontology import is_predicate

    for cid, doc in records[0].items():
        assert is_predicate(doc) == (
            cid not in {service.BUILD, service.DEPLOYMENT, service.OBSERVATION}
        )


def test_hidden_build_does_not_block_other_task_evidence(records):
    first = ingest()
    second = ingest(
        build_id="build-b", deployment_id="attempt-b", receipt_id="receipt-b"
    )
    records[1].add(first["build"]["concept_id"])
    assert service.list_task_deployments("#V#task_a") == [second]
