import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import (
    bypass_access_control,
    get_effective_user_concept_id,
    override_current_actor,
)
from src.backend.services import concept_resolution_service
from src.backend.services.concept_resolution_service import resolve_concept_by_name
from src.backend.services.text_value_service import upsert_text_for_concept


@pytest.fixture(autouse=True)
def clean_collections():
    db = TextValuesRepository.db()
    if db is None:
        yield
        return

    concepts = ConceptsRepository.collection()
    relations = TextRelationsRepository.collection()
    text_values = TextValuesRepository.collection()

    if concepts is None or relations is None or text_values is None:
        yield
        return

    concepts.delete_many({})
    relations.delete_many({})
    text_values.delete_many({})
    yield
    concepts.delete_many({})
    relations.delete_many({})
    text_values.delete_many({})


def test_resolve_concept_by_name_matches_diacritics_insensitively():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#gael_test"
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts.insert_one({"concept_id": concept_id, "relationships": {}})

    with bypass_access_control():
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            text="Gaël",
            lang="fr",
            context={"name_type": "NL"},
        )

    result = resolve_concept_by_name(name="Gael", preferred_languages=["fr"])

    assert result["success"] is True
    assert result["status"] == "resolved"
    assert result["resolved_concept_id"] == concept_id
    assert result["match"]["stage"] in {
        "diacritic_insensitive",
        "casefold_exact",
        "exact",
    }


def test_resolve_concept_by_name_returns_ambiguous_on_tie():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_a = "#V#michael_witbrock_a"
    concept_b = "#V#michael_witbrock_b"
    concepts.insert_one({"concept_id": concept_a, "relationships": {}})
    concepts.insert_one({"concept_id": concept_b, "relationships": {}})

    with bypass_access_control():
        upsert_text_for_concept(
            subject_concept_id=concept_a,
            predicate="hasName",
            text="Michael Witbrock",
            lang="en-NZ",
            context={"name_type": "NL"},
        )
        upsert_text_for_concept(
            subject_concept_id=concept_b,
            predicate="hasName",
            text="Michael Witbrock",
            lang="en-NZ",
            context={"name_type": "NL"},
        )

    result = resolve_concept_by_name(name="Michael Witbrock")

    assert result["success"] is True
    assert result["status"] == "ambiguous"
    assert result["resolved_concept_id"] is None
    assert {c["concept_id"] for c in result["candidates"]} == {concept_a, concept_b}


def test_resolve_concept_by_name_hydrates_candidate_names_in_one_batch(
    monkeypatch,
) -> None:
    candidate_ids = ["#V#batch_candidate_a", "#V#batch_candidate_b"]
    batch_calls: list[tuple[list[str], str | None, int]] = []

    monkeypatch.setattr(
        concept_resolution_service,
        "_search_text_relations",
        lambda *_args, **_kwargs: set(candidate_ids),
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "filter_accessible_concept_ids",
        lambda ids: set(ids),
    )
    monkeypatch.setattr(
        concept_resolution_service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": concept_id, "relationships": {}}
            for concept_id in candidate_ids
        ],
    )

    def _get_texts_for_concepts(
        concept_ids,
        *,
        predicate=None,
        limit_per_concept=50,
        **_kwargs,
    ):
        ordered_ids = list(concept_ids)
        batch_calls.append((ordered_ids, predicate, limit_per_concept))
        return {
            concept_id: [
                {
                    "text": "Shared batch candidate",
                    "lang": "en-NZ",
                    "predicate": "hasName",
                    "context": {"name_type": "NL"},
                }
            ]
            for concept_id in ordered_ids
        }

    monkeypatch.setattr(
        concept_resolution_service,
        "get_texts_for_concepts",
        _get_texts_for_concepts,
    )

    result = resolve_concept_by_name(
        name="Shared batch candidate",
        match_code_strings=False,
    )

    assert result["status"] == "ambiguous"
    assert {
        candidate["concept_id"] for candidate in result["candidates"]
    } == set(candidate_ids)
    assert batch_calls == [(sorted(candidate_ids), "hasName", 200)]


