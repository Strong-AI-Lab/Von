from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta

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
from src.backend.services import von_login_email_governance_service as service


@pytest.fixture()
def governed_login_email(monkeypatch):
    collection = mongomock.MongoClient()["test_von_db"][
        "von_login_email_mutation_receipts"
    ]
    memberships: dict[tuple[str, str], str] = {
        ("#V#actor", "#V#organisation"): "owner",
        ("#V#target", "#V#organisation"): "member",
    }
    bindings: dict[str, set[str]] = {
        "#V#target": {"existing@example.org"},
    }
    calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        service,
        "get_von_login_email_receipts_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        service,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#actor", "trusted_in_process"),
    )
    monkeypatch.setattr(
        service,
        "ontology_authority_membership_mutation_barrier",
        nullcontext,
    )
    monkeypatch.setattr(
        service,
        "von_login_email_binding_barrier",
        lambda _email: nullcontext(),
    )
    monkeypatch.setattr(
        service,
        "is_live_von_operational_administrator",
        lambda _actor_id: False,
    )

    def resolve_membership(user_id, organisation_id):
        role = memberships.get((user_id, organisation_id))
        if role is None:
            return None
        return {
            "user_concept_id": user_id,
            "organisation_concept_id": organisation_id,
            "role": role,
        }

    def get_memberships(user_id):
        return {
            "memberships": [
                {
                    "user_concept_id": member_id,
                    "organisation_concept_id": organisation_id,
                    "role": role,
                }
                for (member_id, organisation_id), role in memberships.items()
                if member_id == user_id
            ]
        }

    def list_emails(user_concept_id):
        return sorted(bindings.get(user_concept_id, set()))

    def list_user_ids(email):
        normalised_email = str(email).strip().casefold()
        return sorted(
            user_id
            for user_id, user_bindings in bindings.items()
            if normalised_email in user_bindings
        )

    def bind_email(*, user_concept_id, email, provenance=None):
        del provenance
        normalised_email = str(email).strip().casefold()
        calls.append(("bind", user_concept_id, normalised_email))
        user_bindings = bindings.setdefault(user_concept_id, set())
        created = normalised_email not in user_bindings
        user_bindings.add(normalised_email)
        return {
            "success": True,
            "user_concept_id": user_concept_id,
            "email": normalised_email,
            "relation_created": created,
            "read_back_user_concept_id": user_concept_id,
        }

    def remove_email(*, user_concept_id, email):
        normalised_email = str(email).strip().casefold()
        calls.append(("remove", user_concept_id, normalised_email))
        user_bindings = bindings.setdefault(user_concept_id, set())
        removed = normalised_email in user_bindings
        user_bindings.discard(normalised_email)
        return {
            "success": True,
            "user_concept_id": user_concept_id,
            "email": normalised_email,
            "removed": removed,
        }

    monkeypatch.setattr(
        service, "resolve_user_organisation_membership", resolve_membership
    )
    monkeypatch.setattr(service, "get_user_memberships", get_memberships)
    monkeypatch.setattr(service, "list_von_login_emails_for_user", list_emails)
    monkeypatch.setattr(service, "list_von_login_email_user_ids", list_user_ids)
    monkeypatch.setattr(service, "bind_von_login_email", bind_email)
    monkeypatch.setattr(service, "remove_von_login_email", remove_email)

    return {
        "bindings": bindings,
        "calls": calls,
        "collection": collection,
        "memberships": memberships,
    }


def _manage(**overrides):
    arguments = {
        "action": "bind",
        "user_concept_id": "#V#target",
        "organisation_concept_id": "#V#organisation",
        "email": "new@example.org",
        "request_id": "request-1",
        "reason": "Manage an organisation member's login identity.",
        "acting_actor_concept_id": "#V#actor",
    }
    arguments.update(overrides)
    return service.manage_von_login_email(**arguments)


@pytest.mark.parametrize("actor_role", ["owner", "admin"])
def test_owner_and_admin_can_read_pre_existing_binding_in_exact_organisation(
    governed_login_email,
    actor_role,
):
    governed_login_email["memberships"][("#V#actor", "#V#organisation")] = actor_role

    result = service.get_von_login_email_bindings(
        user_concept_id="#V#target",
        organisation_concept_id="#V#organisation",
        acting_actor_concept_id="#V#actor",
    )

    assert result["success"] is True
    assert result["user_concept_id"] == "#V#target"
    assert result["organisation_concept_id"] == "#V#organisation"
    assert result["login_emails"] == ["existing@example.org"]
    assert result["total_count"] == 1
    assert result["authority_decision"]["allowed"] is True
    assert result["authority_decision"]["actor_role"] == actor_role
    assert result["authority_decision"]["required_permissions"] == ["MANAGE_MEMBERS"]
    assert governed_login_email["calls"] == []


