from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

TRUSTED_USER = "#V#trusted_user"
TRUSTED_ORG = "#V#trusted_org"
TRUSTED_NAMESPACE = "#V#trusted_user@trusted_org"
CLAIMED_USER = "#V#payload_user"
CLAIMED_ORG = "#V#payload_org"
CLAIMED_NAMESPACE = "#V#payload_user@payload_org"


def _gateway(*, trusted_operator: bool = False):
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )

    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=trusted_operator,
    )


def _route_payloads(
    identity: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    actor = dict(identity or {})
    return {
        "upsert_scoped_assertion": {
            "subject_concept_id": "#V#subject",
            "predicate": "#V#related_to",
            "target_concept_id": "#V#target",
            **actor,
        },
        "retract_scoped_assertion": {
            "assertion_id": "ska_existing",
            **actor,
        },
        "list_scoped_assertions": actor,
        "get_text_relations": {"concept_id": "#V#subject", **actor},
        "get_text_relations_summary": {"concept_id": "#V#subject", **actor},
        "fetch_concept": {
            "concept_id": "#V#subject",
            **actor,
        },
        "find_relations_with_argument": {
            "concept_id": "#V#subject",
            **actor,
        },
    }


def _install_route_fakes(monkeypatch: pytest.MonkeyPatch):
    from src.backend.security import access_control

    calls: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def actor() -> tuple[str | None, str | None]:
        return (
            access_control.get_effective_user_concept_id(),
            access_control.get_effective_organisation_concept_id(),
        )

    def fake_upsert(**kwargs):
        calls["upsert"].append({**kwargs, "_actor": actor()})
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_test",
            "assertion": {
                "assertion_id": "ska_test",
                "object_kind": "concept",
                "predicate": kwargs["predicate"],
            },
            "canonical_publication": False,
        }

    def fake_retract(**kwargs):
        calls["retract"].append({**kwargs, "_actor": actor()})
        assertion = {
            "assertion_id": kwargs["assertion_id"],
            "object_kind": "concept",
            "status": "retracted",
        }
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": kwargs["assertion_id"],
            "assertion": assertion,
            "canonical_read_back": assertion,
            "canonical_publication": False,
        }

    def fake_list(**kwargs):
        calls["list"].append({**kwargs, "_actor": actor()})
        if actor() == (None, None):
            return []
        if kwargs.get("limit") == 101:
            return [
                {"assertion_id": f"ska_{index:03d}"}
                for index in range(101)
            ]
        return [{"assertion_id": "ska_visible"}]

    def fake_page(**kwargs):
        if kwargs.get("offset") == 40:
            calls["list"].append({**kwargs, "_actor": actor()})
            return {
                "items": [
                    {"assertion_id": "ska_page_1"},
                    {"assertion_id": "ska_page_2"},
                ],
                "returned": 2,
                "limit": kwargs.get("limit"),
                "offset": 40,
                "has_more": True,
                "next_offset": 42,
                "visibility_filtered": True,
                "counts_are_lower_bounds": True,
            }
        items = fake_list(**kwargs)
        return {
            "items": items,
            "returned": len(items),
            "limit": kwargs.get("limit"),
            "offset": kwargs.get("offset", 0),
            "has_more": False,
            "next_offset": None,
            "visibility_filtered": False,
            "counts_are_lower_bounds": False,
        }

    def fake_texts(**kwargs):
        calls["text"].append({**kwargs, "_actor": actor()})
        return []

    def fake_summary(concept_id, **kwargs):
        calls["summary"].append(
            {"concept_id": concept_id, **kwargs, "_actor": actor()}
        )
        return {
            "success": True,
            "concept_id": concept_id,
            "context_view": kwargs.get("context_view"),
            "groups": [],
            "groups_found": 0,
            "total_relations_scanned": 0,
            "max_relation_ids_per_group": kwargs[
                "max_relation_ids_per_group"
            ],
        }

    def fake_get_concept(*, concept_id):
        calls["concept"].append({"concept_id": concept_id, "_actor": actor()})
        return {"concept_id": concept_id}

    def fake_find_relations(**kwargs):
        calls["relations"].append({**kwargs, "_actor": actor()})
        return {
            "concept_id": kwargs["concept_id"],
            "context_view": kwargs["context_view"],
            "total_hits": 0,
            "hits": [],
            "paging": {"limit": 0, "offset": 0},
        }

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.upsert_scoped_assertion",
        fake_upsert,
    )
    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.retract_scoped_assertion",
        fake_retract,
    )
    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.list_visible_scoped_assertions",
        fake_list,
    )
    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service."
        "list_visible_scoped_assertions_page",
        fake_page,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        fake_texts,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_text_relations_summary",
        fake_summary,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.describe_concept_access",
        lambda _concept_id: {"exists": True, "accessible": True},
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id_exact",
        fake_get_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.enrich_concept_with_text_relations",
        lambda concept: concept,
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.detect_vacuous_typing",
        lambda _concept: None,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_relation_service.find_relations_with_argument",
        fake_find_relations,
    )
    return calls


