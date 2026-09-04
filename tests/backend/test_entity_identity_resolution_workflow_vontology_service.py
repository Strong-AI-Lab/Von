from __future__ import annotations


def test_bootstrap_seeds_missing_entity_duplicate_reasoning_prompt(monkeypatch) -> None:
    from src.backend.services import (
        entity_identity_resolution_workflow_vontology_service as service,
    )

    seeded: list[dict[str, object]] = []
    has_content_state = {"ready": False}

    monkeypatch.setattr(
        service,
        "ensure_prompt_concept_support",
        lambda **_: {
            "created_prompt_ids": [service.ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID],
            "validated_prompt_ids": [],
            "missing_content_prompt_ids": [
                service.ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID
            ],
            "linked_workflow_ids": [service.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID],
            "errors_by_target": {
                service.ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID: (
                    "prompt_content_missing"
                )
            },
        },
    )
    monkeypatch.setattr(
        service,
        "prompt_concept_has_content",
        lambda *_args, **_kwargs: has_content_state["ready"],
    )
    monkeypatch.setattr(
        service,
        "_load_prompt_seed_text",
        lambda: "Authoritative entity duplicate reasoning prompt seed.",
    )

    def _fake_upsert_singleton_text_relation(**kwargs):
        seeded.append(dict(kwargs))
        has_content_state["ready"] = True
        return {"success": True}

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _fake_upsert_singleton_text_relation,
    )
    publication_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        service,
        "bootstrap_repo_seed_workflow_bundle",
        lambda **kwargs: publication_calls.append(kwargs)
        or {
            "publication": {"counts": {"errors": 0}},
            "typed_workflow_ids": [],
            "typed_step_ids": [],
            "validation_by_workflow_id": {},
        },
    )

    report = service.bootstrap_canonical_entity_identity_resolution_workflow()

    assert report["success"] is True
    assert report["seeded_prompt_ids"] == [
        service.ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID
    ]
    assert report["missing_content_prompt_ids"] == []
    assert report["errors_by_target"] == {}
    assert report["workflow_id"] == service.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID
    assert seeded
    assert seeded[0]["subject_concept_id"] == (
        service.ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID
    )
    assert seeded[0]["predicate"] == "hasContent"
    assert seeded[0]["lang"] == "en-NZ"
    assert publication_calls[0]["target_workflow_ids"] == (
        service.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
    )


def test_bootstrap_does_not_reseed_existing_prompt(monkeypatch) -> None:
    from src.backend.services import (
        entity_identity_resolution_workflow_vontology_service as service,
    )

    monkeypatch.setattr(
        service,
        "ensure_prompt_concept_support",
        lambda **_: {
            "created_prompt_ids": [],
            "validated_prompt_ids": [service.ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID],
            "missing_content_prompt_ids": [],
            "linked_workflow_ids": [service.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID],
            "errors_by_target": {},
        },
    )
    monkeypatch.setattr(service, "prompt_concept_has_content", lambda *_a, **_kw: True)

    seeded: list[dict[str, object]] = []
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: seeded.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        service,
        "bootstrap_repo_seed_workflow_bundle",
        lambda **_kwargs: {
            "publication": {"counts": {"errors": 0}},
            "typed_workflow_ids": [],
            "typed_step_ids": [],
            "validation_by_workflow_id": {},
        },
    )

    report = service.bootstrap_canonical_entity_identity_resolution_workflow()

    assert report["success"] is True
    assert report["seeded_prompt_count"] == 0
    assert report["validated_prompt_ids"] == [
        service.ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID
    ]
    assert seeded == []
