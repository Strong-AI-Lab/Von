from datetime import UTC, datetime

import mongomock
import pytest

from src.backend.services import task_project_transfer_service as transfer
from src.backend.services.knowledge_federation.protocol import digest, sign

USER = "#V#owner"
PROJECT = "#V#project"
TASK = "#V#task"


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    database = mongomock.MongoClient().test
    monkeypatch.setattr(
        transfer.ConceptsRepository, "collection", lambda: database.concepts
    )
    monkeypatch.setattr(
        transfer.TextRelationsRepository, "collection", lambda: database.text_relations
    )
    monkeypatch.setattr(
        transfer.TextValuesRepository, "collection", lambda: database.text_values
    )
    monkeypatch.setattr(transfer, "can_access_concept", lambda concept_id: True)
    monkeypatch.setattr(transfer, "filter_accessible_concept_ids", lambda ids: set(ids))
    monkeypatch.setattr(transfer, "get_effective_user_concept_id", lambda: USER)
    monkeypatch.setattr(transfer, "get_project_home", lambda project_id: None)
    secret = tmp_path / "key"
    secret.write_text("01" * 32)
    secret.chmod(0o600)
    settings = {
        "enabled": True,
        "key_file": str(secret),
        "task_projects": [PROJECT],
        "audiences": ["user:" + USER],
        "identity_map": {USER: USER},
    }
    source = {"node_id": "atlas", "exports": {"dgx": settings}}
    destination = {"node_id": "dgx", "imports": {"atlas": settings}}
    audience = {"#V#specific_to_user": [USER]}
    database.concepts.insert_many(
        [
            {
                "concept_id": PROJECT,
                "relationships": audience,
                "attributes": {
                    "task_project": {"writer": "von", "collection_concept_ids": []}
                },
            },
            {
                "concept_id": TASK,
                "relationships": {
                    **audience,
                    "#V#has_created_by": ["#V#historical_author"],
                },
                "metadata": {
                    "project_concept_id": PROJECT,
                    "comments": [
                        {"author": "#V#historical_author", "body": "Retain me"}
                    ],
                    "coding_dispatch": {"authority": "must not transfer"},
                    "external_references": {
                        "jira": {"issue_id": "42"},
                        "coding_supervision": {"state": "running"},
                    },
                },
                "created_at": datetime(2024, 1, 1, tzinfo=UTC),
            },
        ]
    )
    return database, source, destination


def test_signed_snapshot_preserves_authors_and_dates_without_execution_state(fixture):
    database, source, destination = fixture
    envelope = transfer.capture_project(PROJECT, config=source, recipient="dgx")
    payload = transfer.verify_snapshot(envelope, config=destination, origin="atlas")
    task = next(
        r
        for r in transfer.decoded(payload["body"])["concepts"]
        if r["concept_id"] == TASK
    )
    assert task["relationships"]["#V#has_created_by"] == ["#V#historical_author"]
    assert task["metadata"]["comments"][0]["author"] == "#V#historical_author"
    assert task["created_at"].year == 2024
    assert "coding_dispatch" not in task["metadata"]
    assert set(task["metadata"]["external_references"]) == {"jira"}
    assert database.concepts.find_one({"concept_id": TASK})["metadata"][
        "coding_dispatch"
    ]
    assert (
        transfer.capture_project(PROJECT, config=source, recipient="dgx")["payload"][
            "digest"
        ]
        == payload["digest"]
    )
    database.concepts.update_one(
        {"concept_id": TASK}, {"$push": {"metadata.comments": {"body": "Later change"}}}
    )
    assert (
        transfer.capture_project(PROJECT, config=source, recipient="dgx")["payload"][
            "digest"
        ]
        != payload["digest"]
    )


def test_tampering_and_wrong_recipient_are_rejected(fixture):
    _, source, destination = fixture
    envelope = transfer.capture_project(PROJECT, config=source, recipient="dgx")
    envelope["payload"]["project_id"] = "#V#another"
    with pytest.raises(PermissionError, match="signature"):
        transfer.verify_snapshot(envelope, config=destination, origin="atlas")
    envelope = transfer.capture_project(PROJECT, config=source, recipient="dgx")
    destination["node_id"] = "another"
    with pytest.raises(PermissionError, match="misdirected"):
        transfer.verify_snapshot(envelope, config=destination, origin="atlas")


def test_same_slug_is_not_an_audience_identity_proof(fixture):
    _, source, destination = fixture
    envelope = transfer.capture_project(PROJECT, config=source, recipient="dgx")
    destination["imports"]["atlas"]["identity_map"] = {}
    with pytest.raises(PermissionError, match="identity mapping"):
        transfer.verify_snapshot(envelope, config=destination, origin="atlas")


def test_inaccessible_member_prevents_partial_export(fixture, monkeypatch):
    _, source, _ = fixture
    monkeypatch.setattr(transfer, "can_access_concept", lambda cid: cid != TASK)
    monkeypatch.setattr(
        transfer, "filter_accessible_concept_ids", lambda ids: set(ids) - {TASK}
    )
    with pytest.raises(PermissionError, match="inaccessible record"):
        transfer.capture_project(PROJECT, config=source, recipient="dgx")


def test_admitted_peer_cannot_include_an_unrelated_concept(fixture):
    _, source, destination = fixture
    payload = transfer.capture_project(PROJECT, config=source, recipient="dgx")[
        "payload"
    ]
    payload["body"]["concepts"].append(
        {"concept_id": "#V#production_prompt", "attributes": {}}
    )
    payload["digest"] = digest(payload["body"])
    envelope = sign(payload, bytes.fromhex("01" * 32))
    with pytest.raises(ValueError, match="Unrelated concept"):
        transfer.verify_snapshot(envelope, config=destination, origin="atlas")


def test_subscription_is_checked_independently_of_identity_map(fixture):
    _, source, destination = fixture
    envelope = transfer.capture_project(PROJECT, config=source, recipient="dgx")
    destination["imports"]["atlas"]["audiences"] = ["user:#V#someone_else"]
    with pytest.raises(PermissionError, match="outside the subscription"):
        transfer.verify_snapshot(envelope, config=destination, origin="atlas")