def _assert_all_routes_called_as(
    calls: dict[str, list[dict[str, Any]]],
    *,
    expected_actor: tuple[str, str],
) -> None:
    assert calls["upsert"][-1]["_actor"] == expected_actor
    assert calls["retract"][-1]["_actor"] == expected_actor
    assert calls["list"][-2]["_actor"] == expected_actor
    assert calls["text"][-1]["_actor"] == expected_actor
    assert calls["summary"][-1]["_actor"] == expected_actor
    assert calls["concept"][-1]["_actor"] == expected_actor
    assert calls["relations"][-1]["_actor"] == expected_actor


def test_generic_gateway_payload_identity_degrades_generic_reads_to_base(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = _install_route_fakes(monkeypatch)
    gateway = _gateway()
    identity = {
        "user_concept_id": CLAIMED_USER,
        "organisation_concept_id": CLAIMED_ORG,
        "namespace": CLAIMED_NAMESPACE,
    }

    payloads = _route_payloads(identity)
    mutation = payloads.pop("upsert_scoped_assertion")
    retraction = payloads.pop("retract_scoped_assertion")
    scoped_list = payloads.pop("list_scoped_assertions")
    results = {
        method_name: gateway.invoke(method_name, payload).payload
        for method_name, payload in payloads.items()
    }

    for method_name in (
        "get_text_relations",
        "get_text_relations_summary",
        "fetch_concept",
        "find_relations_with_argument",
    ):
        assert results[method_name]["context_view"] == "base_publication"
    assert results["fetch_concept"]["scoped_assertions"] == []
    assert calls["list"] == []
    for route_calls in calls.values():
        for call in route_calls:
            assert call["_actor"] == (None, None)

    for method_name, payload in (
        ("upsert_scoped_assertion", mutation),
        ("retract_scoped_assertion", retraction),
        ("list_scoped_assertions", scoped_list),
    ):
        result = gateway.invoke(method_name, payload).payload
        assert result["success"] is False, method_name
        assert (
            result["error_code"] == "authenticated_actor_context_required"
        ), method_name


def test_actorless_generic_gateway_preserves_base_compatible_reads(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = _install_route_fakes(monkeypatch)
    gateway = _gateway()
    payloads = _route_payloads()
    mutation = payloads.pop("upsert_scoped_assertion")
    retraction = payloads.pop("retract_scoped_assertion")
    scoped_list = payloads.pop("list_scoped_assertions")

    results = {
        method_name: gateway.invoke(method_name, payload).payload
        for method_name, payload in payloads.items()
    }
    denied_mutation = gateway.invoke(
        "upsert_scoped_assertion",
        mutation,
    ).payload
    denied_list = gateway.invoke(
        "list_scoped_assertions",
        scoped_list,
    ).payload
    denied_retraction = gateway.invoke(
        "retract_scoped_assertion",
        retraction,
    ).payload

    assert all(result.get("success") is not False for result in results.values())
    assert results["fetch_concept"]["scoped_assertions"] == []
    assert results["fetch_concept"]["scoped_assertions_truncated"] is False
    assert results["fetch_concept"]["context_view"] == "base_publication"
    assert results["get_text_relations"]["context_view"] == "base_publication"
    assert (
        results["get_text_relations_summary"]["context_view"]
        == "base_publication"
    )
    assert (
        results["find_relations_with_argument"]["context_view"]
        == "base_publication"
    )
    assert denied_mutation["error_code"] == "authenticated_actor_context_required"
    assert (
        denied_retraction["error_code"]
        == "authenticated_actor_context_required"
    )
    assert denied_list["error_code"] == "authenticated_actor_context_required"
    for route_calls in calls.values():
        for call in route_calls:
            assert call["_actor"] == (None, None)


def test_actorless_base_binding_keeps_global_visibility_enforcement_enabled():
    from types import SimpleNamespace

    from src.backend.integrations.internal_mcp import catalogue as cat
    from src.backend.security.access_control import (
        should_enforce_access_control,
    )

    actorless_scope = SimpleNamespace(
        user_concept_id=None,
        organisation_concept_id=None,
    )
    assert should_enforce_access_control() is False
    with cat._bind_internal_mcp_scoped_assertion_actor(actorless_scope):
        assert should_enforce_access_control() is True
    assert should_enforce_access_control() is False


def test_authenticated_actor_is_bound_across_every_scoped_assertion_path(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security.access_control import override_current_actor

    calls = _install_route_fakes(monkeypatch)
    gateway = _gateway()

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        results = {
            method_name: gateway.invoke(method_name, payload).payload
            for method_name, payload in _route_payloads().items()
        }

    assert results["upsert_scoped_assertion"]["success"] is True
    assert results["retract_scoped_assertion"]["success"] is True
    assert results["list_scoped_assertions"]["success"] is True
    assert results["list_scoped_assertions"]["paging"]["has_more"] is False
    assert results["get_text_relations"]["context_view"] == "actor_effective"
    assert (
        results["get_text_relations_summary"]["context_view"]
        == "actor_effective"
    )
    assert results["find_relations_with_argument"]["total_hits"] == 0
    concept = results["fetch_concept"]
    assert len(concept["scoped_assertions"]) == 100
    assert concept["scoped_assertion_count"] == 100
    assert concept["scoped_assertions_truncated"] is True
    assert concept["scoped_assertion_count_is_lower_bound"] is True

    _assert_all_routes_called_as(
        calls,
        expected_actor=(TRUSTED_USER, TRUSTED_ORG),
    )
    upsert = calls["upsert"][-1]
    assert upsert["acting_user_concept_id"] == TRUSTED_USER
    assert upsert["organisation_concept_id"] == TRUSTED_ORG
    assert upsert["namespace"] == TRUSTED_NAMESPACE
    assert upsert["canonical_publication"] is False
    explicit_list = calls["list"][-2]
    assert explicit_list["user_concept_id"] == TRUSTED_USER
    assert explicit_list["organisation_concept_id"] == TRUSTED_ORG
    assert calls["relations"][-1]["context_view"] == "actor_effective"


def test_authenticated_user_without_org_can_use_user_scoped_assertions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.security.access_control import override_current_actor

    calls = _install_route_fakes(monkeypatch)
    gateway = _gateway()

    with override_current_actor(TRUSTED_USER, None):
        upsert = gateway.invoke(
            "upsert_scoped_assertion",
            {
                "subject_concept_id": "#V#subject",
                "predicate": "#V#related_to",
                "target_concept_id": "#V#target",
                "scope_mode": "user",
            },
        ).payload
        listed = gateway.invoke("list_scoped_assertions", {}).payload

    assert upsert["success"] is True
    assert listed["success"] is True
    assert calls["upsert"][-1]["_actor"] == (TRUSTED_USER, None)
    assert calls["upsert"][-1]["acting_user_concept_id"] == TRUSTED_USER
    assert calls["upsert"][-1]["organisation_concept_id"] is None
    assert calls["upsert"][-1]["scope_mode"] == "user"
    assert calls["list"][-1]["_actor"] == (TRUSTED_USER, None)


def test_invalid_bare_predicate_exposes_one_exact_executable_scoped_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.security.access_control import override_current_actor

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.upsert_scoped_assertion",
        lambda **_kwargs: (_ for _ in ()).throw(
            ValueError("predicate must be an exact #V# concept ID")
        ),
    )
    accessed_concept_ids: list[str] = []

    def can_access(concept_id: str) -> bool:
        accessed_concept_ids.append(concept_id)
        return True

    monkeypatch.setattr(
        "src.backend.security.access_control.can_access_concept",
        can_access,
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service."
        "resolve_existing_predicate_value_kind",
        lambda predicate_id: (
            "concept" if predicate_id == "#V#affiliated_with" else None
        ),
    )

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "upsert_scoped_assertion",
            {
                "subject_concept_id": "#V#subject",
                "predicate": "affiliated_with",
                "target_concept_id": "#V#target",
                "scope_mode": "organisation",
                "evidence": {"source": "official staff page"},
            },
        ).payload

    assert result["success"] is False
    assert result["error_code"] == "invalid_scoped_assertion"
    assert result["effect_status"] == "not_started"
    assert result["mutation_outcome"] == "not_started"
    assert result["changed"] is False
    assert result["error_details"]["rejected_fields"] == ["predicate"]
    assert accessed_concept_ids == [
        "#V#subject",
        "#V#affiliated_with",
        "#V#target",
    ]
    recovery = result["recovery_affordances"]
    assert recovery == [
        {
            "action_type": (
                "retry_exact_scoped_assertion_with_canonical_predicate_id"
            ),
            "tool": "upsert_scoped_assertion",
            "arguments": {
                "subject_concept_id": "#V#subject",
                "predicate": "#V#affiliated_with",
                "target_concept_id": "#V#target",
                "scope_mode": "organisation",
                "evidence": {"source": "official staff page"},
            },
            "resolution": {
                "kind": "canonical_code_identifier",
                "input_predicate": "affiliated_with",
                "predicate_concept_id": "#V#affiliated_with",
                "value_kind": "concept",
            },
        }
    ]
    retry_arguments = recovery[0]["arguments"]
    assert "acting_user_concept_id" not in retry_arguments
    assert "organisation_concept_id" not in retry_arguments
    assert "namespace" not in retry_arguments
    assert "canonical_publication" not in retry_arguments


def test_invalid_bare_text_predicate_exposes_one_exact_executable_scoped_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.security.access_control import override_current_actor
    from src.backend.services.text_relation_predicate_validation_service import (
        TextRelationPredicateResolutionError,
    )

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.upsert_scoped_assertion",
        lambda **_kwargs: (_ for _ in ()).throw(
            TextRelationPredicateResolutionError(
                "unsupported_text_relation_predicate",
                "Unsupported text-relation predicate 'has_email'.",
                details={"predicate": "has_email"},
            )
        ),
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.can_access_concept",
        lambda _concept_id: True,
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service."
        "resolve_existing_predicate_value_kind",
        lambda predicate_id: "text" if predicate_id == "#V#has_email" else None,
    )

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "upsert_scoped_assertion",
            {
                "subject_concept_id": "#V#subject",
                "predicate": "has_email",
                "target_text": "person@example.test",
                "language": "en-NZ",
                "scope_mode": "organisation",
                "evidence": {"source": "official staff page"},
            },
        ).payload

    assert result["effect_status"] == "not_started"
    assert result["changed"] is False
    assert result["error_details"]["rejected_fields"] == ["predicate"]
    assert result["recovery_affordances"][0]["arguments"] == {
        "subject_concept_id": "#V#subject",
        "predicate": "#V#has_email",
        "target_text": "person@example.test",
        "language": "en-NZ",
        "scope_mode": "organisation",
        "evidence": {"source": "official staff page"},
    }
    assert result["recovery_affordances"][0]["resolution"]["value_kind"] == (
        "text"
    )


