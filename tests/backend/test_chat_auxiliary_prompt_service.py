

def test_build_user_specific_system_prompt_returns_none_for_blank_user_id(monkeypatch):
    from src.backend.services.chat_auxiliary_prompt_service import (
        build_user_specific_system_prompt,
    )

    assert build_user_specific_system_prompt("") is None
    assert build_user_specific_system_prompt("   ") is None


def test_build_user_specific_system_prompt_concatenates_multiple_prompts(monkeypatch):
    from src.backend.services import chat_auxiliary_prompt_service as service

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#prompt_a"},
            {"concept_id": "#V#prompt_b"},
        ],
    )

    def _fake_get_content(concept_id):
        if concept_id == "#V#prompt_a":
            return "First"
        if concept_id == "#V#prompt_b":
            return "Second\n"
        raise AssertionError(f"Unexpected concept_id {concept_id}")

    monkeypatch.setattr(service, "_get_prompt_content_for_concept", _fake_get_content)

    result = service.build_user_specific_system_prompt("#V#michael_witbrock")
    assert result == "First\n\nSecond"


def test_build_user_specific_system_prompt_skips_missing_content(monkeypatch):
    from src.backend.services import chat_auxiliary_prompt_service as service

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#prompt_a"},
            {"concept_id": "#V#prompt_b"},
        ],
    )

    def _fake_get_content(concept_id):
        if concept_id == "#V#prompt_a":
            return "   "
        if concept_id == "#V#prompt_b":
            return None
        raise AssertionError(f"Unexpected concept_id {concept_id}")

    monkeypatch.setattr(service, "_get_prompt_content_for_concept", _fake_get_content)

    assert service.build_user_specific_system_prompt("#V#michael_witbrock") is None


def test_get_user_specific_prompt_fragments_returns_ids_and_content(monkeypatch):
    from src.backend.services import chat_auxiliary_prompt_service as service

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#prompt_a"},
            {"concept_id": "#V#prompt_b"},
        ],
    )

    def _fake_get_content(concept_id):
        return f"Content for {concept_id}"

    monkeypatch.setattr(service, "_get_prompt_content_for_concept", _fake_get_content)

    fragments = service.get_user_specific_prompt_fragments("#V#michael_witbrock")
    assert fragments == [
        {"concept_id": "#V#prompt_a", "content": "Content for #V#prompt_a"},
        {"concept_id": "#V#prompt_b", "content": "Content for #V#prompt_b"},
    ]


def test_get_user_specific_prompt_fragments_falls_back_to_text_relations(monkeypatch):
    from src.backend.services import chat_auxiliary_prompt_service as service

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": "#V#prompt_a"},
        ],
    )

    monkeypatch.setattr(
        service, "get_concept_by_concept_id", lambda _cid: {"concept_id": _cid}
    )
    monkeypatch.setattr(
        service,
        "get_texts_for_concept",
        lambda _cid: [
            {"predicate": "hasName", "text": "Ignored"},
            {"predicate": "#V#hasContent", "lang": "en-NZ", "text": "Primary"},
        ],
    )

    fragments = service.get_user_specific_prompt_fragments("#V#michael_witbrock")
    assert fragments == [{"concept_id": "#V#prompt_a", "content": "Primary"}]


def test_get_user_specific_prompt_fragments_queries_multiple_scoping_predicates(
    monkeypatch,
):
    from src.backend.services import chat_auxiliary_prompt_service as service

    captured = {}

    def _fake_find(filter_doc, **_kwargs):
        captured["filter"] = filter_doc
        return []

    monkeypatch.setattr(service.ConceptsRepository, "find", _fake_find)

    service.get_user_specific_prompt_fragments("#V#michael_witbrock")

    filter_doc = captured.get("filter")
    assert isinstance(filter_doc, dict)
    type_filter = filter_doc.get("relationships.is_an_instance_of")
    assert isinstance(type_filter, dict)
    # Default prompt type scoping includes the NZ spelling plus a legacy US-spelling
    # concept ID that has been observed in stored data.
    assert type_filter.get("$in") == [
        "#V#von_chat_behaviour_prompt",
        "#V#von_chat_behavior_prompt",
        "#V#von_llm_prompt",
    ]

    ors = filter_doc.get("$or")
    assert isinstance(ors, list)

    expected_fields = {
        "relationships.#V#specific_to_von_user",
        "relationships.specific_to_von_user",
        "relationships.specific_to_user",
    }
    seen_fields = set()
    for clause in ors:
        assert isinstance(clause, dict)
        ((key, value),) = clause.items()
        if key in expected_fields:
            seen_fields.add(key)
            assert value == "#V#michael_witbrock"

    assert seen_fields == expected_fields
