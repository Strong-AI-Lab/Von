"""Tests for the dedicated workflow retrieval surface.

Tests cover:
- authority-aligned workflow document materialisation;
- retrieval-backed indexing and search behaviour;
- registry integration (eager + lazy workflows);
- rebuild / invalidation behaviour and score normalisation.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from src.backend.services.workflow_capability_service import (
    BUILTIN_WORKFLOW_CAPABILITIES,
    WorkflowCapabilityIndex,
    _WORKFLOW_CAPABILITY_RETRIEVAL_WARM_QUERIES,
    get_workflow_capability_index_readiness_report,
    _workflow_id_to_name,
    build_workflow_capability_text,
    get_workflow_capability_index,
    invalidate_workflow_capability_index,
    prewarm_workflow_capability_index,
    reset_workflow_capability_index,
    run_workflow_capability_index_startup_check,
    search_workflow_capabilities,
)
from workflow_test_support import build_test_conversation_turn_registry


class _FakeWorkflowRetrievalBackend:
    _TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

    def __init__(self) -> None:
        self.docs_by_namespace: dict[str, dict[str, dict[str, Any]]] = {}
        self.queries: list[dict[str, Any]] = []
        self.reset_calls: list[str] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.persistence_dir: str | None = None
        self.runtime_embed_model: object | None = object()
        self.namespace_runtime_state_override: dict[str, Any] | None = None
        self.embedding_signature = {
            "schema_version": "rag_component_signature.v1",
            "kind": "embedder",
            "provider": "fake",
            "model": "fake-workflow-capability-embedder",
            "host": None,
        }

    def get_runtime_embed_model(self) -> object | None:
        return self.runtime_embed_model

    def reset_namespace(self, namespace: str) -> None:
        namespace_key = str(namespace)
        self.reset_calls.append(namespace_key)
        self.docs_by_namespace[namespace_key] = {}

    def upsert_documents(
        self,
        docs: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        *,
        namespace: str | None = None,
        allow_partial_failures: bool = True,
    ) -> tuple[int, int]:
        docs_list = list(docs)
        namespace_key = str(namespace or "")
        self.upsert_calls.append(
            {
                "namespace": namespace_key,
                "count": len(docs_list),
                "allow_partial_failures": allow_partial_failures,
            }
        )
        store = self.docs_by_namespace.setdefault(namespace_key, {})
        for doc in docs_list:
            store[str(doc["id"])] = {
                "id": str(doc["id"]),
                "text": str(doc.get("text") or ""),
                "metadata": dict(doc.get("metadata") or {}),
            }
        return (len(docs_list), 0)

    def get_namespace_runtime_state(self, namespace: str | None = None) -> dict[str, Any]:
        namespace_key = str(namespace or "")
        if self.namespace_runtime_state_override is not None:
            return {
                "namespace": namespace_key,
                **dict(self.namespace_runtime_state_override),
            }
        return {
            "namespace": namespace_key,
            "has_persisted_index": bool(self.docs_by_namespace.get(namespace_key)),
            "compatible": True,
            "status": "compatible",
            "detail": "Fake namespace is compatible.",
            "current_embedding_signature": dict(self.embedding_signature),
            "stored_embedding_signature": dict(self.embedding_signature),
        }

    def query(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        namespace: str | None = None,
        hybrid: bool = True,
        permissions_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        namespace_key = str(namespace or "")
        self.queries.append(
            {
                "query_text": query_text,
                "top_k": top_k,
                "namespace": namespace_key,
                "hybrid": hybrid,
                "permissions_context": dict(permissions_context or {}),
            }
        )
        query_tokens = set(self._TOKEN_RE.findall(str(query_text or "").lower()))
        scored_rows: list[dict[str, Any]] = []
        for doc in self.docs_by_namespace.get(namespace_key, {}).values():
            metadata = dict(doc.get("metadata") or {})
            requested_type = str(
                (permissions_context or {}).get("type") or ""
            ).strip()
            if requested_type and str(metadata.get("type") or "").strip() != requested_type:
                continue
            text_tokens = set(self._TOKEN_RE.findall(str(doc.get("text") or "").lower()))
            overlap = len(query_tokens & text_tokens)
            if overlap <= 0:
                continue
            scored_rows.append(
                {
                    "id": doc["id"],
                    "text": doc["text"],
                    "metadata": metadata,
                    "score": overlap / max(len(query_tokens), 1),
                }
            )
        scored_rows.sort(
            key=lambda row: (
                -float(row.get("score") or 0.0),
                str((row.get("metadata") or {}).get("workflow_id") or ""),
            )
        )
        return scored_rows[:top_k]


@pytest.fixture(autouse=True)
def _fake_retrieval_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeWorkflowRetrievalBackend:
    backend = _FakeWorkflowRetrievalBackend()
    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service._get_workflow_capability_rag_service",
        lambda: backend,
    )
    reset_workflow_capability_index()
    return backend


@pytest.fixture(autouse=True)
def _stub_authoritative_workflow_description_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep capability-index tests focused on indexing/ranking behaviour.

    These unit tests use synthetic registries whose `purpose` values stand in
    for already-resolved authoritative workflow descriptions. Make that explicit
    so the tests do not depend on live Vontology text relations.
    """

    monkeypatch.setattr(
        "src.backend.workflows.vontology_loader.resolve_workflow_description",
        lambda _workflow_id, **kwargs: (
            str(
                kwargs.get("registration_purpose")
                or kwargs.get("definition_purpose")
                or ""
            ).strip(),
            "text_relation:#V#hasDescription",
        ),
    )


