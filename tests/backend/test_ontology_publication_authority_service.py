from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import mongomock
import pytest


def test_json_safe_treats_naive_pymongo_datetimes_as_utc() -> None:
    from src.backend.services import ontology_publication_authority_service as service

    stored = datetime(2026, 8, 13, 17, 5, 57, 150000, tzinfo=UTC).replace(
        tzinfo=None
    )

    assert service._json_safe(stored) == "2026-08-13T17:05:57.150000+00:00"


@pytest.fixture
def authority_stores(monkeypatch):
    from src.backend.services import ontology_publication_authority_service as service

    database = mongomock.MongoClient()["von_authority_test"]
    delegations = database["ontology_authority_delegations"]
    receipts = database["ontology_mutation_receipts"]
    delegations.create_index("delegation_id", unique=True)
    receipts.create_index("receipt_id", unique=True)
    receipts.create_index(
        [("actor_concept_id", 1), ("idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}},
    )
    monkeypatch.setattr(
        service,
        "get_ontology_authority_delegations_collection",
        lambda: delegations,
    )
    monkeypatch.setattr(
        service,
        "get_ontology_mutation_receipts_collection",
        lambda: receipts,
    )
    monkeypatch.setattr(service, "_gateway_actor_trust_source", lambda: None)
    return service, delegations, receipts


def _evidence(
    service,
    *,
    actor: str = "#V#admin",
    role: str,
    organisation: str | None = None,
):
    return service.AuthorityRoleEvidence(
        role=role,
        actor_concept_id=actor,
        organisation_concept_id=organisation,
        relation_id=f"rel:{actor}:{role}:{organisation or 'global'}",
        revision="role-revision-1",
    )


def _intent(
    service,
    *,
    context=None,
    operation: str = "relationship.add",
    predicate: str = "#V#is_a_type_of",
    idempotency_key: str | None = None,
):
    return service.OntologyMutationIntent(
        operation=operation,
        publication_context=context or service.PublicationContext.global_context(),
        target_concept_ids=("#V#graduate_student", "#V#university_student"),
        tool_name="add_relationship",
        predicate=predicate,
        delta={"target": "#V#university_student"},
        idempotency_key=idempotency_key,
    )


def test_concept_publication_context_does_not_globalise_malformed_history(
    monkeypatch,
):
    from src.backend.services import ontology_publication_authority_service as service

    concepts: dict[str, dict[str, Any]] = {
        "#V#global": {"concept_id": "#V#global", "relationships": {}},
        "#V#org": {
            "concept_id": "#V#org",
            "relationships": {"#V#specific_to_organisation": ["#V#organisation_a"]},
        },
        "#V#legacy": {
            "concept_id": "#V#legacy",
            "relationships": {"specific_to_org": ["organisation_a"]},
        },
    }
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda query: concepts.get(query.get("concept_id")),
    )

    assert (
        service.concept_publication_context("#V#global").kind
        == service.PublicationContextKind.GLOBAL
    )
    organisation = service.concept_publication_context("#V#org")
    assert organisation.kind == service.PublicationContextKind.ORGANISATION
    assert organisation.concept_id == "#V#organisation_a"
    historical = service.concept_publication_context("#V#legacy")
    assert historical.kind == service.PublicationContextKind.HISTORICAL
    assert historical.historical_predicates == ("specific_to_org",)


def test_organisation_authority_is_exact_and_does_not_imply_global(
    authority_stores,
    monkeypatch,
):
    service, _delegations, _receipts = authority_stores
    org_a_role = _evidence(
        service,
        role=service.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        organisation="#V#organisation_a",
    )
    monkeypatch.setattr(
        service,
        "resolve_live_semantic_roles",
        lambda actor: (org_a_role,) if actor == "#V#admin" else (),
    )

    with service.override_current_actor("#V#admin", "#V#organisation_a"):
        allowed = service.authorise_ontology_mutation(
            _intent(
                service,
                context=service.PublicationContext.organisation("#V#organisation_a"),
            )
        )
        wrong_org = service.authorise_ontology_mutation(
            _intent(
                service,
                context=service.PublicationContext.organisation("#V#organisation_b"),
            )
        )
        global_attempt = service.authorise_ontology_mutation(_intent(service))

    assert allowed.allowed is True
    assert allowed.reason_code == "semantic_ontology_authority_verified"
    assert wrong_org.allowed is False
    assert wrong_org.reason_code == ("organisation_ontology_admin_authority_required")
    assert global_attempt.allowed is False
    assert global_attempt.reason_code == "global_ontology_admin_authority_required"


def test_global_semantic_authority_and_von_operator_remain_separate(
    authority_stores,
    monkeypatch,
):
    service, _delegations, _receipts = authority_stores
    global_role = _evidence(
        service,
        role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    )
    monkeypatch.setattr(
        service,
        "resolve_live_semantic_roles",
        lambda actor: (global_role,) if actor == "#V#admin" else (),
    )
    with service.override_current_actor("#V#admin", None):
        global_decision = service.authorise_ontology_mutation(_intent(service))
    assert global_decision.allowed is True

    monkeypatch.setattr(
        service,
        "_gateway_actor_trust_source",
        lambda: "trusted_operator_payload_fallback",
    )
    with service.override_current_actor("#V#admin", None):
        operator_decision = service.authorise_ontology_mutation(_intent(service))
    assert operator_decision.allowed is False
    assert operator_decision.reason_code == (
        "von_operator_is_not_semantic_ontology_authority"
    )


def test_ordinary_actor_keeps_exact_private_creation_and_edit_authority(
    authority_stores,
    monkeypatch,
):
    service, _delegations, _receipts = authority_stores
    monkeypatch.setattr(service, "resolve_live_semantic_roles", lambda _actor: ())
    private_context = service.PublicationContext.user("#V#member")
    private_create = _intent(
        service,
        context=private_context,
        operation="concept.create",
        predicate=None,
    )
    private_edit = _intent(service, context=private_context)

    with service.override_current_actor("#V#member", "#V#organisation_a"):
        create_decision = service.authorise_ontology_mutation(private_create)
        edit_decision = service.authorise_ontology_mutation(private_edit)

    assert create_decision.allowed is True
    assert create_decision.reason_code == "semantic_ontology_authority_verified"
    assert edit_decision.allowed is True
    assert edit_decision.reason_code == "semantic_ontology_authority_verified"


def test_reserved_predicates_cannot_be_changed_by_generic_write(
    authority_stores,
    monkeypatch,
):
    service, _delegations, _receipts = authority_stores
    global_role = _evidence(
        service,
        role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    )
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    with service.override_current_actor("#V#admin", None):
        decision = service.authorise_ontology_mutation(
            _intent(service, predicate=service.AUTHORITY_ROLE_PREDICATE)
        )
    assert decision.allowed is False
    assert decision.reason_code == ("dedicated_ontology_governance_operation_required")


def test_exact_agent_delegation_rechecks_live_role_and_binds_audience_target(
    authority_stores,
    monkeypatch,
):
    service, delegations, _receipts = authority_stores
    global_role = _evidence(
        service,
        role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    )
    live_roles = {"#V#admin": (global_role,)}
    monkeypatch.setattr(
        service,
        "resolve_live_semantic_roles",
        lambda actor: live_roles.get(actor, ()),
    )
    intent = _intent(service)
    issued_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    grant = service.issue_agent_delegation(
        intent=intent,
        grantor_actor_concept_id="#V#admin",
        grantor_organisation_concept_id=None,
        delegate_concept_id="#V#von_system",
        audience="adaptive_turn",
        tool_name="add_relationship",
        effect_id="effect-1",
        turn_id="turn-1",
        now=issued_at,
    )

    with (
        service.override_current_actor("#V#admin", None),
        service.bind_ontology_invocation(
            surface="ordinary_turn",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
            delegation_id=grant["delegation_id"],
            effect_id="effect-1",
            turn_id="turn-1",
        ),
    ):
        valid = service.verify_agent_delegation(
            delegation_id=grant["delegation_id"],
            intent=intent,
            invocation=service.current_ontology_invocation(),
            actor_concept_id="#V#admin",
            organisation_concept_id=None,
            now=issued_at + timedelta(seconds=1),
        )
        with service.bind_ontology_invocation(
            surface="ordinary_turn",
            executing_agent_concept_id="#V#von_system",
            audience="wrong_audience",
            delegation_id=grant["delegation_id"],
            effect_id="effect-1",
        ):
            wrong_audience = service.verify_agent_delegation(
                delegation_id=grant["delegation_id"],
                intent=intent,
                invocation=service.current_ontology_invocation(),
                actor_concept_id="#V#admin",
                organisation_concept_id=None,
                now=issued_at + timedelta(seconds=1),
            )

    assert valid.allowed is True
    assert wrong_audience.allowed is False
    assert wrong_audience.reason_code == "ontology_delegation_audience_mismatch"

    live_roles.clear()
    stored = delegations.find_one({"delegation_id": grant["delegation_id"]})
    assert stored is not None
    with service.bind_ontology_invocation(
        surface="ordinary_turn",
        executing_agent_concept_id="#V#von_system",
        audience="adaptive_turn",
        delegation_id=grant["delegation_id"],
        effect_id="effect-1",
        turn_id="turn-1",
    ):
        revoked_role = service.verify_agent_delegation(
            delegation_id=grant["delegation_id"],
            intent=intent,
            invocation=service.current_ontology_invocation(),
            actor_concept_id="#V#admin",
            organisation_concept_id=None,
            now=issued_at + timedelta(seconds=2),
        )
    assert revoked_role.allowed is False
    assert revoked_role.reason_code == ("ontology_delegation_grantor_authority_revoked")


def test_expired_and_explicitly_revoked_delegations_fail_next_effect(
    authority_stores,
    monkeypatch,
):
    service, _delegations, _receipts = authority_stores
    global_role = _evidence(
        service,
        role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    )
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    intent = _intent(service)
    issued_at = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
    grant = service.issue_agent_delegation(
        intent=intent,
        grantor_actor_concept_id="#V#admin",
        grantor_organisation_concept_id=None,
        delegate_concept_id="#V#von_system",
        audience="adaptive_turn",
        tool_name="add_relationship",
        effect_id="effect-expiry",
        ttl_seconds=1,
        now=issued_at,
    )
    with service.bind_ontology_invocation(
        surface="ordinary_turn",
        executing_agent_concept_id="#V#von_system",
        audience="adaptive_turn",
        delegation_id=grant["delegation_id"],
        effect_id="effect-expiry",
    ):
        expired = service.verify_agent_delegation(
            delegation_id=grant["delegation_id"],
            intent=intent,
            invocation=service.current_ontology_invocation(),
            actor_concept_id="#V#admin",
            organisation_concept_id=None,
            now=issued_at + timedelta(seconds=2),
        )
    assert expired.reason_code == "ontology_delegation_expired"

    active = service.issue_agent_delegation(
        intent=intent,
        grantor_actor_concept_id="#V#admin",
        grantor_organisation_concept_id=None,
        delegate_concept_id="#V#von_system",
        audience="adaptive_turn",
        tool_name="add_relationship",
        effect_id="effect-revoke",
        now=issued_at,
    )
    revoked = service.revoke_agent_delegation(
        delegation_id=active["delegation_id"],
        acting_actor_concept_id="#V#admin",
        reason="test revocation",
    )
    assert revoked["status"] == "revoked"


def test_mutation_receipt_reconciles_and_replays_idempotently(
    authority_stores,
    monkeypatch,
):
    service, _delegations, receipts = authority_stores
    global_role = _evidence(
        service,
        role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    )
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    intent = _intent(service, idempotency_key="effect:stable-1")
    calls = {"mutate": 0}
    canonical_state = {"relationship_present": False}

    def mutate():
        calls["mutate"] += 1
        canonical_state["relationship_present"] = True
        return {"success": True, "changed": True, "added": True}

    with service.override_current_actor("#V#admin", None):
        first = service.execute_authorised_ontology_mutation(
            intent=intent,
            mutate=mutate,
            read_back=lambda: dict(canonical_state),
            read_before=lambda: dict(canonical_state),
        )
        second = service.execute_authorised_ontology_mutation(
            intent=intent,
            mutate=mutate,
            read_back=lambda: dict(canonical_state),
            read_before=lambda: dict(canonical_state),
        )

    assert calls["mutate"] == 1
    assert first["success"] is True
    assert first["canonical_read_back"]["relationship_present"] is True
    assert second["idempotent_replay"] is True
    assert first["authority_receipt"]["actor_concept_id"] == "#V#admin"
    assert receipts.count_documents({}) == 1


def test_agent_without_delegation_and_raw_payload_actor_fail_closed(
    authority_stores,
    monkeypatch,
):
    service, _delegations, _receipts = authority_stores
    global_role = _evidence(
        service,
        role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    )
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    with (
        service.override_current_actor("#V#admin", None),
        service.bind_ontology_invocation(
            surface="workflow",
            executing_agent_concept_id="#V#workflow_agent",
            audience="workflow",
            effect_id="effect-no-grant",
        ),
    ):
        missing = service.authorise_ontology_mutation(_intent(service))
    assert missing.reason_code == "ontology_agent_delegation_required"

    monkeypatch.setattr(
        service, "_gateway_actor_trust_source", lambda: "tool_payload_fallback"
    )
    with service.override_current_actor("#V#spoofed_admin", None):
        spoofed = service.authorise_ontology_mutation(_intent(service))
    assert spoofed.reason_code == "client_supplied_identity_is_not_authority"