def test_admin_read_holds_membership_barrier_through_authority_and_binding_read(
    governed_login_email,
    monkeypatch,
):
    barrier_state = {"held": False}
    original_resolve = service.resolve_user_organisation_membership
    original_list = service.list_von_login_emails_for_user

    @contextmanager
    def tracked_barrier():
        assert barrier_state["held"] is False
        barrier_state["held"] = True
        try:
            yield
        finally:
            barrier_state["held"] = False

    def resolve_inside_barrier(user_id, organisation_id):
        assert barrier_state["held"] is True
        return original_resolve(user_id, organisation_id)

    def list_inside_barrier(user_id):
        assert barrier_state["held"] is True
        return original_list(user_id)

    monkeypatch.setattr(
        service,
        "ontology_authority_membership_mutation_barrier",
        tracked_barrier,
    )
    monkeypatch.setattr(
        service,
        "resolve_user_organisation_membership",
        resolve_inside_barrier,
    )
    monkeypatch.setattr(
        service,
        "list_von_login_emails_for_user",
        list_inside_barrier,
    )

    result = service.get_von_login_email_bindings(
        user_concept_id="#V#target",
        organisation_concept_id="#V#organisation",
        acting_actor_concept_id="#V#actor",
    )

    assert result["success"] is True
    assert result["login_emails"] == ["existing@example.org"]
    assert barrier_state["held"] is False


def test_bind_is_receipted_canonically_read_back_and_idempotent(
    governed_login_email,
):
    first = _manage()
    second = _manage()

    assert first["success"] is True
    assert first["effect_status"] == "succeeded"
    assert first["mutation_outcome"] == "succeeded"
    assert first["outcome_finality"] == "terminal_canonical_read_back"
    assert first["changed"] is True
    assert first["canonical_read_back"]["user_concept_id"] == "#V#target"
    assert first["canonical_read_back"]["email"] == "new@example.org"
    assert first["canonical_read_back"]["binding_present"] is True
    assert first["governance_receipt"]["status"] == "succeeded"
    assert second == first
    assert governed_login_email["calls"] == [("bind", "#V#target", "new@example.org")]


def test_binding_that_already_exists_is_a_successful_no_op(governed_login_email):
    result = _manage(email=" Existing@Example.ORG ")

    assert result["success"] is True
    assert result["effect_status"] == "succeeded"
    assert result["changed"] is False
    assert result["canonical_read_back"]["binding_present"] is True
    assert result["canonical_read_back"]["email"] == "existing@example.org"
    assert result["governance_receipt"]["status"] == "succeeded"
    assert governed_login_email["calls"] == []


def test_remove_is_receipted_canonically_read_back_and_idempotent(
    governed_login_email,
):
    first = _manage(
        action="remove",
        email="existing@example.org",
        request_id="remove-1",
    )
    second = _manage(
        action="remove",
        email="existing@example.org",
        request_id="remove-1",
    )

    assert first["success"] is True
    assert first["effect_status"] == "succeeded"
    assert first["changed"] is True
    assert first["canonical_read_back"]["binding_present"] is False
    assert first["governance_receipt"]["status"] == "succeeded"
    assert second == first
    assert governed_login_email["calls"] == [
        ("remove", "#V#target", "existing@example.org")
    ]


def test_member_is_denied_without_leaking_existing_login_email(
    governed_login_email,
):
    governed_login_email["memberships"][("#V#actor", "#V#organisation")] = "member"

    read = service.get_von_login_email_bindings(
        user_concept_id="#V#target",
        organisation_concept_id="#V#organisation",
        acting_actor_concept_id="#V#actor",
    )
    mutation = _manage(email="candidate@example.org")

    assert read["success"] is False
    assert (
        read["error_code"]
        == "organisation_login_identity_management_authority_required"
    )
    assert "existing@example.org" not in json.dumps(read, sort_keys=True)
    assert mutation["success"] is False
    assert (
        mutation["error_code"]
        == "organisation_login_identity_management_authority_required"
    )
    assert "existing@example.org" not in json.dumps(mutation, sort_keys=True)
    assert mutation["governance_receipt"]["status"] == "denied"
    assert governed_login_email["calls"] == []


