from __future__ import annotations

from contextlib import nullcontext

import pytest

from src.backend.services import ontology_authority_vocabulary_service as vocabulary
from src.backend.services.ontology_publication_authority_service import (
    AuthorityRoleEvidence,
    GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
    authority_role_storage_text,
    parse_authority_role_storage_text,
)


def test_bootstrap_only_records_first_explicit_role(monkeypatch) -> None:
    monkeypatch.setattr(
        vocabulary,
        "ontology_authority_membership_mutation_barrier",
        nullcontext,
    )
    monkeypatch.setattr(
        vocabulary,
        "semantic_authority_has_been_bootstrapped",
        lambda: True,
    )
    monkeypatch.setattr(
        vocabulary, "ontology_mutation_resource_lock", lambda _key: nullcontext()
    )
    monkeypatch.setattr(
        vocabulary,
        "upsert_text_for_concept",
        lambda **_: pytest.fail("must not write after bootstrap"),
    )

    with pytest.raises(
        PermissionError, match="ontology_authority_bootstrap_already_completed"
    ):
        vocabulary.bootstrap_first_semantic_authority(
            subject_concept_id="#V#another",
            role="global_ontology_administrator",
        )


def test_bootstrap_rejects_an_organisation_role_root() -> None:
    with pytest.raises(
        ValueError, match="bootstrap_requires_global_ontology_administrator"
    ):
        vocabulary.bootstrap_first_semantic_authority(
            subject_concept_id="#V#org-admin",
            role="organisation_ontology_administrator",
        )


def test_bootstrap_canonicalises_existing_subject_and_verifies_live_role(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        vocabulary,
        "ontology_authority_membership_mutation_barrier",
        nullcontext,
    )
    captured = {}
    monkeypatch.setattr(
        vocabulary,
        "semantic_authority_has_been_bootstrapped",
        lambda: False,
    )
    monkeypatch.setattr(
        vocabulary, "ontology_mutation_resource_lock", lambda _key: nullcontext()
    )
    monkeypatch.setattr(
        vocabulary.ConceptsRepository,
        "find_one",
        lambda query: (
            {"concept_id": query["concept_id"]}
            if query["concept_id"] == "#V#michael_witbrock"
            else None
        ),
    )

    def upsert(**kwargs):
        captured.update(kwargs)
        return {"relation_id": "bootstrap-relation"}

    monkeypatch.setattr(vocabulary, "upsert_text_for_concept", upsert)
    monkeypatch.setattr(
        vocabulary,
        "resolve_live_semantic_roles",
        lambda actor: (
            AuthorityRoleEvidence(
                role=GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
                actor_concept_id=actor,
                organisation_concept_id=None,
                relation_id="bootstrap-relation",
                revision="bootstrap-revision",
            ),
        ),
    )
    monkeypatch.setattr(
        vocabulary,
        "delete_text_relation",
        lambda **_kwargs: pytest.fail("verified bootstrap must not compensate"),
    )

    result = vocabulary.bootstrap_first_semantic_authority(
        subject_concept_id="michael_witbrock",
        role=GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
    )

    assert captured["subject_concept_id"] == "#V#michael_witbrock"
    assert captured["context"]["grant_provenance"]["operator_authority"] == (
        "bootstrap_only"
    )
    assert result["first_grant"]["revision"] == "bootstrap-revision"


def test_bootstrap_rejects_a_missing_subject_before_writing(monkeypatch) -> None:
    monkeypatch.setattr(
        vocabulary,
        "ontology_authority_membership_mutation_barrier",
        nullcontext,
    )
    monkeypatch.setattr(
        vocabulary,
        "semantic_authority_has_been_bootstrapped",
        lambda: False,
    )
    monkeypatch.setattr(
        vocabulary, "ontology_mutation_resource_lock", lambda _key: nullcontext()
    )
    monkeypatch.setattr(vocabulary.ConceptsRepository, "find_one", lambda _query: None)
    monkeypatch.setattr(
        vocabulary,
        "upsert_text_for_concept",
        lambda **_kwargs: pytest.fail("missing subject must not be written"),
    )

    with pytest.raises(
        ValueError,
        match="ontology_authority_bootstrap_subject_not_found",
    ):
        vocabulary.bootstrap_first_semantic_authority(
            subject_concept_id="typo_subject",
            role=GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        )


def test_organisation_role_storage_keeps_each_organisation_distinct() -> None:
    first = authority_role_storage_text(
        ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        "#V#org_a",
    )
    second = authority_role_storage_text(
        ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        "#V#org_b",
    )

    assert first != second
    assert parse_authority_role_storage_text(first, {}) == (
        ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        "#V#org_a",
    )
    assert parse_authority_role_storage_text(second, {}) == (
        ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE,
        "#V#org_b",
    )
