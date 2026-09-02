from __future__ import annotations

import json

from src.backend.services.adaptive_turn_service import (
    _resolve_turn_cursor_evidence_argument,
    _resolve_turn_rename_evidence_arguments,
)
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
        "field_equals",
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


def test_pointer_slice_preserves_bounded_root_source_diagnostics() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-source-diagnostics")
    envelope = store.record(
        "bounded_canonical_read",
        "call-source-diagnostics",
        {
            "coverage_complete": False,
            "counts_are_lower_bounds": True,
            "total_hits_is_lower_bound": True,
            "total_available_is_lower_bound": False,
            "has_more": True,
            "next_offset": 25,
            "offset": 0,
            "limit": 25,
            "total": 500,
            "records": [{"summary": "selected evidence"}],
            "unrelated_large_source_field": "x" * 100_000,
            "not_a_boolean_is_lower_bound": "true",
        },
    )

    expected_source_diagnostics = {
        "coverage_complete": False,
        "counts_are_lower_bounds": True,
        "has_more": True,
        "next_offset": 25,
        "offset": 0,
        "limit": 25,
        "total": 500,
        "total_hits_is_lower_bound": True,
        "total_available_is_lower_bound": False,
    }
    projected = envelope.to_mapping()
    assert projected["source_diagnostics"] == expected_source_diagnostics
    assert len(json.dumps(projected, sort_keys=True)) < 5_000

    selected = store.read(
        envelope.evidence_id,
        json_pointer="/records/0/summary",
        max_chars=100,
        trusted_scope=scope,
        turn_id="turn-source-diagnostics",
    )

    assert selected["success"] is True
    assert selected["content"] == "selected evidence"
    assert selected["source_diagnostics"] == expected_source_diagnostics
    assert selected["has_more"] is False
    assert "unrelated_large_source_field" not in selected
    assert "not_a_boolean_is_lower_bound" not in selected["source_diagnostics"]


def test_absent_source_diagnostics_are_not_projected_as_false() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-no-source-diagnostics")
    envelope = store.record(
        "generic_lookup",
        "call-no-source-diagnostics",
        {"records": [{"summary": "ordinary evidence"}]},
    )

    assert "source_diagnostics" not in envelope.to_mapping()
    selected = store.read(
        envelope.evidence_id,
        json_pointer="/records/0",
        max_chars=100,
        trusted_scope=scope,
        turn_id="turn-no-source-diagnostics",
    )
    assert selected["success"] is True
    assert "source_diagnostics" not in selected


def test_pointer_slice_preserves_recognised_nested_paging_diagnostics() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-nested-paging")
    envelope = store.record(
        "list_scoped_assertions",
        "call-nested-paging",
        {
            "success": True,
            "assertions": [
                {
                    "assertion_id": "assertion-1",
                    "body": "x" * 100_000,
                }
            ],
            "assertions_found": 200,
            "assertions_found_is_lower_bound": True,
            "paging": {
                "limit": 200,
                "offset": 0,
                "returned": 200,
                "has_more": True,
                "next_offset": 200,
                "counts_are_lower_bounds": True,
                "total_available": 500,
                "total_available_is_lower_bound": True,
                "visibility_filtered": True,
                "unrelated_large_field": "y" * 100_000,
            },
        },
    )

    selected = store.read(
        envelope.evidence_id,
        json_pointer="/assertions/0/assertion_id",
        max_chars=100,
        trusted_scope=scope,
        turn_id="turn-nested-paging",
    )

    assert selected["success"] is True
    assert selected["content"] == "assertion-1"
    assert selected["source_diagnostics"] == {
        "assertions_found_is_lower_bound": True,
        "paging": {
            "has_more": True,
            "counts_are_lower_bounds": True,
            "next_offset": 200,
            "offset": 0,
            "limit": 200,
            "total_available": 500,
            "total_available_is_lower_bound": True,
        },
    }
    assert selected["has_more"] is False
    assert "unrelated_large_field" not in selected["source_diagnostics"]["paging"]


def test_store_keeps_rfc_slash_and_empty_string_pointer_semantics() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-rfc-root")
    envelope = store.record(
        "generic_lookup",
        "call-rfc-root",
        {
            "": "empty-key-member",
            "canonical_concept_id": "#V#stable_readback_id",
        },
    )

    slash_member = store.read(
        envelope.evidence_id,
        json_pointer="/",
        max_chars=100,
        trusted_scope=scope,
        turn_id="turn-rfc-root",
    )
    document_root = store.read(
        envelope.evidence_id,
        json_pointer="",
        max_chars=1_000,
        trusted_scope=scope,
        turn_id="turn-rfc-root",
    )

    assert slash_member["success"] is True
    assert slash_member["content"] == "empty-key-member"
    assert document_root["success"] is True
    assert (
        json.loads(document_root["content"])["canonical_concept_id"]
        == "#V#stable_readback_id"
    )


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


