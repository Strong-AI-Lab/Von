from __future__ import annotations

import re
from typing import Any


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def skip(self, n: int):
        self._docs = self._docs[int(n) :]
        return self

    def limit(self, n: int):
        self._docs = self._docs[: int(n)]
        return self

    def __iter__(self):
        return iter(self._docs)


class _TurnExecutionCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        namespace = query.get("namespace")
        if isinstance(namespace, str) and doc.get("namespace") != namespace:
            return False

        decision_filter = query.get("completion_gate.decision")
        decision = (doc.get("completion_gate") or {}).get("decision")
        if isinstance(decision_filter, str):
            if decision != decision_filter:
                return False
        elif isinstance(decision_filter, dict):
            options = decision_filter.get("$in")
            if isinstance(options, list) and decision not in options:
                return False

        workflow_filter = query.get("workflow_selection.selected_workflow_id")
        workflow_id = (doc.get("workflow_selection") or {}).get("selected_workflow_id")
        if isinstance(workflow_filter, str) and workflow_id != workflow_filter:
            return False

        requires_follow_up = query.get("completion_gate.requires_follow_up")
        if isinstance(requires_follow_up, bool):
            if bool((doc.get("completion_gate") or {}).get("requires_follow_up")) != requires_follow_up:
                return False

        prompt_filter = query.get("prompt.preview")
        if isinstance(prompt_filter, dict):
            pattern = prompt_filter.get("$regex")
            flags = prompt_filter.get("$options")
            preview = (doc.get("prompt") or {}).get("preview") or ""
            if not isinstance(pattern, str):
                return False
            regex_flags = re.IGNORECASE if flags == "i" else 0
            if not re.search(pattern, str(preview), regex_flags):
                return False

        created_at_filter = query.get("created_at_utc")
        if isinstance(created_at_filter, dict):
            created_at = doc.get("created_at_utc")
            if not isinstance(created_at, str):
                return False
            gte = created_at_filter.get("$gte")
            if isinstance(gte, str) and created_at < gte:
                return False
            lte = created_at_filter.get("$lte")
            if isinstance(lte, str) and created_at > lte:
                return False

        request_id = query.get("request_id")
        if isinstance(request_id, str) and doc.get("request_id") != request_id:
            return False

        return True

    def find(self, query: dict[str, Any], _projection: dict[str, Any] | None = None):
        docs = [doc for doc in self._docs if self._matches(doc, query)]
        return _Cursor(docs)

    def find_one(self, query: dict[str, Any], _projection: dict[str, Any] | None = None):
        for doc in self.find(query, _projection):
            return doc
        return None

    def count_documents(self, query: dict[str, Any]) -> int:
        return len(list(self.find(query)))

    def aggregate(self, pipeline: list[dict[str, Any]]):
        match = {}
        for stage in pipeline:
            if "$match" in stage and isinstance(stage["$match"], dict):
                match = stage["$match"]
                break
        docs = [doc for doc in self._docs if self._matches(doc, match)]

        counts: dict[str, int] = {}
        for doc in docs:
            decision = (doc.get("completion_gate") or {}).get("decision") or "unknown"
            key = str(decision)
            counts[key] = counts.get(key, 0) + 1
        return [{"_id": key, "count": value} for key, value in counts.items()]


