from src.backend.services import renderer_applicability_vontology_service as service


def test_canonical_renderer_profile_concept_ids_are_deterministic() -> None:
    concept_ids = service.canonical_renderer_profile_concept_ids()
    assert concept_ids == (
        "#V#timeline_renderer",
        "#V#workflow_renderer",
        "#V#table_renderer",
        "#V#narration_renderer",
    )


def test_load_renderer_definitions_from_concept_ids_parses_profile_json(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#timeline_renderer", "relationships": {}}
        ],
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concepts",
        lambda concept_ids, predicates=None, limit_per_concept=20: {
            concept_ids[0]: [
                {
                    "predicate": "#V#has_renderer_profile_json",
                    "text": '{"renderer_type":"timeline","modalities":["visual"],"priority":80,"screen_element_families":["timeline"]}',
                }
            ]
        },
    )

    definitions, diagnostics = service.load_renderer_definitions_from_concept_ids(
        ["#V#timeline_renderer"]
    )

    assert len(definitions) == 1
    assert definitions[0]["renderer_id"] == "#V#timeline_renderer"
    assert definitions[0]["renderer_type"] == "timeline"
    assert definitions[0]["screen_element_families"] == ["timeline"]
    assert diagnostics["loaded_concept_ids"] == ["#V#timeline_renderer"]
    assert diagnostics["loaded_definition_count"] == 1


def test_load_renderer_definitions_reports_malformed_profile_json(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#timeline_renderer", "relationships": {}}
        ],
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concepts",
        lambda concept_ids, predicates=None, limit_per_concept=20: {
            concept_ids[0]: [
                {
                    "predicate": "#V#has_renderer_profile_json",
                    "text": "{not valid json}",
                }
            ]
        },
    )

    definitions, diagnostics = service.load_renderer_definitions_from_concept_ids(
        ["#V#timeline_renderer"]
    )

    assert definitions == []
    assert diagnostics["loaded_definition_count"] == 0
    assert diagnostics["missing_profile_concept_ids"] == []
    assert diagnostics["malformed_profile_concept_ids"] == ["#V#timeline_renderer"]
    assert diagnostics["malformed_profile_count"] == 1
    malformed = diagnostics["malformed_profile_entries"]
    assert isinstance(malformed, list) and malformed
    assert malformed[0]["concept_id"] == "#V#timeline_renderer"
    assert "json_decode_failed" in malformed[0]["error"]


def test_enrich_request_payload_from_concept_derives_type_and_predicates(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {
                "is_an_instance_of": ["#V#task"],
                "#V#has_start_time": ["#V#time_value"],
            },
        },
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda concept_id, predicate=None, limit=50: [
            {"predicate": "#V#has_due_time", "text": "2026-02-15"}
        ]
        if predicate is None
        else [],
    )

    enriched, diagnostics = service.enrich_request_payload_from_concept(
        {"concept_id": "#V#task_123"}
    )

    assert enriched["object_kind"] == "concept"
    assert enriched["concept_type_ids"] == ["#V#task"]
    assert "#V#has_start_time" in set(enriched["present_predicates"])
    assert "#V#has_due_time" in set(enriched["present_predicates"])
    assert diagnostics["concept_lookup_succeeded"] is True
    assert "concept_type_ids" in diagnostics["filled_fields"]
    assert "present_predicates" in diagnostics["filled_fields"]


def test_upsert_renderer_profile_validates_and_persists_singleton_text(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}},
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: {
            "success": True,
            "concept_id": kwargs["subject_concept_id"],
            "predicate": kwargs["predicate"],
            "language": kwargs["lang"],
            "kept_relation_id": "rel_1",
            "replaced_relation_ids": [],
            "replaced_count": 0,
            "relation_created": True,
            "text_value_id": "tv_1",
        },
    )

    result = service.upsert_renderer_profile(
        renderer_concept_id="#V#timeline_renderer",
        renderer_profile={
            "renderer_type": "timeline",
            "modalities": ["visual"],
            "applies_to_object_kinds": ["concept"],
            "priority": 90,
            "screen_element_families": ["timeline"],
        },
    )

    assert result["success"] is True
    assert result["renderer_concept_id"] == "#V#timeline_renderer"
    assert result["renderer_profile"]["renderer_id"] == "#V#timeline_renderer"
    assert result["renderer_profile"]["screen_element_families"] == ["timeline"]
    assert result["text_relation"]["kept_relation_id"] == "rel_1"


def test_bootstrap_canonical_renderer_profiles_persists_existing_only(
    monkeypatch,
) -> None:
    existing_concepts = {
        "#V#table_renderer",
        "#V#narration_renderer",
    }
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}}
        if concept_id in existing_concepts
        else None,
    )
    persisted: list[str] = []
    monkeypatch.setattr(
        service,
        "upsert_renderer_profile",
        lambda **kwargs: (
            persisted.append(kwargs["renderer_concept_id"]),
            {"success": True},
        )[1],
    )

    report = service.bootstrap_canonical_renderer_profiles(
        concept_ids=["#V#table_renderer", "#V#narration_renderer", "#V#workflow_renderer"]
    )

    assert report["success"] is False
    assert set(report["persisted_profile_concept_ids"]) == {
        "#V#table_renderer",
        "#V#narration_renderer",
    }
    assert report["missing_concept_ids"] == ["#V#workflow_renderer"]
    assert set(persisted) == {"#V#table_renderer", "#V#narration_renderer"}
