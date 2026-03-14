"""Tests for lazy workflow registration — JVNAUTOSCI-1424 Phase 1.

Validates:
- LazyWorkflowRegistration dataclass.
- WorkflowRegistry lazy registration, resolution, promotion, thread safety.
- Interaction between lazy and eager registrations.
"""

from __future__ import annotations

import threading

import pytest

from src.backend.workflows.engine import WorkflowDefinition, WorkflowStateSpec
from src.backend.workflows.workflow_registry import (
    LazyWorkflowRegistration,
    WorkflowRegistration,
    WorkflowRegistry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_definition(workflow_id: str, purpose: str = "test") -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="start",
        states={"start": WorkflowStateSpec(state_id="start", terminal=True)},
        termination_states=("start",),
        purpose=purpose,
    )


def _make_registration(
    workflow_id: str,
    *,
    source: str = "built_in",
    purpose: str = "test",
) -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=workflow_id,
        definition=_make_definition(workflow_id, purpose),
        purpose=purpose,
        source=source,
    )


def _make_loader(definitions: dict[str, WorkflowDefinition]):
    """Return a loader callback backed by a dict of definitions."""
    call_log: list[str] = []

    def loader(workflow_id: str) -> WorkflowDefinition | None:
        call_log.append(workflow_id)
        return definitions.get(workflow_id)

    loader.call_log = call_log  # type: ignore[attr-defined]
    return loader


# ---------------------------------------------------------------------------
# LazyWorkflowRegistration dataclass
# ---------------------------------------------------------------------------


class TestLazyWorkflowRegistration:
    def test_fields(self):
        lazy = LazyWorkflowRegistration(
            workflow_id="wf_test",
            purpose="Test",
            source="vontology",
        )
        assert lazy.workflow_id == "wf_test"
        assert lazy.purpose == "Test"
        assert lazy.source == "vontology"
        assert lazy._resolved is None


# ---------------------------------------------------------------------------
# Lazy registration
# ---------------------------------------------------------------------------


class TestRegisterLazy:
    def test_register_lazy_no_conflict(self):
        registry = WorkflowRegistry()
        lazy = LazyWorkflowRegistration(workflow_id="wf_lazy", source="vontology")
        assert registry.register_lazy(lazy) is True
        assert registry.has("wf_lazy")
        assert "wf_lazy" in list(registry.all_workflow_ids())

    def test_register_lazy_skipped_when_eager_exists(self):
        registry = WorkflowRegistry()
        registry.register(_make_registration("wf_eager"))
        lazy = LazyWorkflowRegistration(workflow_id="wf_eager", source="vontology")
        assert registry.register_lazy(lazy) is False

    def test_lazy_does_not_appear_in_eager_ids(self):
        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_lazy", source="vontology"),
        )
        assert "wf_lazy" not in list(registry.eager_workflow_ids())
        assert "wf_lazy" in list(registry.lazy_workflow_ids())

    def test_lazy_registration_count(self):
        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_a", source="vontology"),
        )
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_b", source="vontology"),
        )
        assert registry.lazy_registration_count() == 2


# ---------------------------------------------------------------------------
# Lazy resolution
# ---------------------------------------------------------------------------


class TestLazyResolution:
    def test_get_triggers_lazy_load(self):
        defn = _make_definition("wf_lazy", purpose="loaded")
        loader = _make_loader({"wf_lazy": defn})
        registry = WorkflowRegistry(definition_loader=loader)
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_lazy", source="vontology"),
        )

        result = registry.get("wf_lazy")
        assert result is not None
        assert result.workflow_id == "wf_lazy"
        assert loader.call_log == ["wf_lazy"]

    def test_get_registration_triggers_lazy_load(self):
        defn = _make_definition("wf_lazy")
        loader = _make_loader({"wf_lazy": defn})
        registry = WorkflowRegistry(definition_loader=loader)
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="wf_lazy", purpose="placeholder", source="vontology",
            ),
        )

        reg = registry.get_registration("wf_lazy")
        assert reg is not None
        assert reg.source == "vontology"
        assert reg.purpose == "placeholder"  # Lazy purpose preserved

    def test_resolved_workflow_promoted_to_eager(self):
        defn = _make_definition("wf_lazy")
        loader = _make_loader({"wf_lazy": defn})
        registry = WorkflowRegistry(definition_loader=loader)
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_lazy", source="vontology"),
        )

        registry.get("wf_lazy")

        assert "wf_lazy" in list(registry.eager_workflow_ids())
        assert registry.lazy_registration_count() == 0

    def test_second_get_does_not_call_loader_again(self):
        defn = _make_definition("wf_lazy")
        loader = _make_loader({"wf_lazy": defn})
        registry = WorkflowRegistry(definition_loader=loader)
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_lazy", source="vontology"),
        )

        registry.get("wf_lazy")
        registry.get("wf_lazy")

        assert loader.call_log == ["wf_lazy"]

    def test_get_returns_none_when_loader_returns_none(self):
        loader = _make_loader({})
        registry = WorkflowRegistry(definition_loader=loader)
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_missing", source="vontology"),
        )

        assert registry.get("wf_missing") is None
        assert loader.call_log == ["wf_missing"]

    def test_get_returns_none_when_no_loader_set(self):
        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_lazy", source="vontology"),
        )

        assert registry.get("wf_lazy") is None

    def test_loader_exception_handled_gracefully(self):
        def bad_loader(wf_id: str):
            raise RuntimeError("DB connection failed")

        registry = WorkflowRegistry(definition_loader=bad_loader)
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_bad", source="vontology"),
        )

        assert registry.get("wf_bad") is None
        assert registry.has("wf_bad")  # Lazy entry still exists

    def test_lazy_purpose_preserved_over_definition_purpose(self):
        defn = _make_definition("wf_lazy", purpose="from_definition")
        loader = _make_loader({"wf_lazy": defn})
        registry = WorkflowRegistry(definition_loader=loader)
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="wf_lazy",
                purpose="from_discovery",
                source="vontology",
            ),
        )

        reg = registry.get_registration("wf_lazy")
        assert reg is not None
        assert reg.purpose == "from_discovery"

    def test_definition_purpose_used_when_lazy_purpose_is_none(self):
        defn = _make_definition("wf_lazy", purpose="from_definition")
        loader = _make_loader({"wf_lazy": defn})
        registry = WorkflowRegistry(definition_loader=loader)
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id="wf_lazy",
                purpose=None,
                source="vontology",
            ),
        )

        reg = registry.get_registration("wf_lazy")
        assert reg is not None
        assert reg.purpose == "from_definition"


