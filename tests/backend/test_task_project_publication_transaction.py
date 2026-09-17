"""Real Mongo acceptance for the project publication boundary.

Run only against a disposable transaction fixture, never the application DB:
VON_TASK_TRANSFER_TEST_URI=mongodb://127.0.0.1:27019/?replicaSet=von_task_transfer_test
"""

import os
import uuid
from datetime import UTC, datetime

import pytest
from bson import ObjectId
from pymongo import MongoClient

from src.backend.security.access_control import override_current_actor
from src.backend.services import task_project_publication_service as publish
from src.backend.services.knowledge_federation.protocol import digest, sign
from src.backend.services.task_project_transfer_service import VERSION, portable

pytestmark = pytest.mark.skipif(
    not os.getenv("VON_TASK_TRANSFER_TEST_URI"),
    reason="Disposable Mongo replica-set fixture required",
)
USER = "#V#transfer_fixture_owner"
PROJECT = "#V#transfer_fixture_project"
TASK = "#V#transfer_fixture_task"


@pytest.fixture
def scenario(tmp_path):
    uri = os.environ["VON_TASK_TRANSFER_TEST_URI"]
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    assert client.admin.command("hello").get("setName") == "von_task_transfer_test"
    database = client["test_project_publication_" + uuid.uuid4().hex]
    database.concepts.create_index("concept_id", unique=True)
    database.text_values.create_index([("fingerprint", 1), ("lang", 1)], unique=True)
    database.text_relations.create_index(
        [("subject_concept_id", 1), ("predicate", 1), ("object_text_id", 1)],
        unique=True,
    )
    old = {
        "concept_id": TASK,
        "metadata": {
            "external_references": {"jira": {"issue_id": "42"}},
            "comments": [{"id": "old", "body": "Retained"}],
        },
        "relationships": {"#V#specific_to_user": [USER]},
    }
    database.concepts.insert_one(old)
    database.unrelated.insert_one({"_id": "keep", "text": "Existing DGX work"})
    value_id, relation_id = ObjectId(), ObjectId()
    body = portable(
        {
            "concepts": [
                {
                    "concept_id": PROJECT,
                    "relationships": {"#V#specific_to_user": [USER]},
                    "attributes": {
                        "task_project": {"writer": "von", "collection_concept_ids": []}
                    },
                },
                {
                    "concept_id": TASK,
                    "relationships": {
                        "#V#specific_to_user": [USER],
                        "#V#has_created_by": ["#V#historical_author"],
                    },
                    "metadata": {
                        "project_concept_id": PROJECT,
                        "external_references": {"jira": {"issue_id": "42"}},
                        "comments": [
                            {"id": "old", "body": "Retained"},
                            {"id": "new", "body": "New source history"},
                        ],
                    },
                },
            ],
            "files": [],
            "text_values": [
                {
                    "_id": value_id,
                    "text": "Original exact  text",
                    "lang": "en",
                    "fingerprint": "old",
                }
            ],
            "text_relations": [
                {
                    "_id": relation_id,
                    "subject_concept_id": TASK,
                    "predicate": "hasDescription",
                    "object_text_id": value_id,
                    "context": {},
                }
            ],
        }
    )
    payload = {
        "schema_version": VERSION,
        "origin": "source",
        "recipient": "destination",
        "project_id": PROJECT,
        "actor": USER,
        "captured_at": datetime.now(UTC).isoformat(),
        "source_home": {
            "state": "frozen",
            "home_node_id": "source",
            "epoch": 1,
            "source_identity": {"project": "42"},
        },
        "body": body,
        "digest": digest(body),
    }
    keyfile = tmp_path / "key"
    keyfile.write_text("01" * 32)
    keyfile.chmod(0o600)
    config = {
        "node_id": "destination",
        "home_url": "https://destination.invalid",
        "imports": {
            "source": {
                "enabled": True,
                "key_file": str(keyfile),
                "task_projects": [PROJECT],
                "identity_map": {USER: USER},
                "audiences": ["user:" + USER],
            }
        },
    }
    args = {
        "config": config,
        "origin": "source",
        "database": database,
        "expected_destination_digests": {
            PROJECT: None,
            TASK: publish.concept_digest(old),
        },
        "expected_destination_text_digest": publish.destination_text_digest(
            database, [PROJECT, TASK]
        ),
        "staged_files": {},
        "blob_store": None,
    }
    try:
        with override_current_actor(USER, None):
            yield database, sign(payload, bytes.fromhex("01" * 32)), args
    finally:
        client.drop_database(database.name)
        client.close()


def test_interruption_preserves_prior_complete_view_then_retry_is_atomic(scenario):
    database, envelope, args = scenario
    before = database.concepts.find_one({"concept_id": TASK})

    def interrupt(db, session):
        assert db.concepts.find_one({"concept_id": PROJECT}, session=session)
        assert db.concepts.find_one({"concept_id": PROJECT}) is None
        assert db.concepts.find_one({"concept_id": TASK}) == before
        raise RuntimeError("Injected interrupted publication")

    with pytest.raises(RuntimeError, match="Injected"):
        publish.publish_project_snapshot(envelope, **args, _after_concepts=interrupt)
    assert database.concepts.find_one({"concept_id": TASK}) == before
    assert database.concepts.find_one({"concept_id": PROJECT}) is None
    assert database.task_project_homes.count_documents({}) == 0
    receipt = publish.publish_project_snapshot(envelope, **args)
    assert receipt["canonical_readback"] and receipt["state"] == "prepared"
    verified = publish.verify_project_publication(
        envelope, config=args["config"], origin="source", database=database
    )
    assert verified["text_relations_verified"] == 1
    assert database.concepts.find_one({"concept_id": TASK})["relationships"][
        "#V#has_created_by"
    ] == ["#V#historical_author"]
    assert database.unrelated.find_one({"_id": "keep"})["text"] == "Existing DGX work"
    database.concepts.update_one(
        {"concept_id": TASK}, {"$set": {"metadata.later_accepted_edit": "Keep me"}}
    )
    assert publish.publish_project_snapshot(envelope, **args)["_id"] == receipt["_id"]
    assert (
        database.concepts.find_one({"concept_id": TASK})["metadata"][
            "later_accepted_edit"
        ]
        == "Keep me"
    )
    assert database.task_dispatch_authorities.find_one({"_id": TASK})["actor_concept_id"] is None


def test_changed_destination_is_not_replaced_by_an_older_preflight(scenario):
    database, envelope, args = scenario
    database.concepts.update_one(
        {"concept_id": TASK},
        {
            "$push": {
                "metadata.comments": {"id": "local", "body": "Accepted local change"}
            }
        },
    )
    with pytest.raises(ValueError, match="changed after transfer preflight"):
        publish.publish_project_snapshot(envelope, **args)
    assert database.concepts.find_one({"concept_id": PROJECT}) is None
    assert (
        database.concepts.find_one({"concept_id": TASK})["metadata"]["comments"][-1][
            "id"
        ]
        == "local"
    )


def test_text_only_change_after_preflight_is_preserved(scenario):
    database, envelope, args = scenario
    value_id = database.text_values.insert_one(
        {"text": "Later local text", "lang": "en", "fingerprint": "local"}
    ).inserted_id
    relation_id = database.text_relations.insert_one(
        {
            "subject_concept_id": TASK,
            "predicate": "hasDescription",
            "object_text_id": value_id,
        }
    ).inserted_id
    with pytest.raises(ValueError, match="text changed"):
        publish.publish_project_snapshot(envelope, **args)
    assert database.text_relations.find_one({"_id": relation_id})
    assert database.concepts.find_one({"concept_id": PROJECT}) is None
