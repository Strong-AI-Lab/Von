"""Tier-3 authority regressions for JVNAUTOSCI-2632.

These examples are deterministic reconstructions of the blocked 12 August
2026 candidature classifications.  They exercise only in-memory authority
stores; no test reads or mutates live Vontology data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from typing import Any

import mongomock
import pytest

BLOCKED_CANDIDATURE_SOURCE_IDS = (
    "#V#andrew_probert",
    "#V#andrey_borro",
    "#V#bin_zhang",
    "#V#person_gael_gendron_3705b277",
    "#V#gibran_zazueta_cruz",
    "#V#ichieh_wei",
    "#V#jian_cheng",
    "#V#junchen_liu",
    "#V#kobe_knowles",
    "#V#minjung_kim",
    "#V#nathan_young",
    "#V#neet_zkan_tan",
    "#V#rebecca_allcock",
    "#V#shaoxin_zhong",
    "#V#stefan_fuchs",
    "#V#tim_hartill",
    "#V#timothy_pistotti",
    "#V#xianda_zheng",
    "#V#yaotian_shi",
    "#V#zhu_yonghua",
    "#V#yuchen_su",
    "#V#yueying_zhou",
    "#V#ziqin_zhu",
)

PREVIOUSLY_APPLIED_CANDIDATURE_SOURCE_IDS = (
    "#V#aaron_keesing",
    "#V#beryl_qi",
    "#V#lilin_zhang",
    "#V#qiyi_zhang",
    "#V#zhenyun_deng",
)

BLOCKED_CANDIDATURE_TUPLES = tuple(
    (source_id, "#V#is_an_instance_of", "#V#student")
    for source_id in BLOCKED_CANDIDATURE_SOURCE_IDS
)
GRADUATE_STUDENT_TAXONOMY_TUPLE = (
    "#V#graduate_student",
    "#V#is_a_type_of",
    "#V#university_student",
)


@pytest.fixture
def authority_service(monkeypatch):
    from src.backend.services import ontology_publication_authority_service as service

    database = mongomock.MongoClient()["tier3_ontology_authority"]
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


def _role(service, *, role: str, organisation: str | None = None):
    return service.AuthorityRoleEvidence(
        role=role,
        actor_concept_id="#V#semantic_admin",
        organisation_concept_id=organisation,
        relation_id=f"role:{role}:{organisation or 'global'}",
        revision="tier3",
    )


def _intent(
    service,
    *,
    source_id: str = "#V#graduate_student",
    predicate: str = "#V#is_a_type_of",
    target_id: str = "#V#university_student",
    context=None,
    source_contexts=(),
    idempotency_key: str | None = None,
):
    return service.OntologyMutationIntent(
        operation="relationship.add",
        publication_context=context or service.PublicationContext.global_context(),
        source_contexts=tuple(source_contexts),
        target_concept_ids=(source_id, target_id),
        tool_name="add_relationship",
        predicate=predicate,
        delta={"target_concept_id": target_id},
        idempotency_key=idempotency_key,
    )


def test_motivating_candidature_fixture_is_complete_and_exact() -> None:
    assert len(BLOCKED_CANDIDATURE_TUPLES) == 23
    assert len(set(BLOCKED_CANDIDATURE_SOURCE_IDS)) == 23
    assert len(PREVIOUSLY_APPLIED_CANDIDATURE_SOURCE_IDS) == 5
    assert not set(BLOCKED_CANDIDATURE_SOURCE_IDS).intersection(
        PREVIOUSLY_APPLIED_CANDIDATURE_SOURCE_IDS
    )
    assert "#V#minjung_kim" in BLOCKED_CANDIDATURE_SOURCE_IDS
    assert "#V#lilin_zhang" in PREVIOUSLY_APPLIED_CANDIDATURE_SOURCE_IDS
    assert all(
        predicate == "#V#is_an_instance_of" and target == "#V#student"
        for _source, predicate, target in BLOCKED_CANDIDATURE_TUPLES
    )
    assert GRADUATE_STUDENT_TAXONOMY_TUPLE == (
        "#V#graduate_student",
        "#V#is_a_type_of",
        "#V#university_student",
    )


@pytest.mark.parametrize("source_id,predicate,target_id", BLOCKED_CANDIDATURE_TUPLES)
def test_global_semantic_admin_can_authorise_each_previously_blocked_candidature(
    authority_service,
    monkeypatch,
    source_id: str,
    predicate: str,
    target_id: str,
) -> None:
    service, _delegations, _receipts = authority_service
    global_role = _role(
        service,
        role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    )
    monkeypatch.setattr(
        service,
        "resolve_live_semantic_roles",
        lambda _actor: (global_role,),
    )
    with service.override_current_actor("#V#semantic_admin", None):
        decision = service.authorise_ontology_mutation(
            _intent(
                service,
                source_id=source_id,
                predicate=predicate,
                target_id=target_id,
            )
        )
    assert decision.allowed is True
    assert decision.reason_code == "semantic_ontology_authority_verified"


def test_global_semantic_admin_can_authorise_the_blocked_taxonomy_edge(
    authority_service,
    monkeypatch,
) -> None:
    service, _delegations, _receipts = authority_service
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    with service.override_current_actor("#V#semantic_admin", None):
        decision = service.authorise_ontology_mutation(
            _intent(service, source_id=GRADUATE_STUDENT_TAXONOMY_TUPLE[0])
        )
    assert decision.allowed is True


def test_motivating_candidature_and_taxonomy_changes_execute_with_read_back(
    authority_service,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the original 24 effects, not merely their policy decisions."""

    from src.backend.db.repositories import concepts_repository
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.security import access_control
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import relationship_write_service as relationship

    service, _delegations, _receipts = authority_service
    database = mongomock.MongoClient()["tier3_motivating_canonical_effects"]
    concepts = database["concepts"]
    concept_ids = {
        *BLOCKED_CANDIDATURE_SOURCE_IDS,
        "#V#student",
        "#V#graduate_student",
        "#V#university_student",
    }
    concepts.insert_many(
        [
            {"concept_id": concept_id, "relationships": {}}
            for concept_id in sorted(concept_ids)
        ]
    )
    monkeypatch.setattr(
        concepts_repository,
        "get_concepts_collection",
        lambda: concepts,
    )
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: concepts)
    monkeypatch.setattr(command, "can_access_concept", lambda _item: True)

    aliases = {
        "#V#is_an_instance_of": "is_an_instance_of",
        "#V#is_a_type_of": "is_a_type_of",
        "#V#has_instance": "has_instance",
        "#V#has_subtype": "has_subtype",
    }
    inverse_map = {
        "is_an_instance_of": "has_instance",
        "has_instance": "is_an_instance_of",
        "is_a_type_of": "has_subtype",
        "has_subtype": "is_a_type_of",
    }
    relationship_kinds = frozenset(inverse_map)

    def normalise(predicate: str) -> str:
        return aliases.get(predicate, predicate)

    monkeypatch.setattr(relationship, "normalise_structural_predicate", normalise)
    monkeypatch.setattr(
        relationship,
        "is_structural_predicate",
        lambda predicate: normalise(predicate) in relationship_kinds,
    )
    monkeypatch.setattr(
        relationship,
        "get_relationship_kinds_set",
        lambda: relationship_kinds,
    )
    monkeypatch.setattr(
        relationship,
        "get_structural_inverse_map",
        lambda: inverse_map,
    )
    monkeypatch.setattr(command, "normalise_structural_predicate", normalise)
    monkeypatch.setattr(
        command,
        "is_structural_predicate",
        lambda predicate: normalise(predicate) in relationship_kinds,
    )
    monkeypatch.setattr(command, "get_structural_inverse_map", lambda: inverse_map)
    monkeypatch.setattr(
        relationship,
        "_sync_relationship_extent_index_for_sources",
        lambda _items: None,
    )
    monkeypatch.setattr(
        relationship,
        "_invalidate_workflow_routing_projection_for_relationship_change",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        relationship,
        "_emit_relationship_mutation_event",
        lambda **_kwargs: None,
    )

    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service,
        "resolve_live_semantic_roles",
        lambda _actor: (global_role,),
    )

    results = []
    with service.override_current_actor("#V#semantic_admin", None):
        for index, (source_id, predicate, target_id) in enumerate(
            (*BLOCKED_CANDIDATURE_TUPLES, GRADUATE_STUDENT_TAXONOMY_TUPLE)
        ):
            results.append(
                catalogue._add_relationship(
                    source_id=source_id,
                    predicate=predicate,
                    target=target_id,
                    request_id=f"tier3-motivating-effect:{index}",
                )
            )

    assert len(results) == 24
    assert all(result.get("success") is True for result in results)
    assert all(
        result.get("authority_receipt", {}).get("status") == "succeeded"
        and result.get("canonical_read_back", {}).get("relationship_present") is True
        and result.get("canonical_read_back", {}).get("inverse_relationship_required")
        is False
        and result.get("canonical_read_back", {}).get("inverse_relationship_present")
        is None
        for result in results
    )
    for source_id in BLOCKED_CANDIDATURE_SOURCE_IDS:
        assert concepts.find_one({"concept_id": source_id})["relationships"][
            "is_an_instance_of"
        ] == ["#V#student"]
    assert (
        "has_instance"
        not in concepts.find_one({"concept_id": "#V#student"})["relationships"]
    )
    assert concepts.find_one({"concept_id": "#V#graduate_student"})["relationships"][
        "is_a_type_of"
    ] == ["#V#university_student"]
    assert (
        "has_subtype"
        not in concepts.find_one({"concept_id": "#V#university_student"})[
            "relationships"
        ]
    )


