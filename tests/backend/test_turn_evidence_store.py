from __future__ import annotations

import json

from src.backend.services.turn_evidence_store import (
    DEFAULT_PREVIEW_MAX_CHARS,
    EvidenceEnvelope,
    TrustedTurnScope,
    TurnEvidenceStore,
)


def _scope(
    user: str = "#V#test_user",
    organisation: str = "#V#test_organisation",
) -> TrustedTurnScope:
    return TrustedTurnScope(
        user_concept_id=user,
        organisation_concept_id=organisation,
        namespace=f"{user}@{organisation}",
    )


def test_record_returns_opaque_bounded_provenance_envelope() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-1")
    raw_value = {
        "items": [
            {
                "title": "General evidence",
                "body": "x" * 100_000,
            }
        ]
    }

    envelope = store.record(
        "generic_lookup",
        "call-1",
        raw_value,
        provenance={
            "source_system": "test-source",
            "source_locator": "opaque-source-1",
            "oversized_note": "n" * 10_000,
        },
    )

    assert isinstance(envelope, EvidenceEnvelope)
    projected = envelope.to_mapping()
    assert projected["schema_version"] == "turn_evidence_envelope.v1"
    assert projected["trust_boundary"] == "untrusted_tool_output"
    assert projected["tool_name"] == "generic_lookup"
    assert projected["call_id"] == "call-1"
    assert projected["turn_id"] == "turn-1"
    assert len(projected["sha256"]) == 64
    assert projected["size_bytes"] > 100_000
    assert len(projected["preview"]) == DEFAULT_PREVIEW_MAX_CHARS
    assert projected["preview_truncated"] is True
    assert projected["available_selectors"] == [
        "json_pointer",
        "offset",
        "query",
    ]
    assert projected["provenance"]["source_system"] == "test-source"
    assert len(projected["provenance"]["oversized_note"]) == 300
    assert projected["provenance"]["oversized_note_truncated"] is True

    # The handle carries no actor, tool, call, or storage-key material.
    assert projected["evidence_id"].startswith("ev_")
    for forbidden_fragment in (
        "test_user",
        "test_organisation",
        "generic_lookup",
        "call-1",
    ):
        assert forbidden_fragment not in projected["evidence_id"]

    assert store.index() == [projected]
    first_index = store.index()
    first_index[0]["shape"]["keys"].append("mutated")
    assert "mutated" not in store.index()[0]["shape"]["keys"]


def test_read_supports_bounded_pointer_slices_and_escaped_tokens() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-2")
    envelope = store.record(
        "generic_lookup",
        "call-2",
        {
            "items": [
                {"body": "zero"},
                {"body": "abcdefghij"},
            ],
            "a/b": {"~key": "escaped-pointer-value"},
        },
    )

    first_slice = store.read(
        envelope.evidence_id,
        json_pointer="/items/1/body",
        offset=2,
        max_chars=3,
        trusted_scope=scope,
        turn_id="turn-2",
    )
    assert first_slice["success"] is True
    assert first_slice["content"] == "cde"
    assert first_slice["content_format"] == "text"
    assert first_slice["total_chars"] == 10
    assert first_slice["returned_chars"] == 3
    assert first_slice["has_more"] is True
    assert first_slice["next_offset"] == 5
    assert first_slice["tool_name"] == "generic_lookup"
    assert first_slice["call_id"] == "call-2"
    assert first_slice["trust_boundary"] == "untrusted_tool_output"

    escaped = store.read(
        envelope.evidence_id,
        json_pointer="/a~1b/~0key",
        max_chars=100,
        trusted_scope=scope,
        turn_id="turn-2",
    )
    assert escaped["success"] is True
    assert escaped["content"] == "escaped-pointer-value"
    assert escaped["has_more"] is False


def test_read_hides_handle_existence_across_actor_and_turn_boundaries() -> None:
    owner_scope = _scope()
    store = TurnEvidenceStore(owner_scope, "turn-owner")
    envelope = store.record("generic_lookup", "call-owner", {"secret": "value"})

    wrong_actor = store.read(
        envelope.evidence_id,
        trusted_scope=_scope(user="#V#other_user"),
        turn_id="turn-owner",
    )
    wrong_turn = store.read(
        envelope.evidence_id,
        trusted_scope=owner_scope,
        turn_id="turn-other",
    )
    unknown = store.read(
        "ev_unknown",
        trusted_scope=owner_scope,
        turn_id="turn-owner",
    )

    for result in (wrong_actor, wrong_turn, unknown):
        assert result["success"] is False
        assert result["error_code"] == "evidence_not_found"
        assert "tool_name" not in result
        assert "call_id" not in result
        assert "provenance" not in result


def test_read_query_returns_bounded_model_selectable_matches() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-query")
    envelope = store.record(
        "generic_lookup",
        "call-query",
        {
            "records": [
                {"name": "first", "text": "prefix NEEDLE suffix"},
                {"name": "needle-key", "text": "second needle occurrence"},
                {"name": "irrelevant", "text": "other material"},
            ]
        },
    )

    result = store.read(
        envelope.evidence_id,
        query="needle",
        max_chars=2_000,
        trusted_scope=scope,
        turn_id="turn-query",
    )

    assert result["success"] is True
    assert result["content_format"] == "query_matches"
    assert result["returned_match_count"] == 3
    assert result["returned_chars"] <= 2_000
    assert result["has_more"] is False
    pointers = {match["json_pointer"] for match in result["matches"]}
    assert "/records/0/text" in pointers
    assert "/records/1/name" in pointers
    assert "/records/1/text" in pointers

    bounded = store.read(
        envelope.evidence_id,
        query="needle",
        max_chars=10,
        trusted_scope=scope,
        turn_id="turn-query",
    )
    assert bounded["returned_chars"] <= 10
    assert bounded["matches"] == []
    assert bounded["has_more"] is True
    assert bounded["next_offset"] == 0

    selected = store.read(
        envelope.evidence_id,
        json_pointer="/records/1",
        query="needle",
        max_chars=2_000,
        trusted_scope=scope,
        turn_id="turn-query",
    )
    assert selected["success"] is True
    assert selected["selector"]["json_pointer"] == "/records/1"
    assert {
        match["json_pointer"] for match in selected["matches"]
    } == {
        "/records/1/name",
        "/records/1/text",
    }


def test_binary_evidence_is_hydrated_only_as_a_bounded_base64_slice() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-binary")
    envelope = store.record(
        "binary_lookup",
        "call-binary",
        b"abcdefghij",
    )

    assert envelope.content_type == "application/octet-stream"
    assert envelope.size_bytes == 10
    result = store.read(
        envelope.evidence_id,
        offset=4,
        max_chars=5,
        trusted_scope=scope,
        turn_id="turn-binary",
    )
    assert result["success"] is True
    assert result["content_format"] == "base64"
    assert result["content"] == "ZGVmZ"
    assert result["returned_chars"] == 5
    assert result["has_more"] is True

    # The compact index does not grow with the ten-byte source beyond its
    # bounded preview and metadata projection.
    assert len(json.dumps(store.index(), sort_keys=True)) < 5_000