def test_predicate_recovery_probe_failure_preserves_not_started_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.security.access_control import override_current_actor

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.upsert_scoped_assertion",
        lambda **_kwargs: (_ for _ in ()).throw(
            ValueError("predicate must be an exact #V# concept ID")
        ),
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.can_access_concept",
        lambda _concept_id: (_ for _ in ()).throw(RuntimeError("read unavailable")),
    )

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "upsert_scoped_assertion",
            {
                "subject_concept_id": "#V#subject",
                "predicate": "affiliated_with",
                "target_concept_id": "#V#target",
            },
        ).payload

    assert result["success"] is False
    assert result["error_code"] == "invalid_scoped_assertion"
    assert result["effect_status"] == "not_started"
    assert result["mutation_outcome"] == "not_started"
    assert result["changed"] is False
    assert result["error_details"]["rejected_fields"] == ["predicate"]
    assert "recovery_affordances" not in result


@pytest.mark.parametrize(
    (
        "error_message",
        "predicate",
        "predicate_visible",
        "predicate_kind",
        "argument_overrides",
    ),
    [
        (
            "scope_mode must be 'user' or 'organisation'",
            "affiliated_with",
            True,
            "concept",
            {},
        ),
        (
            "predicate must be an exact #V# concept ID",
            "affiliated with",
            True,
            "concept",
            {},
        ),
        (
            "predicate must be an exact #V# concept ID",
            "affiliated_with",
            False,
            "concept",
            {},
        ),
        (
            "predicate must be an exact #V# concept ID",
            "affiliated_with",
            True,
            None,
            {},
        ),
        (
            "predicate must be an exact #V# concept ID",
            "affiliated_with",
            True,
            "text",
            {},
        ),
        (
            "predicate must be an exact #V# concept ID",
            "affiliated_with",
            True,
            "concept",
            {"target_concept_id": "target"},
        ),
    ],
    ids=[
        "different-validation-error",
        "not-a-code-identifier",
        "hidden-predicate",
        "untyped-predicate",
        "wrong-value-kind",
        "non-exact-target",
    ],
)
def test_invalid_scoped_assertion_exposes_no_unexecutable_retry(
    monkeypatch: pytest.MonkeyPatch,
    error_message: str,
    predicate: str,
    predicate_visible: bool,
    predicate_kind: str | None,
    argument_overrides: dict[str, Any],
) -> None:
    from src.backend.security.access_control import override_current_actor

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.upsert_scoped_assertion",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError(error_message)),
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.can_access_concept",
        lambda concept_id: (
            predicate_visible
            if concept_id == "#V#affiliated_with"
            else True
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service."
        "resolve_existing_predicate_value_kind",
        lambda _predicate_id: predicate_kind,
    )

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        arguments = {
            "subject_concept_id": "#V#subject",
            "predicate": predicate,
            "target_concept_id": "#V#target",
            "scope_mode": "organisation",
            **argument_overrides,
        }
        result = _gateway().invoke(
            "upsert_scoped_assertion",
            arguments,
        ).payload

    assert result["effect_status"] == "not_started"
    assert result["changed"] is False
    assert "recovery_affordances" not in result
    assert "predicate_resolution" not in result["error_details"]


