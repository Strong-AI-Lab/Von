"""Bounded, actor-scoped task-project snapshots for an explicit home transfer.

Snapshots are data, never execution requests. Their file manifests refer to
separately verified bytes. Publication must be one database transaction; a
standalone MongoDB server cannot safely activate this adapter.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from bson import json_util

from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import (
    can_access_concept,
    filter_accessible_concept_ids,
    get_effective_user_concept_id,
)
from ..security.visibility_predicates import (
    get_specific_to_org_values,
    get_specific_to_user_values,
)
from .knowledge_federation.protocol import digest, encode, key, peer_config, sign
from .task_project_home_service import get_project_home

VERSION = "task_project_snapshot.v1"
MAX_CONCEPTS = 20000
MAX_BYTES = 256 * 1024 * 1024
# Dispatch, supervision and execution state must be established at the new home.
EXECUTION_METADATA = {
    "dispatch_requested",
    "coding_supervision",
    "coding_dispatch",
    "dispatch_authority",
    "task_execution",
    "execution_lease",
    "execution_state",
}
CONCEPT_FIELDS = {
    "concept_id",
    "guid",
    "created_at",
    "updated_at",
    "relationships",
    "metadata",
    "attributes",
    "system_tags",
    "user_tags",
}


def portable(value: Any) -> Any:
    """Preserve BSON dates and identifiers without local storage identities."""
    import json

    return json.loads(json_util.dumps(value))


def decoded(value: Any) -> Any:
    import json

    return json_util.loads(json.dumps(value))


def _scope(document: dict) -> dict:
    relationships = document.get("relationships") or {}
    return {
        "users": sorted(get_specific_to_user_values(relationships)),
        "organisations": sorted(get_specific_to_org_values(relationships)),
    }


def _semantic_concept(document: dict) -> dict:
    result = copy.deepcopy({k: v for k, v in document.items() if k in CONCEPT_FIELDS})
    metadata = result.get("metadata", {})
    for field in EXECUTION_METADATA:
        metadata.pop(field, None)
    external = metadata.get("external_references", {})
    for field in EXECUTION_METADATA:
        external.pop(field, None)
    # Operational federation/dispatch receipts are local; preserve their source
    # digest in the signed manifest rather than accepting them as authority.
    metadata.pop("federation", None)
    return result


def referenced_file_ids(value: Any) -> set[str]:
    ids = set()
    if isinstance(value, dict):
        for field, item in value.items():
            if (
                field in {"file_copy_concept_id", "file_concept_id"}
                and isinstance(item, str)
                and item.startswith("#V#")
            ):
                ids.add(item)
            else:
                ids.update(referenced_file_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(referenced_file_ids(item))
    return ids


def capture_project(project_id: str, *, config: dict, recipient: str) -> dict:
    """Read one admitted project through the current authenticated actor.

    The raw repository read follows exact actor admission so historical
    relationships to absent participants are retained as provenance. It does
    not import their accounts, memberships, prompts or execution state.
    """
    actor = get_effective_user_concept_id()
    settings = peer_config(config, "exports", recipient)
    if not actor or project_id not in settings.get("task_projects", []):
        raise PermissionError("Task project export is not admitted")
    if not can_access_concept(project_id):
        raise PermissionError("Task project is inaccessible")
    initial_home = get_project_home(project_id)
    collection = ConceptsRepository.collection()
    project = collection.find_one({"concept_id": project_id})
    record = ((project or {}).get("attributes") or {}).get("task_project")
    if not record or record.get("writer") != "von":
        raise ValueError("A native-writer task project is required")
    collection_ids = record.get("collection_concept_ids", [])
    query = {
        "$or": [
            {"concept_id": {"$in": [project_id, *collection_ids]}},
            {"metadata.project_concept_id": project_id},
        ]
    }
    documents = list(collection.find(query).limit(MAX_CONCEPTS + 1))
    if len(documents) > MAX_CONCEPTS:
        raise ValueError("Task project exceeds the bounded concept inventory")
    ids = {d["concept_id"] for d in documents}
    if not set(collection_ids).issubset(ids):
        raise ValueError("Project collection inventory is incomplete")
    audiences = set(settings.get("audiences", []))
    if filter_accessible_concept_ids(ids) != ids:
        raise PermissionError("Project contains an inaccessible record")
    for document in documents:
        scope = _scope(document)
        keys = {f"user:{v}" for v in scope["users"]} | {
            f"org:{v}" for v in scope["organisations"]
        }
        if not keys or not keys.intersection(audiences):
            raise PermissionError("Task record has no admitted private audience")
    semantic = [_semantic_concept(d) for d in documents]
    file_ids = referenced_file_ids(semantic)
    if len(file_ids) > MAX_CONCEPTS:
        raise ValueError("Task project exceeds the bounded file inventory")
    if filter_accessible_concept_ids(file_ids) != file_ids:
        raise PermissionError("A task source file is inaccessible")
    file_documents = list(collection.find({"concept_id": {"$in": sorted(file_ids)}}))
    if {d["concept_id"] for d in file_documents} != file_ids:
        raise ValueError("Missing task source file")
    files = sorted(
        (_semantic_concept(d) for d in file_documents), key=lambda d: d["concept_id"]
    )
    relations = list(
        TextRelationsRepository.collection().find(
            {
                "subject_concept_id": {
                    "$in": sorted(ids | {d["concept_id"] for d in files})
                }
            }
        )
    )
    text_ids = {r["object_text_id"] for r in relations}
    # Historic stores may use a string representation of a BSON ObjectId.
    from bson import ObjectId

    query_ids = set(text_ids)
    for value in text_ids:
        if ObjectId.is_valid(str(value)):
            query_ids.add(ObjectId(str(value)))
    texts = list(
        TextValuesRepository.collection().find({"_id": {"$in": list(query_ids)}})
    )
    if {str(v["_id"]) for v in texts} != {str(v) for v in text_ids}:
        raise ValueError("Dangling task text reference in source snapshot")
    body = portable(
        {
            "concepts": sorted(semantic, key=lambda d: d["concept_id"]),
            "text_relations": sorted(relations, key=lambda r: str(r["_id"])),
            "text_values": sorted(texts, key=lambda r: str(r["_id"])),
            "files": files,
        }
    )
    if len(encode(body)) > MAX_BYTES:
        raise ValueError("Task project exceeds the bounded snapshot size")
    home = get_project_home(project_id)
    if home != initial_home:
        raise ValueError("Project home changed during snapshot capture; retry")
    payload = {
        "schema_version": VERSION,
        "origin": config["node_id"],
        "recipient": recipient,
        "project_id": project_id,
        "actor": actor,
        "captured_at": datetime.now(UTC).isoformat(),
        "source_home": portable(home),
        "digest": digest(body),
        "body": body,
    }
    return sign(payload, key(settings))


def verify_snapshot(envelope: dict, *, config: dict, origin: str) -> dict:
    """Authenticate an admitted immutable snapshot, before any staging effects."""
    import hashlib
    import hmac

    settings = peer_config(config, "imports", origin)
    payload = envelope["payload"]
    expected = hmac.new(key(settings), encode(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, envelope.get("signature", "")):
        raise PermissionError("Invalid task snapshot signature")
    if (
        payload.get("schema_version") != VERSION
        or payload.get("origin") != origin
        or payload.get("recipient") != config.get("node_id")
    ):
        raise PermissionError("Incompatible or misdirected task snapshot")
    if payload.get("project_id") not in settings.get("task_projects", []):
        raise PermissionError("Task project import is not admitted")
    body = payload["body"]
    if len(encode(body)) > MAX_BYTES or digest(body) != payload.get("digest"):
        raise ValueError("Invalid task snapshot bytes or digest")
    if set(body) != {"concepts", "files", "text_values", "text_relations"}:
        raise ValueError("Unsupported task snapshot section")
    records = body["concepts"]
    project_id = payload["project_id"]
    projects = [r for r in records if r["concept_id"] == project_id]
    if len(projects) != 1:
        raise ValueError("Missing task project record")
    project = projects[0].get("attributes", {}).get("task_project", {})
    if project.get("writer") != "von":
        raise ValueError("Task project writer is not Von")
    collection_ids = set(project.get("collection_concept_ids", []))
    for record in records:
        cid = record["concept_id"]
        if cid == project_id:
            continue
        if cid in collection_ids:
            if not record.get("attributes", {}).get("task_collection"):
                raise ValueError("Invalid task collection record")
        elif record.get("metadata", {}).get("project_concept_id") != project_id:
            raise ValueError("Unrelated concept in task project snapshot")
    subject_ids = {r["concept_id"] for r in [*records, *body["files"]]}
    if (
        len(subject_ids) != len(records) + len(body["files"])
        or len(body["files"]) > MAX_CONCEPTS
    ):
        raise ValueError("Duplicate or excessive snapshot concept identifiers")
    if not collection_ids.issubset({r["concept_id"] for r in records}):
        raise ValueError("Incomplete task collection inventory")
    values = decoded(body["text_values"])
    value_ids = {str(r["_id"]) for r in values}
    relations = decoded(body["text_relations"])
    if len(value_ids) != len(values) or value_ids != {
        str(r["object_text_id"]) for r in relations
    }:
        raise ValueError("Incomplete or duplicate task text value inventory")
    if len({str(r["_id"]) for r in relations}) != len(relations):
        raise ValueError("Duplicate task text relation identifiers")
    if any(r["subject_concept_id"] not in subject_ids for r in body["text_relations"]):
        raise ValueError("Unrelated task text relation")
    if {r["concept_id"] for r in body["files"]} != referenced_file_ids(records):
        raise ValueError("Task file inventory differs from its references")
    if len(records) > MAX_CONCEPTS or len({r["concept_id"] for r in records}) != len(
        records
    ):
        raise ValueError("Invalid task snapshot concept inventory")
    # Identity mappings are checked explicitly, independently of subscriptions.
    # A source username/organisation slug cannot create local account authority.
    mappings = settings.get("identity_map", {})
    for record in [*records, *body["files"]]:
        scope = _scope(record)
        audience_keys = {f"user:{v}" for v in scope["users"]} | {
            f"org:{v}" for v in scope["organisations"]
        }
        if not audience_keys.intersection(settings.get("audiences", [])):
            raise PermissionError("Task snapshot audience is outside the subscription")
        for value in scope["users"] + scope["organisations"]:
            if mappings.get(value) != value:
                raise PermissionError("Unverified task audience identity mapping")
        semantic = portable(_semantic_concept(decoded(record)))
        if semantic != record:
            raise ValueError(
                "Snapshot contains execution or unsupported concept fields"
            )
    return payload
