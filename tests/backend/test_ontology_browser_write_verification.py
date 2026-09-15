"""Real governed HTTP writes against isolated storage, including divergent effects."""

from urllib.parse import quote

import pytest
from flask import Flask

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import override_current_actor
from src.backend.server.routes import concept_routes, vontology_routes
from src.backend.services import concept_service, text_value_service
from src.backend.services import ontology_mutation_command_service as command
from src.backend.services import ontology_publication_authority_service as authority
from src.backend.services import window_session_context_service as windows

ACTOR = "#V#fixture_author"
ORG = "#V#fixture_sail"
CONCEPT = "#V#fixture_description"


@pytest.fixture
def browser_writes(isolated_window_session_db, monkeypatch):
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "0")
    monkeypatch.setattr(authority, "_gateway_actor_trust_source", lambda: None)
    app = Flask(__name__)
    app.secret_key = "isolated-tests-only"
    app.register_blueprint(concept_routes.concept_bp, url_prefix="/api/concepts")
    app.register_blueprint(
        vontology_routes.vontology_bp, url_prefix="/vontology/api/vontology"
    )
    # Synthetic fixture seed only; tested mutations go through canonical services.
    collection = ConceptsRepository.collection()
    collection.insert_many(
        [
            {
                "concept_id": ACTOR,
                "relationships": {"is_an_instance_of": ["#V#person"]},
            },
            {"concept_id": "#V#thing", "relationships": {}},
            {"concept_id": CONCEPT, "relationships": {"#V#specific_to_user": [ACTOR]}},
        ]
    )
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            user_concept_id=ACTOR,
            organisation_concept_id=ORG,
            auth_provider="browser_test_fixture",
            role_in_org="member",
            namespace="#V#fixture_author@fixture_sail",
        )
    windows.clear_window_organisation("personal-window", namespace=ACTOR, user_id=ACTOR)
    windows.set_window_organisation(
        "sail-window", ORG, "member", "#V#fixture_author@fixture_sail", ACTOR
    )
    return client


@pytest.mark.parametrize("window", ["personal-window", "sail-window"])
def test_creation_defaults_remain_private_in_both_windows(browser_writes, window):
    client = browser_writes
    payload = {
        "concept_id": "#V#fixture_created",
        "name": "Fixture created",
        "kind": "instance",
        "parent_concept_ids": ["#V#thing"],
    }
    response = client.post(
        "/api/concepts/", json=payload, headers={"X-Von-Window-Session": window}
    )
    result = response.get_json()
    assert response.status_code == 201, result
    assert result["canonical_read_back"]["verified"] is True
    assert result["authority_receipt"]["status"] == "succeeded"
    with override_current_actor(ACTOR, ORG if window == "sail-window" else None):
        stored = concept_service.get_concept_by_concept_id("#V#fixture_created")
    assert stored["relationships"]["#V#specific_to_user"] == [ACTOR]
    assert not stored["relationships"].get("#V#specific_to_organisation")
    # Repeating a create reconciles the exact visible record; no second effect.
    repeated = client.post(
        "/api/concepts/", json=payload, headers={"X-Von-Window-Session": window}
    )
    assert repeated.get_json()["success"] is True, repeated.get_json()
    assert (
        ConceptsRepository.collection().count_documents(
            {"concept_id": "#V#fixture_created"}
        )
        == 1
    )


