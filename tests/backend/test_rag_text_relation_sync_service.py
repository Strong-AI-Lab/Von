from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import mongomock
from bson import ObjectId


class _ConceptsCollection:
    def __init__(self, visible_concepts: set[str]):
        self.visible_concepts = set(visible_concepts)

    def find_one(
        self, query: Dict[str, Any], _projection: Optional[Dict[str, Any]] = None
    ):
        # Our service wraps the base filter with apply_concept_query_filter, so concept_id may be
        # nested under $and. Extract it loosely.
        def _extract_concept_id(q: Any) -> Optional[str]:
            if isinstance(q, dict):
                if "concept_id" in q and isinstance(q["concept_id"], str):
                    return q["concept_id"]
                if "$and" in q and isinstance(q["$and"], list):
                    for part in q["$and"]:
                        found = _extract_concept_id(part)
                        if found:
                            return found
                if "$or" in q and isinstance(q["$or"], list):
                    for part in q["$or"]:
                        found = _extract_concept_id(part)
                        if found:
                            return found
            return None

        concept_id = _extract_concept_id(query)
        if concept_id and concept_id in self.visible_concepts:
            return {"_id": "ok"}
        return None


class _StubRAG:
    def __init__(self):
        self.upserts = []
        self.deletes = []
        self.delete_batches = []

    def upsert_documents(self, docs, *, namespace=None, allow_partial_failures=True):
        self.upserts.extend(docs)
        return (len(docs), 0)

    def delete_documents(self, ids, *, namespace=None):
        batch = list(ids)
        self.delete_batches.append(batch)
        self.deletes.extend(batch)
        return len(batch)


def test_one_pass_iterator_opens_each_store_once_and_batches_visibility(
    monkeypatch,
):
    from src.backend.services import rag_text_relation_sync_service as svc

    text_value_ids = [ObjectId(), ObjectId()]
    base_relations = [
        {
            "_id": ObjectId(),
            "subject_concept_id": f"#V#subject_{index}",
            "predicate": "hasDescription",
            "object_text_id": text_value_ids[index],
        }
        for index in range(2)
    ]
    base_find_calls = []

    def _find_base(query, **kwargs):
        base_find_calls.append((query, kwargs))
        return iter(base_relations)

    monkeypatch.setattr(svc.TextRelationsRepository, "find", _find_base)
    text_value_find_calls = []

    def _find_text_values(query, _projection=None, **_kwargs):
        text_value_find_calls.append(query)
        return [
            {
                "_id": text_value_id,
                "text": f"Text {text_value_id}",
                "lang": "en-NZ",
            }
            for text_value_id in text_value_ids
        ]

    monkeypatch.setattr(
        svc.TextValuesRepository,
        "find",
        _find_text_values,
    )
    monkeypatch.setattr(
        svc.TextValuesRepository,
        "find_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("iterator must batch TextValue hydration")
        ),
    )

    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    for index in range(2):
        scoped_collection.insert_one(
            {
                "assertion_id": f"ska_{index}",
                "subject_concept_id": f"#V#subject_{index}",
                "predicate": "hasDescription",
                "object_kind": "text",
                "object_text": {
                    "text": f"Scoped {index}",
                    "language": "en-NZ",
                },
                "scope": {"audience_keys": ["org:#V#org"]},
                "status": "asserted",
            }
        )

    class _CountingCollection:
        def __init__(self, delegate):
            self.delegate = delegate
            self.find_calls = []

        def find(self, query):
            self.find_calls.append(query)
            return self.delegate.find(query)

    counting_scoped = _CountingCollection(scoped_collection)
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: counting_scoped,
    )
    visibility_batches = []

    def _visible(concept_ids, **_kwargs):
        batch = set(concept_ids)
        visibility_batches.append(batch)
        return batch

    monkeypatch.setattr(svc, "_visible_concept_ids_in_namespace", _visible)

    docs = list(
        svc.iter_text_relation_docs_for_namespace(
            namespace="#V#user@org",
            source_batch_size=2,
        )
    )

    assert len(base_find_calls) == 1
    assert base_find_calls[0][1]["sort"] == [("_id", 1)]
    assert len(text_value_find_calls) == 1
    assert set(text_value_find_calls[0]["_id"]["$in"]) == set(text_value_ids)
    assert len(counting_scoped.find_calls) == 1
    assert [doc.doc_id.split(":", 1)[0] for doc in docs] == [
        "text_relation",
        "text_relation",
        "scoped_assertion",
        "scoped_assertion",
    ]
    assert visibility_batches == [
        {
            "#V#subject_0",
            "#V#subject_1",
            svc.predicate_concept_id_for_storage("hasDescription"),
        },
        {
            "#V#subject_0",
            "#V#subject_1",
            svc.predicate_concept_id_for_storage("hasDescription"),
        },
    ]


