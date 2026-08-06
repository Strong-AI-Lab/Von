"""Tests for the Thinking-card LLM timestamp capture (JVNAUTOSCI-2385) and the
default-mode progress precedence contract (JVNAUTOSCI-2381).

These cover the support-surface helpers only: timestamp stamping, exchange
timestamp extraction, the evidence-source lineage, and the precedence contract
projection. No decision policy lives in these helpers.
"""

from __future__ import annotations

from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# JVNAUTOSCI-2385: timestamp capture on LLM call entries
# ---------------------------------------------------------------------------


def test_stamp_llm_call_timestamps_sets_canonical_keys():
    from src.backend.workflows.llm_call_telemetry import stamp_llm_call_timestamps

    entry: dict = {}
    result = stamp_llm_call_timestamps(entry, duration_ms=1500)

    assert result is entry
    assert "completed_at_utc" in entry
    assert "started_at_utc" in entry
    assert entry.get("at_utc") == entry["completed_at_utc"]

    completed = datetime.fromisoformat(entry["completed_at_utc"])
    started = datetime.fromisoformat(entry["started_at_utc"])
    # started should precede completed by ~the duration when duration is given.
    assert started < completed
    delta_ms = (completed - started).total_seconds() * 1000.0
    assert 1400 <= delta_ms <= 1600


def test_stamp_llm_call_timestamps_without_duration_mirrors_completion():
    from src.backend.workflows.llm_call_telemetry import stamp_llm_call_timestamps

    entry: dict = {}
    stamp_llm_call_timestamps(entry)

    assert entry["started_at_utc"] == entry["completed_at_utc"]
    assert entry["at_utc"] == entry["completed_at_utc"]


def test_stamp_llm_call_timestamps_preserves_existing_at_utc():
    from src.backend.workflows.llm_call_telemetry import stamp_llm_call_timestamps

    preset = datetime(2026, 6, 1, 3, 4, 5, tzinfo=timezone.utc).isoformat()
    entry: dict = {"at_utc": preset}
    stamp_llm_call_timestamps(entry, duration_ms=200)

    # setdefault must not clobber an already-recorded canonical timestamp.
    assert entry["at_utc"] == preset
    # completion is still freshly stamped.
    assert entry["completed_at_utc"] != preset


def test_stamp_llm_call_timestamps_rejects_negative_and_bool_duration():
    from src.backend.workflows.llm_call_telemetry import stamp_llm_call_timestamps

    negative: dict = {}
    stamp_llm_call_timestamps(negative, duration_ms=-50)
    assert negative["started_at_utc"] == negative["completed_at_utc"]

    boolean: dict = {}
    # bool is an int subclass; must not be treated as a real duration.
    stamp_llm_call_timestamps(boolean, duration_ms=True)  # type: ignore[arg-type]
    assert boolean["started_at_utc"] == boolean["completed_at_utc"]


# ---------------------------------------------------------------------------
# JVNAUTOSCI-2385: exchange timestamp extraction + payload counter
# ---------------------------------------------------------------------------


def test_extract_llm_exchange_timestamps_uses_canonical_keys():
    from src.backend.services.turn_execution_diagnostics_service import (
        _extract_llm_exchange_timestamps,
    )

    entry = {
        "completed_at_utc": "2026-06-01T03:04:05.123456+00:00",
        "started_at_utc": "2026-06-01T03:04:03.000000+00:00",
    }
    result = _extract_llm_exchange_timestamps(entry)

    assert result["completed_at_utc"] == "2026-06-01T03:04:05.123456+00:00"
    assert result["started_at_utc"] == "2026-06-01T03:04:03.000000+00:00"
    assert result["at_utc"] == "2026-06-01T03:04:05.123456+00:00"


def test_extract_llm_exchange_timestamps_falls_back_to_alternative_keys():
    from src.backend.services.turn_execution_diagnostics_service import (
        _extract_llm_exchange_timestamps,
    )

    entry = {
        "timestamp": "2026-06-01T05:00:00+00:00",
        "requested_at_utc": "2026-06-01T04:59:58+00:00",
    }
    result = _extract_llm_exchange_timestamps(entry)

    assert result["completed_at_utc"] == "2026-06-01T05:00:00+00:00"
    assert result["started_at_utc"] == "2026-06-01T04:59:58+00:00"
    assert result["at_utc"] == "2026-06-01T05:00:00+00:00"


