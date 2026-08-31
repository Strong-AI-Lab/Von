from __future__ import annotations

from contextlib import nullcontext

import mongomock
import pytest

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    InternalMCPTransport,
)
from src.backend.integrations.internal_mcp.tool_contract_registry import (
    get_canonical_tool_registry,
)
from src.backend.services import organisation_membership_governance_service as service


@pytest.fixture()
def governed_membership(monkeypatch):
    collection = mongomock.MongoClient()["test_von_db"][
        "organisation_membership_mutation_receipts"
    ]
    state: dict[tuple[str, str], str] = {("#V#actor", "#V#organisation"): "admin"}
    calls: list[tuple[str, str, str, str | None]] = []

    monkeypatch.setattr(
        service,
        "get_organisation_membership_receipts_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        service,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#actor", "trusted_in_process"),
    )
    monkeypatch.setattr(
        service, "ontology_authority_membership_mutation_barrier", nullcontext
    )
    monkeypatch.setattr(
        service,
        "organisation_membership_scope_barrier",
        lambda _user_id, _organisation_id: nullcontext(),
    )

    def resolve(user_id, organisation_id):
        role = state.get((user_id, organisation_id))
        if role is None:
            return None
        return {
            "user_concept_id": user_id,
            "organisation_concept_id": organisation_id,
            "role": role,
        }

    def create(user_id, organisation_id, role="member"):
        calls.append(("add", user_id, organisation_id, role))
        state[(user_id, organisation_id)] = role
        return {"relationship_created": True}

    def update(user_id, organisation_id, role):
        calls.append(("set_role", user_id, organisation_id, role))
        state[(user_id, organisation_id)] = role
        return {"role_updated": True}

    def remove(user_id, organisation_id):
        calls.append(("remove", user_id, organisation_id, None))
        state.pop((user_id, organisation_id), None)
        return {"membership_removed": True}

    monkeypatch.setattr(service, "resolve_user_organisation_membership", resolve)
    monkeypatch.setattr(service, "create_organisation_membership", create)
    monkeypatch.setattr(service, "update_user_role", update)
    monkeypatch.setattr(service, "remove_organisation_membership", remove)
    monkeypatch.setattr(
        service,
        "get_organisation_members",
        lambda _organisation_id, role_filter=None: {
            "members": [],
            "role_filter": role_filter,
        },
    )
    return {"collection": collection, "state": state, "calls": calls}


def _manage(**overrides):
    arguments = {
        "action": "add",
        "user_concept_id": "#V#target",
        "organisation_concept_id": "#V#organisation",
        "request_id": "request-1",
        "role": "member",
        "acting_actor_concept_id": "#V#actor",
    }
    arguments.update(overrides)
    return service.manage_organisation_membership(**arguments)


def test_add_is_receipted_canonically_read_back_and_idempotent(
    governed_membership,
):
    first = _manage()
    second = _manage()

    assert first["success"] is True
    assert first["effect_status"] == "succeeded"
    assert first["changed"] is True
    assert first["canonical_read_back"] == {
        "user_concept_id": "#V#target",
        "organisation_concept_id": "#V#organisation",
        "membership_present": True,
        "role": "member",
    }
    assert first["governance_receipt"]["status"] == "succeeded"
    assert second == first
    assert governed_membership["calls"] == [
        ("add", "#V#target", "#V#organisation", "member")
    ]


def test_request_id_cannot_be_reused_for_a_different_intent(governed_membership):
    assert _manage()["success"] is True

    conflict = _manage(action="remove", role=None)

    assert conflict["success"] is False
    assert conflict["error_code"] == "idempotency_request_conflict"
    assert len(governed_membership["calls"]) == 1


def test_operational_membership_permission_is_required(
    governed_membership, monkeypatch
):
    governed_membership["state"][("#V#actor", "#V#organisation")] = "member"

    denied = _manage()

    assert denied["success"] is False
    assert (
        denied["error_code"] == "organisation_membership_management_authority_required"
    )
    assert denied["authority_decision"]["authority_source"] == (
        "represented_organisation_membership_role"
    )
    assert (
        denied["authority_decision"]["semantic_ontology_authority_considered"] is False
    )
    assert governed_membership["calls"] == []
    assert denied["governance_receipt"]["status"] == "denied"


