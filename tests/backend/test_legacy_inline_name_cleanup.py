from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import mongomock
import pytest
from flask import Flask


@pytest.fixture
def legacy_name_runtime(monkeypatch: pytest.MonkeyPatch):
    from src.backend.db.repositories import concepts_repository, text_value_repository
    from src.backend.services import concept_service
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    database = mongomock.MongoClient()["legacy_inline_name_cleanup"]
    concepts = database["concepts"]
    text_relations = database["text_relations"]
    text_values = database["text_values"]
    delegations = database["delegations"]
    receipts = database["receipts"]
    delegations.create_index("delegation_id", unique=True)
    receipts.create_index("receipt_id", unique=True)
    receipts.create_index(
        [("actor_concept_id", 1), ("idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}},
    )

    monkeypatch.setattr(
        concepts_repository,
        "get_concepts_collection",
        lambda: concepts,
    )
    monkeypatch.setattr(
        text_value_repository,
        "get_text_relations_collection",
        lambda: text_relations,
    )
    monkeypatch.setattr(
        text_value_repository,
        "get_text_values_collection",
        lambda: text_values,
    )
    monkeypatch.setattr(
        authority,
        "get_ontology_authority_delegations_collection",
        lambda: delegations,
    )
    monkeypatch.setattr(
        authority,
        "get_ontology_mutation_receipts_collection",
        lambda: receipts,
    )
    monkeypatch.setattr(authority, "_gateway_actor_trust_source", lambda: None)
    monkeypatch.setattr(authority, "resolve_live_semantic_roles", lambda _actor: ())
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    cache_invalidations: list[bool] = []
    monkeypatch.setattr(
        concept_service,
        "_invalidate_concept_mutation_caches",
        lambda: cache_invalidations.append(True),
    )
    return {
        "authority": authority,
        "command": command,
        "concepts": concepts,
        "text_relations": text_relations,
        "text_values": text_values,
        "receipts": receipts,
        "cache_invalidations": cache_invalidations,
    }


def _seed_concept(
    runtime: dict[str, Any],
    *,
    concept_id: str = "#V#legacy_named_concept",
    names: list[Any] | None = None,
    canonical_name: str | None = "Canonical name",
    global_scope: bool = False,
) -> tuple[str, list[Any]]:
    stored_names = list(
        names
        if names is not None
        else [
            {"name": "Malformed Legacy Name", "language": "en", "type": "NL"},
            {
                "name": "Jožef Štefan — 研究 👩🏽‍🔬 e\u0301",
                "language": "sl",
                "type": "NL",
                "annotation": "δοκιμή",
            },
        ]
    )
    relationships = {} if global_scope else {"#V#specific_to_user": ["#V#admin"]}
    runtime["concepts"].insert_one(
        {
            "concept_id": concept_id,
            "guid": f"guid:{concept_id}",
            "created_at": datetime(2026, 8, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 8, 1, tzinfo=UTC),
            "embedding_status": "indexed",
            "names": stored_names,
            "relationships": relationships,
        }
    )
    if canonical_name is not None:
        runtime["text_values"].insert_one(
            {
                "_id": "canonical-name-text",
                "text": canonical_name,
                "lang": "en-NZ",
                "provenance": {"source": "curated"},
            }
        )
        runtime["text_relations"].insert_one(
            {
                "_id": "canonical-name-relation",
                "subject_concept_id": concept_id,
                "predicate": "hasName",
                "object_text_id": "canonical-name-text",
                "context": {"name_type": "NL"},
            }
        )
    return concept_id, stored_names


def _delete(
    runtime: dict[str, Any],
    *,
    concept_id: str,
    names: list[Any],
    ordinal: int,
    request_id: str,
) -> dict[str, Any]:
    command = runtime["command"]
    selector = command.legacy_name_selector_metadata(
        concept_id=concept_id,
        names=names,
        ordinal=ordinal,
    )
    with runtime["authority"].override_current_actor("#V#admin", None):
        return command.delete_legacy_name(
            concept_id=concept_id,
            legacy_name_selector=selector,
            request_id=request_id,
        )


def test_governed_legacy_name_cleanup_is_exact_and_preserves_unicode(
    legacy_name_runtime: dict[str, Any],
) -> None:
    concept_id, names = _seed_concept(legacy_name_runtime)
    canonical_before = legacy_name_runtime["text_relations"].find_one(
        {"_id": "canonical-name-relation"}
    )
    canonical_text_before = legacy_name_runtime["text_values"].find_one(
        {"_id": "canonical-name-text"}
    )
    preserved_utf8 = names[1]["name"].encode("utf-8")

    result = _delete(
        legacy_name_runtime,
        concept_id=concept_id,
        names=names,
        ordinal=0,
        request_id="legacy-name:success",
    )

    assert result["success"] is True
    assert result["authority_receipt"]["status"] == "succeeded"
    assert result["canonical_read_back"]["legacy_name_selected_entry_absent"] is True
    assert result["canonical_read_back"]["canonical_has_name_present"] is True
    stored = legacy_name_runtime["concepts"].find_one({"concept_id": concept_id})
    assert stored["names"] == [names[1]]
    assert stored["names"][0]["name"].encode("utf-8") == preserved_utf8
    assert stored["embedding_status"] == "stale"
    assert stored["updated_at"].replace(tzinfo=UTC) > datetime(2026, 8, 1, tzinfo=UTC)
    assert (
        legacy_name_runtime["text_relations"].find_one(
            {"_id": "canonical-name-relation"}
        )
        == canonical_before
    )
    assert (
        legacy_name_runtime["text_values"].find_one({"_id": "canonical-name-text"})
        == canonical_text_before
    )
    assert legacy_name_runtime["cache_invalidations"] == [True]


def test_stale_or_repeated_selector_is_refused_without_a_second_write(
    legacy_name_runtime: dict[str, Any],
) -> None:
    concept_id, names = _seed_concept(legacy_name_runtime)
    first = _delete(
        legacy_name_runtime,
        concept_id=concept_id,
        names=names,
        ordinal=0,
        request_id="legacy-name:first",
    )
    second = _delete(
        legacy_name_runtime,
        concept_id=concept_id,
        names=names,
        ordinal=0,
        request_id="legacy-name:duplicate",
    )

    assert first["success"] is True
    assert second["success"] is False
    assert second["error_code"] == "legacy_name_snapshot_precondition_failed"
    assert legacy_name_runtime["concepts"].find_one({"concept_id": concept_id})[
        "names"
    ] == [names[1]]
    assert legacy_name_runtime["cache_invalidations"] == [True]


def test_selector_refuses_snapshot_drift_before_effect(
    legacy_name_runtime: dict[str, Any],
) -> None:
    concept_id, names = _seed_concept(legacy_name_runtime)
    selector = legacy_name_runtime["command"].legacy_name_selector_metadata(
        concept_id=concept_id,
        names=names,
        ordinal=0,
    )
    legacy_name_runtime["concepts"].update_one(
        {"concept_id": concept_id},
        {"$push": {"names": {"name": "Concurrent alias", "language": "en"}}},
    )

    with legacy_name_runtime["authority"].override_current_actor("#V#admin", None):
        result = legacy_name_runtime["command"].delete_legacy_name(
            concept_id=concept_id,
            legacy_name_selector=selector,
            request_id="legacy-name:stale",
        )

    assert result["success"] is False
    assert result["error_code"] == "legacy_name_snapshot_precondition_failed"
    assert legacy_name_runtime["cache_invalidations"] == []


def test_cleanup_refuses_to_remove_legacy_name_without_canonical_has_name(
    legacy_name_runtime: dict[str, Any],
) -> None:
    concept_id, names = _seed_concept(legacy_name_runtime, canonical_name=None)

    result = _delete(
        legacy_name_runtime,
        concept_id=concept_id,
        names=names,
        ordinal=0,
        request_id="legacy-name:no-canonical",
    )

    assert result["success"] is False
    assert result["error_code"] == "canonical_has_name_required_for_legacy_cleanup"
    assert (
        legacy_name_runtime["concepts"].find_one({"concept_id": concept_id})["names"]
        == names
    )


def test_cleanup_obeys_semantic_governance_denial(
    legacy_name_runtime: dict[str, Any],
) -> None:
    concept_id, names = _seed_concept(legacy_name_runtime, global_scope=True)

    result = _delete(
        legacy_name_runtime,
        concept_id=concept_id,
        names=names,
        ordinal=0,
        request_id="legacy-name:denied",
    )

    assert result["success"] is False
    assert result["error_code"] == "global_ontology_admin_authority_required"
    assert (
        legacy_name_runtime["concepts"].find_one({"concept_id": concept_id})["names"]
        == names
    )
    assert legacy_name_runtime["cache_invalidations"] == []


def test_legacy_cleanup_and_canonical_has_name_writes_share_the_concept_lock(
    legacy_name_runtime: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concept_id, names = _seed_concept(legacy_name_runtime)
    command = legacy_name_runtime["command"]
    acquired: list[str] = []

    @contextmanager
    def record_lock(resource_key: str):
        acquired.append(resource_key)
        yield

    monkeypatch.setattr(command, "ontology_mutation_resource_lock", record_lock)
    legacy_result = _delete(
        legacy_name_runtime,
        concept_id=concept_id,
        names=names,
        ordinal=0,
        request_id="legacy-name:shared-lock",
    )
    expected_key = f"ontology-concept-names:{concept_id}"
    assert legacy_result["success"] is True
    assert expected_key in acquired

    acquired.clear()
    with legacy_name_runtime["authority"].override_current_actor("#V#admin", None):
        command.execute_governed_ontology_method(
            method_name="upsert_text_relation",
            arguments={
                "concept_id": concept_id,
                "predicate": "hasName",
                "text": "Another canonical name",
                "language": "en-NZ",
                "context": {"name_type": "NL"},
                "provenance": {"source": "test"},
                "request_id": "canonical-name:shared-lock",
            },
            mutate=lambda: {
                "success": False,
                "changed": False,
                "error_code": "test_no_effect",
            },
        )
    assert expected_key in acquired


def test_http_delete_binds_the_enriched_selector_without_rewriting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.server.routes import concept_routes

    captured: dict[str, Any] = {}

    def delete(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"success": True, "changed": True}

    monkeypatch.setattr(concept_routes, "delete_legacy_name", delete)
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(concept_routes.concept_bp, url_prefix="/api/concepts")
    selector = {
        "concept_id": "#V#unicode_研究",
        "ordinal": 2,
        "entry_sha256": "a" * 64,
        "names_snapshot_sha256": "b" * 64,
    }

    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#admin"
    response = client.delete(
        "/api/concepts/%23V%23unicode_%E7%A0%94%E7%A9%B6/legacy-names",
        json={
            "legacy_name_selector": selector,
            "request_id": "legacy-name:http",
        },
    )

    assert response.status_code == 200
    assert captured == {
        "concept_id": "#V#unicode_研究",
        "legacy_name_selector": selector,
        "request_id": "legacy-name:http",
    }


def test_http_delete_requires_session_actor_then_executes_the_governed_command(
    legacy_name_runtime: dict[str, Any],
) -> None:
    from src.backend.server.routes import concept_routes

    concept_id, names = _seed_concept(legacy_name_runtime)
    selector = legacy_name_runtime["command"].legacy_name_selector_metadata(
        concept_id=concept_id,
        names=names,
        ordinal=0,
    )
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(concept_routes.concept_bp, url_prefix="/api/concepts")
    client = app.test_client()
    path = "/api/concepts/%23V%23legacy_named_concept/legacy-names"

    denied = client.delete(
        path,
        json={"legacy_name_selector": selector, "request_id": "http:denied"},
    )
    assert denied.status_code == 403
    assert denied.get_json()["error_code"] == "authenticated_actor_context_required"

    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#admin"
    accepted = client.delete(
        path,
        json={"legacy_name_selector": selector, "request_id": "http:accepted"},
    )
    assert accepted.status_code == 200
    assert accepted.get_json()["canonical_read_back"][
        "legacy_name_selected_entry_absent"
    ] is True


def test_http_delete_rejects_client_supplied_actor_before_command_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.security import access_control
    from src.backend.server.routes import concept_routes

    command_called = False

    def unexpected_delete(**_kwargs: Any) -> dict[str, Any]:
        nonlocal command_called
        command_called = True
        return {"success": True, "changed": True}

    monkeypatch.setattr(
        access_control,
        "get_effective_user_concept_id_with_source",
        lambda: (
            "#V#client_supplied_actor",
            access_control.LEGACY_IDENTITY_HEADER_ACTOR_SOURCE,
        ),
    )
    monkeypatch.setattr(concept_routes, "delete_legacy_name", unexpected_delete)
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(concept_routes.concept_bp, url_prefix="/api/concepts")

    response = app.test_client().delete(
        "/api/concepts/%23V%23legacy_named_concept/legacy-names",
        json={
            "legacy_name_selector": {
                "concept_id": "#V#legacy_named_concept",
                "ordinal": 0,
                "entry_sha256": "a" * 64,
                "names_snapshot_sha256": "b" * 64,
            }
        },
    )

    assert response.status_code == 403
    assert response.get_json()["error_code"] == "authenticated_actor_context_required"
    assert command_called is False


def test_enrichment_exposes_exact_legacy_selector_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import concept_service, text_value_service
    from src.backend.services.ontology_mutation_command_service import (
        legacy_name_selector_metadata,
    )

    concept_id = "#V#unicode_projection"
    legacy_names = [
        {"name": "Jožef Štefan — 研究 👩🏽‍🔬 e\u0301", "language": "sl", "type": "NL"}
    ]
    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concepts",
        lambda concept_ids, **_kwargs: {
            concept_ids[0]: [
                {
                    "text": "Canonical name",
                    "lang": "en-NZ",
                    "predicate": "hasName",
                    "context": {"name_type": "NL"},
                    "relation_id": "canonical-relation",
                }
            ]
        },
    )

    enriched = concept_service.enrich_concept_with_text_relations(
        {"concept_id": concept_id, "names": legacy_names}
    )

    assert enriched["names"][0]["storage_kind"] == "text_relation"
    legacy = enriched["names"][1]
    assert legacy["name"] == legacy_names[0]["name"]
    assert legacy["storage_kind"] == "legacy_inline"
    assert legacy["legacy_name_selector"] == legacy_name_selector_metadata(
        concept_id=concept_id,
        names=legacy_names,
        ordinal=0,
    )