def test_scoped_list_forwards_offset_and_reports_bounded_page(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security.access_control import override_current_actor

    calls = _install_route_fakes(monkeypatch)
    gateway = _gateway()

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = gateway.invoke(
            "list_scoped_assertions",
            {
                "argument_concept_id": "#V#subject",
                "predicates": ["hasDescription"],
                "object_kind": "text",
                "languages": ["en-NZ"],
                "assertion_form": "standalone_text",
                "context_id": "project:salons",
                "limit": 2,
                "offset": 40,
            },
        ).payload

    assert result["success"] is True
    assert [row["assertion_id"] for row in result["assertions"]] == [
        "ska_page_1",
        "ska_page_2",
    ]
    assert result["assertions_found"] == 2
    assert result["assertions_found_is_lower_bound"] is True
    assert result["paging"] == {
        "limit": 2,
        "offset": 40,
        "returned": 2,
        "has_more": True,
        "next_offset": 42,
        "visibility_filtered": True,
        "counts_are_lower_bounds": True,
    }
    forwarded = calls["list"][-1]
    assert forwarded["offset"] == 40
    assert forwarded["languages"] == ["en-NZ"]
    assert forwarded["assertion_form"] == "standalone_text"
    assert forwarded["context_id"] == "project:salons"
    assert forwarded["_actor"] == (TRUSTED_USER, TRUSTED_ORG)


def test_trusted_operator_fallback_is_bound_across_every_scoped_assertion_path(
    monkeypatch: pytest.MonkeyPatch,
):
    calls = _install_route_fakes(monkeypatch)
    gateway = _gateway(trusted_operator=True)
    identity = {
        "user_concept_id": TRUSTED_USER,
        "organisation_concept_id": TRUSTED_ORG,
        "namespace": TRUSTED_NAMESPACE,
    }

    results = {
        method_name: gateway.invoke(method_name, payload).payload
        for method_name, payload in _route_payloads(identity).items()
    }

    assert all(result.get("success") is not False for result in results.values())
    _assert_all_routes_called_as(
        calls,
        expected_actor=(TRUSTED_USER, TRUSTED_ORG),
    )
    assert calls["upsert"][-1]["acting_user_concept_id"] == TRUSTED_USER
    assert calls["upsert"][-1]["namespace"] == TRUSTED_NAMESPACE
    assert calls["retract"][-1]["acting_user_concept_id"] == TRUSTED_USER
    assert calls["retract"][-1]["namespace"] == TRUSTED_NAMESPACE


def test_authenticated_generic_reads_ignore_conflicting_payload_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security.access_control import override_current_actor

    calls = _install_route_fakes(monkeypatch)
    gateway = _gateway()
    conflicting_identity = {
        "user_concept_id": CLAIMED_USER,
        "organisation_concept_id": CLAIMED_ORG,
        "namespace": CLAIMED_NAMESPACE,
    }

    payloads = _route_payloads(conflicting_identity)
    mutation = payloads.pop("upsert_scoped_assertion")
    retraction = payloads.pop("retract_scoped_assertion")
    scoped_list = payloads.pop("list_scoped_assertions")
    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        results = {
            method_name: gateway.invoke(method_name, payload).payload
            for method_name, payload in payloads.items()
        }
        for method_name, payload in (
            ("upsert_scoped_assertion", mutation),
            ("retract_scoped_assertion", retraction),
            ("list_scoped_assertions", scoped_list),
        ):
            result = gateway.invoke(method_name, payload).payload
            assert result["success"] is False, method_name
            assert result["error_code"] == "workflow_actor_scope_mismatch"

    assert all(
        result["context_view"] == "actor_effective"
        for result in results.values()
    )
    for route_calls in calls.values():
        for call in route_calls:
            assert call["_actor"] == (TRUSTED_USER, TRUSTED_ORG)


def test_truncated_actor_effective_relation_lookup_disables_offset_continuation(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security import access_control
    from src.backend.security.access_control import override_current_actor

    captured: dict[str, Any] = {}

    def truncated_lookup(**kwargs):
        captured.update(kwargs)
        captured["_actor"] = (
            access_control.get_effective_user_concept_id(),
            access_control.get_effective_organisation_concept_id(),
        )
        return {
            "concept_id": kwargs["concept_id"],
            "context_view": kwargs["context_view"],
            "total_hits": 500,
            "total_hits_is_lower_bound": True,
            "hits": [],
            "paging": {
                "limit": 50,
                "offset": 500,
                "returned": 0,
                "total_available": 500,
                "total_available_is_lower_bound": True,
            },
            "relation_query_diagnostics": {
                "scoped_concept_query_truncated": True,
            },
        }

    monkeypatch.setattr(
        "src.backend.services.concept_relation_service."
        "find_relations_with_argument",
        truncated_lookup,
    )

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "find_relations_with_argument",
            {
                "concept_id": "#V#subject",
                "predicate_filter": ["#V#related_to"],
                "relation_kind": "binary",
                "limit": 50,
                "offset": 500,
            },
        ).payload

    assert captured["context_view"] == "actor_effective"
    assert captured["_actor"] == (TRUSTED_USER, TRUSTED_ORG)
    assert result["total_hits_is_lower_bound"] is True
    assert result["paging"]["continuation_supported"] is False
    assert result["paging"]["next_offset"] is None
    assert (
        result["paging"]["offset_semantics"]
        == "bounded_actor_overlay_snapshot"
    )
    assert result["continuation"]["can_continue_with_offset"] is False
    recovery = result["continuation"]["recovery_options"][1]
    assert recovery["tool"] == "list_scoped_assertions"
    assert recovery["scope"] == "scoped_overlay_only"
    assert recovery["arguments"] == {
        "argument_concept_id": "#V#subject",
        "predicates": ["#V#related_to"],
        "object_kind": "concept",
        "limit": 200,
        "offset": 0,
    }


def test_index_coverage_lower_bound_exposes_exact_predicate_restart() -> None:
    from src.backend.integrations.internal_mcp.catalogue import (
        _annotate_bounded_relation_lookup,
    )

    payload = {
        "concept_id": "#V#subject",
        "context_view": "base_publication",
        "total_hits": 0,
        "total_hits_is_lower_bound": True,
        "hits": [],
        "paging": {
            "limit": 50,
            "offset": 0,
            "returned": 0,
            "total_available": 0,
            "total_available_is_lower_bound": True,
        },
        "relation_query_diagnostics": {
            "scoped_concept_query_truncated": False,
            "actor_effective_text_query_truncated": False,
            "incoming_asserted_binary": {
                "requested": True,
                "path": "relationship_extent_index_unavailable",
                "complete": False,
            },
        },
    }

    result = _annotate_bounded_relation_lookup(
        payload,
        concept_id="#V#subject",
        predicate_filter=None,
        relation_kind="binary",
    )

    assert result["paging"]["continuation_supported"] is False
    assert result["paging"]["next_offset"] is None
    assert result["paging"]["offset_semantics"] == "bounded_incoming_relation_snapshot"
    continuation = result["continuation"]
    assert continuation["status"] == "bounded_incoming_relation_coverage_incomplete"
    assert continuation["can_continue_with_offset"] is False
    recovery = continuation["recovery_options"][0]
    assert recovery == {
        "action": "narrow_and_restart",
        "tool": "find_relations_with_argument",
        "restart_offset": 0,
        "preserve_arguments": {
            "concept_id": "#V#subject",
            "relation_kind": "binary",
            "offset": 0,
        },
        "required_argument": "predicate_filter",
        "required_value_kind": "exact_represented_predicate_ids",
        "reason": (
            "An exact predicate permits a bounded canonical incoming-"
            "relationship lookup."
        ),
    }


def test_text_assertion_receipt_includes_derived_maintenance_schedule(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security.access_control import override_current_actor

    assertion = {
        "assertion_id": "ska_scheduled",
        "subject_concept_id": "#V#subject",
        "predicate": "hasDescription",
        "object_kind": "text",
        "object_text": {"text": "Scoped context.", "language": "en-NZ"},
    }
    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.upsert_scoped_assertion",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_scheduled",
            "assertion": assertion,
            "canonical_read_back": assertion,
            "canonical_publication": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service."
        "maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: {
            "mechanism": "in_memory_bounded_queue",
            "durable": False,
            "success": True,
            "scheduled": True,
        },
    )

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "upsert_scoped_assertion",
            {
                "subject_concept_id": "#V#subject",
                "predicate": "hasDescription",
                "target_text": "Scoped context.",
            },
        ).payload

    assert result["effect_status"] == "succeeded"
    assert result["changed"] is True
    assert result["derived_maintenance"] == {
        "mechanism": "in_memory_bounded_queue",
        "durable": False,
        "success": True,
        "scheduled": True,
    }