def test_iterator_releases_working_caches_between_raw_batches(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    text_value_ids = [ObjectId() for _ in range(4)]
    base_relations = [
        {
            "_id": ObjectId(),
            "subject_concept_id": f"#V#subject_{index}",
            "predicate": "hasDescription",
            "object_text_id": text_value_ids[index],
        }
        for index in range(4)
    ]
    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: iter(base_relations),
    )
    monkeypatch.setattr(
        svc.TextValuesRepository,
        "find",
        lambda query, *_args, **_kwargs: [
            {
                "_id": text_value_id,
                "text": f"Text {text_value_id}",
                "lang": "en-NZ",
            }
            for text_value_id in query["_id"]["$in"]
        ],
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: None,
    )
    visibility_batches = []

    def _visible(concept_ids, **_kwargs):
        batch = set(concept_ids)
        visibility_batches.append(batch)
        return batch

    monkeypatch.setattr(svc, "_visible_concept_ids_in_namespace", _visible)
    cache_sizes = []
    original_projection = svc._base_relation_to_rag_doc

    def _capture_cache_sizes(relation, **kwargs):
        cache_sizes.append(
            (
                len(kwargs["concept_visibility"]),
                len(kwargs["text_value_cache"]),
            )
        )
        return original_projection(relation, **kwargs)

    monkeypatch.setattr(
        svc,
        "_base_relation_to_rag_doc",
        _capture_cache_sizes,
    )

    docs = list(
        svc.iter_text_relation_docs_for_namespace(
            namespace="#V#user@org",
            source_batch_size=2,
        )
    )

    predicate_id = svc.predicate_concept_id_for_storage("hasDescription")
    assert len(docs) == 4
    assert cache_sizes == [(3, 2), (3, 2), (3, 2), (3, 2)]
    assert visibility_batches == [
        {"#V#subject_0", "#V#subject_1", predicate_id},
        {"#V#subject_2", "#V#subject_3", predicate_id},
    ]


def test_iterator_flushes_stale_only_raw_batches(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    text_value_ids = [ObjectId() for _ in range(5)]
    base_relations = [
        {
            "_id": f"r{index}",
            "subject_concept_id": f"#V#blocked_{index}",
            "predicate": "hasDescription",
            "object_text_id": text_value_ids[index],
        }
        for index in range(5)
    ]
    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: iter(base_relations),
    )
    monkeypatch.setattr(
        svc.TextValuesRepository,
        "find",
        lambda query, *_args, **_kwargs: [
            {
                "_id": text_value_id,
                "text": f"Text {text_value_id}",
                "lang": "en-NZ",
            }
            for text_value_id in query["_id"]["$in"]
        ],
    )
    predicate_id = svc.predicate_concept_id_for_storage("hasDescription")
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: set(concept_ids) & {predicate_id},
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: None,
    )
    flushed = []

    docs = list(
        svc.iter_text_relation_docs_for_namespace(
            namespace="#V#user@org",
            source_batch_size=2,
            stale_doc_sink=lambda stale_ids: flushed.append(list(stale_ids)),
        )
    )

    assert docs == []
    assert flushed == [
        ["text_relation:r0", "text_relation:r1"],
        ["text_relation:r2", "text_relation:r3"],
        ["text_relation:r4"],
    ]


