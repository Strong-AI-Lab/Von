from types import SimpleNamespace


def _sample_llm_debug() -> dict:
    return {
        "request_id": "req-critic-1",
        "model": "gpt-5.4-mini",
        "workflow_discovery": {"match_count": 1},
        "tool_invocations": [
            {
                "tool": "search_concepts",
                "arguments": {"query": "paper"},
                "result": {"results": [{"concept_id": "#V#paper", "name": "Paper"}]},
            }
        ],
        "search_evidence": [
            {
                "tool": "search_concepts",
                "query": "paper",
                "result": {"results": [{"concept_id": "#V#paper", "name": "Paper"}]},
            }
        ],
        "turn_execution_diagnostics": {
            "latest_progress": {"phase": "completed"},
            "workflow_stage_path": {"path": [{"stage_id": "tool_execute"}]},
        },
        "llm_allowed_tools": ["search_concepts"],
        "allowed_write_tools": [],
        "aux_llm_calls": [{"model": "gpt-5.4-mini", "prompt": "x", "response": "y"}],
    }


def _history_context() -> dict:
    llm_debug = _sample_llm_debug()
    target_message = {
        "role": "assistant",
        "content": "Draft analysis",
        "timestamp": "2026-03-29T04:00:00Z",
        "llm_debug_data": llm_debug,
    }
    return {
        "user_id": "#V#user",
        "session_id": "sess-critic-1",
        "namespace": "#V#user@org",
        "org_id": "#V#org",
        "history": [
            {"role": "user", "content": "Inspect the paper representation"},
            target_message,
        ],
        "target_index": 1,
        "target_message": target_message,
        "target_llm_debug": llm_debug,
        "prompt_text": "Inspect the paper representation",
    }


def test_episode_critic_bundle_reconstructs_turn_record_and_emits_receipts(monkeypatch):
    from src.backend.services import episode_critic_evidence_service as svc

    monkeypatch.setattr(svc, "_load_turn_execution_record", lambda **_: None)
    monkeypatch.setattr(svc, "_resolve_history_context", lambda **_: _history_context())
    monkeypatch.setattr(
        svc,
        "get_latest_workflow_use_episode",
        lambda **_: {"episode_id": "wfep_1", "workflow_id": "#V#tool_calling_workflow"},
    )
    monkeypatch.setattr(
        svc,
        "_resolve_workflow_definition_identity",
        lambda **_: (
            {
                "workflow_id": "#V#tool_calling_workflow",
                "definition_hash": "deadbeef",
                "source": "runtime_registry",
            },
            "workflow_registry",
        ),
    )

    class _StubManager:
        def get_instance(self, _instance_id):
            return None

        def list_instances(self, **_kwargs):
            return []

    monkeypatch.setattr(svc, "WorkflowInstanceManager", lambda: _StubManager())

    result = svc.build_episode_critic_evidence_bundle(request_id="req-critic-1")

    assert result["success"] is True
    assert result["ready_for_critic"] is True
    assert result["fail_closed"] is False
    assert (
        result["source_resolution"]["turn_execution_record_source"]
        == "chat_history.reconstructed_turn_execution_record"
    )
    assert result["observed_evidence"]["turn_execution_record"]["request_id"] == "req-critic-1"
    assert result["observed_evidence"]["tool_ledger"]["search_evidence_count"] == 1
    assert result["expected_context"]["allowed_tool_families"] == ["search"]
    assert result["receipts"]["turn_execution_record"]["present"] is True
    assert result["bundle_receipt"]["sha256"]


def test_episode_critic_bundle_fails_closed_when_core_context_is_missing(monkeypatch):
    from src.backend.services import episode_critic_evidence_service as svc

    monkeypatch.setattr(
        svc,
        "_load_turn_execution_record",
        lambda **_: {
            "request_id": "req-critic-2",
            "session_id": "sess-critic-2",
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
            "execution": {"tool_invocations": [], "required_effects_contract": {}},
            "required_effects": [],
            "postcondition_checks": [],
            "completion_gate": {},
            "critic": {},
        },
    )
    monkeypatch.setattr(svc, "_resolve_history_context", lambda **_: None)
    monkeypatch.setattr(svc, "get_latest_workflow_use_episode", lambda **_: None)
    monkeypatch.setattr(svc, "_resolve_workflow_definition_identity", lambda **_: (None, None))

    class _StubManager:
        def get_instance(self, _instance_id):
            return None

        def list_instances(self, **_kwargs):
            return []

    monkeypatch.setattr(svc, "WorkflowInstanceManager", lambda: _StubManager())

    result = svc.build_episode_critic_evidence_bundle(request_id="req-critic-2")

    assert result["success"] is True
    assert result["ready_for_critic"] is False
    assert result["fail_closed"] is True
    assert "selected_llm_debug_missing" in result["fail_closed_reason_codes"]
    assert "neighbouring_context_missing" in result["fail_closed_reason_codes"]
    assert "workflow_definition_identity_missing" in result["fail_closed_reason_codes"]
