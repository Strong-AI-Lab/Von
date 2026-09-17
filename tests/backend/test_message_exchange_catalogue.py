from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from src.backend.services import message_catalogue_service as catalogue


@pytest.fixture
def collection(monkeypatch):
    monkeypatch.setattr(
        catalogue,
        "get_user_memberships",
        lambda actor: {"memberships": [{"organisation_concept_id": "#V#lab"}]},
    )
    coll = mongomock.MongoClient().db.concepts
    monkeypatch.setattr(catalogue, "get_concepts_collection", lambda: coll)
    monkeypatch.setattr(catalogue, "apply_concept_query_filter", lambda query: query)
    return coll


def message(i, recipients=None, org="#V#lab"):
    return {
        "concept_id": f"#V#message_{i:03}",
        "created_at": datetime(2026, 9, 1, tzinfo=UTC) + timedelta(seconds=i),
        "relationships": {
            "is_an_instance_of": ["#V#direct_message"],
            "#V#has_sender": ["#V#bob"],
            "#V#has_recipient": recipients or ["#V#alice"],
        },
        "concept_data": {"organisation_concept_id": org, "content_fallback": str(i)},
    }


def test_latest_page_and_earlier_cursor_are_chronological_without_overlap(collection):
    collection.insert_many([message(i) for i in range(65)])
    first = catalogue.get_exchange(
        "#V#alice", ["#V#alice", "#V#bob"], organisation="#V#lab"
    )
    assert [m["concept_id"] for m in first["messages"]] == [
        f"#V#message_{i:03}" for i in range(15, 65)
    ]
    earlier = catalogue.get_exchange(
        "#V#alice",
        ["#V#bob", "#V#alice"],
        organisation="#V#lab",
        before=first["before"],
    )
    assert len(earlier["messages"]) == 15 and not earlier["has_more"]
    assert earlier["messages"][-1]["concept_id"] == "#V#message_014"


def test_group_exchange_cannot_leak_into_pair_and_org_is_preserved(collection):
    collection.insert_many(
        [
            message(1),
            message(2, ["#V#alice", "#V#carol"]),
            message(3, org="#V#elsewhere"),
        ]
    )
    pair = catalogue.get_exchange(
        "#V#alice", ["#V#alice", "#V#bob"], organisation="#V#lab"
    )
    assert [m["concept_id"] for m in pair["messages"]] == ["#V#message_001"]
    group = catalogue.get_exchange(
        "#V#alice", ["#V#alice", "#V#bob", "#V#carol"], organisation="#V#lab"
    )
    assert [m["concept_id"] for m in group["messages"]] == ["#V#message_002"]


def test_actor_must_be_a_participant(collection):
    collection.insert_one(message(1))
    with pytest.raises(PermissionError):
        catalogue.get_exchange("#V#eve", ["#V#alice", "#V#bob"])


def test_deleted_messages_and_equal_timestamp_paging(collection):
    a, b = message(1), message(2)
    b["created_at"] = a["created_at"]
    deleted = message(3)
    deleted["concept_data"]["deleted"] = True
    collection.insert_many([a, b, deleted])
    first = catalogue.get_exchange(
        "#V#alice", ["#V#alice", "#V#bob"], organisation="#V#lab", limit=1
    )
    assert first["messages"][0]["concept_id"] == b["concept_id"]
    next_page = catalogue.get_exchange(
        "#V#alice",
        ["#V#alice", "#V#bob"],
        organisation="#V#lab",
        before=first["before"],
    )
    assert [m["concept_id"] for m in next_page["messages"]] == [a["concept_id"]]


@pytest.fixture
def reference_store(monkeypatch):
    from src.backend.services import concept_service

    store = {}
    calls = []

    def read(cid):
        if cid not in store:
            raise concept_service.ConceptNotFoundError(cid)
        return store[cid]

    def create(**kwargs):
        calls.append(kwargs)
        scope = concept_service._resolve_creation_visibility_scope(
            created_by_concept_id=kwargs['created_by_concept_id'],
            organisation_concept_id=kwargs['organisation_concept_id'],
            event_namespace=None,
            visibility_scope_mode=kwargs['visibility_scope_mode'],
        )
        kwargs['relationships'] = scope['relationships']
        store[kwargs["concept_id"]] = kwargs
        return kwargs

    monkeypatch.setattr(concept_service, "get_concept_by_concept_id_exact", read)
    monkeypatch.setattr(concept_service, "create_concept", create)
    return store, calls


def test_reference_is_private_idempotent_and_retains_exact_locator(
    collection, reference_store
):
    collection.insert_one(message(1))
    store, calls = reference_store
    first = catalogue.materialise_exchange_reference(
        "#V#alice", ["#V#alice", "#V#bob"], organisation="#V#lab"
    )
    again = catalogue.materialise_exchange_reference(
        "#V#alice", ["#V#bob", "#V#alice"], organisation="#V#lab"
    )
    assert first == again
    assert len(calls) == 1
    assert calls[0]["created_by_concept_id"] == "#V#alice"
    assert calls[0]["organisation_concept_id"] == "#V#lab"
    assert calls[0]["resolve_visibility_from_event_namespace"] is False
    assert first["message_exchange_reference"]["participant_ids"] == [
        "#V#alice",
        "#V#bob",
    ]
    assert first["concept_id"] in store
    assert store[first["concept_id"]]["relationships"] == {"#V#specific_to_user": ["#V#alice"]}
    # Reuse must still prove that there is a currently visible source exchange.
    collection.delete_many({})
    with pytest.raises(PermissionError):
        catalogue.materialise_exchange_reference(
            "#V#alice", ["#V#alice", "#V#bob"], organisation="#V#lab"
        )


def test_reference_denies_other_actor_group_and_organisation(
    collection, reference_store
):
    collection.insert_one(message(1))
    for actor, participants, org in [
        ("#V#eve", ["#V#alice", "#V#bob"], "#V#lab"),
        ("#V#alice", ["#V#alice", "#V#bob", "#V#carol"], "#V#lab"),
        ("#V#alice", ["#V#alice", "#V#bob"], "#V#other_lab"),
    ]:
        with pytest.raises(PermissionError):
            catalogue.materialise_exchange_reference(
                actor, participants, organisation=org
            )
    assert not reference_store[1]