def test_sync_text_relations_indexes_visible_concepts(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    # Visible concept set for the namespace.
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: {
            concept_id
            for concept_id in concept_ids
            if concept_id
            in {
                "#V#allowed",
                svc.predicate_concept_id_for_storage("hasDescription"),
            }
        },
    )

    # Two relations: one visible, one not.
    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextRelationsRepository.find",
        lambda _filter, projection=None, sort=None, skip=0, limit=0: [
            {
                "_id": "r1",
                "subject_concept_id": "#V#allowed",
                "predicate": "hasDescription",
                "object_text_id": "000000000000000000000001",
            },
            {
                "_id": "r2",
                "subject_concept_id": "#V#blocked",
                "predicate": "hasDescription",
                "object_text_id": "000000000000000000000002",
            },
        ][skip : (skip + limit) if limit else None],
    )

    def _tv_find(filter_doc, _projection=None, **_kwargs):
        return [
            {
                "_id": oid,
                "text": (
                    "Hello world"
                    if str(oid).endswith("1")
                    else "Should not index"
                ),
                "lang": "en-NZ",
            }
            for oid in filter_doc["_id"]["$in"]
        ]

    monkeypatch.setattr(
        "src.backend.db.repositories.text_value_repository.TextValuesRepository.find",
        _tv_find,
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: None,
    )

    rag = _StubRAG()
    monkeypatch.setattr(svc, "get_rag_service", lambda *_args, **_kwargs: rag)

    result = svc.sync_text_relations_to_rag(namespace="#V#user@org", limit=100)
    assert result["success"] is True

    # Only the visible concept relation should be upserted.
    assert len(rag.upserts) == 1
    doc = rag.upserts[0]
    assert doc["id"] == "text_relation:r1"
    assert "Hello world" in doc["text"]

    meta = doc["metadata"]
    assert meta["type"] == "text_relation"
    assert meta["source"] == "vontology_text_relation"
    assert meta["concept_id"] == "#V#allowed"
    assert meta["subject_concept_id"] == "#V#allowed"
    assert meta["predicate"] == "hasDescription"
    assert meta["relation_id"] == "r1"
    assert meta["language"] == "en-NZ"
    assert rag.deletes == ["text_relation:r2"]
    assert result["deleted"] == 1

    # Required for permission filtering.
    assert meta["user_id"] == "#V#user"
    assert meta["organisation_concept_id"] == "#V#org"
    assert meta["org_id"] == "#V#org"


def test_full_sync_deletes_stale_only_batch_in_bounded_chunks(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    text_value_ids = [ObjectId() for _ in range(5)]
    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: iter(
            [
                {
                    "_id": f"r{index}",
                    "subject_concept_id": f"#V#blocked_{index}",
                    "predicate": "hasDescription",
                    "object_text_id": text_value_ids[index],
                }
                for index in range(5)
            ]
        ),
    )
    monkeypatch.setattr(
        svc.TextValuesRepository,
        "find",
        lambda query, *_args, **_kwargs: [
            {
                "_id": text_value_id,
                "text": f"Text {text_value_id}",
                "lang": "en-NZ",
            }
            for text_value_id in query["_id"]["$in"]
        ],
    )
    predicate_id = svc.predicate_concept_id_for_storage("hasDescription")
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: set(concept_ids) & {predicate_id},
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: None,
    )
    rag = _StubRAG()
    monkeypatch.setattr(svc, "get_rag_service", lambda *_args, **_kwargs: rag)

    result = svc.sync_text_relations_to_rag(
        namespace="#V#user@org",
        limit=100,
        batch_size=2,
    )

    assert result == {
        "success": True,
        "namespace": "#V#user@org",
        "total_candidates": 0,
        "added": 0,
        "failed": 0,
        "deleted": 5,
    }
    assert rag.upserts == []
    assert rag.delete_batches == [
        ["text_relation:r0", "text_relation:r1"],
        ["text_relation:r2", "text_relation:r3"],
        ["text_relation:r4"],
    ]