def test_role_assignment_requires_manage_roles(governed_membership, monkeypatch):
    monkeypatch.setattr(
        service,
        "get_effective_permissions",
        lambda _role: {"MANAGE_MEMBERS"},
    )

    denied = _manage(role="admin")

    assert denied["success"] is False
    assert denied["authority_decision"]["required_permissions"] == [
        "MANAGE_MEMBERS",
        "MANAGE_ROLES",
    ]
    assert governed_membership["calls"] == []


def test_add_does_not_silently_change_an_existing_role(governed_membership):
    governed_membership["state"][("#V#target", "#V#organisation")] = "admin"

    result = _manage()

    assert result["success"] is False
    assert result["error_code"] == "membership_already_exists_with_different_role"
    assert governed_membership["state"][("#V#target", "#V#organisation")] == "admin"
    assert governed_membership["calls"] == []


def test_last_owner_cannot_be_removed(governed_membership, monkeypatch):
    governed_membership["state"][("#V#target", "#V#organisation")] = "owner"
    governed_membership["state"][("#V#actor", "#V#organisation")] = "owner"
    monkeypatch.setattr(
        service,
        "get_organisation_members",
        lambda _organisation_id, role_filter=None: {
            "members": [{"user_concept_id": "#V#target", "role": "owner"}],
            "role_filter": role_filter,
        },
    )

    denied = _manage(action="remove", role=None)

    assert denied["success"] is False
    assert denied["error_code"] == "last_organisation_owner_required"
    assert governed_membership["calls"] == []


def test_exception_after_effect_is_reconciled_from_canonical_state(
    governed_membership, monkeypatch
):
    governed_membership["state"][("#V#target", "#V#organisation")] = "member"

    def remove_then_raise(user_id, organisation_id):
        governed_membership["state"].pop((user_id, organisation_id), None)
        governed_membership["calls"].append(("remove", user_id, organisation_id, None))
        raise RuntimeError("transport interrupted")

    monkeypatch.setattr(service, "remove_organisation_membership", remove_then_raise)

    result = _manage(action="remove", role=None)

    assert result["success"] is True
    assert result["canonical_read_back"]["membership_present"] is False
    assert result["reconciled_after_exception"] == "RuntimeError"
    assert result["governance_receipt"]["status"] == "succeeded"


def test_semantic_authority_precondition_denial_is_not_reported_as_crossed(
    governed_membership, monkeypatch
):
    governed_membership["state"][("#V#target", "#V#organisation")] = "member"

    def deny_remove(_user_id, _organisation_id):
        raise PermissionError(
            "organisation_ontology_authority_must_be_revoked_before_membership"
        )

    monkeypatch.setattr(service, "remove_organisation_membership", deny_remove)

    result = _manage(action="remove", role=None)

    assert result["success"] is False
    assert result["effect_status"] == "not_started"
    assert result["mutation_outcome"] == "not_started"
    assert result["changed"] is False
    assert result["error_code"] == (
        "organisation_ontology_authority_must_be_revoked_before_membership"
    )
    assert result["canonical_read_back"]["membership_present"] is True


def test_pending_receipt_can_be_reconciled_after_finalisation_outage(
    governed_membership, monkeypatch
):
    finalise = service._finalise_receipt

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("receipt store update unavailable")

    monkeypatch.setattr(service, "_finalise_receipt", unavailable)
    result = _manage()

    assert result["success"] is True
    assert result["canonical_read_back"]["membership_present"] is True
    assert result["receipt_finalisation"] == "pending_reconciliation"
    assert result["governance_receipt"]["status"] == "pending"

    monkeypatch.setattr(service, "_finalise_receipt", finalise)
    reconciled = service.get_organisation_membership_receipt(
        receipt_id=result["governance_receipt"]["receipt_id"],
        acting_actor_concept_id="#V#actor",
    )

    assert reconciled["success"] is True
    assert reconciled["governance_receipt"]["status"] == "succeeded"
    assert reconciled["response_projection"]["reconciled_from_pending_receipt"] is True


def test_legacy_header_identity_cannot_authorise_membership(
    governed_membership, monkeypatch
):
    monkeypatch.setattr(
        service,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#actor", "legacy_identity_header"),
    )

    denied = _manage()

    assert denied["success"] is False
    assert denied["error_code"] == "authenticated_actor_context_required"
    assert governed_membership["collection"].count_documents({}) == 0