def test_resolve_concept_by_name_falls_back_when_batch_hydration_is_truncated(
    monkeypatch,
) -> None:
    candidate_ids = ["#V#name_heavy_candidate", "#V#starved_candidate"]
    query = "Shared complete candidate"
    batch_calls: list[list[str]] = []
    single_calls: list[tuple[str, str | None, int]] = []

    monkeypatch.setattr(
        concept_resolution_service,
        "_search_text_relations",
        lambda *_args, **_kwargs: set(candidate_ids),
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "filter_accessible_concept_ids",
        lambda ids: set(ids),
    )
    monkeypatch.setattr(
        concept_resolution_service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": concept_id, "relationships": {}}
            for concept_id in candidate_ids
        ],
    )

    def _truncated_batch(concept_ids, *, query_metadata, **_kwargs):
        ordered_ids = list(concept_ids)
        batch_calls.append(ordered_ids)
        query_metadata["relation_query_truncated"] = True
        return {
            ordered_ids[0]: [
                {
                    "text": query,
                    "lang": "en-NZ",
                    "predicate": "hasName",
                    "context": {"name_type": "NL"},
                }
            ]
        }

    def _complete_single(concept_id, *, predicate=None, limit=50, **_kwargs):
        single_calls.append((concept_id, predicate, limit))
        return [
            {
                "text": query,
                "lang": "en-NZ",
                "predicate": "hasName",
                "context": {"name_type": "NL"},
            }
        ]

    monkeypatch.setattr(
        concept_resolution_service,
        "get_texts_for_concepts",
        _truncated_batch,
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "get_texts_for_concept",
        _complete_single,
    )

    result = resolve_concept_by_name(
        name=query,
        match_code_strings=False,
    )

    assert result["status"] == "ambiguous"
    assert {
        candidate["concept_id"] for candidate in result["candidates"]
    } == set(candidate_ids)
    assert batch_calls == [sorted(candidate_ids)]
    assert single_calls == [
        (concept_id, "hasName", 200) for concept_id in sorted(candidate_ids)
    ]


def test_resolve_concept_by_name_bounds_each_stage_and_filters_before_fallback(
    monkeypatch,
) -> None:
    private_concept_id = "#V#private_bounded_candidate"
    public_concept_id = "#V#public_bounded_candidate"
    query = "Bounded accessible match"
    search_calls: list[tuple[str, dict[str, object]]] = []
    access_calls: list[set[str]] = []

    def _search(query_text, **kwargs):
        search_calls.append((query_text, dict(kwargs)))
        if kwargs.get("exact") or kwargs.get("prefix"):
            return {private_concept_id}
        return {public_concept_id}

    def _filter_accessible(candidate_ids):
        candidate_set = set(candidate_ids)
        access_calls.append(candidate_set)
        return candidate_set.intersection({public_concept_id})

    monkeypatch.setattr(
        concept_resolution_service,
        "_search_text_relations",
        _search,
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "filter_accessible_concept_ids",
        _filter_accessible,
    )
    monkeypatch.setattr(
        concept_resolution_service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: [
            {"concept_id": public_concept_id, "relationships": {}}
        ],
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "get_texts_for_concepts",
        lambda concept_ids, **_kwargs: {
            concept_id: [
                {
                    "text": query,
                    "lang": "en-NZ",
                    "predicate": "hasName",
                    "context": {"name_type": "NL"},
                }
            ]
            for concept_id in concept_ids
        },
    )

    result = resolve_concept_by_name(
        name=query,
        match_code_strings=False,
        max_results=7,
    )

    assert result["status"] == "resolved"
    assert result["resolved_concept_id"] == public_concept_id
    assert search_calls == [
        (
            query,
            {
                "exact": True,
                "result_limit": 7,
                "allow_fallback_scan": False,
            },
        ),
        (query, {"prefix": True, "result_limit": 7}),
        (query, {"result_limit": 7}),
    ]
    assert access_calls == [
        {private_concept_id},
        {private_concept_id},
        {public_concept_id},
    ]


