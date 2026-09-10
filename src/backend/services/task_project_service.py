"""Project and collection references for the canonical task store.

These are represented identities with small inspectable reference records, not
a parallel task store. Membership never changes a task's audience. Source
configuration stays in file-copy archives rather than being copied to tasks.
"""

from __future__ import annotations

import hashlib
from datetime import UTC
from typing import Any
from urllib.parse import urlsplit

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import can_access_concept
from .concept_service import (
    ConceptNotFoundError,
    create_concept,
    get_concept_by_concept_id_exact,
    update_concept,
)

PROJECT_RECORD = "task_project"
COLLECTION_RECORD = "task_collection"


def _find_concept(concept_id):
    try:
        return get_concept_by_concept_id_exact(concept_id)
    except ConceptNotFoundError:
        return None


def _record(concept_id, key):
    if not can_access_concept(concept_id):
        raise ValueError("Project or collection is not accessible")
    doc = _find_concept(concept_id)
    record = (doc.get("attributes") or {}).get(key) if doc else None
    if not isinstance(record, dict):
        raise ValueError("Project or collection not found")  # noqa: TRY004
    return {"concept_id": concept_id, **record}


def get_task_project(project_concept_id):
    return _record(project_concept_id, PROJECT_RECORD)


def get_task_collection(collection_concept_id):
    return _record(collection_concept_id, COLLECTION_RECORD)


def list_project_collections(project):
    """A shared project can reference views with a narrower audience."""
    return [
        get_task_collection(value)
        for value in project["collection_concept_ids"]
        if can_access_concept(value)
    ]


def list_task_projects(*, limit=100, offset=0):
    query = {f"attributes.{PROJECT_RECORD}": {"$exists": True}}
    docs = ConceptsRepository.find(
        query,
        projection={"concept_id": 1, "attributes.task_project": 1},
        limit=max(1, min(int(limit), 200)),
        skip=max(0, int(offset)),
    )
    return [
        {"concept_id": doc["concept_id"], **doc["attributes"][PROJECT_RECORD]}
        for doc in docs
    ]


