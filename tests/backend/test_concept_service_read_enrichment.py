from src.backend.services import concept_service, text_value_service


def test_enrichment_is_read_only_and_preserves_legacy_text(monkeypatch):
    calls: list[str] = []

    def _get_texts(*, predicate, **_kwargs):
        calls.append(predicate)
        if predicate == "hasName":
            return [
                {
                    "text": "Relation name",
                    "lang": "en-NZ",
                    "context": {"name_type": "NL"},
                    "relation_id": "rel-1",
                }
            ]
        return []

    def _unexpected_write(*_args, **_kwargs):
        raise AssertionError("read enrichment must not write")

    monkeypatch.setattr(text_value_service, "get_texts_for_concept", _get_texts)
    monkeypatch.setattr(text_value_service, "upsert_text_for_concept", _unexpected_write)
    monkeypatch.setattr(
        concept_service.ConceptsRepository,
        "update_one",
        _unexpected_write,
    )

    enriched = concept_service.enrich_concept_with_text_relations(
        {
            "concept_id": "#V#legacy_projection",
            "names": [
                {"name": "Legacy name", "language": "en", "type": "NL"},
            ],
            "description": "Legacy description",
        }
    )

    assert [item["name"] for item in enriched["names"][:2]] == [
        "Relation name",
        "Legacy name",
    ]
    assert enriched["names"][1]["relation_id"] is None
    assert enriched["description"] == "Legacy description"
    assert calls == ["hasName", "hasContent", "hasNote"]


def test_enrichment_deduplicates_legacy_name_already_in_relations(monkeypatch):
    def _get_texts(*, predicate, **_kwargs):
        if predicate == "hasName":
            return [
                {
                    "text": "Same name",
                    "lang": "en",
                    "context": {"name_type": "NL"},
                    "relation_id": "rel-1",
                }
            ]
        return []

    monkeypatch.setattr(text_value_service, "get_texts_for_concept", _get_texts)

    enriched = concept_service.enrich_concept_with_text_relations(
        {
            "concept_id": "#V#deduplicated_projection",
            "names": [{"name": "Same name", "language": "en", "type": "NL"}],
        }
    )

    assert [item["name"] for item in enriched["names"]].count("Same name") == 1
