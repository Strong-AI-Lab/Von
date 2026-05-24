from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    AuthoritativePromptUnavailableError,
    InternalMCPChatOrchestrator,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
)
from src.backend.workflows import WorkflowRegistry
from workflow_test_support import register_authoritative_test_workflows


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


def _extract_preflight_text_for_prompt(
    llm: _CapturingLLM,
    prompt: str,
) -> str | None:
    for call in llm.calls:
        if call.get("prompt") != prompt:
            continue
        context_messages = call.get("context") or []
        for msg in context_messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, str) and "ONTOLOGY PRE-FLIGHT" in content:
                return content
    return None


_TEST_BASE_PROMPT = (
    "You have access to internal MCP tools.\n\n"
    "{auth_status}\n"
    "INTERNAL EXECUTION GUARDRAILS:\n"
    "- Do NOT mention budgets, caps, or internal limits unless the user explicitly asks for diagnostics.\n"
    "Available tools:\n"
    "{listing}"
)

_TEST_BASE_PROMPT_WITH_GROUNDING_POLICY = (
    "You have access to internal MCP tools.\n\n"
    "{auth_status}\n"
    "INTERNAL EXECUTION GUARDRAILS:\n"
    "- Do NOT mention budgets, caps, or internal limits unless the user explicitly "
    "asks for diagnostics.\n"
    "GROUNDING GUARDRAILS:\n"
    "- list_papers is inventory-only: it enumerates cached/stored PDFs and does "
    "NOT by itself establish authorship, ownership, provenance, or any "
    "user/entity relationship.\n"
    "- Do NOT treat cache presence, file presence, storage inventory, or generic "
    "listing tools as sufficient evidence that an artefact belongs to, was "
    "authored by, or is otherwise related to a person or entity.\n"
    "Available tools:\n"
    "{listing}"
)


def _seed_authoritative_conversation_turn_registry(monkeypatch) -> None:
    def _build_registry(*, defer_parity_work: bool = True) -> WorkflowRegistry:
        assert defer_parity_work is True
        registry = WorkflowRegistry()
        register_authoritative_test_workflows(registry)
        return registry

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_shared_workflow_registry_read_only",
        _build_registry,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_shared_durable_action_registry",
        lambda: ActionRegistry(),
    )


@pytest.fixture(autouse=True)
def _stub_shared_runtime_registries(monkeypatch):
    _seed_authoritative_conversation_turn_registry(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.WorkflowSelector.enabled",
        lambda self: False,
    )


@pytest.fixture(autouse=True)
def _stub_authoritative_base_prompt(monkeypatch):
    monkeypatch.setattr(
        InternalMCPChatOrchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda self, preferred_language=None: (
            _TEST_BASE_PROMPT,
            "#V#test_base_prompt",
        ),
    )


def _stub_turn_current_request_prompt(
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: InternalMCPChatOrchestrator,
) -> None:
    def _render_authoritative_prompt(prompt_ids, **kwargs):
        requested_prompt_ids = [
            str(item).strip()
            for item in (prompt_ids or ())
            if isinstance(item, str) and str(item).strip()
        ]
        variables = dict(kwargs.get("variables") or {})
        turn_text = str(variables.get("turn_text") or "").strip()
        return SimpleNamespace(
            text=f"Current turn request:\n{turn_text}",
            prompt_id=requested_prompt_ids[0] if requested_prompt_ids else None,
            variables=variables,
            truncated=False,
        )

    monkeypatch.setattr(
        orchestrator,
        "_render_authoritative_prompt",
        _render_authoritative_prompt,
    )


def test_orchestrator_constructor_uses_shared_runtime_registries(monkeypatch):
    workflow_registry = WorkflowRegistry()
    durable_actions = ActionRegistry()
    durable_actions.register(
        ActionSpec(
            action_id="durable.test",
            handler=lambda request: WorkflowActionResult(outputs={"ok": True}),
            description="durable stub",
        )
    )
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_shared_workflow_registry_read_only",
        lambda *, defer_parity_work=True: calls.append(("workflow", defer_parity_work))
        or workflow_registry,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.get_shared_durable_action_registry",
        lambda: calls.append(("actions", None)) or durable_actions,
    )

    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, _CapturingGateway()))

    assert calls == [("workflow", True), ("actions", None)]
    assert orchestrator._workflow_registry is workflow_registry
    assert orchestrator._action_registry is not durable_actions
    assert orchestrator._action_registry.get("durable.test") is not None
    assert durable_actions.get("missing_tool_call.assess") is None
    assert orchestrator._action_registry.has_fallback_handler() is True