def test_collect_includes_scoped_assertion_for_namespace(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    scoped_collection.insert_one(
        {
            "assertion_id": "ska_123",
            "subject_concept_id": "#V#gillian_dobbie",
            "predicate": "hasDescription",
            "object_kind": "text",
            "object_text": {
                "text": "Organisation-scoped programme context.",
                "language": "en-NZ",
            },
            "scope": {
                "mode": "organisation",
                "audience_keys": ["org:#V#org"],
            },
            "provenance": {
                "asserted_by_user_concept_id": "#V#asserting_user",
                "organisation_concept_id": "#V#org",
                "turn_id": "turn-123",
            },
            "status": "asserted",
            "canonical_publication": False,
            "updated_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
        }
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: scoped_collection,
    )
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: set(concept_ids),
    )

    docs = svc.collect_text_relation_docs_for_namespace(
        namespace="#V#user@org",
    )

    assert len(docs) == 1
    assert docs[0].doc_id == "scoped_assertion:ska_123"
    assert "Organisation-scoped programme context." in docs[0].text
    assert docs[0].metadata["type"] == "scoped_knowledge_assertion"
    assert docs[0].metadata["assertion_id"] == "ska_123"
    assert docs[0].metadata["relation_id"] is None
    assert docs[0].metadata["row_kind"] == "scoped_assertion"
    assert docs[0].metadata["canonical_publication"] is False
    assert docs[0].metadata["organisation_concept_id"] == "#V#org"
    assert docs[0].metadata["audience_user_concept_id"] == "#V#user"
    assert docs[0].metadata["audience_organisation_concept_id"] == "#V#org"
    assert docs[0].metadata["audience_keys"] == ["org:#V#org"]
    assert docs[0].metadata["asserted_by_user_concept_id"] == "#V#asserting_user"
    assert docs[0].metadata["assertion_provenance"]["turn_id"] == "turn-123"


def test_combined_pages_respect_one_budget_without_repeating_scoped_rows(
    monkeypatch,
):
    from src.backend.services import rag_text_relation_sync_service as svc

    base_relations = [
        {
            "_id": f"r{index}",
            "subject_concept_id": "#V#allowed",
            "predicate": "hasDescription",
            "object_text_id": f"{index + 1:024d}",
        }
        for index in range(2)
    ]
    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda _filter, projection=None, sort=None, skip=0, limit=0: base_relations[
            skip : skip + limit
        ],
    )
    monkeypatch.setattr(
        svc.TextValuesRepository,
        "find",
        lambda query, _projection=None, **_kwargs: [
            {
                "_id": object_id,
                "text": f"Base {str(object_id)[-1]}",
                "lang": "en-NZ",
            }
            for object_id in query["_id"]["$in"]
        ],
    )
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: set(concept_ids),
    )

    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    for index in range(3):
        scoped_collection.insert_one(
            {
                "assertion_id": f"ska_{index}",
                "subject_concept_id": "#V#allowed",
                "predicate": "hasDescription",
                "object_kind": "text",
                "object_text": {
                    "text": f"Scoped {index}",
                    "language": "en-NZ",
                },
                "scope": {"audience_keys": ["org:#V#org"]},
                "status": "asserted",
                "updated_at": datetime(
                    2026,
                    7,
                    29,
                    12 - index,
                    tzinfo=timezone.utc,
                ),
            }
        )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: scoped_collection,
    )

    page_one = svc.collect_text_relation_docs_for_namespace(
        namespace="#V#user@org",
        skip=0,
        limit=2,
    )
    page_two = svc.collect_text_relation_docs_for_namespace(
        namespace="#V#user@org",
        skip=2,
        limit=2,
    )
    page_three = svc.collect_text_relation_docs_for_namespace(
        namespace="#V#user@org",
        skip=4,
        limit=2,
    )

    assert [doc.doc_id for doc in page_one] == ["text_relation:r0", "text_relation:r1"]
    assert [doc.doc_id for doc in page_two] == [
        "scoped_assertion:ska_0",
        "scoped_assertion:ska_1",
    ]
    assert [doc.doc_id for doc in page_three] == ["scoped_assertion:ska_2"]
    all_ids = [
        doc.doc_id for page in (page_one, page_two, page_three) for doc in page
    ]
    assert len(all_ids) == len(set(all_ids))
    assert all(len(page) <= 2 for page in (page_one, page_two, page_three))