def test_resolve_concept_by_name_honours_instance_of_filter():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    researcher_type = "#V#researcher"
    concepts.insert_one({"concept_id": researcher_type, "relationships": {}})

    good = "#V#alex_researcher"
    bad = "#V#alex_not_researcher"

    concepts.insert_one(
        {
            "concept_id": good,
            "relationships": {"is_an_instance_of": [researcher_type]},
        }
    )
    concepts.insert_one({"concept_id": bad, "relationships": {}})

    with bypass_access_control():
        upsert_text_for_concept(
            subject_concept_id=good,
            predicate="hasName",
            text="Alex Example",
            lang="en-NZ",
            context={"name_type": "NL"},
        )
        upsert_text_for_concept(
            subject_concept_id=bad,
            predicate="hasName",
            text="Alex Example",
            lang="en-NZ",
            context={"name_type": "NL"},
        )

    result = resolve_concept_by_name(name="Alex Example", instance_of=researcher_type)

    assert result["success"] is True
    assert result["status"] == "resolved", result
    assert result["resolved_concept_id"] == good


def test_resolve_concept_by_name_can_resolve_code_style_identifier():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#michael_witbrock"
    concepts.insert_one({"concept_id": concept_id, "relationships": {}})

    result = resolve_concept_by_name(name="michael_witbrock", match_code_strings=True)

    assert result["success"] is True
    assert result["status"] == "resolved"
    assert result["resolved_concept_id"] == concept_id


def test_resolve_concept_by_name_does_not_resolve_deleted_concepts_from_stale_text_relations():
    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#stale_deleted_concept"
    concepts.insert_one({"concept_id": concept_id, "relationships": {}})

    # Add a hasName relation that matches a code-style identifier exactly.
    # This reproduces the historical failure mode where name resolution could
    # return a deleted concept due to stale text_relations.
    with bypass_access_control():
        upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasName",
            text=concept_id,
            lang="en-NZ",
            context={"name_type": "CODE"},
        )

    # Simulate partial deletion: concept doc gone, text_relations remain.
    ConceptsRepository.delete_one({"concept_id": concept_id})

    result = resolve_concept_by_name(name=concept_id, match_code_strings=False)

    assert result["success"] is True
    assert result["status"] == "not_found"
    assert result["resolved_concept_id"] is None


def test_resolve_concept_by_name_audit_counts_only_actor_accessible_candidates(
    monkeypatch,
) -> None:
    private_concept_id = "#V#secret_quasar_project"
    private_name = "Secret Quasar Project"

    monkeypatch.setattr(
        concept_resolution_service,
        "_search_text_relations",
        lambda *_args, **_kwargs: {private_concept_id},
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "filter_accessible_concept_ids",
        lambda candidate_ids: (
            set(candidate_ids)
            if get_effective_user_concept_id() == "#V#actor_b"
            else set()
        ),
    )
    monkeypatch.setattr(
        concept_resolution_service.ConceptsRepository,
        "find",
        lambda *_args, **_kwargs: (
            [{"concept_id": private_concept_id, "relationships": {}}]
            if get_effective_user_concept_id() == "#V#actor_b"
            else []
        ),
    )
    monkeypatch.setattr(
        concept_resolution_service,
        "get_texts_for_concepts",
        lambda concept_ids, **_kwargs: {
            concept_id: [
                {
                    "text": private_name,
                    "lang": "en-NZ",
                    "predicate": "hasName",
                    "context": {"name_type": "NL"},
                }
            ]
            for concept_id in concept_ids
        },
    )
    monkeypatch.setattr(
        concept_resolution_service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )

    with override_current_actor("#V#actor_b"):
        actor_b_result = resolve_concept_by_name(
            name=private_name,
            match_code_strings=False,
        )
    with override_current_actor("#V#actor_a"):
        actor_a_result = resolve_concept_by_name(
            name=private_name,
            match_code_strings=False,
        )

    assert actor_b_result["status"] == "resolved"
    assert actor_b_result["resolved_concept_id"] == private_concept_id
    assert any(
        item.get("hits") == 1
        for item in actor_b_result["audit"]
        if item.get("stage") == "candidate_generation"
    )
    assert actor_a_result["status"] == "not_found"
    assert actor_a_result["resolved_concept_id"] is None
    assert all(
        item.get("hits", 0) == 0
        for item in actor_a_result["audit"]
        if item.get("stage") == "candidate_generation"
    )