def test_instruction_message_preserves_authoritative_guardrail_wording_without_budget_leak(
    monkeypatch,
):
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._gateway = cast(Any, _CapturingGateway())
    orchestrator._max_tool_invocations = 30
    orchestrator._tool_batch_cap = 10
    orchestrator._last_base_system_prompt_telemetry = None

    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda preferred_language=None: (
            _TEST_BASE_PROMPT_WITH_GROUNDING_POLICY,
            "#V#test_base_prompt",
        ),
    )

    instruction = orchestrator._instruction_message(preferred_language="en")

    assert "Server limits:" not in instruction
    assert "INTERNAL EXECUTION GUARDRAILS:" in instruction
    assert "Do NOT mention budgets, caps, or internal limits" in instruction
    assert "inventory-only" in instruction
    assert "Do NOT treat cache presence" in instruction
    assert orchestrator._last_base_system_prompt_telemetry == {
        "type": "base_system_prompt",
        "source": "vontology",
        "prompt_type_id": "#V#von_chat_base_system_prompt",
        "prompt_concept_id": "#V#test_base_prompt",
    }


def test_instruction_message_does_not_inject_grounding_policy_after_authoritative_prompt(
    monkeypatch,
):
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._gateway = cast(Any, _CapturingGateway())
    orchestrator._max_tool_invocations = 30
    orchestrator._tool_batch_cap = 10
    orchestrator._last_base_system_prompt_telemetry = None

    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda preferred_language=None: (_TEST_BASE_PROMPT, "#V#test_base_prompt"),
    )

    instruction = orchestrator._instruction_message(preferred_language="en")

    assert "GROUNDING GUARDRAILS:" not in instruction
    assert "inventory-only" not in instruction
    assert "Do NOT treat cache presence" not in instruction


def test_instruction_message_keeps_tool_index_compact_for_large_catalogue(
    monkeypatch,
):
    class _LargeGateway(_CapturingGateway):
        def describe_methods(self):
            catalogue: dict[str, dict[str, Any]] = {}
            for index in range(160):
                catalogue[f"tool_{index:03d}"] = {
                    "category": "read",
                    "description": "Synthetic tool for prompt-size regression coverage.",
                    "input_schema": {
                        "required": [],
                        "optional": [],
                        "allow_unknown": False,
                        "description": None,
                    },
                    "output_schema": None,
                }
            return catalogue

    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._gateway = cast(Any, _LargeGateway())
    orchestrator._max_tool_invocations = 30
    orchestrator._tool_batch_cap = 10
    orchestrator._last_base_system_prompt_telemetry = None

    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda preferred_language=None: (_TEST_BASE_PROMPT, "#V#test_base_prompt"),
    )

    instruction = orchestrator._instruction_message(preferred_language="en")

    assert "Available tool names" in instruction
    assert "tool_000" in instruction
    assert "tool_159" in instruction
    assert len(instruction) < 12_000


def test_instruction_message_requires_authoritative_base_prompt(monkeypatch):
    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._gateway = cast(Any, _CapturingGateway())
    orchestrator._max_tool_invocations = 30
    orchestrator._tool_batch_cap = 10
    orchestrator._last_base_system_prompt_telemetry = None

    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda preferred_language=None: (None, None),
    )

    with pytest.raises(AuthoritativePromptUnavailableError):
        orchestrator._instruction_message(preferred_language="en")


def test_instruction_message_keeps_arxiv_family_labels_compact(monkeypatch):
    class _PaperGateway(_CapturingGateway):
        def describe_methods(self):
            return {
                "search_arxiv": {"description": "Search papers"},
                "download_paper": {"description": "Download paper"},
            }

    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    orchestrator._gateway = cast(Any, _PaperGateway())
    orchestrator._max_tool_invocations = 30
    orchestrator._tool_batch_cap = 10
    orchestrator._last_base_system_prompt_telemetry = None

    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda preferred_language=None: (_TEST_BASE_PROMPT, "#V#test_base_prompt"),
    )

    instruction = orchestrator._instruction_message(preferred_language="en")

    assert "search_arxiv" in instruction
    assert "- arxiv:" in instruction.lower()
    assert "download_paper" in instruction


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


def test_build_topic_vocabulary_query_text():
    """Test structural topic-query construction from conversation context."""
    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    # Test with prompt only
    query_text = orchestrator._build_topic_vocabulary_query_text(
        "Tell me about machine learning algorithms", None
    )
    assert isinstance(query_text, str)
    assert "machine learning algorithms" in query_text.lower()

    # Test with context
    context = [
        {"role": "user", "content": "I want to learn about neural networks"},
        {"role": "assistant", "content": "Sure, I can help with that."},
        {"role": "user", "content": "What about deep learning?"},
    ]
    query_text = orchestrator._build_topic_vocabulary_query_text(
        "How do transformers work?", context
    )
    assert isinstance(query_text, str)
    lowered_query = query_text.lower()
    assert "transformers" in lowered_query
    assert "neural networks" in lowered_query
    assert "deep learning" in lowered_query


def test_build_topic_vocabulary_query_text_filters_concept_ids():
    """Test that explicit concept IDs are filtered from the query text."""
    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )

    query_text = orchestrator._build_topic_vocabulary_query_text(
        "Create a concept #V#machine_learning_algorithm as type", None
    )
    assert isinstance(query_text, str)
    assert "#V#machine_learning_algorithm" not in query_text
    assert "Create a concept" in query_text


