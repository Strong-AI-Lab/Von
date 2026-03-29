from typing import Any

from src.backend.services.chat_history_service import (
    _upsert_turn_execution_projection_for_message,
)


def _sample_record() -> dict:
    return {
        "request_id": "req-1607-1",
        "session_id": "sess-1607-1",
        "namespace": "#V#user@org",
        "user_id": "#V#user",
        "org_id": "#V#org",
        "created_at_utc": "2026-03-29T05:00:00Z",
        "workflow_selection": {
            "selected_workflow_id": "#V#tool_calling_workflow",
        },
        "workflow_routing_diagnostics": {
            "dispatch": {"dispatch_workflow_id": "#V#tool_calling_workflow"},
        },
        "execution": {
            "tool_invocations": [
                {
                    "tool": "search_concepts",
                    "status": "ok",
                    "payload_fingerprint": "fp-search",
                    "result_summary": "Found 1 concept",
                }
            ],
            "search_evidence": [
                {
                    "tool": "search_concepts",
                    "query": "paper",
                    "arguments_sha256": "sha-args",
                    "result_sha256": "sha-result",
                    "result_truncated": False,
                }
            ],
        },
        "critic": {
            "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
            "summary": {
                "verified_count": 2,
                "not_verified_count": 0,
                "inconclusive_count": 0,
                "error_count": 0,
            },
        },
        "completion_gate": {
            "decision": "completed",
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
            "blocking_failure_codes": [],
        },
    }


def _sample_llm_debug() -> dict:
    return {
        "tool_invocations": [
            {
                "tool": "search_concepts",
                "effective_payload": {
                    "results": [{"concept_id": "#V#paper", "name": "Paper"}]
                },
            },
            {
                "tool": "create_task",
                "effective_payload": {"task_concept_id": "#V#task_123"},
            },
            {
                "tool": "jira_create_issue",
                "effective_payload": {"issue_key": "JVNAUTOSCI-999"},
            },
        ],
        "search_evidence": [
            {
                "tool": "search_concepts",
                "query": "paper",
                "result": {"results": [{"concept_id": "#V#paper", "name": "Paper"}]},
            }
        ],
    }


class _StubUpdateResult:
    def __init__(self, *, modified_count: int, matched_count: int, upserted_id):
        self.modified_count = modified_count
        self.matched_count = matched_count
        self.upserted_id = upserted_id


class _StubCollection:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    def list_indexes(self):
        return []

    def create_index(self, *_args, **_kwargs):
        return None

    def update_one(self, query, update, upsert=False):
        memory_id = query["memory_id"]
        inserted = memory_id not in self.docs
        payload = dict(update["$set"])
        self.docs[memory_id] = payload
        return _StubUpdateResult(
            modified_count=0 if inserted else 1,
            matched_count=0 if inserted else 1,
            upserted_id=memory_id if inserted and upsert else None,
        )

    def find_one(self, query, _projection=None):
        return self.docs.get(query["memory_id"])


def test_build_episode_critique_memory_state_extracts_links_and_receipts(monkeypatch):
    from src.backend.services import episode_critique_memory_service as svc

    monkeypatch.setattr(
        svc,
        "get_latest_workflow_use_episode",
        lambda **_: {
            "episode_id": "wfep_123",
            "workflow_id": "#V#tool_calling_workflow",
            "stable_key": "stable-123",
            "source": "chat_turn_workflow",
            "instance_id": "#V#wf_instance_1",
        },
    )

    state = svc.build_episode_critique_memory_state_from_turn(
        record=_sample_record(),
        llm_debug_data=_sample_llm_debug(),
    )

    assert state is not None
    assert state["memory_id"].startswith("#V#episode_critique_memory_")
    assert state["subject_episode"]["episode_id"] == "wfep_123"
    assert state["critic"]["verdict"] == "pass"
    assert state["critic"]["confidence"] == 1.0
    assert "#V#paper" in state["implicated"]["concept_ids"]
    assert "#V#task_123" in state["remediation"]["task_ids"]
    assert "JVNAUTOSCI-999" in state["remediation"]["jira_issue_keys"]
    assert state["evidence_receipts"]["turn_execution_record"]["request_id"] == "req-1607-1"
    assert state["evidence_receipts"]["receipt_hash"]


def test_upsert_episode_critique_memory_projection_persists_document(monkeypatch):
    from src.backend.services import episode_critique_memory_service as svc

    coll = _StubCollection()
    monkeypatch.setattr(svc, "get_episode_critique_memories_collection", lambda: coll)

    record = {
        "memory_id": "#V#episode_critique_memory_abc",
        "request_id": "req-1607-1",
        "namespace": "#V#user@org",
        "session_id": "sess-1607-1",
        "subject_episode": {"episode_id": "wfep_123", "workflow_id": "#V#workflow"},
        "critic": {"verdict": "pass", "confidence": 1.0, "unresolved_check_count": 0},
        "implicated": {
            "workflow_ids": ["#V#workflow"],
            "tool_names": ["search_concepts"],
            "concept_ids": ["#V#paper"],
        },
        "remediation": {
            "task_ids": ["#V#task_123"],
            "jira_issue_keys": ["JVNAUTOSCI-999"],
        },
        "recommendations": ["No remediation is currently indicated by the recorded critic evidence."],
        "dedupe_fingerprint": "fingerprint-123",
        "evidence_receipts": {"receipt_hash": "receipt-123"},
        "created_at_utc": "2026-03-29T05:00:00Z",
        "updated_at_utc": "2026-03-29T05:05:00Z",
    }

    outcome = svc.upsert_episode_critique_memory_projection(record=record)

    assert outcome["updated"] is True
    stored = coll.docs["#V#episode_critique_memory_abc"]
    assert stored["verdict"] == "pass"
    assert stored["receipt_hash"] == "receipt-123"
    assert stored["remediation_task_ids"] == ["#V#task_123"]


def test_chat_history_projection_path_invokes_episode_critique_memory_upsert(monkeypatch):
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.upsert_turn_execution_record_projection",
        lambda **kwargs: {"updated": True, "request_id": kwargs["record"]["request_id"]},
    )

    def _fake_upsert_episode_critique_memory_from_turn(**kwargs):
        captured.update(kwargs)
        return {"success": True, "memory_id": "#V#episode_critique_memory_xyz"}

    monkeypatch.setattr(
        "src.backend.services.chat_history_service.upsert_episode_critique_memory_from_turn",
        _fake_upsert_episode_critique_memory_from_turn,
    )

    llm_debug_data = {"turn_execution_record": _sample_record()}
    _upsert_turn_execution_projection_for_message(
        message={"role": "assistant", "content": "Done"},
        llm_debug_data=llm_debug_data,
        user_id="#V#user",
        session_id="sess-1607-1",
        namespace="#V#user@org",
        org_id="#V#org",
    )

    assert captured["record"]["request_id"] == "req-1607-1"
    assert captured["llm_debug_data"] is llm_debug_data
    assert captured["namespace"] == "#V#user@org"