def _enable_fake_backend_persistence(
    backend: _FakeWorkflowRetrievalBackend,
    tmp_path: Any,
) -> None:
    backend.persistence_dir = str(tmp_path)

    def _namespace_persist_dir(namespace: str) -> str:
        return str(tmp_path / "namespaces" / f"{namespace}_fake")

    backend._namespace_persist_dir = _namespace_persist_dir  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Index — build and search
# ---------------------------------------------------------------------------


class TestWorkflowCapabilityIndex:
    def test_empty_index_returns_no_results(self):
        index = WorkflowCapabilityIndex()
        results = index.search("hello")
        assert results == []

    def test_single_entry_matches_query(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow(
            "#V#test_workflow",
            "Tool calling pipeline for external API calls and data retrieval",
            metadata={"name": "Test Workflow", "source": "test"},
        )
        results = index.search("API calls and data retrieval")
        assert len(results) >= 1
        assert results[0].workflow_id == "#V#test_workflow"

    def test_multiple_entries_rank_by_relevance(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow(
            "#V#chat",
            "Simple conversational greetings and acknowledgements without tools",
        )
        index.index_workflow(
            "#V#tools",
            "Tool calling pipeline for external API calls data retrieval file operations",
        )
        index.index_workflow(
            "#V#narration",
            "Narrative generation and storytelling prose composition",
        )
        results = index.search("retrieve data from external API")
        assert len(results) >= 1
        # The tool workflow should be the top result.
        assert results[0].workflow_id == "#V#tools"

    def test_max_results_limits_output(self):
        index = WorkflowCapabilityIndex()
        for i in range(20):
            index.index_workflow(f"#V#wf_{i}", f"workflow {i} capability text")
        results = index.search("workflow capability", max_results=5)
        assert len(results) <= 5

    def test_min_score_filters_low_matches(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow("#V#match", "keyword aligned with query terms")
        index.index_workflow("#V#unrelated", "completely different topic about cooking")
        results = index.search("keyword aligned query", min_score=0.5)
        # The unrelated entry should be excluded or scored very low.
        for r in results:
            assert r.relevance_score >= 0.5

    def test_exclude_ids(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow("#V#a", "same text for testing exclusion")
        index.index_workflow("#V#b", "same text for testing exclusion")
        results = index.search("testing exclusion", exclude_ids={"#V#a"})
        ids = [r.workflow_id for r in results]
        assert "#V#a" not in ids
        assert "#V#b" in ids

    def test_score_normalisation_0_to_1(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow("#V#a", "alpha bravo charlie")
        index.index_workflow("#V#b", "delta echo foxtrot")
        results = index.search("alpha bravo")
        for r in results:
            assert 0.0 <= r.relevance_score <= 1.0

    def test_search_passes_full_query_text_to_retrieval_backend(
        self,
        _fake_retrieval_backend: _FakeWorkflowRetrievalBackend,
    ) -> None:
        index = WorkflowCapabilityIndex()
        index.index_workflow(
            "#V#meeting_invitation_testing_workflow",
            "Testing workflow for meeting invitation experiments.",
        )

        query_text = "Use Testing Workflows tools for experiments"
        index.search(query_text)

        assert _fake_retrieval_backend.queries
        assert _fake_retrieval_backend.queries[-1]["query_text"] == query_text
        assert _fake_retrieval_backend.queries[-1]["namespace"] == "workflow_capabilities"
        permissions_context = _fake_retrieval_backend.queries[-1]["permissions_context"]
        assert permissions_context["type"] == "workflow_capability"
        assert permissions_context["retrieval_candidate_limit"] >= 10

    def test_index_sync_resets_backend_namespace(
        self,
        _fake_retrieval_backend: _FakeWorkflowRetrievalBackend,
    ) -> None:
        index = WorkflowCapabilityIndex()
        index.index_workflow(
            "#V#tool_calling_workflow",
            "Tool workflow for grounded retrieval and relation lookups.",
        )

        assert _fake_retrieval_backend.reset_calls == ["workflow_capabilities"]

    def test_size_reflects_entries(self):
        index = WorkflowCapabilityIndex()
        assert index.size == 0
        index.index_workflow("#V#one", "first workflow")
        assert index.size == 1
        index.index_workflow("#V#two", "second workflow")
        assert index.size == 2
        # Re-indexing same ID replaces.
        index.index_workflow("#V#one", "updated first workflow")
        assert index.size == 2

    def test_to_discovery_dict_format(self):
        index = WorkflowCapabilityIndex()
        index.index_workflow(
            "#V#test_wf",
            "Description for test workflow",
            metadata={"name": "Test Wf"},
        )
        results = index.search("test workflow")
        assert len(results) >= 1
        d = results[0].to_discovery_dict()
        assert d["concept_id"] == "#V#test_wf"
        assert d["name"] == "Test Wf"
        assert "match_source" in d
        assert d["match_source"] == "capability_index"
        assert isinstance(d["relevance_score"], float)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestIndexFromRegistry:
    def test_indexes_conversation_turn_workflows_from_vontology_purpose(self):
        registry = build_test_conversation_turn_registry()
        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)
        assert count > 0
        results = index.search("tool calling pipeline")
        assert any(r.workflow_id == "#V#tool_calling_workflow" for r in results)

    def test_indexes_lazy_registrations(self):
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        registry = WorkflowRegistry()
        lazy = LazyWorkflowRegistration(
            workflow_id="#V#test_lazy_wf",
            purpose="Analyse research papers and extract key insights",
            source="vontology",
        )
        registry.register_lazy(lazy)
        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)
        assert count >= 1
        results = index.search("research papers key insights")
        assert any(r.workflow_id == "#V#test_lazy_wf" for r in results)

    def test_lazy_without_purpose_is_skipped(self):
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        registry = WorkflowRegistry()
        lazy = LazyWorkflowRegistration(
            workflow_id="#V#entity_resolution_workflow",
            source="vontology",
        )
        registry.register_lazy(lazy)
        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)
        assert count == 0
        results = index.search("entity resolution")
        assert all(r.workflow_id != "#V#entity_resolution_workflow" for r in results)

    def test_invalid_workflow_id_is_skipped_even_with_authoritative_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        malformed_workflow_id = (
            "#V#the_enrichment_workflow_isn_t_the_right_one_we_need_a_new_"
            "vontology_search_workflow_i_suspect_manually_retrieve_the_"
            "v_timothy_pistotti_concept_and_then_look_at_its_types_and_"
            "relations_in_particular_ones_about_supervision_workflow"
        )
        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id=malformed_workflow_id,
                purpose="Find supervision evidence in Vontology.",
                source="vontology",
            )
        )
        monkeypatch.setattr(
            "src.backend.workflows.vontology_loader.batch_fetch_workflow_routing_metadata",
            lambda workflow_ids: {
                malformed_workflow_id: {
                    "description_text": "Find supervision evidence in Vontology.",
                    "description_source": "text_relation:#V#hasDescription",
                }
            },
        )

        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)

        assert count == 0
        assert malformed_workflow_id not in index._entries

    def test_non_vontology_registration_is_skipped(self):
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import WorkflowRegistration
        from src.backend.workflows.engine import WorkflowDefinition, WorkflowStateSpec

        registry = WorkflowRegistry()
        definition = WorkflowDefinition(
            workflow_id="#V#built_in_workflow",
            initial_state="start",
            states={"start": WorkflowStateSpec(state_id="start", terminal=True)},
            termination_states=("start",),
            purpose="Built-in workflow purpose text",
        )
        registry.register(
            WorkflowRegistration(
                workflow_id="#V#built_in_workflow",
                definition=definition,
                purpose="Built-in workflow purpose text",
                source="built_in",
            )
        )

        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)

        assert count == 0
        assert index.search("workflow purpose text") == []

    def test_index_from_registry_includes_discovery_exemplars_in_capability_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="#V#workflow_repair_or_create_workflow",
                purpose="Repair existing workflows or create new ones from requests.",
                source="vontology",
            )
        )
        monkeypatch.setattr(
            "src.backend.workflows.vontology_loader.batch_fetch_workflow_routing_metadata",
            lambda workflow_ids: {
                "#V#workflow_repair_or_create_workflow": {
                    "description_text": (
                        "Repair existing workflows or create new ones from requests."
                    ),
                    "description_source": "text_relation:#V#hasDescription",
                    "discovery_exemplars": {
                        "schema_version": "workflow_discovery_exemplars.v1",
                        "keywords": ["workflow creation", "workflow repair"],
                        "examples": [
                            "Create a workflow from this description request."
                        ],
                    },
                    "discovery_exemplars_source": (
                        "text_relation:#V#hasWorkflowDiscoveryExemplarsJson"
                    ),
                }
            },
        )

        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)

        assert count == 1
        capability_text = index._entries[
            "#V#workflow_repair_or_create_workflow"
        ].text
        assert "Keywords: workflow creation, workflow repair" in capability_text
        assert (
            "Example requests: Create a workflow from this description request."
            in capability_text
        )

        results = index.search("Create a workflow from this description request.")
        assert results
        assert results[0].workflow_id == "#V#workflow_repair_or_create_workflow"

    def test_rebuild_replaces_stale_retrieval_docs(
        self,
    ) -> None:
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="#V#workflow_repair_or_create_workflow",
                purpose="Repair workflows from requests.",
                source="vontology",
            )
        )

        index = WorkflowCapabilityIndex()
        assert index.index_from_registry(registry) == 1
        assert index.search("repair workflows")

        empty_registry = WorkflowRegistry()
        assert index.index_from_registry(empty_registry) == 0
        assert index.search("repair workflows") == []

    def test_index_from_registry_uses_batched_routing_metadata(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="#V#workflow_repair_or_create_workflow",
                purpose="",
                source="vontology",
            )
        )
        monkeypatch.setattr(
            "src.backend.workflows.vontology_loader.batch_fetch_workflow_routing_metadata",
            lambda workflow_ids: {
                "#V#workflow_repair_or_create_workflow": {
                    "description_text": (
                        "Repair existing workflows or create new ones from requests."
                    ),
                    "description_source": "text_relation:#V#hasDescription",
                    "discovery_exemplars": {
                        "schema_version": "workflow_discovery_exemplars.v1",
                        "keywords": ["workflow creation", "workflow repair"],
                        "examples": [
                            "Create a workflow from this description request."
                        ],
                    },
                    "discovery_exemplars_source": (
                        "text_relation:#V#hasWorkflowDiscoveryExemplarsJson"
                    ),
                }
            },
        )
        monkeypatch.setattr(
            "src.backend.workflows.vontology_loader.resolve_workflow_description",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("batch metadata should avoid per-workflow description fetches")
            ),
        )
        monkeypatch.setattr(
            "src.backend.workflows.vontology_loader.resolve_workflow_discovery_exemplars",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("batch metadata should avoid per-workflow exemplar fetches")
            ),
        )

        index = WorkflowCapabilityIndex()
        count = index.index_from_registry(registry)

        assert count == 1
        results = index.search("Create a workflow from this description request.")
        assert results
        assert results[0].workflow_id == "#V#workflow_repair_or_create_workflow"

    def test_blocking_build_reuses_current_persisted_manifest_without_backend_reset(
        self,
        tmp_path: Any,
        _fake_retrieval_backend: _FakeWorkflowRetrievalBackend,
    ) -> None:
        import src.backend.services.workflow_capability_service as capability_service
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        _enable_fake_backend_persistence(_fake_retrieval_backend, tmp_path)
        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="#V#workflow_repair_or_create_workflow",
                purpose="Repair workflows from requests.",
                source="vontology",
            )
        )

        first_index = capability_service._perform_workflow_capability_index_build(
            mode="blocking",
            workflow_registry=registry,
        )
        assert first_index.size == 1
        assert _fake_retrieval_backend.reset_calls == ["workflow_capabilities"]
        assert len(_fake_retrieval_backend.upsert_calls) == 1

        reset_workflow_capability_index()

        second_index = capability_service._perform_workflow_capability_index_build(
            mode="blocking",
            workflow_registry=registry,
        )

        assert second_index.size == 1
        assert _fake_retrieval_backend.reset_calls == ["workflow_capabilities"]
        assert len(_fake_retrieval_backend.upsert_calls) == 1
        readiness = get_workflow_capability_index_readiness_report()
        assert readiness["last_manifest_status"] == "loaded"
        assert readiness["ready"] is True

    def test_blocking_build_rebuilds_when_authoritative_manifest_digest_changes(
        self,
        tmp_path: Any,
        _fake_retrieval_backend: _FakeWorkflowRetrievalBackend,
    ) -> None:
        import src.backend.services.workflow_capability_service as capability_service
        from src.backend.workflows import WorkflowRegistry
        from src.backend.workflows.workflow_registry import LazyWorkflowRegistration

        _enable_fake_backend_persistence(_fake_retrieval_backend, tmp_path)

        first_registry = WorkflowRegistry()
        first_registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="#V#workflow_repair_or_create_workflow",
                purpose="Repair workflows from requests.",
                source="vontology",
            )
        )
        capability_service._perform_workflow_capability_index_build(
            mode="blocking",
            workflow_registry=first_registry,
        )

        reset_workflow_capability_index()
        second_registry = WorkflowRegistry()
        second_registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="#V#workflow_repair_or_create_workflow",
                purpose="Repair or create workflows from current user requests.",
                source="vontology",
            )
        )

        second_index = capability_service._perform_workflow_capability_index_build(
            mode="blocking",
            workflow_registry=second_registry,
        )

        assert second_index.size == 1
        assert _fake_retrieval_backend.reset_calls == [
            "workflow_capabilities",
            "workflow_capabilities",
        ]
        assert len(_fake_retrieval_backend.upsert_calls) == 2
        readiness = get_workflow_capability_index_readiness_report()
        assert readiness["last_manifest_status"] == "written"


