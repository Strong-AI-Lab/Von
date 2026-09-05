from __future__ import annotations

from src.backend.services.turn_execution_record_service import (
    build_turn_execution_record,
    extract_learning_advice_exposures,
    turn_execution_record_evidence_sha256,
)


def _exposure(**overrides):
    payload = {
        "type": "adaptive_turn_learning_advice_exposure",
        "schema_version": "adaptive_capability_learning_advice_exposure.v1",
        "consumer": "direct_adaptive_turn",
        "decision_kind": "capability_choice",
        "arm": "B",
        "source": "simple_sidecar",
        "status": "exposed",
        "experiment_id": "#V#experiment_one",
        "case_id": "ambiguous_messages_one",
        "candidate_id": "#V#candidate_one",
        "candidate_revision": 1,
        "candidate_evaluation_disposition": "undecided",
        "candidate_body_sha256": "a" * 64,
        "candidate_revision_identity_sha256": "b" * 64,
        "candidate_source_locator_sha256": "c" * 64,
        "projection_sha256": "d" * 64,
        "model_visible": True,
        "model_visible_call_ids": ["request-1:llm:1"],
        "completed_model_visible_call_ids": ["request-1:llm:1"],
        "visibility_indeterminate_call_ids": [],
        "dispositions": [
            {
                "call_id": "tool-call-1",
                "capability_name": "message_list_direct",
                "disposition": "followed",
                "private_body": "nested learned text must not persist either",
            }
        ],
        "private_body": "never persist this learned text in the compact view",
    }
    payload.update(overrides)
    return payload


def test_extract_learning_advice_exposures_is_body_free_and_bounded() -> None:
    exposures = extract_learning_advice_exposures(
        [_exposure(case_id=f"case-{index}") for index in range(6)]
    )

    assert len(exposures) == 4
    assert exposures[0]["dispositions"][0]["disposition"] == "followed"
    assert "call_id" not in exposures[0]["dispositions"][0]
    assert "private_body" not in exposures[0]["dispositions"][0]
    assert "private_body" not in exposures[0]
    assert "never persist" not in repr(exposures)


def test_canonical_turn_record_sanitises_learning_advice_aux_event() -> None:
    exposure = _exposure()
    record = build_turn_execution_record(
        request_id="request-1",
        session_id="session-1",
        namespace="#V#alice@test_org",
        user_id="#V#alice",
        org_id="#V#test_org",
        prompt_text="Summarise recent messages to me.",
        response_text="Here are your messages.",
        interaction_timestamp_utc="2026-09-04T00:00:00Z",
        llm_calls=[],
        aux_llm_calls=[exposure],
        tool_invocations=[],
    )

    expected = extract_learning_advice_exposures([exposure])[0]
    assert "learning_advice_exposures" not in record
    stored_aux = next(
        item
        for item in record["aux_llm_calls"]
        if item.get("type") == "adaptive_turn_learning_advice_exposure"
    )
    assert stored_aux == {"type": exposure["type"], **expected}
    assert "never persist" not in repr(stored_aux)
    assert "nested learned text" not in repr(stored_aux)


def test_canonical_turn_record_does_not_add_an_exposure_side_channel() -> None:
    record = build_turn_execution_record(
        request_id="request-2",
        session_id="session-2",
        namespace="#V#alice@test_org",
        user_id="#V#alice",
        org_id="#V#test_org",
        prompt_text="Hello",
        response_text="Hello",
        interaction_timestamp_utc="2026-09-04T00:00:00Z",
        llm_calls=[],
        aux_llm_calls=[],
        tool_invocations=[],
    )

    assert "learning_advice_exposures" not in record


def test_turn_record_evidence_digest_ignores_only_storage_insertion_metadata() -> None:
    record = {
        "schema_version": "turn_execution_record.v1",
        "request_id": "request-digest",
        "updated_at_utc": "2026-09-04T00:00:00Z",
        "final_response": {"response_sha256": "a" * 64},
    }
    first = {**record, "_id": "mongo-one", "inserted_at": object()}
    second = {**record, "_id": "mongo-two", "inserted_at": object()}

    assert turn_execution_record_evidence_sha256(first) == (
        turn_execution_record_evidence_sha256(second)
    )
    changed = {**record, "updated_at_utc": "2026-09-04T00:00:01Z"}
    assert turn_execution_record_evidence_sha256(changed) != (
        turn_execution_record_evidence_sha256(record)
    )