def test_discover_types_for_topic_context(monkeypatch):
    """Test type discovery based on structural topic query text."""
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

    types = orchestrator._discover_types_for_topic_context(
        "neural network learning", "en"
    )

    assert len(types) == 2
    assert types[0]["concept_id"] == "#V#neural_network"
    assert types[0]["name"] == "Neural Network"
    assert any(c["filter_kind"] == ["type"] for c in search_calls)
    assert search_calls[0]["query"] == "neural network learning"


def test_discover_predicates_for_topic_context(monkeypatch):
    """Test predicate discovery based on structural topic query text."""
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

    predicates = orchestrator._discover_predicates_for_topic_context(
        "neural network training", "en"
    )

    assert len(predicates) == 1
    assert predicates[0]["concept_id"] == "#V#has_training_data"
    assert search_calls[0]["query"] == "neural network training"


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

    query_text = "machine learning"

    # First discovery should hit the search
    types1 = orchestrator._discover_types_for_topic_context(query_text, "en")
    predicates1 = orchestrator._discover_predicates_for_topic_context(query_text, "en")

    # Second discovery with same query should use cache
    cache_key = orchestrator._get_topic_vocabulary_cache_key(query_text)

    # Manually populate cache to simulate what _build_ontology_preflight does
    import time

    orchestrator._topic_vocabulary_cache[cache_key] = {
        "timestamp": time.time(),
        "types": types1,
        "predicates": predicates1,
    }

    # Verify cache key generation is consistent
    cache_key2 = orchestrator._get_topic_vocabulary_cache_key(query_text)
    assert cache_key == cache_key2

    # Verify cache-key normalisation ignores case and repeated whitespace.
    cache_key3 = orchestrator._get_topic_vocabulary_cache_key("  MACHINE   learning  ")
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
    assert "topic_query_text" in telemetry
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
    assert "topic_context_similarity_search" in telemetry.get(
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

    second_preflight = _extract_preflight_text_for_prompt(
        llm, "Continue with affiliations."
    )
    third_preflight = _extract_preflight_text_for_prompt(
        llm, "Continue and verify institutions."
    )

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


def test_annotation_candidates_surface_in_preflight_telemetry_and_prompt(monkeypatch):
    """JVNAUTOSCI-991: annotation candidates should be visible in planning context."""

    def _fake_search_concepts(
        *, query="", instance_of=None, filter_kind=None, **_kwargs
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            return {"results": []}
        return {"results": []}

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda concept_id, predicate=None, limit=50: [],
    )

    def _fake_extract_annotations(
        text, use_llm=None, use_match=True, return_timings=False
    ):
        return [
            {
                "span": {
                    "start": 0,
                    "end": 10,
                    "text": "John Smith",
                    "type": "#V#person",
                },
                "suggested_type_id": "#V#person",
                "candidates": [
                    {"concept_id": "#V#john_smith", "name": "John Smith"},
                ],
            }
        ]

    monkeypatch.setattr(
        "src.backend.services.annotation_extraction_service.extract_annotations",
        _fake_extract_annotations,
    )

    def _fake_get_concept_by_concept_id(concept_id):
        if concept_id == "#V#john_smith":
            return {
                "concept_id": "#V#john_smith",
                "relationships": {
                    "is_an_instance_of": ["#V#person"],
                    "#V#has_affiliation": ["#V#auckland_university_of_technology"],
                },
            }
        return None

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        _fake_get_concept_by_concept_id,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )
    _stub_turn_current_request_prompt(monkeypatch, orchestrator)
    llm = _CapturingLLM(["ok"])

    result = orchestrator.run(
        prompt="John Smith authored this paper from AUT.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        conversation_session_id="session-991-annotation",
    )

    preflight_entries = [
        entry
        for entry in (result.aux_llm_calls or [])
        if entry.get("type") == "ontology_preflight"
    ]
    assert preflight_entries, "expected ontology preflight telemetry"
    telemetry = preflight_entries[0]

    assert telemetry.get("annotation_span_count") == 1
    assert "#V#john_smith" in (telemetry.get("annotation_seed_candidate_ids") or [])
    assert "#V#person" in (telemetry.get("annotation_suggested_type_ids") or [])
    assert "#V#has_affiliation" in (
        telemetry.get("annotation_region_predicate_ids") or []
    )
    assert "#V#auckland_university_of_technology" in (
        telemetry.get("annotation_region_related_concept_ids") or []
    )

    type_suggestions = telemetry.get("final_type_suggestions") or []
    predicate_suggestions = telemetry.get("final_predicate_suggestions") or []
    assert any(
        isinstance(item, dict)
        and item.get("concept_id") == "#V#person"
        and item.get("source_path") == "annotation_span_type_hint"
        for item in type_suggestions
    )
    assert any(
        isinstance(item, dict)
        and item.get("concept_id") == "#V#has_affiliation"
        and item.get("source_path") == "annotation_region_search"
        for item in predicate_suggestions
    )

    context_messages = llm.calls[0]["context"] or []
    preflight_text = next(
        (
            msg.get("content")
            for msg in context_messages
            if isinstance(msg, dict)
            and isinstance(msg.get("content"), str)
            and "ONTOLOGY PRE-FLIGHT" in msg["content"]
        ),
        None,
    )
    assert isinstance(preflight_text, str)
    assert "Annotation-derived candidate concepts" in preflight_text
    assert "#V#john_smith" in preflight_text