# ---------------------------------------------------------------------------
# Conversation-turn capabilities from authoritative purpose text
# ---------------------------------------------------------------------------


class TestPurposeDrivenCapabilities:
    def test_builtin_override_surface_is_empty(self):
        assert BUILTIN_WORKFLOW_CAPABILITIES == {}

    def test_chat_assistant_matches_greeting_queries(self):
        index = WorkflowCapabilityIndex()
        index.index_from_registry(build_test_conversation_turn_registry())
        results = index.search("hello how are you")
        assert len(results) >= 1
        top = results[0]
        assert top.workflow_id == "#V#chat_assistant_workflow"

    def test_tool_calling_matches_data_queries(self):
        index = WorkflowCapabilityIndex()
        index.index_from_registry(build_test_conversation_turn_registry())
        results = index.search("search arXiv for papers about transformers")
        assert len(results) >= 1
        top = results[0]
        assert top.workflow_id == "#V#tool_calling_workflow"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_workflow_id_to_name(self):
        assert _workflow_id_to_name("#V#tool_calling_workflow") == "Tool Calling Workflow"
        assert _workflow_id_to_name("some_workflow") == "Some Workflow"

    def test_build_capability_text_uses_purpose(self):
        text = build_workflow_capability_text(
            "#V#unknown_wf",
            purpose="Custom purpose text",
        )
        assert "Custom purpose text" in text

    def test_build_capability_text_prefers_description_when_present(self):
        text = build_workflow_capability_text(
            "#V#chat_assistant_workflow",
            description="Direct conversational response workflow.",
        )
        assert "Direct conversational response workflow." in text

    def test_build_capability_text_fallback(self):
        text = build_workflow_capability_text("#V#mystery_workflow")
        assert "Mystery Workflow" in text


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_returns_same_instance(self):
        reset_workflow_capability_index()
        idx1 = get_workflow_capability_index()
        idx2 = get_workflow_capability_index()
        assert idx1 is idx2

    def test_reset_creates_new_instance(self):
        reset_workflow_capability_index()
        idx1 = get_workflow_capability_index()
        reset_workflow_capability_index()
        idx2 = get_workflow_capability_index()
        assert idx1 is not idx2


