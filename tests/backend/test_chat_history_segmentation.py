import sys
import os

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import pytest
from unittest.mock import MagicMock, patch
from src.backend.services.chat_history_service import get_chat_history_segments, ChatHistoryServiceError

@pytest.fixture
def mock_collection():
    return MagicMock()

@pytest.fixture
def mock_get_collection(mock_collection):
    with patch('src.backend.services.chat_history_service.get_chat_history_collection_service', return_value=mock_collection) as mock:
        yield mock

def test_get_chat_history_segments_empty(mock_collection, mock_get_collection):
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = []
    mock_collection.find.return_value = mock_cursor

    segments = get_chat_history_segments("user1", "session1")
    assert segments == []

def test_get_chat_history_segments_no_history(mock_collection, mock_get_collection):
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = [{"history": []}]
    mock_collection.find.return_value = mock_cursor

    segments = get_chat_history_segments("user1", "session1")
    assert segments == []

def test_get_chat_history_segments_single_segment(mock_collection, mock_get_collection):
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"}
    ]
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = [{"history": history}]
    mock_collection.find.return_value = mock_cursor

    segments = get_chat_history_segments("user1", "session1")
    assert len(segments) == 1
    assert segments[0] == history

def test_get_chat_history_segments_with_reset(mock_collection, mock_get_collection):
    history = [
        {"role": "user", "content": "msg1"},
        {"role": "system", "content": "__RESET__"},
        {"role": "user", "content": "msg2"}
    ]
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = [{"history": history}]
    mock_collection.find.return_value = mock_cursor

    segments = get_chat_history_segments("user1", "session1")

    assert len(segments) == 2
    assert len(segments[0]) == 1
    assert segments[0][0]["content"] == "msg1"
    assert len(segments[1]) == 1
    assert segments[1][0]["content"] == "msg2"

def test_get_chat_history_segments_multiple_resets(mock_collection, mock_get_collection):
    history = [
        {"role": "user", "content": "msg1"},
        {"role": "system", "content": "__RESET__"},
        {"role": "user", "content": "msg2"},
        {"role": "system", "content": "__RESET__"},
        {"role": "user", "content": "msg3"}
    ]
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = [{"history": history}]
    mock_collection.find.return_value = mock_cursor

    segments = get_chat_history_segments("user1", "session1")

    assert len(segments) == 3
    assert segments[0][0]["content"] == "msg1"
    assert segments[1][0]["content"] == "msg2"
    assert segments[2][0]["content"] == "msg3"

def test_get_chat_history_segments_consecutive_resets(mock_collection, mock_get_collection):
    history = [
        {"role": "user", "content": "msg1"},
        {"role": "system", "content": "__RESET__"},
        {"role": "system", "content": "__RESET__"},
        {"role": "user", "content": "msg2"}
    ]
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = [{"history": history}]
    mock_collection.find.return_value = mock_cursor

    segments = get_chat_history_segments("user1", "session1")

    assert len(segments) == 3
    assert segments[0][0]["content"] == "msg1"
    assert segments[1] == [] # Empty segment between resets
    assert segments[2][0]["content"] == "msg2"

def test_get_chat_history_segments_ends_with_reset(mock_collection, mock_get_collection):
    history = [
        {"role": "user", "content": "msg1"},
        {"role": "system", "content": "__RESET__"}
    ]
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = [{"history": history}]
    mock_collection.find.return_value = mock_cursor

    segments = get_chat_history_segments("user1", "session1")

    assert len(segments) == 2
    assert segments[0][0]["content"] == "msg1"
    assert segments[1] == [] # Empty current segment after reset
