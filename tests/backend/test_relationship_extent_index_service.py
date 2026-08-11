import pytest
from flask import Flask

mongomock = pytest.importorskip("mongomock")


@pytest.fixture(autouse=True)
def _reset_relationship_extent_readiness_cache():
    from src.backend.security import access_control
    from src.backend.services import relationship_extent_index_service as service

    service._READINESS_CACHE.update({"ready": None, "checked_at": 0.0})
    service._SUCCESSFUL_OPERATION_MAX_SECONDS.clear()
    access_control.invalidate_current_access_evaluator()
    yield
    service._READINESS_CACHE.update({"ready": None, "checked_at": 0.0})
    service._SUCCESSFUL_OPERATION_MAX_SECONDS.clear()
    access_control.invalidate_current_access_evaluator()


def test_relationship_extent_sync_reports_advisory_without_hard_deadline(monkeypatch):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    monkeypatch.setattr(service, "RELATIONSHIP_EXTENT_SYNC_ADVISORY_SECONDS", 0.1)
    monotonic_times = iter([0.0, 0.2])
    monkeypatch.setattr(service.time, "perf_counter", lambda: next(monotonic_times))
    monkeypatch.setattr(
        service,
        "get_relationship_extent_index_collection",
        lambda: client.db.relationship_extent_index,
    )

    result = service.sync_relationship_extent_index_for_concept_doc(
        {
            "concept_id": "#V#source",
            "relationships": {"#V#mentions": ["#V#target"]},
        }
    )

    assert result["success"] is True
    assert result["elapsed_time_enforcement"] == "advisory"
    assert result["advisory_seconds"] == 0.1
    assert result["advisory_exceeded"] is True
    assert result["hard_timeout_seconds"] is None


def test_relationship_extent_sync_completes_concept_read_and_writes(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    coll = client.db.relationship_extent_index
    observed_operations: list[str] = []

    def fake_find_one(_query, _projection):
        observed_operations.append("find_one")
        return {
            "concept_id": "#V#source",
            "relationships": {"#V#mentions": ["#V#target"]},
        }

    real_delete_many = coll.delete_many
    real_replace_one = coll.replace_one

    def tracked_delete_many(query):
        observed_operations.append("delete_many")
        return real_delete_many(query)

    def tracked_replace_one(query, document, *, upsert):
        observed_operations.append("replace_one")
        return real_replace_one(query, document, upsert=upsert)

    monkeypatch.setattr(service.ConceptsRepository, "find_one", fake_find_one)
    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: coll
    )
    monkeypatch.setattr(coll, "delete_many", tracked_delete_many)
    monkeypatch.setattr(coll, "replace_one", tracked_replace_one)

    result = service._sync_relationship_extent_index_for_concept_id_now(
        "#V#source"
    )

    assert result["success"] is True
    assert result["elapsed_time_enforcement"] == "advisory"
    assert result["hard_timeout_seconds"] is None
    assert observed_operations == ["find_one", "replace_one", "delete_many"]