def test_search_workflow_capabilities_populates_empty_index_from_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_workflow_capability_index()
    registry = build_test_conversation_turn_registry()

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        lambda defer_parity_work=False: registry,
    )

    results = search_workflow_capabilities(
        "general tool-calling workflows for external APIs",
        max_results=5,
        min_score=0.01,
    )

    assert any(
        result.workflow_id == "#V#tool_calling_workflow" for result in results
    )


def test_search_workflow_capabilities_non_blocking_triggers_background_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_workflow_capability_index()
    started: list[tuple[bool, object | None]] = []
    sentinel_registry = object()

    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service._start_background_workflow_capability_index_build",
        lambda *, force_refresh=False, workflow_registry=None: (
            started.append((bool(force_refresh), workflow_registry)) or True
        ),
    )

    results = search_workflow_capabilities(
        "general tool-calling workflows for external APIs",
        max_results=5,
        min_score=0.01,
        non_blocking=True,
        workflow_registry=sentinel_registry,
    )

    assert results == []
    assert started == [(False, sentinel_registry)]


def test_prewarm_workflow_capability_index_starts_background_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[bool, object | None]] = []
    sentinel_registry = object()

    monkeypatch.setattr(
        "src.backend.services.workflow_capability_service._start_background_workflow_capability_index_build",
        lambda *, force_refresh=False, workflow_registry=None: (
            observed.append((bool(force_refresh), workflow_registry)) or True
        ),
    )

    started = prewarm_workflow_capability_index(
        force_refresh=True,
        workflow_registry=sentinel_registry,
    )

    assert started is True
    assert observed == [(True, sentinel_registry)]