@pytest.mark.parametrize(
    ("source_context", "destination"),
    (
        (
            "historical",
            ("global", None),
        ),
        (
            "mixed",
            ("organisation", "#V#organisation_a"),
        ),
    ),
)
def test_historical_or_mixed_scope_adoption_requires_global_source_authority(
    authority_service,
    monkeypatch,
    source_context: str,
    destination: tuple[str, str | None],
) -> None:
    service, _delegations, _receipts = authority_service
    if source_context == "historical":
        source = service.PublicationContext(
            service.PublicationContextKind.HISTORICAL,
            source="legacy_specific_to_org",
            historical_predicates=("specific_to_org",),
        )
    else:
        source = service.PublicationContext(
            service.PublicationContextKind.MIXED,
            source="conflicting_scope_edges",
            historical_predicates=("#V#specific_to_organisation", "specific_to_org"),
        )
    destination_context = (
        service.PublicationContext.global_context()
        if destination[0] == "global"
        else service.PublicationContext.organisation(str(destination[1]))
    )
    intent = service.OntologyMutationIntent(
        operation="scope.change",
        publication_context=destination_context,
        source_contexts=(source,),
        target_concept_ids=("#V#legacy_concept",),
        tool_name="change_concept_publication_scope",
        delta={"fixture": source_context},
    )

    org_only = _role(
        service,
        role=service.ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        organisation="#V#organisation_a",
    )
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (org_only,)
    )
    with service.override_current_actor("#V#semantic_admin", "#V#organisation_a"):
        denied = service.authorise_ontology_mutation(intent)
    assert denied.allowed is False
    assert denied.reason_code == "global_ontology_admin_authority_required"

    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    if destination[0] == "organisation":
        roles = (global_role, org_only)
    else:
        roles = (global_role,)
    monkeypatch.setattr(service, "resolve_live_semantic_roles", lambda _actor: roles)
    with service.override_current_actor("#V#semantic_admin", "#V#organisation_a"):
        allowed = service.authorise_ontology_mutation(intent)
    assert allowed.allowed is True
    assert allowed.reason_code == "semantic_ontology_authority_verified"