def test_annotation_candidates_reused_across_two_follow_up_turns(monkeypatch):
    """JVNAUTOSCI-991: session memory should retain annotation candidates."""

    def _fake_search_concepts(
        *, query="", instance_of=None, filter_kind=None, **_kwargs
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            return {"results": []}
        return {"results": []}

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda concept_id, predicate=None, limit=50: [],
    )

    def _fake_extract_annotations(
        text, use_llm=None, use_match=True, return_timings=False
    ):
        text_lower = str(text or "").lower()
        if "continue" in text_lower:
            return []
        return [
            {
                "span": {
                    "start": 0,
                    "end": 10,
                    "text": "John Smith",
                    "type": "#V#person",
                },
                "suggested_type_id": "#V#person",
                "candidates": [
                    {"concept_id": "#V#john_smith", "name": "John Smith"},
                ],
            }
        ]

    monkeypatch.setattr(
        "src.backend.services.annotation_extraction_service.extract_annotations",
        _fake_extract_annotations,
    )

    def _fake_get_concept_by_concept_id(concept_id):
        if concept_id == "#V#john_smith":
            return {
                "concept_id": "#V#john_smith",
                "relationships": {
                    "is_an_instance_of": ["#V#person"],
                    "#V#has_affiliation": ["#V#auckland_university_of_technology"],
                },
            }
        return None

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        _fake_get_concept_by_concept_id,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )
    llm = _CapturingLLM(["ok", "ok", "ok"])
    session_id = "session-991-followups"

    orchestrator.run(
        prompt="John Smith authored this paper from AUT.",
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

    second_preflight = _extract_preflight_text_for_prompt(
        llm, "Continue with affiliations."
    )
    third_preflight = _extract_preflight_text_for_prompt(
        llm, "Continue and verify institutions."
    )
    assert second_preflight is not None, "expected preflight on first follow-up turn"
    assert third_preflight is not None, "expected preflight on second follow-up turn"
    assert "#V#john_smith" in second_preflight
    assert "#V#john_smith" in third_preflight

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
    assert any(
        isinstance(item, dict) and item.get("concept_id") == "#V#john_smith"
        for item in (second_telemetry.get("session_memory_annotation_candidates") or [])
    )
    assert "#V#auckland_university_of_technology" in (
        second_telemetry.get("session_memory_related_concept_ids") or []
    )


def test_salient_predicates_by_type_surface_in_preflight(monkeypatch):
    """JVNAUTOSCI-992: preflight should expose salient predicates by relevant type."""

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
                        "similarity_score": 0.91,
                    }
                ]
            }
        if filter_kind == ["predicate"]:
            return {"results": []}
        return {"results": []}

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )

    def _fake_get_texts_for_concept(concept_id, predicate=None, limit=50):
        if predicate != "hasName":
            return []
        if concept_id == "#V#authored_by":
            return [
                {"text": "authored by", "lang": "en", "context": {"name_type": "NL"}}
            ]
        if concept_id == "#V#published_in":
            return [
                {"text": "published in", "lang": "en", "context": {"name_type": "NL"}}
            ]
        return []

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        _fake_get_texts_for_concept,
    )
    monkeypatch.setattr(
        "src.backend.services.annotation_extraction_service.extract_annotations",
        lambda text, use_llm=None, use_match=True, return_timings=False: [],
    )

    def _fake_get_concept_by_concept_id(concept_id):
        if concept_id == "#V#scientific_paper":
            return {
                "concept_id": "#V#scientific_paper",
                "name": "Scientific Paper",
                "relationships": {
                    "#V#salient_binary_predicate_for_type": [
                        "#V#authored_by",
                        "#V#published_in",
                    ]
                },
            }
        return None

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        _fake_get_concept_by_concept_id,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )
    llm = _CapturingLLM(["ok"])

    result = orchestrator.run(
        prompt="Analyse this scientific paper and suggest relevant predicates.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        conversation_session_id="session-992-salient",
    )

    preflight_entries = [
        entry
        for entry in (result.aux_llm_calls or [])
        if entry.get("type") == "ontology_preflight"
    ]
    assert preflight_entries, "expected ontology preflight telemetry"
    telemetry = preflight_entries[0]

    assert "#V#scientific_paper" in (telemetry.get("salient_type_candidate_ids") or [])
    salient_by_type = telemetry.get("salient_predicates_by_type") or []
    assert any(
        isinstance(group, dict)
        and group.get("type_concept_id") == "#V#scientific_paper"
        for group in salient_by_type
    )
    assert "#V#authored_by" in (telemetry.get("salient_predicate_ids") or [])

    predicate_suggestions = telemetry.get("final_predicate_suggestions") or []
    assert any(
        isinstance(item, dict)
        and item.get("concept_id") == "#V#authored_by"
        and item.get("source_path") == "salient_predicate_for_type"
        for item in predicate_suggestions
    )
    assert "salient_predicate_for_type" in (
        telemetry.get("final_suggestion_paths") or []
    )

    context_messages = llm.calls[0]["context"] or []
    preflight_text = next(
        (
            msg.get("content")
            for msg in context_messages
            if isinstance(msg, dict)
            and isinstance(msg.get("content"), str)
            and "ONTOLOGY PRE-FLIGHT" in msg["content"]
        ),
        None,
    )
    assert isinstance(preflight_text, str)
    assert "Salient predicates by relevant type" in preflight_text
    assert "#V#authored_by" in preflight_text


