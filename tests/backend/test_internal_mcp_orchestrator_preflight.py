from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)


class _StubResult:
    def __init__(self, payload, duration_ms=1.0):
        self.payload = payload
        self.duration_ms = duration_ms


class _CapturingGateway:
    enabled = True

    def __init__(self):
        self.invocations = []

    def describe_methods(self):
        return {}

    def invoke(self, tool_name, payload=None):
        self.invocations.append({"tool": tool_name, "payload": payload})
        return _StubResult({"ok": True})


class _CapturingLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate(self, prompt, *, context=None, model=None):
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        if self._responses:
            return self._responses.pop(0)
        return "ok"


def test_orchestrator_injects_deterministic_preflight_context(monkeypatch):
    def _fake_search_concepts(
        *, query="", instance_of=None, filter_kind=None, **_kwargs
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            assert filter_kind == ["predicate"]
            return {
                "results": [
                    {"concept_id": "#V#author_predicate"},
                    {"concept_id": "#V#affiliation_predicate"},
                ]
            }
        # Return empty results for topic queries
        return {"results": []}

    def _fake_get_texts_for_concept(concept_id, predicate=None, limit=50):
        if predicate != "hasName":
            return []
        if concept_id == "#V#author_predicate":
            return [
                {
                    "text": "Auteur",
                    "lang": "fr",
                    "context": {"name_type": "NL"},
                },
                {
                    "text": "Author",
                    "lang": "en",
                    "context": {"name_type": "NL"},
                },
            ]
        if concept_id == "#V#affiliation_predicate":
            return [
                {
                    "text": "Affiliation",
                    "lang": "fr",
                    "context": {"name_type": "NL"},
                }
            ]
        return []

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        _fake_get_texts_for_concept,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    llm = _CapturingLLM(["ok"])
    result = orchestrator.run(
        prompt="List the authors and affiliations for this paper.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="fr",
    )

    assert result.response_text == "ok"
    assert llm.calls, "expected a model call"

    context_messages = llm.calls[0]["context"] or []
    assert any(
        msg.get("role") == "system"
        and "ONTOLOGY PRE-FLIGHT" in (msg.get("content") or "")
        for msg in context_messages
    )
    assert any(
        "#V#author_predicate" in (msg.get("content") or "") for msg in context_messages
    )
    assert any(
        "#V#affiliation_predicate" in (msg.get("content") or "")
        for msg in context_messages
    )
    assert any("Auteur" in (msg.get("content") or "") for msg in context_messages)

    preflight_entries = [
        entry
        for entry in (result.aux_llm_calls or [])
        if entry.get("type") == "ontology_preflight"
    ]
    assert preflight_entries, "expected ontology preflight telemetry"
    assert preflight_entries[0].get("stage") == "deterministic_preflight"


# --- JVNAUTOSCI-1052: Topic vocabulary discovery tests ---


def test_extract_topic_keywords_from_context():
    """Test keyword extraction from conversation context."""
    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    # Test with prompt only
    keywords = orchestrator._extract_topic_keywords_from_context(
        "Tell me about machine learning algorithms", None
    )
    assert "machine" in keywords or "learning" in keywords or "algorithms" in keywords

    # Test with context
    context = [
        {"role": "user", "content": "I want to learn about neural networks"},
        {"role": "assistant", "content": "Sure, I can help with that."},
        {"role": "user", "content": "What about deep learning?"},
    ]
    keywords = orchestrator._extract_topic_keywords_from_context(
        "How do transformers work?", context
    )
    # Should include keywords from both prompt and recent user messages
    assert len(keywords) > 0
    # Should filter stop words
    assert "about" not in keywords
    assert "want" not in keywords


def test_extract_topic_keywords_filters_concept_ids():
    """Test that explicit concept IDs are filtered from keywords."""
    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    keywords = orchestrator._extract_topic_keywords_from_context(
        "Create a concept #V#machine_learning_algorithm as type", None
    )
    # The concept ID should be removed, but 'machine', 'learning', 'algorithm' should
    # not appear from the ID (they might appear from other words in the prompt)
    assert "#V#machine_learning_algorithm" not in " ".join(keywords)


def test_discover_types_for_topic(monkeypatch):
    """Test type discovery based on topic keywords."""
    search_calls = []

    def _fake_search_concepts(*, query="", filter_kind=None, **_kwargs):
        search_calls.append({"query": query, "filter_kind": filter_kind})
        if filter_kind == ["type"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#neural_network",
                        "name": "Neural Network",
                        "similarity_score": 0.85,
                    },
                    {
                        "concept_id": "#V#deep_learning_model",
                        "name": "Deep Learning Model",
                        "similarity_score": 0.75,
                    },
                ]
            }
        return {"results": []}

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    types = orchestrator._discover_types_for_topic(
        ["neural", "network", "learning"], "en"
    )

    assert len(types) == 2
    assert types[0]["concept_id"] == "#V#neural_network"
    assert types[0]["name"] == "Neural Network"
    assert any(c["filter_kind"] == ["type"] for c in search_calls)