# ---------------------------------------------------------------------------
# Eager registration evicts lazy
# ---------------------------------------------------------------------------


class TestEagerEvictsLazy:
    def test_register_evicts_lazy(self):
        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_test", source="vontology"),
        )
        assert registry.lazy_registration_count() == 1

        registry.register(_make_registration("wf_test"))
        assert registry.lazy_registration_count() == 0
        assert "wf_test" in list(registry.eager_workflow_ids())

    def test_register_or_replace_evicts_lazy(self):
        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_test", source="vontology"),
        )
        registry.register_or_replace(_make_registration("wf_test"))
        assert registry.lazy_registration_count() == 0

    def test_register_if_absent_evicts_lazy(self):
        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_test", source="vontology"),
        )
        # register_if_absent should succeed because lazy is not eager
        result = registry.register_if_absent(_make_registration("wf_test"))
        assert result is True
        assert registry.lazy_registration_count() == 0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_lazy_resolution(self):
        """Multiple threads resolving the same lazy entry get consistent results."""
        defn = _make_definition("wf_concurrent")
        call_count = 0
        call_lock = threading.Lock()

        def counting_loader(wf_id: str) -> WorkflowDefinition | None:
            nonlocal call_count
            with call_lock:
                call_count += 1
            return defn

        registry = WorkflowRegistry(definition_loader=counting_loader)
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_concurrent", source="vontology"),
        )

        results: list[WorkflowDefinition | None] = [None] * 8

        def resolve(idx: int):
            results[idx] = registry.get("wf_concurrent")

        threads = [threading.Thread(target=resolve, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should get the same definition.
        for r in results:
            assert r is not None
            assert r.workflow_id == "wf_concurrent"

        # Loader should be called exactly once (lock ensures single resolution).
        assert call_count == 1


# ---------------------------------------------------------------------------
# set_definition_loader
# ---------------------------------------------------------------------------


class TestSetDefinitionLoader:
    def test_replace_loader_after_init(self):
        registry = WorkflowRegistry()
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_test", source="vontology"),
        )

        # No loader — get returns None.
        assert registry.get("wf_test") is None

        # Attach loader.
        defn = _make_definition("wf_test")
        registry.set_definition_loader(lambda wf_id: defn if wf_id == "wf_test" else None)

        # Now lazy resolution works.
        assert registry.get("wf_test") is not None


# ---------------------------------------------------------------------------
# all_workflow_ids
# ---------------------------------------------------------------------------


class TestAllWorkflowIds:
    def test_includes_both_eager_and_lazy(self):
        registry = WorkflowRegistry()
        registry.register(_make_registration("wf_eager"))
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_lazy", source="vontology"),
        )

        ids = sorted(registry.all_workflow_ids())
        assert ids == ["wf_eager", "wf_lazy"]

    def test_deduplicated_after_resolution(self):
        defn = _make_definition("wf_lazy")
        loader = _make_loader({"wf_lazy": defn})
        registry = WorkflowRegistry(definition_loader=loader)
        registry.register_lazy(
            LazyWorkflowRegistration(workflow_id="wf_lazy", source="vontology"),
        )

        registry.get("wf_lazy")  # Resolve (promotes to eager).

        ids = list(registry.all_workflow_ids())
        assert ids.count("wf_lazy") == 1