def test_read_field_equals_finds_json_null_without_serialised_text_matching() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-structural-null")
    envelope = store.record(
        "conversation_list",
        "call-conversation-list",
        {
            "coverage_complete": True,
            "has_more": False,
            "conversations": [
                {"session_id": "unnamed", "session_name": None},
                {"session_id": "named", "session_name": "Named conversation"},
            ],
        },
    )

    result = store.read(
        envelope.evidence_id,
        field_equals={"session_name": None},
        max_chars=2_000,
        trusted_scope=scope,
        turn_id="turn-structural-null",
    )

    assert result["success"] is True
    assert result["content_format"] == "field_matches"
    assert result["returned_match_count"] == 1
    assert result["matches"] == [
        {
            "json_pointer": "/conversations/0",
            "match_kind": "mapping_fields",
            "matched_fields": ["session_name"],
            "field_values": {"session_name": None},
        }
    ]
    assert result["conclusion"]["global_conclusion_supported"] is True


def test_structural_text_query_returns_executable_field_equals_recovery() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-structural-query")
    envelope = store.record(
        "conversation_list",
        "call-conversation-list",
        {"conversations": [{"session_name": None}]},
    )

    for query in ('"session_name":null', '"session_name": null'):
        result = store.read(
            envelope.evidence_id,
            query=query,
            trusted_scope=scope,
            turn_id="turn-structural-query",
        )

        assert result["success"] is False
        assert result["error_code"] == "structural_query_requires_field_equals"
        assert result["recovery"] == {
            "selector": "field_equals",
            "field_equals": {"session_name": None},
        }


def test_empty_query_on_incomplete_source_cannot_support_global_conclusion() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-incomplete-query")
    envelope = store.record(
        "conversation_list",
        "call-incomplete-list",
        {
            "coverage_complete": False,
            "has_more": True,
            "conversations": [{"session_name": "Named conversation"}],
        },
    )

    result = store.read(
        envelope.evidence_id,
        query="missing term",
        trusted_scope=scope,
        turn_id="turn-incomplete-query",
    )

    assert result["success"] is True
    assert result["matches"] == []
    assert result["conclusion"] == {
        "scope": "selected_evidence",
        "source_coverage_complete": False,
        "source_has_more": True,
        "global_conclusion_supported": False,
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


def test_turn_runtime_resolves_exact_cursor_from_short_evidence_handle() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-cursor")
    envelope = store.record(
        "conversation_search",
        "call-search",
        {"continuation_cursor": "signed-opaque-cursor", "has_more": True},
    )

    resolved, error = _resolve_turn_cursor_evidence_argument(
        capability_name="conversation_search",
        arguments={
            "query": "*",
            "name_present": False,
            "cursor_evidence_id": envelope.evidence_id,
        },
        evidence_store=store,
        scope=scope,
        turn_id="turn-cursor",
    )

    assert error is None
    assert resolved == {
        "query": "*",
        "name_present": False,
        "cursor": "signed-opaque-cursor",
    }


def test_turn_runtime_rejects_cursor_evidence_from_another_capability() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-cursor-mismatch")
    envelope = store.record(
        "conversation_list",
        "call-list",
        {"continuation_cursor": "signed-opaque-cursor"},
    )

    _resolved, error = _resolve_turn_cursor_evidence_argument(
        capability_name="conversation_search",
        arguments={"query": "*", "cursor_evidence_id": envelope.evidence_id},
        evidence_store=store,
        scope=scope,
        turn_id="turn-cursor-mismatch",
    )

    assert error is not None
    assert error["error_code"] == "cursor_evidence_tool_mismatch"


def test_turn_runtime_hydrates_rename_tokens_from_matching_inspection_items() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-rename")
    envelope = store.record(
        "conversation_inspect_batch",
        "call-inspect",
        {
            "results": [
                {
                    "evidence": {
                        "session_id": "session-001",
                        "evidence_token": "signed-rename-token-001",
                    },
                }
            ]
        },
    )

    resolved, error = _resolve_turn_rename_evidence_arguments(
        capability_name="conversation_manage_batch",
        arguments={
            "action": "rename",
            "inspection_evidence_id": envelope.evidence_id,
            "rename_items": [
                {
                    "session_id": "session-001",
                    "session_name": "Scientific relation extraction",
                    "evidence_item_index": 0,
                }
            ],
        },
        evidence_store=store,
        scope=scope,
        turn_id="turn-rename",
    )

    assert error is None
    assert resolved == {
        "action": "rename",
        "rename_items": [
            {
                "session_id": "session-001",
                "session_name": "Scientific relation extraction",
                "evidence_token": "signed-rename-token-001",
            }
        ],
    }


def test_turn_runtime_rejects_rename_evidence_for_different_session() -> None:
    scope = _scope()
    store = TurnEvidenceStore(scope, "turn-rename-mismatch")
    envelope = store.record(
        "conversation_inspect_batch",
        "call-inspect",
        {
            "results": [
                {
                    "evidence": {
                        "session_id": "session-001",
                        "evidence_token": "signed-rename-token-001",
                    },
                }
            ]
        },
    )

    _resolved, error = _resolve_turn_rename_evidence_arguments(
        capability_name="conversation_manage_batch",
        arguments={
            "action": "rename",
            "inspection_evidence_id": envelope.evidence_id,
            "rename_items": [
                {
                    "session_id": "session-002",
                    "session_name": "Wrong conversation",
                    "evidence_item_index": 0,
                }
            ],
        },
        evidence_store=store,
        scope=scope,
        turn_id="turn-rename-mismatch",
    )

    assert error is not None
    assert error["error_code"] == "inspection_evidence_mismatch"
