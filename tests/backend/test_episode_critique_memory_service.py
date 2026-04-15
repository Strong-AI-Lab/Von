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


class _StubCursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = list(docs)

    def sort(self, field: str, direction: int):
        reverse = direction < 0
        self._docs = sorted(
            self._docs,
            key=lambda item: item.get(field) or "",
            reverse=reverse,
        )
        return self

    def limit(self, value: int):
        self._docs = self._docs[:value]
        return self

    def __iter__(self):
        return iter(self._docs)


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

    def find(self, query, projection=None):
        namespace = query.get("namespace")
        workflow_candidates = {
            clause_value
            for clause in query.get("$or", [])
            for clause_key, clause_value in clause.items()
            if clause_key
            in {"workflow_id", "implicated_workflow_ids", "improvement_target_workflow_ids"}
        }
        docs = []
        for doc in self.docs.values():
            if namespace and doc.get("namespace") != namespace:
                continue
            if int(doc.get("improvement_suggestion_count") or 0) <= 0:
                continue
            direct_workflow_id = doc.get("workflow_id")
            implicated_ids = set(doc.get("implicated_workflow_ids") or [])
            target_ids = set(doc.get("improvement_target_workflow_ids") or [])
            if workflow_candidates and not (
                direct_workflow_id in workflow_candidates
                or implicated_ids.intersection(workflow_candidates)
                or target_ids.intersection(workflow_candidates)
            ):
                continue
            if projection:
                projected = {}
                for key, include in projection.items():
                    if key == "_id" or not include:
                        continue
                    projected[key] = doc.get(key)
                docs.append(projected)
            else:
                docs.append(dict(doc))
        return _StubCursor(docs)


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
        "critic": {
            "verdict": "pass",
            "confidence": 1.0,
            "unresolved_check_count": 0,
            "format_over_content_diagnostic": {
                "status": "suspected",
                "summary": "Structured output may have displaced nuance.",
                "confidence": 0.67,
                "reason_codes": [
                    "structured_output_contract_present",
                    "grounded_tool_path_unused",
                ],
                "selected_model": "gpt-5.4-mini",
                "selected_provider": "openai",
                "observed_stage_id": "selector_decision",
                "raw_response_format": "json_object",
            },
        },
        "implicated": {
            "workflow_ids": ["#V#workflow"],
            "tool_names": ["search_concepts"],
            "concept_ids": ["#V#paper"],
        },
        "remediation": {
            "task_ids": ["#V#task_123"],
            "jira_issue_keys": ["JVNAUTOSCI-999"],
        },
        "routing": {
            "decision": "task_and_jira",
            "reason_codes": ["repeat_threshold_met"],
            "fingerprint": "route-fingerprint-123",
            "repeat_count": 3,
            "task_action": "created_new",
            "jira_action": "created_new",
        },
        "recommendations": ["No remediation is currently indicated by the recorded critic evidence."],
        "improvement_suggestions": [
            {
                "suggestion_id": "tool_addition_alpha",
                "category": "tool_addition",
                "priority": "high",
                "target_surface": "tool",
                "target_workflow_id": "#V#workflow",
                "target_tool_name": "search_web",
                "title": "Add missing web search support",
                "rationale": "The episode lacked a required external lookup.",
                "suggested_change": "Add a reusable web-search tool surface.",
                "evidence_refs": ["capability_gaps:missing_search_tool"],
                "recursion_level": 0,
            }
        ],
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
    assert stored["routing_decision"] == "task_and_jira"
    assert stored["routing_fingerprint"] == "route-fingerprint-123"
    assert stored["improvement_suggestion_count"] == 1
    assert stored["improvement_suggestion_categories"] == ["tool_addition"]
    assert stored["improvement_target_tool_names"] == ["search_web"]
    assert stored["format_over_content_status"] == "suspected"
    assert stored["format_over_content_confidence"] == 0.67
    assert stored["format_over_content_reason_codes"] == [
        "structured_output_contract_present",
        "grounded_tool_path_unused",
    ]
    assert stored["format_over_content_model"] == "gpt-5.4-mini"
    assert stored["format_over_content_provider"] == "openai"
    assert stored["format_over_content_stage_id"] == "selector_decision"
    assert stored["format_over_content_raw_response_format"] == "json_object"