@pytest.mark.parametrize("route", ["patch", "put", "legacy"])
@pytest.mark.parametrize("window", ["personal-window", "sail-window"])
def test_description_exact_metadata_language_and_repeated_edit(
    browser_writes, route, window
):
    client = browser_writes
    with override_current_actor(ACTOR, ORG):
        text_value_service.upsert_text_for_concept(
            CONCEPT,
            "hasDescription",
            "Old description",
            lang="en",
            context={"confidence_score": 0.91},
            provenance={"source": "fixture-source", "attribution": "fixture-author"},
        )
    raw = "Source: imported\nConfidence: 20%\n\n  ## Heading\n\nText  with spacing.  "
    expected = "## Heading\n\nText  with spacing."
    path = f"/api/concepts/{quote(CONCEPT, safe='')}"
    headers = {"X-Von-Window-Session": window}

    def edit(text):
        if route == "legacy":
            return client.post(
                "/vontology/api/vontology/update_description",
                json={"identifier": CONCEPT, "description": text},
                headers=headers,
            )
        if route == "put":
            return client.put(path, json={"description": text}, headers=headers)
        return client.patch(
            path + "/description", json={"description": text}, headers=headers
        )

    for text in [raw, raw, expected.replace("Text", "TEXT")]:
        response = edit(text)
        assert response.status_code == 200, response.get_json()
        with override_current_actor(ACTOR, ORG):
            rows = text_value_service.get_texts_for_concept(
                CONCEPT, predicate="hasDescription"
            )
        assert len(rows) == 1
        assert rows[0]["text"] == (expected if text == raw else text)
        assert rows[0]["lang"] == "en"
        assert rows[0]["context"]["confidence_score"] == 0.91
        assert rows[0]["context"]["write_strategy"] == "relations_only_v2"
        assert rows[0]["provenance"]["source"] == "fixture-source"
    receipts = authority.get_ontology_mutation_receipts_collection()
    assert receipts.count_documents({"status": "indeterminate"}) == 0
    assert receipts.count_documents({"status": "succeeded"}) == 3


@pytest.mark.parametrize(
    "divergence", ["absent", "text", "language", "context", "provenance", "duplicate"]
)
def test_real_divergence_never_becomes_success(browser_writes, monkeypatch, divergence):
    original = concept_service.update_concept_description

    def divergent(concept_id, text, **kwargs):
        if divergence == "absent":
            return True
        success = original(concept_id, text, **kwargs)
        relation = TextRelationsRepository.collection().find_one(
            {"subject_concept_id": concept_id}
        )
        text_id = relation["object_text_id"]
        from bson import ObjectId

        if divergence in {"text", "language", "provenance"}:
            key, value = {
                "text": ("text", "Different body"),
                "language": ("lang", "fr"),
                "provenance": ("provenance", {"source": "wrong"}),
            }[divergence]
            TextValuesRepository.collection().update_one(
                {"_id": ObjectId(text_id)}, {"$set": {key: value}}
            )
        elif divergence == "context":
            TextRelationsRepository.collection().update_one(
                {"_id": relation["_id"]}, {"$set": {"context": {"wrong": True}}}
            )
        elif divergence == "duplicate":
            text_value_service.upsert_text_for_concept(
                concept_id, "hasDescription", "Unpruned other description", lang="en"
            )
        return success

    monkeypatch.setattr(concept_service, "update_concept_description", divergent)
    response = browser_writes.patch(
        f"/api/concepts/{quote(CONCEPT, safe='')}/description",
        json={"description": "Requested description"},
    )
    result = response.get_json()
    assert response.status_code == 503, result
    assert result["effect_status"] == "indeterminate"
    assert result["retryable"] is False
    assert result["canonical_read_back"]["verified"] is False
    assert result["authority_receipt"]["status"] == "indeterminate"


def test_personal_scope_does_not_resurrect_browser_wide_org(browser_writes):
    with browser_writes.application.test_request_context(
        headers={"X-Von-Window-Session": "personal-window"}
    ):
        from flask import session

        session.update(
            user_concept_id=ACTOR,
            organisation_concept_id=ORG,
            auth_provider="browser_test_fixture",
        )
        assert concept_routes._get_current_org_concept_id() is None
        assert concept_service._resolve_creation_actor_context(
            created_by_concept_id=ACTOR,
            organisation_concept_id=None,
            event_namespace=None,
        ) == (ACTOR, None)


@pytest.mark.parametrize(
    "window,expected_status", [("personal-window", 400), ("sail-window", 201)]
)
def test_explicit_organisation_uses_only_the_selected_window(
    browser_writes, monkeypatch, window, expected_status
):
    monkeypatch.setattr(
        authority,
        "resolve_live_semantic_roles",
        lambda actor: (
            authority.AuthorityRoleEvidence(
                role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                actor_concept_id=ACTOR,
                organisation_concept_id=ORG,
                relation_id="fixture-org-role",
                revision="1",
            ),
        ),
    )
    response = browser_writes.post(
        "/api/concepts/",
        headers={"X-Von-Window-Session": window},
        json={
            "name": "Organisation record",
            "concept_id": "#V#fixture_org_record",
            "kind": "type",
            "scope_mode": "organisation_general",
        },
    )
    assert response.status_code == expected_status, response.get_json()
    stored = ConceptsRepository.collection().find_one(
        {"concept_id": "#V#fixture_org_record"}
    )
    if expected_status == 400:
        assert stored is None
        assert response.get_json()["error_code"] == "organisation_context_required"
    else:
        assert stored["relationships"]["#V#specific_to_organisation"] == [ORG]
        assert not stored["relationships"].get("#V#specific_to_user")


