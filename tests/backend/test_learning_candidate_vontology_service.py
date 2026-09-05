"""Focused tests for non-active, source-grounded learning candidates."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from pymongo.errors import DuplicateKeyError

from src.backend.services import learning_candidate_vontology_service as service
from src.backend.services.learning_advice_experiment_service import (
    build_learning_advice_experiment_manifest,
    build_learning_advice_experiment_plan,
    build_learning_advice_experiment_run_binding,
    compute_learning_advice_paired_results,
    evaluate_learning_advice_content_decision,
)


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
            ("#V#alice", "session-alice"),
            ("#V#alice", "session-without-concept"),
        }
        self.chat_session_checks: list[dict[str, Any]] = []
        self.transcript_page_calls: list[dict[str, Any]] = []
        self.debug_entry_calls: list[dict[str, Any]] = []
        self.chat_histories: dict[tuple[str, str], list[dict[str, Any]]] = {
            ("#V#alice", "session-alice"): [
                {
                    "role": "user",
                    "content": "Please check the finished work.",
                    "turn_id": "turn-user-1",
                    "message_id": "message-user-1",
                },
                {
                    "role": "assistant",
                    "content": "I claimed completion before checking.",
                    "turn_id": "turn-assistant-1",
                },
                {
                    "role": "user",
                    "content": "The read-back is the important evidence.",
                    "turn_id": "turn-user-2",
                },
            ],
            ("#V#alice", "session-without-concept"): [],
        }
        self.chat_debug_entries: dict[tuple[str, str, int], dict[str, Any]] = {
            ("#V#alice", "session-alice", 1): {
                "request_id": "request-assistant-1",
                "turn_execution_record": {
                    "schema_version": "turn_execution_record.v1",
                    "request_id": "request-assistant-1",
                    "terminal_status": "completed",
                    "tool_invocations": [
                        {
                            "tool": "gmail_list_messages",
                            "status": "ok",
                            "evidence": {"sha256": "a" * 64},
                        }
                    ],
                },
            }
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
        assert kwargs["identity_mode"] == "exact"
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
        **kwargs: Any,
    ) -> bool:
        self.chat_session_checks.append(
            {"user_id": user_id, "session_id": session_id, **kwargs}
        )
        return (user_id, session_id) in self.owned_chat_sessions

    def get_chat_history_transcript_page(
        self,
        *,
        user_id: str,
        session_id: str,
        namespace: str | None = None,
        include_legacy: bool = True,
        offset: int = 0,
        page_size: int = 50,
    ) -> dict[str, Any]:
        self.transcript_page_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "namespace": namespace,
                "include_legacy": include_legacy,
                "offset": offset,
                "page_size": page_size,
            }
        )
        history = self.chat_histories.get((user_id, session_id))
        if history is None:
            return {"found": False, "messages": []}
        messages: list[dict[str, Any]] = []
        for history_index, value in enumerate(
            history[offset : offset + page_size], start=offset
        ):
            message = deepcopy(value)
            message["source_locator"] = {
                "session_id": session_id,
                "history_index": history_index,
                "turn_id": message.get("turn_id"),
                "message_id": message.get("message_id"),
            }
            messages.append(message)
        return {"found": True, "messages": messages}

    def get_chat_history_debug_entry(
        self,
        *,
        user_id: str,
        session_id: str,
        history_index: int,
        namespace: str | None = None,
        include_legacy: bool = True,
        hydrate_blob_refs: bool = True,
    ) -> dict[str, Any] | None:
        self.debug_entry_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "history_index": history_index,
                "namespace": namespace,
                "include_legacy": include_legacy,
                "hydrate_blob_refs": hydrate_blob_refs,
            }
        )
        value = self.chat_debug_entries.get((user_id, session_id, history_index))
        return deepcopy(value) if value is not None else None


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
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_transcript_page",
        memory.get_chat_history_transcript_page,
    )
    monkeypatch.setattr(
        service.chat_history_service,
        "get_chat_history_debug_entry",
        memory.get_chat_history_debug_entry,
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
    assert store.debug_entry_calls == []


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


def test_session_source_preserves_explicit_non_legacy_boundary(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion(
        source={
            "kind": "conversation",
            "session_id": "session-without-concept",
            "include_legacy": False,
        },
        idempotency_key="non-legacy-discussion",
        request_id="non-legacy-discussion-request",
    )

    assert captured["source"]["locator"]["include_legacy"] is False
    assert store.chat_session_checks
    assert all(call["include_legacy"] is False for call in store.chat_session_checks)


def test_session_source_rejects_non_boolean_legacy_boundary(
    store: _MemoryStore,
) -> None:
    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="source.include_legacy must be a boolean",
    ):
        _capture_from_discussion(
            source={
                "kind": "conversation",
                "session_id": "session-without-concept",
                "include_legacy": "false",
            },
            idempotency_key="invalid-legacy-boundary",
        )

    assert store.insert_count == 0
    assert store.text_write_count == 0


def test_exact_conversation_messages_are_verified_and_attested(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion(
        source={
            "kind": "conversation",
            "session_id": "session-alice",
            "include_legacy": False,
            "history_indices": [2, 0, 2],
        },
        idempotency_key="exact-discussion-evidence",
        request_id="exact-discussion-evidence-request",
    )

    def sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def stable_digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    locator_two = {
        "session_id": "session-alice",
        "history_index": 2,
        "turn_id": "turn-user-2",
    }
    locator_zero = {
        "session_id": "session-alice",
        "history_index": 0,
        "turn_id": "turn-user-1",
        "message_id": "message-user-1",
    }
    assert captured["source"] == {
        "kind": "conversation",
        "locator": {
            "session_id": "session-alice",
            "owner_user_id": "#V#alice",
            "include_legacy": False,
            "namespace": "#V#alice@research_org",
        },
        "message_evidence": [
            {
                "source_locator": locator_two,
                "content_sha256": sha256_text(
                    "The read-back is the important evidence."
                ),
                "role_sha256": sha256_text("user"),
                "turn_identity_sha256": stable_digest(locator_two),
            },
            {
                "source_locator": locator_zero,
                "content_sha256": sha256_text("Please check the finished work."),
                "role_sha256": sha256_text("user"),
                "turn_identity_sha256": stable_digest(locator_zero),
            },
        ],
    }
    stored_source = store.docs[captured["candidate_id"]]["concept_data"][
        "learning_candidate"
    ]["source"]
    assert stored_source == captured["source"]
    assert "Please check the finished work." not in repr(stored_source)
    assert store.transcript_page_calls
    assert all(call["include_legacy"] is False for call in store.transcript_page_calls)

    read_back = service.get_learning_candidate(
        captured["candidate_id"],
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert read_back["source"] == captured["source"]
    assert len(read_back["source_locator_sha256"]) == 64


def test_exact_assistant_turn_execution_evidence_is_opt_in_and_body_free(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion(
        source={
            "kind": "conversation",
            "session_id": "session-alice",
            "include_legacy": False,
            "include_execution_evidence": True,
            "history_indices": [0, 1],
        },
        idempotency_key="exact-turn-execution-evidence",
        request_id="exact-turn-execution-evidence-request",
    )

    exact_record = store.chat_debug_entries[("#V#alice", "session-alice", 1)][
        "turn_execution_record"
    ]
    encoded_record = json.dumps(
        exact_record,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_digest = hashlib.sha256(encoded_record.encode("utf-8")).hexdigest()
    source = captured["source"]

    assert source["locator"]["include_execution_evidence"] is True
    assert "turn_execution_evidence" not in source["message_evidence"][0]
    assert source["message_evidence"][1]["turn_execution_evidence"] == {
        "request_id": "request-assistant-1",
        "turn_execution_record_sha256": expected_digest,
    }
    assert exact_record["tool_invocations"][0]["tool"] not in repr(
        source["message_evidence"][1]["turn_execution_evidence"]
    )
    expected_debug_read = {
        "user_id": "#V#alice",
        "session_id": "session-alice",
        "history_index": 1,
        "namespace": "#V#alice@research_org",
        "include_legacy": False,
        "hydrate_blob_refs": False,
    }
    # Capture performs canonical read-back, so it attests once before and once
    # after persistence.
    assert store.debug_entry_calls == [expected_debug_read, expected_debug_read]

    read_back = service.get_learning_candidate(
        captured["candidate_id"],
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert read_back["source"] == source
    assert store.debug_entry_calls == [
        expected_debug_read,
        expected_debug_read,
        expected_debug_read,
    ]


def test_missing_opted_in_assistant_execution_evidence_blocks_capture(
    store: _MemoryStore,
) -> None:
    store.chat_debug_entries.clear()

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="assistant message has no exact turn execution record",
    ):
        _capture_from_discussion(
            source={
                "kind": "conversation",
                "session_id": "session-alice",
                "include_execution_evidence": True,
                "history_indices": [1],
            },
            idempotency_key="missing-turn-execution-evidence",
        )

    assert store.insert_count == 0
    assert store.text_write_count == 0


def test_changed_turn_execution_evidence_is_re_attested_for_every_operation(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion(
        source={
            "kind": "conversation",
            "session_id": "session-alice",
            "include_execution_evidence": True,
            "history_indices": [1],
        },
        idempotency_key="changed-turn-execution-evidence",
    )
    store.chat_debug_entries[("#V#alice", "session-alice", 1)]["turn_execution_record"][
        "terminal_status"
    ] = "failed"
    writes_before = (store.update_count, store.text_write_count)

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="message evidence no longer matches",
    ):
        service.get_learning_candidate(
            captured["candidate_id"],
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    listed = service.list_learning_candidates(
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    listed_candidate = next(
        item for item in listed if item["candidate_id"] == captured["candidate_id"]
    )
    assert listed_candidate["projection_status"] == "invalid"

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="message evidence no longer matches",
    ):
        service.revise_learning_candidate(
            captured["candidate_id"],
            body="A revision that must not survive stale source evidence.",
            revision_request_id="stale-execution-evidence-revision",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="message evidence no longer matches",
    ):
        service.record_learning_candidate_disposition(
            captured["candidate_id"],
            experiment_run_id="#V#experiment_run_stale_execution_evidence",
            disposition_request_id="stale-execution-evidence-disposition",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    assert (store.update_count, store.text_write_count) == writes_before


def test_changed_cited_message_is_detected_on_readback(store: _MemoryStore) -> None:
    captured = _capture_from_discussion(
        source={
            "kind": "conversation",
            "session_id": "session-alice",
            "history_indices": [1],
        },
        idempotency_key="changed-discussion-evidence",
        request_id="changed-discussion-evidence-request",
    )
    store.chat_histories[("#V#alice", "session-alice")][1]["content"] = (
        "This stored transcript message was changed."
    )

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="message evidence no longer matches",
    ):
        service.get_learning_candidate(
            captured["candidate_id"],
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )


def test_changed_cited_message_blocks_revision_before_any_write(
    store: _MemoryStore,
) -> None:
    captured = _capture_from_discussion(
        source={
            "kind": "conversation",
            "session_id": "session-alice",
            "history_indices": [1],
        },
        idempotency_key="changed-before-revision",
        request_id="changed-before-revision-request",
    )
    store.chat_histories[("#V#alice", "session-alice")][1]["content"] = (
        "This transcript message changed before the requested revision."
    )
    writes_before = (store.update_count, store.text_write_count)

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="message evidence no longer matches",
    ):
        service.revise_learning_candidate(
            captured["candidate_id"],
            body="A revision that must not be persisted.",
            revision_request_id="blocked-revision",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    assert (store.update_count, store.text_write_count) == writes_before


def test_unreadable_or_unbounded_history_indices_are_rejected_before_write(
    store: _MemoryStore,
) -> None:
    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="is not a readable message",
    ):
        _capture_from_discussion(
            source={
                "kind": "conversation",
                "session_id": "session-alice",
                "history_indices": [99],
            },
            idempotency_key="missing-discussion-evidence",
        )

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="at most 32 distinct indices",
    ):
        _capture_from_discussion(
            source={
                "kind": "conversation",
                "session_id": "session-alice",
                "history_indices": list(range(33)),
            },
            idempotency_key="unbounded-discussion-evidence",
        )

    assert store.insert_count == 0
    assert store.text_write_count == 0


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


def test_explicit_idempotency_key_allows_cross_turn_capture_retry(
    store: _MemoryStore,
) -> None:
    first = _capture_from_discussion(request_id="turn-one")
    replay = _capture_from_discussion(request_id="turn-two")

    assert replay["candidate_id"] == first["candidate_id"]
    assert replay["idempotent"] is True
    assert replay["capture_request_id"] == "turn-one"
    assert store.insert_count == 1


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


def test_revision_cannot_erase_a_concurrent_same_revision_disposition(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion()
    candidate_id = candidate["candidate_id"]
    original_update = store.update_one

    def disposition_wins_before_revision_cas(
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> SimpleNamespace:
        state = store.docs[candidate_id]["concept_data"]["learning_candidate"]
        state["evaluation_disposition"] = "rejected"
        state["disposition_history"] = [{"marker": "concurrent-disposition"}]
        state["updated_at"] = "2026-09-05T00:00:01+00:00"
        return original_update(query, update, upsert=upsert)

    monkeypatch.setattr(
        service.ConceptsRepository,
        "update_one",
        disposition_wins_before_revision_cas,
    )

    with pytest.raises(
        service.LearningCandidateConflictError,
        match="changed before this revision was stored",
    ):
        service.revise_learning_candidate(
            candidate_id,
            body="A revision that must not erase concurrently learned evidence.",
            revision_request_id="revision-loses-to-concurrent-disposition",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    stored = store.docs[candidate_id]["concept_data"]["learning_candidate"]
    assert stored["revision"] == 1
    assert stored["evaluation_disposition"] == "rejected"
    assert stored["disposition_history"] == [{"marker": "concurrent-disposition"}]


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


def _experiment_state_for_candidate(
    candidate: dict[str, Any],
    *,
    decision: str = "arm_b_content_win",
    run_id: str = "#V#experiment_run_learning_advice",
) -> dict[str, Any]:
    runtime_snapshot = {
        "code_revision": "candidate-disposition-test-revision",
        "capability_catalogue_sha256": "1" * 64,
        "workflow_catalogue_sha256": "2" * 64,
        "relevant_ontology_sha256": "3" * 64,
        "acting_support_sha256": "4" * 64,
        "context_budget_tokens": 32_000,
        "turn_budget_seconds": 180.0,
        "final_synthesis_reserve_seconds": 30.0,
        "final_answer_reserve_seconds": 0.0,
    }
    acting_support_identity = {"gateway_sha256": "e" * 64}
    candidate_ref = {
        "candidate_id": candidate["candidate_id"],
        "revision": candidate["revision"],
        "evaluation_disposition": candidate["evaluation_disposition"],
        "body_sha256": candidate["body_sha256"],
        "revision_identity_sha256": candidate["revision_identity_sha256"],
        "source_locator_sha256": candidate["source_locator_sha256"],
    }
    cases: list[dict[str, Any]] = []
    for index in range(1, 7):
        cases.append(
            {
                "case_id": f"candidate-disposition-case-{index}",
                "kind": "applicable" if index <= 4 else "control",
                "prompt": f"Independent held-out case {index}",
                "evaluator_concept_id": "#V#candidate_disposition_evaluator",
                "evaluator_sha256": "5" * 64,
                "rubric_concept_id": "#V#candidate_disposition_rubric",
                "rubric_sha256": "6" * 64,
                "evaluation_contract": {
                    "allowed_first_capabilities": ["bounded_read"],
                    "required_success_capability": "bounded_read",
                    "after_capability_failure": "report_specific_failure",
                    "focused_clarification_verdict": "fail",
                    "unsolicited_alternative_policy": "fail",
                    "read_only": True,
                },
            }
        )
    decision_rule = {
        "minimum_applicable_b_passes": 6,
        "minimum_applicable_pass_delta": 2,
        "require_more_paired_improvements_than_regressions": True,
        "forbid_b_only_material_failure": True,
        "require_control_non_inferiority": True,
    }
    manifest = build_learning_advice_experiment_manifest(
        experiment_spec_id="#V#experiment_spec_learning_advice",
        experiment_run_id=run_id,
        candidate_ref=candidate_ref,
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
        namespace="#V#alice@research_org",
        provider="openai",
        model_id="gpt-5.6-luna",
        model_parameters={"temperature": 0.2},
        runtime_snapshot=runtime_snapshot,
        cases=cases,
        decision_rule=decision_rule,
        randomisation_seed=2720,
        repeats=2,
    )
    plan = build_learning_advice_experiment_plan(manifest)
    binding = build_learning_advice_experiment_run_binding(manifest, plan=plan)
    request_ids: list[str] = []
    trial_ids: list[str] = []
    trial_rows: list[dict[str, Any]] = []
    trial_observations: list[dict[str, Any]] = []
    turn_execution_records: dict[str, dict[str, Any]] = {}

    def _digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    for index, planned_trial in enumerate(plan["trials"], start=1):
        request_id = f"trial-request-{index:02d}"
        observation_id = f"trial-observation-{index:02d}"
        request_ids.append(request_id)
        trial_ids.append(observation_id)
        if planned_trial["case_kind"] == "control":
            verdict = "pass"
        elif decision == "arm_b_not_supported":
            verdict = "pass" if planned_trial["arm"] == "A" else "fail"
        else:
            verdict = "fail" if planned_trial["arm"] == "A" else "pass"
        external_availability_changed = bool(
            decision == "inconclusive"
            and planned_trial["case_kind"] == "applicable"
            and planned_trial["case_id"] == "candidate-disposition-case-1"
        )
        model_call_receipt = {
            "schema_version": "learning_advice_evaluator_model_call_receipt.v1",
            "call_id": f"evaluation-call-{index:02d}",
            "provider": "openai",
            "requested_model": "gpt-5.6-luna",
            "selected_model": "gpt-5.6-luna",
            "effective_model": "gpt-5.6-luna",
            "provider_observed_model": "gpt-5.6-luna",
            "provider_request_sent": True,
            "success": True,
        }
        response_sha256 = hashlib.sha256(
            f"synthetic-response-{index:02d}".encode("utf-8")
        ).hexdigest()
        trial_ter = {
            "schema_version": "turn_execution_record.v1",
            "request_id": request_id,
            "session_id": f"trial-session-{index:02d}",
            "namespace": "#V#alice@research_org",
            "user_id": "#V#alice",
            "org_id": "#V#research_org",
            "prompt": {
                "sha256": hashlib.sha256(
                    planned_trial["prompt"].encode("utf-8")
                ).hexdigest()
            },
            "final_response": {"response_sha256": response_sha256},
            "aux_llm_calls": [
                {
                    "type": "learning_advice_experiment_trial_binding",
                    "schema_version": "learning_advice_experiment_trial_execution.v1",
                    "experiment_spec_id": manifest["experiment_spec_id"],
                    "experiment_run_id": run_id,
                    "manifest_sha256": manifest["manifest_sha256"],
                    "plan_sha256": plan["plan_sha256"],
                    "trial_id": planned_trial["trial_id"],
                    "pair_id": planned_trial["pair_id"],
                    "case_id": planned_trial["case_id"],
                    "repeat": planned_trial["repeat"],
                    "arm": planned_trial["arm"],
                    "turn_id": f"trial-turn-{index:02d}",
                }
            ],
        }
        turn_execution_records[request_id] = trial_ter
        runtime_identity = {
            "schema_version": "learning_advice_evaluator_runtime_identity.v1",
            "provider": "openai",
            "requested_model": "gpt-5.6-luna",
            "selected_model": "gpt-5.6-luna",
            "effective_model": "gpt-5.6-luna",
            "model_parameters_sha256": _digest(manifest["model"]["parameters"]),
            "code_revision": runtime_snapshot["code_revision"],
            "runtime_snapshot_sha256": _digest(runtime_snapshot),
        }
        evaluation_provenance = {
            "schema_version": "learning_advice_evaluator_provenance.v1",
            "model_call_id": model_call_receipt["call_id"],
            "provider": "openai",
            "requested_model": "gpt-5.6-luna",
            "selected_model": "gpt-5.6-luna",
            "effective_model": "gpt-5.6-luna",
            "provider_observed_model": "gpt-5.6-luna",
            "model_call_receipt_sha256": _digest(model_call_receipt),
            "model_parameters_sha256": _digest(manifest["model"]["parameters"]),
            "code_revision": runtime_snapshot["code_revision"],
            "runtime_snapshot_sha256": _digest(runtime_snapshot),
            "runtime_code_identity_sha256": _digest(runtime_identity),
            "evaluator_concept_id": planned_trial["evaluator_concept_id"],
            "evaluator_sha256": planned_trial["evaluator_sha256"],
            "rubric_concept_id": planned_trial["rubric_concept_id"],
            "rubric_sha256": planned_trial["rubric_sha256"],
        }
        trial = {
            "schema_version": "learning_advice_experiment_observation.v1",
            "observation_kind": "paired_trial",
            "experiment_spec_id": manifest["experiment_spec_id"],
            "experiment_run_id": run_id,
            "manifest_sha256": manifest["manifest_sha256"],
            "plan_sha256": plan["plan_sha256"],
            "trial_id": planned_trial["trial_id"],
            "pair_id": planned_trial["pair_id"],
            "case_id": planned_trial["case_id"],
            "case_kind": planned_trial["case_kind"],
            "repeat": planned_trial["repeat"],
            "arm": planned_trial["arm"],
            "candidate_ref": candidate_ref,
            "model": manifest["model"],
            "runtime_snapshot": runtime_snapshot,
            "trial_integrity_valid": True,
            "execution_identity": {
                "request_id": request_id,
                "session_id": f"trial-session-{index:02d}",
                "turn_id": f"trial-turn-{index:02d}",
            },
            "turn_execution_request_ids": [request_id],
            "execution": {
                "acting_support_identity": acting_support_identity,
                "response_sha256": response_sha256,
                "turn_execution_record_sha256": _digest(trial_ter),
            },
            "evaluation": {
                "schema_version": "learning_advice_blind_evaluation_result.v1",
                "evaluation_id": f"evaluation-{index:02d}",
                "status": "completed",
                "passed": verdict == "pass",
                "verdict": verdict,
                "evaluator_output_sha256": hashlib.sha256(
                    f"evaluator-output-{index:02d}".encode()
                ).hexdigest(),
                "capability_choice": verdict,
                "work_product": verdict,
                "external_availability_changed": external_availability_changed,
                "material_failure": False,
                "evaluator_concept_id": planned_trial["evaluator_concept_id"],
                "evaluator_sha256": planned_trial["evaluator_sha256"],
                "rubric_concept_id": planned_trial["rubric_concept_id"],
                "rubric_sha256": planned_trial["rubric_sha256"],
                "authority_attestations": [
                    {
                        "kind": "evaluator",
                        "concept_id": planned_trial["evaluator_concept_id"],
                        "sha256": planned_trial["evaluator_sha256"],
                        "utf8_byte_count": 100,
                        "status": "canonical_bytes_attested",
                    },
                    {
                        "kind": "rubric",
                        "concept_id": planned_trial["rubric_concept_id"],
                        "sha256": planned_trial["rubric_sha256"],
                        "utf8_byte_count": 200,
                        "status": "canonical_bytes_attested",
                    },
                ],
                "model_call_receipt": model_call_receipt,
                "evaluation_provenance": evaluation_provenance,
                "reason_codes": [],
                "material_failure_codes": [],
            },
        }
        trial_rows.append(trial)
        trial_observations.append(
            {
                "observation_id": observation_id,
                "observation_type": "learning_advice_trial",
                "turn_execution_request_ids": [request_id],
                "evidence": {"learning_advice_trial": trial},
            }
        )
    paired_results = compute_learning_advice_paired_results(
        trial_rows,
        required_applicable_pair_count=8,
        required_control_pair_count=4,
    )
    content_decision = evaluate_learning_advice_content_decision(
        paired_results,
        decision_rule=decision_rule,
    )
    assert content_decision["decision"] == decision
    result: dict[str, Any] = {
        "schema_version": "learning_advice_experiment_result.v1",
        "experiment_spec_id": "#V#experiment_spec_learning_advice",
        "experiment_run_id": run_id,
        "manifest_sha256": manifest["manifest_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "candidate_ref": candidate_ref,
        "runtime_snapshot": runtime_snapshot,
        "acting_support_identity": acting_support_identity,
        "planned_trial_count": 24,
        "executed_trial_count": 24,
        "persisted_ter_count": 24,
        "persisted_trial_observation_count": 24,
        "valid_trial_count": 24,
        "completed_evaluation_count": 24,
        "paired_results": paired_results,
        "content_decision": content_decision,
        "secondary_metrics": {},
        "trial_observation_ids": trial_ids,
    }
    result["result_evidence_sha256"] = hashlib.sha256(
        json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    observation_verdict = {
        "arm_b_content_win": "pass",
        "arm_b_not_supported": "fail",
        "inconclusive": "inconclusive",
    }[decision]
    return {
        "run_id": run_id,
        "experiment_spec_id": "#V#experiment_spec_learning_advice",
        "namespace": "#V#alice@research_org",
        "user_id": "#V#alice",
        "org_id": "#V#research_org",
        "status": "completed",
        "verdict": "partial",
        "completed_at_utc": "2026-09-05T00:00:00+00:00",
        "metadata": {"learning_advice_experiment": binding},
        "turn_execution_request_ids": request_ids,
        "observations": [
            *trial_observations,
            {
                "observation_id": "final-result",
                "observation_type": "learning_advice_experiment_result",
                "verdict": observation_verdict,
                "evidence": {"learning_advice_result": result},
            },
        ],
        "_test_turn_execution_records": turn_execution_records,
    }


def _install_experiment_readback(
    monkeypatch: pytest.MonkeyPatch,
    experiment_state: dict[str, Any],
) -> None:
    canonical_state = deepcopy(experiment_state)
    records = canonical_state.pop("_test_turn_execution_records")
    monkeypatch.setattr(
        "src.backend.services.experiment_run_service.get_experiment_run_state",
        lambda _run_id: deepcopy(canonical_state),
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_turn_execution_record_projection",
        lambda *, request_id, namespace: (
            deepcopy(records.get(request_id))
            if namespace == "#V#alice@research_org"
            else None
        ),
    )


def test_disposition_is_derived_from_canonical_experiment_and_is_idempotent(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion()
    experiment_state = _experiment_state_for_candidate(candidate)
    _install_experiment_readback(monkeypatch, experiment_state)

    retained = service.record_learning_candidate_disposition(
        candidate["candidate_id"],
        experiment_run_id=experiment_state["run_id"],
        disposition_request_id="retain-from-bounded-experiment",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    replay = service.record_learning_candidate_disposition(
        candidate["candidate_id"],
        experiment_run_id=experiment_state["run_id"],
        disposition_request_id="retain-from-bounded-experiment",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )

    assert retained["evaluation_disposition"] == "retained"
    assert retained["disposition_recorded"] is True
    assert retained["disposition_history"][-1]["evidence_verdict"] == "supports_use"
    assert replay["idempotent"] is True
    assert replay["disposition_recorded"] is False
    assert len(replay["disposition_history"]) == 1


def test_negative_experiment_rejects_candidate_and_revision_resets_hypothesis(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion()
    experiment_state = _experiment_state_for_candidate(
        candidate,
        decision="arm_b_not_supported",
    )
    _install_experiment_readback(monkeypatch, experiment_state)

    rejected = service.record_learning_candidate_disposition(
        candidate["candidate_id"],
        experiment_run_id=experiment_state["run_id"],
        disposition_request_id="reject-from-bounded-experiment",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    revised = service.revise_learning_candidate(
        candidate["candidate_id"],
        body="A narrowed candidate that must earn new evidence.",
        revision_request_id="narrow-after-negative-evidence",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )

    assert rejected["evaluation_disposition"] == "rejected"
    assert rejected["disposition_history"][-1]["evidence_verdict"] == (
        "does_not_support_use"
    )
    assert revised["revision"] == 2
    assert revised["evaluation_disposition"] == "undecided"
    assert revised["prior_revisions"][-1]["evaluation_disposition"] == "rejected"


def test_stale_run_cannot_overwrite_a_newer_candidate_disposition(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion()
    rejecting_run = _experiment_state_for_candidate(
        candidate,
        decision="arm_b_not_supported",
        run_id="#V#experiment_run_reject_candidate",
    )
    retaining_stale_run = _experiment_state_for_candidate(
        candidate,
        decision="arm_b_content_win",
        run_id="#V#experiment_run_stale_retain_candidate",
    )

    _install_experiment_readback(monkeypatch, rejecting_run)
    rejected = service.record_learning_candidate_disposition(
        candidate["candidate_id"],
        experiment_run_id=rejecting_run["run_id"],
        disposition_request_id="reject-current-candidate",
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert rejected["evaluation_disposition"] == "rejected"

    _install_experiment_readback(monkeypatch, retaining_stale_run)
    with pytest.raises(
        service.LearningCandidateConflictError,
        match="disposition changed after this experiment was frozen",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=retaining_stale_run["run_id"],
            disposition_request_id="must-not-resurrect-stale-candidate",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    current = service.get_learning_candidate(
        candidate["candidate_id"],
        actor_user_id="#V#alice",
        organisation_concept_id="#V#research_org",
    )
    assert current["evaluation_disposition"] == "rejected"
    assert len(current["disposition_history"]) == 1


def test_inconclusive_or_tampered_experiment_cannot_change_disposition(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion()
    experiment_state = _experiment_state_for_candidate(
        candidate,
        decision="inconclusive",
    )
    _install_experiment_readback(monkeypatch, experiment_state)

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="inconclusive experiment cannot change",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id="do-not-decide-inconclusive",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    experiment_state["observations"][-1]["evidence"]["learning_advice_result"][
        "content_decision"
    ]["decision"] = "arm_b_content_win"
    _install_experiment_readback(monkeypatch, experiment_state)
    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="result digest does not match",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id="reject-tampered-evidence",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )


def test_nonterminal_unbound_or_foreign_experiment_cannot_change_disposition(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion()
    experiment_state = _experiment_state_for_candidate(candidate)
    writes_before = store.update_count

    experiment_state["status"] = "running"
    experiment_state["verdict"] = None
    experiment_state.pop("completed_at_utc")
    _install_experiment_readback(monkeypatch, experiment_state)
    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="not in a valid terminal state",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id="reject-nonterminal-evidence",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    experiment_state = _experiment_state_for_candidate(candidate)
    experiment_state["metadata"]["learning_advice_experiment"]["binding_sha256"] = (
        "f" * 64
    )
    _install_experiment_readback(monkeypatch, experiment_state)
    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="not bound to inspectable frozen evidence",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id="reject-unbound-evidence",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    experiment_state = _experiment_state_for_candidate(candidate)
    experiment_state["observations"].insert(
        0,
        {
            "observation_id": "foreign-observation",
            "observation_type": "unrelated",
            "evidence": {},
        },
    )
    _install_experiment_readback(monkeypatch, experiment_state)
    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="foreign or reordered evidence",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id="reject-foreign-evidence",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    assert store.update_count == writes_before


def test_redigested_self_report_cannot_override_canonical_trial_evidence(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion()
    experiment_state = _experiment_state_for_candidate(candidate)
    final_observation = experiment_state["observations"][-1]
    result = final_observation["evidence"]["learning_advice_result"]
    result["paired_results"]["applicable"]["b_pass_count"] = 0
    result["content_decision"]["decision"] = "arm_b_not_supported"
    result_payload = deepcopy(result)
    result_payload.pop("result_evidence_sha256")
    result["result_evidence_sha256"] = hashlib.sha256(
        json.dumps(
            result_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    final_observation["verdict"] = "fail"
    _install_experiment_readback(monkeypatch, experiment_state)

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="paired experiment result was not derived from its trials",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id="reject-redigested-self-report",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )


def test_redigested_contradictory_evaluator_verdict_cannot_set_disposition(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion()
    experiment_state = _experiment_state_for_candidate(candidate)
    trial_observations = experiment_state["observations"][:-1]
    forged_trial = next(
        item["evidence"]["learning_advice_trial"]
        for item in trial_observations
        if item["evidence"]["learning_advice_trial"]["evaluation"]["verdict"] == "pass"
    )
    evaluation = forged_trial["evaluation"]
    evaluation["capability_choice"] = "fail"
    evaluation["work_product"] = "fail"
    evaluator_payload = deepcopy(evaluation)
    evaluator_payload.pop("evaluator_output_sha256")
    evaluation["evaluator_output_sha256"] = hashlib.sha256(
        json.dumps(
            evaluator_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    result = experiment_state["observations"][-1]["evidence"]["learning_advice_result"]
    result_payload = deepcopy(result)
    result_payload.pop("result_evidence_sha256")
    result["result_evidence_sha256"] = hashlib.sha256(
        json.dumps(
            result_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    _install_experiment_readback(monkeypatch, experiment_state)
    writes_before = store.update_count

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="verdict contradicts its evaluator dimensions",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id="reject-redigested-contradictory-evaluator",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    assert store.update_count == writes_before


def test_unattested_evaluator_result_cannot_change_candidate_disposition(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion()
    experiment_state = _experiment_state_for_candidate(candidate)
    first_trial = experiment_state["observations"][0]["evidence"][
        "learning_advice_trial"
    ]
    first_trial["evaluation"]["model_call_receipt"].pop("provider_observed_model")
    _install_experiment_readback(monkeypatch, experiment_state)

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="evaluator model receipt is invalid",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id="reject-unattested-evaluator",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )


@pytest.mark.parametrize("readback_kind", ["missing", "different"])
def test_missing_or_changed_trial_ter_cannot_change_candidate_disposition(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
    readback_kind: str,
) -> None:
    candidate = _capture_from_discussion()
    experiment_state = _experiment_state_for_candidate(candidate)
    records = deepcopy(experiment_state["_test_turn_execution_records"])
    canonical_state = deepcopy(experiment_state)
    canonical_state.pop("_test_turn_execution_records")
    first_request_id = canonical_state["turn_execution_request_ids"][0]
    if readback_kind == "missing":
        records.pop(first_request_id)
    else:
        records[first_request_id]["final_response"]["response_sha256"] = "0" * 64
    monkeypatch.setattr(
        "src.backend.services.experiment_run_service.get_experiment_run_state",
        lambda _run_id: deepcopy(canonical_state),
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_turn_execution_record_projection",
        lambda *, request_id, namespace: (
            deepcopy(records.get(request_id))
            if namespace == "#V#alice@research_org"
            else None
        ),
    )
    writes_before = store.update_count

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="trial turn execution record does not match",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id=f"reject-{readback_kind}-trial-ter",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    assert store.update_count == writes_before


def test_changed_source_blocks_disposition_before_candidate_write(
    store: _MemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _capture_from_discussion(
        source={
            "kind": "conversation",
            "session_id": "session-alice",
            "history_indices": [1],
        }
    )
    experiment_state = _experiment_state_for_candidate(candidate)
    _install_experiment_readback(monkeypatch, experiment_state)
    store.chat_histories[("#V#alice", "session-alice")][1]["content"] = (
        "The cited message changed after the experiment."
    )
    writes_before = store.update_count

    with pytest.raises(
        service.InvalidLearningCandidateData,
        match="message evidence no longer matches",
    ):
        service.record_learning_candidate_disposition(
            candidate["candidate_id"],
            experiment_run_id=experiment_state["run_id"],
            disposition_request_id="must-not-ratchet-stale-source",
            actor_user_id="#V#alice",
            organisation_concept_id="#V#research_org",
        )

    assert store.update_count == writes_before
