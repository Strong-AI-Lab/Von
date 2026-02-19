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


class _DB:
    def __init__(self, collections: dict[str, Any]):
        self._collections = dict(collections)

    def __getitem__(self, key: str):
        return self._collections[key]


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
            "prompt": {"preview": "Relations were not added"},
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
            "prompt": {"preview": "Relations were not added"},
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