def test_discover_predicates_for_topic(monkeypatch):
    """Test predicate discovery based on topic keywords."""
    search_calls = []

    def _fake_search_concepts(*, query="", filter_kind=None, **_kwargs):
        search_calls.append({"query": query, "filter_kind": filter_kind})
        if filter_kind == ["predicate"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#has_training_data",
                        "name": "has training data",
                        "similarity_score": 0.80,
                    },
                ]
            }
        return {"results": []}

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    predicates = orchestrator._discover_predicates_for_topic(
        ["neural", "network", "training"], "en"
    )

    assert len(predicates) == 1
    assert predicates[0]["concept_id"] == "#V#has_training_data"


def test_topic_vocabulary_caching(monkeypatch):
    """Test that topic vocabulary is cached properly."""
    call_count = {"types": 0, "predicates": 0}

    def _fake_search_concepts(*, query="", filter_kind=None, **_kwargs):
        if filter_kind == ["type"]:
            call_count["types"] += 1
            return {
                "results": [
                    {"concept_id": "#V#test_type", "name": "Test Type"},
                ]
            }
        if filter_kind == ["predicate"]:
            call_count["predicates"] += 1
            return {
                "results": [
                    {"concept_id": "#V#test_predicate", "name": "Test Predicate"},
                ]
            }
        return {"results": []}

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    keywords = ["machine", "learning"]

    # First discovery should hit the search
    types1 = orchestrator._discover_types_for_topic(keywords, "en")
    predicates1 = orchestrator._discover_predicates_for_topic(keywords, "en")

    # Second discovery with same keywords should use cache
    cache_key = orchestrator._get_topic_vocabulary_cache_key(keywords)

    # Manually populate cache to simulate what _build_ontology_preflight does
    import time

    orchestrator._topic_vocabulary_cache[cache_key] = {
        "timestamp": time.time(),
        "types": types1,
        "predicates": predicates1,
    }

    # Verify cache key generation is consistent
    cache_key2 = orchestrator._get_topic_vocabulary_cache_key(keywords)
    assert cache_key == cache_key2

    # Verify keywords order doesn't affect cache key
    cache_key3 = orchestrator._get_topic_vocabulary_cache_key(["learning", "machine"])
    assert cache_key == cache_key3


def test_topic_vocabulary_in_preflight_telemetry(monkeypatch):
    """Test that topic vocabulary appears in preflight telemetry."""

    def _fake_search_concepts(
        *, query="", instance_of=None, filter_kind=None, **_kwargs
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            return {"results": []}
        if filter_kind == ["type"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#research_paper",
                        "name": "Research Paper",
                        "similarity_score": 0.90,
                    },
                ]
            }
        if filter_kind == ["predicate"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#has_citation",
                        "name": "has citation",
                        "similarity_score": 0.85,
                    },
                ]
            }
        return {"results": []}

    def _fake_get_texts_for_concept(concept_id, predicate=None, limit=50):
        return []

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        _fake_get_texts_for_concept,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    llm = _CapturingLLM(["ok"])
    result = orchestrator.run(
        prompt="Find papers about machine learning research",
        context=[
            {"role": "user", "content": "I'm researching neural networks"},
        ],
        llm_client=llm,
        model=None,
        preferred_language="en",
    )

    preflight_entries = [
        entry
        for entry in (result.aux_llm_calls or [])
        if entry.get("type") == "ontology_preflight"
    ]

    assert preflight_entries, "expected ontology preflight telemetry"
    telemetry = preflight_entries[0]

    # Check topic vocabulary is in telemetry
    assert "topic_keywords" in telemetry
    assert "topic_types" in telemetry
    assert "topic_predicates" in telemetry
    assert "topic_vocabulary_cached" in telemetry

    # Verify types were discovered
    assert len(telemetry["topic_types"]) > 0
    assert any(t["concept_id"] == "#V#research_paper" for t in telemetry["topic_types"])


def test_topic_vocabulary_in_system_prompt(monkeypatch):
    """Test that topic vocabulary appears in the system prompt."""

    def _fake_search_concepts(
        *, query="", instance_of=None, filter_kind=None, **_kwargs
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            return {"results": []}
        if filter_kind == ["type"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#scientific_paper",
                        "name": "Scientific Paper",
                        "similarity_score": 0.88,
                    },
                ]
            }
        if filter_kind == ["predicate"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#authored_by",
                        "name": "authored by",
                        "similarity_score": 0.82,
                    },
                ]
            }
        return {"results": []}

    def _fake_get_texts_for_concept(concept_id, predicate=None, limit=50):
        return []

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        _fake_get_texts_for_concept,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    llm = _CapturingLLM(["ok"])
    orchestrator.run(
        prompt="Analyse this scientific paper about quantum computing",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
    )

    assert llm.calls, "expected a model call"
    context_messages = llm.calls[0]["context"] or []

    # Find the preflight message
    preflight_content = None
    for msg in context_messages:
        content = msg.get("content") or ""
        if "ONTOLOGY PRE-FLIGHT" in content:
            preflight_content = content
            break

    assert preflight_content is not None, "expected preflight message in context"

    # Check topic vocabulary section
    assert "Topic-relevant vocabulary" in preflight_content
    assert "#V#scientific_paper" in preflight_content
    assert "#V#authored_by" in preflight_content
    assert "Types:" in preflight_content
    assert "Predicates:" in preflight_content