def test_scoped_visible_pages_scan_past_revoked_raw_rows_without_duplicates(
    monkeypatch,
):
    from src.backend.services import rag_text_relation_sync_service as svc

    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    subjects = ["#V#revoked", "#V#a", "#V#b", "#V#c"]
    for index, subject_id in enumerate(subjects):
        scoped_collection.insert_one(
            {
                "assertion_id": f"ska_{index}",
                "subject_concept_id": subject_id,
                "predicate": "hasDescription",
                "object_kind": "text",
                "object_text": {
                    "text": f"Scoped {subject_id}",
                    "language": "en-NZ",
                },
                "scope": {"audience_keys": ["org:#V#org"]},
                "status": "asserted",
                "updated_at": datetime(
                    2026,
                    7,
                    29,
                    12 - index,
                    tzinfo=timezone.utc,
                ),
            }
        )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: scoped_collection,
    )
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: {
            concept_id
            for concept_id in concept_ids
            if concept_id != "#V#revoked"
        },
    )

    pages = [
        svc.collect_text_relation_docs_for_namespace(
            namespace="#V#user@org",
            skip=offset,
            limit=1,
        )
        for offset in range(3)
    ]

    assert [[doc.doc_id for doc in page] for page in pages] == [
        ["scoped_assertion:ska_1"],
        ["scoped_assertion:ska_2"],
        ["scoped_assertion:ska_3"],
    ]
    all_ids = [doc.doc_id for page in pages for doc in page]
    assert len(all_ids) == len(set(all_ids))


def test_scoped_projection_requires_current_subject_and_predicate_visibility(
    monkeypatch,
):
    from src.backend.services import rag_text_relation_sync_service as svc

    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    scoped_collection.insert_one(
        {
            "assertion_id": "ska_hidden_predicate",
            "subject_concept_id": "#V#subject",
            "predicate": "#V#private_predicate",
            "object_kind": "text",
            "object_text": {"text": "Scoped.", "language": "en-NZ"},
            "scope": {"audience_keys": ["org:#V#org"]},
            "status": "asserted",
            "updated_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
        }
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: scoped_collection,
    )
    checked = []

    def _visible(concept_ids, **_kwargs):
        checked.append(set(concept_ids))
        return {
            concept_id
            for concept_id in concept_ids
            if concept_id != "#V#private_predicate"
        }

    monkeypatch.setattr(svc, "_visible_concept_ids_in_namespace", _visible)

    docs = svc.collect_text_relation_docs_for_namespace(
        namespace="#V#user@org",
        limit=1,
    )

    assert docs == []
    assert checked == [{"#V#subject", "#V#private_predicate"}]


def test_collect_limit_zero_does_not_query_either_store(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    def _unexpected_query(*_args, **_kwargs):
        raise AssertionError("limit=0 must not query a backing store")

    monkeypatch.setattr(svc.TextRelationsRepository, "find", _unexpected_query)
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        _unexpected_query,
    )

    assert (
        svc.collect_text_relation_docs_for_namespace(
            namespace="#V#user@org",
            limit=0,
        )
        == []
    )