def test_blocking_build_runs_all_warm_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.services.workflow_capability_service as capability_service

    class _FakeIndex:
        def __init__(self) -> None:
            self.search_queries: list[str] = []

        def index_from_registry(self, registry: object) -> int:
            assert registry is sentinel_registry
            return 2

        def search(self, query: str, max_results: int = 1) -> list[object]:
            assert max_results == 1
            self.search_queries.append(query)
            return []

    sentinel_registry = object()
    fake_index = _FakeIndex()

    monkeypatch.setattr(
        capability_service,
        "get_workflow_capability_index",
        lambda: fake_index,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        lambda defer_parity_work=False: sentinel_registry,
    )

    result = capability_service._perform_workflow_capability_index_build(
        mode="blocking"
    )

    assert result is fake_index
    assert fake_index.search_queries == list(
        _WORKFLOW_CAPABILITY_RETRIEVAL_WARM_QUERIES
    )


def test_index_sync_trims_backend_document_metadata(
    _fake_retrieval_backend: _FakeWorkflowRetrievalBackend,
) -> None:
    index = WorkflowCapabilityIndex()

    index.index_workflow(
        "#V#test_workflow",
        "Capability text for metadata trimming.",
        metadata={
            "name": "Test Workflow",
            "source": "vontology",
            "description_source": "text_relation:#V#hasDescription",
            "purpose": "This field should stay in memory only and not reach the backend metadata payload.",
            "summary_text": "This should also stay out of backend metadata.",
        },
    )

    stored = _fake_retrieval_backend.docs_by_namespace["workflow_capabilities"][
        "workflow_capability:#V#test_workflow"
    ]
    assert stored["metadata"] == {
        "workflow_id": "#V#test_workflow",
        "name": "Test Workflow",
        "type": "workflow_capability",
        "source": "vontology",
        "description_source": "text_relation:#V#hasDescription",
    }


