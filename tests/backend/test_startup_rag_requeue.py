import sys
import os

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from unittest.mock import MagicMock

from src.backend.server import utils_flask


def test_is_running_under_pytest_true_when_env_set(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    assert utils_flask._is_running_under_pytest() is True


def test_startup_requeue_updates_only_skipped_or_missing(monkeypatch):
    # Make the reconcile deterministic
    monkeypatch.setenv("VON_RAG_STARTUP_REQUEUE_LIMIT", "2")

    mock_coll = MagicMock()
    # Return two IDs (limit=2)
    mock_coll.find.return_value.sort.return_value.limit.return_value = [
        {"_id": "a"},
        {"_id": "b"},
    ]

    mock_update_result = MagicMock()
    mock_update_result.modified_count = 2
    mock_coll.update_many.return_value = mock_update_result

    mock_db = {"interaction_sessions": mock_coll}

    # Patch get_db in the module under test
    monkeypatch.setattr(utils_flask, "get_db", lambda: mock_db)

    logger = MagicMock()
    utils_flask._startup_requeue_unindexed_interaction_sessions(logger)

    # Ensures we only update those two IDs (capped)
    args, _ = mock_coll.update_many.call_args
    assert args[0] == {"_id": {"$in": ["a", "b"]}}
    assert args[1]["$set"]["indexing_status"] == "pending"
