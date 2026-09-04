from __future__ import annotations

import src.backend.services.vontology_concept_stats_service as stats_service


def _concept_doc(
    concept_id: str,
    *,
    is_a_type_of: list[str] | None = None,
    is_an_instance_of: list[str] | None = None,
    extra_relationships: dict[str, object] | None = None,
):
    relationships = {}
    if is_a_type_of is not None:
        relationships["is_a_type_of"] = list(is_a_type_of)
    if is_an_instance_of is not None:
        relationships["is_an_instance_of"] = list(is_an_instance_of)
    if extra_relationships:
        relationships.update(extra_relationships)
    return {
        "concept_id": concept_id,
        "relationships": relationships,
    }


def _seed_docs():
    return [
        _concept_doc("#V#thing", is_a_type_of=[]),
        _concept_doc("#V#animal", is_a_type_of=["#V#thing"]),
        _concept_doc("#V#mammal", is_a_type_of=["#V#animal"]),
        _concept_doc("#V#bird", is_a_type_of=["#V#animal"]),
        _concept_doc(
            "#V#domesticated_mammal",
            is_a_type_of=["#V#mammal"],
            is_an_instance_of=["#V#animal"],
        ),
        _concept_doc("#V#predicate", is_a_type_of=["#V#thing"]),
        _concept_doc("#V#has_pet", is_an_instance_of=["#V#predicate"]),
        _concept_doc(
            "#V#alice",
            is_an_instance_of=["#V#mammal"],
            extra_relationships={"#V#has_pet": ["#V#cat", "#V#dog"]},
        ),
        _concept_doc(
            "#V#bob",
            is_an_instance_of=["#V#mammal"],
            extra_relationships={"#V#has_pet": "#V#cat"},
        ),
        _concept_doc("#V#parrot", is_an_instance_of=["#V#bird"]),
    ]


def test_stats_snapshot_counts_types_and_predicate_extent(monkeypatch):
    stats_service._reset_vontology_concept_stats_cache_for_tests()
    monkeypatch.setattr(
        stats_service.ConceptsRepository, "find", lambda *_args, **_kwargs: _seed_docs()
    )
    monkeypatch.setattr(
        stats_service.TextRelationsRepository, "collection", lambda: object()
    )
    monkeypatch.setattr(
        stats_service.TextRelationsRepository,
        "aggregate",
        lambda *_args, **_kwargs: [{"_id": "#V#has_pet", "count": 2}],
    )

    rebuilt = stats_service.rebuild_vontology_concept_stats_snapshot(
        scope_key="test-scope",
        reason="unit-test",
    )
    assert rebuilt["success"] is True
    assert rebuilt["stats_status"] == stats_service.STATS_STATUS_AVAILABLE

    payload = stats_service.get_vontology_concept_stats(
        ["#V#animal", "#V#mammal", "#V#has_pet", "#V#predicate"],
        scope_key="test-scope",
    )
    assert payload["stats_status"] == stats_service.STATS_STATUS_AVAILABLE

    animal = payload["concept_stats"]["#V#animal"]
    assert animal["kind"] == "type"
    assert animal["direct_instance_count"] == 1
    assert animal["direct_pure_instance_count"] == 0
    assert animal["has_direct_pure_instances"] is False
    assert animal["total_instance_count_in_subtree"] == 4
    assert animal["has_any_instances_in_subtree"] is True
    assert animal["direct_subtype_count"] == 2
    assert animal["total_subtype_count_in_subtree"] == 3

    mammal = payload["concept_stats"]["#V#mammal"]
    assert mammal["kind"] == "type"
    assert mammal["direct_instance_count"] == 2
    assert mammal["direct_pure_instance_count"] == 2
    assert mammal["has_direct_pure_instances"] is True
    assert mammal["total_instance_count_in_subtree"] == 2
    assert mammal["direct_subtype_count"] == 1
    assert mammal["total_subtype_count_in_subtree"] == 1

    has_pet = payload["concept_stats"]["#V#has_pet"]
    assert has_pet["kind"] == "predicate"
    assert has_pet["extent_count"] == 5
    assert has_pet["extent_count_is_exact"] is True
    assert has_pet["extent_count_unavailable"] is False

    predicate = payload["concept_stats"]["#V#predicate"]
    assert predicate["kind"] == "type"
    assert predicate["direct_instance_count"] == 1
    assert predicate["direct_pure_instance_count"] == 1


