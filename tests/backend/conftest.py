"""Backend test configuration.

Shared fixtures and env-var defaults for the backend test suite.
"""

import os
import sys
from pathlib import Path

import pytest

_BACKEND_TESTS_DIR = Path(__file__).resolve().parent
if str(_BACKEND_TESTS_DIR) not in sys.path:
    # Keep backend-shared test helpers importable without turning tests into a package.
    sys.path.insert(0, str(_BACKEND_TESTS_DIR))


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
