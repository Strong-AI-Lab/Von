from __future__ import annotations

from src.backend.services.chat_concept_reference_service import (
    build_context_concept_reference_metadata,
)


def test_build_context_concept_reference_metadata_classifies_and_marks_missing(
    monkeypatch,
):
    node_content_calls = []

    def _stub_node_content(
        concept_id: str,
        *,
        reconstruct_md: bool = True,
        resolve_display_name: bool = True,
    ):
        node_content_calls.append(
            (concept_id, reconstruct_md, resolve_display_name)
        )
        if concept_id == "#V#person":
            return {
                "concept_id": concept_id,
                "display_name": "Person (legacy)",
                "kind": "type",
                "is_a_type_of": ["#V#agent"],
            }
        if concept_id == "#V#has_email":
            return {
                "concept_id": concept_id,
                "display_name": "Has Email",
                "kind": "predicate",
                "is_a_type_of": [],
            }
        return {"error": "not found"}

    def _stub_texts_for_concept(*, subject_concept_id: str, **_kwargs):
        if subject_concept_id == "#V#person":
            return [
                {
                    "text": "Person",
                    "lang": "en-NZ",
                    "context": {"name_type": "NL"},
                }
            ]
        return []

    def _stub_find(*_args, **_kwargs):
        return [
            {
                "concept_id": "#V#agent",
                "name": "Agent",
                "names": [{"name": "Agent", "language": "en-NZ", "type": "NL"}],
            }
        ]

    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.get_vontology_node_content",
        _stub_node_content,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.get_texts_for_concept",
        _stub_texts_for_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.ConceptsRepository.find",
        _stub_find,
    )

    payload = build_context_concept_reference_metadata(
        [{"role": "user", "content": "Use #V#person, #V#has_email, and #V#missing."}],
        max_concepts=10,
        include_direct_supertypes=True,
        max_direct_supertypes=3,
    )

    by_id = {entry["concept_id"]: entry for entry in payload["concepts"]}

    assert by_id["#V#person"]["exists"] is True
    assert by_id["#V#person"]["kind"] == "type"
    assert by_id["#V#person"]["name"] == "Person"
    assert by_id["#V#person"]["direct_supertypes"] == [
        {"concept_id": "#V#agent", "name": "Agent"}
    ]

    assert by_id["#V#has_email"]["exists"] is True
    assert by_id["#V#has_email"]["kind"] == "predicate"

    assert by_id["#V#missing"]["exists"] is False
    assert by_id["#V#missing"]["kind"] is None
    assert by_id["#V#missing"]["name"] is None
    assert node_content_calls == [
        ("#V#person", False, False),
        ("#V#has_email", False, False),
        ("#V#missing", False, False),
    ]


def test_build_context_concept_reference_metadata_enforces_concept_cap(monkeypatch):
    def _stub_node_content(
        concept_id: str,
        *,
        reconstruct_md: bool = True,
        resolve_display_name: bool = True,
    ):
        return {
            "concept_id": concept_id,
            "display_name": concept_id,
            "kind": "individual",
            "is_a_type_of": [],
        }

    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.get_vontology_node_content",
        _stub_node_content,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.get_texts_for_concept",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.ConceptsRepository.find",
        lambda *_args, **_kwargs: [],
    )

    payload = build_context_concept_reference_metadata(
        [{"role": "assistant", "content": "#V#one #V#two #V#three #V#four"}],
        max_concepts=2,
        include_direct_supertypes=False,
    )

    assert payload["concept_count"] == 2
    assert payload["concept_count_capped"] is True
    assert [entry["concept_id"] for entry in payload["concepts"]] == [
        "#V#one",
        "#V#two",
    ]


def test_build_context_concept_reference_metadata_attaches_stats_when_available(
    monkeypatch,
):
    def _stub_node_content(
        concept_id: str,
        *,
        reconstruct_md: bool = True,
        resolve_display_name: bool = True,
    ):
        return {
            "concept_id": concept_id,
            "display_name": concept_id,
            "kind": "type",
            "is_a_type_of": [],
        }

    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.get_vontology_node_content",
        _stub_node_content,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.get_texts_for_concept",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.services.chat_concept_reference_service.ConceptsRepository.find",
        lambda *_args, **_kwargs: [],
    )

    def _stub_stats(concept_ids, **_kwargs):
        return {
            "stats_status": "available",
            "concept_stats": {
                concept_id: {
                    "kind": "type",
                    "stats_status": "available",
                    "stats_generated_at": "2026-02-10T08:00:00Z",
                    "direct_instance_count": 1,
                    "total_instance_count_in_subtree": 1,
                    "has_any_instances_in_subtree": True,
                    "direct_subtype_count": 0,
                    "total_subtype_count_in_subtree": 0,
                }
                for concept_id in concept_ids
            },
        }

    monkeypatch.setattr(
        "src.backend.services.vontology_concept_stats_service.get_vontology_concept_stats",
        _stub_stats,
    )

    payload = build_context_concept_reference_metadata(
        [{"role": "assistant", "content": "#V#one"}],
        max_concepts=10,
        include_direct_supertypes=False,
        include_stats=True,
        stats_rebuild_if_needed=False,
    )
    by_id = {entry["concept_id"]: entry for entry in payload["concepts"]}

    assert by_id["#V#one"]["stats"]["stats_status"] == "available"
    assert by_id["#V#one"]["stats"]["direct_instance_count"] == 1
