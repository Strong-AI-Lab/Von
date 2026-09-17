"""Atomic publication of an admitted task-project snapshot.

The transfer adapter owns these canonical concept/text writes. File bytes must
already be staged and verified. One MongoDB transaction publishes the complete
view and a prepared (non-executing) home. Execution state is never imported.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime

from bson import ObjectId
from pymongo import InsertOne, ReplaceOne, UpdateOne
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from ..db.mongo_client import get_db
from ..security.access_control import get_effective_user_concept_id
from .knowledge_federation.protocol import digest
from .task_project_transfer_service import (
    decoded,
    portable,
    verify_snapshot,
    _semantic_concept,
)
from .text_value_service import _compute_fingerprint

RECEIPTS = "task_project_transfer_receipts"


def concept_digest(document: dict | None) -> str | None:
    return digest(portable(_semantic_concept(document))) if document else None


def destination_text_digest(database, ids, *, session=None):
    relations = list(
        database.text_relations.find(
            {"subject_concept_id": {"$in": ids}}, session=session
        )
    )
    value_ids = {r["object_text_id"] for r in relations}
    value_ids.update(
        ObjectId(str(v)) for v in list(value_ids) if ObjectId.is_valid(str(v))
    )
    values = list(
        database.text_values.find({"_id": {"$in": list(value_ids)}}, session=session)
    )
    return digest(
        portable(
            {
                "relations": sorted(relations, key=lambda r: str(r["_id"])),
                "values": sorted(values, key=lambda r: str(r["_id"])),
            }
        )
    )


def check_identity_and_retention(existing: dict, incoming: dict) -> None:
    """A shared slug alone is never authority to replace an existing object."""
    old, new = existing.get("metadata", {}), incoming.get("metadata", {})
    old_jira = old.get("external_references", {}).get("jira", {})
    new_jira = new.get("external_references", {}).get("jira", {})
    if old_jira.get("issue_id") and old_jira.get("issue_id") == new_jira.get(
        "issue_id"
    ):
        for field in ("task_history", "comments", "worklog", "attachments"):
            incoming_entries = {digest(portable(row)) for row in new.get(field, [])}
            if any(
                digest(portable(row)) not in incoming_entries
                for row in old.get(field, [])
            ):
                raise ValueError(
                    "Destination task history is not retained by the source"
                )
        return
    a, b = existing.get("attributes", {}), incoming.get("attributes", {})
    if a.get("sha256") and a.get("sha256") == b.get("sha256"):
        return
    for field, keys in (
        ("task_project", ("source_site", "source_id", "organisation_concept_id")),
        ("task_collection", ("project_concept_id", "source_id")),
    ):
        x, y = a.get(field), b.get(field)
        if x and y and all(x.get(k) == y.get(k) for k in keys):
            return
    if concept_digest(existing) == concept_digest(incoming):
        return
    raise ValueError("Unresolved destination concept identity collision")


def verify_staged_files(payload: dict, staged: dict, blob_store) -> dict:
    """Read back actual destination bytes before opening a database transaction."""
    files = decoded(payload["body"])["files"]
    if set(staged) != {r["concept_id"] for r in files}:
        raise ValueError("Staged file inventory differs from the snapshot")
    result = {}
    for document in files:
        cid = document["concept_id"]
        expected = document.get("attributes", {})
        record = staged[cid]
        data = blob_store.get_bytes(record["blob_key"])
        if len(data) != expected.get("size_bytes") or hashlib.sha256(
            data
        ).hexdigest() != expected.get("sha256"):
            raise ValueError("Staged task file bytes do not match the source")
        result[cid] = {k: record[k] for k in ("blob_key", "blob_uri", "blob_backend")}
    return result


def publish_project_snapshot(
    envelope: dict,
    *,
    config: dict,
    origin: str,
    expected_destination_digests: dict,
    expected_destination_text_digest: str,
    staged_files: dict,
    blob_store,
    database=None,
    _after_concepts=None,
) -> dict:
    """Publish data and a prepared home, preserving a prior view on interruption.

    The private callback is a fault-injection seam for transaction acceptance.
    It is not exposed through a tool or HTTP route. Replaying a committed
    snapshot returns its receipt without replacing later accepted writes.
    """
    payload = verify_snapshot(envelope, config=config, origin=origin)
    actor = get_effective_user_concept_id()
    if not actor or actor != payload["actor"]:
        raise PermissionError(
            "Task transfer actor does not match the admitted snapshot"
        )
    project_id = payload["project_id"]
    home = decoded(payload.get("source_home"))
    if not home or home.get("state") != "frozen" or home.get("home_node_id") != origin:
        raise ValueError("A quiescent frozen source home is required")
    database = database if database is not None else get_db()
    if database is None or not database.client.admin.command("hello").get("setName"):
        raise RuntimeError("Task project publication requires MongoDB transactions")
    receipt_id = digest([origin, project_id, payload["digest"], home["epoch"]])
    prior = database[RECEIPTS].find_one({"_id": receipt_id})
    if prior:
        return prior
    file_locations = verify_staged_files(payload, staged_files, blob_store)
    body = decoded(payload["body"])
    documents = body["concepts"] + body["files"]
    ids = [r["concept_id"] for r in documents]
    if set(expected_destination_digests) != set(ids):
        raise ValueError("Exact destination preflight inventory is required")
    now = datetime.now(UTC)

    def commit(session):
        prior = database[RECEIPTS].find_one({"_id": receipt_id}, session=session)
        if prior:
            return prior
        if database.task_project_homes.find_one({"_id": project_id}, session=session):
            raise ValueError(
                "Destination project already has a home; reconcile instead of replaying"
            )
        if (
            destination_text_digest(database, ids, session=session)
            != expected_destination_text_digest
        ):
            raise ValueError("Destination text changed after transfer preflight")
        existing = {
            r["concept_id"]: r
            for r in database.concepts.find(
                {"concept_id": {"$in": ids}}, session=session
            )
        }
        replacements = []
        for source in documents:
            cid = source["concept_id"]
            previous = existing.get(cid)
            if concept_digest(previous) != expected_destination_digests[cid]:
                raise ValueError("Destination changed after transfer preflight")
            if previous:
                check_identity_and_retention(previous, source)
            document = copy.deepcopy(source)
            if previous:
                document["_id"] = previous["_id"]
                if previous.get("guid"):
                    document["guid"] = previous["guid"]
            document["embedding_status"] = "pending"
            document.setdefault("metadata", {})["federation"] = {
                "origin": origin,
                "source_concept_id": cid,
                "source_project_id": project_id,
                "snapshot_digest": payload["digest"],
                "transferred_at": now,
                "dispatch_requires_current_actor": True,
            }
            if cid in file_locations:
                document.setdefault("attributes", {}).update(file_locations[cid])
                document["attributes"]["file_copy_metadata_storage"] = "attributes.v1"
            replacements.append(ReplaceOne({"concept_id": cid}, document, upsert=True))
        if replacements:
            database.concepts.bulk_write(replacements, session=session)
        task_ids = [
            document["concept_id"]
            for document in body["concepts"]
            if (document.get("metadata") or {}).get("project_concept_id") == project_id
        ]
        if database.task_dispatch_authorities.find_one(
            {"_id": {"$in": task_ids}, "actor_concept_id": {"$ne": None}},
            session=session,
        ):
            raise ValueError(
                "Destination task has a current assignment; reconcile before transfer"
            )
        if task_ids:
            database.task_dispatch_authorities.bulk_write(
                [
                    UpdateOne(
                        {"_id": cid},
                        {
                            "$set": {
                                "actor_concept_id": None,
                                "revoked": True,
                                "imported_snapshot_digest": payload["digest"],
                                "project_concept_id": project_id,
                            }
                        },
                        upsert=True,
                    )
                    for cid in task_ids
                ],
                session=session,
            )
        if _after_concepts:
            _after_concepts(database, session)

        # Exact text identity preserves bytes even where older normalised
        # fingerprints had collapsed whitespace/case variants.
        wanted = {}
        for value in body["text_values"]:
            fingerprint = _compute_fingerprint(
                value["text"], value.get("lang", "en"), identity_mode="exact"
            )
            wanted[str(value["_id"])] = (fingerprint, value)
        by_fingerprint = {
            r["fingerprint"]: r
            for r in database.text_values.find(
                {"fingerprint": {"$in": sorted({fp for fp, _ in wanted.values()})}},
                session=session,
            )
        }
        inserts, value_map = [], {}
        for source_id, (fingerprint, source) in wanted.items():
            value = by_fingerprint.get(fingerprint)
            if value is None:
                value = copy.deepcopy(source)
                value["_id"] = ObjectId()
                value["fingerprint"] = fingerprint
                by_fingerprint[fingerprint] = value
                inserts.append(InsertOne(value))
            if value["text"] != source["text"] or value.get("lang", "en") != source.get(
                "lang", "en"
            ):
                raise ValueError("Destination exact text identity collision")
            value_map[source_id] = value["_id"]
        if inserts:
            database.text_values.bulk_write(inserts, session=session)
        database.text_relations.delete_many(
            {"subject_concept_id": {"$in": ids}}, session=session
        )
        relations = []
        seen = set()
        for original in body["text_relations"]:
            relation = copy.deepcopy(original)
            source_relation_id = str(relation.pop("_id"))
            source_value_id = str(relation["object_text_id"])
            relation["object_text_id"] = value_map[str(relation["object_text_id"])]
            semantic_key = (
                relation["subject_concept_id"],
                relation["predicate"],
                str(relation["object_text_id"]),
            )
            if semantic_key in seen:
                raise ValueError("Source text relations collapse under exact identity")
            seen.add(semantic_key)
            relation["context"] = dict(relation.get("context") or {})
            relation.setdefault("context", {})[
                "federation_source_relation_id"
            ] = source_relation_id
            relation["context"]["federation_source_text_value"] = {
                k: v
                for k, v in wanted[source_value_id][1].items()
                if k not in {"text", "lang", "fingerprint"}
            }
            relation["_id"] = ObjectId()
            relations.append(InsertOne(relation))
        if relations:
            database.text_relations.bulk_write(relations, session=session)
        record = next(r for r in body["concepts"] if r["concept_id"] == project_id)[
            "attributes"
        ]["task_project"]
        prepared = {
            "_id": project_id,
            "schema_version": "task_project_home.v1",
            "home_node_id": config["node_id"],
            "home_url": config["home_url"],
            "epoch": home["epoch"] + 1,
            "state": "prepared",
            "operations": {},
            "source_identity": home["source_identity"],
            "snapshot_digest": payload["digest"],
            "updated_at": now,
            "source_captured_at": payload["captured_at"],
        }
        database.task_project_homes.insert_one(prepared, session=session)
        receipt = {
            "_id": receipt_id,
            "schema_version": "task_project_publication.v1",
            "project_id": project_id,
            "origin": origin,
            "recipient": config["node_id"],
            "snapshot_digest": payload["digest"],
            "source_epoch": home["epoch"],
            "home_epoch": prepared["epoch"],
            "state": "prepared",
            "concept_count": len(body["concepts"]),
            "file_count": len(body["files"]),
            "text_relation_count": len(relations),
            "published_at": now,
            "canonical_readback": True,
            "dispatch_state_copied": False,
        }
        # Read within the transaction to distinguish intended from persisted data.
        if database.concepts.count_documents(
            {"concept_id": {"$in": ids}}, session=session
        ) != len(ids):
            raise RuntimeError("Published task-project inventory is incomplete")
        if database.text_relations.count_documents(
            {"subject_concept_id": {"$in": ids}}, session=session
        ) != len(relations):
            raise RuntimeError("Published task-project text inventory is incomplete")
        database[RECEIPTS].insert_one(receipt, session=session)
        return receipt

    with database.client.start_session() as session:
        receipt = session.with_transaction(
            commit,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
        )
    persisted = database[RECEIPTS].find_one({"_id": receipt_id})
    if not persisted:
        raise RuntimeError("Committed task-project receipt is not readable")
    return persisted


def verify_project_publication(envelope, *, config, origin, database=None):
    """Compare the complete admitted semantic slice with canonical storage.

    Local GUIDs on proven collisions, exact-text storage identities, blob routes
    and local federation receipts may differ. Source text-value provenance is
    retained on each relation when exact text reuses an existing local value.
    """
    payload = verify_snapshot(envelope, config=config, origin=origin)
    if get_effective_user_concept_id() != payload["actor"]:
        raise PermissionError("Task publication verification actor mismatch")
    database = database if database is not None else get_db()
    body = decoded(payload["body"])
    documents = body["concepts"] + body["files"]
    ids = [r["concept_id"] for r in documents]
    existing = {
        r["concept_id"]: r for r in database.concepts.find({"concept_id": {"$in": ids}})
    }
    file_ids = {r["concept_id"] for r in body["files"]}
    for document in documents:
        cid = document["concept_id"]
        if cid not in existing:
            raise ValueError("Published task concept is missing")
        source, target = _semantic_concept(document), _semantic_concept(existing[cid])
        if "metadata" not in source and not target.get("metadata"):
            target.pop("metadata", None)
        for value in (source, target):
            value.pop("guid", None)
            if cid in file_ids:
                for field in (
                    "blob_backend",
                    "blob_key",
                    "blob_uri",
                    "file_copy_metadata_storage",
                ):
                    value.get("attributes", {}).pop(field, None)
        if digest(portable(source)) != digest(portable(target)):
            raise ValueError(f"Published task concept differs: {cid}")
    relations = list(database.text_relations.find({"subject_concept_id": {"$in": ids}}))
    source_values = {str(v["_id"]): v for v in body["text_values"]}
    target_values = {
        str(v["_id"]): v
        for v in database.text_values.find(
            {"_id": {"$in": [r["object_text_id"] for r in relations]}}
        )
    }
    by_source = {
        r.get("context", {}).get("federation_source_relation_id"): r for r in relations
    }
    if len(relations) != len(body["text_relations"]) or len(by_source) != len(
        relations
    ):
        raise ValueError("Published text relation inventory differs")
    for relation in body["text_relations"]:
        source = copy.deepcopy(relation)
        target = copy.deepcopy(by_source.get(str(source.pop("_id"))))
        if target is None:
            raise ValueError("Published source text relation is missing")
        target.pop("_id")
        source_value = source_values[str(source.pop("object_text_id"))]
        target_value = target_values[str(target.pop("object_text_id"))]
        context = target["context"]
        context.pop("federation_source_relation_id")
        provenance = context.pop("federation_source_text_value")
        if "context" not in source and not context:
            target.pop("context")
        expected_provenance = {
            k: v
            for k, v in source_value.items()
            if k not in {"text", "lang", "fingerprint"}
        }
        if (
            digest(portable(source)) != digest(portable(target))
            or provenance != expected_provenance
            or any(
                source_value.get(k, "en" if k == "lang" else None)
                != target_value.get(k, "en" if k == "lang" else None)
                for k in ("text", "lang")
            )
        ):
            raise ValueError("Published task text or provenance differs")
    return {
        "project_id": payload["project_id"],
        "snapshot_digest": payload["digest"],
        "concepts_verified": len(documents),
        "text_relations_verified": len(relations),
        "canonical_readback": True,
    }