def test_membership_barrier_contention_cannot_trigger_unauthorised_read(
    governed_login_email,
    monkeypatch,
):
    governed_login_email["memberships"][("#V#actor", "#V#organisation")] = "member"

    @contextmanager
    def busy_membership_barrier():
        raise service.OntologyMutationResourceBusy("busy")
        yield

    monkeypatch.setattr(
        service,
        "ontology_authority_membership_mutation_barrier",
        busy_membership_barrier,
    )
    monkeypatch.setattr(
        service,
        "_binding_observation",
        lambda *_args, **_kwargs: pytest.fail(
            "authority failure must not trigger a binding read"
        ),
    )

    result = _manage(email="existing@example.org", request_id="busy-membership")

    assert result["success"] is False
    assert result["error_code"] == "ontology_mutation_resource_busy"
    assert result["effect_status"] == "not_started"
    assert result["canonical_read_back"] is None
    assert "existing@example.org" not in json.dumps(
        result.get("canonical_read_back"), sort_keys=True
    )


def test_target_must_be_a_member_of_the_exact_organisation(governed_login_email):
    governed_login_email["memberships"].pop(("#V#target", "#V#organisation"))

    result = _manage()

    assert result["success"] is False
    assert result["error_code"] == "target_organisation_membership_required"
    assert result["effect_status"] == "not_started"
    assert result["governance_receipt"]["status"] == "denied"
    assert governed_login_email["calls"] == []


def test_actor_authority_is_exact_to_the_requested_organisation(
    governed_login_email,
):
    governed_login_email["memberships"][("#V#target", "#V#other_org")] = "member"

    result = _manage(organisation_concept_id="#V#other_org")

    assert result["success"] is False
    assert (
        result["error_code"]
        == "organisation_login_identity_management_authority_required"
    )
    assert result["authority_decision"]["organisation_concept_id"] == "#V#other_org"
    assert result["governance_receipt"]["status"] == "denied"
    assert governed_login_email["calls"] == []


def test_cross_organisation_principal_mutation_requires_authority_in_every_org(
    governed_login_email,
):
    governed_login_email["memberships"][("#V#target", "#V#other_org")] = "member"

    result = _manage()

    assert result["success"] is False
    assert (
        result["error_code"] == "cross_organisation_login_identity_authority_required"
    )
    assert result["effect_status"] == "not_started"
    assert result["governance_receipt"]["status"] == "denied"
    assert governed_login_email["calls"] == []


def test_cross_org_denial_does_not_reveal_global_email_occupancy(
    governed_login_email,
):
    governed_login_email["memberships"][("#V#target", "#V#other_org")] = "member"
    governed_login_email["bindings"]["#V#outside_person"] = {"claimed@example.org"}

    occupied = _manage(
        email="claimed@example.org",
        request_id="cross-org-occupied",
    )
    unoccupied = _manage(
        email="unclaimed@example.org",
        request_id="cross-org-unoccupied",
    )

    for result in (occupied, unoccupied):
        assert result["success"] is False
        assert (
            result["error_code"]
            == "cross_organisation_login_identity_authority_required"
        )
        assert result["canonical_read_back"]["binding_present"] is False
        assert result["governance_receipt"]["status"] == "denied"
    assert "#V#outside_person" not in json.dumps(occupied, sort_keys=True)
    assert governed_login_email["calls"] == []


@pytest.mark.parametrize(
    "malformed_inventory",
    [
        {"memberships": "not-a-list"},
        {"memberships": [None]},
        {"memberships": [{"role": "member"}]},
        {"memberships": []},
    ],
)
def test_global_mutation_authority_fails_closed_on_malformed_inventory(
    governed_login_email,
    monkeypatch,
    malformed_inventory,
):
    monkeypatch.setattr(
        service,
        "get_user_memberships",
        lambda _user_id: malformed_inventory,
    )

    result = _manage(request_id="malformed-membership-inventory")

    assert result["success"] is False
    assert (
        result["error_code"] == "cross_organisation_login_identity_authority_required"
    )
    assert result["effect_status"] == "not_started"
    assert result["authority_decision"]["membership_inventory_complete"] is False
    assert result["governance_receipt"]["status"] == "denied"
    assert governed_login_email["calls"] == []


