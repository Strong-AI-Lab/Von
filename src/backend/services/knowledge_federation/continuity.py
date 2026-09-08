"""Portable discovery and on-demand content, without cloning editable carriers.

Only admitted native sources are exported. Cached evidence cannot become a new
source, an identity binding, or an execution continuation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from pathlib import PurePosixPath

from .protocol import MAX_RECORDS, digest, encode, key, now, peer_config, sign
from .source import portable

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_CONTENT_BYTES = MAX_FILE_BYTES * 4 // 3 + 65536
PAGE_CHARS = 60000
CATALOGUE_FIELDS = {
    "status",
    "provenance",
    "title",
    "source_id",
    "user_id",
    "namespace",
    "updated_at",
    "preview",
    "sha256",
    "size_bytes",
    "content_type",
    "original_filename",
    "availability",
    "audience",
    "source_links",
}


def subscriptions(settings, field):
    values = settings.get(field, [])
    if not isinstance(values, list) or any(
        not isinstance(a, str) or a == "public" or a not in settings["audiences"]
        for a in values
    ):
        raise PermissionError("continuity requires explicit private subscriptions")
    return values


def file_audience(document, settings):
    """Narrow to a single explicitly configured native visibility target.

    Canonical file visibility allows either an explicit user or org target.
    Prefer the user target, so a personal file is not widened to the whole org.
    Do not export ambiguous legacy attributes or unscoped/public files by default.
    """
    from ...security.visibility_predicates import (
        SPECIFIC_TO_ORG_PREDICATES_READ,
        SPECIFIC_TO_USER_PREDICATES,
    )

    attrs = document.get("attributes") or {}
    if attrs.get("source_system") == "von_federation" or attrs.get(
        "conversation_image"
    ):
        return None
    relationships = document.get("relationships") or {}
    admitted = subscriptions(settings, "file_audiences")
    for prefix, predicates in (
        ("user", SPECIFIC_TO_USER_PREDICATES),
        ("org", SPECIFIC_TO_ORG_PREDICATES_READ),
    ):
        values = set()
        for predicate in predicates:
            raw = relationships.get(predicate, [])
            if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
                return None
            values.update(raw)
        if values:
            if len(values) != 1:
                return None
            audience = prefix + ":" + next(iter(values))
            return audience if audience in admitted else None
    return None


def file_record(document, settings):
    audience = file_audience(document, settings)
    if not audience:
        return None
    attrs = document.get("attributes") or {}
    cid = document["concept_id"]
    sha = attrs.get("sha256")
    size = attrs.get("size_bytes")
    supported = (
        isinstance(sha, str)
        and len(sha) == 64
        and isinstance(size, int)
        and 0 < size <= MAX_FILE_BYTES
    )
    filename = (
        attrs.get("original_filename")
        or document.get("name")
        or attrs.get("arxiv_id")
        or cid
    )
    if not isinstance(filename, str):
        filename = "artefact"
    claim = {
        "status": "asserted",
        "audience": audience,
        "source_id": cid,
        "title": filename[:500],
        "original_filename": filename[:500],
        "sha256": sha,
        "size_bytes": size,
        "content_type": attrs.get("content_type"),
        "updated_at": portable(document.get("updated_at")),
        "availability": "on_demand"
        if supported
        else "source_only_metadata_or_size_limit",
        "provenance": {
            "source_surface": "computer_file_copy",
            "source_concept_id": cid,
        },
    }
    return {
        "id": "file_" + digest([cid, audience]),
        "audience": audience,
        "kind": "file_manifest",
        "claim": claim,
        "vocabulary": [],
    }


def valid_owner_namespace(user, namespace):
    return (
        isinstance(namespace, str)
        and isinstance(user, str)
        and not any(c.isspace() for c in namespace)
        and (
            namespace == user
            or (namespace.startswith(user + "@") and len(namespace) > len(user) + 1)
        )
    )


def conversation_users(settings):
    users = settings.get("conversation_users", [])
    if not isinstance(users, list) or any(
        not isinstance(user, str) or "user:" + user not in settings["audiences"]
        for user in users
    ):
        raise PermissionError("conversation owners require explicit private admission")
    return users


def conversation_scopes(settings):
    scopes = settings.get("conversation_scopes", [])
    for scope in scopes:
        user, namespace = scope.get("user_id"), scope.get("namespace")
        if (
            not isinstance(user, str)
            or "user:" + user not in settings["audiences"]
            or not valid_owner_namespace(user, namespace)
        ):
            raise PermissionError(
                "conversation scope requires exact owner and namespace"
            )
    return scopes


def conversation_record(document, scope):
    source_id = document.get("session_id")
    if not isinstance(source_id, str) or not source_id:
        return None
    audience = "user:" + scope["user_id"]
    messages = document.get("history") or []
    preview = "\n".join(
        m["content"][:500]
        for m in messages[-2:]
        if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)
    )
    return {
        "id": "chat_" + digest([source_id, scope["user_id"], scope["namespace"]]),
        "audience": audience,
        "kind": "conversation",
        "vocabulary": [],
        "claim": {
            "status": "asserted",
            "audience": audience,
            "source_id": source_id,
            "user_id": scope["user_id"],
            "namespace": scope["namespace"],
            "title": str(document.get("session_name") or "Untitled conversation")[:500],
            "updated_at": portable(document.get("updated_at")),
            "preview": preview,
            "availability": "on_demand",
            "provenance": {
                "source_surface": "chat_history",
                "projection": "visible_text_only_not_editable_or_execution_state",
            },
        },
    }


def collect_catalogue(db, settings):
    result = []
    queries = list(conversation_scopes(settings))
    users = conversation_users(settings)
    if users:
        queries.append({"user_id": {"$in": users}})
    if queries:
        query = {"$or": queries}
        projection = {
            "session_id": 1,
            "user_id": 1,
            "namespace": 1,
            "session_name": 1,
            "updated_at": 1,
            "history": {"$slice": [{"$ifNull": ["$history", []]}, -2]},
        }
        for document in db["chat_history"].aggregate(
            [
                {"$match": query},
                {"$limit": MAX_RECORDS + 1},
                {"$project": projection},
                {
                    "$project": {
                        "session_id": 1,
                        "user_id": 1,
                        "namespace": 1,
                        "session_name": 1,
                        "updated_at": 1,
                        "history.role": 1,
                        "history.content": 1,
                    }
                },
            ]
        ):
            scope = {
                "user_id": document.get("user_id"),
                "namespace": document.get("namespace"),
            }
            if not valid_owner_namespace(scope["user_id"], scope["namespace"]):
                continue
            record = conversation_record(document, scope)
            if record:
                result.append(record)
    if subscriptions(settings, "file_audiences"):
        # The indexed blob metadata projection avoids fetching binary contents.
        for document in db["concepts"].find(
            {"attributes.blob_key": {"$exists": True}},
            {
                "concept_id": 1,
                "attributes.blob_key": 1,
                "attributes.sha256": 1,
                "attributes.size_bytes": 1,
                "attributes.content_type": 1,
                "attributes.original_filename": 1,
                "attributes.arxiv_id": 1,
                "attributes.source_system": 1,
                "attributes.conversation_image": 1,
                "relationships": 1,
                "name": 1,
                "updated_at": 1,
            },
        ):
            record = file_record(document, settings)
            if record:
                result.append(record)
            if len(result) > MAX_RECORDS:
                raise ValueError("continuity catalogue capacity exceeded")
    if len({r["id"] for r in result}) != len(result):
        raise ValueError("duplicate canonical conversation/file identity")
    return result


def validate_catalogue_record(row):
    claim = row["claim"]
    if (
        set(claim) - CATALOGUE_FIELDS
        or claim.get("audience") != row["audience"]
        or row["audience"] == "public"
        or row["vocabulary"]
        or not isinstance(claim.get("source_id"), str)
    ):
        raise PermissionError("invalid continuity record scope/fields")
    if row["kind"] == "conversation":
        user = claim.get("user_id")
        if (
            not isinstance(user, str)
            or row["audience"] != "user:" + user
            or not valid_owner_namespace(user, claim.get("namespace"))
        ):
            raise PermissionError("conversation owner mismatch")
        expected = "chat_" + digest([claim["source_id"], user, claim["namespace"]])
    else:
        expected = "file_" + digest([claim["source_id"], row["audience"]])
    if row["id"] != expected:
        raise ValueError("continuity source identity mismatch")


def serve_content(config, peer, request, *, db):
    """SSH-authenticated peer request; recheck native scope on every fetch.

    Requests name an exact already-admitted head record, never a file path/URL.
    A change since discovery requires resynchronisation, not a silent substitute.
    """
    settings = peer_config(config, "exports", peer)
    snapshot = db["knowledge_federation_exports"].find_one({"_id": peer}) or {}
    record = next(
        (r for r in snapshot.get("records", []) if r["id"] == request.get("id")), None
    )
    if not record or record["audience"] not in settings["audiences"]:
        raise PermissionError("content is not admitted")
    claim = record["claim"]
    if request.get("record_digest") != digest(record):
        raise ValueError("catalogue changed; synchronise before retrieval")
    body = {}
    if record["kind"] == "conversation":
        scope = {"user_id": claim["user_id"], "namespace": claim["namespace"]}
        if scope not in conversation_scopes(settings) and scope[
            "user_id"
        ] not in conversation_users(settings):
            raise PermissionError("conversation subscription withdrawn")
        document = db["chat_history"].find_one(
            {**scope, "session_id": claim["source_id"]},
            {"history.role": 1, "history.content": 1},
        )
        if not document:
            raise PermissionError("conversation withdrawn")
        text = "\n\n".join(
            m["role"] + ": " + m["content"]
            for m in document.get("history", [])
            if m.get("role") in {"user", "assistant"}
            and isinstance(m.get("content"), str)
        )
        content_digest = hashlib.sha256(text.encode()).hexdigest()
        if (
            request.get("content_digest")
            and request["content_digest"] != content_digest
        ):
            raise ValueError("conversation changed between pages; restart reading")
        offset = request.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(text):
            raise ValueError("invalid content offset")
        body = {
            "text": text[offset : offset + PAGE_CHARS],
            "offset": offset,
            "next_offset": offset + PAGE_CHARS
            if offset + PAGE_CHARS < len(text)
            else None,
            "content_digest": content_digest,
            "total_chars": len(text),
        }
    elif record["kind"] == "file_manifest":
        document = db["concepts"].find_one({"concept_id": claim["source_id"]})
        current = file_record(document, settings) if document else None
        if not current or digest(current) != digest(record):
            raise PermissionError(
                "file changed or withdrawn; synchronise before retrieval"
            )
        if claim["availability"] != "on_demand":
            raise ValueError(
                "file is source-only: metadata incomplete or exceeds transfer bound"
            )
        from ..computer_file_copy_service import fetch_file_copy_bytes

        prefix, actor = record["audience"].split(":", 1)
        from ...security.access_control import override_current_actor

        # Source identity is resolved solely from the revalidated admitted
        # record, never from caller-supplied identity fields.
        with override_current_actor(
            actor if prefix == "user" else None, actor if prefix == "org" else None
        ):
            result = fetch_file_copy_bytes(
                file_copy_concept_id=claim["source_id"],
                user_concept_id=actor if prefix == "user" else None,
                organisation_concept_id=actor if prefix == "org" else None,
                max_bytes=MAX_FILE_BYTES,
            )
        if not result.get("success"):
            raise RuntimeError("source file bytes unavailable")
        data = result["data"]
        if (
            hashlib.sha256(data).hexdigest() != claim["sha256"]
            or len(data) != claim["size_bytes"]
        ):
            raise ValueError("source file integrity mismatch")
        body = {
            "data_base64": base64.b64encode(data).decode(),
            "sha256": claim["sha256"],
        }
    else:
        raise ValueError("record has no retrievable content")
    return sign(
        {
            "schema_version": "von_federation_content.v1",
            "origin": config["node_id"],
            "recipient": peer,
            "request": request,
            "checked_at": now(),
            "body": body,
        },
        key(settings),
    )


def fetch_content(federated_id, *, offset=0, content_digest=None, config=None, db=None):
    from .protocol import load_config
    from .service import database, search
    from .transport import remote

    live_config = config is None
    config = load_config() if live_config else config
    db = database() if db is None else db
    selected = search(federated_id=federated_id, config=config, db=db)["results"]
    if len(selected) != 1:
        raise PermissionError(
            "content unavailable in current actor scope or freshness lease"
        )
    record = selected[0]
    origin = record["origin"]
    raw_record = {
        k: record[k] for k in ("id", "audience", "kind", "claim", "vocabulary")
    }
    request = {
        "id": record["id"],
        "record_digest": digest(raw_record),
        "offset": offset,
        "content_digest": content_digest,
        "nonce": secrets.token_hex(16),
    }
    envelope = remote(config, origin, "content", request)
    payload = envelope.get("payload", {})
    settings = peer_config(config, "imports", origin)
    expected = hmac.new(key(settings), encode(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(envelope.get("signature", ""))):
        raise PermissionError("content authentication failed")
    if (
        payload.get("schema_version") != "von_federation_content.v1"
        or payload.get("origin") != origin
        or payload.get("recipient") != config["node_id"]
        or payload.get("request") != request
        or abs(
            (
                datetime.now(UTC) - datetime.fromisoformat(payload["checked_at"])
            ).total_seconds()
        )
        > 300
    ):
        raise PermissionError("content response binding/freshness mismatch")
    # Admission might have been revoked while the remote transfer was running.
    current = search(
        federated_id=federated_id,
        config=load_config() if live_config else config,
        db=db,
    )
    if not current["results"]:
        raise PermissionError("content read authority expired")
    return record, payload["body"]


def read_conversation(federated_id, *, offset=0, content_digest=None):
    record, body = fetch_content(
        federated_id, offset=offset, content_digest=content_digest
    )
    if record["kind"] != "conversation":
        raise ValueError("use import_federated_file for file content")
    return {
        "federated_id": federated_id,
        "source": record["claim"],
        **body,
        "editable_original": False,
        "execution_authority": False,
    }


def import_file(federated_id):
    from ...security.access_control import get_effective_user_concept_id
    from ..computer_file_copy_service import import_bytes_file_copy

    user = get_effective_user_concept_id()
    if not user:
        raise PermissionError("file materialisation requires a trusted user")
    record, body = fetch_content(federated_id)
    if record["kind"] != "file_manifest":
        raise ValueError("record is not a file")
    data = base64.b64decode(body["data_base64"], validate=True)
    claim = record["claim"]
    if (
        len(data) > MAX_FILE_BYTES
        or len(data) != claim["size_bytes"]
        or hashlib.sha256(data).hexdigest() != claim["sha256"]
    ):
        raise ValueError("received file integrity mismatch")
    # The receiving actor's copy is private. Remote visibility is provenance,
    # never authority to publish a file or to copy membership/role grants.
    result = import_bytes_file_copy(
        data=data,
        user_concept_id=user,
        namespace=user,
        original_filename=PurePosixPath(
            claim["original_filename"].replace("\\", "/")
        ).name
        or "artefact",
        content_type=claim.get("content_type"),
        source_system="von_federation",
        source_identifier=federated_id,
        blob_key="federation/" + digest([user, federated_id]) + "/" + claim["sha256"],
        source_uri="von-federation:" + federated_id,
        metadata={
            "federation_origin": record["origin"],
            "federation_source_id": claim["source_id"],
            "federation_source_audience": record["audience"],
            "federation_source_sha256": claim["sha256"],
        },
    )
    return {
        **result,
        "federated_id": federated_id,
        "local_copy_scope": "user:" + user,
        "source_provenance": claim["provenance"],
    }