class _ChatHistoryCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        namespace = query.get("namespace")
        if isinstance(namespace, str) and doc.get("namespace") != namespace:
            return False

        session_id_filter = query.get("session_id")
        session_id = doc.get("session_id")
        if isinstance(session_id_filter, str):
            if session_id != session_id_filter:
                return False
        elif isinstance(session_id_filter, dict):
            options = session_id_filter.get("$in")
            if isinstance(options, list) and session_id not in options:
                return False

        return True

    def find(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        docs = [doc for doc in self._docs if self._matches(doc, query)]
        if not isinstance(projection, dict):
            return _Cursor(docs)

        include_keys = {key for key, include in projection.items() if include}
        projected_docs: list[dict[str, Any]] = []
        for doc in docs:
            projected_doc: dict[str, Any] = {}
            for key in include_keys:
                if key in doc:
                    projected_doc[key] = doc[key]
            projected_docs.append(projected_doc)
        return _Cursor(projected_docs)


class _DiagnosticsChatHistoryCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def find_one(self, query: dict[str, Any], projection: dict[str, Any] | None = None):
        namespace = query.get("namespace")
        history_filter = query.get("history")
        elem_match = (
            history_filter.get("$elemMatch")
            if isinstance(history_filter, dict)
            else None
        )
        requested_role = elem_match.get("role") if isinstance(elem_match, dict) else None
        requested_request_id = (
            elem_match.get("llm_debug_data.request_id")
            if isinstance(elem_match, dict)
            else None
        )

        for doc in self._docs:
            if isinstance(namespace, str) and doc.get("namespace") != namespace:
                continue

            matched = False
            for entry in doc.get("history") or []:
                if not isinstance(entry, dict):
                    continue
                if isinstance(requested_role, str) and entry.get("role") != requested_role:
                    continue
                llm_debug = entry.get("llm_debug_data")
                debug_request_id = (
                    llm_debug.get("request_id")
                    if isinstance(llm_debug, dict)
                    else None
                )
                if (
                    isinstance(requested_request_id, str)
                    and debug_request_id != requested_request_id
                ):
                    continue
                matched = True
                break

            if not matched:
                continue

            if not isinstance(projection, dict):
                return doc

            include_keys = {
                key for key, include in projection.items() if include and key != "_id"
            }
            projected_doc: dict[str, Any] = {}
            for key in include_keys:
                if key in doc:
                    projected_doc[key] = doc[key]
            return projected_doc

        return None


class _DB:
    def __init__(self, collections: dict[str, Any]):
        self._collections = dict(collections)

    def __getitem__(self, key: str):
        return self._collections[key]


def _build_gateway():
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport

    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _build_hesitancy_trace_docs() -> list[dict[str, Any]]:
    return [
        {
            "request_id": "req-hes-1",
            "session_id": "chat-hes-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-20T00:00:00Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
                "evidence_payload": {
                    "terminal_outcome": "retrying",
                    "repeat_iteration": True,
                },
            },
            "completion_gate_repeat_iteration": True,
            "completion_gate_loop_attempts": 1,
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "prompt": {"preview": "JVNAUTOSCI-1326 hesitancy trace turn 1"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        },
        {
            "request_id": "req-hes-2",
            "session_id": "chat-hes-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-20T00:00:15Z",
            "completion_gate": {
                "decision": "partial",
                "decision_reason": "Mutation execution observed but verification is inconclusive.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_2"],
                "evidence_payload": {
                    "terminal_outcome": "stall_latency_budget_exhausted",
                    "repeat_iteration": False,
                    "repeat_stop_reason": "stall_latency_budget_exhausted",
                    "escalation_signal": True,
                    "escalation_reason": "stall_latency_budget_exhausted",
                },
            },
            "completion_gate_repeat_iteration": False,
            "completion_gate_loop_attempts": 2,
            "completion_gate_loop_stop_reason": "stall_latency_budget_exhausted",
            "completion_gate_escalation_signal": True,
            "completion_gate_escalation_reason": "stall_latency_budget_exhausted",
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "required_effects": [{"effect_id": "effect_2", "status": "satisfied"}],
            "prompt": {"preview": "JVNAUTOSCI-1326 hesitancy trace turn 2"},
            "critic": {"summary": {"inconclusive_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        },
        {
            "request_id": "req-hes-3",
            "session_id": "chat-hes-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-20T00:00:35Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
                "evidence_payload": {"terminal_outcome": "completed"},
            },
            "completion_gate_repeat_iteration": False,
            "completion_gate_loop_attempts": 0,
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "required_effects": [],
            "prompt": {"preview": "JVNAUTOSCI-1326 hesitancy trace turn 3"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": True,
            },
        },
    ]


def test_rag_list_indexed_supports_turn_execution_records(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "request_id": "req-1",
            "session_id": "chat-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T00:57:25Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "workflow_routing_diagnostics": {
                "schema_version": "workflow_routing_diagnostics.v1",
                "dispatch": {
                    "selected_execution_mode": "tool_pipeline",
                    "failure_codes": ["tool_dispatch_handoff_zero_execution"],
                },
            },
            "prompt": {"preview": "Those relations were not added."},
            "critic": {"summary": {"not_verified_count": 1}},
        },
        {
            "request_id": "req-2",
            "session_id": "chat-2",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:01:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "plain_response",
            },
            "prompt": {"preview": "What is the status?"},
            "critic": {"summary": {"not_verified_count": 0}},
        },
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._rag_list_indexed(
        namespace="#V#user@org",
        collection="turn_execution_records",
        decision="escalation_required",
        limit=10,
        offset=0,
    )

    assert result["success"] is True
    assert result["collection"] == "turn_execution_records"
    assert result["provenance"]["item_kind"] == "turn_execution_record_list"
    assert result["provenance"]["source_system"] == "mongo.turn_execution_records"
    assert result["decision_counts"]["escalation_required"] == 1
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["request_id"] == "req-1"
    assert item["decision"] == "escalation_required"
    assert item["unresolved_effect_count"] == 1
    assert item["item_kind"] == "turn_execution_record"
    assert item["source_system"] == "mongo.turn_execution_records"
    assert item["workflow_routing_diagnostics"]["dispatch"]["failure_codes"] == [
        "tool_dispatch_handoff_zero_execution"
    ]