def test_startup_check_records_not_ready_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.services.workflow_capability_service as capability_service

    reset_workflow_capability_index()

    monkeypatch.setattr(
        capability_service,
        "ensure_workflow_capability_index_populated",
        lambda **_kwargs: capability_service.get_workflow_capability_index(),
    )

    report = run_workflow_capability_index_startup_check(timeout_seconds=0.01)

    assert report["success"] is False
    assert report["ready"] is False
    assert report["status"] == "not_ready"
    readiness = get_workflow_capability_index_readiness_report()
    assert readiness["status"] == "not_ready"
    assert readiness["startup_check"]["status"] == "not_ready"


def test_readiness_report_requires_query_surface_warmth() -> None:
    reset_workflow_capability_index()
    index = get_workflow_capability_index()
    index.index_workflow("#V#test_workflow", "Capability text for warm readiness test")

    readiness = get_workflow_capability_index_readiness_report()

    assert readiness["ready"] is False
    assert readiness["status"] == "warming"
    assert readiness["summary"] == "Workflow capability index query surface warming."


def test_startup_check_warms_query_surface_for_ready_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.services.workflow_capability_service as capability_service

    reset_workflow_capability_index()
    index = get_workflow_capability_index()
    index.index_workflow("#V#test_workflow", "Capability text for startup warm test")

    warm_calls: list[float | None] = []

    monkeypatch.setattr(
        capability_service,
        "ensure_workflow_capability_index_populated",
        lambda **_kwargs: capability_service.get_workflow_capability_index(),
    )

    def _fake_warm(index_arg: Any, *, timeout_seconds: float | None = None, mode: str = "warm") -> None:
        assert index_arg is index
        assert mode == "startup"
        warm_calls.append(timeout_seconds)
        capability_service._set_workflow_capability_query_surface_state(
            ready=True,
            error=None,
            warmed_monotonic=123.0,
        )

    monkeypatch.setattr(
        capability_service,
        "_warm_workflow_capability_query_surface",
        _fake_warm,
    )

    report = run_workflow_capability_index_startup_check(timeout_seconds=1.5)

    assert warm_calls
    assert report["success"] is True
    assert report["ready"] is True
    assert report["status"] == "ready"