def test_scope_change_does_not_leak_inaccessible_historical_target(monkeypatch) -> None:
    from src.backend.services import ontology_scope_change_service as scope

    monkeypatch.setattr(scope, "can_access_concept", lambda _concept_id: False)
    monkeypatch.setattr(
        scope,
        "scope_read_back",
        lambda _concept_id: pytest.fail("must not inspect an inaccessible target"),
    )

    with pytest.raises(PermissionError, match="ontology_scope_target_not_accessible"):
        scope.change_concept_publication_scope(
            concept_id="#V#historical_concept",
            destination_kind="global",
            expected_scope_fingerprint="opaque-fingerprint",
            request_id="fixture-no-leak",
        )


def test_global_admin_can_adopt_historical_scope_with_canonical_read_back(
    authority_service,
    monkeypatch,
) -> None:
    """The repair path removes legacy scope only after explicit global authority."""

    from src.backend.services import ontology_scope_change_service as scope

    service, _delegations, _receipts = authority_service
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    monkeypatch.setattr(scope, "can_access_concept", lambda _concept_id: True)
    before = {
        "concept_id": "#V#legacy_concept",
        "publication_context": {
            "kind": "historical",
            "concept_id": None,
            "source": "legacy_specific_to_org",
            "historical_predicates": ["specific_to_org"],
        },
        "scope_edges": {"specific_to_org": ["#V#organisation_a"]},
        "scope_fingerprint": "scope-before",
    }
    after = {
        **before,
        "publication_context": {
            "kind": "global",
            "concept_id": None,
            "source": "resolved",
        },
        "scope_edges": {},
        "scope_fingerprint": "scope-after",
    }
    # The execution lock rechecks the optimistic snapshot immediately before
    # crossing the first visibility-edge mutation boundary.
    reads = iter((before, before, after))
    removed: list[dict[str, Any]] = []
    monkeypatch.setattr(scope, "scope_read_back", lambda _concept_id: next(reads))
    monkeypatch.setattr(
        scope,
        "remove_relationship",
        lambda **kwargs: removed.append(kwargs) or {"success": True, "removed": True},
    )

    with service.override_current_actor("#V#semantic_admin", None):
        result = scope.change_concept_publication_scope(
            concept_id="#V#legacy_concept",
            destination_kind="global",
            expected_scope_fingerprint="scope-before",
            request_id="adopt-historical-fixture",
            preview=False,
        )

    assert result["success"] is True
    assert result["authority_decision"]["allowed"] is True
    assert result["canonical_read_back"] == after
    assert removed == [
        {
            "source_id": "#V#legacy_concept",
            "predicate": "specific_to_org",
            "target": "#V#organisation_a",
            "mode": "soft_delete",
            "cascade": "warn",
            "dry_run": False,
            "confirmed": True,
            "reason": "ontology scope transition",
            "request_id": "adopt-historical-fixture:remove:0",
        }
    ]