def test_composite_private_description_preserves_both_restrictions(
    browser_writes, monkeypatch
):
    ConceptsRepository.collection().update_one(
        {"concept_id": CONCEPT},
        {"$set": {"relationships.#V#specific_to_organisation": [ORG]}},
    )
    monkeypatch.setattr(
        authority,
        "resolve_live_semantic_roles",
        lambda actor: (
            authority.AuthorityRoleEvidence(
                role=authority.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
                actor_concept_id=ACTOR,
                organisation_concept_id=ORG,
                relation_id="fixture-org-role",
                revision="1",
            ),
        ),
    )
    response = browser_writes.patch(
        f"/api/concepts/{quote(CONCEPT, safe='')}/description",
        json={"description": "Composite private description"},
        headers={"X-Von-Window-Session": "sail-window"},
    )
    assert response.status_code == 200, response.get_json()
    stored = ConceptsRepository.collection().find_one({"concept_id": CONCEPT})
    assert stored["relationships"] == {
        "#V#specific_to_user": [ACTOR],
        "#V#specific_to_organisation": [ORG],
    }


@pytest.mark.parametrize("scope_change", ["global", "add_org"])
def test_creation_scope_mismatch_stays_indeterminate(
    browser_writes, monkeypatch, scope_change
):
    original = concept_service.create_concept

    def divergent(**kwargs):
        result = original(**kwargs)
        patch = (
            {"$unset": {"relationships.#V#specific_to_user": ""}}
            if scope_change == "global"
            else {"$set": {"relationships.#V#specific_to_organisation": [ORG]}}
        )
        ConceptsRepository.collection().update_one(
            {"concept_id": result["concept_id"]}, patch
        )
        return result

    monkeypatch.setattr(concept_service, "create_concept", divergent)
    response = browser_writes.post(
        "/api/concepts/",
        json={
            "concept_id": "#V#fixture_scope_mismatch",
            "name": "Scope mismatch",
            "kind": "type",
        },
    )
    result = response.get_json()
    assert response.status_code == 503, result
    assert result["effect_status"] == "indeterminate"
    assert result["canonical_read_back"]["verified"] is False


def test_metadata_read_failure_does_not_start_a_description_write(
    browser_writes, monkeypatch
):
    def unavailable(*args, **kwargs):
        raise ConnectionError("fixture read unavailable")

    monkeypatch.setattr(concept_service, "get_texts_for_concept", unavailable)
    response = browser_writes.patch(
        f"/api/concepts/{quote(CONCEPT, safe='')}/description",
        json={"description": "Draft"},
    )
    assert response.status_code == 500
    assert (
        TextRelationsRepository.collection().count_documents(
            {"subject_concept_id": CONCEPT}
        )
        == 0
    )
    assert (
        authority.get_ontology_mutation_receipts_collection().count_documents({}) == 0
    )


def test_existing_identical_text_value_is_reused_without_duplicate_or_provenance_loss(
    browser_writes,
):
    with override_current_actor(ACTOR, ORG):
        seeded = text_value_service.upsert_text_for_concept(
            CONCEPT,
            "hasDescription",
            "Existing exact description",
            lang="en",
            provenance={"source": "original-import", "record_id": "fixture-17"},
        )
    count_before = TextValuesRepository.collection().count_documents({})
    response = browser_writes.patch(
        f"/api/concepts/{quote(CONCEPT, safe='')}/description",
        json={"description": "Existing exact description"},
    )
    assert response.status_code == 200, response.get_json()
    with override_current_actor(ACTOR, ORG):
        rows = text_value_service.get_texts_for_concept(
            CONCEPT, predicate="hasDescription"
        )
    assert len(rows) == 1
    assert rows[0]["text_value_id"] == seeded["text_value_id"]
    assert rows[0]["provenance"] == {
        "source": "original-import",
        "record_id": "fixture-17",
    }
    assert TextValuesRepository.collection().count_documents({}) == count_before