def test_preflight_telemetry_exposes_discovery_path_provenance(monkeypatch):
    """JVNAUTOSCI-987: final suggestions should include explicit source paths."""

    def _fake_search_concepts(
        *, query="", instance_of=None, filter_kind=None, **_kwargs
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            return {
                "results": [
                    {"concept_id": "#V#preflight_has_author"},
                ]
            }
        if filter_kind == ["type"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#scientific_paper",
                        "name": "Scientific Paper",
                        "similarity_score": 0.88,
                    },
                ]
            }
        if filter_kind == ["predicate"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#authored_by",
                        "name": "authored by",
                        "similarity_score": 0.82,
                    },
                ]
            }
        return {"results": []}

    def _fake_get_texts_for_concept(concept_id, predicate=None, limit=50):
        if predicate != "hasName":
            return []
        if concept_id == "#V#preflight_has_author":
            return [
                {
                    "text": "has author",
                    "lang": "en",
                    "context": {"name_type": "NL"},
                }
            ]
        return []

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        _fake_get_texts_for_concept,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    llm = _CapturingLLM(["ok"])
    result = orchestrator.run(
        prompt="Find predicate suggestions for this scientific paper.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        conversation_session_id="session-987-telemetry",
    )

    preflight_entries = [
        entry
        for entry in (result.aux_llm_calls or [])
        if entry.get("type") == "ontology_preflight"
    ]
    assert preflight_entries, "expected ontology preflight telemetry"
    telemetry = preflight_entries[0]

    assert telemetry.get("final_type_suggestions"), "expected final type suggestions"
    assert telemetry.get("final_predicate_suggestions"), "expected final predicates"
    assert telemetry.get("final_suggestion_paths"), "expected source path summary"
    assert "topic_keyword_similarity_search" in telemetry.get(
        "final_suggestion_paths", []
    )
    assert "preflight_predicate_registry" in telemetry.get("final_suggestion_paths", [])

    predicate_suggestions = telemetry.get("final_predicate_suggestions") or []
    assert any(
        item.get("source_path") == "preflight_predicate_registry"
        for item in predicate_suggestions
        if isinstance(item, dict)
    )


def test_preflight_session_memory_keeps_context_for_two_follow_up_turns(monkeypatch):
    """JVNAUTOSCI-987: keep relevant concept context across two follow-up turns."""

    def _fake_search_concepts(
        *, query="", instance_of=None, filter_kind=None, **_kwargs
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            return {"results": []}
        query_lower = str(query or "").lower()
        if filter_kind == ["type"] and "scientific" in query_lower:
            return {
                "results": [
                    {
                        "concept_id": "#V#scientific_paper",
                        "name": "Scientific Paper",
                        "similarity_score": 0.9,
                    },
                ]
            }
        if filter_kind == ["predicate"] and "scientific" in query_lower:
            return {
                "results": [
                    {
                        "concept_id": "#V#authored_by",
                        "name": "authored by",
                        "similarity_score": 0.86,
                    },
                ]
            }
        return {"results": []}

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda concept_id, predicate=None, limit=50: [],
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )
    llm = _CapturingLLM(["ok", "ok", "ok"])
    session_id = "session-987-followups"

    orchestrator.run(
        prompt="Analyse this scientific paper and extract author affiliations.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        conversation_session_id=session_id,
    )
    second = orchestrator.run(
        prompt="Continue with affiliations.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        conversation_session_id=session_id,
    )
    third = orchestrator.run(
        prompt="Continue and verify institutions.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        conversation_session_id=session_id,
    )

    def _extract_preflight_text(call_index: int) -> str | None:
        context_messages = llm.calls[call_index].get("context") or []
        for msg in context_messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str) and "ONTOLOGY PRE-FLIGHT" in content:
                return content
        return None

    second_preflight = _extract_preflight_text(1)
    third_preflight = _extract_preflight_text(2)

    assert second_preflight is not None, "expected preflight on first follow-up turn"
    assert third_preflight is not None, "expected preflight on second follow-up turn"
    assert "#V#scientific_paper" in second_preflight
    assert "#V#authored_by" in second_preflight
    assert "#V#scientific_paper" in third_preflight
    assert "#V#authored_by" in third_preflight

    second_telemetry = [
        entry
        for entry in (second.aux_llm_calls or [])
        if entry.get("type") == "ontology_preflight"
    ][0]
    third_telemetry = [
        entry
        for entry in (third.aux_llm_calls or [])
        if entry.get("type") == "ontology_preflight"
    ][0]
    assert second_telemetry.get("session_memory_reused") is True
    assert third_telemetry.get("session_memory_reused") is True