def _issue_delegation(service, *, issued_at: datetime):
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    intent = _intent(service)
    return (
        intent,
        global_role,
        service.issue_agent_delegation(
            intent=intent,
            grantor_actor_concept_id="#V#semantic_admin",
            grantor_organisation_concept_id=None,
            delegate_concept_id="#V#von_system",
            audience="adaptive_turn",
            tool_name="add_relationship",
            effect_id="tier3-effect",
            turn_id="tier3-turn",
            now=issued_at,
        ),
    )


@pytest.mark.parametrize(
    ("override", "expected_reason"),
    (
        ({"audience": "wrong"}, "ontology_delegation_audience_mismatch"),
        ({"effect_id": "wrong-effect"}, "ontology_delegation_effect_id_mismatch"),
        (
            {"tool_name": "remove_relationship"},
            "ontology_delegation_tool_name_mismatch",
        ),
        (
            {"target_id": "#V#different_target"},
            "ontology_delegation_intent_fingerprint_mismatch",
        ),
    ),
)
def test_delegation_is_exact_over_audience_effect_tool_and_target(
    authority_service,
    monkeypatch,
    override: dict[str, str],
    expected_reason: str,
) -> None:
    service, _delegations, _receipts = authority_service
    issued_at = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    _issued_intent, _role_evidence, grant = _issue_delegation(
        service,
        issued_at=issued_at,
    )
    attempted_intent = _intent(
        service,
        target_id=override.get("target_id", "#V#university_student"),
    )
    invocation = service.OntologyInvocationContext(
        surface="ordinary_turn",
        executing_agent_concept_id="#V#von_system",
        audience=override.get("audience", "adaptive_turn"),
        delegation_id=grant["delegation_id"],
        effect_id=override.get("effect_id", "tier3-effect"),
    )
    if "tool_name" in override:
        attempted_intent = service.OntologyMutationIntent(
            **{**attempted_intent.__dict__, "tool_name": override["tool_name"]}
        )
    decision = service.verify_agent_delegation(
        delegation_id=grant["delegation_id"],
        intent=attempted_intent,
        invocation=invocation,
        actor_concept_id="#V#semantic_admin",
        organisation_concept_id=None,
        now=issued_at + timedelta(seconds=1),
    )
    assert decision.allowed is False
    assert decision.reason_code == expected_reason