def ensure_jira_project(
    project,
    *,
    actor_concept_id,
    archive_reference=None,
    organisation_concept_id=None,
    existing_project_concept_id=None,
):
    """Reuse a source-identity binding; deterministic IDs recover interrupted creates.

    A caller may associate a previously represented domain project explicitly.
    Its broader publication context is never used for private source metadata.
    """
    if not actor_concept_id or not project.get("id") or not project.get("key"):
        raise ValueError("Actor and Jira project identity are required")
    site = urlsplit(project.get("self", "")).netloc.lower()
    if not site:
        raise ValueError("Jira project site identity is required")
    identity = hashlib.sha256(
        f"{site}\n{project['id']}\n{actor_concept_id}\n{organisation_concept_id}".encode()
    ).hexdigest()[:24]
    project_id = f"#V#jira_project_{identity}"
    collection_id = f"#V#jira_project_tasks_{identity}"
    prior = _find_concept(project_id)
    old = ((prior or {}).get("attributes") or {}).get(PROJECT_RECORD, {})
    if existing_project_concept_id and not can_access_concept(
        existing_project_concept_id
    ):
        raise ValueError("Associated domain project is not accessible")
    record: dict[str, Any] = {
        **old,
        "name": project.get("name", project["key"]),
        "key": project["key"],
        "source_system": "jira",
        "source_site": site,
        "source_id": str(project["id"]),
        "organisation_concept_id": organisation_concept_id,
        "source_url": project.get("self"),
        "description": project.get("description", old.get("description", "")),
        "collection_concept_ids": sorted(
            set(old.get("collection_concept_ids", [])) | {collection_id}
        ),
        "writer": old.get("writer", "jira"),
        "cutover_state": old.get("cutover_state", "importing"),
    }
    if archive_reference:
        previous = old.get("source_archive")
        if previous and previous.get("content_sha256") != archive_reference.get(
            "content_sha256"
        ):
            history = list(old.get("source_archive_history") or [])
            if not any(
                item.get("content_sha256") == previous.get("content_sha256")
                for item in history
            ):
                history.append(previous)
            record["source_archive_history"] = history
        record["source_archive"] = archive_reference
    if existing_project_concept_id:
        record["domain_project_concept_id"] = existing_project_concept_id
    if prior:
        # Issue responses carry a partial project object. Refresh only fields
        # this source supplies, never a read/modify/write copy of the whole
        # record: a concurrent archive publication or writer decision must not
        # be replaced by the importer's older snapshot.
        fields = {
            "name",
            "key",
            "source_system",
            "source_site",
            "source_id",
            "organisation_concept_id",
            "source_url",
        }
        if "description" in project:
            fields.add("description")
        if collection_id not in old.get("collection_concept_ids", []):
            fields.add("collection_concept_ids")
        if archive_reference:
            fields.add("source_archive")
            if "source_archive_history" in record:
                fields.add("source_archive_history")
        if existing_project_concept_id:
            fields.add("domain_project_concept_id")
        updates = {
            f"attributes.{PROJECT_RECORD}.{field}": record[field]
            for field in fields
            if old.get(field) != record[field]
        }
        if updates:
            update_concept(project_id, updates)
    else:
        create_concept(
            name=record["name"],
            concept_id=project_id,
            parent_concept_ids=["#V#project"],
            create_as_instance=True,
            attributes={PROJECT_RECORD: record},
            created_by_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            maintain_relationship_inverses=False,
            visibility_scope_mode=(
                "user_only_default"
                if not organisation_concept_id
                else "user_org_default"
            ),
        )
    collection = _find_concept(collection_id)
    if not collection:
        create_concept(
            name=f"{record['name']} tasks",
            concept_id=collection_id,
            parent_concept_ids=["#V#first_order_collection"],
            create_as_instance=True,
            attributes={
                COLLECTION_RECORD: {
                    "name": f"{record['name']} tasks",
                    "project_concept_ids": [project_id],
                    "selection": {
                        "kind": "project_membership",
                        "project_concept_id": project_id,
                    },
                    "source_project_key": project["key"],
                    "hidden_by_default": False,
                }
            },
            created_by_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            maintain_relationship_inverses=False,
            visibility_scope_mode=(
                "user_only_default"
                if not organisation_concept_id
                else "user_org_default"
            ),
        )
    # Read both ends before publishing a usable context to the importer.
    saved_project, saved_collection = get_task_project(project_id), get_task_collection(
        collection_id
    )
    if project_id not in saved_collection["project_concept_ids"]:
        raise ValueError("Project collection association mismatch")
    return {
        "project_concept_id": project_id,
        "collection_concept_ids": [collection_id],
        "project": saved_project,
        "collection": saved_collection,
    }


def validate_task_membership(project_concept_id, collection_concept_ids):
    project = get_task_project(project_concept_id) if project_concept_id else None
    collections = list(dict.fromkeys(collection_concept_ids or []))
    for collection_id in collections:
        collection = get_task_collection(collection_id)
        if project and project_concept_id not in collection["project_concept_ids"]:
            raise ValueError("Collection does not cover the selected project")
    return project, collections


def find_jira_task_project(
    *, project_key, source_site=None, organisation_concept_id=None
):
    """Resolve only an accessible source binding, including for legacy sync jobs."""
    query = {
        f"attributes.{PROJECT_RECORD}.key": project_key,
        f"attributes.{PROJECT_RECORD}.source_system": "jira",
    }
    if source_site:
        query[f"attributes.{PROJECT_RECORD}.source_site"] = source_site
    if organisation_concept_id:
        query[f"attributes.{PROJECT_RECORD}.organisation_concept_id"] = (
            organisation_concept_id
        )
    docs = list(
        ConceptsRepository.find(
            query,
            projection={"concept_id": 1, f"attributes.{PROJECT_RECORD}": 1},
            limit=2,
        )
    )
    if len(docs) > 1:
        raise ValueError("Ambiguous Jira project binding")
    return (
        {"concept_id": docs[0]["concept_id"], **docs[0]["attributes"][PROJECT_RECORD]}
        if docs
        else None
    )