def test_standalone_text_tool_binds_actor_and_admits_before_link_enrichment(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security.access_control import override_current_actor

    captured: dict[str, Any] = {}
    assertion = {
        "assertion_id": "ska_raw",
        "assertion_form": "standalone_text",
        "assertion_revision": 1,
        "object_kind": "text",
        "object_text": {
            "text": "  Susan hosted gatherings in Svalbard.\n",
            "language": "en-NZ",
        },
        "concept_links": [],
        "canonical_publication": False,
        "rag_index": {"status": "pending"},
    }

    def store(**kwargs):
        captured["store"] = kwargs
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_raw",
            "assertion": assertion,
            "canonical_read_back": assertion,
            "canonical_publication": False,
            "derived_maintenance": {
                "durable": True,
                "rag_index": {"status": "pending"},
            },
        }

    def reject_links(**kwargs):
        captured["links"] = kwargs
        raise ValueError("one proposed concept is unresolved")

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.store_text_assertion",
        store,
    )
    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service."
        "add_text_assertion_concept_links",
        reject_links,
    )
    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "store_text_assertion",
            {
                "text": "  Susan hosted gatherings in Svalbard.\n",
                "source_occurrence_key": "source:7",
                "concept_links": [
                    {"concept_id": "#V#unresolved", "role": "involves"}
                ],
            },
        ).payload

    assert result["success"] is True
    assert result["effect_status"] == "succeeded"
    assert result["canonical_read_back"] == assertion
    assert result["enrichment"] == {
        "success": False,
        "changed": False,
        "links_added": 0,
        "retryable": True,
        "error_code": "invalid_concept_links",
        "error": "one proposed concept is unresolved",
    }
    assert captured["store"] == {
        "text": "  Susan hosted gatherings in Svalbard.\n",
        "language": "en-NZ",
        "scope_mode": "user",
        "context_id": None,
        "source_event_id": "source:7",
        "evidence": None,
        "acting_user_concept_id": TRUSTED_USER,
        "organisation_concept_id": TRUSTED_ORG,
        "namespace": TRUSTED_NAMESPACE,
        "turn_id": None,
        "canonical_publication": False,
    }
    assert captured["links"]["acting_user_concept_id"] == TRUSTED_USER
    assert captured["links"]["organisation_concept_id"] == TRUSTED_ORG
    assert captured["links"]["namespace"] == TRUSTED_NAMESPACE


