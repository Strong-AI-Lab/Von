import importlib

import pytest

pytest.importorskip(
    "mongomock", reason="mongomock is required for workflow trace tests"
)


@pytest.fixture()
def mock_db_env(monkeypatch):
    # Must be set before importing mongo_client so USE_MOCK_DB is computed correctly.
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    import src.backend.db.mongo_client as mongo_client

    importlib.reload(mongo_client)

    yield

    monkeypatch.delenv("VON_USE_MOCK_DB", raising=False)


def test_trace_store_roundtrip_and_redaction(mock_db_env):
    from src.backend.workflows.trace_model import WorkflowExecutionTrace
    from src.backend.workflows.trace_store import (
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

    filtered = list_recent_workflow_execution_traces(
        limit=10,
        workflow_id="#V#chat_assistant_workflow",
    )
    assert any(item.get("execution_id") == trace.execution_id for item in filtered)


def test_trace_store_preserves_token_counts_while_redacting_secrets(mock_db_env):
    from src.backend.workflows.trace_model import WorkflowExecutionTrace
    from src.backend.workflows.trace_store import (
        get_workflow_execution_trace,
        insert_workflow_execution_trace,
    )

    trace = WorkflowExecutionTrace(workflow_id="#V#chat_assistant_workflow")
    trace.metadata["llm_usage"] = {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }
    trace.metadata["api_token"] = "super-secret"
    trace.record_action(
        action_id="paper.rank",
        outputs={
            "llm_calls": [
                {
                    "model_name": "gpt-5-mini",
                    "provider": "openai",
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 30,
                        "total_tokens": 150,
                    },
                }
            ]
        },
        duration_ms=900,
    )
    trace.finish_completed()

    stored = insert_workflow_execution_trace(trace.to_storage_document())
    assert stored == trace.execution_id

    loaded = get_workflow_execution_trace(trace.execution_id)
    assert isinstance(loaded, dict)
    metadata = loaded.get("metadata")
    assert isinstance(metadata, dict)
    usage = metadata.get("llm_usage")
    assert usage == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }
    assert metadata.get("api_token") == "[redacted]"
    actions = loaded.get("actions")
    assert isinstance(actions, list)
    action_outputs = actions[0].get("outputs")
    assert action_outputs == {
        "llm_calls": [
            {
                "model_name": "gpt-5-mini",
                "provider": "openai",
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                },
            }
        ]
    }