def test_extract_llm_exchange_timestamps_empty_when_absent():
    from src.backend.services.turn_execution_diagnostics_service import (
        _extract_llm_exchange_timestamps,
    )

    assert _extract_llm_exchange_timestamps({}) == {}


def test_normalise_llm_exchange_entry_surfaces_timestamps():
    from src.backend.services.turn_execution_diagnostics_service import (
        _normalise_llm_exchange_entry,
    )

    entry = {
        "type": "llm.generate",
        "model": "gpt-5.4-mini",
        "completed_at_utc": "2026-06-01T03:04:05.123456+00:00",
        "started_at_utc": "2026-06-01T03:04:03.000000+00:00",
        "prompt_capture": {"text": "EXACT PROMPT", "is_truncated": False},
        "response_capture": {"text": "EXACT RESPONSE", "is_truncated": False},
    }
    payload = _normalise_llm_exchange_entry(entry, source="unit", source_index=0)

    assert payload["at_utc"] == "2026-06-01T03:04:05.123456+00:00"
    assert payload["started_at_utc"] == "2026-06-01T03:04:03.000000+00:00"
    assert payload["completed_at_utc"] == "2026-06-01T03:04:05.123456+00:00"
    assert payload["model"] == "gpt-5.4-mini"


def test_normalise_llm_exchange_entry_accepts_preview_and_selected_model_aliases():
    from src.backend.services.turn_execution_diagnostics_service import (
        _normalise_llm_exchange_entry,
    )

    entry = {
        "entry_type": "live_llm_request",
        "stage": "buttonify",
        "selected_model": "gpt-5.4-mini",
        "selected_provider": "openai",
        "llm_request_sent_at_utc": "2026-06-02T16:45:51.542809Z",
        "llm_first_output_at_utc": "2026-06-02T16:45:54.356959Z",
        "prompt_preview": {
            "text": "EXACT QUICK REPLY PROMPT",
            "char_count": 24,
            "is_truncated": False,
        },
        "response_preview": {
            "text": "[\"Continue\", \"Stop\"]",
            "char_count": 20,
            "is_truncated": False,
        },
    }

    payload = _normalise_llm_exchange_entry(entry, source="unit", source_index=0)

    assert payload["call_type"] == "live_llm_request"
    assert payload["model"] == "gpt-5.4-mini"
    assert payload["provider"] == "openai"
    assert payload["prompt"]["text"] == "EXACT QUICK REPLY PROMPT"
    assert payload["response"]["text"] == "[\"Continue\", \"Stop\"]"
    assert payload["started_at_utc"] == "2026-06-02T16:45:51.542809Z"
    assert payload["at_utc"] == "2026-06-02T16:45:54.356959Z"