def test_pre_existing_bind_requires_global_identity_authority_to_certify_no_op(
    governed_login_email,
):
    governed_login_email["memberships"][("#V#target", "#V#other_org")] = "member"

    result = _manage(email="existing@example.org")

    assert result["success"] is False
    assert result["changed"] is False
    assert (
        result["error_code"] == "cross_organisation_login_identity_authority_required"
    )
    assert result["canonical_read_back"]["binding_present"] is True
    assert governed_login_email["calls"] == []


def test_ambiguous_pre_existing_bind_is_not_certified_as_success(
    governed_login_email,
):
    governed_login_email["bindings"]["#V#outside_person"] = {"existing@example.org"}

    result = _manage(email="existing@example.org")

    assert result["success"] is False
    assert result["error_code"] == "von_login_email_binding_unavailable"
    assert result["effect_status"] == "not_started"
    assert result["canonical_read_back"]["binding_present"] is True
    assert result["canonical_read_back"]["binding_unambiguous"] is False
    assert "#V#outside_person" not in json.dumps(result, sort_keys=True)
    assert governed_login_email["calls"] == []


def test_conflicting_binding_does_not_disclose_other_identity(
    governed_login_email,
):
    governed_login_email["bindings"]["#V#outside_person"] = {"claimed@example.org"}

    result = _manage(email="claimed@example.org")

    assert result["success"] is False
    assert result["error_code"] == "von_login_email_binding_unavailable"
    assert "#V#outside_person" not in json.dumps(result, sort_keys=True)
    assert result["canonical_read_back"] == {
        "user_concept_id": "#V#target",
        "email": "claimed@example.org",
        "binding_present": False,
    }
    assert result["governance_receipt"]["status"] == "denied"
    serialised = json.dumps(result, sort_keys=True)
    assert "bound_user_count" not in serialised
    assert "binding_unambiguous" not in serialised
    assert governed_login_email["calls"] == []


def test_foreign_bound_remove_no_op_does_not_reveal_account_occupancy(
    governed_login_email,
):
    governed_login_email["bindings"]["#V#outside_person"] = {"claimed@example.org"}

    result = _manage(
        action="remove",
        email="claimed@example.org",
        request_id="remove-foreign",
    )

    assert result["success"] is True
    assert result["changed"] is False
    assert result["canonical_read_back"] == {
        "user_concept_id": "#V#target",
        "email": "claimed@example.org",
        "binding_present": False,
    }
    serialised = json.dumps(result, sort_keys=True)
    assert "#V#outside_person" not in serialised
    assert "bound_user_count" not in serialised
    assert "binding_unambiguous" not in serialised


def test_cross_org_mutation_succeeds_when_actor_administers_every_target_org(
    governed_login_email,
):
    governed_login_email["memberships"].update(
        {
            ("#V#target", "#V#other_org"): "member",
            ("#V#actor", "#V#other_org"): "admin",
        }
    )

    result = _manage()

    assert result["success"] is True
    assert result["changed"] is True
    assert result["authority_decision"]["all_target_organisations_covered"] is True
    assert governed_login_email["calls"] == [("bind", "#V#target", "new@example.org")]


def test_global_operational_admin_can_manage_member_without_org_membership(
    governed_login_email,
    monkeypatch,
):
    governed_login_email["memberships"].pop(("#V#actor", "#V#organisation"))
    monkeypatch.setattr(
        service,
        "is_live_von_operational_administrator",
        lambda _actor_id: True,
    )

    result = _manage()

    assert result["success"] is True
    assert result["authority_decision"]["authority_scope"] == (
        "global_operational_administrator"
    )
    assert governed_login_email["calls"] == [("bind", "#V#target", "new@example.org")]


def test_gateway_payload_identity_cannot_authorise_login_email_mutation(
    governed_login_email,
):
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "manage_von_login_email_binding",
        {
            "action": "bind",
            "user_concept_id": "#V#target",
            "organisation_concept_id": "#V#organisation",
            "email": "new@example.org",
            "request_id": "untrusted-payload-attempt",
        },
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "client_supplied_identity_is_not_authority"
    assert governed_login_email["calls"] == []
    assert governed_login_email["collection"].count_documents({}) == 0