def test_salient_predicates_persist_for_two_follow_up_turns(monkeypatch):
    """JVNAUTOSCI-992: salient predicate suggestions should survive two follow-up turns."""

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
                        "similarity_score": 0.91,
                    }
                ]
            }
        if filter_kind == ["predicate"]:
            return {"results": []}
        return {"results": []}

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda concept_id, predicate=None, limit=50: (
            [{"text": "authored by", "lang": "en", "context": {"name_type": "NL"}}]
            if predicate == "hasName" and concept_id == "#V#authored_by"
            else []
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.annotation_extraction_service.extract_annotations",
        lambda text, use_llm=None, use_match=True, return_timings=False: [],
    )

    def _fake_get_concept_by_concept_id(concept_id):
        if concept_id == "#V#scientific_paper":
            return {
                "concept_id": "#V#scientific_paper",
                "name": "Scientific Paper",
                "relationships": {
                    "#V#salient_binary_predicate_for_type": ["#V#authored_by"]
                },
            }
        return None

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        _fake_get_concept_by_concept_id,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )
    llm = _CapturingLLM(["ok", "ok", "ok"])
    session_id = "session-992-followups"

    orchestrator.run(
        prompt="Analyse this scientific paper and suggest relevant predicates.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        conversation_session_id=session_id,
    )
    second = orchestrator.run(
        prompt="Continue with predicates.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        conversation_session_id=session_id,
    )
    third = orchestrator.run(
        prompt="Continue and verify relation choices.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        conversation_session_id=session_id,
    )

    second_preflight = _extract_preflight_text_for_prompt(
        llm, "Continue with predicates."
    )
    third_preflight = _extract_preflight_text_for_prompt(
        llm, "Continue and verify relation choices."
    )
    assert second_preflight is not None, "expected preflight on first follow-up turn"
    assert third_preflight is not None, "expected preflight on second follow-up turn"
    assert "#V#authored_by" in second_preflight
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
    assert any(
        isinstance(group, dict)
        and group.get("type_concept_id") == "#V#scientific_paper"
        for group in (
            second_telemetry.get("session_memory_salient_predicates_by_type") or []
        )
    )


