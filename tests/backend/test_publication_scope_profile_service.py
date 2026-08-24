from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

import src.backend.services.publication_scope_profile_vontology_service as publication_seed_module
from src.backend.services import concept_service
from src.backend.services.publication_scope_profile_service import (
    PUBLICATION_SCOPE_PROFILE_LINK_PREDICATE,
    PUBLICATION_SCOPE_PROFILE_TEXT_PREDICATE,
    resolve_publication_scope_profile,
)
from src.backend.services.publication_scope_profile_vontology_service import (
    bootstrap_canonical_publication_scope_profiles,
    ensure_publication_scope_profiles_current_for_startup,
)
from src.backend.services.text_value_service import upsert_singleton_text_relation


@pytest.fixture
def publication_profile_store(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    from src.backend.db.mongo_client import close_connection, get_db
    from src.backend.security.access_control import invalidate_current_access_evaluator
    from src.backend.services.concept_predicate_metadata_service import (
        invalidate_cache,
    )

    close_connection()
    invalidate_cache()
    invalidate_current_access_evaluator()
    try:
        db = get_db()
        assert db is not None
        report = bootstrap_canonical_publication_scope_profiles()
        assert report["success"] is True, json.dumps(
            report, sort_keys=True, default=str
        )
        yield
    finally:
        close_connection()
        invalidate_cache()
        invalidate_current_access_evaluator()


def _write_profile(
    *,
    profile_id: str,
    bound_subject_id: str,
    plane: str,
    scope_mode: str,
    version: str = "1",
    applicability: dict[str, Any] | None = None,
) -> None:
    try:
        existing_profile = concept_service.get_concept_by_concept_id_exact(profile_id)
    except concept_service.ConceptNotFoundError:
        existing_profile = None
    if existing_profile is None:
        concept_service.create_concept(
            name=profile_id.removeprefix("#V#").replace("_", " "),
            concept_id=profile_id,
            parent_concept_ids=["#V#publication_scope_profile"],
            create_as_instance=True,
            visibility_scope_mode="global_general",
            maintain_relationship_inverses=False,
        )
    subject = concept_service.get_concept_by_concept_id_exact(bound_subject_id)
    assert subject is not None
    relationships = dict(subject.get("relationships") or {})
    links = list(relationships.get(PUBLICATION_SCOPE_PROFILE_LINK_PREDICATE) or [])
    if profile_id not in links:
        links.append(profile_id)
    concept_service.update_concept(
        bound_subject_id,
        {f"relationships.{PUBLICATION_SCOPE_PROFILE_LINK_PREDICATE}": links},
    )
    payload: dict[str, Any] = {
        "schema_version": "publication_scope_profile.v1",
        "profile_id": profile_id.removeprefix("#V#"),
        "version": version,
        "status": "active",
        "plane": plane,
        "recommended_scope_mode": scope_mode,
        "source": "test",
    }
    if applicability:
        payload["applicability"] = applicability
    upsert_singleton_text_relation(
        subject_concept_id=profile_id,
        predicate=PUBLICATION_SCOPE_PROFILE_TEXT_PREDICATE,
        text=json.dumps(payload, sort_keys=True),
        lang="en-NZ",
        context={"source": "test"},
        garbage_collect=True,
    )


def test_type_profiles_resolve_and_subtype_overrides_parent(
    publication_profile_store: None,
) -> None:
    paper = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper"],
    )
    draft = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#paper_under_preparation"],
    )
    candidature = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#phd_candidature"],
    )
    note = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#personal_research_note"],
    )

    assert paper["selected_scope_mode"] == "global_general"
    assert draft["selected_scope_mode"] == "organisation_general"
    assert candidature["selected_scope_mode"] == "organisation_general"
    assert note["selected_scope_mode"] == "user_only_default"
    assert {row["bound_subject_concept_id"] for row in draft["matched_profiles"]} == {
        "#V#paper_under_preparation",
        "#V#scientific_paper",
    }
    assert [row["bound_subject_concept_id"] for row in draft["decisive_profiles"]] == [
        "#V#paper_under_preparation"
    ]


def test_incomparable_type_conflict_requires_reasoned_model_judgement(
    publication_profile_store: None,
) -> None:
    ambiguous = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper", "#V#personal_research_note"],
    )
    assert ambiguous["success"] is False
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["error_code"] == "ambiguous_publication_scope_profile"
    assert ambiguous["selected_scope_mode"] is None

    unresolved_override = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper", "#V#personal_research_note"],
        selected_scope_mode="global_general",
    )
    assert unresolved_override["status"] == "invalid_selection"
    assert (
        unresolved_override["error_code"]
        == "publication_scope_conflict_reason_required"
    )

    resolved = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper", "#V#personal_research_note"],
        selected_scope_mode="global_general",
        selection_reason="The artefact is the released public paper, not my notes.",
        producer="#V#von_system",
    )
    assert resolved["success"] is True
    assert resolved["selection_source"] == "explicit_justified_override"
    assert resolved["decision_evidence"]["selection_reason"]


