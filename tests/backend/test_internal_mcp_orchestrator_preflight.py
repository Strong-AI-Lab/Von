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
        assert instance_of == "#V#conversation_preflight_predicate"
        assert filter_kind == ["predicate"]
        return {
            "results": [
                {"concept_id": "#V#author_predicate"},
                {"concept_id": "#V#affiliation_predicate"},
            ]
        }

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