def test_delegation_rejects_a_different_actor_even_when_the_agent_is_exact(
    authority_service,
    monkeypatch,
) -> None:
    service, _delegations, _receipts = authority_service
    issued_at = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    intent, _role_evidence, grant = _issue_delegation(service, issued_at=issued_at)
    decision = service.verify_agent_delegation(
        delegation_id=grant["delegation_id"],
        intent=intent,
        invocation=service.OntologyInvocationContext(
            surface="ordinary_turn",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
            delegation_id=grant["delegation_id"],
            effect_id="tier3-effect",
            turn_id="tier3-turn",
        ),
        actor_concept_id="#V#other_actor",
        organisation_concept_id=None,
        now=issued_at + timedelta(seconds=1),
    )
    assert decision.allowed is False
    assert decision.reason_code == "ontology_delegation_actor_mismatch"


@pytest.mark.parametrize("turn_id", (None, "different-turn"))
def test_same_turn_delegation_rejects_missing_or_different_turn(
    authority_service,
    monkeypatch,
    turn_id: str | None,
) -> None:
    service, _delegations, _receipts = authority_service
    issued_at = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    intent, _role_evidence, grant = _issue_delegation(service, issued_at=issued_at)

    decision = service.verify_agent_delegation(
        delegation_id=grant["delegation_id"],
        intent=intent,
        invocation=service.OntologyInvocationContext(
            surface="ordinary_turn",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
            delegation_id=grant["delegation_id"],
            effect_id="tier3-effect",
            turn_id=turn_id,
        ),
        actor_concept_id="#V#semantic_admin",
        organisation_concept_id=None,
        now=issued_at + timedelta(seconds=1),
    )

    assert decision.allowed is False
    assert decision.reason_code == "ontology_delegation_turn_mismatch"


def test_explicitly_revoked_delegation_cannot_start_the_next_agent_effect(
    authority_service,
    monkeypatch,
) -> None:
    service, _delegations, _receipts = authority_service
    issued_at = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    intent, _role_evidence, grant = _issue_delegation(service, issued_at=issued_at)
    revoked = service.revoke_agent_delegation(
        delegation_id=grant["delegation_id"],
        acting_actor_concept_id="#V#semantic_admin",
        reason="tier3-revocation",
    )
    assert revoked["status"] == "revoked"
    decision = service.verify_agent_delegation(
        delegation_id=grant["delegation_id"],
        intent=intent,
        invocation=service.OntologyInvocationContext(
            surface="ordinary_turn",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
            delegation_id=grant["delegation_id"],
            effect_id="tier3-effect",
        ),
        actor_concept_id="#V#semantic_admin",
        organisation_concept_id=None,
        now=issued_at + timedelta(seconds=1),
    )
    assert decision.allowed is False
    assert decision.reason_code == "ontology_delegation_revoked"


def test_only_the_exact_grantor_can_revoke_a_delegation_without_disclosure(
    authority_service,
    monkeypatch,
) -> None:
    service, delegations, _receipts = authority_service
    issued_at = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    _intent_value, _role_evidence, grant = _issue_delegation(
        service, issued_at=issued_at
    )

    for delegation_id in (grant["delegation_id"], "unknown-delegation"):
        with pytest.raises(
            PermissionError,
            match="ontology_delegation_revoke_authority_required",
        ):
            service.revoke_agent_delegation(
                delegation_id=delegation_id,
                acting_actor_concept_id="#V#different_global_admin",
                reason="foreign-revocation-attempt",
            )

    stored = delegations.find_one({"delegation_id": grant["delegation_id"]})
    assert stored is not None
    assert stored["status"] == "active"

    revoked = service.revoke_agent_delegation(
        delegation_id=grant["delegation_id"],
        acting_actor_concept_id="#V#semantic_admin",
        reason="grantor-revocation",
    )
    assert revoked["status"] == "revoked"


def test_workflow_and_internal_gateway_surfaces_deny_global_effect_without_delegation(
    authority_service,
    monkeypatch,
) -> None:
    """Neither route acquires shared publication authority from actor context."""

    service, _delegations, _receipts = authority_service
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    intent = _intent(service)
    contexts = (
        service.OntologyInvocationContext(
            surface="workflow",
            executing_agent_concept_id="#V#von_system",
            audience="workflow",
            effect_id="workflow:fixture:state:add_relationship",
            workflow_id="#V#fixture_workflow",
        ),
        service.OntologyInvocationContext(
            surface="internal_mcp",
            executing_agent_concept_id="#V#von_system",
            audience="internal_mcp",
            effect_id="mcp:fixture:add_relationship",
        ),
    )
    for invocation in contexts:
        with (
            service.override_current_actor("#V#semantic_admin", None),
            service.bind_ontology_invocation(**invocation.__dict__),
        ):
            decision = service.authorise_ontology_mutation(intent)
        assert decision.allowed is False
        assert decision.reason_code == "ontology_agent_delegation_required"