def test_scoped_query_uses_audience_status_time_shape_and_mapped_page(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    class _Cursor:
        def __init__(self):
            self.sort_spec = None
            self.skip_value = None
            self.limit_value = None

        def sort(self, spec):
            self.sort_spec = spec
            return self

        def skip(self, value):
            self.skip_value = value
            return self

        def limit(self, value):
            self.limit_value = value
            return self

        def __iter__(self):
            return iter(())

    class _Collection:
        def __init__(self):
            self.query = None
            self.cursor = _Cursor()

        def find(self, query):
            self.query = query
            return self.cursor

    collection = _Collection()
    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    updated_since = datetime(2026, 7, 1, tzinfo=timezone.utc)

    svc.collect_text_relation_docs_for_namespace(
        namespace="#V#user@org",
        predicates=["hasDescription"],
        languages=["en-NZ"],
        updated_since=updated_since,
        skip=7,
        limit=3,
    )

    assert collection.query == {
        "scope.audience_keys": {
            "$in": ["user:#V#user", "org:#V#org"],
        },
        "status": "asserted",
        "object_kind": "text",
        "updated_at": {"$gte": updated_since},
        "predicate": {"$in": ["hasDescription"]},
        "object_text.language": {"$in": ["en-NZ"]},
    }
    assert collection.cursor.sort_spec == [
        ("updated_at", -1),
        ("assertion_id", 1),
    ]
    # Public offsets count visible projections. The raw cursor starts at zero
    # and scans through revoked/inaccessible candidates until that visible
    # offset and page budget are satisfied or storage is exhausted.
    assert collection.cursor.skip_value == 0
    assert collection.cursor.limit_value == 10


def test_sync_reindexes_scoped_rows_when_namespace_has_no_base_rows(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: set(concept_ids),
    )
    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    scoped_collection.insert_one(
        {
            "assertion_id": "ska_only",
            "subject_concept_id": "#V#only_scoped",
            "predicate": "hasDescription",
            "object_kind": "text",
            "object_text": {"text": "Only scoped.", "language": "en-NZ"},
            "scope": {"audience_keys": ["user:#V#user"]},
            "status": "asserted",
            "updated_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
        }
    )
    scoped_collection.insert_one(
        {
            "assertion_id": "ska_retracted",
            "subject_concept_id": "#V#only_scoped",
            "predicate": "hasDescription",
            "object_kind": "text",
            "object_text": {
                "text": "Retracted scoped.",
                "language": "en-NZ",
            },
            "scope": {"audience_keys": ["user:#V#user"]},
            "status": "retracted",
            "updated_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
        }
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: scoped_collection,
    )
    rag = _StubRAG()
    monkeypatch.setattr(svc, "get_rag_service", lambda *_args, **_kwargs: rag)

    result = svc.sync_text_relations_to_rag(
        namespace="#V#user@org",
        limit=10,
    )

    assert result == {
        "success": True,
        "namespace": "#V#user@org",
        "total_candidates": 1,
        "added": 1,
        "failed": 0,
        "deleted": 1,
    }
    assert [doc["id"] for doc in rag.upserts] == ["scoped_assertion:ska_only"]
    assert rag.deletes == ["scoped_assertion:ska_retracted"]


def test_list_index_items_includes_scoped_candidates_without_relation_id(
    monkeypatch,
):
    from src.backend.services import rag_text_relation_sync_service as svc

    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: set(concept_ids),
    )
    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    scoped_collection.insert_one(
        {
            "assertion_id": "ska_listed",
            "subject_concept_id": "#V#subject",
            "predicate": "hasDescription",
            "object_kind": "text",
            "object_text": {"text": "Listed scoped.", "language": "en-NZ"},
            "scope": {"audience_keys": ["org:#V#org"]},
            "status": "asserted",
            "updated_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
        }
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: scoped_collection,
    )

    report = svc.list_text_relation_index_items(
        namespace="#V#user@org",
        limit=10,
        scan_limit=10,
    )

    assert report["total"] == 1
    assert len(report["items"]) == 1
    item = report["items"][0]
    assert item["index_item_id"] == "scoped_assertion:ska_listed"
    assert item["row_kind"] == "scoped_assertion"
    assert item["relation_id"] is None
    assert item["assertion_id"] == "ska_listed"
    assert item["preview_length"] == len("Listed scoped.")
    assert item["source"] == "scoped_knowledge_assertion"


