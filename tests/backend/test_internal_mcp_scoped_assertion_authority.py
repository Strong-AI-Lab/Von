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


def test_index_coverage_lower_bound_is_not_labelled_as_actor_overlay() -> None:
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

    assert result == payload
    assert "continuation" not in result


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