def test_rag_candidates_surface_in_preflight_telemetry_and_prompt(monkeypatch):
    """JVNAUTOSCI-989: preflight should surface bounded RAG concept evidence."""

    def _fake_search_concepts(
        *, query="", instance_of=None, filter_kind=None, **_kwargs
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            return {"results": []}
        return {"results": []}

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        _fake_search_concepts,
    )
    monkeypatch.setattr(
        "src.backend.services.annotation_extraction_service.extract_annotations",
        lambda text, use_llm=None, use_match=True, return_timings=False: [],
    )

    def _fake_get_texts_for_concept(concept_id, predicate=None, limit=50):
        if predicate != "hasName":
            return []
        if concept_id == "#V#scientific_paper":
            return [
                {
                    "text": "Scientific Paper",
                    "lang": "en",
                    "context": {"name_type": "NL"},
                }
            ]
        if concept_id == "#V#authored_by":
            return [
                {"text": "authored by", "lang": "en", "context": {"name_type": "NL"}}
            ]
        return []

    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        _fake_get_texts_for_concept,
    )

    def _fake_get_concept_by_concept_id(concept_id):
        if concept_id == "#V#scientific_paper":
            return {
                "concept_id": "#V#scientific_paper",
                "name": "Scientific Paper",
                "relationships": {
                    "is_a_type_of": ["#V#document"],
                },
            }
        if concept_id == "#V#authored_by":
            return {
                "concept_id": "#V#authored_by",
                "name": "authored by",
                "relationships": {
                    "is_an_instance_of": ["#V#predicate"],
                },
            }
        return None

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        _fake_get_concept_by_concept_id,
    )

    rag_results = [
        {
            "id": "text_relation:rag-1",
            "score": 0.98,
            "text": (
                "Concept: #V#scientific_paper\n"
                "Predicate: hasDescription\n"
                "Language: en\n\n"
                "Peer-reviewed article structure used for scientific communication."
            ),
            "metadata": {
                "concept_id": "#V#scientific_paper",
                "predicate": "hasDescription",
            },
        },
        {
            "id": "text_relation:rag-2",
            "score": 0.96,
            "text": (
                "Concept: #V#authored_by\n"
                "Predicate: hasNote\n"
                "Language: en\n\n"
                "Relates a paper to its author identity."
            ),
            "metadata": {
                "concept_id": "#V#authored_by",
                "predicate": "hasNote",
            },
        },
    ]
    for idx in range(20):
        rag_results.append(
            {
                "id": f"text_relation:extra-{idx}",
                "score": max(0.0, 0.70 - (idx * 0.01)),
                "text": (
                    f"Concept: #V#candidate_{idx}\n"
                    "Predicate: hasDescription\n"
                    "Language: en\n\n"
                    f"Auxiliary concept evidence {idx}."
                ),
                "metadata": {
                    "concept_id": f"#V#candidate_{idx}",
                    "predicate": "hasDescription",
                },
            }
        )

    class _RagGateway(_CapturingGateway):
        def describe_methods(self):
            return {"search_knowledge_base": {"name": "search_knowledge_base"}}

        def invoke(self, tool_name, payload=None):
            self.invocations.append({"tool": tool_name, "payload": payload})
            if tool_name == "search_knowledge_base":
                return _StubResult(
                    {
                        "success": True,
                        "results": rag_results,
                        "count": len(rag_results),
                    }
                )
            return _StubResult({"ok": True})

    gateway = cast(Any, _RagGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )
    _stub_turn_current_request_prompt(monkeypatch, orchestrator)
    llm = _CapturingLLM(["ok"])

    result = orchestrator.run(
        prompt="Find author-affiliation concepts for this scientific paper.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
        user_namespace="#V#michael_witbrock",
        conversation_session_id="session-989-rag",
    )

    rag_calls = [
        call
        for call in gateway.invocations
        if call.get("tool") == "search_knowledge_base"
    ]
    assert rag_calls, "expected preflight to invoke RAG search"
    rag_payload = rag_calls[0].get("payload") or {}
    assert rag_payload.get("top_k") == orchestrator._RAG_PREFLIGHT_TOP_K
    assert rag_payload.get("mode") == "concepts"
    assert rag_payload.get("predicates") == ["hasDescription", "hasNote"]

    preflight_entries = [
        entry
        for entry in (result.aux_llm_calls or [])
        if entry.get("type") == "ontology_preflight"
    ]
    assert preflight_entries, "expected ontology preflight telemetry"
    telemetry = preflight_entries[0]

    assert telemetry.get("rag_invoked") is True
    assert telemetry.get("rag_used") is True
    assert telemetry.get("rag_namespace_present") is True
    assert isinstance(telemetry.get("rag_query"), str)
    assert telemetry.get("rag_selected_concept_id") == "#V#scientific_paper"
    assert "#V#scientific_paper" in (telemetry.get("rag_selected_concept_ids") or [])
    assert (
        len(telemetry.get("rag_candidates") or [])
        == orchestrator._RAG_PREFLIGHT_MAX_CANDIDATES
    )
    assert "rag_concept_text_search" in (telemetry.get("final_suggestion_paths") or [])

    rag_candidates = telemetry.get("rag_candidates") or []
    assert any(
        isinstance(item, dict)
        and item.get("concept_id") == "#V#scientific_paper"
        and "Peer-reviewed article" in str(item.get("evidence_snippet") or "")
        for item in rag_candidates
    )

    type_suggestions = telemetry.get("final_type_suggestions") or []
    predicate_suggestions = telemetry.get("final_predicate_suggestions") or []
    assert any(
        isinstance(item, dict)
        and item.get("concept_id") == "#V#scientific_paper"
        and item.get("source_path") == "rag_concept_text_search"
        for item in type_suggestions
    )
    assert any(
        isinstance(item, dict)
        and item.get("concept_id") == "#V#authored_by"
        and item.get("source_path") == "rag_concept_text_search"
        for item in predicate_suggestions
    )

    if llm.calls:
        context_messages = llm.calls[0]["context"] or []
        preflight_text = next(
            (
                msg.get("content")
                for msg in context_messages
                if isinstance(msg, dict)
                and isinstance(msg.get("content"), str)
                and "ONTOLOGY PRE-FLIGHT" in msg["content"]
            ),
            None,
        )
        assert isinstance(preflight_text, str)
        assert "RAG-assisted concept candidates" in preflight_text
        assert "Peer-reviewed article structure" in preflight_text


