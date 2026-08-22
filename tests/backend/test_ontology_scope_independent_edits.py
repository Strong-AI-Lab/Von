from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

USER_EDGE = ("#V#specific_to_user", "#V#member")
ORGANISATION_EDGE = (
    "#V#specific_to_organisation",
    "#V#organisation_a",
)


def _edge_payload(edges: set[tuple[str, str]]) -> dict[str, list[str]]:
    payload: dict[str, list[str]] = {}
    for predicate, target in sorted(edges):
        payload.setdefault(predicate, []).append(target)
    return payload


def _install_scope_fixture(monkeypatch, initial_edges: set[tuple[str, str]]):
    from src.backend.services import ontology_scope_change_service as scope

    state = {"edges": set(initial_edges), "fingerprint": "scope-before"}
    events: list[tuple[str, str, str]] = []

    def read_back(concept_id: str) -> dict[str, Any]:
        context = scope._context_for_edges(
            sorted(state["edges"]),
            source="concept_visibility",
        )
        return {
            "concept_id": concept_id,
            "publication_context": context.to_mapping(),
            "scope_edges": _edge_payload(state["edges"]),
            "scope_fingerprint": state["fingerprint"],
        }

    def add_relationship(source_id: str, predicate: str, target: str):
        events.append(("add", predicate, target))
        state["edges"].add((predicate, target))
        state["fingerprint"] = "scope-after"
        return {"success": True, "forward_modified": True}

    def remove_relationship(**kwargs):
        predicate = kwargs["predicate"]
        target = kwargs["target"]
        events.append(("remove", predicate, target))
        state["edges"].discard((predicate, target))
        state["fingerprint"] = "scope-after"
        return {"success": True, "removed": True}

    def execute(*, mutate, read_back: Any, verify_read_back, preview, **_kwargs):
        result = dict(mutate())
        canonical = read_back()
        if not preview:
            assert verify_read_back(result, canonical) is True
        result["canonical_read_back"] = canonical
        return result

    monkeypatch.setattr(scope, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(scope, "scope_read_back", read_back)
    monkeypatch.setattr(scope, "add_relationship", add_relationship)
    monkeypatch.setattr(scope, "remove_relationship", remove_relationship)
    monkeypatch.setattr(scope, "ontology_mutation_resource_lock", lambda _key: nullcontext())
    monkeypatch.setattr(
        scope,
        "authorise_ontology_mutation",
        lambda _intent: SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(scope, "execute_authorised_ontology_mutation", execute)
    return scope, state, events


@pytest.mark.parametrize(
    ("initial_edges", "kind", "enabled", "expected_edges"),
    (
        (set(), "user", True, {USER_EDGE}),
        (set(), "organisation", True, {ORGANISATION_EDGE}),
        ({USER_EDGE}, "organisation", True, {USER_EDGE, ORGANISATION_EDGE}),
        ({ORGANISATION_EDGE}, "user", True, {USER_EDGE, ORGANISATION_EDGE}),
        ({USER_EDGE}, "user", False, set()),
        ({ORGANISATION_EDGE}, "organisation", False, set()),
        ({USER_EDGE, ORGANISATION_EDGE}, "user", False, {ORGANISATION_EDGE}),
        ({USER_EDGE, ORGANISATION_EDGE}, "organisation", False, {USER_EDGE}),
    ),
)
def test_scope_edit_preview_changes_only_selected_restriction(
    monkeypatch,
    initial_edges,
    kind,
    enabled,
    expected_edges,
):
    from src.backend.services import ontology_publication_authority_service as authority

    scope, _state, events = _install_scope_fixture(monkeypatch, initial_edges)
    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        result = scope.change_concept_publication_scope(
            concept_id="#V#scope_fixture",
            expected_scope_fingerprint="scope-before",
            request_id=f"preview-{kind}-{enabled}",
            scope_edit={"kind": kind, "enabled": enabled},
            preview=True,
            invocation_tool_name=scope.PREVIEW_SCOPE_CHANGE_TOOL_NAME,
        )

    assert result["success"] is True
    assert result["changed"] is False
    assert result["resolved_scope_edit"] == {
        "kind": kind,
        "enabled": enabled,
        "concept_id": (
            USER_EDGE[1] if kind == "user" else ORGANISATION_EDGE[1]
        ),
    }
    assert result["destination_scope_edges"] == _edge_payload(expected_edges)
    assert {
        (item["predicate"], item["target"])
        for item in result["scope_delta"]["add"]
    } == expected_edges - initial_edges
    assert {
        (item["predicate"], item["target"])
        for item in result["scope_delta"]["remove"]
    } == initial_edges - expected_edges
    assert events == []


def test_scope_edit_execute_adds_without_replacing_complement(monkeypatch):
    from src.backend.services import ontology_publication_authority_service as authority

    scope, state, events = _install_scope_fixture(monkeypatch, {ORGANISATION_EDGE})
    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        result = scope.change_concept_publication_scope(
            concept_id="#V#scope_fixture",
            expected_scope_fingerprint="scope-before",
            request_id="execute-add-user",
            scope_edit={
                "kind": "user",
                "enabled": True,
                "concept_id": "#V#member",
            },
            preview=False,
        )

    assert result["success"] is True
    assert state["edges"] == {USER_EDGE, ORGANISATION_EDGE}
    assert events == [("add", *USER_EDGE)]
    assert result["destination_edge_added"] is True
    assert result["canonical_read_back"]["publication_context"]["kind"] == (
        "composite"
    )


def test_scope_edit_execute_removes_without_replacing_complement(monkeypatch):
    from src.backend.services import ontology_publication_authority_service as authority

    scope, state, events = _install_scope_fixture(
        monkeypatch,
        {USER_EDGE, ORGANISATION_EDGE},
    )
    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        result = scope.change_concept_publication_scope(
            concept_id="#V#scope_fixture",
            expected_scope_fingerprint="scope-before",
            request_id="execute-remove-user",
            scope_edit={
                "kind": "user",
                "enabled": False,
                "concept_id": "#V#member",
            },
            preview=False,
        )

    assert result["success"] is True
    assert state["edges"] == {ORGANISATION_EDGE}
    assert events == [("remove", *USER_EDGE)]
    assert result["destination_edge_added"] is False


def test_whole_scope_replacement_still_adds_before_removing(monkeypatch):
    from src.backend.services import ontology_publication_authority_service as authority

    scope, state, events = _install_scope_fixture(monkeypatch, {USER_EDGE})
    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        result = scope.change_concept_publication_scope(
            concept_id="#V#scope_fixture",
            expected_scope_fingerprint="scope-before",
            request_id="replace-user-with-org",
            destination_kind="organisation",
            destination_concept_id="#V#organisation_a",
            preview=False,
        )

    assert result["success"] is True
    assert state["edges"] == {ORGANISATION_EDGE}
    assert events == [
        ("add", *ORGANISATION_EDGE),
        ("remove", *USER_EDGE),
    ]


@pytest.mark.parametrize(
    "invalid_scope_edges",
    (
        {"specific_to_org": ["#V#organisation_a"]},
        {
            "#V#specific_to_user": [
                "#V#member",
                "#V#other_member",
            ]
        },
    ),
)
def test_scope_edit_rejects_legacy_or_multiple_targets(
    monkeypatch,
    invalid_scope_edges,
):
    scope, _state, events = _install_scope_fixture(monkeypatch, set())
    legacy = {
        "concept_id": "#V#scope_fixture",
        "publication_context": {"kind": "mixed", "concept_id": None},
        "scope_edges": invalid_scope_edges,
        "scope_fingerprint": "scope-before",
    }
    monkeypatch.setattr(scope, "scope_read_back", lambda _concept_id: legacy)

    with pytest.raises(scope.OntologyScopeChangeRequestError) as exc_info:
        scope.change_concept_publication_scope(
            concept_id="#V#scope_fixture",
            expected_scope_fingerprint="scope-before",
            request_id="reject-legacy-edit",
            scope_edit={"kind": "user", "enabled": True},
            preview=True,
        )

    assert exc_info.value.reason_code == "ambiguous_publication_scope_transition"
    assert events == []


def test_scope_edit_add_rejects_a_browser_selected_other_principal(monkeypatch):
    from src.backend.services import ontology_publication_authority_service as authority

    scope, _state, events = _install_scope_fixture(monkeypatch, set())
    with (
        authority.override_current_actor("#V#member", "#V#organisation_a"),
        pytest.raises(scope.OntologyScopeChangeRequestError) as exc_info,
    ):
        scope.change_concept_publication_scope(
            concept_id="#V#scope_fixture",
            expected_scope_fingerprint="scope-before",
            request_id="reject-other-org",
            scope_edit={
                "kind": "organisation",
                "enabled": True,
                "concept_id": "#V#organisation_b",
            },
            preview=True,
        )

    assert exc_info.value.reason_code == "scope_edit_target_mismatch"
    assert events == []


def test_scope_edit_requires_trusted_organisation_only_when_adding(monkeypatch):
    from src.backend.services import ontology_publication_authority_service as authority

    scope, _state, events = _install_scope_fixture(monkeypatch, {USER_EDGE})
    with (
        authority.override_current_actor("#V#member", None),
        pytest.raises(scope.OntologyScopeChangeRequestError) as exc_info,
    ):
        scope.change_concept_publication_scope(
            concept_id="#V#scope_fixture",
            expected_scope_fingerprint="scope-before",
            request_id="missing-org-add",
            scope_edit={"kind": "organisation", "enabled": True},
            preview=True,
        )
    assert exc_info.value.reason_code == "organisation_context_required"
    assert events == []

    scope, state, events = _install_scope_fixture(monkeypatch, {ORGANISATION_EDGE})
    with authority.override_current_actor("#V#member", None):
        result = scope.change_concept_publication_scope(
            concept_id="#V#scope_fixture",
            expected_scope_fingerprint="scope-before",
            request_id="remove-org-without-current-org",
            scope_edit={"kind": "organisation", "enabled": False},
            preview=False,
        )
    assert result["success"] is True
    assert state["edges"] == set()
    assert events == [("remove", *ORGANISATION_EDGE)]
