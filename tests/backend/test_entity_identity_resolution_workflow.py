"""Tests for the durable entity identity-resolution workflow.

Under JVNAUTOSCI-2148 the workflow stopped doing scoring/clustering in Python
and instead routes through an LLM rumination stage that consumes
``#V#entity_duplicate_reasoning_prompt``. These tests exercise the support
primitives (scan, gather_evidence, apply) and verify the workflow shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _request(action_id: str, data: dict):
    from src.backend.workflows.action_registry import (
        WorkflowActionRequest,
        WorkflowEnvironment,
    )

    return WorkflowActionRequest(
        action_id=action_id,
        inputs={},
        environment=WorkflowEnvironment(llm_client=None),
        data=data,
    )


def test_workflow_definition_structure() -> None:
    from src.backend.workflows.durable.entity_identity_resolution_workflow import (
        ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
        ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        build_entity_identity_resolution_workflow_test_definition,
    )

    workflow = build_entity_identity_resolution_workflow_test_definition()
    assert workflow.workflow_id == ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID
    assert workflow.initial_state == "scan"
    assert set(workflow.states.keys()) == {
        "scan",
        "gather_evidence",
        "reason_about_identity",
        "apply",
        "complete",
        "failed",
    }
    assert workflow.states["complete"].terminal is True
    assert workflow.states["failed"].terminal is True

    reason_state = workflow.states["reason_about_identity"]
    assert len(reason_state.actions) == 1
    action = reason_state.actions[0]
    assert action.action_id == "llm.action"
    assert action.execution_mode == "llm"
    prompt_ids = (action.prompt_contract or {}).get("requested_prompt_concept_ids", [])
    assert ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID in prompt_ids
    assert (action.validation_policy or {}).get("output_format") == "json_value"
    mappings = (reason_state.metadata or {}).get("tool_output_context_mappings", [])
    assert any(
        m.get("context_key") == "identity_reasoning_payload"
        and m.get("tool_output_field") == "validated_json"
        for m in mappings
    )


def test_scan_handler_emits_candidate_pairs_no_scoring() -> None:
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    pairs = [
        {
            "a_concept_id": "#V#person_alice_1",
            "b_concept_id": "#V#person_alice_2",
            "name_key": "alice smith",
            "scope_key": "u:global|o:global",
        },
    ]
    diagnostics = {
        "docs_scanned": 5,
        "docs_kept_for_indexing": 3,
        "name_key_count": 2,
        "candidate_pair_count": 1,
        "max_candidate_pairs": 600,
        "truncated": False,
        "focused_mode": False,
        "focused_concept_count": 0,
        "focused_name_key_count": 0,
        "scan_limit_used": 300,
    }

    with patch.object(
        mod, "_enumerate_candidate_pairs", return_value=(pairs, diagnostics)
    ):
        result = mod._handle_scan_candidates(
            _request("identity_resolution.scan_candidates", {})
        )

    assert result.ok
    out = result.outputs
    assert out["candidate_pairs"] == pairs
    summary = out["identity_resolution_scan_summary"]
    assert summary["candidate_pair_count"] == 1
    assert summary["focused_mode"] is False
    forbidden = {
        "duplicate_recommendations",
        "duplicate_clusters",
        "auto_apply_confidence_threshold",
        "review_confidence_threshold",
        "max_pair_evaluations_used",
    }
    assert forbidden.isdisjoint(out.keys())
    assert forbidden.isdisjoint(summary.keys())


def test_scan_handler_focuses_to_candidate_concepts() -> None:
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    captured: dict = {}

    def fake_enumerate(
        *,
        scan_limit,
        candidate_concept_ids,
        candidate_names,
        max_candidate_pairs,
    ):
        captured["candidate_concept_ids"] = list(candidate_concept_ids)
        captured["candidate_names"] = list(candidate_names)
        return (
            [
                {
                    "a_concept_id": "#V#person_michael_g1",
                    "b_concept_id": "#V#person_michael_g2",
                    "name_key": "michael witbrock",
                    "scope_key": "u:global|o:global",
                }
            ],
            {
                "docs_scanned": 4,
                "docs_kept_for_indexing": 2,
                "name_key_count": 1,
                "candidate_pair_count": 1,
                "max_candidate_pairs": max_candidate_pairs,
                "truncated": False,
                "focused_mode": True,
                "focused_concept_count": len(candidate_concept_ids),
                "focused_name_key_count": len(candidate_names),
                "scan_limit_used": scan_limit,
            },
        )

    with patch.object(mod, "_enumerate_candidate_pairs", side_effect=fake_enumerate):
        result = mod._handle_scan_candidates(
            _request(
                "identity_resolution.scan_candidates",
                {
                    "candidate_concepts": [
                        "#V#person_michael_g1",
                        "#V#person_michael_g2",
                    ],
                    "candidate_names": ["Michael Witbrock"],
                },
            )
        )

    assert result.ok
    summary = result.outputs["identity_resolution_scan_summary"]
    assert summary["focused_mode"] is True
    assert summary["candidate_concept_count"] == 2
    assert summary["candidate_name_hint_count"] == 1
    assert captured["candidate_concept_ids"] == [
        "#V#person_michael_g1",
        "#V#person_michael_g2",
    ]


def test_enumerate_candidate_pairs_indexes_focused_docs_outside_scan_cursor() -> None:
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    focus_doc = {
        "concept_id": "#V#zz_focus_person",
        "name": "Michael Witbrock",
        "names": ["Michael Witbrock"],
        "computed_kind": "instance",
        "relationships": {"is_an_instance_of": ["#V#person"]},
    }
    duplicate_doc = {
        "concept_id": "#V#aa_duplicate_person",
        "name": "Michael Witbrock",
        "names": ["Michael Witbrock"],
        "computed_kind": "instance",
        "relationships": {"is_an_instance_of": ["#V#person"]},
    }

    def fake_find(query, **_kwargs):
        if query.get("concept_id") == {"$in": ["#V#zz_focus_person"]}:
            return [focus_doc]
        return [duplicate_doc]

    with patch.object(mod.ConceptsRepository, "find", side_effect=fake_find):
        pairs, diagnostics = mod._enumerate_candidate_pairs(
            scan_limit=20,
            candidate_concept_ids=["#V#zz_focus_person"],
            candidate_names=[],
            max_candidate_pairs=10,
        )

    assert pairs == [
        {
            "a_concept_id": "#V#aa_duplicate_person",
            "b_concept_id": "#V#zz_focus_person",
            "name_key": "michael witbrock",
            "scope_key": "u:global|o:global",
        }
    ]
    assert diagnostics["focused_mode"] is True
    assert diagnostics["focused_docs_preloaded"] == 1
    assert diagnostics["docs_kept_for_indexing"] == 2


def test_gather_evidence_returns_structured_profile() -> None:
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    enriched = [
        {
            "pair_ids": ["#V#a", "#V#b"],
            "name_key": "alice smith",
            "scope_match": True,
            "a": {"concept_id": "#V#a", "display_name": "Alice"},
            "b": {"concept_id": "#V#b", "display_name": "Alice"},
            "shared_facts": {
                "type_ids": ["#V#person"],
                "source_refs": ["orcid:0000-1"],
                "relationship_targets": [],
            },
        }
    ]
    diagnostics = {
        "candidate_pair_count_in": 1,
        "evidence_pair_count_out": 1,
        "unique_concepts_profiled": 2,
    }

    with patch.object(
        mod,
        "build_candidate_evidence_pairs",
        return_value=(enriched, diagnostics),
    ):
        result = mod._handle_gather_evidence(
            _request(
                "identity_resolution.gather_evidence",
                {
                    "candidate_pairs": [
                        {
                            "a_concept_id": "#V#a",
                            "b_concept_id": "#V#b",
                            "name_key": "alice smith",
                            "scope_key": "u:global|o:global",
                        }
                    ],
                },
            )
        )

    assert result.ok
    assert result.outputs["candidate_evidence_pairs"] == enriched
    assert result.outputs["candidate_evidence_diagnostics"] == diagnostics


def test_workflow_executor_runs_scan_evidence_llm_apply_path() -> None:
    from src.backend.workflows.action_registry import (
        ActionRegistry,
        WorkflowActionResult,
        WorkflowEnvironment,
    )
    from src.backend.workflows.engine import WorkflowExecutor
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    candidate_pairs = [
        {
            "a_concept_id": "#V#person_michael_a",
            "b_concept_id": "#V#person_michael_b",
            "name_key": "michael witbrock",
            "scope_key": "u:global|o:global",
        }
    ]
    scan_diagnostics = {
        "docs_scanned": 2,
        "docs_kept_for_indexing": 2,
        "focused_docs_preloaded": 1,
        "name_key_count": 1,
        "candidate_pair_count": 1,
        "max_candidate_pairs": 600,
        "truncated": False,
        "focused_mode": True,
        "focused_concept_count": 1,
        "focused_name_key_count": 1,
        "scan_limit_used": 300,
    }
    evidence_pairs = [
        {
            "pair_ids": ["#V#person_michael_a", "#V#person_michael_b"],
            "name_key": "michael witbrock",
            "a": {"concept_id": "#V#person_michael_a"},
            "b": {"concept_id": "#V#person_michael_b"},
            "shared_facts": {"type_ids": ["#V#person"]},
        }
    ]
    evidence_diagnostics = {
        "candidate_pair_count_in": 1,
        "evidence_pair_count_out": 1,
        "skipped_missing_profile": 0,
        "skipped_invalid": 0,
        "unique_concepts_profiled": 2,
    }
    llm_context: dict = {}

    def fake_llm_step(request):
        llm_context["policy"] = dict(request.llm_policy or {})
        llm_context["candidate_evidence_pairs"] = request.data.get(
            "candidate_evidence_pairs"
        )
        return WorkflowActionResult(
            outputs={
                "validated_json": {
                    "identity_recommendations": [
                        {
                            "pair_ids": [
                                "#V#person_michael_a",
                                "#V#person_michael_b",
                            ],
                            "action": "queue_review",
                            "source_id": "#V#person_michael_b",
                            "target_id": "#V#person_michael_a",
                            "confidence": 0.72,
                            "rationale": "Evidence is suggestive but incomplete.",
                            "evidence_refs": ["pair.name_key"],
                        }
                    ]
                }
            }
        )

    registry = ActionRegistry()
    mod.register_entity_identity_resolution_actions(registry)

    with (
        patch.object(
            mod,
            "_enumerate_candidate_pairs",
            return_value=(candidate_pairs, scan_diagnostics),
        ),
        patch.object(
            mod,
            "build_candidate_evidence_pairs",
            return_value=(evidence_pairs, evidence_diagnostics),
        ),
        patch(
            "src.backend.workflows.llm_step_executor.execute_llm_step",
            side_effect=fake_llm_step,
        ),
    ):
        result = WorkflowExecutor(registry=registry, max_transitions=10).run(
            mod.build_entity_identity_resolution_workflow_test_definition(),
            environment=WorkflowEnvironment(llm_client=None),
            data={
                "dry_run": True,
                "candidate_concepts": ["#V#person_michael_a"],
                "candidate_names": ["Michael Witbrock"],
            },
        )

    assert result.completed is True
    assert result.final_state == "complete"
    assert llm_context["candidate_evidence_pairs"] == evidence_pairs
    assert llm_context["policy"]["tool_mode"] == "disallowed"
    assert result.data["identity_reasoning_payload"] == {
        "identity_recommendations": [
            {
                "pair_ids": ["#V#person_michael_a", "#V#person_michael_b"],
                "action": "queue_review",
                "source_id": "#V#person_michael_b",
                "target_id": "#V#person_michael_a",
                "confidence": 0.72,
                "rationale": "Evidence is suggestive but incomplete.",
                "evidence_refs": ["pair.name_key"],
            }
        ]
    }
    apply_summary = result.data["identity_resolution_apply_summary"]
    assert apply_summary["would_queue_count"] == 1
    assert apply_summary["merged_count"] == 0
    assert result.data["identity_resolution_result"]["candidate_pair_count"] == 1


def test_apply_handler_consumes_llm_recommendations_and_dispatches() -> None:
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    payload = {
        "identity_recommendations": [
            {
                "pair_ids": ["#V#person_alice_dup", "#V#person_alice"],
                "action": "auto_merge",
                "source_id": "#V#person_alice_dup",
                "target_id": "#V#person_alice",
                "confidence": 0.96,
                "rationale": "shared ORCID",
                "evidence_refs": ["shared.source_refs:orcid:0000-1"],
            },
            {
                "pair_ids": ["#V#person_pat_dup", "#V#person_pat"],
                "action": "queue_review",
                "source_id": "#V#person_pat_dup",
                "target_id": "#V#person_pat",
                "confidence": 0.72,
                "rationale": "ambiguous",
                "evidence_refs": ["name.token_overlap"],
            },
            {
                "action": "leave_distinct",
                "source_id": "#V#x",
                "target_id": "#V#y",
                "confidence": 0.1,
            },
        ]
    }

    merge_mock = MagicMock(return_value={"success": True, "operations": [{}, {}]})
    queue_mock = MagicMock(
        return_value={"success": True, "assertion": {"assertion_id": "ura_test_1"}}
    )

    with (
        patch.object(mod, "merge_concepts", merge_mock),
        patch.object(mod, "_record_merge_audit", return_value=None),
        patch.object(mod, "_queue_uncertain", queue_mock),
    ):
        result = mod._handle_apply_resolutions(
            _request(
                "identity_resolution.apply_resolutions",
                {"identity_reasoning_payload": payload},
            )
        )

    assert result.ok
    summary = result.outputs["identity_resolution_apply_summary"]
    assert summary["merged_count"] == 1
    assert summary["queued_count"] == 1
    assert summary["failed_count"] == 0
    assert summary["skipped_count"] == 1  # leave_distinct
    assert summary["recommendation_count"] == 3
    assert "v2_llm_rumination" in summary["policy_version"]
    merge_mock.assert_called_once()
    merge_call = merge_mock.call_args
    assert merge_call.args == ("#V#person_alice_dup", "#V#person_alice")
    assert merge_call.kwargs["simulate"] is False
    assert merge_call.kwargs["request"].action_id == (
        "identity_resolution.apply_resolutions"
    )
    queue_mock.assert_called_once()


def test_durable_merge_binds_workflow_agent_and_exact_delegation(monkeypatch) -> None:
    from src.backend.services import ontology_publication_authority_service as authority
    from src.backend.workflows.action_registry import WorkflowEnvironment
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    captured = {}

    def governed_merge(**kwargs):
        captured["kwargs"] = kwargs
        captured["invocation"] = authority.current_ontology_invocation()
        return {"success": False, "error_code": "test_stop"}

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._merge_concepts",
        governed_merge,
    )
    request = _request("identity_resolution.apply_resolutions", {})
    request = type(request)(
        **{
            **request.__dict__,
            "environment": WorkflowEnvironment(
                llm_client=None,
                ontology_delegation_id="delegation:exact",
            ),
            "workflow_id": mod.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
            "workflow_state_id": "apply",
        }
    )

    result = mod.merge_concepts(
        "#V#duplicate",
        "#V#canonical",
        simulate=False,
        request=request,
    )

    assert result["error_code"] == "test_stop"
    assert captured["kwargs"] == {
        "source_id": "#V#duplicate",
        "target_id": "#V#canonical",
        "simulate": False,
    }
    invocation = captured["invocation"]
    assert invocation.surface == "workflow"
    assert invocation.executing_agent_concept_id == "#V#von_system"
    assert invocation.audience == "workflow"
    assert invocation.delegation_id == "delegation:exact"
    assert invocation.effect_id.endswith(":apply:merge_concepts")


def test_apply_handler_dry_run_does_not_write() -> None:
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    payload = {
        "identity_recommendations": [
            {
                "action": "auto_merge",
                "source_id": "#V#a",
                "target_id": "#V#b",
                "confidence": 0.95,
            },
            {
                "action": "queue_review",
                "source_id": "#V#c",
                "target_id": "#V#d",
                "confidence": 0.7,
            },
        ]
    }

    merge_mock = MagicMock()
    queue_mock = MagicMock()

    with (
        patch.object(mod, "merge_concepts", merge_mock),
        patch.object(mod, "_queue_uncertain", queue_mock),
    ):
        result = mod._handle_apply_resolutions(
            _request(
                "identity_resolution.apply_resolutions",
                {"identity_reasoning_payload": payload, "dry_run": True},
            )
        )

    summary = result.outputs["identity_resolution_apply_summary"]
    assert summary["merged_count"] == 0
    assert summary["queued_count"] == 0
    assert summary["would_merge_count"] == 1
    assert summary["would_queue_count"] == 1
    merge_mock.assert_not_called()
    queue_mock.assert_not_called()


def test_apply_handler_fails_closed_on_missing_payload() -> None:
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    merge_mock = MagicMock()
    queue_mock = MagicMock()

    with (
        patch.object(mod, "merge_concepts", merge_mock),
        patch.object(mod, "_queue_uncertain", queue_mock),
    ):
        result = mod._handle_apply_resolutions(
            _request("identity_resolution.apply_resolutions", {})
        )

    summary = result.outputs["identity_resolution_apply_summary"]
    assert summary["merged_count"] == 0
    assert summary["queued_count"] == 0
    assert summary["fail_closed_reason"] == "missing_identity_reasoning_payload"
    merge_mock.assert_not_called()
    queue_mock.assert_not_called()


def test_apply_handler_handles_validated_json_envelope() -> None:
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    payload = {
        "validated_json": {
            "identity_recommendations": [
                {
                    "action": "queue_review",
                    "source_id": "#V#x",
                    "target_id": "#V#y",
                    "confidence": 0.66,
                }
            ]
        }
    }
    queue_mock = MagicMock(return_value={"success": True, "assertion": {}})

    with (
        patch.object(mod, "merge_concepts", MagicMock()),
        patch.object(mod, "_queue_uncertain", queue_mock),
    ):
        result = mod._handle_apply_resolutions(
            _request(
                "identity_resolution.apply_resolutions",
                {"identity_reasoning_payload": payload},
            )
        )

    summary = result.outputs["identity_resolution_apply_summary"]
    assert summary["queued_count"] == 1
    queue_mock.assert_called_once()


def test_apply_handler_skips_malformed_recommendations() -> None:
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    payload = {
        "identity_recommendations": [
            {"action": "not_a_real_action", "source_id": "#V#a", "target_id": "#V#b"},
            {"action": "auto_merge", "source_id": "#V#a", "target_id": "#V#a"},
            {"action": "queue_review", "source_id": "", "target_id": "#V#b"},
        ]
    }

    with (
        patch.object(mod, "merge_concepts", MagicMock()),
        patch.object(mod, "_queue_uncertain", MagicMock()),
    ):
        result = mod._handle_apply_resolutions(
            _request(
                "identity_resolution.apply_resolutions",
                {"identity_reasoning_payload": payload},
            )
        )

    summary = result.outputs["identity_resolution_apply_summary"]
    assert summary["merged_count"] == 0
    assert summary["queued_count"] == 0
    assert summary["malformed_recommendation_count"] == 3


def test_register_actions_installs_four_handlers() -> None:
    from src.backend.workflows.action_registry import ActionRegistry
    from src.backend.workflows.durable import entity_identity_resolution_workflow as mod

    registry = ActionRegistry()
    mod.register_entity_identity_resolution_actions(registry)

    expected = {
        "identity_resolution.scan_candidates",
        "identity_resolution.gather_evidence",
        "identity_resolution.apply_resolutions",
        "identity_resolution.finalise",
    }
    assert expected.issubset(set(registry.all_action_ids()))