def test_multiple_type_agreement_and_single_profile_override(
    publication_profile_store: None,
) -> None:
    agreement = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper", "#V#public_dataset_release"],
    )
    override = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper"],
        selected_scope_mode="organisation_general",
        selection_reason="This paper is still confined to the named consortium.",
        producer="#V#von_system",
    )

    assert agreement["success"] is True
    assert agreement["selected_scope_mode"] == "global_general"
    assert len(agreement["decisive_profiles"]) == 2
    assert override["success"] is True
    assert override["recommended_scope_mode"] == "global_general"
    assert override["selected_scope_mode"] == "organisation_general"
    assert override["selection_source"] == "explicit_justified_override"


def test_predicate_scope_is_independent_of_global_entity_scope(
    publication_profile_store: None,
) -> None:
    decision = resolve_publication_scope_profile(
        plane="assertion",
        predicate_concept_id="#V#has_read",
        subject_type_concept_ids=["#V#person"],
        object_type_concept_ids=["#V#scientific_paper"],
        source_kind="public_record",
        stable_identity_present=True,
    )
    assert decision["success"] is True
    assert decision["selected_scope_mode"] == "user_only_default"
    assert decision["carrier"] == {
        "supported": True,
        "carrier": "scoped_assertion",
        "tool": "upsert_scoped_assertion",
        "scope_mode": "user",
    }
    assert decision["assertion_scope_selected_from_entity_scope"] is False
    hints = {row["role"]: row for row in decision["entity_context_hints"]}
    assert hints["subject"]["recommended_scope_mode"] == "global_general"
    assert hints["object"]["recommended_scope_mode"] == "global_general"
    assert hints["subject"]["used_to_select_assertion_scope"] is False


def test_conditional_predicate_profiles_do_not_apply_tightest_scope(
    publication_profile_store: None,
) -> None:
    public = resolve_publication_scope_profile(
        plane="assertion",
        predicate_concept_id="#V#organises",
        source_kind="public_event_record",
    )
    internal = resolve_publication_scope_profile(
        plane="assertion",
        predicate_concept_id="#V#organises",
        source_kind="internal_organisation_record",
    )
    unknown = resolve_publication_scope_profile(
        plane="assertion",
        predicate_concept_id="#V#organises",
    )

    assert public["selected_scope_mode"] == "global_general"
    assert public["carrier"]["carrier"] == "canonical_relationship_candidate"
    assert internal["selected_scope_mode"] == "organisation_general"
    assert internal["carrier"]["scope_mode"] == "organisation"
    assert unknown["status"] == "no_profile"
    assert unknown["requires_model_judgement"] is True


def test_diverse_type_defaults_and_lifecycle_conditions_are_represented(
    publication_profile_store: None,
) -> None:
    public_release = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#public_dataset_release"],
    )
    internal_evaluation = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#internal_model_evaluation"],
    )
    personal_hypothesis = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#personal_hypothesis"],
    )
    draft_grant = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#grant"],
        lifecycle_state="draft",
    )
    awarded_grant = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#grant"],
        lifecycle_state="awarded",
    )
    unspecified_grant = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#grant"],
    )

    assert public_release["selected_scope_mode"] == "global_general"
    assert internal_evaluation["selected_scope_mode"] == "organisation_general"
    assert personal_hypothesis["selected_scope_mode"] == "user_only_default"
    assert draft_grant["selected_scope_mode"] == "organisation_general"
    assert awarded_grant["selected_scope_mode"] == "global_general"
    assert unspecified_grant["status"] == "no_profile"


def test_specialised_predicate_profile_overrides_inherited_profile(
    publication_profile_store: None,
) -> None:
    concept_service.create_concept(
        name="Internal authored by",
        concept_id="#V#internal_authored_by",
        parent_concept_ids=["#V#authored_by"],
        create_as_instance=True,
        visibility_scope_mode="global_general",
        maintain_relationship_inverses=False,
    )
    concept_service.update_concept(
        "#V#internal_authored_by",
        {"relationships.is_a_type_of": ["#V#authored_by"]},
    )
    _write_profile(
        profile_id="#V#publication_profile_internal_authorship_org",
        bound_subject_id="#V#internal_authored_by",
        plane="assertion",
        scope_mode="organisation_general",
        applicability={"source_kinds": ["public_scholarly_metadata"]},
    )

    decision = resolve_publication_scope_profile(
        plane="assertion",
        predicate_concept_id="#V#internal_authored_by",
        source_kind="public_scholarly_metadata",
    )
    assert decision["selected_scope_mode"] == "organisation_general"
    assert {
        row["bound_subject_concept_id"] for row in decision["matched_profiles"]
    } == {"#V#internal_authored_by", "#V#authored_by"}
    assert [
        row["bound_subject_concept_id"] for row in decision["decisive_profiles"]
    ] == ["#V#internal_authored_by"]