def test_relationship_extent_readiness_failure_fails_closed_without_deadline(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as service

    class FailingSettings:
        def find_one(self, *_args, **_kwargs):
            raise RuntimeError("simulated readiness failure")

    monkeypatch.setattr(
        service,
        "get_application_settings_collection",
        lambda: FailingSettings(),
    )

    assert service.relationship_extent_index_ready() is False


def test_relationship_extent_readiness_requires_explicit_complete_state(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    index = client.db.relationship_extent_index
    settings = client.db.application_settings
    index.insert_one(
        {
            "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
            "relation_id": "one-incrementally-written-row",
        }
    )
    monkeypatch.setattr(
        service,
        "get_relationship_extent_index_collection",
        lambda: index,
    )
    monkeypatch.setattr(
        service,
        "get_application_settings_collection",
        lambda: settings,
    )

    assert service.relationship_extent_index_ready() is False
    assert service.query_relationship_extent_index(
        target_value="#V#missing-from-partial-index"
    ) == ([], -1)

    settings.insert_one(
        {
            "setting_name": service.RELATIONSHIP_EXTENT_INDEX_STATE_SETTING,
            "value": {
                "status": "ready",
                "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
            },
        }
    )
    service._READINESS_CACHE.update({"ready": None, "checked_at": 0.0})

    assert service.relationship_extent_index_ready() is True


def test_relationship_extent_query_batches_exact_predicates() -> None:
    from src.backend.services import relationship_extent_index_service as service

    query, should_query = service._build_relationship_extent_index_query(
        predicate_ids=["#V#supervises", "#V#co_supervises", "#V#supervises"],
        target_value="#V#person",
    )

    assert should_query is True
    assert query == {
        "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
        "predicate_id": {"$in": ["#V#supervises", "#V#co_supervises"]},
        "target_value": "#V#person",
    }


def test_relationship_extent_rebuild_enforces_batch_size_for_dense_source(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    coll = client.db.relationship_extent_index
    settings = client.db.application_settings
    batch_sizes: list[int] = []
    real_replace_one = coll.replace_one

    def tracked_bulk_write(operations, *, ordered):
        operation_list = list(operations)
        batch_sizes.append(len(operation_list))
        for operation in operation_list:
            real_replace_one(
                operation._filter,
                operation._doc,
                upsert=operation._upsert,
            )

    monkeypatch.setattr(
        service,
        "get_relationship_extent_index_collection",
        lambda: coll,
    )
    monkeypatch.setattr(
        service,
        "get_application_settings_collection",
        lambda: settings,
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "concept_id": "#V#dense_source",
                "relationships": {
                    "#V#mentions": [
                        f"#V#target_{index}" for index in range(7)
                    ]
                },
            }
        ],
    )
    monkeypatch.setattr(coll, "bulk_write", tracked_bulk_write)

    result = service.rebuild_relationship_extent_index(
        batch_size=3,
        reason="dense_source_test",
    )

    assert result["success"] is True
    assert result["source_count"] == 1
    assert result["edge_count"] == 7
    assert batch_sizes == [3, 3, 1]
    assert coll.count_documents({}) == 7
    state = settings.find_one(
        {"setting_name": service.RELATIONSHIP_EXTENT_INDEX_STATE_SETTING}
    )
    assert state["value"]["status"] == "ready"


def test_relationship_extent_query_cursor_and_count_complete_without_deadline(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as service

    observed_projection = None
    observed_batch_size = None
    expected_doc = {
        "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
        "relation_id": "row-1",
    }

    class DeadlineAwareCursor:
        def sort(self, _value):
            return self

        def skip(self, _value):
            return self

        def limit(self, _value):
            return self

        def batch_size(self, value):
            nonlocal observed_batch_size
            observed_batch_size = value
            return self

        def __iter__(self):
            return iter([expected_doc])

    class DeadlineAwareCollection:
        def find(self, _query, projection=None):
            nonlocal observed_projection
            observed_projection = projection
            return DeadlineAwareCursor()

        def count_documents(self, _query):
            return 1

    monkeypatch.setattr(service, "relationship_extent_index_ready", lambda: True)
    monkeypatch.setattr(
        service,
        "get_relationship_extent_index_collection",
        lambda: DeadlineAwareCollection(),
    )

    docs, total = service.query_relationship_extent_index(
        predicate_id="#V#mentions",
        offset=1,
        limit=1,
        sort=[("relation_id", 1)],
        projection={"_id": 0, "source_concept_id": 1},
        batch_size=20_000,
    )

    assert docs == [expected_doc]
    assert total == 1
    assert observed_projection == {"_id": 0, "source_concept_id": 1}
    assert observed_batch_size == 20_000


def test_relationship_extent_query_timeout_returns_fallback_sentinel(monkeypatch):
    from src.backend.services import relationship_extent_index_service as service

    class FailingCursor:
        def __iter__(self):
            raise RuntimeError("simulated extent read timeout")

    class FailingCollection:
        def find(self, _query):
            return FailingCursor()

    monkeypatch.setattr(service, "relationship_extent_index_ready", lambda: True)
    monkeypatch.setattr(
        service,
        "get_relationship_extent_index_collection",
        lambda: FailingCollection(),
    )

    assert service.query_relationship_extent_index(
        predicate_id="#V#mentions"
    ) == ([], -1)


def test_bulk_extent_sync_coalesces_repeated_source_ids(monkeypatch):
    from src.backend.services import relationship_extent_index_service as service

    synced: list[list[str]] = []
    monkeypatch.setattr(
        service,
        "_sync_relationship_extent_index_for_concept_ids_now",
        lambda source_ids: synced.append(list(source_ids)) or {"success": True},
    )

    with service.defer_relationship_extent_index_sync():
        first = service.sync_relationship_extent_index_for_concept_id("#V#shared")
        with service.defer_relationship_extent_index_sync():
            service.sync_relationship_extent_index_for_concept_id("#V#other")
            service.sync_relationship_extent_index_for_concept_id("#V#shared")
        assert synced == []

    assert first["deferred"] is True
    assert synced == [["#V#other", "#V#shared"]]


def test_bulk_extent_sync_uses_one_fetch_and_batched_upsert_then_delete(monkeypatch):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    coll = client.db.relationship_extent_index
    coll.insert_many(
        [
            {"source_concept_id": "#V#source_a", "relation_id": "stale-a"},
            {"source_concept_id": "#V#source_b", "relation_id": "stale-b"},
        ]
    )
    find_calls: list[tuple[dict, dict]] = []
    bulk_operation_batches: list[list[str]] = []
    def fake_find(query, projection):
        find_calls.append((query, projection))
        return [
            {
                "concept_id": "#V#source_a",
                "relationships": {"#V#mentions": ["#V#target_a"]},
            },
            {
                "concept_id": "#V#source_b",
                "relationships": {"#V#mentions": ["#V#target_b"]},
            },
        ]

    real_replace_one = coll.replace_one
    real_delete_many = coll.delete_many

    class _BulkResult:
        def __init__(self, *, deleted_count=0):
            self.deleted_count = deleted_count

    def tracked_bulk_write(operations, *, ordered):
        operation_list = list(operations)
        operation_names = [type(operation).__name__ for operation in operation_list]
        bulk_operation_batches.append(operation_names)
        deleted_count = 0
        for operation in operation_list:
            if type(operation).__name__ == "ReplaceOne":
                real_replace_one(
                    operation._filter,
                    operation._doc,
                    upsert=operation._upsert,
                )
            elif type(operation).__name__ == "DeleteMany":
                deleted_count += real_delete_many(operation._filter).deleted_count
        return _BulkResult(deleted_count=deleted_count)

    monkeypatch.setattr(service.ConceptsRepository, "find", fake_find)
    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: coll
    )
    monkeypatch.setattr(coll, "bulk_write", tracked_bulk_write)

    result = service._sync_relationship_extent_index_for_concept_ids_now(
        ["#V#source_b", "#V#source_a", "#V#source_a"]
    )

    assert result["success"] is True
    assert result["source_count"] == 2
    assert result["deleted"] == 2
    assert result["inserted"] == 2
    assert find_calls[0][0] == {
        "concept_id": {"$in": ["#V#source_a", "#V#source_b"]}
    }
    assert bulk_operation_batches == [
        ["ReplaceOne", "ReplaceOne"],
        ["DeleteMany", "DeleteMany"],
    ]
    assert coll.count_documents({"relation_id": {"$in": ["stale-a", "stale-b"]}}) == 0


def test_failed_batch_refresh_preserves_prior_rows_and_marks_index_degraded(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    coll = client.db.relationship_extent_index
    settings = client.db.application_settings
    coll.insert_many(
        [
            {
                "source_concept_id": "#V#source",
                "relation_id": "old-source-row",
                "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
            },
            {
                "source_concept_id": "#V#other",
                "relation_id": "other-row",
                "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
            },
        ]
    )

    monkeypatch.setattr(
        service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {
                "concept_id": "#V#source",
                "relationships": {"#V#mentions": ["#V#new-target"]},
            }
        ],
    )
    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: coll
    )
    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: settings
    )
    monkeypatch.setattr(
        coll,
        "bulk_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated bulk upsert failure")
        ),
    )
    service._READINESS_CACHE.update({"ready": True, "checked_at": 0.0})

    with pytest.raises(RuntimeError, match="simulated bulk upsert failure"):
        service._sync_relationship_extent_index_for_concept_ids_now(["#V#source"])

    assert coll.count_documents({"source_concept_id": "#V#source"}) == 1
    state = settings.find_one(
        {"setting_name": service.RELATIONSHIP_EXTENT_INDEX_STATE_SETTING}
    )
    assert state["value"]["status"] == "degraded"
    assert state["value"]["source_count"] == 1
    assert service.relationship_extent_index_ready() is False