def test_gateway_payload_identity_cannot_become_membership_authority(
    governed_membership,
):
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "manage_organisation_membership",
        {
            "action": "add",
            "user_concept_id": "#V#actor",
            "organisation_concept_id": "#V#organisation",
            "request_id": "untrusted-payload-attempt",
            "role": "member",
        },
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "client_supplied_identity_is_not_authority"
    assert governed_membership["calls"] == []
    assert governed_membership["collection"].count_documents({}) == 0


def test_receipt_read_is_actor_bound(governed_membership, monkeypatch):
    mutation = _manage()
    receipt_id = mutation["governance_receipt"]["receipt_id"]

    owned = service.get_organisation_membership_receipt(
        receipt_id=receipt_id,
        acting_actor_concept_id="#V#actor",
    )
    assert owned["success"] is True
    assert owned["governance_receipt"]["canonical_read_back"]["role"] == "member"

    monkeypatch.setattr(
        service,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#other_actor", "trusted_in_process"),
    )
    hidden = service.get_organisation_membership_receipt(
        receipt_id=receipt_id,
        acting_actor_concept_id="#V#other_actor",
    )
    assert hidden["success"] is False
    assert hidden["error_code"] == "organisation_membership_receipt_not_found"


def test_identity_options_use_membership_extent_not_type_extent(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#actor", "trusted_in_process"),
    )
    monkeypatch.setattr(
        service,
        "get_effective_organisation_concept_id",
        lambda: "#V#second_org",
    )
    monkeypatch.setattr(
        service,
        "get_user_memberships",
        lambda _actor: {
            "memberships": [
                {"organisation_concept_id": "#V#typed_org", "role": "admin"},
                {"organisation_concept_id": "#V#second_org", "role": "member"},
            ]
        },
    )
    monkeypatch.setattr(
        service,
        "get_vontology_node_and_descendant_ids",
        lambda _type_id: ["#V#von_user_organisation", "#V#lab_type"],
    )
    concepts = {
        "#V#typed_org": {
            "concept_id": "#V#typed_org",
            "name": "Typed Organisation",
            "relationships": {"is_an_instance_of": ["#V#lab_type"]},
        },
        "#V#second_org": {
            "concept_id": "#V#second_org",
            "name": "Membership-Only Organisation",
            "relationships": {"is_an_instance_of": ["#V#other_type"]},
        },
    }
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        lambda query: concepts.get(query["concept_id"]),
    )

    result = service.list_identity_organisation_options(
        acting_actor_concept_id="#V#actor"
    )

    assert result["success"] is True
    assert result["total_count"] == 2
    assert result["complete"] is True
    assert result["organisations"] == [
        {
            "concept_id": "#V#typed_org",
            "name": "Typed Organisation",
            "role": "admin",
            "selectable": True,
            "target_resolved": True,
            "is_von_user_organisation": True,
            "is_current": False,
        },
        {
            "concept_id": "#V#second_org",
            "name": "Membership-Only Organisation",
            "role": "member",
            "selectable": True,
            "target_resolved": True,
            "is_von_user_organisation": False,
            "is_current": True,
        },
    ]


def test_internal_mcp_catalogue_exposes_actor_bound_membership_lifecycle():
    catalogue = build_default_catalogue()

    discovery = catalogue.get("list_identity_organisation_options")
    mutation = catalogue.get("manage_organisation_membership")
    receipt = catalogue.get("get_organisation_membership_receipt")

    assert discovery is not None and discovery.category == "read"
    assert mutation is not None and mutation.category == "write"
    assert mutation.ordinary_turn_effect is True
    assert receipt is not None and receipt.category == "read"
    for definition in (discovery, mutation, receipt):
        assert definition.ordinary_turn_trusted_argument_bindings == {
            "acting_actor_concept_id": "actor_user_concept_id"
        }

    contracts = get_canonical_tool_registry()
    for name in (
        "list_identity_organisation_options",
        "manage_organisation_membership",
        "get_organisation_membership_receipt",
    ):
        exposure = contracts[name].exposure
        assert exposure.expose_in_internal_catalogue is True
        assert exposure.expose_in_vontology_stdio is False
        assert exposure.expose_in_vonrag_stdio is False
        assert exposure.expose_in_manifest is False