def test_invalidate_workflow_capability_index_clears_cached_entries() -> None:
    reset_workflow_capability_index()
    index = get_workflow_capability_index()
    index.index_workflow("#V#test_workflow", "Capability text for invalidation test")

    result = invalidate_workflow_capability_index()

    assert result["success"] is True
    assert result["had_cached_entries"] is True
    assert get_workflow_capability_index().size == 0


def test_invalidate_workflow_capability_index_records_reason_and_backend_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.backend.services.workflow_capability_service as capability_service

    reset_workflow_capability_index()
    index = get_workflow_capability_index()
    index.index_workflow("#V#test_workflow", "Capability text for invalidation test")

    reset_calls: list[str] = []

    class _FakeRagService:
        def reset_namespace(self, namespace: str) -> None:
            reset_calls.append(str(namespace))

    monkeypatch.setattr(
        capability_service,
        "_get_workflow_capability_rag_service",
        lambda: _FakeRagService(),
    )

    result = invalidate_workflow_capability_index(
        reason="RAG embedder changed; rebuild required.",
        reset_backend_namespace=True,
    )
    readiness = get_workflow_capability_index_readiness_report()

    assert result["success"] is True
    assert result["backend_namespace_reset"] is True
    assert reset_calls == ["workflow_capabilities"]
    assert readiness["status"] == "rebuild_required"
    assert readiness["detail"] == "RAG embedder changed; rebuild required."


def test_readiness_report_starts_auto_rebuild_for_embedding_signature_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    _fake_retrieval_backend: _FakeWorkflowRetrievalBackend,
) -> None:
    import src.backend.services.workflow_capability_service as capability_service

    reset_workflow_capability_index()
    _fake_retrieval_backend.namespace_runtime_state_override = {
        "has_persisted_index": True,
        "compatible": False,
        "status": "embedding_signature_mismatch",
        "detail": "Persisted index embeddings were built with a different embedding signature.",
        "current_embedding_signature": dict(_fake_retrieval_backend.embedding_signature),
        "stored_embedding_signature": None,
    }
    started: list[dict[str, Any]] = []

    def _fake_start_background(
        *,
        force_refresh: bool = False,
        workflow_registry: object | None = None,
        mode: str = "background",
    ) -> bool:
        started.append(
            {
                "force_refresh": force_refresh,
                "workflow_registry": workflow_registry,
                "mode": mode,
            }
        )
        return True

    monkeypatch.setattr(
        capability_service,
        "_start_background_workflow_capability_index_build",
        _fake_start_background,
    )

    readiness = get_workflow_capability_index_readiness_report()

    assert started == [
        {
            "force_refresh": True,
            "workflow_registry": None,
            "mode": "auto_rebuild",
        }
    ]
    assert readiness["status"] == "rebuilding"
    assert readiness["summary"] == "Workflow capability index rebuilding."
    assert readiness["auto_rebuild"]["attempt_count"] == 1
    assert readiness["auto_rebuild"]["last_status"] == "started"
    assert readiness["auto_rebuild"]["started_this_report"] is True