def test_deferred_extent_sync_failure_does_not_fail_canonical_mutation(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as service

    attempts: list[list[str]] = []

    def fail_batch(source_ids):
        attempts.append(list(source_ids))
        raise RuntimeError("derived index unavailable")

    monkeypatch.setattr(
        service,
        "_sync_relationship_extent_index_for_concept_ids_now",
        fail_batch,
    )

    with service.defer_relationship_extent_index_sync():
        service.sync_relationship_extent_index_for_concept_id("#V#source")

    assert attempts == [["#V#source"]]


def test_successful_source_refresh_does_not_clear_global_degraded_state(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    coll = client.db.relationship_extent_index
    settings = client.db.application_settings
    settings.insert_one(
        {
            "setting_name": service.RELATIONSHIP_EXTENT_INDEX_STATE_SETTING,
            "value": {
                "status": "degraded",
                "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
            },
        }
    )
    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: coll
    )
    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: settings
    )

    result = service.sync_relationship_extent_index_for_concept_doc(
        {
            "concept_id": "#V#source",
            "relationships": {"#V#mentions": ["#V#target"]},
        }
    )

    assert result["success"] is True
    assert service.relationship_extent_index_ready() is False


def test_relationship_extent_index_materialises_dynamic_incoming_rows(monkeypatch):
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    coll = client.db.relationship_extent_index
    settings = client.db.application_settings

    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: coll
    )
    monkeypatch.setattr(
        service, "get_application_settings_collection", lambda: settings
    )
    monkeypatch.setattr(
        service,
        "get_relationship_kinds_set",
        lambda: {"is_a_type_of", "has_subtype", "is_an_instance_of", "has_instance"},
    )

    result = service.sync_relationship_extent_index_for_concept_doc(
        {
            "concept_id": "#V#source",
            "relationships": {
                "#V#attended_event": ["#V#target"],
                "is_a_type_of": ["#V#target"],
            },
        }
    )

    assert result["success"] is True
    assert result["inserted"] == 2
    settings.insert_one(
        {
            "setting_name": service.RELATIONSHIP_EXTENT_INDEX_STATE_SETTING,
            "value": {
                "status": "ready",
                "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
            },
        }
    )
    service._READINESS_CACHE.update({"ready": None, "checked_at": 0.0})

    rows, used_index = service.incoming_dynamic_extent_rows_for_target("#V#target")

    assert used_index is True
    assert len(rows) == 1
    assert rows[0]["predicate_id"] == "#V#attended_event"
    assert rows[0]["arg1_value"] == "#V#source"
    assert rows[0]["arg2_value"] == "#V#target"