def test_normalise_llm_exchange_entry_preserves_usage_and_model_lineage():
    from src.backend.services.turn_execution_diagnostics_service import (
        _normalise_llm_exchange_entry,
    )

    payload = _normalise_llm_exchange_entry(
        {
            "call_id": "call-2624",
            "type": "adaptive_turn_model_call",
            "provider": "openai",
            "requested_model": "requested-model",
            "selected_model": "selected-model",
            "effective_model": "effective-model",
            "model_identity_source": "provider_response",
            "provider_request_sent": True,
            "usage": {
                "status": "reported",
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        },
        source="unit",
        source_index=0,
    )

    assert payload["call_id"] == "call-2624"
    assert payload["model"] == "effective-model"
    assert payload["requested_model"] == "requested-model"
    assert payload["selected_model"] == "selected-model"
    assert payload["effective_model"] == "effective-model"
    assert payload["usage"]["total_tokens"] == 120


def test_collect_llm_exchange_entries_salvages_embedded_stage_summaries():
    from src.backend.services.turn_execution_diagnostics_service import (
        _collect_llm_exchange_entries,
    )

    llm_debug = {
        "turn_execution_diagnostics": {
            "stage_diagnostics": [
                {
                    "stage_id": "selected_workflow_execution",
                    "llm_exchange_summaries": [
                        {
                            "entry_type": "live_llm_request",
                            "selected_model": "gpt-5.4-mini",
                            "prompt_preview": {"text": "PROMPT", "char_count": 6},
                            "response_preview": {"text": "RESPONSE", "char_count": 8},
                            "llm_first_output_at_utc": "2026-06-02T16:45:54Z",
                        }
                    ],
                }
            ]
        }
    }

    entries = _collect_llm_exchange_entries(llm_debug=llm_debug, turn_record=None)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["sequence_no"] == 1
    assert entry["stage"] == "selected_workflow_execution"
    assert entry["workflow_stage_id"] == "selected_workflow_execution"
    assert entry["prompt"]["text"] == "PROMPT"
    assert entry["response"]["text"] == "RESPONSE"


def test_collect_llm_exchange_entries_filters_non_llm_embedded_aux_entries():
    from src.backend.services.turn_execution_diagnostics_service import (
        _collect_llm_exchange_entries,
    )

    llm_debug = {
        "turn_execution_diagnostics": {
            "aux_llm_calls": [
                {"type": "workflow_execution_trace", "workflow_id": "#V#demo"},
                {"type": "workflow_stage", "stage": "screen_backfill"},
                {
                    "type": "workflow_selector",
                    "stage": "selector_preparation",
                    "prompt": {"text": "SELECTOR PROMPT"},
                    "response": {"text": "SELECTOR RESPONSE"},
                },
            ]
        }
    }

    entries = _collect_llm_exchange_entries(llm_debug=llm_debug, turn_record=None)

    assert len(entries) == 1
    assert entries[0]["call_type"] == "workflow_selector"
    assert entries[0]["prompt"]["text"] == "SELECTOR PROMPT"


def test_collect_llm_exchange_entries_filters_skipped_pseudo_calls():
    from src.backend.services.turn_execution_diagnostics_service import (
        _collect_llm_exchange_entries,
    )

    llm_debug = {
        "llm_interaction": {
            "calls": [
                {
                    "type": "llm.generate_skipped",
                    "stage": "narration",
                    "model": "gemma4:31b",
                },
                {
                    "type": "llm.generate",
                    "stage": "narration",
                    "model": "gemma4:31b",
                    "prompt": {"text": "PROMPT"},
                    "response": {"text": "RESPONSE"},
                },
            ]
        }
    }

    entries = _collect_llm_exchange_entries(llm_debug=llm_debug, turn_record=None)

    assert len(entries) == 1
    assert entries[0]["call_type"] == "llm.generate"
    assert entries[0]["prompt"]["text"] == "PROMPT"


def test_finalise_llm_debug_info_persists_primary_calls_and_cost_summary(monkeypatch):
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(von_routes, "get_model_registry_snapshot", lambda: {})

    llm_call = {
        "call_id": "req-2385:llm:1",
        "type": "llm.generate",
        "stage": "screen_backfill",
        "provider": "openai",
        "effective_model": "gpt-test",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "prompt": {"text": "PROMPT"},
        "response": {"text": "RESPONSE"},
    }
    result = von_routes._finalise_llm_debug_info(
        llm_debug_info={
            "request_id": "req-2385",
            "interaction_timestamp_utc": "2026-06-02T16:45:00Z",
            "llm_interaction": {"calls": [llm_call]},
            "aux_llm_calls": [],
            "tool_invocations": [],
        },
        prompt_text="User prompt",
        response_text="Assistant response",
        session_id="session-1",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
    )

    assert result["turn_execution_record"]["request_id"] == "req-2385"
    assert result["turn_execution_record"]["llm_calls"] == [llm_call]
    summary = result["turn_execution_record"]["llm_usage_cost_summary"]
    assert summary["usage"]["total_tokens"] == 15
    assert summary["estimated_cost"]["status"] == "unavailable"


def test_build_turn_execution_record_persists_primary_and_aux_llm_logs():
    from src.backend.services.turn_execution_record_service import (
        build_turn_execution_record,
    )

    llm_call = {
        "type": "llm.generate",
        "stage": "narration",
        "model": "gpt-5.4-mini",
        "prompt": {"text": "PROMPT"},
        "response": {"text": "RESPONSE"},
    }
    aux_call = {
        "type": "workflow_selector",
        "stage": "selector_preparation",
        "prompt": {"text": "SELECTOR PROMPT"},
        "response": {"text": "SELECTOR RESPONSE"},
    }

    record = build_turn_execution_record(
        request_id="req-2385",
        session_id="session-1",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="User prompt",
        response_text="Assistant response",
        interaction_timestamp_utc="2026-06-02T16:45:00Z",
        llm_calls=[llm_call],
        aux_llm_calls=[aux_call],
        tool_invocations=[],
    )

    assert record["llm_calls"] == [llm_call]
    assert record["aux_llm_calls"] == [aux_call]
    assert record["execution"]["llm_calls"] == [llm_call]
    assert record["execution"]["aux_llm_calls"] == [aux_call]


# ---------------------------------------------------------------------------
# JVNAUTOSCI-2381: evidence lineage + precedence contract
# ---------------------------------------------------------------------------


def test_selected_workflow_event_preserves_effect_and_recovery_meaning():
    from src.backend.server.routes.von_routes import (
        _normalise_selected_workflow_execution_event,
    )

    event = _normalise_selected_workflow_execution_event(
        {
            "status": "tool_completed",
            "selected_workflow_execution_event": {
                "status": "workflow_execution_failed",
                "event_kind": "workflow_execution_failed",
                "workflow_id": "#V#student_research_description_workflow",
                "selected_workflow_name": (
                    "Student research description workflow"
                ),
                "effect_status": "failed",
                "mutation_outcome": "partial",
                "outcome_finality": "terminal_for_turn",
                "semantic_effect": True,
                "changed": False,
                "next_action": "Inspect workflow instance",
            },
        },
        sequence_no=7,
        at_utc="2026-08-06T10:00:00Z",
    )

    assert event is not None
    assert event["effect_status"] == "failed"
    assert event["mutation_outcome"] == "partial"
    assert event["outcome_finality"] == "terminal_for_turn"
    assert event["semantic_effect"] is True
    assert event["changed"] is False
    assert event["next_action"] == "Inspect workflow instance"


def test_selected_workflow_evidence_sources_collects_lineage():
    from src.backend.server.routes.von_routes import _selected_workflow_evidence_sources

    payload = {
        "selected_workflow_id": "#V#demo",
        "workflow_routing_diagnostics": {
            "selected_workflow_id": "#V#demo",
            "dispatch": {"dispatch_workflow_id": "#V#demo"},
        },
        "selected_workflow_execution": {"workflow_id": "#V#demo"},
        "workflow_stage_path": {"path": ["selector", "dispatch"]},
    }
    sources = _selected_workflow_evidence_sources(payload)

    assert "progress.selected_workflow_id" in sources
    assert "workflow_routing_diagnostics.selected_workflow_id" in sources
    assert "workflow_routing_diagnostics.dispatch.dispatch_workflow_id" in sources
    assert "selected_workflow_execution" in sources
    assert "workflow_stage_path" in sources


def test_selected_workflow_evidence_sources_empty_when_no_evidence():
    from src.backend.server.routes.von_routes import _selected_workflow_evidence_sources

    assert _selected_workflow_evidence_sources({}) == []


def test_precedence_contract_foregrounds_selected_workflow():
    from src.backend.server.routes.von_routes import (
        _build_thinking_progress_precedence_contract,
    )

    contract = _build_thinking_progress_precedence_contract(
        selected_workflow_id="#V#demo",
        has_tool_evidence=True,
        blocker_summary=None,
        post_processing_only=True,
        evidence_sources=["progress.selected_workflow_id"],
    )

    assert contract["schema_version"] == "thinking_progress_contract.v1"
    assert (
        contract["precedence_rule"] == "selected_workflow_execution_before_finalisation"
    )
    precedence = contract["default_row_precedence"]
    assert precedence[0] == "selected_workflow_execution"
    assert precedence.index("selected_workflow_execution") < precedence.index(
        "finalisation"
    )
    assert contract["selected_workflow_evidence_present"] is True
    assert contract["finalisation_demoted"] is True
    assert contract["selected_workflow_evidence_sources"] == [
        "progress.selected_workflow_id"
    ]


def test_precedence_contract_includes_blocker_before_finalisation():
    from src.backend.server.routes.von_routes import (
        _build_thinking_progress_precedence_contract,
    )

    contract = _build_thinking_progress_precedence_contract(
        selected_workflow_id="#V#demo",
        has_tool_evidence=False,
        blocker_summary="Awaiting required tool",
        post_processing_only=False,
        evidence_sources=["progress.selected_workflow_id"],
    )

    precedence = contract["default_row_precedence"]
    assert contract["blocker_present"] is True
    assert precedence.index("blocker_next_action") < precedence.index("finalisation")
    assert contract["finalisation_demoted"] is False


def test_precedence_contract_without_selected_workflow_uses_tool_then_finalisation():
    from src.backend.server.routes.von_routes import (
        _build_thinking_progress_precedence_contract,
    )

    contract = _build_thinking_progress_precedence_contract(
        selected_workflow_id=None,
        has_tool_evidence=True,
        blocker_summary=None,
        post_processing_only=False,
        evidence_sources=[],
    )

    assert contract["selected_workflow_evidence_present"] is False
    precedence = contract["default_row_precedence"]
    assert precedence[0] == "tool_execution"
    assert "selected_workflow_execution" not in precedence


def test_interpretability_payload_includes_progress_contract():
    from src.backend.server.routes.von_routes import (
        _build_thinking_interpretability_payload,
    )

    payload = {
        "stage": "response_finalising",
        "selected_workflow_id": "#V#demo",
        "selected_workflow_name": "Demo",
        "tool_history": [{"tool": "fetch_concept"}],
    }
    interpretability = _build_thinking_interpretability_payload(payload)

    contract = interpretability.get("progress_contract")
    assert isinstance(contract, dict)
    assert contract["selected_workflow_evidence_present"] is True
    assert (
        "progress.selected_workflow_id"
        in contract["selected_workflow_evidence_sources"]
    )


# ---------------------------------------------------------------------------
# JVNAUTOSCI-2385: exchange blob hydration surfaces the exact prompt/response
# ---------------------------------------------------------------------------


def _write_exchange_blob(
    tmp_path,
    *,
    prompt,
    response,
    model="gpt-5.4-mini",
    extra=None,
    first_output_at_utc="2026-06-02T14:09:20.365608+00:00",
):
    """Write an exchange blob against a local store and return its ref."""

    import os

    from src.backend.services.llm_exchange_blob_writer import (
        get_llm_exchange_blob_writer_from_env,
        reset_llm_exchange_blob_writer_singleton_for_tests,
    )

    os.environ["VON_BLOB_STORE_BACKEND"] = "local"
    os.environ["VON_BLOB_STORE_LOCAL_ROOT"] = str(tmp_path / "blob_store")
    # Ensure the shared store is used (no dedicated container/bucket/root).
    for key in (
        "VON_LLM_EXCHANGE_CONTAINER",
        "VON_LLM_EXCHANGE_BUCKET",
        "VON_LLM_EXCHANGE_LOCAL_ROOT",
        "VON_LLM_EXCHANGE_DISABLE",
    ):
        os.environ.pop(key, None)
    reset_llm_exchange_blob_writer_singleton_for_tests()

    writer = get_llm_exchange_blob_writer_from_env()
    assert writer is not None
    ref = writer.write(
        turn_execution_id="turn-test",
        stage="llm.action",
        workflow_stage_id="recovery_decision",
        call_type="llm.generate",
        model=model,
        provider="openai",
        request_prompt=prompt,
        request_context=None,
        response=response,
        prepared_at_utc="2026-06-02T14:09:18.000000+00:00",
        sent_at_utc="2026-06-02T14:09:19.000000+00:00",
        first_output_at_utc=first_output_at_utc,
        extra=extra,
    )
    assert "error" not in ref, ref
    return ref


def test_read_llm_exchange_blob_ref_roundtrips_exact_bodies(tmp_path):
    from src.backend.services.llm_exchange_blob_writer import (
        read_llm_exchange_blob_ref,
    )

    ref = _write_exchange_blob(
        tmp_path,
        prompt="EXACT PROMPT TEXT",
        response={"text": "EXACT RESPONSE TEXT"},
    )
    body = read_llm_exchange_blob_ref(ref)

    assert isinstance(body, dict)
    assert body["request"]["prompt"] == "EXACT PROMPT TEXT"
    assert body["response"] == {"text": "EXACT RESPONSE TEXT"}
    assert body["model"] == "gpt-5.4-mini"


def test_read_llm_exchange_blob_ref_returns_none_for_unusable_ref():
    from src.backend.services.llm_exchange_blob_writer import (
        read_llm_exchange_blob_ref,
    )

    assert read_llm_exchange_blob_ref({}) is None
    assert read_llm_exchange_blob_ref({"error": "boom"}) is None
    assert read_llm_exchange_blob_ref({"key": "   "}) is None


def test_hydrate_llm_exchange_blob_into_entry_surfaces_exact_exchange(tmp_path):
    from src.backend.services.turn_execution_diagnostics_service import (
        _hydrate_llm_exchange_blob_into_entry,
    )

    ref = _write_exchange_blob(
        tmp_path,
        prompt="# prompt_turn_execution_recovery_decision\nEXACT PROMPT",
        response={"text": '{"turn_next_action": {"action_type": "respond"}}'},
    )
    entry = {
        "call_type": "llm.generate",
        "stage": "llm.action",
        "exchange_blob_ref": dict(ref),
        "unavailable_reason": "exchange_in_blob_ref",
    }
    _hydrate_llm_exchange_blob_into_entry(entry)

    assert entry["prompt"]["text"].startswith(
        "# prompt_turn_execution_recovery_decision"
    )
    assert "respond" in entry["response"]["text"]
    assert entry["model"] == "gpt-5.4-mini"
    assert entry["provider"] == "openai"
    assert entry["at_utc"] == "2026-06-02T14:09:20.365608+00:00"
    assert entry["started_at_utc"] == "2026-06-02T14:09:19.000000+00:00"
    assert entry["exchange_source"] == "blob_ref_hydrated"
    # The unavailable marker is cleared once both bodies are present.
    assert "unavailable_reason" not in entry
    assert entry["prompt_recorded"] is True
    assert entry["response_recorded"] is True


def test_hydrate_failed_llm_exchange_blob_surfaces_failure_metadata(tmp_path):
    from src.backend.services.turn_execution_diagnostics_service import (
        _hydrate_llm_exchange_blob_into_entry,
    )

    ref = _write_exchange_blob(
        tmp_path,
        prompt="FAILED PROMPT",
        response=None,
        extra={
            "status": "failed",
            "success": False,
            "failure": {
                "error": "LLM timed out after 120s",
                "error_class": "TimeoutError",
                "failure_kind": "candidate_error",
            },
        },
        first_output_at_utc=None,
    )
    entry = {
        "call_type": "llm.generate",
        "stage": "response_finalising",
        "exchange_blob_ref": dict(ref),
        "unavailable_reason": "exchange_in_blob_ref",
    }
    _hydrate_llm_exchange_blob_into_entry(entry)

    assert entry["prompt"]["text"] == "FAILED PROMPT"
    assert "response" not in entry
    assert entry["prompt_recorded"] is True
    assert entry.get("response_recorded") is not True
    assert entry["status"] == "failed"
    assert entry["success"] is False
    assert entry["error_class"] == "TimeoutError"
    assert entry["failure_kind"] == "candidate_error"
    assert entry["unavailable_reason"] == "failed_before_response"


def test_hydrate_llm_exchange_blob_into_entry_noop_without_ref():
    from src.backend.services.turn_execution_diagnostics_service import (
        _hydrate_llm_exchange_blob_into_entry,
    )

    entry = {"call_type": "llm.generate", "stage": "llm.action"}
    _hydrate_llm_exchange_blob_into_entry(entry)

    assert "prompt" not in entry
    assert "response" not in entry
    assert "exchange_source" not in entry


def test_hydrate_llm_exchange_blob_into_entry_preserves_existing_bodies(tmp_path):
    from src.backend.services.turn_execution_diagnostics_service import (
        _hydrate_llm_exchange_blob_into_entry,
    )

    ref = _write_exchange_blob(
        tmp_path,
        prompt="BLOB PROMPT",
        response={"text": "BLOB RESPONSE"},
    )
    entry = {
        "call_type": "llm.generate",
        "exchange_blob_ref": dict(ref),
        "prompt": {"text": "INLINE PROMPT", "char_count": 12, "is_truncated": False},
        "response": {
            "text": "INLINE RESPONSE",
            "char_count": 15,
            "is_truncated": False,
        },
    }
    _hydrate_llm_exchange_blob_into_entry(entry)

    # Already-present inline bodies must not be overwritten from the blob.
    assert entry["prompt"]["text"] == "INLINE PROMPT"
    assert entry["response"]["text"] == "INLINE RESPONSE"
    assert "exchange_source" not in entry
