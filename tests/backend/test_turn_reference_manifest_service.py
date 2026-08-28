from __future__ import annotations

from src.backend.services.turn_reference_manifest_service import (
    build_turn_reference_manifest,
    build_turn_reference_manifest_from_debug,
)


def test_manifest_types_only_structured_or_actor_visible_references() -> None:
    visible_assertion = "ska_visible_1234"
    hidden_assertion = "ska_hidden_1234"
    evidence_id = "ev_exact_turn_evidence"
    instance_id = "bd571204-d0ac-43e6-8764-11d757c37d35"
    namespace = "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    text = (
        f"Assertion `{visible_assertion}` and `{hidden_assertion}`; evidence "
        f"`{evidence_id}`; instance `{instance_id}`; namespace `{namespace}`."
    )

    def resolve(assertion_id, **_kwargs):
        if assertion_id == visible_assertion:
            return {
                "assertion_id": assertion_id,
                "assertion_revision": 2,
                "status": "asserted",
            }
        return None

    manifest = build_turn_reference_manifest(
        response_text=text,
        request_id="request-1",
        evidence_index=[
            {
                "schema_version": "turn_evidence_envelope.v1",
                "evidence_id": evidence_id,
                "tool_name": "upsert_scoped_assertion",
                "status": "ok",
                "preview": '{"assertion_id":"ska_visible_1234"}',
                "sha256": "abc",
                "size_bytes": 42,
            }
        ],
        outcome_report={
            "facts": [
                {
                    "instance_id": instance_id,
                    "workflow_id": "#V#identity_resolution_workflow",
                    "effect_status": "succeeded",
                    "evidence_id": evidence_id,
                    "target_ids": [instance_id],
                }
            ]
        },
        assertion_resolver=resolve,
    )

    references = {item["reference_id"]: item for item in manifest["references"]}
    assert references[visible_assertion]["reference_type"] == "scoped_assertion"
    assert hidden_assertion not in references
    assert references[evidence_id]["reference_type"] == "turn_evidence"
    assert references[evidence_id]["evidence"]["preview"].startswith("{")
    assert references[evidence_id]["lifecycle"] == {
        "handle_scope": "exact_actor_and_turn",
        "complete_result_lifetime": "ordinary_turn_only",
        "persisted_projection": "bounded_envelope",
        "durable_evidence_receipt": False,
    }
    assert references[instance_id]["reference_type"] == "workflow_instance"
    assert namespace not in references


def test_duplicate_workflow_target_is_one_typed_instance_reference() -> None:
    instance_id = "bd571204-d0ac-43e6-8764-11d757c37d35"
    manifest = build_turn_reference_manifest(
        response_text=f"instance `{instance_id}`; target `{instance_id}`",
        request_id="request-2",
        outcome_report={
            "facts": [
                {
                    "instance_id": instance_id,
                    "target_ids": [instance_id],
                    "effect_status": "succeeded",
                }
            ]
        },
    )

    assert manifest["reference_count"] == 1
    assert manifest["references"][0]["reference_type"] == "workflow_instance"
    assert manifest["references"][0]["occurrence_count"] == 2


def test_manifest_can_be_reconstructed_from_legacy_debug_projections() -> None:
    evidence_id = "ev_legacy_turn_evidence"
    instance_id = "bd571204-d0ac-43e6-8764-11d757c37d35"
    manifest = build_turn_reference_manifest_from_debug(
        response_text=f"instance `{instance_id}`; evidence `{evidence_id}`",
        debug_info={
            "request_id": "request-legacy",
            "aux_llm_calls": [
                {
                    "type": "adaptive_turn_evidence_index",
                    "evidence": [
                        {
                            "evidence_id": evidence_id,
                            "tool_name": "workflow_get_instance",
                            "preview": '{"status":"failed"}',
                        }
                    ],
                },
                {
                    "type": "adaptive_turn_effect_outcome_report",
                    "schema_version": "adaptive_turn_effect_outcome_report.v1",
                    "facts": [
                        {
                            "instance_id": instance_id,
                            "workflow_id": "#V#legacy_workflow",
                            "effect_status": "failed",
                            "evidence_id": evidence_id,
                        }
                    ],
                },
            ],
        },
    )

    references = {item["reference_id"]: item for item in manifest["references"]}
    assert references[evidence_id]["source"] == "persisted_evidence_index"
    assert references[evidence_id]["evidence"]["preview"] == '{"status":"failed"}'
    assert references[instance_id]["workflow"] == {
        "workflow_id": "#V#legacy_workflow",
        "effect_status": "failed",
        "canonical_readback_verified": False,
    }