def test_standalone_text_tool_rejects_payload_identity_without_actor(
    monkeypatch: pytest.MonkeyPatch,
):
    called = False

    def store(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("actorless payload must not reach storage")

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.store_text_assertion",
        store,
    )
    result = _gateway().invoke(
        "store_text_assertion",
        {
            "text": "Payload-claimed private assertion.",
            "acting_user_concept_id": CLAIMED_USER,
            "organisation_concept_id": CLAIMED_ORG,
            "namespace": CLAIMED_NAMESPACE,
        },
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "authenticated_actor_context_required"
    assert called is False


def test_later_text_assertion_link_enrichment_uses_trusted_actor(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security.access_control import override_current_actor

    captured: dict[str, Any] = {}

    def add_links(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_raw",
            "links_added": 1,
            "assertion": {
                "assertion_id": "ska_raw",
                "assertion_form": "standalone_text",
                "concept_links": [{"concept_id": "#V#svalbard"}],
            },
            "canonical_publication": False,
        }

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service."
        "add_text_assertion_concept_links",
        add_links,
    )
    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "add_text_assertion_concept_links",
            {
                "assertion_id": "ska_raw",
                "links": [
                    {
                        "concept_id": "#V#svalbard",
                        "role": "involves",
                    }
                ],
            },
        ).payload

    assert result["success"] is True
    assert result["links_added"] == 1
    assert captured == {
        "assertion_id": "ska_raw",
        "links": [
            {
                "concept_id": "#V#svalbard",
                "role": "involves",
            }
        ],
        "acting_user_concept_id": TRUSTED_USER,
        "organisation_concept_id": TRUSTED_ORG,
        "namespace": TRUSTED_NAMESPACE,
        "turn_id": None,
    }