def test_readiness_report_throttles_repeated_auto_rebuild_status_calls(
    monkeypatch: pytest.MonkeyPatch,
    _fake_retrieval_backend: _FakeWorkflowRetrievalBackend,
) -> None:
    import src.backend.services.workflow_capability_service as capability_service

    reset_workflow_capability_index()
    _fake_retrieval_backend.namespace_runtime_state_override = {
        "has_persisted_index": True,
        "compatible": False,
        "status": "embedding_signature_mismatch",
        "detail": "Persisted index embeddings were built with a different embedding signature.",
        "current_embedding_signature": dict(_fake_retrieval_backend.embedding_signature),
        "stored_embedding_signature": None,
    }
    started: list[str] = []

    monkeypatch.setattr(
        capability_service,
        "_start_background_workflow_capability_index_build",
        lambda *, force_refresh=False, workflow_registry=None, mode="background": (
            started.append(mode) or True
        ),
    )

    first = get_workflow_capability_index_readiness_report()
    second = get_workflow_capability_index_readiness_report()

    assert first["status"] == "rebuilding"
    assert second["status"] == "error"
    assert started == ["auto_rebuild"]
    assert second["auto_rebuild"]["attempt_count"] == 1
    assert second["auto_rebuild"]["last_status"] == "skipped"
    assert second["auto_rebuild"]["skipped_reason_this_report"] == "throttled"


def test_readiness_report_does_not_auto_rebuild_without_runtime_embedder(
    monkeypatch: pytest.MonkeyPatch,
    _fake_retrieval_backend: _FakeWorkflowRetrievalBackend,
) -> None:
    import src.backend.services.workflow_capability_service as capability_service

    reset_workflow_capability_index()
    _fake_retrieval_backend.runtime_embed_model = None
    _fake_retrieval_backend.namespace_runtime_state_override = {
        "has_persisted_index": True,
        "compatible": False,
        "status": "embedding_signature_mismatch",
        "detail": "Persisted index embeddings were built with a different embedding signature.",
        "current_embedding_signature": dict(_fake_retrieval_backend.embedding_signature),
        "stored_embedding_signature": None,
    }

    monkeypatch.setattr(
        capability_service,
        "_start_background_workflow_capability_index_build",
        lambda **_kwargs: pytest.fail("auto rebuild should not start"),
    )

    readiness = get_workflow_capability_index_readiness_report()

    assert readiness["status"] == "error"
    assert readiness["auto_rebuild"]["attempt_count"] == 0
    assert readiness["auto_rebuild"]["last_status"] == "skipped"
    assert (
        readiness["auto_rebuild"]["skipped_reason_this_report"]
        == "runtime_embedder_unavailable"
    )


def test_readiness_report_does_not_auto_rebuild_while_build_in_progress(
    monkeypatch: pytest.MonkeyPatch,
    _fake_retrieval_backend: _FakeWorkflowRetrievalBackend,
) -> None:
    import src.backend.services.workflow_capability_service as capability_service

    reset_workflow_capability_index()
    _fake_retrieval_backend.namespace_runtime_state_override = {
        "has_persisted_index": True,
        "compatible": False,
        "status": "embedding_signature_mismatch",
        "detail": "Persisted index embeddings were built with a different embedding signature.",
        "current_embedding_signature": dict(_fake_retrieval_backend.embedding_signature),
        "stored_embedding_signature": None,
    }
    capability_service._set_workflow_capability_rebuild_state(
        build_in_progress=True,
        mode="background",
        error=None,
    )

    monkeypatch.setattr(
        capability_service,
        "_start_background_workflow_capability_index_build",
        lambda **_kwargs: pytest.fail("auto rebuild should not start"),
    )

    readiness = get_workflow_capability_index_readiness_report()

    assert readiness["status"] == "building"
    assert readiness["auto_rebuild"]["attempt_count"] == 0
    assert readiness["auto_rebuild"]["last_status"] == "skipped"
    assert readiness["auto_rebuild"]["skipped_reason_this_report"] == "build_in_progress"