def test_rag_preflight_skips_search_without_namespace(monkeypatch):
    """JVNAUTOSCI-989: RAG discovery should fail closed without a namespace."""

    monkeypatch.setattr(
        "src.backend.services.concept_search_service.search_concepts",
        lambda **_kwargs: {"results": []},
    )
    monkeypatch.setattr(
        "src.backend.services.annotation_extraction_service.extract_annotations",
        lambda text, use_llm=None, use_match=True, return_timings=False: [],
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda concept_id, predicate=None, limit=50: [],
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )
    llm = _CapturingLLM(["ok"])

    result = orchestrator.run(
        prompt="Find concept IDs related to this paper topic and #V#person.",
        context=[],
        llm_client=llm,
        model=None,
        preferred_language="en",
    )

    assert not any(
        call.get("tool") == "search_knowledge_base" for call in gateway.invocations
    )

    preflight_entries = [
        entry
        for entry in (result.aux_llm_calls or [])
        if entry.get("type") == "ontology_preflight"
    ]
    assert preflight_entries, "expected ontology preflight telemetry"
    telemetry = preflight_entries[0]
    assert telemetry.get("rag_invoked") is False
    assert telemetry.get("rag_used") is False
    assert "rag_namespace_missing" in (telemetry.get("rag_preflight_errors") or [])


