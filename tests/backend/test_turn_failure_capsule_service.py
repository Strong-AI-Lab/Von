from __future__ import annotations

import json

from src.backend.services.turn_failure_capsule_service import (
    TURN_FAILURE_CAPSULE_MAX_BYTES,
    build_turn_failure_capsule,
    turn_failure_capsule_size_bytes,
)


def _paper_outcome_report() -> dict:
    return {
        "type": "adaptive_turn_effect_outcome_report",
        "schema_version": "adaptive_turn_effect_outcome_report.v1",
        "terminal_status": "effect_partially_completed",
        "response_authority": "canonical_outcome",
        "model_draft": {
            "authority": "non_authoritative",
            "preview": (
                "The paper was fully represented in #V#michael_witbrock@uoasail "
                "with api_key=never-copy-this."
            ),
        },
        "canonical_scopes": [
            {"mode": "user", "concept_id": "#V#michael_witbrock"}
        ],
        "facts": [
            {
                "effect_id": "effect-download-paper",
                "tool": "download_paper",
                "effect_status": "failed",
                "initial_effect_status": "failed",
                "changed": False,
                "outcome_resolved": False,
                "canonical_readback_present": False,
                "error_code": "arxiv_acquisition_unavailable",
                "error": (
                    "RuntimeError: <asyncio.locks.Lock object at 0x10ABCDEF> "
                    "is bound to a different event loop"
                ),
                "argument_identity": {"concept_id": "#V#private_argument"},
                "receipt": {"access_token": "never-copy-this"},
                "target_ids": ["#V#private_target"],
            },
            {
                "effect_id": "effect-create-paper",
                "tool": "create_concepts",
                "effect_status": "indeterminate",
                "initial_effect_status": "indeterminate",
                "changed": None,
                "current_outcome_status": "target_observed",
                "outcome_resolved": True,
                "reconciliation_status": "current_state_observed",
                "canonical_readback_present": True,
                "canonical_readback_verified": False,
                "workflow_id": "#V#paper_representation_workflow",
                "instance_id": "paper-instance-opaque",
                "evidence_id": "evidence-paper-readback",
                "error_code": "ontology_mutation_postcondition_failed",
            },
            {
                "effect_id": "effect-marker",
                "tool": "record_source_processing_marker",
                "effect_status": "succeeded",
                "initial_effect_status": "succeeded",
                "changed": True,
                "canonical_readback_present": True,
                "canonical_readback_verified": True,
            },
        ],
    }


def test_capsule_projects_exact_paper_lock_incident_without_private_scope() -> None:
    capsule = build_turn_failure_capsule(
        request_id="paper-partial-current-state-readback",
        terminal_status="effect_partially_completed",
        response_authority="canonical_outcome",
        visible_response=(
            "User scope #V#michael_witbrock. The acquisition failed because "
            "the asyncio lock used another event loop."
        ),
        outcome_report=_paper_outcome_report(),
        code_version="v20260818_backend+gabc123",
        git_commit="abc123" * 6 + "abcd",
        sensitive_identity_values=(
            "#V#michael_witbrock@uoasail",
            "#V#michael_witbrock",
            "#V#uoasail",
        ),
        generated_at_utc="2026-08-18T00:00:00Z",
    )

    assert capsule["schema_version"] == "turn_failure_capsule.v1"
    assert capsule["terminal_status"] == "effect_partially_completed"
    assert capsule["response_authority"] == "canonical_outcome"
    assert capsule["producer"] == {
        "code_version": "v20260818_backend+gabc123",
        "git_commit": "abc123" * 6 + "abcd",
    }
    assert capsule["canonical_scope_modes"] == ["user"]
    assert capsule["effect_summary"] == {
        "total_count": 3,
        "included_count": 3,
        "omitted_count": 0,
    }

    download = next(
        effect for effect in capsule["effects"] if effect["name"] == "download_paper"
    )
    assert download["status"] == "failed"
    assert download["changed"] is False
    assert download["error"]["code"] == "arxiv_acquisition_unavailable"
    assert download["error"]["preview"]["text"] == (
        "RuntimeError: asyncio lock is bound to a different event loop"
    )

    create = next(
        effect for effect in capsule["effects"] if effect["name"] == "create_concepts"
    )
    assert create["initial_status"] == "indeterminate"
    assert create["status"] == "indeterminate"
    assert create["current_outcome_status"] == "target_observed"
    assert create["outcome_resolved"] is True
    assert create["reconciliation_status"] == "current_state_observed"
    assert create["canonical_readback_verdict"] == "present_unverified"
    assert create["instance_id"] == "paper-instance-opaque"
    assert create["evidence_id"] == "evidence-paper-readback"

    assert capsule["pre_presentation_draft"]["authority"] == "non_authoritative"
    serialised = json.dumps(capsule, sort_keys=True)
    for forbidden in (
        "#V#michael_witbrock",
        "#V#uoasail",
        "never-copy-this",
        "private_argument",
        "private_target",
        "argument_identity",
        "receipt",
        "target_ids",
        "0x10ABCDEF",
    ):
        assert forbidden not in serialised
    assert capsule["redaction"]["applied"] is True
    assert capsule["redaction"]["redacted_count"] >= 4