def test_restricted_and_secret_profiles_return_typed_non_mutating_outcomes(
    publication_profile_store: None,
) -> None:
    restricted = resolve_publication_scope_profile(
        plane="assertion",
        predicate_concept_id="#V#has_salary",
        selected_scope_mode="organisation_general",
        selection_reason="Attempted ordinary organisation placement",
    )
    secret = resolve_publication_scope_profile(
        plane="assertion",
        predicate_concept_id="#V#has_secret_value",
        selected_scope_mode="global_general",
        selection_reason="Attempted public placement",
    )

    assert restricted["status"] == "unsupported_carrier"
    assert restricted["selected_scope_mode"] == "restricted_context_required"
    assert restricted["mutation_performed"] is False
    assert restricted["carrier"]["supported"] is False
    assert secret["status"] == "unsupported_carrier"
    assert secret["selected_scope_mode"] == "external_secure_storage"
    assert secret["error_code"] == "ordinary_vontology_storage_prohibited"
    assert secret["mutation_performed"] is False


def test_profile_absence_preserves_reasoned_model_judgement(
    publication_profile_store: None,
) -> None:
    concept_service.create_concept(
        name="Unprofiled artefact",
        concept_id="#V#unprofiled_artefact",
        parent_concept_ids=["#V#thing"],
        create_as_instance=False,
        visibility_scope_mode="global_general",
        maintain_relationship_inverses=False,
    )
    absent = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#unprofiled_artefact"],
    )
    selected = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#unprofiled_artefact"],
        selected_scope_mode="organisation_general",
        selection_reason="The task describes an internal lab artefact.",
        producer="#V#von_system",
    )

    assert absent["status"] == "no_profile"
    assert absent["requires_model_judgement"] is True
    assert selected["success"] is True
    assert selected["selection_source"] == "explicit_model_judgement_no_profile"
    assert selected["required_authority"]["profile_is_authority_grant"] is False


def test_unavailable_subject_and_explicit_protected_carrier_fail_closed(
    publication_profile_store: None,
) -> None:
    unavailable = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#not_visible_or_missing_type"],
        selected_scope_mode="global_general",
        selection_reason="A payload claim must not replace a readable type.",
    )
    concept_service.create_concept(
        name="Unprofiled secure artefact",
        concept_id="#V#unprofiled_secure_artefact",
        parent_concept_ids=["#V#thing"],
        create_as_instance=False,
        visibility_scope_mode="global_general",
        maintain_relationship_inverses=False,
    )
    external = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#unprofiled_secure_artefact"],
        selected_scope_mode="external_secure_storage",
        selection_reason="This artefact contains credentials.",
    )

    assert unavailable["success"] is False
    assert unavailable["status"] == "unavailable_subject"
    assert unavailable["error_code"] == "publication_scope_subject_unavailable"
    assert unavailable["selected_scope_mode"] is None
    assert external["success"] is False
    assert external["status"] == "unsupported_carrier"
    assert external["error_code"] == "ordinary_vontology_storage_prohibited"


def test_profile_recommendation_cannot_bypass_authority_or_downgrade(
    publication_profile_store: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority
    from src.backend.services.create_concepts_parent_resolution_service import (
        ParentResolutionResult,
    )

    decision = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper"],
    )
    assert decision["selected_scope_mode"] == "global_general"
    assert decision["required_authority"]["profile_is_authority_grant"] is False

    monkeypatch.setattr(
        "src.backend.services.create_concepts_parent_resolution_service."
        "resolve_parent_for_create_concepts",
        lambda parent_id: ParentResolutionResult(
            requested_parent_id=parent_id,
            canonical_parent_id=parent_id,
            resolved_parent_id=parent_id,
            fallback_used=False,
            fallback_candidates_checked=(),
            fallback_selected_parent_id=None,
            resolved_parent_kind="type",
        ),
    )
    monkeypatch.setattr(
        command, "_concept_exists_unfiltered", lambda _concept_id: False
    )
    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(authority, "resolve_live_semantic_roles", lambda _actor: ())
    mutation_calls = 0

    def mutate() -> dict[str, Any]:
        nonlocal mutation_calls
        mutation_calls += 1
        return {"success": True, "changed": True}

    with authority.override_current_actor("#V#member", "#V#organisation_a"):
        denied = command.execute_governed_ontology_method(
            method_name="create_concepts",
            arguments={
                "parent_id": "#V#scientific_paper",
                "concepts": [
                    {
                        "concept_id": "#V#profile_cannot_grant_global_authority",
                        "name": "Profile cannot grant global authority",
                        "kind": "instance",
                    }
                ],
                "scope_mode": decision["selected_scope_mode"],
            },
            mutate=mutate,
        )

    assert mutation_calls == 0
    assert denied["success"] is False
    assert denied["effect_status"] == "not_started"
    assert denied["error_code"] == "global_ontology_admin_authority_required"
    assert denied["required_authority"]["authority_kind"] == (
        "global_ontology_administrator"
    )
    assert denied["authority_recovery"]["action_type"] == (
        "ask_for_exact_ontology_authority"
    )
    assert "scope_selection" not in denied


