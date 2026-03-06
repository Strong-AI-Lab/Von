from src.backend.services import representation_contract_vontology_service as service


def test_canonical_representation_profile_concept_ids_are_deterministic() -> None:
    concept_ids = service.canonical_representation_profile_concept_ids()
    assert concept_ids == (
        "#V#representation_contract_profile_paper",
        "#V#representation_contract_profile_person",
        "#V#representation_contract_profile_company",
        "#V#representation_contract_profile_meeting",
    )


def test_load_representation_profiles_from_concept_ids_parses_profile_json(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda concept_id, predicate=None, limit=20: [
            {
                "predicate": predicate,
                "text": (
                    "{"
                    '"profile_id":"paper",'
                    '"target_entity_class":"scholarly_paper",'
                    '"effect_type":"scholarly_representation",'
                    '"description":"Represent paper metadata.",'
                    '"intent_patterns":["\\\\bpaper\\\\s+representation\\\\b"],'
                    '"domain_terms":["paper","arxiv"],'
                    '"required_tools_by_source":{"file_copy":["interpret_file_copy"]},'
                    '"required_predicates":["#V#paper"],'
                    '"default_decision_policy":{"completion_block_on_unresolved_effects":true}'
                    "}"
                ),
            }
        ]
        if predicate == "#V#has_representation_contract_profile_json"
        else [],
    )

    profiles, diagnostics = service.load_representation_contract_profiles_from_concept_ids(
        ["#V#representation_contract_profile_paper"]
    )

    assert len(profiles) == 1
    assert profiles[0]["profile_id"] == "paper"
    assert profiles[0]["target_entity_class"] == "scholarly_paper"
    assert profiles[0]["required_tools_by_source"]["file_copy"] == [
        "interpret_file_copy"
    ]
    assert diagnostics["loaded_concept_ids"] == [
        "#V#representation_contract_profile_paper"
    ]
    assert diagnostics["loaded_profile_count"] == 1
    assert isinstance(diagnostics.get("profile_version_hash"), str)


def test_load_representation_profiles_reports_malformed_profile_json(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}},
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda concept_id, predicate=None, limit=20: [
            {
                "predicate": predicate,
                "text": "{not valid json}",
            }
        ]
        if predicate == "#V#has_representation_contract_profile_json"
        else [],
    )

    profiles, diagnostics = service.load_representation_contract_profiles_from_concept_ids(
        ["#V#representation_contract_profile_paper"]
    )

    assert profiles == []
    assert diagnostics["loaded_profile_count"] == 0
    assert diagnostics["missing_profile_concept_ids"] == []
    assert diagnostics["malformed_profile_concept_ids"] == [
        "#V#representation_contract_profile_paper"
    ]
    malformed = diagnostics["malformed_profile_entries"]
    assert isinstance(malformed, list) and malformed
    assert malformed[0]["concept_id"] == "#V#representation_contract_profile_paper"
    assert "json_decode_failed" in malformed[0]["error"]


def test_upsert_representation_profile_validates_and_persists_singleton_text(
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

    result = service.upsert_representation_contract_profile(
        profile_concept_id="#V#representation_contract_profile_paper",
        representation_profile={
            "profile_id": "paper",
            "target_entity_class": "scholarly_paper",
            "effect_type": "scholarly_representation",
            "intent_patterns": [r"\bpaper\s+representation\b"],
            "domain_terms": ["paper", "arxiv"],
            "required_tools_by_source": {"file_copy": ["interpret_file_copy"]},
            "required_predicates": ["#V#paper"],
        },
    )

    assert result["success"] is True
    assert (
        result["profile_concept_id"] == "#V#representation_contract_profile_paper"
    )
    assert result["representation_profile"]["profile_id"] == "paper"
    assert result["text_relation"]["kept_relation_id"] == "rel_1"


def test_bootstrap_canonical_representation_profiles_persists_existing_only(
    monkeypatch,
) -> None:
    existing_concepts = {
        "#V#representation_contract_profile_paper",
        "#V#representation_contract_profile_person",
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
        "upsert_representation_contract_profile",
        lambda **kwargs: (
            persisted.append(kwargs["profile_concept_id"]),
            {"success": True},
        )[1],
    )

    report = service.bootstrap_canonical_representation_contract_profiles(
        concept_ids=[
            "#V#representation_contract_profile_paper",
            "#V#representation_contract_profile_person",
            "#V#representation_contract_profile_company",
        ]
    )

    assert report["success"] is False
    assert set(report["persisted_profile_concept_ids"]) == {
        "#V#representation_contract_profile_paper",
        "#V#representation_contract_profile_person",
    }
    assert report["missing_concept_ids"] == ["#V#representation_contract_profile_company"]
    assert set(persisted) == {
        "#V#representation_contract_profile_paper",
        "#V#representation_contract_profile_person",
    }


def test_ensure_canonical_representation_profiles_creates_missing_type_and_profiles(
    monkeypatch,
) -> None:
    existing_concepts: set[str] = set()
    created_concepts: list[dict[str, object]] = []
    persisted_profiles: list[str] = []

    def _fake_get_concept(concept_id):
        if concept_id in existing_concepts:
            return {"concept_id": concept_id, "relationships": {}}
        return None

    def _fake_create_concept(**kwargs):
        concept_id = kwargs["concept_id"]
        existing_concepts.add(concept_id)
        created_concepts.append(dict(kwargs))
        return {"concept_id": concept_id}

    def _fake_upsert_profile(**kwargs):
        persisted_profiles.append(kwargs["profile_concept_id"])
        return {"success": True, "profile_concept_id": kwargs["profile_concept_id"]}

    monkeypatch.setattr(service, "get_concept_by_concept_id", _fake_get_concept)
    monkeypatch.setattr(service.concept_service, "create_concept", _fake_create_concept)
    monkeypatch.setattr(
        service,
        "upsert_representation_contract_profile",
        _fake_upsert_profile,
    )

    report = service.ensure_canonical_representation_contract_profiles(
        concept_ids=[
            "#V#representation_contract_profile_paper",
            "#V#representation_contract_profile_person",
        ]
    )

    assert report["success"] is True
    assert report["profile_type_created"] is True
    assert report["profile_type_id"] == "#V#representation_contract_profile"
    assert set(report["created_profile_concept_ids"]) == {
        "#V#representation_contract_profile_paper",
        "#V#representation_contract_profile_person",
    }
    assert set(report["persisted_profile_concept_ids"]) == {
        "#V#representation_contract_profile_paper",
        "#V#representation_contract_profile_person",
    }
    assert report["missing_concept_ids"] == []
    assert report["unknown_canonical_profile_concept_ids"] == []
    assert report["errors_by_concept_id"] == {}

    created_ids = [entry["concept_id"] for entry in created_concepts]
    assert created_ids[0] == "#V#representation_contract_profile"
    assert set(created_ids[1:]) == {
        "#V#representation_contract_profile_paper",
        "#V#representation_contract_profile_person",
    }
    assert set(persisted_profiles) == {
        "#V#representation_contract_profile_paper",
        "#V#representation_contract_profile_person",
    }