def test_capsule_scopes_exact_workflow_instance_readback_verdict() -> None:
    report = _paper_outcome_report()
    report["facts"].append(
        {
            "effect_id": "effect-workflow-instance",
            "tool": "Scholarly Article Metadata Representation Workflow",
            "effect_status": "failed",
            "initial_effect_status": "failed",
            "changed": True,
            "current_outcome_status": "failed",
            "outcome_resolved": True,
            "reconciliation_status": "canonically_verified",
            "canonical_readback_present": True,
            "canonical_readback_verified": False,
            "workflow_instance_operational_readback": True,
            "workflow_instance_readback_verified": True,
            "workflow_id": "#V#scholarly_article_metadata_representation_workflow",
            "instance_id": "workflow-instance-failed-1",
            "evidence_id": "evidence-workflow-instance-readback",
        }
    )

    capsule = build_turn_failure_capsule(
        request_id="workflow-instance-readback-verdict",
        terminal_status="effect_failed",
        response_authority="canonical_outcome",
        visible_response="The workflow instance failed.",
        outcome_report=report,
        generated_at_utc="2026-08-18T00:00:00Z",
    )

    workflow = next(
        effect
        for effect in capsule["effects"]
        if effect["effect_id"] == "effect-workflow-instance"
    )
    assert workflow["canonical_readback_verdict"] == "workflow_instance_verified"
    assert workflow["instance_id"] == "workflow-instance-failed-1"
    assert workflow["evidence_id"] == "evidence-workflow-instance-readback"
    domain_create = next(
        effect for effect in capsule["effects"] if effect["name"] == "create_concepts"
    )
    assert domain_create["canonical_readback_verdict"] == "present_unverified"
    domain_marker = next(
        effect
        for effect in capsule["effects"]
        if effect["name"] == "record_source_processing_marker"
    )
    assert domain_marker["canonical_readback_verdict"] == "verified"


def test_capsule_distinguishes_unverified_workflow_instance_readback() -> None:
    report = _paper_outcome_report()
    report["facts"] = [
        {
            "effect_id": "effect-workflow-instance-unverified",
            "tool": "Scholarly Article Metadata Representation Workflow",
            "effect_status": "failed",
            "current_outcome_status": "failed",
            "outcome_resolved": True,
            "reconciliation_status": "canonically_verified",
            "canonical_readback_present": True,
            "canonical_readback_verified": False,
            "workflow_instance_operational_readback": True,
            "workflow_instance_readback_verified": False,
            "workflow_id": "#V#scholarly_article_metadata_representation_workflow",
            "instance_id": "workflow-instance-unverified-1",
            "evidence_id": "evidence-workflow-instance-readback",
        }
    ]

    capsule = build_turn_failure_capsule(
        request_id="workflow-instance-unverified-verdict",
        terminal_status="effect_failed",
        response_authority="canonical_outcome",
        visible_response="The workflow instance read-back was inconsistent.",
        outcome_report=report,
        generated_at_utc="2026-08-18T00:00:00Z",
    )

    assert capsule["effects"][0]["canonical_readback_verdict"] == (
        "workflow_instance_unverified"
    )