def test_build_episode_assessment_state_normalises_improvement_suggestions():
    from src.backend.services import episode_critique_memory_service as svc

    state = svc.build_episode_critique_memory_state_from_episode_assessment(
        evidence_bundle={
            "episode_locator": {
                "request_id": "req-1838",
                "workflow_id": "#V#alpha_workflow",
                "namespace": "#V#user@org",
            },
            "capability_gaps": [],
        },
        assessment={
            "verdict": "fail",
            "summary": "The episode selected the wrong route and lacked a needed tool.",
            "improvement_suggestions": [
                {
                    "category": "workflow_fix",
                    "priority": "HIGH",
                    "target_surface": "workflow_definition",
                    "title": "Repair routing policy",
                    "rationale": "Another eligible workflow should have won.",
                    "suggested_change": "Tighten routing exemplars for #V#alpha_workflow.",
                    "evidence_refs": ["expected_context.routing_quality_signals"],
                },
                {
                    "category": "critic_improvement",
                    "target_surface": "critic",
                    "title": "Keep critic self-repair bounded",
                    "rationale": "The critique should not recurse indefinitely.",
                    "suggested_change": "Restrict critic follow-up to one level.",
                    "evidence_refs": ["episode_locator.request_id"],
                    "recursion_level": 7,
                },
            ],
        },
    )

    assert state is not None
    suggestions = state["improvement_suggestions"]
    assert len(suggestions) == 2
    assert suggestions[0]["category"] == "workflow_change"
    assert suggestions[0]["priority"] == "high"
    assert suggestions[0]["target_surface"] == "workflow"
    assert suggestions[0]["target_workflow_id"] == "#V#alpha_workflow"
    assert suggestions[1]["category"] == "critic_self_improvement"
    assert suggestions[1]["target_surface"] == "episode_critic"
    assert suggestions[1]["recursion_level"] == 1


def test_build_episode_assessment_state_preserves_format_over_content_diagnostic():
    from src.backend.services import episode_critique_memory_service as svc

    state = svc.build_episode_critique_memory_state_from_episode_assessment(
        evidence_bundle={
            "episode_locator": {
                "request_id": "req-1890",
                "workflow_id": "#V#chat_assistant_workflow",
                "namespace": "#V#user@org",
            },
            "format_over_content_diagnostic": {
                "status": "suspected",
                "summary": "Structured output may have displaced nuance.",
                "confidence": 0.72,
                "reason_codes": [
                    "structured_output_contract_present",
                    "grounded_tool_path_unused",
                ],
                "selected_model": "gpt-5.4-mini",
                "observed_stage_id": "selector_decision",
                "raw_response_format": "json_object",
            },
            "capability_gaps": [],
        },
        assessment={
            "verdict": "fail",
            "summary": "The response was tidy but insufficiently grounded.",
        },
    )

    assert state is not None
    diagnostic = state["critic"]["format_over_content_diagnostic"]
    assert diagnostic["status"] == "suspected"
    assert diagnostic["confidence"] == 0.72
    assert diagnostic["reason_codes"] == [
        "structured_output_contract_present",
        "grounded_tool_path_unused",
    ]
    assert diagnostic["selected_model"] == "gpt-5.4-mini"
    assert diagnostic["observed_stage_id"] == "selector_decision"
    assert diagnostic["raw_response_format"] == "json_object"


