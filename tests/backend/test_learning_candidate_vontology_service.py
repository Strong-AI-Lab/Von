"""Focused tests for non-active, source-grounded learning candidates."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from pymongo.errors import DuplicateKeyError

from src.backend.services import learning_candidate_vontology_service as service


def _nested_value(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _set_nested_value(document: dict[str, Any], path: str, value: Any) -> None:
    target = document
    parts = path.split(".")
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = deepcopy(value)


class _MemoryStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.descriptions: dict[str, str] = {}
        self.actor: str | None = None
        self.org: str | None = None
        self.insert_count = 0
        self.update_count = 0
        self.text_write_count = 0
        self.owned_chat_sessions: set[tuple[str, str]] = {
            ("#V#alice", "session-without-concept")
        }

    @contextmanager
    def actor_scope(self, actor: str | None, org: str | None):
        previous = (self.actor, self.org)
        self.actor, self.org = actor, org
        try:
            yield
        finally:
            self.actor, self.org = previous

    def visible(self, doc: dict[str, Any]) -> bool:
        relationships = doc.get("relationships")
        relationships = relationships if isinstance(relationships, dict) else {}
        users = set(service.get_specific_to_user_values(relationships))
        orgs = set(service.get_specific_to_org_values(relationships))
        if not users and not orgs:
            return True
        return bool(
            (self.actor and self.actor in users) or (self.org and self.org in orgs)
        )

    def matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for field, expected in query.items():
            observed = _nested_value(doc, field)
            if isinstance(expected, dict) and "$in" in expected:
                choices = set(expected["$in"])
                if isinstance(observed, list):
                    if not choices.intersection(observed):
                        return False
                elif observed not in choices:
                    return False
                continue
            if isinstance(observed, list):
                if expected not in observed:
                    return False
            elif observed != expected:
                return False
        return True

    def find_one(
        self, query: dict[str, Any], _projection: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        for doc in self.docs.values():
            if self.matches(doc, query) and self.visible(doc):
                return deepcopy(doc)
        return None

    def find(
        self,
        query: dict[str, Any],
        projection: dict[str, Any] | None = None,
        sort: list[tuple[str, int]] | None = None,
        skip: int = 0,
        limit: int = 0,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        docs = [
            deepcopy(doc)
            for doc in self.docs.values()
            if self.matches(doc, query) and self.visible(doc)
        ]
        for field, direction in reversed(sort or []):
            docs.sort(
                key=lambda doc: str(_nested_value(doc, field) or ""),
                reverse=direction < 0,
            )
        if skip:
            docs = docs[skip:]
        if limit:
            docs = docs[:limit]
        if projection:
            projected: list[dict[str, Any]] = []
            for doc in docs:
                row: dict[str, Any] = {}
                for field, include in projection.items():
                    if include and field != "_id":
                        _set_nested_value(row, field, _nested_value(doc, field))
                projected.append(row)
            return projected
        return docs

    def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        concept_id = str(doc.get("concept_id") or "")
        if concept_id in self.docs:
            raise DuplicateKeyError("duplicate concept")
        self.docs[concept_id] = deepcopy(doc)
        self.insert_count += 1
        return SimpleNamespace(inserted_id=concept_id)

    def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> SimpleNamespace:
        del upsert
        for concept_id, doc in self.docs.items():
            if not self.matches(doc, query) or not self.visible(doc):
                continue
            for field, value in (update.get("$set") or {}).items():
                _set_nested_value(doc, field, value)
            self.docs[concept_id] = doc
            self.update_count += 1
            return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    def upsert_text(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["predicate"] == "hasDescription"
        self.descriptions[kwargs["subject_concept_id"]] = kwargs["text"]
        self.text_write_count += 1
        return {"success": True, "relation_id": f"rel-{self.text_write_count}"}

    def get_texts(
        self,
        subject_concept_id: str,
        predicate: str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        text = self.descriptions.get(subject_concept_id)
        if text is None:
            return []
        return [{"predicate": predicate or "hasDescription", "text": text}]

    def get_texts_many(
        self,
        subject_concept_ids: list[str],
        predicate: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            concept_id: self.get_texts(concept_id, predicate=predicate)
            for concept_id in subject_concept_ids
        }

    def has_chat_history_session(
        self,
        user_id: str,
        session_id: str,
        **_kwargs: Any,
    ) -> bool:
        return (user_id, session_id) in self.owned_chat_sessions


def _global_concept(concept_id: str) -> dict[str, Any]:
    return {"concept_id": concept_id, "relationships": {}}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _MemoryStore:
    memory = _MemoryStore()
    memory.docs.update(
        {
            "#V#alice": _global_concept("#V#alice"),
            "#V#bob": _global_concept("#V#bob"),
            "#V#von_system": _global_concept("#V#von_system"),
            "#V#research_org": _global_concept("#V#research_org"),
            "#V#research_team": _global_concept("#V#research_team"),
            "#V#workflow_prepare_brief": _global_concept("#V#workflow_prepare_brief"),
            "#V#role_ecosystem_steward": _global_concept("#V#role_ecosystem_steward"),
            "#V#earth_ecosystems": _global_concept("#V#earth_ecosystems"),
            "#V#purpose_restore_wetlands": _global_concept(
                "#V#purpose_restore_wetlands"
            ),
            "#V#conversation_alice": {
                "concept_id": "#V#conversation_alice",
                "relationships": {
                    "is_an_instance_of": ["#V#conversation"],
                    "#V#specific_to_user": ["#V#alice"],
                },
                "metadata": {"session_id": "session-alice"},
            },
            "#V#episode_memory_alice": {
                "concept_id": "#V#episode_memory_alice",
                "relationships": {
                    "is_an_instance_of": ["#V#episode_critique_memory"],
                    "#V#specific_to_user": ["#V#alice"],
                },
                "concept_data": {
                    "episode_critique_memory": {
                        "schema_version": "episode_critique_memory.v1",
                        "memory_id": "#V#episode_memory_alice",
                        "request_id": "request-episode-1",
                        "subject_episode": {"stable_key": "episode-stable-1"},
                    }
                },
            },
            "#V#episode_memory_org": {
                "concept_id": "#V#episode_memory_org",
                "relationships": {
                    "is_an_instance_of": ["#V#episode_critique_memory"],
                    "#V#specific_to_organisation": ["#V#research_org"],
                },
                "concept_data": {
                    "episode_critique_memory": {
                        "schema_version": "episode_critique_memory.v1",
                        "memory_id": "#V#episode_memory_org",
                        "request_id": "request-org-episode",
                        "subject_episode": {"stable_key": "org-episode-stable"},
                    }
                },
            },
        }
    )
    monkeypatch.setattr(service.ConceptsRepository, "find_one", memory.find_one)
    monkeypatch.setattr(service.ConceptsRepository, "find", memory.find)
    monkeypatch.setattr(service.ConceptsRepository, "insert_one", memory.insert_one)
    monkeypatch.setattr(service.ConceptsRepository, "update_one", memory.update_one)
    monkeypatch.setattr(service, "override_current_actor", memory.actor_scope)
    monkeypatch.setattr(
        service, "suppress_event_workflow_launches", lambda _reason: nullcontext()
    )
    monkeypatch.setattr(service, "upsert_singleton_text_relation", memory.upsert_text)
    monkeypatch.setattr(service, "get_texts_for_concept", memory.get_texts)
    monkeypatch.setattr(service, "get_texts_for_concepts", memory.get_texts_many)
    monkeypatch.setattr(
        service.chat_history_service,
        "has_chat_history_session",
        memory.has_chat_history_session,
    )
    return memory


def _capture_from_discussion(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "body": "Check the canonical work product before claiming completion.",
        "source": {
            "kind": "conversation",
            "conversation_concept_id": "#V#conversation_alice",
            "session_id": "session-alice",
        },
        "contributor_concept_ids": ["#V#alice", "#V#von_system"],
        "target_concept_ids": ["#V#workflow_prepare_brief"],
        "actor_user_id": "#V#alice",
        "organisation_concept_id": "#V#research_org",
        "idempotency_key": "discussion-candidate-1",
        "request_id": "request-discussion-1",
    }
    values.update(overrides)
    return service.capture_learning_candidate(**values)


def test_capture_discussion_candidate_is_artifact_and_canonical_readback(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion()

    assert captured["created"] is True
    assert captured["lifecycle_state"] == "non_active"
    assert captured["source"] == {
        "kind": "conversation",
        "locator": {
            "conversation_concept_id": "#V#conversation_alice",
            "session_id": "session-alice",
        },
    }
    doc = store.docs[captured["candidate_id"]]
    assert doc["relationships"]["is_an_instance_of"] == ["#V#artifact"]
    assert doc["relationships"]["#V#authored_by"] == ["#V#von_system"]
    assert doc["relationships"]["related_to"] == ["#V#conversation_alice"]
    assert doc["relationships"]["#V#specific_to_user"] == ["#V#alice"]
    assert "body" not in doc["concept_data"]["learning_candidate"]
    assert store.descriptions[captured["candidate_id"]] == captured["body"]

    read_back = service.get_learning_candidate(
        captured["candidate_id"],
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert read_back["body"] == captured["body"]
    assert read_back["body_sha256"] == captured["body_sha256"]


def test_capture_candidate_from_episode_memory_preserves_stable_locator(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion(
        source={
            "kind": "episode_critique_memory",
            "memory_id": "#V#episode_memory_alice",
        },
        idempotency_key="episode-candidate-1",
        request_id="request-capture-episode",
    )

    assert captured["source"] == {
        "kind": "episode_critique_memory",
        "locator": {
            "memory_id": "#V#episode_memory_alice",
            "episode_stable_key": "episode-stable-1",
            "request_id": "request-episode-1",
        },
    }
    assert store.docs[captured["candidate_id"]]["relationships"]["related_to"] == [
        "#V#episode_memory_alice"
    ]


def test_capture_owned_discussion_without_materialising_conversation_concept(
    store: _MemoryStore,
) -> None:
    before_ids = set(store.docs)
    captured = _capture_from_discussion(
        source={"kind": "conversation", "session_id": "session-without-concept"},
        idempotency_key="unmaterialised-discussion",
        request_id="unmaterialised-discussion-request",
    )

    assert captured["source"] == {
        "kind": "conversation",
        "locator": {
            "session_id": "session-without-concept",
            "owner_user_id": "#V#alice",
            "namespace": "#V#alice@research_org",
        },
    }
    candidate_doc = store.docs[captured["candidate_id"]]
    assert "related_to" not in candidate_doc["relationships"]
    assert set(store.docs) - before_ids == {captured["candidate_id"]}

    # Later materialisation of a conversation concept must not alter the retry
    # identity chosen from the original session-only source.
    store.docs["#V#conversation_materialised_later"] = {
        "concept_id": "#V#conversation_materialised_later",
        "relationships": {
            "is_an_instance_of": ["#V#conversation"],
            "#V#specific_to_user": ["#V#alice"],
        },
        "metadata": {"session_id": "session-without-concept"},
    }
    replay = _capture_from_discussion(
        source={"kind": "conversation", "session_id": "session-without-concept"},
        idempotency_key="unmaterialised-discussion",
        request_id="unmaterialised-discussion-request",
    )
    assert replay["candidate_id"] == captured["candidate_id"]
    assert replay["idempotent"] is True


def test_session_source_cannot_select_a_different_namespace(
    store: _MemoryStore,
) -> None:
    with pytest.raises(service.LearningCandidateAccessError) as exc_info:
        _capture_from_discussion(
            source={
                "kind": "conversation",
                "session_id": "session-without-concept",
                "namespace": "#V#alice@different_org",
            },
            idempotency_key="wrong-source-namespace",
        )

    assert exc_info.value.reason_code == (
        "learning_candidate_source_namespace_mismatch"
    )
    assert store.insert_count == 0
    assert store.text_write_count == 0


def test_organisation_candidate_can_target_role_without_user_beneficiary(
    store: _MemoryStore,
) -> None:
    captured = service.capture_learning_candidate(
        body="Preserve wetland-impact evidence with each restoration decision.",
        source={
            "kind": "episode_critique_memory",
            "memory_id": "#V#episode_memory_org",
        },
        contributor_concept_ids=["#V#von_system"],
        target_concept_ids=["#V#role_ecosystem_steward"],
        audience_concept_ids=["#V#research_team"],
        beneficiary_concept_ids=["#V#research_org", "#V#earth_ecosystems"],
        purpose_concept_ids=["#V#purpose_restore_wetlands"],
        organisation_concept_id="#V#research_org",
        visibility_scope="organisation",
        idempotency_key="org-role-learning-1",
    )

    assert captured["authorship"]["capture_actor_concept_id"] is None
    assert captured["authorship"]["capture_organisation_concept_id"] == (
        "#V#research_org"
    )
    assert captured["target_concept_ids"] == ["#V#role_ecosystem_steward"]
    assert "#V#alice" not in captured["beneficiary_concept_ids"]
    assert captured["beneficiary_concept_ids"] == [
        "#V#research_org",
        "#V#earth_ecosystems",
    ]
    relationships = store.docs[captured["candidate_id"]]["relationships"]
    assert relationships["#V#specific_to_organisation"] == ["#V#research_org"]
    assert "#V#specific_to_user" not in relationships
    assert (
        "#V#role_ecosystem_steward" not in relationships["#V#specific_to_organisation"]
    )


def test_invisible_source_is_denied_before_any_write(store: _MemoryStore) -> None:
    before_docs = deepcopy(store.docs)

    with pytest.raises(service.LearningCandidateAccessError) as exc_info:
        _capture_from_discussion(
            actor_user_id="#V#bob",
            organisation_concept_id=None,
            namespace=None,
            idempotency_key="bob-cannot-see-source",
        )

    assert exc_info.value.reason_code == "learning_candidate_source_not_visible"
    assert store.docs == before_docs
    assert store.insert_count == 0
    assert store.text_write_count == 0


def test_invalid_conversation_locator_is_rejected_before_write(
    store: _MemoryStore,
) -> None:
    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="does not match the canonical conversation",
    ):
        _capture_from_discussion(
            source={
                "kind": "conversation",
                "conversation_concept_id": "#V#conversation_alice",
                "session_id": "different-session",
            }
        )

    assert store.insert_count == 0
    assert store.text_write_count == 0


def test_capture_retry_is_idempotent_without_semantic_global_deduplication(
    store: _MemoryStore,
) -> None:
    first = _capture_from_discussion()
    replay = _capture_from_discussion()
    independent = _capture_from_discussion(
        idempotency_key="different-request-same-body",
        request_id="different-request",
    )

    assert replay["candidate_id"] == first["candidate_id"]
    assert replay["created"] is False
    assert replay["idempotent"] is True
    assert independent["candidate_id"] != first["candidate_id"]
    assert store.insert_count == 2

    with pytest.raises(service.LearningCandidateConflictError):
        _capture_from_discussion(body="Different data under the same retry identity.")


def test_other_actor_cannot_read_or_revise_candidate(store: _MemoryStore) -> None:
    captured = _capture_from_discussion()

    with pytest.raises(service.LearningCandidateNotFoundError):
        service.get_learning_candidate(captured["candidate_id"], actor_user_id="#V#bob")
    with pytest.raises(service.LearningCandidateNotFoundError):
        service.revise_learning_candidate(
            captured["candidate_id"],
            body="Bob's attempted revision",
            revision_request_id="bob-revision",
            actor_user_id="#V#bob",
        )

    assert store.update_count == 0
    assert store.descriptions[captured["candidate_id"]] == captured["body"]


def test_semantic_references_do_not_grant_candidate_visibility(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion(
        contributor_concept_ids=["#V#bob"],
        target_concept_ids=["#V#role_ecosystem_steward"],
        audience_concept_ids=["#V#bob", "#V#research_team"],
        beneficiary_concept_ids=["#V#research_org", "#V#earth_ecosystems"],
        purpose_concept_ids=["#V#purpose_restore_wetlands"],
        idempotency_key="semantic-refs-do-not-authorise",
        request_id="semantic-refs-do-not-authorise",
    )

    relationships = store.docs[captured["candidate_id"]]["relationships"]
    assert relationships["#V#specific_to_user"] == ["#V#alice"]
    with pytest.raises(service.LearningCandidateNotFoundError):
        service.get_learning_candidate(captured["candidate_id"], actor_user_id="#V#bob")
    assert service.list_learning_candidates(actor_user_id="#V#bob") == []


def test_revision_retains_source_history_authorship_and_non_active_state(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion()
    revised = service.revise_learning_candidate(
        captured["candidate_id"],
        body="Read back the canonical work product before reporting its outcome.",
        revision_request_id="revision-2",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
        target_concept_ids=["#V#workflow_prepare_brief"],
        beneficiary_concept_ids=["#V#research_org"],
    )

    assert revised["revised"] is True
    assert revised["revision"] == 2
    assert revised["lifecycle_state"] == "non_active"
    assert revised["source"] == captured["source"]
    assert revised["authorship"]["author_concept_id"] == "#V#von_system"
    assert revised["authorship"]["capture_actor_concept_id"] == "#V#alice"
    assert revised["beneficiary_concept_ids"] == ["#V#research_org"]
    assert revised["prior_revisions"][0]["revision"] == 1
    assert revised["prior_revisions"][0]["body"] == captured["body"]
    assert revised["prior_revisions"][0]["body_sha256"] == captured["body_sha256"]

    replay = service.revise_learning_candidate(
        captured["candidate_id"],
        body=revised["body"],
        revision_request_id="revision-2",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
        target_concept_ids=["#V#workflow_prepare_brief"],
        beneficiary_concept_ids=["#V#research_org"],
    )
    assert replay["idempotent"] is True
    assert replay["revision"] == 2
    assert len(replay["prior_revisions"]) == 1


def test_list_is_actor_filtered_and_omits_candidate_when_source_is_withdrawn(
    store: _MemoryStore,
) -> None:
    discussion = _capture_from_discussion()
    episode = _capture_from_discussion(
        source={
            "kind": "episode_critique_memory",
            "memory_id": "#V#episode_memory_alice",
        },
        idempotency_key="episode-list-candidate",
        request_id="episode-list-request",
    )

    listed = service.list_learning_candidates(
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
        source_kind="conversation",
    )
    assert [item["candidate_id"] for item in listed] == [discussion["candidate_id"]]
    assert service.list_learning_candidates(actor_user_id="#V#bob") == []

    store.docs["#V#episode_memory_alice"]["relationships"]["#V#specific_to_user"] = [
        "#V#bob"
    ]
    listed_after_withdrawal = service.list_learning_candidates(
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert discussion["candidate_id"] in {
        item["candidate_id"] for item in listed_after_withdrawal
    }
    assert episode["candidate_id"] not in {
        item["candidate_id"] for item in listed_after_withdrawal
    }


def test_hidden_reference_is_rejected_without_candidate_write(
    store: _MemoryStore,
) -> None:
    store.docs["#V#hidden_target"] = {
        "concept_id": "#V#hidden_target",
        "relationships": {"#V#specific_to_user": ["#V#bob"]},
    }

    with pytest.raises(service.LearningCandidateAccessError) as exc_info:
        _capture_from_discussion(target_concept_ids=["#V#hidden_target"])

    assert exc_info.value.reason_code == "learning_candidate_reference_not_visible"
    assert store.insert_count == 0
    assert store.text_write_count == 0


def test_revoked_reference_is_redacted_without_hiding_candidate(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion()
    store.docs["#V#workflow_prepare_brief"]["relationships"] = {
        "#V#specific_to_user": ["#V#bob"]
    }

    read_back = service.get_learning_candidate(
        captured["candidate_id"],
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert read_back["candidate_id"] == captured["candidate_id"]
    assert read_back["target_concept_ids"] == []
    assert read_back["semantic_reference_projection"]["redacted_counts"] == {
        "target_concept_ids": 1
    }

    listed = service.list_learning_candidates(
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert [candidate["candidate_id"] for candidate in listed] == [
        captured["candidate_id"]
    ]


def test_hidden_historical_refs_do_not_erase_organisation_learning(
    store: _MemoryStore,
) -> None:
    captured = service.capture_learning_candidate(
        body="Retain team evidence with the organisational decision.",
        source={
            "kind": "episode_critique_memory",
            "memory_id": "#V#episode_memory_org",
        },
        contributor_concept_ids=["#V#research_team"],
        target_concept_ids=["#V#workflow_prepare_brief"],
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
        visibility_scope="organisation",
        idempotency_key="org-learning-with-departed-contributor",
    )
    service.revise_learning_candidate(
        captured["candidate_id"],
        body="A revised lesson for the role.",
        revision_request_id="move-target-to-role",
        actor_user_id="#V#bob",
        organisation_concept_id="#V#research_org",
        contributor_concept_ids=["#V#von_system"],
        target_concept_ids=["#V#role_ecosystem_steward"],
    )
    store.docs["#V#research_team"]["relationships"] = {
        "#V#specific_to_user": ["#V#alice"]
    }
    store.docs["#V#workflow_prepare_brief"]["relationships"] = {
        "#V#specific_to_user": ["#V#alice"]
    }

    read_back = service.get_learning_candidate(
        captured["candidate_id"],
        actor_user_id="#V#bob",
        organisation_concept_id="#V#research_org",
    )
    assert read_back["body"] == "A revised lesson for the role."
    assert read_back["lifecycle_state"] == "non_active"
    assert read_back["prior_revisions"][0]["contributor_concept_ids"] == []
    assert read_back["prior_revisions"][0]["target_concept_ids"] == []
    assert read_back["prior_revisions"][0]["semantic_reference_projection"] == {
        "redacted_counts": {
            "contributor_concept_ids": 1,
            "target_concept_ids": 1,
        }
    }
    assert read_back["semantic_reference_projection"] == {
        "access_basis": "candidate_and_source_visibility",
        "redacted_counts": {},
        "historical_redacted_reference_count": 2,
    }
    assert "#V#research_team" not in repr(read_back)
    assert "#V#workflow_prepare_brief" not in repr(read_back)

    listed = service.list_learning_candidates(
        actor_user_id="#V#bob",
        organisation_concept_id="#V#research_org",
    )
    assert [candidate["candidate_id"] for candidate in listed] == [
        captured["candidate_id"]
    ]


def test_episode_source_must_be_canonical_memory_instance(
    store: _MemoryStore,
) -> None:
    store.docs["#V#memory_impostor"] = {
        "concept_id": "#V#memory_impostor",
        "relationships": {"is_an_instance_of": ["#V#artifact"]},
        "concept_data": {
            "episode_critique_memory": {
                "schema_version": "episode_critique_memory.v1",
                "memory_id": "#V#memory_impostor",
            }
        },
    }

    with pytest.raises(service.LearningCandidateAccessError) as exc_info:
        _capture_from_discussion(
            source={
                "kind": "episode_critique_memory",
                "memory_id": "#V#memory_impostor",
            }
        )

    assert exc_info.value.reason_code == "learning_candidate_source_not_visible"
    assert store.insert_count == 0


def test_organisation_visible_candidate_allows_trusted_org_revision(
    store: _MemoryStore,
) -> None:
    captured = service.capture_learning_candidate(
        body="Keep evidence with the organisational decision.",
        source={
            "kind": "episode_critique_memory",
            "memory_id": "#V#episode_memory_org",
        },
        contributor_concept_ids=["#V#alice"],
        target_concept_ids=["#V#role_ecosystem_steward"],
        beneficiary_concept_ids=["#V#earth_ecosystems"],
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
        visibility_scope="organisation",
        idempotency_key="shared-org-candidate",
    )

    same_org_read = service.get_learning_candidate(
        captured["candidate_id"],
        actor_user_id="#V#bob",
        organisation_concept_id="#V#research_org",
    )
    assert same_org_read["body"] == captured["body"]

    revised_by_member = service.revise_learning_candidate(
        captured["candidate_id"],
        body="Keep canonical evidence with the organisational decision.",
        revision_request_id="org-member-revision",
        actor_user_id="#V#bob",
        organisation_concept_id="#V#research_org",
    )
    assert (
        revised_by_member["authorship"]["last_revised_by_actor_concept_id"] == "#V#bob"
    )
    assert revised_by_member["lifecycle_state"] == "non_active"

    revised_by_org_scope = service.revise_learning_candidate(
        captured["candidate_id"],
        body="Keep read-back evidence with the organisational decision.",
        revision_request_id="org-scope-revision",
        organisation_concept_id="#V#research_org",
    )
    assert (
        revised_by_org_scope["authorship"]["last_revised_by_actor_concept_id"] is None
    )
    assert (
        revised_by_org_scope["authorship"]["last_revised_by_organisation_concept_id"]
        == "#V#research_org"
    )


def test_historical_revision_retry_does_not_overwrite_newer_revision(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion()
    revision_a = service.revise_learning_candidate(
        captured["candidate_id"],
        body="Revision A",
        revision_request_id="revision-A",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    revision_b = service.revise_learning_candidate(
        captured["candidate_id"],
        body="Revision B",
        revision_request_id="revision-B",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )

    delayed_a = service.revise_learning_candidate(
        captured["candidate_id"],
        body="Revision A",
        revision_request_id="revision-A",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert delayed_a["idempotent"] is True
    assert delayed_a["replayed_revision"] == revision_a["revision"]
    assert delayed_a["revision"] == revision_b["revision"]
    assert delayed_a["body"] == "Revision B"
    assert store.descriptions[captured["candidate_id"]] == "Revision B"

    with pytest.raises(service.LearningCandidateConflictError):
        service.revise_learning_candidate(
            captured["candidate_id"],
            body="Different payload reusing revision A",
            revision_request_id="revision-A",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )


def test_exact_revision_retry_repairs_interrupted_body_write(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_from_discussion()
    write_attempts = 0

    def _fail_first_revision_body_write(**kwargs: Any) -> dict[str, Any]:
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            raise RuntimeError("simulated revision text relation outage")
        return store.upsert_text(**kwargs)

    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        _fail_first_revision_body_write,
    )
    revision_arguments = {
        "body": "A revision whose body write can be retried exactly.",
        "revision_request_id": "repair-interrupted-revision",
        "actor_user_id": "#V#alice",
        "organisation_concept_id": "#V#research_org",
    }
    with pytest.raises(RuntimeError, match="revision text relation outage"):
        service.revise_learning_candidate(
            captured["candidate_id"],
            **revision_arguments,
        )

    stored_state = store.docs[captured["candidate_id"]]["concept_data"][
        "learning_candidate"
    ]
    assert stored_state["revision"] == 2
    assert store.descriptions[captured["candidate_id"]] == captured["body"]

    repaired = service.revise_learning_candidate(
        captured["candidate_id"],
        **revision_arguments,
    )
    assert repaired["revised"] is False
    assert repaired["idempotent"] is True
    assert repaired["revision"] == 2
    assert repaired["body"] == revision_arguments["body"]
    assert store.descriptions[captured["candidate_id"]] == revision_arguments["body"]


def test_partial_capture_is_explicit_in_list_and_exact_retry_repairs_it(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_attempts = 0

    def _fail_first_body_write(**kwargs: Any) -> dict[str, Any]:
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            raise RuntimeError("simulated text relation outage")
        return store.upsert_text(**kwargs)

    monkeypatch.setattr(
        service, "upsert_singleton_text_relation", _fail_first_body_write
    )
    with pytest.raises(RuntimeError, match="simulated text relation outage"):
        _capture_from_discussion()

    invalid_rows = service.list_learning_candidates(
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert len(invalid_rows) == 1
    assert invalid_rows[0] == {
        "candidate_id": next(
            concept_id
            for concept_id in store.docs
            if concept_id.startswith("#V#learning_candidate_")
        ),
        "schema_version": "learning_candidate.v1",
        "lifecycle_state": "non_active",
        "projection_status": "invalid",
        "error_code": "learning_candidate_invalid_projection",
    }
    repaired = _capture_from_discussion()
    assert repaired["created"] is False
    assert repaired["idempotent"] is True
    assert repaired["body"] == (
        "Check the canonical work product before claiming completion."
    )
