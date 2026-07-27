from src.backend.services import concept_service, text_value_service


def test_enrichment_is_read_only_and_preserves_legacy_text(monkeypatch):
    calls: list[tuple[list[str], tuple[str, ...], int]] = []

    def _get_texts(concept_ids, *, predicates, limit_per_concept, **_kwargs):
        calls.append((list(concept_ids), tuple(predicates), limit_per_concept))
        return {
            "#V#legacy_projection": [
                {
                    "text": "Relation name",
                    "lang": "en-NZ",
                    "predicate": "hasName",
                    "context": {"name_type": "NL"},
                    "relation_id": "rel-1",
                }
            ]
        }

    def _unexpected_write(*_args, **_kwargs):
        raise AssertionError("read enrichment must not write")

    monkeypatch.setattr(text_value_service, "get_texts_for_concepts", _get_texts)
    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete batch must not issue per-predicate reads")
        ),
    )
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
    assert calls == [
        (
            ["#V#legacy_projection"],
            ("hasName", "hasContent", "hasNote"),
            102,
        )
    ]


def test_enrichment_deduplicates_legacy_name_already_in_relations(monkeypatch):
    def _get_texts(concept_ids, **_kwargs):
        return {
            concept_ids[0]: [
                {
                    "text": "Same name",
                    "lang": "en",
                    "predicate": "hasName",
                    "context": {"name_type": "NL"},
                    "relation_id": "rel-1",
                }
            ]
        }

    monkeypatch.setattr(text_value_service, "get_texts_for_concepts", _get_texts)

    enriched = concept_service.enrich_concept_with_text_relations(
        {
            "concept_id": "#V#deduplicated_projection",
            "names": [{"name": "Same name", "language": "en", "type": "NL"}],
        }
    )

    assert [item["name"] for item in enriched["names"]].count("Same name") == 1


def test_enrichment_falls_back_when_combined_batch_reaches_cap(monkeypatch):
    batched_rows = [
        {
            "text": f"Batch name {index}",
            "lang": "en",
            "predicate": "hasName",
            "context": {"name_type": "NL"},
            "relation_id": f"batch-{index}",
        }
        for index in range(102)
    ]
    single_calls: list[str] = []

    def _truncated_batch(concept_ids, *, query_metadata, **_kwargs):
        query_metadata["relation_query_truncated"] = True
        return {concept_ids[0]: batched_rows}

    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concepts",
        _truncated_batch,
    )

    def _get_texts(*, predicate, **_kwargs):
        single_calls.append(predicate)
        return {
            "hasName": [
                {
                    "text": "Exact fallback name",
                    "lang": "en",
                    "context": {"name_type": "NL"},
                    "relation_id": "fallback-name",
                }
            ],
            "hasContent": [{"text": "Exact fallback content"}],
            "hasNote": [{"text": "Exact fallback note"}],
        }[predicate]

    monkeypatch.setattr(text_value_service, "get_texts_for_concept", _get_texts)

    enriched = concept_service.enrich_concept_with_text_relations(
        {"concept_id": "#V#name_heavy_projection"}
    )

    assert single_calls == ["hasName", "hasContent", "hasNote"]
    assert enriched["names"][0]["name"] == "Exact fallback name"
    assert enriched["content"] == "Exact fallback content"
    assert enriched["note"] == "Exact fallback note"


def test_enrichment_uses_complete_102_relation_batch_without_fallback(monkeypatch):
    batched_rows = [
        {
            "text": f"Name {index}",
            "lang": "en",
            "predicate": "hasName",
            "context": {"name_type": "NL"},
            "relation_id": f"name-{index}",
        }
        for index in range(100)
    ]
    batched_rows.extend(
        [
            {
                "text": "Complete content",
                "lang": "en",
                "predicate": "hasContent",
                "relation_id": "content-1",
            },
            {
                "text": "Complete note",
                "lang": "en",
                "predicate": "hasNote",
                "relation_id": "note-1",
            },
        ]
    )

    def _complete_batch(concept_ids, *, query_metadata, **_kwargs):
        query_metadata["relation_query_truncated"] = False
        return {concept_ids[0]: batched_rows}

    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concepts",
        _complete_batch,
    )
    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concept",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete raw relation batch must not fall back")
        ),
    )

    enriched = concept_service.enrich_concept_with_text_relations(
        {"concept_id": "#V#complete_102_projection"}
    )

    assert len(
        [item for item in enriched["names"] if item["relation_id"] is not None]
    ) == 100
    assert enriched["content"] == "Complete content"
    assert enriched["note"] == "Complete note"


def test_text_batch_truncation_uses_raw_relations_not_joined_rows(monkeypatch):
    concept_id = "#V#raw_relation_sentinel"
    relations = [
        {
            "_id": f"rel-{index}",
            "subject_concept_id": concept_id,
            "object_text_id": f"tv-{index}",
            "predicate": "hasName",
        }
        for index in range(103)
    ]
    text_values = [
        {"_id": f"tv-{index}", "text": f"Name {index}", "lang": "en"}
        for index in range(102)
    ]

    monkeypatch.setattr(
        text_value_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    monkeypatch.setattr(
        text_value_service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: relations,
    )
    monkeypatch.setattr(
        text_value_service.TextValuesRepository,
        "find",
        lambda *_args, **_kwargs: text_values,
    )
    metadata = {}

    rows = text_value_service.get_texts_for_concepts(
        [concept_id],
        predicates=("hasName", "hasContent", "hasNote"),
        limit_per_concept=102,
        query_metadata=metadata,
    )[concept_id]

    assert len(rows) == 102
    assert metadata == {
        "raw_relation_count": 103,
        "relation_query_limit": 103,
        "relation_query_truncated": True,
    }