def test_get_preview_supports_scoped_identifier_with_live_authority_checks(
    monkeypatch,
):
    from src.backend.services import rag_text_relation_sync_service as svc

    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    scoped_collection.insert_one(
        {
            "assertion_id": "ska_preview",
            "subject_concept_id": "#V#subject",
            "predicate": "hasDescription",
            "object_kind": "text",
            "object_text": {"text": "Scoped preview.", "language": "en-NZ"},
            "scope": {"audience_keys": ["org:#V#org"]},
            "status": "asserted",
        }
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: scoped_collection,
    )
    visible_concepts = {"#V#subject", "#V#hasDescription"}
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: set(concept_ids) & visible_concepts,
    )

    prefixed = svc.get_text_relation_preview(
        namespace="#V#user@org",
        relation_id="scoped_assertion:ska_preview",
    )
    exact = svc.get_text_relation_preview(
        namespace="#V#user@org",
        relation_id="ska_preview",
    )

    assert prefixed is not None
    assert exact is not None
    assert prefixed.doc_id == "scoped_assertion:ska_preview"
    assert prefixed.metadata["assertion_id"] == "ska_preview"
    assert prefixed.metadata["relation_id"] is None

    scoped_collection.update_one(
        {"assertion_id": "ska_preview"},
        {"$set": {"status": "retracted"}},
    )
    assert (
        svc.get_text_relation_preview(
            namespace="#V#user@org",
            relation_id="ska_preview",
        )
        is None
    )

    scoped_collection.update_one(
        {"assertion_id": "ska_preview"},
        {
            "$set": {
                "status": "asserted",
                "scope.audience_keys": ["org:#V#other"],
            }
        },
    )
    assert (
        svc.get_text_relation_preview(
            namespace="#V#user@org",
            relation_id="ska_preview",
        )
        is None
    )

    scoped_collection.update_one(
        {"assertion_id": "ska_preview"},
        {"$set": {"scope.audience_keys": ["org:#V#org"]}},
    )
    visible_concepts.remove("#V#hasDescription")
    assert (
        svc.get_text_relation_preview(
            namespace="#V#user@org",
            relation_id="ska_preview",
        )
        is None
    )


def test_exact_scoped_sync_bypasses_base_first_scan_budget(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    monkeypatch.setattr(
        svc.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact scoped sync must not scan base relations")
        ),
    )
    monkeypatch.setattr(
        svc,
        "_visible_concept_ids_in_namespace",
        lambda concept_ids, **_kwargs: set(concept_ids),
    )
    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    scoped_collection.insert_one(
        {
            "assertion_id": "ska_exact",
            "subject_concept_id": "#V#subject",
            "predicate": "hasDescription",
            "object_kind": "text",
            "object_text": {"text": "Exact scoped.", "language": "en-NZ"},
            "scope": {"audience_keys": ["org:#V#org"]},
            "status": "asserted",
        }
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: scoped_collection,
    )
    rag = _StubRAG()
    monkeypatch.setattr(svc, "get_rag_service", lambda *_args, **_kwargs: rag)

    result = svc.sync_scoped_assertions_to_rag(
        namespace="#V#user@org",
        assertion_ids=["ska_exact"],
    )

    assert result["success"] is True
    assert result["sync_scope"] == "exact_scoped_assertions"
    assert [row["id"] for row in rag.upserts] == ["scoped_assertion:ska_exact"]


def test_exact_scoped_sync_removes_retracted_derived_document(monkeypatch):
    from src.backend.services import rag_text_relation_sync_service as svc

    scoped_collection = mongomock.MongoClient()["von_test"][
        "scoped_knowledge_assertions"
    ]
    scoped_collection.insert_one(
        {
            "assertion_id": "ska_retracted",
            "subject_concept_id": "#V#subject",
            "predicate": "hasDescription",
            "object_kind": "text",
            "object_text": {"text": "Retracted.", "language": "en-NZ"},
            "scope": {"audience_keys": ["org:#V#org"]},
            "status": "retracted",
        }
    )
    monkeypatch.setattr(
        svc,
        "get_scoped_knowledge_assertions_collection",
        lambda: scoped_collection,
    )
    rag = _StubRAG()
    monkeypatch.setattr(svc, "get_rag_service", lambda *_args, **_kwargs: rag)

    result = svc.sync_scoped_assertions_to_rag(
        namespace="#V#user@org",
        assertion_ids=["ska_retracted"],
    )

    assert result["success"] is True
    assert result["added"] == 0
    assert result["deleted"] == 1
    assert rag.deletes == ["scoped_assertion:ska_retracted"]
