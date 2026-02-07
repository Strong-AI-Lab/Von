"""Backend test configuration.

Shared fixtures and env-var defaults for the backend test suite.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _disable_workflow_selector_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the workflow selector to OFF for backend tests.

    JVNAUTOSCI-825 changed the runtime default from OFF to ON.  Most existing
    orchestrator tests were written without accounting for the selector's extra
    LLM call, so they break when it fires unexpectedly.

    Tests that explicitly need the selector (e.g.,
    test_orchestrator_workflow_selector_routing.py) override this via their own
    ``monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "1")``.
    """
    monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "0")