def test_text_retraction_uses_trusted_actor_and_reports_index_refresh(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security.access_control import override_current_actor

    captured: dict[str, dict[str, Any]] = {}
    assertion = {
        "assertion_id": "ska_retracted",
        "subject_concept_id": "#V#subject",
        "predicate": "hasDescription",
        "object_kind": "text",
        "status": "retracted",
    }

    def retract(**kwargs):
        captured["retract"] = kwargs
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_retracted",
            "assertion": assertion,
            "canonical_read_back": assertion,
            "canonical_publication": False,
        }

    def schedule(**kwargs):
        captured["schedule"] = kwargs
        return {
            "mechanism": "in_memory_bounded_queue",
            "durable": False,
            "success": True,
            "scheduled": True,
        }

    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service."
        "retract_scoped_assertion",
        retract,
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service."
        "maybe_sync_concept_text_relations_to_rag",
        schedule,
    )

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "retract_scoped_assertion",
            {"assertion_id": "ska_retracted"},
        ).payload

    assert captured["retract"] == {
        "assertion_id": "ska_retracted",
        "acting_user_concept_id": TRUSTED_USER,
        "organisation_concept_id": TRUSTED_ORG,
        "namespace": TRUSTED_NAMESPACE,
    }
    assert captured["schedule"] == {
        "namespace": TRUSTED_NAMESPACE,
        "concept_id": "#V#subject",
        "predicate": "hasDescription",
        "scoped_assertion_id": "ska_retracted",
    }
    assert result["effect_status"] == "succeeded"
    assert result["changed"] is True
    assert result["derived_maintenance"] == {
        "mechanism": "in_memory_bounded_queue",
        "durable": False,
        "success": True,
        "scheduled": True,
    }