def test_live_profile_edit_changes_fresh_decision_and_bootstrap_preserves_it(
    publication_profile_store: None,
) -> None:
    profile_id = "#V#publication_profile_scientific_paper_global"
    before = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper"],
    )
    assert before["selected_scope_mode"] == "global_general"

    edited_payload = {
        "schema_version": "publication_scope_profile.v1",
        "profile_id": "scientific_paper_local_research_exception",
        "version": "2",
        "status": "active",
        "plane": "instance",
        "recommended_scope_mode": "user_only_default",
        "source": "live_vontology_edit",
    }
    upsert_singleton_text_relation(
        subject_concept_id=profile_id,
        predicate=PUBLICATION_SCOPE_PROFILE_TEXT_PREDICATE,
        text=json.dumps(edited_payload, sort_keys=True),
        lang="en-NZ",
        context={"source": "test_live_edit"},
        garbage_collect=True,
    )

    after = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper"],
    )
    assert after["selected_scope_mode"] == "user_only_default"
    assert after["decision_evidence"]["profile_versions"][profile_id] == "2"

    bootstrap = bootstrap_canonical_publication_scope_profiles()
    assert bootstrap["success"] is True
    assert profile_id in bootstrap["preserved_profile_ids"]
    assert bootstrap["errors"] == []
    preserved = resolve_publication_scope_profile(
        plane="instance",
        type_concept_ids=["#V#scientific_paper"],
    )
    assert preserved["selected_scope_mode"] == "user_only_default"


def test_bootstrap_is_idempotent_when_profiles_are_unchanged(
    publication_profile_store: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concept_reads = 0
    text_reads = 0
    original_concept_read = concept_service.get_concepts_by_concept_ids_exact
    original_text_read = publication_seed_module.get_texts_for_concepts

    def _concept_read(concept_ids):
        nonlocal concept_reads
        concept_reads += 1
        return original_concept_read(concept_ids)

    def _text_read(*args, **kwargs):
        nonlocal text_reads
        text_reads += 1
        return original_text_read(*args, **kwargs)

    monkeypatch.setattr(
        concept_service,
        "get_concepts_by_concept_ids_exact",
        _concept_read,
    )
    monkeypatch.setattr(publication_seed_module, "get_texts_for_concepts", _text_read)
    monkeypatch.setattr(
        publication_seed_module,
        "upsert_singleton_text_relation",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("current profile payloads must not be rewritten")
        ),
    )
    monkeypatch.setattr(
        concept_service,
        "create_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("current concepts must not be recreated")
        ),
    )
    monkeypatch.setattr(
        concept_service,
        "update_concept",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("current relationships must not be rewritten")
        ),
    )

    second = bootstrap_canonical_publication_scope_profiles()
    assert second["success"] is True
    assert second["changed"] is False
    assert second["read_strategy"] == "batched_canonical_state"
    assert second["read_phases"] == 2
    assert second["counts"]["concepts_created"] == 0
    assert second["counts"]["profiles_seeded"] == 0
    assert second["counts"]["profiles_preserved"] == 19
    assert second["counts"]["relationships_changed"] == 0
    assert concept_reads == 1
    assert text_reads == 1


def test_publication_profile_startup_receipt_hit_performs_no_canonical_scan(
    publication_profile_store: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        publication_seed_module.seed_freshness,
        "check_startup_seed_freshness",
        lambda **_kwargs: {
            "fresh": True,
            "reason": "dependency_receipt_current",
            "events_examined": 0,
            "metadata": {"counts": {"profiles_preserved": 19}},
        },
    )
    monkeypatch.setattr(
        concept_service,
        "get_concepts_by_concept_ids_exact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("receipt hit must not scan canonical concepts")
        ),
    )
    monkeypatch.setattr(
        publication_seed_module,
        "get_texts_for_concepts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("receipt hit must not scan canonical text")
        ),
    )

    report = ensure_publication_scope_profiles_current_for_startup()

    assert report["success"] is True
    assert report["skipped"] is True
    assert report["read_strategy"] == "dependency_receipt"
    assert report["canonical_read_batches"] == {
        "concepts": 0,
        "text_assertions": 0,
    }