def test_actor_bound_workflow_private_effect_uses_live_direct_authority(
    authority_service,
    monkeypatch,
) -> None:
    service, _delegations, _receipts = authority_service
    monkeypatch.setattr(
        service,
        "_gateway_actor_trust_source",
        lambda: "preexisting_authenticated_or_workflow_context",
    )
    intent = _intent(
        service,
        context=service.PublicationContext.user("#V#actor"),
        source_contexts=(service.PublicationContext.user("#V#actor"),),
    )
    with (
        service.override_current_actor("#V#actor", None),
        service.bind_ontology_invocation(
            surface="workflow",
            executing_agent_concept_id="#V#von_system",
            audience="workflow",
            effect_id="workflow:fixture:private:add_relationship",
            workflow_id="#V#fixture_workflow",
            actor_bound_workflow_effect=True,
        ),
    ):
        decision = service.authorise_ontology_mutation(intent)

    assert decision.allowed is True
    assert decision.delegation_id is None
    assert decision.reason_code == "semantic_ontology_authority_verified"
    assert decision.trust_source == "preexisting_authenticated_or_workflow_context"


def test_workflow_binds_a_stable_effect_identity_and_passes_the_grant_opaquely() -> (
    None
):
    from src.backend.services.ontology_publication_authority_service import (
        current_ontology_invocation,
    )
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.trace_model import WorkflowExecutionTrace
    from src.backend.workflows.workflow_mcp_tool_actions import (
        _ontology_invocation_context,
    )

    request = WorkflowActionRequest(
        action_id="workflow_mcp.invoke_tool",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=object(),
            ontology_delegation_id="server-issued-only",
        ),
        data={},
        trace=WorkflowExecutionTrace(
            workflow_id="#V#publication_workflow",
            execution_id="execution-that-may-change-after-resume",
            instance_id="durable-instance-123",
        ),
        workflow_id="#V#publication_workflow",
        workflow_state_id="publish-edge",
    )
    with _ontology_invocation_context(
        request=request,
        resolved_tool_name="add_relationship",
    ):
        invocation = current_ontology_invocation()
    assert invocation is not None
    assert invocation.executing_agent_concept_id == "#V#von_system"
    assert invocation.audience == "workflow"
    assert invocation.delegation_id == "server-issued-only"
    assert invocation.effect_id == (
        "workflow:#V#publication_workflow:durable-instance-123:"
        "publish-edge:add_relationship"
    )
    assert invocation.workflow_id == "#V#publication_workflow"
    assert invocation.actor_bound_workflow_effect is True


def test_workflow_effect_identity_is_stable_per_instance_and_distinct_between_instances() -> (
    None
):
    from src.backend.services.ontology_publication_authority_service import (
        current_ontology_invocation,
    )
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.trace_model import WorkflowExecutionTrace
    from src.backend.workflows.workflow_mcp_tool_actions import (
        _ontology_invocation_context,
    )

    def _effect_id(*, instance_id: str, execution_id: str) -> str:
        request = WorkflowActionRequest(
            action_id="workflow_mcp.invoke_tool",
            inputs={},
            environment=WorkflowEnvironment(llm_client=object()),
            data={},
            trace=WorkflowExecutionTrace(
                workflow_id="#V#publication_workflow",
                execution_id=execution_id,
                instance_id=instance_id,
            ),
            workflow_id="#V#publication_workflow",
            workflow_state_id="publish-edge",
        )
        with _ontology_invocation_context(
            request=request,
            resolved_tool_name="add_relationship",
        ):
            invocation = current_ontology_invocation()
        assert invocation is not None
        assert invocation.effect_id is not None
        return invocation.effect_id

    first = _effect_id(instance_id="instance-a", execution_id="execution-a")
    resumed = _effect_id(instance_id="instance-a", execution_id="execution-b")
    separate = _effect_id(instance_id="instance-b", execution_id="execution-a")

    assert resumed == first
    assert separate != first