def test_rag_get_item_supports_turn_execution_records(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    doc = {
        "request_id": "req-9",
        "session_id": "chat-9",
        "namespace": "#V#user@org",
        "created_at_utc": "2026-02-19T00:59:00Z",
        "updated_at_utc": "2026-02-19T01:00:00Z",
        "completion_gate": {
            "decision": "partial",
            "decision_reason": "Mutation execution observed but verification is inconclusive.",
            "safe_to_claim_completion": False,
            "requires_follow_up": True,
            "blocking_effect_ids": ["effect_2"],
        },
        "workflow_selection": {
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selector_verdict": "tool_seeking",
        },
        "workflow_routing_diagnostics": {
            "schema_version": "workflow_routing_diagnostics.v1",
            "selector": {
                "prompt": {"text": "Select workflow", "char_count": 15},
                "response": {
                    "text": "#V#tool_calling_workflow",
                    "char_count": 24,
                },
            },
            "dispatch": {
                "selected_execution_mode": "tool_pipeline",
                "last_successful_boundary": "workflow_handoff",
            },
        },
        "prompt": {"preview": "Proceed with predicates"},
        "required_effects": [{"effect_id": "effect_2", "status": "satisfied"}],
        "postcondition_checks": [
            {"check_id": "check_effect_2", "status": "inconclusive"}
        ],
        "critic": {"summary": {"inconclusive_count": 1}},
    }

    coll = _TurnExecutionCollection([doc])
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._rag_get_item(
        namespace="#V#user@org",
        collection="turn_execution_records",
        session_id="req-9",
    )

    assert result["success"] is True
    assert result["request_id"] == "req-9"
    assert result["decision"] == "partial"
    assert result["requires_follow_up"] is True
    assert result["item_kind"] == "turn_execution_record"
    assert result["provenance"]["item_kind"] == "turn_execution_record_item"
    assert result["workflow_routing_diagnostics"]["selector"]["response"]["text"] == (
        "#V#tool_calling_workflow"
    )
    assert (
        result["workflow_routing_diagnostics"]["dispatch"]["last_successful_boundary"]
        == "workflow_handoff"
    )


def test_turn_execution_list_includes_rag_indexing_state_from_chat_history(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_docs = [
        {
            "request_id": "req-indexed-1",
            "session_id": "chat-indexed-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:20:00Z",
            "completion_gate": {"decision": "completed", "requires_follow_up": False},
            "required_effects": [],
            "workflow_selection": {"selected_workflow_id": "#V#chat_assistant_workflow"},
            "prompt": {"preview": "All done"},
            "critic": {"summary": {"not_verified_count": 0}},
        },
        {
            "request_id": "req-partial-1",
            "session_id": "chat-partial-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:21:00Z",
            "completion_gate": {"decision": "partial", "requires_follow_up": True},
            "required_effects": [{"effect_id": "effect_1", "status": "satisfied"}],
            "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
            "prompt": {"preview": "Attempted write"},
            "critic": {"summary": {"inconclusive_count": 1}},
        },
    ]
    chat_docs = [
        {
            "session_id": "chat-indexed-1",
            "namespace": "#V#user@org",
            "history": [{"content": "A"}, {"content": "B"}],
            "rag_indexed_success": 2,
            "rag_indexed_failed": 0,
        },
        {
            "session_id": "chat-partial-1",
            "namespace": "#V#user@org",
            "history": [{"content": "A"}, {"content": "B"}, {"content": "C"}],
            "rag_indexed_success": 1,
            "rag_indexed_failed": 1,
        },
    ]

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB(
            {
                "turn_execution_records": _TurnExecutionCollection(turn_docs),
                "chat_history": _ChatHistoryCollection(chat_docs),
            }
        ),
    )

    result = cat._turn_execution_list(namespace="#V#user@org", limit=20, offset=0)

    assert result["success"] is True
    assert result["collection"] == "turn_execution_records"
    by_request_id = {item["request_id"]: item for item in result["items"]}

    indexed_state = by_request_id["req-indexed-1"]["rag_indexing_state"]
    assert indexed_state["status"] == "indexed"
    assert indexed_state["sync_state"] == "synchronised"
    assert indexed_state["fully_indexed"] is True
    assert indexed_state["messages_pending_indexing"] == 0

    partial_state = by_request_id["req-partial-1"]["rag_indexing_state"]
    assert partial_state["status"] == "partial"
    assert partial_state["sync_state"] == "error"
    assert partial_state["fully_indexed"] is False
    assert partial_state["messages_pending_indexing"] == 1

    assert result["rag_indexing_state_counts"]["indexed"] == 1
    assert result["rag_indexing_state_counts"]["partial"] == 1
    assert "rag_indexing_lookup_warning" not in result


def test_turn_execution_get_includes_rag_indexing_state_from_chat_history(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_doc = {
        "request_id": "req-failed-indexing",
        "session_id": "chat-failed-indexing",
        "namespace": "#V#user@org",
        "created_at_utc": "2026-02-19T01:22:00Z",
        "updated_at_utc": "2026-02-19T01:23:00Z",
        "completion_gate": {"decision": "partial", "requires_follow_up": True},
        "required_effects": [{"effect_id": "effect_2", "status": "satisfied"}],
        "postcondition_checks": [{"check_id": "check_1", "status": "inconclusive"}],
        "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
        "prompt": {"preview": "Attempted update"},
        "critic": {"summary": {"inconclusive_count": 1}},
    }
    chat_docs = [
        {
            "session_id": "chat-failed-indexing",
            "namespace": "#V#user@org",
            "history": [{"content": "A"}, {"content": "B"}],
            "rag_indexed_success": 0,
            "rag_indexed_failed": 2,
        }
    ]

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB(
            {
                "turn_execution_records": _TurnExecutionCollection([turn_doc]),
                "chat_history": _ChatHistoryCollection(chat_docs),
            }
        ),
    )

    result = cat._turn_execution_get(
        namespace="#V#user@org",
        request_id="req-failed-indexing",
    )

    assert result["success"] is True
    state = result["rag_indexing_state"]
    assert state["status"] == "indexing_failed"
    assert state["sync_state"] == "error"
    assert state["indexed"] is False
    assert state["reason_code"] == "all_indexing_attempts_failed"
    assert "rag_indexing_lookup_warning" not in result


def test_turn_execution_list_reports_lookup_warning_when_chat_history_unavailable(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_docs = [
        {
            "request_id": "req-warning-1",
            "session_id": "chat-warning-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:24:00Z",
            "completion_gate": {"decision": "partial", "requires_follow_up": True},
            "required_effects": [],
            "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
            "prompt": {"preview": "Attempted update"},
            "critic": {"summary": {"inconclusive_count": 1}},
        }
    ]

    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": _TurnExecutionCollection(turn_docs)}),
    )

    result = cat._turn_execution_list(namespace="#V#user@org", limit=20, offset=0)

    assert result["success"] is True
    assert result["rag_indexing_lookup_warning"]["reason_code"] == (
        "chat_history_lookup_unavailable"
    )
    state = result["items"][0]["rag_indexing_state"]
    assert state["status"] == "unknown"
    assert state["reason_code"] == "chat_history_lookup_unavailable"


def test_turn_execution_list_and_get_wrappers(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "request_id": "req-wrap-1",
            "session_id": "chat-wrap-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:00:00Z",
            "completion_gate": {
                "decision": "escalation_required",
                "requires_follow_up": True,
                "safe_to_claim_completion": False,
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
            "prompt": {"preview": "JVNAUTOSCI-1202: Relations were not added"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        }
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    listed = cat._turn_execution_list(namespace="#V#user@org", limit=10, offset=0)
    assert listed["success"] is True
    assert listed["collection"] == "turn_execution_records"
    assert len(listed["items"]) == 1
    assert listed["items"][0]["request_id"] == "req-wrap-1"

    got = cat._turn_execution_get(namespace="#V#user@org", request_id="req-wrap-1")
    assert got["success"] is True
    assert got["request_id"] == "req-wrap-1"


def test_turn_execution_get_diagnostics_returns_embedded_payload(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    chat_docs = [
        {
            "user_id": "#V#user",
            "session_id": "chat-diag-1",
            "namespace": "#V#user@org",
            "organisation_concept_id": "#V#org",
            "history": [
                {"role": "user", "content": "Show the turn diagnostics"},
                {
                    "role": "assistant",
                    "content": "Done",
                    "timestamp": "2026-04-01T00:00:02Z",
                    "llm_debug_data": {
                        "request_id": "req-diag-1",
                        "interaction_timestamp_utc": "2026-04-01T00:00:03Z",
                        "code_version": "v20260401+g1234567",
                        "code_version_details": {
                            "schema_version": "runtime_code_version.v1",
                            "version": "v20260401+g1234567",
                        },
                        "turn_execution_diagnostics": {
                            "request_id": "req-diag-1",
                            "generated_at_utc": "2026-04-01T00:00:03Z",
                            "prompt_preview": "Show the turn diagnostics",
                            "progress_events": [],
                            "activity_history": [],
                            "phase_history": [],
                            "tool_history": [],
                            "stage_diagnostics": [],
                            "workflow_stage_model": {
                                "schema_version": "conversation_turn_stage_model.v1",
                                "stages": [],
                            },
                            "workflow_stage_path": {
                                "schema_version": "conversation_turn_stage_path.v1",
                                "path": [],
                            },
                            "timing_breakdown": {
                                "schema_version": "conversation_turn_timing_breakdown.v1",
                                "stages": [],
                                "llm_calls_by_stage_model": [],
                                "totals": {
                                    "elapsed_ms": 42,
                                    "observed_timeline_ms": 42,
                                    "phase_elapsed_ms": 42,
                                    "llm_elapsed_ms": 0,
                                    "llm_call_count": 0,
                                },
                            },
                        },
                        "workflow_routing_diagnostics": {
                            "schema_version": "workflow_routing_diagnostics.v1",
                            "dispatch": {"dispatch_terminal_status": "completed"},
                        },
                    },
                },
            ],
        }
    ]

    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_chat_history_collection_service",
        lambda read_only=True: _DiagnosticsChatHistoryCollection(chat_docs),
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_records_collection",
        lambda: None,
    )

    result = cat._turn_execution_get_diagnostics(
        namespace="#V#user@org",
        request_id="req-diag-1",
    )

    assert result["success"] is True
    assert result["schema_version"] == "turn_execution_diagnostics.v1"
    assert result["request_id"] == "req-diag-1"
    assert result["chat_session_id"] == "chat-diag-1"
    assert result["source_system"] == "mongo.chat_history"
    assert result["workflow_routing_diagnostics"]["schema_version"] == (
        "workflow_routing_diagnostics.v1"
    )
    assert result["provenance"]["item_kind"] == "turn_execution_diagnostics_item"
    assert result["provenance"]["source_system"] == "mongo.chat_history"


def test_turn_execution_get_diagnostics_reconstructs_from_projection(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    turn_docs = [
        {
            "request_id": "req-lossy-1",
            "session_id": "chat-lossy-1",
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "prompt": {"preview": "Attempted update"},
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "workflow_discovery": {"match_count": 1},
            },
            "workflow_routing_diagnostics": {
                "schema_version": "workflow_routing_diagnostics.v1",
                "dispatch": {"dispatch_terminal_status": "completed"},
            },
            "execution": {
                "tool_invocations": [
                    {"tool_name": "add_relationship", "status": "completed"}
                ],
                "workflow_stage_model": {
                    "schema_version": "conversation_turn_stage_model.v1",
                    "stages": [],
                },
                "workflow_stage_path": {
                    "schema_version": "conversation_turn_stage_path.v1",
                    "path": [],
                },
            },
        }
    ]

    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_chat_history_collection_service",
        lambda read_only=True: None,
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_records_collection",
        lambda: _TurnExecutionCollection(turn_docs),
    )

    result = cat._turn_execution_get_diagnostics(
        namespace="#V#user@org",
        request_id="req-lossy-1",
    )

    assert result["success"] is True
    assert result["request_id"] == "req-lossy-1"
    assert result["source_system"] == "mongo.turn_execution_records"
    assert result["reconstruction"]["lossy"] is True
    assert result["tool_call_count"] == 1
    assert result["workflow_stage_path"]["schema_version"] == (
        "conversation_turn_stage_path.v1"
    )


def test_turn_execution_search_failures_reports_modes_and_recommendations(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "request_id": "req-fail-1",
            "session_id": "chat-fail-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T00:57:25Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "JVNAUTOSCI-1202: Relations were not added"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        },
        {
            "request_id": "req-fail-2",
            "session_id": "chat-fail-2",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:00:10Z",
            "completion_gate": {
                "decision": "failed",
                "decision_reason": "Mutation attempt failed or was blocked.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_9"],
            },
            "required_effects": [{"effect_id": "effect_9", "status": "not_satisfied"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "Proceed with predicate update"},
            "critic": {"summary": {"error_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        },
        {
            "request_id": "req-ok-1",
            "session_id": "chat-ok-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:10:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "plain_response",
            },
            "prompt": {"preview": "What is the current status?"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": True,
            },
        },
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_search_failures(
        namespace="#V#user@org",
        limit=20,
        offset=0,
    )

    assert result["success"] is True
    assert result["collection"] == "turn_execution_records"
    assert result["provenance"]["item_kind"] == "turn_execution_failure_report"
    assert result["returned_count"] == 2
    assert result["likely_failure_count"] == 2
    assert result["failure_mode_counts"]["mutation_not_executed"] == 1
    assert result["failure_mode_counts"]["mutation_failed_or_blocked"] == 1
    assert "req-fail-1" in result["example_request_ids"]
    assert "req-fail-2" in result["example_request_ids"]
    assert any(
        "route through #V#conversation_turn_execution_workflow" in rec
        for rec in result["recommendations"]
    )


def test_turn_execution_build_benchmark_returns_metrics_and_replay_cases(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "request_id": "req-fail-1",
            "session_id": "chat-fail-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T00:57:25Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "JVNAUTOSCI-1202: Relations were not added"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        },
        {
            "request_id": "req-fail-2",
            "session_id": "chat-fail-2",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:00:10Z",
            "completion_gate": {
                "decision": "partial",
                "decision_reason": "Mutation execution observed but verification is inconclusive.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_2"],
            },
            "required_effects": [{"effect_id": "effect_2", "status": "satisfied"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "Proceed with predicate update"},
            "critic": {"summary": {"inconclusive_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        },
        {
            "request_id": "req-ok-1",
            "session_id": "chat-ok-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:10:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "plain_response",
            },
            "prompt": {"preview": "What is the current status?"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": True,
            },
        },
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_build_benchmark(
        namespace="#V#user@org",
        limit=20,
        offset=0,
        max_cases=2,
    )

    assert result["success"] is True
    assert result["collection"] == "turn_execution_records"
    assert result["provenance"]["item_kind"] == "turn_execution_benchmark_report"

    metrics = result.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics["scanned_count"] == 3
    assert metrics["likely_failure_count"] == 2
    assert metrics["likely_failure_rate_pct"] == 66.67
    assert metrics["failure_mode_counts"]["mutation_not_executed"] == 1
    assert isinstance(result.get("benchmark_fingerprint"), str)
    assert len(result["benchmark_fingerprint"]) == 16

    replay_cases = result.get("replay_cases")
    assert isinstance(replay_cases, list)
    assert len(replay_cases) == 2
    first_case = replay_cases[0]
    assert first_case["failure_mode"] == "mutation_not_executed"
    assert first_case["confidence"] == "high"
    assert first_case["pass_criteria"]["action_attempted"] is True
    assert first_case["pass_criteria"]["postcondition_satisfied"] is True
    assert first_case["pass_criteria"]["no_false_success"] is True
    triage = first_case.get("triage")
    assert isinstance(triage, dict)
    assert "JVNAUTOSCI-1202" in triage.get("jira_issue_keys", [])
    assert (
        "https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-1202"
        in triage.get("jira_browse_urls", [])
    )

    seeded_cases = result.get("seeded_cases")
    assert replay_cases == seeded_cases

    triage_index = result.get("triage_index")
    assert isinstance(triage_index, dict)
    assert triage_index["issue_link_count"] == 1
    assert triage_index["issue_links"][0]["issue_key"] == "JVNAUTOSCI-1202"


def test_turn_execution_build_benchmark_flags_regression_when_baseline_is_better(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "request_id": "req-fail-1",
            "session_id": "chat-fail-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T00:57:25Z",
            "completion_gate": {
                "decision": "escalation_required",
                "decision_reason": "Required mutation was not executed.",
                "safe_to_claim_completion": False,
                "requires_follow_up": True,
                "blocking_effect_ids": ["effect_1"],
            },
            "required_effects": [{"effect_id": "effect_1", "status": "not_executed"}],
            "workflow_selection": {
                "selected_workflow_id": "#V#tool_calling_workflow",
                "selector_verdict": "tool_seeking",
            },
            "prompt": {"preview": "Follow up on JVNAUTOSCI-1204 benchmark"},
            "critic": {"summary": {"not_verified_count": 1}},
            "final_response": {
                "completion_claim_detected": True,
                "completion_claim_validated": False,
            },
        },
        {
            "request_id": "req-ok-1",
            "session_id": "chat-ok-1",
            "namespace": "#V#user@org",
            "created_at_utc": "2026-02-19T01:10:00Z",
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "blocking_effect_ids": [],
            },
            "required_effects": [],
            "workflow_selection": {
                "selected_workflow_id": "#V#chat_assistant_workflow",
                "selector_verdict": "plain_response",
            },
            "prompt": {"preview": "What is the current status?"},
            "critic": {"summary": {"not_verified_count": 0}},
            "final_response": {
                "completion_claim_detected": False,
                "completion_claim_validated": True,
            },
        },
    ]

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_build_benchmark(
        namespace="#V#user@org",
        limit=20,
        offset=0,
        baseline_likely_failure_rate_pct=10.0,
        baseline_false_success_rate_pct=5.0,
        baseline_unresolved_follow_up_rate_pct=5.0,
        regression_tolerance_pct=0.5,
    )

    assert result["success"] is True
    regression = result.get("regression_assessment")
    assert isinstance(regression, dict)
    assert regression["baseline_provided"] is True
    assert regression["regression_detected"] is True
    comparisons = regression.get("comparisons")
    assert isinstance(comparisons, list)
    assert any(
        row.get("metric") == "likely_failure_rate_pct" and row.get("regressed") is True
        for row in comparisons
    )


def test_turn_execution_build_benchmark_reports_gap_when_no_records(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    coll = _TurnExecutionCollection([])
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    result = cat._turn_execution_build_benchmark(
        namespace="#V#user@org",
        limit=20,
        offset=0,
        max_cases=5,
    )

    assert result["success"] is True
    metrics = result.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics["scanned_count"] == 0

    capability_gaps = result.get("capability_gaps")
    assert isinstance(capability_gaps, list)
    assert any(gap.get("gap_id") == "no_turn_execution_records" for gap in capability_gaps)


def test_turn_execution_build_benchmark_hesitancy_trace_gateway_e2e(monkeypatch):
    docs = _build_hesitancy_trace_docs()
    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_benchmark",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "max_cases": 5,
            "include_completed": True,
            "baseline_unresolved_follow_up_rate_pct": 70.0,
            "regression_tolerance_pct": 1.0,
        },
    ).payload

    assert payload["success"] is True
    metrics = payload.get("metrics")
    assert isinstance(metrics, dict)
    assert metrics.get("scanned_count") == 3
    assert metrics.get("likely_failure_count") == 2
    assert metrics.get("unresolved_follow_up_count") == 2

    selection_metrics = metrics.get("selection_metrics")
    assert isinstance(selection_metrics, dict)
    assert selection_metrics.get("likely_failure_tool_workflow_count") == 2
    assert selection_metrics.get("likely_failure_plain_response_count") == 0
    assert selection_metrics.get("likely_failure_tool_workflow_rate_pct") == 100.0

    gate_metrics = metrics.get("gate_metrics")
    assert isinstance(gate_metrics, dict)
    assert gate_metrics.get("false_success_count") == 0
    assert gate_metrics.get("requires_follow_up_count") == 2

    retry_metrics = metrics.get("retry_metrics")
    assert isinstance(retry_metrics, dict)
    assert retry_metrics.get("follow_up_count") == 2
    assert retry_metrics.get("follow_up_with_retry_signal_count") == 2
    assert retry_metrics.get("bounded_retry_stop_count") == 1
    assert retry_metrics.get("stall_latency_stop_count") == 1

    user_imposition_metrics = metrics.get("user_imposition_metrics")
    assert isinstance(user_imposition_metrics, dict)
    assert user_imposition_metrics.get("follow_up_turn_count") == 2
    assert user_imposition_metrics.get("follow_up_turn_rate_pct") == 66.67
    assert user_imposition_metrics.get("escalation_signal_count") == 1

    signals = payload.get("benchmark_signals")
    assert isinstance(signals, list)
    signal_by_id = {
        signal.get("signal_id"): signal
        for signal in signals
        if isinstance(signal, dict)
    }
    assert signal_by_id["workflow_selection_prefers_tool_path"]["status"] == "pass"
    assert signal_by_id["completion_gate_false_success_guard"]["status"] == "pass"
    assert signal_by_id["retry_guardrail_signals_recorded"]["status"] == "pass"
    assert signal_by_id["user_imposition_rate_vs_baseline"]["status"] == "pass"

    summary = payload.get("benchmark_signal_summary")
    assert isinstance(summary, dict)
    assert summary.get("fail_count") == 0
    assert summary.get("pass_count", 0) >= 4

    replay_cases = payload.get("replay_cases")
    assert isinstance(replay_cases, list)
    assert replay_cases
    triage = replay_cases[0].get("triage")
    assert isinstance(triage, dict)
    assert "JVNAUTOSCI-1326" in triage.get("jira_issue_keys", [])


def test_turn_execution_build_benchmark_hesitancy_signals_detect_plain_response_regression(
    monkeypatch,
):
    docs = _build_hesitancy_trace_docs()
    docs[0]["workflow_selection"] = {
        "selected_workflow_id": "#V#chat_assistant_workflow",
        "selector_verdict": "plain_response",
    }

    coll = _TurnExecutionCollection(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB({"turn_execution_records": coll}),
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "turn_execution_build_benchmark",
        {
            "namespace": "#V#user@org",
            "limit": 20,
            "offset": 0,
            "max_cases": 5,
            "include_completed": True,
            "baseline_unresolved_follow_up_rate_pct": 70.0,
            "regression_tolerance_pct": 1.0,
        },
    ).payload

    assert payload["success"] is True
    signals = payload.get("benchmark_signals")
    assert isinstance(signals, list)
    signal_by_id = {
        signal.get("signal_id"): signal
        for signal in signals
        if isinstance(signal, dict)
    }
    assert signal_by_id["workflow_selection_prefers_tool_path"]["status"] == "fail"
    details = signal_by_id["workflow_selection_prefers_tool_path"].get("details")
    assert isinstance(details, dict)
    assert details.get("observed_plain_response_count") == 1

    summary = payload.get("benchmark_signal_summary")
    assert isinstance(summary, dict)
    assert summary.get("fail_count", 0) >= 1


def test_turn_execution_backfill_wrapper_returns_provenance(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.backfill_turn_execution_records_from_chat_history",
        lambda **kwargs: {
            "success": True,
            "namespace": kwargs.get("namespace"),
            "dry_run": kwargs.get("dry_run"),
            "synthesise_missing_records": kwargs.get("synthesise_missing_records"),
            "candidate_records": 3,
            "upserted_count": 0,
        },
    )

    result = cat._turn_execution_backfill_from_chat_history(
        namespace="#V#user@org",
        dry_run=True,
        limit_sessions=100,
        synthesise_missing_records=False,
    )

    assert result["success"] is True
    assert result["namespace"] == "#V#user@org"
    assert result["synthesise_missing_records"] is False
    assert result["candidate_records"] == 3
    assert result["provenance"]["item_kind"] == "turn_execution_backfill_report"


def test_turn_execution_namespace_coverage_wrapper_returns_provenance(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.build_turn_execution_namespace_coverage_report",
        lambda **kwargs: {
            "success": True,
            "namespace_filter": kwargs.get("namespace"),
            "namespaces_scanned": 1,
            "coverage_by_namespace": [
                {
                    "namespace": "#V#user@org",
                    "assistant_messages_scanned": 10,
                    "assistant_messages_with_turn_execution_record": 4,
                    "projected_records_total": 4,
                }
            ],
        },
    )

    result = cat._turn_execution_namespace_coverage_report(
        namespace="#V#user@org",
        limit_namespaces=5,
        limit_sessions_per_namespace=50,
        limit_projected_records_per_namespace=500,
    )

    assert result["success"] is True
    assert result["namespace_filter"] == "#V#user@org"
    assert result["namespaces_scanned"] == 1
    assert (
        result["provenance"]["item_kind"]
        == "turn_execution_namespace_coverage_report"
    )