def test_management_receipt_read_is_actor_bound(
    governed_login_email,
    monkeypatch,
):
    mutation = _manage()
    receipt_id = mutation["governance_receipt"]["receipt_id"]

    owned = service.get_von_login_email_management_receipt(
        receipt_id=receipt_id,
        acting_actor_concept_id="#V#actor",
    )
    assert owned["success"] is True
    assert owned["governance_receipt"]["canonical_read_back"]["binding_present"] is True

    monkeypatch.setattr(
        service,
        "get_effective_user_concept_id_with_source",
        lambda: ("#V#other_actor", "trusted_in_process"),
    )
    hidden = service.get_von_login_email_management_receipt(
        receipt_id=receipt_id,
        acting_actor_concept_id="#V#other_actor",
    )

    assert hidden["success"] is False
    assert hidden["error_code"] == "von_login_email_management_receipt_not_found"


def test_exception_after_binding_is_reconciled_from_canonical_state(
    governed_login_email,
    monkeypatch,
):
    def bind_then_raise(*, user_concept_id, email, provenance=None):
        del provenance
        normalised_email = str(email).strip().casefold()
        governed_login_email["bindings"].setdefault(user_concept_id, set()).add(
            normalised_email
        )
        governed_login_email["calls"].append(
            ("bind", user_concept_id, normalised_email)
        )
        raise RuntimeError("transport interrupted")

    monkeypatch.setattr(service, "bind_von_login_email", bind_then_raise)

    result = _manage()

    assert result["success"] is True
    assert result["canonical_read_back"]["binding_present"] is True
    assert result["reconciled_after_exception"] == "RuntimeError"
    assert result["governance_receipt"]["status"] == "succeeded"


def test_value_error_after_binding_is_reconciled_from_canonical_state(
    governed_login_email,
    monkeypatch,
):
    def bind_then_raise(*, user_concept_id, email, provenance=None):
        del provenance
        normalised_email = str(email).strip().casefold()
        governed_login_email["bindings"].setdefault(user_concept_id, set()).add(
            normalised_email
        )
        governed_login_email["calls"].append(
            ("bind", user_concept_id, normalised_email)
        )
        raise ValueError("post-write response failed")

    monkeypatch.setattr(service, "bind_von_login_email", bind_then_raise)

    result = _manage()

    assert result["success"] is True
    assert result["canonical_read_back"]["binding_present"] is True
    assert result["reconciled_after_exception"] == "ValueError"
    assert result["governance_receipt"]["status"] == "succeeded"


def test_receipt_finalisation_outage_preserves_canonical_success_and_reconciles(
    governed_login_email,
    monkeypatch,
):
    original_finalise = service._finalise_receipt

    def unavailable_finalisation(*_args, **_kwargs):
        raise RuntimeError("receipt store update unavailable")

    monkeypatch.setattr(service, "_finalise_receipt", unavailable_finalisation)

    result = _manage()

    assert result["success"] is True
    assert result["canonical_read_back"]["binding_present"] is True
    assert result["outcome_finality"] == "canonical_effect_verified_receipt_pending"
    assert result["receipt_finalisation"] == "pending_reconciliation"
    assert result["governance_receipt"]["status"] == "pending"
    assert result["governance_receipt"]["effect_phase"] == "effect_may_have_started"

    monkeypatch.setattr(service, "_finalise_receipt", original_finalise)
    monkeypatch.setattr(service, "_pending_receipt_is_stale", lambda _receipt: True)
    reconciled = service.get_von_login_email_management_receipt(
        receipt_id=result["governance_receipt"]["receipt_id"],
        acting_actor_concept_id="#V#actor",
    )

    assert reconciled["success"] is True
    assert reconciled["governance_receipt"]["status"] == "succeeded"
    assert reconciled["governance_receipt"]["effect_phase"] == "terminal"
    assert reconciled["response_projection"]["success"] is True
    assert reconciled["response_projection"]["reconciled_from_pending_receipt"] is True