def test_incoming_extent_page_filters_private_rows_across_batches(monkeypatch):
    from src.backend.security import access_control
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    index_coll = client.db.relationship_extent_index
    concepts = client.db.concepts
    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: index_coll
    )
    monkeypatch.setattr(service, "relationship_extent_index_ready", lambda: True)
    monkeypatch.setattr(
        service,
        "get_relationship_kinds_set",
        lambda: {"is_a_type_of", "has_subtype", "is_an_instance_of", "has_instance"},
    )
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: concepts)

    for idx, source_id in enumerate(
        [
            "#V#a_private_0",
            "#V#b_private_1",
            "#V#c_private_2",
            "#V#z_public_1",
            "#V#z_public_2",
        ]
    ):
        index_coll.insert_one(
            {
                "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
                "relation_id": f"r{idx}",
                "source_concept_id": source_id,
                "predicate_id": "#V#mentions",
                "target_value": "#V#target",
                "target_index": idx,
                "arg2_index": 2,
            }
        )
    concepts.insert_many(
        [
            {
                "concept_id": source_id,
                "relationships": {"specific_to_user": ["#V#other_user"]},
            }
            for source_id in ("#V#a_private_0", "#V#b_private_1", "#V#c_private_2")
        ]
        + [
            {"concept_id": "#V#z_public_1", "relationships": {}},
            {"concept_id": "#V#z_public_2", "relationships": {}},
        ]
    )

    app = Flask(__name__)
    app.secret_key = "test"
    with app.test_request_context("/vontology/api/vontology/relationships/extent"):
        rows, used_index, diagnostics = (
            service.incoming_dynamic_extent_rows_page_for_target(
                "#V#target",
                requested_predicate="#V#mentions",
                visible_limit=2,
                batch_size=2,
                max_index_rows_scanned=10,
                time_budget_ms=10000,
            )
        )

    assert used_index is True
    assert [row["arg1_value"] for row in rows] == ["#V#z_public_1", "#V#z_public_2"]
    assert diagnostics["index_batches"] == 3
    assert diagnostics["index_rows_scanned"] == 5
    assert diagnostics["rows_filtered_by_access"] == 3
    assert diagnostics["complete"] is True