def test_workflow_effect_identity_is_distinct_for_untraced_top_level_executions() -> (
    None
):
    from src.backend.services.ontology_publication_authority_service import (
        current_ontology_invocation,
    )
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
        WorkflowExecutionScope,
    )
    from src.backend.workflows.workflow_mcp_tool_actions import (
        _ontology_invocation_context,
    )

    def _effect_id(scope: WorkflowExecutionScope) -> str:
        request = WorkflowActionRequest(
            action_id="workflow_mcp.invoke_tool",
            inputs={},
            environment=WorkflowEnvironment(llm_client=object()),
            data={},
            workflow_id="#V#publication_workflow",
            workflow_state_id="publish-edge",
            execution_scope=scope,
        )
        with _ontology_invocation_context(
            request=request,
            resolved_tool_name="add_relationship",
        ):
            invocation = current_ontology_invocation()
        assert invocation is not None
        assert invocation.effect_id is not None
        return invocation.effect_id

    first_scope = WorkflowExecutionScope()
    second_scope = WorkflowExecutionScope()

    first = _effect_id(first_scope)
    repeated = _effect_id(first_scope)
    separate = _effect_id(second_scope)

    assert repeated == first
    assert separate != first


def test_nested_durable_effect_replay_after_write_reuses_the_exact_receipt(
    authority_service,
    monkeypatch,
) -> None:
    """A child replay before checkpoint persistence must not repeat its write."""

    from src.backend.services.ontology_publication_authority_service import (
        current_ontology_invocation,
    )
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )
    from src.backend.workflows.trace_model import (
        WorkflowExecutionTrace,
        build_child_workflow_effect_identity_metadata,
    )
    from src.backend.workflows.workflow_mcp_tool_actions import (
        _ontology_invocation_context,
    )

    def _nested_effect_id(
        *,
        root_instance_id: str,
        child_execution_id: str,
        item_index: int,
    ) -> str:
        parent_trace = WorkflowExecutionTrace(
            workflow_id="#V#reference_set_workflow",
            execution_id=f"root-execution-for-{child_execution_id}",
            instance_id=root_instance_id,
        )
        child_trace = WorkflowExecutionTrace(
            workflow_id="#V#reference_item_workflow",
            execution_id=child_execution_id,
            metadata=build_child_workflow_effect_identity_metadata(
                parent_trace,
                invocation_kind="for_each",
                parent_workflow_id="#V#reference_set_workflow",
                parent_state_id="dispatch_reference_items",
                child_workflow_id="#V#reference_item_workflow",
                discriminator=item_index,
            ),
        )
        request = WorkflowActionRequest(
            action_id="workflow_mcp.invoke_tool",
            inputs={},
            environment=WorkflowEnvironment(llm_client=object()),
            data={},
            trace=child_trace,
            workflow_id="#V#reference_item_workflow",
            workflow_state_id="create_article",
        )
        with _ontology_invocation_context(
            request=request,
            resolved_tool_name="create_concepts",
        ):
            invocation = current_ontology_invocation()
        assert invocation is not None
        assert invocation.effect_id is not None
        return invocation.effect_id

    first_effect_id = _nested_effect_id(
        root_instance_id="durable-root-instance",
        child_execution_id="child-execution-before-crash",
        item_index=0,
    )
    replay_effect_id = _nested_effect_id(
        root_instance_id="durable-root-instance",
        child_execution_id="child-execution-after-resume",
        item_index=0,
    )
    sibling_effect_id = _nested_effect_id(
        root_instance_id="durable-root-instance",
        child_execution_id="child-execution-sibling",
        item_index=1,
    )
    other_instance_effect_id = _nested_effect_id(
        root_instance_id="other-durable-root-instance",
        child_execution_id="child-execution-before-crash",
        item_index=0,
    )

    assert replay_effect_id == first_effect_id
    assert sibling_effect_id != first_effect_id
    assert other_instance_effect_id != first_effect_id

    service, _delegations, receipts = authority_service
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    first_intent = _intent(service, idempotency_key=first_effect_id)
    replay_intent = _intent(service, idempotency_key=replay_effect_id)
    mutation_calls = {"count": 0}

    def mutate() -> dict[str, Any]:
        mutation_calls["count"] += 1
        return {"success": True, "changed": True}

    with service.override_current_actor("#V#semantic_admin", None):
        first = service.execute_authorised_ontology_mutation(
            intent=first_intent,
            mutate=mutate,
            read_before=lambda: {"present": False},
            read_back=lambda: {"present": True},
        )
        replay = service.execute_authorised_ontology_mutation(
            intent=replay_intent,
            mutate=mutate,
            read_before=lambda: {"present": False},
            read_back=lambda: {"present": True},
        )

    assert first["success"] is True
    assert replay["success"] is True
    assert replay["idempotent_replay"] is True
    assert mutation_calls["count"] == 1
    assert receipts.count_documents({}) == 1


