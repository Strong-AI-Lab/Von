import importlib

import pytest


@pytest.fixture()
def mock_db_env(monkeypatch):
    # Must be set before importing mongo_client so USE_MOCK_DB is computed correctly.
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    import backend.db.mongo_client as mongo_client

    importlib.reload(mongo_client)

    yield

    monkeypatch.delenv("VON_USE_MOCK_DB", raising=False)


def test_trace_store_roundtrip_and_redaction(mock_db_env):
    from backend.workflows.trace_model import WorkflowExecutionTrace
    from backend.workflows.trace_store import (
        get_workflow_execution_trace,
        insert_workflow_execution_trace,
        list_recent_workflow_execution_traces,
    )

    trace = WorkflowExecutionTrace(workflow_id="#V#chat_assistant_workflow")
    trace.user_namespace = "#V#unit_test_user"

    step = trace.start_step(
        "tool.search_knowledge_base",
        inputs={
            "query": "hello",
            "api_key": "abc123",
            "token": "def456",
        },
    )
    step.finish_success({"result": "ok"})
    trace.finish_completed()

    doc = trace.to_storage_document()
    stored = insert_workflow_execution_trace(doc)
    assert stored == trace.execution_id

    loaded = get_workflow_execution_trace(trace.execution_id)
    assert isinstance(loaded, dict)
    assert loaded.get("execution_id") == trace.execution_id
    assert loaded.get("workflow_id") == "#V#chat_assistant_workflow"

    steps = loaded.get("steps")
    assert isinstance(steps, list)
    assert steps

    inputs = steps[0].get("inputs")
    assert isinstance(inputs, dict)
    # Redacted by key name.
    assert inputs.get("api_key") == "[redacted]"
    assert inputs.get("token") == "[redacted]"

    recent = list_recent_workflow_execution_traces(limit=10)
    assert any(item.get("execution_id") == trace.execution_id for item in recent)