def test_capsule_redacts_secret_forms_from_every_text_surface() -> None:
    report = _paper_outcome_report()
    report["model_draft"]["preview"] = (
        "Authorization: Bearer bearer-secret password=hunter2 "
        "mongodb://admin:password@db.example/von nonce=draft-nonce\n"
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nprivate-key-material\n"
        "-----END ENCRYPTED PRIVATE KEY-----"
    )
    report["facts"][0]["error"] = (
        "token=token-secret sk-abcdefghijklmno "
        "eyJabcdefghijk.abcdefghijk.abcdefghijk "
        "ghp_abcdefghijklmnopqrstuv\n"
        "  File \"/Users/private/person/project/secret.py\", line 41, in run\n"
        "    private_function(secret_argument)\n"
        "RuntimeError: failed"
    )
    capsule = build_turn_failure_capsule(
        request_id="secret-redaction",
        terminal_status="effect_failed",
        response_authority="canonical_outcome",
        visible_response=(
            "https://example.test/?api_key=visible-secret&token=query-secret "
            "C:\\Users\\private\\Von\\trace.log cookie=session-secret "
            "signature=visible-signature"
        ),
        outcome_report=report,
        generated_at_utc="2026-08-18T00:00:00Z",
    )

    serialised = json.dumps(capsule, sort_keys=True)
    for secret in (
        "bearer-secret",
        "hunter2",
        "admin:password",
        "private-key-material",
        "token-secret",
        "query-secret",
        "sk-abcdefghijklmno",
        "eyJabcdefghijk",
        "ghp_abcdefghijklmnopqrstuv",
        "/Users/private/person",
        "private_function(secret_argument)",
        "C:\\Users\\private",
        "visible-secret",
        "session-secret",
        "draft-nonce",
        "visible-signature",
    ):
        assert secret not in serialised
    assert capsule["redaction"]["redacted_count"] >= 8


def test_capsule_hard_bound_is_deterministic_and_keeps_direct_failure() -> None:
    facts = []
    for index in range(100):
        facts.append(
            {
                "effect_id": f"effect-{index}-" + "e" * 500,
                "tool": f"tool-{index}-" + "n" * 500,
                "effect_status": "failed",
                "initial_effect_status": "indeterminate",
                "current_outcome_status": "target_observed",
                "outcome_resolved": index % 2 == 0,
                "reconciliation_status": "current_state_observed",
                "canonical_readback_present": True,
                "error_code": f"direct_failure_{index}",
                "error": "diagnostic " + "x" * 5000,
                "workflow_id": "workflow-" + "w" * 500,
                "instance_id": "instance-" + "i" * 500,
                "evidence_id": "evidence-" + "v" * 500,
            }
        )
    report = {
        "schema_version": "adaptive_turn_effect_outcome_report.v1",
        "facts": facts,
        "canonical_scopes": [{"mode": "user"}],
        "model_draft": {"preview": "draft " + "d" * 20_000},
    }
    kwargs = dict(
        request_id="hard-bound",
        terminal_status="effect_failed",
        response_authority="canonical_outcome",
        visible_response="visible " + "v" * 30_000,
        outcome_report=report,
        code_version="version-" + "c" * 500,
        git_commit="a" * 500,
        generated_at_utc="2026-08-18T00:00:00Z",
    )

    first = build_turn_failure_capsule(**kwargs)
    second = build_turn_failure_capsule(**kwargs)

    assert first == second
    assert turn_failure_capsule_size_bytes(first) <= TURN_FAILURE_CAPSULE_MAX_BYTES
    assert first["terminal_status"] == "effect_failed"
    assert first["effects"][0]["error"]["code"] == "direct_failure_0"
    assert first["effect_summary"]["total_count"] == 100
    assert first["effect_summary"]["omitted_count"] > 0
    assert first["redaction"]["truncated"] is True


def test_completed_turn_capsule_has_no_fabricated_failure() -> None:
    capsule = build_turn_failure_capsule(
        request_id="completed-neighbour",
        terminal_status="completed",
        response_authority="model",
        visible_response="The requested read completed.",
        outcome_report=None,
        code_version="v-test",
        generated_at_utc="2026-08-18T00:00:00Z",
    )

    assert capsule["terminal_status"] == "completed"
    assert capsule["response_authority"] == "model"
    assert capsule["effects"] == []
    assert capsule["effect_summary"] == {
        "total_count": 0,
        "included_count": 0,
        "omitted_count": 0,
    }
    assert "turn_error" not in capsule


def test_non_effect_route_error_uses_only_supplied_typed_error() -> None:
    capsule = build_turn_failure_capsule(
        request_id="route-error",
        terminal_status="model_error",
        response_authority=None,
        visible_response=None,
        outcome_report=None,
        turn_error_code="provider_transport_failed",
        turn_error_text="Authorization: Bearer do-not-copy connection failed",
        generated_at_utc="2026-08-18T00:00:00Z",
    )

    assert capsule["effects"] == []
    assert capsule["response_authority"] == "not_recorded"
    assert capsule["turn_error"]["code"] == "provider_transport_failed"
    assert capsule["turn_error"]["preview"]["text"] == (
        "Authorization: Bearer [redacted] connection failed"
    )
    assert "do-not-copy" not in json.dumps(capsule)
