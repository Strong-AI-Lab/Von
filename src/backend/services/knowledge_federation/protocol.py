"""Bounded complete-slice reconciliation (JVNAUTOSCI-2730).

A generation is one atomic document, not a distributed transaction or an
all-history event stream. Failed/partial transfers cannot publish partial views.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pymongo.errors import DuplicateKeyError

VERSION = "von_knowledge_snapshot.v2"
CONFIG_VERSION = "von_federation_config.v1"
MAX_BYTES = 12 * 1024 * 1024
MAX_RECORDS = 20000
NODE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")


def now() -> str:
    return datetime.now(UTC).isoformat()


def encode(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def private_file(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() or p.is_symlink():
        raise ValueError("federation files must be absolute regular paths")
    stat = p.stat()
    if not p.is_file() or stat.st_mode & 0o077 or stat.st_uid != os.getuid():
        raise PermissionError(
            "federation file must be owned by this user and mode 0600"
        )
    return p


def load_config(path: str | None = None) -> dict:
    path = path or os.getenv("VON_FEDERATION_CONFIG")
    if not path:
        return {"node_id": None, "imports": {}, "exports": {}}
    config = json.loads(private_file(path).read_text())
    if config.get("schema_version") != CONFIG_VERSION or not NODE.fullmatch(
        config.get("node_id", "")
    ):
        raise ValueError("invalid federation configuration version/node")
    for direction in ("imports", "exports"):
        for peer, settings in config.get(direction, {}).items():
            if not NODE.fullmatch(peer) or peer == config["node_id"]:
                raise ValueError("invalid peer node")
            if not isinstance(settings.get("audiences"), list):
                raise TypeError("explicit audience subscriptions required")
            for audience in settings["audiences"]:
                validate_audience(audience)
            if direction == "imports":
                mappings = settings.get("audience_map", {})
                for audience in settings["audiences"]:
                    target = mappings.get(audience)
                    if audience == "public":
                        if target != "public":
                            raise ValueError("public mapping must be explicit")
                    elif not isinstance(target, str) or not target.startswith(
                        audience.split(":", 1)[0] + ":#V#"
                    ):
                        raise ValueError("private mappings must preserve audience kind")
    return config


def validate_audience(value: str) -> None:
    if value != "public" and not re.fullmatch(r"(?:user|org):#V#[^\s]{1,500}", value):
        raise ValueError("invalid federation audience")


def peer_config(config: dict, direction: str, peer: str) -> dict:
    settings = config.get(direction, {}).get(peer)
    if not settings or settings.get("enabled") is not True:
        raise PermissionError("federation peer is not admitted")
    return settings


def key(settings: dict) -> bytes:
    value = bytes.fromhex(private_file(settings["key_file"]).read_text().strip())
    if len(value) < 32:
        raise ValueError("federation key must have at least 256 bits")
    return value


def sign(payload: dict, secret: bytes) -> dict:
    return {
        "payload": payload,
        "signature": hmac.new(secret, encode(payload), hashlib.sha256).hexdigest(),
    }


def validate_payload(
    payload: dict, *, origin: str, recipient: str, audiences: list[str]
) -> None:
    if len(encode(payload)) > MAX_BYTES:
        raise ValueError("snapshot exceeds pilot byte bound")
    if set(payload) != {
        "schema_version",
        "origin",
        "recipient",
        "generation",
        "checked_at",
        "records",
        "digest",
    }:
        raise ValueError("invalid snapshot envelope")
    if (
        payload["schema_version"] != VERSION
        or payload["origin"] != origin
        or payload["recipient"] != recipient
    ):
        raise PermissionError("incompatible or misdirected snapshot")
    if type(payload["generation"]) is not int or payload["generation"] < 1:
        raise ValueError("invalid generation")
    checked = datetime.fromisoformat(payload["checked_at"])
    if checked.tzinfo is None or (checked - datetime.now(UTC)).total_seconds() > 300:
        raise ValueError("invalid source clock")
    rows = payload["records"]
    if (
        not isinstance(rows, list)
        or len(rows) > MAX_RECORDS
        or digest(rows) != payload["digest"]
    ):
        raise ValueError("invalid snapshot records/digest")
    ids = set()
    for row in rows:
        if set(row) != {"id", "audience", "kind", "claim", "vocabulary"}:
            raise ValueError("invalid knowledge record")
        if row["id"] in ids or not isinstance(row["id"], str) or len(row["id"]) > 1000:
            raise ValueError("duplicate/invalid source assertion identity")
        ids.add(row["id"])
        if row["audience"] not in audiences:
            raise PermissionError("snapshot audience outside subscription")
        if row["kind"] not in {
            "scoped_assertion",
            "public_relation",
            "conversation",
            "file_manifest",
        }:
            raise ValueError("unsupported knowledge kind")
        claim = row["claim"]
        if not isinstance(claim, dict) or not isinstance(row["vocabulary"], list):
            raise TypeError("invalid claim/vocabulary")
        if claim.get("status") not in {"asserted", "retracted"} or not isinstance(
            claim.get("provenance"), dict
        ):
            raise ValueError("missing claim lifecycle/provenance")
        from .source import CLAIM_FIELDS, permitted_predicate

        if not permitted_predicate(claim.get("predicate")):
            raise PermissionError(
                "governance content cannot enter the knowledge projection"
            )
        if row["kind"] in {"conversation", "file_manifest"}:
            from .continuity import validate_catalogue_record

            validate_catalogue_record(row)
        elif row["kind"] == "scoped_assertion":
            scope = claim.get("scope", {})
            expected = (
                f"user:{scope.get('user_concept_id')}"
                if scope.get("mode") == "user"
                else f"org:{scope.get('organisation_concept_id')}"
            )
            if (
                row["audience"] == "public"
                or scope.get("audience_keys") != [row["audience"]]
                or scope.get("mode") not in {"user", "organisation"}
                or expected != row["audience"]
                or claim.get("assertion_id") != row["id"]
                or set(claim) - CLAIM_FIELDS
            ):
                raise PermissionError("scoped assertion identity/scope/fields mismatch")
        elif row["audience"] != "public" or set(claim) - {
            "subject_concept_id",
            "predicate",
            "object_concept_id",
            "object_kind",
            "status",
            "provenance",
        }:
            raise ValueError("public relation audience/fields mismatch")
        for reference in row["vocabulary"]:
            if not isinstance(reference, dict) or set(reference) - {
                "source_concept_id",
                "kind",
                "texts",
                "definition_coverage",
            }:
                raise ValueError("invalid source vocabulary")
            if not isinstance(reference.get("source_concept_id"), str) or not reference[
                "source_concept_id"
            ].startswith("#V#"):
                raise ValueError("invalid vocabulary identifier")
            for text in reference.get("texts", []):
                if text.get("predicate") not in {
                    "hasName",
                    "#V#hasName",
                    "hasDescription",
                    "#V#hasDescription",
                }:
                    raise ValueError("unsupported vocabulary text")


def make_snapshot(collection, config: dict, peer: str, records: list[dict]) -> dict:
    """CAS publishes a whole signed outbox head; same content keeps its generation."""
    settings = peer_config(config, "exports", peer)
    secret = key(settings)
    for _ in range(8):
        previous = collection.find_one({"_id": peer})
        current_records = sorted(
            records() if callable(records) else records, key=lambda row: row["id"]
        )
        content_digest = digest(current_records)
        generation = (previous or {}).get("generation", 0)
        if not previous or previous["digest"] != content_digest:
            generation += 1
        payload = {
            "schema_version": VERSION,
            "origin": config["node_id"],
            "recipient": peer,
            "generation": generation,
            "checked_at": now(),
            "records": current_records,
            "digest": content_digest,
        }
        validate_payload(
            payload,
            origin=config["node_id"],
            recipient=peer,
            audiences=settings["audiences"],
        )
        document = {"_id": peer, **payload}
        if previous:
            result = collection.replace_one(
                {
                    "_id": peer,
                    "generation": previous["generation"],
                    "checked_at": previous["checked_at"],
                },
                document,
            )
            if not result.matched_count:
                continue
        else:
            try:
                collection.insert_one(document)
            except DuplicateKeyError:
                continue
        return sign(payload, secret)
    raise RuntimeError("concurrent federation export; retry")


def compact_snapshot(envelope, known_digest):
    """Omit unchanged records only when the receiver advertises that exact digest.

    Signature remains over the full payload. Receiver reconstruction must match
    its authenticated stored head and is verified before advancing freshness.
    """
    if known_digest and envelope["payload"]["digest"] == known_digest:
        return {
            "payload": {k: v for k, v in envelope["payload"].items() if k != "records"},
            "signature": envelope["signature"],
            "records_omitted": True,
        }
    return envelope


def apply_snapshot(collection, config: dict, origin: str, envelope: dict) -> dict:
    settings = peer_config(config, "imports", origin)
    payload = envelope.get("payload", {})
    if envelope.get("records_omitted") is True:
        cached = collection.find_one({"_id": origin})
        if (
            not cached
            or cached["digest"] != payload.get("digest")
            or "records" in payload
        ):
            raise ValueError("compact snapshot cache mismatch; retry full exchange")
        payload = {**payload, "records": cached["records"]}
    expected = hmac.new(key(settings), encode(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(envelope.get("signature", ""))):
        raise PermissionError("snapshot authentication failed")
    validate_payload(
        payload,
        origin=origin,
        recipient=config["node_id"],
        audiences=settings["audiences"],
    )
    for _ in range(8):
        previous = collection.find_one({"_id": origin})
        if previous:
            if payload["generation"] < previous["generation"]:
                raise ValueError("stale snapshot generation")
            if (
                payload["generation"] == previous["generation"]
                and payload["digest"] != previous["digest"]
            ):
                raise ValueError("origin equivocation at same generation")
            if datetime.fromisoformat(payload["checked_at"]) < datetime.fromisoformat(
                previous["checked_at"]
            ):
                raise ValueError("stale snapshot observation")
        document = {"_id": origin, **payload, "received_at": now()}
        if previous:
            result = collection.replace_one(
                {
                    "_id": origin,
                    "generation": previous["generation"],
                    "checked_at": previous["checked_at"],
                },
                document,
            )
            if not result.matched_count:
                continue
        else:
            try:
                collection.insert_one(document)
            except DuplicateKeyError:
                continue
        old_ids = {row["id"] for row in (previous or {}).get("records", [])}
        new_ids = {row["id"] for row in payload["records"]}
        read_back = collection.find_one({"_id": origin})
        # Another importer may already have advanced the head; never certify
        # an older head as the currently visible one.
        return {
            "status": (
                "already_applied"
                if previous and previous["digest"] == payload["digest"]
                else "applied"
            ),
            "origin": origin,
            "generation": payload["generation"],
            "digest": payload["digest"],
            "records": len(new_ids),
            "withdrawn": len(old_ids - new_ids),
            "current_generation": read_back["generation"],
            "current_digest": read_back["digest"],
            "canonical_publication": False,
            "execution_authority": False,
        }
    raise RuntimeError("concurrent federation import; retry")
