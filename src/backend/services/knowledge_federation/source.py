"""Canonical read adapters for explicitly selected knowledge (no replica relay)."""

from __future__ import annotations

import json
from datetime import datetime

from bson import ObjectId

from .protocol import MAX_RECORDS, digest, peer_config

CLAIM_FIELDS = frozenset(
    {
        "schema_version",
        "assertion_id",
        "assertion_revision",
        "assertion_form",
        "subject_concept_id",
        "predicate",
        "object_kind",
        "object_text",
        "object_concept_id",
        "assertion_context",
        "scope",
        "status",
        "epistemic_status",
        "created_at",
        "updated_at",
        "provenance",
        "retracted_at",
        "retraction_provenance",
        "reassertion_provenance",
    }
)


def portable(value):
    def convert(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError("unsupported source value")

    return json.loads(json.dumps(value, default=convert))


def audience_can_read(concept: dict | None, audience: str) -> bool:
    """No malformed/legacy scope can accidentally become public on export."""
    from ...security.visibility_predicates import (
        SPECIFIC_TO_ORG_PREDICATES_READ,
        SPECIFIC_TO_USER_PREDICATES,
    )

    if not concept:
        return False
    relationships = concept.get("relationships", {})
    if not isinstance(relationships, dict):
        return False
    for prefix, predicates in (
        ("user", SPECIFIC_TO_USER_PREDICATES),
        ("org", SPECIFIC_TO_ORG_PREDICATES_READ),
    ):
        for predicate in predicates:
            if predicate not in relationships:
                continue
            values = relationships[predicate]
            if not isinstance(values, list) or any(
                not isinstance(v, str) or not v.startswith("#V#") for v in values
            ):
                return False
            if values and (len(values) != 1 or audience != f"{prefix}:{values[0]}"):
                return False
    return True


def permitted_predicate(predicate: str | None) -> bool:
    from ..ontology_publication_authority_service import (
        RESERVED_GENERIC_MUTATION_PREDICATES,
    )
    from ..text_relation_read_policy import is_hidden_from_generic_text_reads

    canonical = (
        predicate if not predicate or predicate.startswith("#V#") else f"#V#{predicate}"
    )
    return (
        canonical not in RESERVED_GENERIC_MUTATION_PREDICATES
        and not is_hidden_from_generic_text_reads(canonical)
    )


def collect_records(db, config: dict, peer: str) -> list[dict]:
    settings = peer_config(config, "exports", peer)
    ids = settings.get("assertion_ids", [])
    relations = settings.get("public_relations", [])
    if len(ids) + len(relations) > MAX_RECORDS or len(set(ids)) != len(ids):
        raise ValueError("explicit source selector bound exceeded/duplicate IDs")
    rows = []

    def vocabulary(references, audience):
        result = []
        for concept_id in sorted(set(references)):
            concept = db["concepts"].find_one({"concept_id": concept_id})
            if not audience_can_read(concept, audience):
                return None
            # Only ordinary, unqualified canonical vocabulary text is carried.
            # Context-qualified text needs its own scope adapter; never assume
            # that a broadly visible concept makes all attached text public.
            texts = []
            for relation in (
                db["text_relations"]
                .find(
                    {
                        "subject_concept_id": concept_id,
                        "predicate": {
                            "$in": [
                                "hasName",
                                "#V#hasName",
                                "hasDescription",
                                "#V#hasDescription",
                            ]
                        },
                        "$or": [{"context": {}}, {"context": {"$exists": False}}],
                    }
                )
                .sort("_id", 1)
                .limit(5)
            ):
                text_id = relation.get("object_text_id")
                try:
                    value = db["text_values"].find_one({"_id": ObjectId(text_id)})
                except (TypeError, ValueError):
                    value = None
                if value and isinstance(value.get("text"), str):
                    texts.append(
                        {
                            "predicate": relation["predicate"],
                            "text": value["text"],
                            "language": value.get("lang"),
                            "source_relation_id": str(relation["_id"]),
                        }
                    )
            result.append(
                {
                    "source_concept_id": concept_id,
                    "kind": "source_reference",
                    "texts": texts,
                    "definition_coverage": "bounded_canonical_vocabulary_only",
                }
            )
        return result

    for document in db["scoped_knowledge_assertions"].find(
        {"assertion_id": {"$in": ids}}
    ):
        scope = document.get("scope", {})
        audience = scope.get("audience_keys", [])
        if len(audience) != 1 or audience[0] not in settings["audiences"]:
            continue
        audience = audience[0]
        if audience == "public" or not permitted_predicate(document.get("predicate")):
            continue
        expected = (
            f"user:{scope.get('user_concept_id')}"
            if scope.get("mode") == "user"
            else f"org:{scope.get('organisation_concept_id')}"
        )
        if audience != expected or scope.get("mode") not in {"user", "organisation"}:
            raise ValueError("source assertion scope is inconsistent")
        if document.get("status") not in {"asserted", "retracted"}:
            raise ValueError("unsupported source lifecycle")
        refs = [
            document[k]
            for k in ("subject_concept_id", "object_concept_id")
            if document.get(k)
        ]
        predicate = document.get("predicate")
        if predicate:
            refs.append(predicate if predicate.startswith("#V#") else f"#V#{predicate}")
        vocab = vocabulary(refs, audience)
        if vocab is None:
            continue
        claim = portable({k: v for k, v in document.items() if k in CLAIM_FIELDS})
        rows.append(
            {
                "id": document["assertion_id"],
                "audience": audience,
                "kind": "scoped_assertion",
                "claim": claim,
                "vocabulary": vocab,
            }
        )

    if relations and "public" not in settings["audiences"]:
        raise PermissionError("public selection requires explicit public subscription")
    for selector in relations:
        subject, predicate, target = (
            selector[k] for k in ("subject", "predicate", "object")
        )
        if not permitted_predicate(predicate):
            raise PermissionError("governance predicates are not federation knowledge")
        concept = db["concepts"].find_one({"concept_id": subject})
        if not concept or target not in concept.get("relationships", {}).get(
            predicate, []
        ):
            continue  # Withdrawal/removal is represented by absence in the next complete slice.
        canonical_predicate = (
            predicate if predicate.startswith("#V#") else f"#V#{predicate}"
        )
        vocab = vocabulary([subject, canonical_predicate, target], "public")
        if vocab is None:
            continue
        identity = "ground_" + digest([subject, canonical_predicate, target])
        rows.append(
            {
                "id": identity,
                "audience": "public",
                "kind": "public_relation",
                "claim": {
                    "subject_concept_id": subject,
                    "predicate": canonical_predicate,
                    "object_concept_id": target,
                    "object_kind": "concept",
                    "status": "asserted",
                    "provenance": {
                        "source_surface": "concepts.relationships",
                        "source_concept_id": subject,
                        "source_metadata": portable(
                            {
                                k: concept[k]
                                for k in (
                                    "source",
                                    "provenance",
                                    "licence",
                                    "license",
                                    "source_uri",
                                    "external_ids",
                                )
                                if k in concept
                            }
                        ),
                    },
                },
                "vocabulary": vocab,
            }
        )
    return sorted(rows, key=lambda row: row["id"])


def stable_records(db, config: dict, peer: str) -> list[dict]:
    """Bounded reconciliation, not a cross-document database snapshot claim.

    Two matching scans detect concurrent changes to the small selected slice.
    A later change appears on the next poll; no timestamp-only cursor can skip it.
    """
    previous = collect_records(db, config, peer)
    for _ in range(3):
        current = collect_records(db, config, peer)
        if current == previous:
            return current
        previous = current
    raise RuntimeError(
        "source slice changed during export; retry without advancing generation"
    )