def test_stale_pre_effect_receipt_cannot_become_success_from_later_state(
    governed_login_email,
    monkeypatch,
):
    original_finalise = service._finalise_receipt
    governed_login_email["memberships"][("#V#actor", "#V#organisation")] = "member"

    def unavailable_finalisation(*_args, **_kwargs):
        raise RuntimeError("receipt store update unavailable")

    monkeypatch.setattr(service, "_finalise_receipt", unavailable_finalisation)
    denied = _manage(request_id="denied-finalisation-outage")

    assert denied["success"] is False
    assert denied["receipt_finalisation"] == "pending_reconciliation"
    assert denied["governance_receipt"]["status"] == "pending"
    assert denied["governance_receipt"]["effect_phase"] == "pre_effect"

    # A later independent change matching the requested postcondition must not
    # make a receipt for an effect that never crossed authority look successful.
    governed_login_email["bindings"]["#V#target"].add("new@example.org")
    governed_login_email["memberships"][("#V#actor", "#V#organisation")] = "owner"
    monkeypatch.setattr(service, "_finalise_receipt", original_finalise)
    monkeypatch.setattr(service, "_pending_receipt_is_stale", lambda _receipt: True)

    reconciled = service.get_von_login_email_management_receipt(
        receipt_id=denied["governance_receipt"]["receipt_id"],
        acting_actor_concept_id="#V#actor",
    )

    assert reconciled["success"] is True
    assert reconciled["governance_receipt"]["status"] == "failed"
    assert reconciled["governance_receipt"]["effect_phase"] == "terminal"
    assert reconciled["governance_receipt"]["canonical_read_back"] is None
    assert reconciled["response_projection"]["success"] is False
    assert (
        reconciled["response_projection"]["error_code"]
        == "von_login_email_mutation_not_started"
    )
    assert reconciled["response_projection"].get("canonical_read_back") is None


def test_pending_receipt_staleness_accepts_mongo_naive_utc_datetime(
    governed_login_email,
):
    collection = governed_login_email["collection"]
    collection.insert_one(
        {
            "receipt_id": "naive-time-receipt",
            "updated_at": datetime.now(UTC) - timedelta(minutes=2),
        }
    )
    stored = collection.find_one({"receipt_id": "naive-time-receipt"})

    assert stored["updated_at"].tzinfo is None
    assert service._pending_receipt_is_stale(stored) is True


def test_pending_effect_reconciliation_requires_current_exact_org_authority(
    governed_login_email,
    monkeypatch,
):
    original_finalise = service._finalise_receipt

    def unavailable_finalisation(*_args, **_kwargs):
        raise RuntimeError("receipt store update unavailable")

    monkeypatch.setattr(service, "_finalise_receipt", unavailable_finalisation)
    result = _manage(request_id="effect-finalisation-outage")

    assert result["success"] is True
    assert result["governance_receipt"]["status"] == "pending"
    assert result["governance_receipt"]["effect_phase"] == "effect_may_have_started"

    governed_login_email["memberships"][("#V#actor", "#V#organisation")] = "member"
    monkeypatch.setattr(service, "_finalise_receipt", original_finalise)
    monkeypatch.setattr(service, "_pending_receipt_is_stale", lambda _receipt: True)

    reconciled = service.get_von_login_email_management_receipt(
        receipt_id=result["governance_receipt"]["receipt_id"],
        acting_actor_concept_id="#V#actor",
    )

    assert reconciled["success"] is True
    assert reconciled["governance_receipt"]["status"] == "indeterminate"
    assert reconciled["governance_receipt"]["effect_phase"] == "terminal"
    assert reconciled["governance_receipt"]["canonical_read_back"] is None
    assert reconciled["response_projection"]["success"] is False
    assert (
        reconciled["response_projection"]["error_code"]
        == "von_login_email_receipt_reconciliation_authority_required"
    )
    assert reconciled["response_projection"]["canonical_read_back"] is None
    assert "binding_present" not in json.dumps(reconciled, sort_keys=True)


def test_internal_catalogue_exposes_admin_login_email_lifecycle_only_internally():
    catalogue = build_default_catalogue()

    read = catalogue.get("get_von_login_email_bindings")
    mutation = catalogue.get("manage_von_login_email_binding")
    receipt = catalogue.get("get_von_login_email_management_receipt")

    assert read is not None and read.category == "read"
    assert mutation is not None and mutation.category == "write"
    assert mutation.ordinary_turn_effect is True
    assert mutation.write_guardrail == {"ordinary_turn_explicit_request": True}
    assert receipt is not None and receipt.category == "read"
    assert read.ordinary_turn_trusted_argument_bindings == {
        "acting_actor_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
    }
    assert mutation.ordinary_turn_trusted_argument_bindings == {
        "acting_actor_concept_id": "actor_user_concept_id",
        "organisation_concept_id": "actor_organisation_concept_id",
        "request_id": "turn_id",
    }

    contracts = get_canonical_tool_registry()
    for name in (
        "get_von_login_email_bindings",
        "manage_von_login_email_binding",
        "get_von_login_email_management_receipt",
    ):
        exposure = contracts[name].exposure
        assert exposure.expose_in_internal_catalogue is True
        assert exposure.expose_in_vontology_stdio is False
        assert exposure.expose_in_vonrag_stdio is False
        assert exposure.expose_in_manifest is False