def test_retraction_schedule_failure_does_not_hide_durable_receipt(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security.access_control import override_current_actor

    assertion = {
        "assertion_id": "ska_retracted",
        "subject_concept_id": "#V#subject",
        "predicate": "hasDescription",
        "object_kind": "text",
        "status": "retracted",
    }
    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service."
        "retract_scoped_assertion",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_retracted",
            "assertion": assertion,
            "canonical_read_back": assertion,
            "canonical_publication": False,
        },
    )

    def fail_schedule(**_kwargs):
        raise RuntimeError("refresh queue unavailable")

    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service."
        "maybe_sync_concept_text_relations_to_rag",
        fail_schedule,
    )

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "retract_scoped_assertion",
            {"assertion_id": "ska_retracted"},
        ).payload

    assert result["effect_status"] == "succeeded"
    assert result["changed"] is True
    assert result["derived_maintenance"] == {
        "mechanism": "in_memory_bounded_queue",
        "durable": False,
        "success": False,
        "scheduled": False,
        "skipped": False,
        "reason": "schedule_failed",
        "error": "refresh queue unavailable",
    }


def test_rag_scheduling_failure_does_not_hide_durable_assertion_receipt(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.backend.security.access_control import override_current_actor

    assertion = {
        "assertion_id": "ska_durable",
        "subject_concept_id": "#V#subject",
        "predicate": "hasDescription",
        "object_kind": "text",
        "object_text": {"text": "Scoped context.", "language": "en-NZ"},
    }
    monkeypatch.setattr(
        "src.backend.services.scoped_assertion_service.upsert_scoped_assertion",
        lambda **_kwargs: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "assertion_id": "ska_durable",
            "assertion": assertion,
            "canonical_read_back": assertion,
            "canonical_publication": False,
            "storage_surface": "scoped_knowledge_assertions",
        },
    )

    def _fail_rag_schedule(**_kwargs):
        raise RuntimeError("derived index unavailable")

    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service."
        "maybe_sync_concept_text_relations_to_rag",
        _fail_rag_schedule,
    )

    with override_current_actor(TRUSTED_USER, TRUSTED_ORG):
        result = _gateway().invoke(
            "upsert_scoped_assertion",
            {
                "subject_concept_id": "#V#subject",
                "predicate": "hasDescription",
                "target_text": "Scoped context.",
            },
        ).payload

    assert result["success"] is True
    assert result["effect_status"] == "succeeded"
    assert result["assertion_id"] == "ska_durable"
    assert result["canonical_read_back"] == assertion
    assert result["derived_maintenance"] == {
        "mechanism": "in_memory_bounded_queue",
        "durable": False,
        "success": False,
        "scheduled": False,
        "skipped": False,
        "reason": "schedule_failed",
        "error": "derived index unavailable",
    }
