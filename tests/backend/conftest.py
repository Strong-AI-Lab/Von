"""Backend test configuration.

Shared backend test helpers live here. Runtime workflow-routing defaults are no
longer overridden globally because selector compatibility env toggles were
removed from production code.
"""

import re
import shutil
import sys
from pathlib import Path
from typing import Generator

import pytest

_BACKEND_TESTS_DIR = Path(__file__).resolve().parent
if str(_BACKEND_TESTS_DIR) not in sys.path:
    # Keep backend-shared test helpers importable without turning tests into a package.
    sys.path.insert(0, str(_BACKEND_TESTS_DIR))

_WORKSPACE_TMP_ROOT = _BACKEND_TESTS_DIR.parent.parent / "tmp" / "pytest_backend"


@pytest.fixture()
def workspace_tmp_path(request) -> Generator[Path, None, None]:
    """Provide a writable per-test directory under the workspace.

    The default pytest tmpdir fixtures can hit Windows sandbox permission issues
    in agent runs. Keeping these test artefacts under the repository workspace
    makes the affected backend tests deterministic in both local and agent
    environments.
    """

    safe_nodeid = re.sub(r"[^A-Za-z0-9._-]+", "_", request.node.nodeid).strip("_")
    path = _WORKSPACE_TMP_ROOT / safe_nodeid
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def isolated_window_session_db(monkeypatch):
    """Exercise durable bindings and mutation receipts against a fresh mock DB."""
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_isolated_window_sessions")
    from src.backend.db.mongo_client import close_connection
    from src.backend.services import window_session_context_service as windows

    close_connection()
    monkeypatch.setattr(windows, "_window_session_store", None)
    monkeypatch.setattr(windows, "_window_session_binding_repository", None)
    try:
        yield
    finally:
        if windows._window_session_store is not None:
            windows._window_session_store.stop_cleanup_thread()
        close_connection()