def test_list_recent_workflow_improvement_suggestions_filters_and_flattens(monkeypatch):
    from src.backend.services import episode_critique_memory_service as svc

    coll = _StubCollection()
    coll.docs["#V#episode_critique_memory_1"] = {
        "memory_id": "#V#episode_critique_memory_1",
        "request_id": "req-1838-a",
        "workflow_id": "#V#alpha_workflow",
        "namespace": "#V#user@org",
        "created_at_utc": "2026-04-13T04:00:00Z",
        "verdict": "fail",
        "critic_summary_text": "Routing failed.",
        "receipt_hash": "receipt-a",
        "improvement_suggestion_count": 1,
        "improvement_target_workflow_ids": ["#V#alpha_workflow"],
        "improvement_suggestions": [
            {
                "suggestion_id": "workflow_change_alpha",
                "category": "workflow_change",
                "priority": "high",
                "target_surface": "workflow",
                "target_workflow_id": "#V#alpha_workflow",
                "title": "Repair routing policy",
                "rationale": "Routing selected the wrong workflow.",
                "suggested_change": "Tighten routing exemplars.",
                "evidence_refs": ["expected_context.routing_quality_signals"],
                "recursion_level": 0,
            }
        ],
    }
    coll.docs["#V#episode_critique_memory_2"] = {
        "memory_id": "#V#episode_critique_memory_2",
        "request_id": "req-1838-b",
        "workflow_id": "#V#beta_workflow",
        "namespace": "#V#user@org",
        "created_at_utc": "2026-04-13T05:00:00Z",
        "verdict": "fail",
        "critic_summary_text": "Other workflow failure.",
        "receipt_hash": "receipt-b",
        "improvement_suggestion_count": 1,
        "improvement_target_workflow_ids": ["#V#beta_workflow"],
        "improvement_suggestions": [
            {
                "suggestion_id": "workflow_change_beta",
                "category": "workflow_change",
                "priority": "medium",
                "target_surface": "workflow",
                "target_workflow_id": "#V#beta_workflow",
                "title": "Repair beta routing",
                "rationale": "Different workflow.",
                "suggested_change": "Adjust beta routing metadata.",
                "evidence_refs": ["episode_locator.request_id"],
                "recursion_level": 0,
            }
        ],
    }
    monkeypatch.setattr(svc, "get_episode_critique_memories_collection", lambda: coll)

    suggestions = svc.list_recent_workflow_improvement_suggestions(
        "#V#alpha_workflow",
        namespace="#V#user@org",
    )

    assert len(suggestions) == 1
    assert suggestions[0]["memory_id"] == "#V#episode_critique_memory_1"
    assert suggestions[0]["request_id"] == "req-1838-a"
    assert suggestions[0]["critic_summary_text"] == "Routing failed."


def test_record_episode_critique_memory_routing_merges_remediation_links(monkeypatch):
    from src.backend.services import episode_critique_memory_service as svc

    base_state = {
        "memory_id": "#V#episode_critique_memory_abc",
        "namespace": "#V#user@org",
        "user_id": "#V#user",
        "org_id": "#V#org",
        "remediation": {
            "task_ids": ["#V#task_existing"],
            "jira_issue_keys": ["JVNAUTOSCI-1700"],
        },
    }
    persisted_payloads: list[dict] = []

    monkeypatch.setattr(svc, "get_episode_critique_memory_state", lambda _memory_id: base_state)
    monkeypatch.setattr(
        svc,
        "_persist_episode_critique_memory_state",
        lambda **kwargs: persisted_payloads.append(kwargs["state"]) or kwargs["state"],
    )
    monkeypatch.setattr(
        svc,
        "upsert_episode_critique_memory_projection",
        lambda **kwargs: {"updated": True, "record": kwargs["record"]},
    )

    outcome = svc.record_episode_critique_memory_routing(
        memory_id="#V#episode_critique_memory_abc",
        routing={
            "decision": "task_and_jira",
            "reason_codes": ["repeat_threshold_met"],
            "fingerprint": "route-fingerprint-123",
            "repeat_count": 3,
        },
        remediation_task_ids=["#V#task_existing", "#V#task_new"],
        remediation_issue_keys=["JVNAUTOSCI-1700", "JVNAUTOSCI-1710"],
    )

    assert outcome["success"] is True
    assert persisted_payloads
    updated_state = persisted_payloads[-1]
    assert updated_state["routing"]["decision"] == "task_and_jira"
    assert updated_state["remediation"]["task_ids"] == [
        "#V#task_existing",
        "#V#task_new",
    ]
    assert updated_state["remediation"]["jira_issue_keys"] == [
        "JVNAUTOSCI-1700",
        "JVNAUTOSCI-1710",
    ]


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