def test_stats_snapshot_conceals_login_identity_predicate_extent(monkeypatch):
    stats_service._reset_vontology_concept_stats_cache_for_tests()
    docs = [
        *_seed_docs(),
        _concept_doc(
            "#V#hasVonLoginEmail",
            is_an_instance_of=["#V#predicate"],
        ),
        _concept_doc(
            "#V#target",
            extra_relationships={"#V#hasVonLoginEmail": "#V#email_value"},
        ),
    ]
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        stats_service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: docs,
    )
    monkeypatch.setattr(
        stats_service.TextRelationsRepository,
        "collection",
        lambda: object(),
    )

    def aggregate(pipeline):
        captured["pipeline"] = pipeline
        # Defence in depth: even a backend returning an out-of-policy row must
        # not let its real count reach the generic stats snapshot.
        return [{"_id": "#V#hasVonLoginEmail", "count": 7}]

    monkeypatch.setattr(
        stats_service.TextRelationsRepository,
        "aggregate",
        aggregate,
    )

    rebuilt = stats_service.rebuild_vontology_concept_stats_snapshot(
        scope_key="hidden-predicate-scope",
        reason="unit-test",
    )
    payload = stats_service.get_vontology_concept_stats(
        ["#V#hasVonLoginEmail"],
        scope_key="hidden-predicate-scope",
    )

    assert rebuilt["success"] is True
    hidden_stats = payload["concept_stats"]["#V#hasVonLoginEmail"]
    assert hidden_stats["kind"] == "predicate"
    assert hidden_stats["extent_count"] == 0
    assert hidden_stats["extent_count_is_exact"] is True
    assert hidden_stats["extent_count_unavailable"] is False
    match = captured["pipeline"][0]["$match"]["predicate"]
    assert match["$nin"] == ["#V#hasVonLoginEmail", "hasVonLoginEmail"]


def test_stats_invalidation_marks_snapshot_stale_until_rebuilt(monkeypatch):
    stats_service._reset_vontology_concept_stats_cache_for_tests()
    monkeypatch.setattr(
        stats_service.ConceptsRepository, "find", lambda *_args, **_kwargs: _seed_docs()
    )
    monkeypatch.setattr(
        stats_service.TextRelationsRepository, "collection", lambda: object()
    )
    monkeypatch.setattr(
        stats_service.TextRelationsRepository, "aggregate", lambda *_args, **_kwargs: []
    )

    stats_service.rebuild_vontology_concept_stats_snapshot(scope_key="test-scope")
    stale_info = stats_service.invalidate_vontology_concept_stats_cache(
        reason="mutation",
        affected_concepts=["#V#mammal"],
    )
    assert stale_info["mutation_version"] == 1

    stale_payload = stats_service.get_vontology_concept_stats(
        ["#V#mammal"],
        scope_key="test-scope",
        rebuild_if_needed=False,
    )
    assert stale_payload["stats_status"] == stats_service.STATS_STATUS_STALE
    assert stale_payload["concept_stats"]["#V#mammal"]["stats_unavailable"] is True
    assert stale_payload["concept_stats"]["#V#mammal"]["stats_reason"] == "cache_stale"

    refreshed_payload = stats_service.get_vontology_concept_stats(
        ["#V#mammal"],
        scope_key="test-scope",
        rebuild_if_needed=True,
    )
    assert refreshed_payload["stats_status"] == stats_service.STATS_STATUS_AVAILABLE
    assert refreshed_payload["concept_stats"]["#V#mammal"]["direct_instance_count"] == 2
    assert (
        refreshed_payload["concept_stats"]["#V#mammal"]["direct_pure_instance_count"]
        == 2
    )


def test_stats_without_snapshot_fast_fail_as_failed_status():
    stats_service._reset_vontology_concept_stats_cache_for_tests()

    payload = stats_service.get_vontology_concept_stats(
        ["#V#missing"],
        scope_key="test-scope",
        rebuild_if_needed=False,
    )
    assert payload["stats_status"] == stats_service.STATS_STATUS_FAILED
    assert payload["concept_stats"]["#V#missing"]["stats_unavailable"] is True
    assert payload["concept_stats"]["#V#missing"]["stats_reason"] == "cache_unavailable"