def test_incoming_extent_page_stops_after_has_more_evidence(monkeypatch):
    from src.backend.security import access_control
    from src.backend.services import relationship_extent_index_service as service

    client = mongomock.MongoClient()
    index_coll = client.db.relationship_extent_index
    concepts = client.db.concepts
    monkeypatch.setattr(
        service, "get_relationship_extent_index_collection", lambda: index_coll
    )
    monkeypatch.setattr(service, "relationship_extent_index_ready", lambda: True)
    monkeypatch.setattr(
        service,
        "get_relationship_kinds_set",
        lambda: {"is_a_type_of", "has_subtype", "is_an_instance_of", "has_instance"},
    )
    monkeypatch.setattr(access_control, "get_concepts_collection", lambda: concepts)

    for idx in range(5):
        source_id = f"#V#public_{idx}"
        index_coll.insert_one(
            {
                "schema_version": service.RELATIONSHIP_EXTENT_INDEX_SCHEMA_VERSION,
                "relation_id": f"r{idx}",
                "source_concept_id": source_id,
                "predicate_id": "#V#mentions",
                "target_value": "#V#target",
                "target_index": idx,
                "arg2_index": 2,
            }
        )
        concepts.insert_one({"concept_id": source_id, "relationships": {}})

    with access_control.override_current_actor(user_concept_id="#V#viewer"):
        rows, used_index, diagnostics = (
            service.incoming_dynamic_extent_rows_page_for_target(
                "#V#target",
                requested_predicate="#V#mentions",
                visible_limit=2,
                batch_size=2,
                max_index_rows_scanned=10,
                time_budget_ms=10000,
            )
        )

    assert used_index is True
    assert len(rows) == 2
    assert diagnostics["has_more"] is True
    assert diagnostics["complete"] is False
    assert diagnostics["index_rows_scanned"] == 4


def test_incoming_extent_page_failure_returns_typed_canonical_fallback(
    monkeypatch,
):
    from src.backend.services import relationship_extent_index_service as service

    class FailingCursor:
        def sort(self, _value):
            return self

        def limit(self, _value):
            return self

        def batch_size(self, _value):
            return self

        def __iter__(self):
            raise RuntimeError("simulated extent page failure")

    class FailingCollection:
        def find(self, _query):
            return FailingCursor()

    monkeypatch.setattr(service, "relationship_extent_index_ready", lambda: True)
    monkeypatch.setattr(
        service,
        "get_relationship_extent_index_collection",
        lambda: FailingCollection(),
    )
    monkeypatch.setattr(
        service,
        "get_relationship_kinds_set",
        lambda: {"is_a_type_of", "has_subtype", "is_an_instance_of", "has_instance"},
    )

    rows, used_index, diagnostics = (
        service.incoming_dynamic_extent_rows_page_for_target(
            "#V#target",
            requested_predicate="#V#mentions",
            time_budget_ms=500,
        )
    )

    assert rows == []
    assert used_index is False
    assert diagnostics["reason"] == "extent_index_query_failed"


def test_predicate_structured_extent_uses_relationship_extent_index(monkeypatch):
    from src.backend.server.routes import predicate_routes

    client = mongomock.MongoClient()
    concepts = client.db.concepts
    concepts.insert_many(
        [
            {"concept_id": "#V#source", "name": "Source concept"},
            {"concept_id": "#V#target", "name": "Target concept"},
        ]
    )

    monkeypatch.setattr(predicate_routes, "get_concepts_collection", lambda: concepts)
    monkeypatch.setattr(
        predicate_routes,
        "query_relationship_extent_index",
        lambda **kwargs: (
            [
                {
                    "source_concept_id": "#V#source",
                    "predicate_id": "#V#attended_event",
                    "target_value": "#V#target",
                    "target_index": 0,
                }
            ],
            1,
        ),
    )

    items, total = predicate_routes._query_structured_relations_extent(
        "#V#attended_event",
        subject_type=None,
        object_type=None,
        limit=100,
        offset=0,
        sort_by="updated_at",
        sort_order="desc",
    )

    assert total == 1
    assert items == [
        {
            "subject": "#V#source",
            "subject_name": "Source concept",
            "predicate": "#V#attended_event",
            "object": "#V#target",
            "object_name": "Target concept",
            "source": "structured",
            "updated_at": None,
        }
    ]