def test_concurrent_same_idempotency_key_executes_the_canonical_effect_once(
    authority_service,
    monkeypatch,
) -> None:
    """A second caller must reconcile the receipt, never replay an in-flight write."""

    service, _delegations, receipts = authority_service
    global_role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    monkeypatch.setattr(
        service, "resolve_live_semantic_roles", lambda _actor: (global_role,)
    )
    intent = _intent(service, idempotency_key="same-effect-under-concurrency")
    first_mutation_started = Event()
    release_first_mutation = Event()
    calls = {"count": 0}
    calls_lock = Lock()
    results: list[dict[str, Any]] = []

    def mutate() -> dict[str, Any]:
        with calls_lock:
            calls["count"] += 1
            ordinal = calls["count"]
        if ordinal == 1:
            first_mutation_started.set()
            assert release_first_mutation.wait(timeout=2)
        return {"success": True, "changed": True, "ordinal": ordinal}

    def invoke() -> None:
        with service.override_current_actor("#V#semantic_admin", None):
            results.append(
                service.execute_authorised_ontology_mutation(
                    intent=intent,
                    mutate=mutate,
                    read_before=lambda: {"present": False},
                    read_back=lambda: {"present": True},
                )
            )

    first = Thread(target=invoke)
    second = Thread(target=invoke)
    first.start()
    assert first_mutation_started.wait(timeout=2)
    second.start()
    second.join(timeout=2)
    release_first_mutation.set()
    first.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls["count"] == 1
    assert receipts.count_documents({}) == 1
    assert any(result.get("idempotent_replay") is True for result in results)


def test_authority_is_rechecked_immediately_before_the_canonical_effect(
    authority_service,
    monkeypatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command

    service, _delegations, _receipts = authority_service
    intent = _intent(service, idempotency_key="live-reauthorisation-fixture")
    role = _role(service, role=service.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE)
    initially_allowed = service.OntologyAuthorityDecision(
        allowed=True,
        decision_id="initial-authority",
        reason_code="semantic_ontology_authority_verified",
        message="Initially authorised.",
        actor_concept_id="#V#semantic_admin",
        organisation_concept_id=None,
        intent_fingerprint=intent.fingerprint,
        role_evidence=(role,),
    )
    revoked_before_effect = service.OntologyAuthorityDecision(
        allowed=False,
        decision_id="effect-boundary-authority",
        reason_code="global_ontology_admin_authority_required",
        message="Authority was revoked before the effect.",
        actor_concept_id="#V#semantic_admin",
        organisation_concept_id=None,
        intent_fingerprint=intent.fingerprint,
    )
    monkeypatch.setattr(
        service, "authorise_ontology_mutation", lambda _intent: initially_allowed
    )
    monkeypatch.setattr(
        command, "authorise_ontology_mutation", lambda _intent: revoked_before_effect
    )
    monkeypatch.setattr(command, "build_ontology_mutation_intent", lambda **_kw: intent)
    monkeypatch.setattr(
        command,
        "canonical_read_back_for_method",
        lambda **_kw: {"relationship_present": False},
    )
    monkeypatch.setattr(command, "ontology_authority_resource_keys", lambda _intent: ())
    mutate_calls = {"count": 0}

    def mutate() -> dict[str, Any]:
        mutate_calls["count"] += 1
        return {"success": True, "changed": True}

    with service.override_current_actor("#V#semantic_admin", None):
        result = command.execute_governed_ontology_method(
            method_name="add_relationship",
            arguments={},
            mutate=mutate,
        )

    assert result["success"] is False
    assert result["effect_status"] == "not_started"
    assert result["error_code"] == "global_ontology_admin_authority_required"
    assert mutate_calls["count"] == 0