def associate_jira_source_collection(
    *,
    name,
    source_id,
    source_kind,
    project_concept_ids,
    issue_keys,
    archive_reference,
    actor_concept_id,
):
    """Keep source board/filter membership as a snapshot, with native additions.

    The source query/configuration stays in its archive. It is never executed as
    policy in Von. Explicit membership is refreshed only while Jira is writer.
    """
    projects = [get_task_project(value) for value in dict.fromkeys(project_concept_ids)]
    if not projects:
        raise ValueError("A source collection must cover at least one project")
    site = projects[0]["source_site"]
    if any(p["source_site"] != site for p in projects):
        raise ValueError("Source collection projects must belong to one Jira site")
    identity = hashlib.sha256(
        f"{actor_concept_id}:{site}:{source_kind}:{source_id}".encode()
    ).hexdigest()[:24]
    collection_id = f"#V#jira_task_collection_{identity}"
    previous = _find_concept(collection_id)
    old = (previous or {}).get("attributes", {}).get(COLLECTION_RECORD, {})
    record = {
        **old,
        "name": name,
        "source_id": str(source_id),
        "source_kind": source_kind,
        "project_concept_ids": sorted(p["concept_id"] for p in projects),
        "selection": {
            "kind": "source_issue_keys",
            "issue_keys": sorted(set(issue_keys)),
        },
        "source_archive": archive_reference,
        "hidden_by_default": False,
    }
    if previous and any(p.get("writer") == "von" for p in projects):
        return get_task_collection(collection_id)
    if previous:
        update_concept(collection_id, {f"attributes.{COLLECTION_RECORD}": record})
    else:
        create_concept(
            name=name,
            concept_id=collection_id,
            parent_concept_ids=["#V#first_order_collection"],
            create_as_instance=True,
            attributes={COLLECTION_RECORD: record},
            created_by_concept_id=actor_concept_id,
            maintain_relationship_inverses=False,
            visibility_scope_mode="user_only_default",
        )
    for project in projects:
        ids = sorted(set(project["collection_concept_ids"]) | {collection_id})
        update_concept(
            project["concept_id"],
            {f"attributes.{PROJECT_RECORD}.collection_concept_ids": ids},
        )
    return get_task_collection(collection_id)


def collection_task_query(collection_concept_id):
    collection = get_task_collection(collection_concept_id)
    selection = collection.get("selection", {})
    clauses = [{"metadata.collection_concept_ids": collection_concept_id}]
    if selection.get("kind") == "source_issue_keys":
        clauses.append(
            {
                "metadata.external_references.jira.external_id": {
                    "$in": selection.get("issue_keys", [])
                }
            }
        )
    elif selection.get("kind") == "project_membership":
        clauses.append({"metadata.project_concept_id": selection["project_concept_id"]})
    return {"$or": clauses}


def set_task_project_writer(project_concept_id, *, writer, actor_concept_id, evidence):
    """Record a bounded cutover or reversible pause with its reviewable receipts."""
    from datetime import datetime

    project = get_task_project(project_concept_id)
    if writer not in {"jira", "von"} or not actor_concept_id:
        raise ValueError("Writer and actor are required")
    if writer == "von":
        reconciliation = evidence.get("reconciliation", {})
        if reconciliation.get(
            "project_concept_id"
        ) != project_concept_id or not reconciliation.get("retention_reconciled"):
            raise ValueError("A successful project content reconciliation is required")
        if any(
            not evidence.get(key)
            for key in (
                "site_retention_receipt",
                "ordinary_use_receipt",
                "restore_receipt",
                "final_delta_receipt",
            )
        ):
            raise ValueError(
                "Site retention, ordinary-use, restore and final-delta receipts are required"
            )
    history = list(project.get("writer_history") or [])
    history.append(
        {
            "from": project.get("writer", "jira"),
            "to": writer,
            "at": datetime.now(UTC).isoformat(),
            "actor_concept_id": actor_concept_id,
            "evidence": evidence,
        }
    )
    update_concept(
        project_concept_id,
        {
            f"attributes.{PROJECT_RECORD}.writer": writer,
            f"attributes.{PROJECT_RECORD}.cutover_state": (
                "von_authoritative" if writer == "von" else "importing"
            ),
            f"attributes.{PROJECT_RECORD}.writer_history": history,
        },
    )
    saved = get_task_project(project_concept_id)
    if saved["writer"] != writer:
        raise ValueError("Project writer read-back failed")
    return saved