def test_specialised_preflight_shadow_mode_reports_evaluation_without_applying(
    monkeypatch,
):
    """JVNAUTOSCI-990: shadow mode should evaluate but not apply suggestions."""

    _seed_authoritative_conversation_turn_registry(monkeypatch)
    monkeypatch.setenv("VON_MCP_SPECIALISED_PREFLIGHT_MODE", "shadow")
    monkeypatch.setattr(
        InternalMCPChatOrchestrator,
        "_build_topic_vocabulary_query_text",
        lambda self, prompt, context, max_recent_messages=5, max_chars=320: None,
    )
    monkeypatch.setattr(
        "src.backend.services.annotation_extraction_service.extract_annotations",
        lambda text, use_llm=None, use_match=True, return_timings=False: [],
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda concept_id, predicate=None, limit=50: [],
    )

    def _fake_search_concepts(
        *,
        query="",
        instance_of=None,
        filter_kind=None,
        match_type=None,
        min_similarity=None,
        limit=None,
        **_kwargs,
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            return {"results": []}
        if filter_kind == ["type"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#scientific_paper",
                        "name": "Scientific Paper",
                        "similarity_score": 0.84,
                    }
                ]
            }
        if filter_kind == ["predicate"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#authored_by",
                        "name": "authored by",
                        "similarity_score": 0.82,
                    }
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
    llm = _CapturingLLM(["ok"])

    result = orchestrator.run(
        prompt="Use #V#person as context for this relation mapping.",
        context=[],
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

    assert telemetry.get("specialised_preflight_mode") == "shadow"
    assert telemetry.get("specialised_preflight_workflow_invoked") is True
    assert telemetry.get("specialised_preflight_workflow_available") is True
    assert telemetry.get("specialised_preflight_recommendation") == "go_active_trial"
    assert telemetry.get("specialised_preflight_applied_type_suggestions") == []
    assert telemetry.get("specialised_preflight_applied_predicate_suggestions") == []

    raw_types = telemetry.get("specialised_preflight_raw_type_suggestions") or []
    raw_predicates = (
        telemetry.get("specialised_preflight_raw_predicate_suggestions") or []
    )
    assert any(
        isinstance(item, dict) and item.get("concept_id") == "#V#scientific_paper"
        for item in raw_types
    )
    assert any(
        isinstance(item, dict) and item.get("concept_id") == "#V#authored_by"
        for item in raw_predicates
    )
    assert "specialised_preflight_workflow" not in (
        telemetry.get("final_suggestion_paths") or []
    )


def test_specialised_preflight_active_mode_applies_fallback_suggestions(monkeypatch):
    """JVNAUTOSCI-990: active mode should merge specialised fallback suggestions."""

    _seed_authoritative_conversation_turn_registry(monkeypatch)
    monkeypatch.setenv("VON_MCP_SPECIALISED_PREFLIGHT_MODE", "active")
    monkeypatch.setattr(
        InternalMCPChatOrchestrator,
        "_build_topic_vocabulary_query_text",
        lambda self, prompt, context, max_recent_messages=5, max_chars=320: None,
    )
    monkeypatch.setattr(
        "src.backend.services.annotation_extraction_service.extract_annotations",
        lambda text, use_llm=None, use_match=True, return_timings=False: [],
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda concept_id, predicate=None, limit=50: [],
    )

    def _fake_search_concepts(
        *,
        query="",
        instance_of=None,
        filter_kind=None,
        match_type=None,
        min_similarity=None,
        limit=None,
        **_kwargs,
    ):
        if instance_of == "#V#conversation_preflight_predicate":
            return {"results": []}
        if filter_kind == ["type"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#scientific_paper",
                        "name": "Scientific Paper",
                        "similarity_score": 0.85,
                    }
                ]
            }
        if filter_kind == ["predicate"]:
            return {
                "results": [
                    {
                        "concept_id": "#V#authored_by",
                        "name": "authored by",
                        "similarity_score": 0.83,
                    }
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
    llm = _CapturingLLM(["ok"])

    result = orchestrator.run(
        prompt="Use #V#person as context for this relation mapping.",
        context=[],
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

    assert telemetry.get("specialised_preflight_mode") == "active"
    assert telemetry.get("specialised_preflight_workflow_invoked") is True
    assert telemetry.get("specialised_preflight_recommendation") == "go_adopted"

    applied_types = (
        telemetry.get("specialised_preflight_applied_type_suggestions") or []
    )
    applied_predicates = (
        telemetry.get("specialised_preflight_applied_predicate_suggestions") or []
    )
    assert any(
        isinstance(item, dict) and item.get("concept_id") == "#V#scientific_paper"
        for item in applied_types
    )
    assert any(
        isinstance(item, dict) and item.get("concept_id") == "#V#authored_by"
        for item in applied_predicates
    )
    assert "specialised_preflight_workflow" in (
        telemetry.get("final_suggestion_paths") or []
    )

    context_messages = llm.calls[0]["context"] or []
    preflight_text = next(
        (
            msg.get("content")
            for msg in context_messages
            if isinstance(msg, dict)
            and isinstance(msg.get("content"), str)
            and "ONTOLOGY PRE-FLIGHT" in msg["content"]
        ),
        None,
    )
    assert isinstance(preflight_text, str)
    assert "Specialised workflow fallback candidates" in preflight_text


def test_preflight_stage_authorities_classify_mechanical_stages_correctly(
    monkeypatch,
):
    """JVNAUTOSCI-1629: ontology preflight should have no prompt-semantic stage.

    The top-level decision_source should remain composite_preflight, and the
    turn-context query construction stage should be classified as structural
    context construction rather than prompt-semantic inference.
    """

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
    _stub_turn_current_request_prompt(monkeypatch, orchestrator)

    llm = _CapturingLLM(["ok"])
    result = orchestrator.run(
        prompt="Tell me about machine learning papers",
        context=[
            {"role": "user", "content": "I'm interested in deep learning"},
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

    # Top-level decision_source must be composite, not prompt_semantic_inference.
    assert telemetry["decision_source"] == "composite_preflight"

    # This preflight path no longer relies on prompt-semantic inference.
    assert telemetry["possible_inappropriate_python_code_use"] is False

    # Per-stage sub-annotations must be present.
    stage_authorities = telemetry.get("preflight_stage_authorities")
    assert isinstance(stage_authorities, list)
    assert len(stage_authorities) > 0

    stages_by_name = {item["stage"]: item for item in stage_authorities}

    # Mechanical stages must NOT be prompt_semantic_inference.
    mechanical_stages = {
        "explicit_id_extraction",
        "predicate_registry_load",
        "topic_context_query_construction",
        "topic_type_discovery",
        "topic_predicate_discovery",
        "annotation_extraction",
        "annotation_region_expansion",
        "rag_concept_discovery",
        "salient_predicate_aggregation",
        "session_memory_carry_over",
    }
    for stage_name in mechanical_stages:
        if stage_name in stages_by_name:
            assert (
                stages_by_name[stage_name]["decision_source"]
                != "prompt_semantic_inference"
            ), f"Stage {stage_name} should not be classified as prompt_semantic_inference"

    assert "topic_context_query_construction" in stages_by_name
    assert (
        stages_by_name["topic_context_query_construction"]["decision_source"]
        == "turn_context_query_construction"
    )

    assert not any(
        item.get("decision_source") == "prompt_semantic_inference"
        for item in stage_authorities
        if isinstance(item, dict)
    )

    # Each sub-stage entry must have required fields.
    for item in stage_authorities:
        assert "stage" in item
        assert "decision_source" in item
        assert "changed_outcome" in item


def test_run_derives_identity_components_from_namespace_for_model_selection(
    monkeypatch,
) -> None:
    captured_model_contexts: list[dict[str, Any]] = []

    def _capture_stage_model(
        self,
        *,
        stage,
        default_model,
        policy_state,
        registry_snapshot,
        user_concept_id,
        org_concept_id,
        **_kwargs,
    ):
        captured_model_contexts.append(
            {
                "stage": stage,
                "user_concept_id": user_concept_id,
                "org_concept_id": org_concept_id,
            }
        )
        return default_model

    monkeypatch.setattr(
        InternalMCPChatOrchestrator,
        "_select_model_for_stage",
        _capture_stage_model,
    )

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway, max_tool_invocations=1, max_context_chars=80_000
    )
    _stub_turn_current_request_prompt(monkeypatch, orchestrator)

    llm = _CapturingLLM(["ok"])
    result = orchestrator.run(
        prompt="Hello",
        context=[],
        llm_client=llm,
        model="gpt-5.4-nano",
        user_namespace="#V#user_alpha@org_beta",
    )

    assert result.response_text == "ok"
    assert captured_model_contexts, "expected stage model selection to run"
    assert any(
        entry["user_concept_id"] == "#V#user_alpha"
        and entry["org_concept_id"] == "#V#org_beta"
        for entry in captured_model_contexts
    )
    assert not any(
        entry["user_concept_id"] == "#V#user_alpha@org_beta"
        for entry in captured_model_contexts
    )
