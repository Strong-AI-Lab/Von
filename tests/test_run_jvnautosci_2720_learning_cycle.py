from __future__ import annotations

import copy
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

import pytest

import scripts.run_jvnautosci_2720_learning_cycle as learning_cycle
from scripts.run_jvnautosci_2720_learning_cycle import (
    DEFAULT_FIXTURE_PATH,
    FIXED_FIXTURE_SHA256,
    FIXED_MODEL_ID,
    FIXED_MODEL_PARAMETERS,
    FIXED_PROVIDER,
    FORMATION_SCHEMA_VERSION,
    FrozenMessageWorld,
    LearningCycleError,
    LiveCycleBackend,
    ModelGeneration,
    _sha256,
    _text_sha256,
    attest_rejected_candidate_projection_block,
    build_blind_evaluator_prompt,
    build_candidate_formation_prompt,
    build_exact_frozen_read_handler,
    build_readback,
    execute_cycle,
    load_fixture,
    main,
    normalise_formation_result,
    preflight_summary,
    prepare_cycle,
)

_PRIVATE_SOURCE_TEXT = "private-source-message-must-not-escape"
_PRIVATE_DIRECT_MESSAGE = "private-direct-message-must-not-escape"
_PRIVATE_GMAIL_SNIPPET = "private-gmail-snippet-must-not-escape"
_CANDIDATE_BODY = (
    "When a message request is compatible with more than one bounded channel, use "
    "the shared situation to choose; after a typed read failure, try another plausible "
    "authorised channel before claiming global inability."
)
_FIXED_SCOPE = {
    "actor_user_id": "#V#michael_witbrock",
    "organisation_concept_id": "#V#the_lu_witbrock_household",
    "namespace": "#V#michael_witbrock@the_lu_witbrock_household",
}


class _FakeGateway:
    enabled = True

    def describe_methods(self) -> dict[str, dict[str, Any]]:
        return {
            "gmail_list_messages": {
                "category": "read",
                "ordinary_turn_effect": False,
                "description": "Read the frozen Gmail view.",
            },
            "message_list_direct": {
                "category": "read",
                "ordinary_turn_effect": False,
                "description": "Read the frozen Von direct-message view.",
            },
        }


class _FakeBackend:
    def __init__(
        self,
        fixture: Mapping[str, Any],
        *,
        existing_candidate: bool = False,
        formation_outcome: str = "capture_one_candidate",
        experiment_decision: str = "arm_b_content_win",
    ) -> None:
        self.fixture = copy.deepcopy(dict(fixture))
        self.events: list[str] = []
        self.write_events: list[str] = []
        self.formation_prompts: list[str] = []
        self.formation_outcome = formation_outcome
        self.experiment_decision = experiment_decision
        self.authority: dict[str, dict[str, Any]] = {}
        self.formation_records: dict[str, dict[str, Any]] = {}
        self.turn_execution_records: dict[str, dict[str, Any]] = {}
        self.candidate = self._candidate() if existing_candidate else None
        self.manifest: dict[str, Any] | None = None
        self.plan: dict[str, Any] | None = None
        self.run: dict[str, Any] | None = None
        self.world = FrozenMessageWorld(
            gateway=_FakeGateway(),
            snapshot={
                "gateway_identity_sha256": "1" * 64,
                "replay_world_sha256": "2" * 64,
                "model_visible_texts": [
                    _PRIVATE_DIRECT_MESSAGE,
                    _PRIVATE_GMAIL_SNIPPET,
                ],
            },
            trusted_argument_values={
                "actor_user_concept_id": "#V#michael_witbrock",
                "actor_organisation_concept_id": "#V#the_lu_witbrock_household",
                "turn_namespace": ("#V#michael_witbrock@the_lu_witbrock_household"),
                "gmail_profile": {
                    "schema_version": "trusted_argument_choice.v1",
                    "default_selector": "#V#gmail_profile",
                    "choices": [
                        {
                            "selector": "#V#gmail_profile",
                            "value": "runtime-profile",
                        }
                    ],
                },
            },
            public_summary={
                "schema_version": "jvnautosci_2720_frozen_message_world.v1",
                "capabilities": ["gmail_list_messages", "message_list_direct"],
                "gateway_identity_sha256": "1" * 64,
                "replay_world_sha256": "2" * 64,
                "result_sha256": {
                    "gmail_list_messages": "3" * 64,
                    "message_list_direct": "4" * 64,
                },
                "message_list_direct_success": True,
                "message_list_direct_count": 1,
                "gmail_list_messages_success": False,
            },
        )
        if self.candidate is not None:
            source_material = self.load_source_material(self.fixture, scope={})
            self.events.clear()
            generation = self._formation_generation()
            formation = self._formation_result()
            self.formation_records[str(self.candidate["capture_request_id"])] = (
                self._formation_record(
                    request_id=str(self.candidate["capture_request_id"]),
                    fixture=self.fixture,
                    source_material=source_material,
                    formation=formation,
                    generation=generation,
                    scope={
                        "actor_user_id": "#V#michael_witbrock",
                        "organisation_concept_id": "#V#the_lu_witbrock_household",
                        "namespace": ("#V#michael_witbrock@the_lu_witbrock_household"),
                    },
                )
            )

    def _source(self) -> dict[str, Any]:
        capture = self.fixture["candidate_formation"]["capture_source"]
        evidence = [
            {
                "source_locator": {
                    "session_id": capture["session_id"],
                    "history_index": index,
                },
                "content_sha256": _text_sha256(f"{_PRIVATE_SOURCE_TEXT}-{index}"),
                "role_sha256": _text_sha256("assistant" if position % 2 else "user"),
                "turn_identity_sha256": f"{position + 10:064x}",
                **(
                    {
                        "turn_execution_evidence": {
                            "request_id": f"source-request-{index}",
                            "turn_execution_record_sha256": f"{index + 100:064x}",
                        }
                    }
                    if position % 2
                    else {}
                ),
            }
            for position, index in enumerate(capture["history_indices"])
        ]
        return {
            "kind": "conversation",
            "locator": {
                "session_id": capture["session_id"],
                "owner_user_id": "#V#michael_witbrock",
                "include_legacy": False,
                "include_execution_evidence": True,
                "namespace": capture["namespace"],
            },
            "message_evidence": evidence,
        }

    def _candidate(self) -> dict[str, Any]:
        source = self._source()
        return {
            "schema_version": "learning_candidate.v1",
            "lifecycle_state": "non_active",
            "evaluation_disposition": "undecided",
            "candidate_id": "#V#learning_candidate_test_2720",
            "revision": 1,
            "body": _CANDIDATE_BODY,
            "body_sha256": _text_sha256(_CANDIDATE_BODY),
            "revision_identity_sha256": "a" * 64,
            "source_locator_sha256": _sha256(source),
            "target_concept_ids": ["#V#communicating"],
            "contributor_concept_ids": ["#V#michael_witbrock", "#V#von_system"],
            "audience_concept_ids": ["#V#the_lu_witbrock_household"],
            "beneficiary_concept_ids": [
                "#V#michael_witbrock",
                "#V#the_lu_witbrock_household",
            ],
            "purpose_concept_ids": [],
            "authorship": {"author_concept_id": "#V#von_system"},
            "visibility": {
                "scope": "actor",
                "actor_user_id": "#V#michael_witbrock",
                "organisation_concept_id": "#V#the_lu_witbrock_household",
            },
            "namespace": "#V#michael_witbrock@the_lu_witbrock_household",
            "source": source,
            "capture_request_id": "existing-formation-request",
        }

    def load_source_material(
        self, fixture: Mapping[str, Any], *, scope: Mapping[str, str]
    ) -> dict[str, Any]:
        del scope
        self.events.append("load_source_material")
        rows = []
        for position, index in enumerate(
            fixture["source_conversation"]["history_indices"]
        ):
            role = "assistant" if position % 2 else "user"
            row: dict[str, Any] = {
                "history_index": index,
                "role": role,
                "content": f"{_PRIVATE_SOURCE_TEXT}-{index}",
                "content_sha256": _text_sha256(f"{_PRIVATE_SOURCE_TEXT}-{index}"),
                "source_locator": {
                    "session_id": fixture["candidate_formation"]["capture_source"][
                        "session_id"
                    ],
                    "history_index": index,
                },
            }
            if role == "assistant":
                row["execution_evidence"] = {
                    "request_id": f"source-request-{index}",
                    "turn_execution_record_sha256": f"{index + 100:064x}",
                    "tool_invocations": [
                        {
                            "tool": "gmail_list_messages",
                            "success": False,
                            "error_code": "invalid_grant",
                        }
                    ],
                }
            rows.append(row)
        return {
            "session_id": fixture["candidate_formation"]["capture_source"][
                "session_id"
            ],
            "namespace": fixture["candidate_formation"]["capture_source"]["namespace"],
            "include_legacy": False,
            "include_execution_evidence": True,
            "messages": rows,
        }

    def safe_source_summary(self, source_material: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "session_id": source_material["session_id"],
            "namespace": source_material["namespace"],
            "message_count": len(source_material["messages"]),
            "messages": [
                {
                    "history_index": item["history_index"],
                    "role": item["role"],
                    "content_sha256": item["content_sha256"],
                }
                for item in source_material["messages"]
            ],
        }

    def list_candidates(
        self, *, scope: Mapping[str, str], target_concept_id: str
    ) -> list[dict[str, Any]]:
        del scope, target_concept_id
        self.events.append("list_candidates")
        return [copy.deepcopy(self.candidate)] if self.candidate else []

    def read_candidate(
        self, candidate_id: str, *, scope: Mapping[str, str]
    ) -> dict[str, Any]:
        del scope
        assert self.candidate and candidate_id == self.candidate["candidate_id"]
        return copy.deepcopy(self.candidate)

    def read_authority(
        self,
        concept_id: str,
        *,
        scope: Mapping[str, str],
        expected_sha256: str,
    ) -> dict[str, Any]:
        del scope
        self.events.append(f"read_authority:{concept_id}")
        state = self.authority.get(concept_id)
        if state is None:
            return {
                "concept_id": concept_id,
                "exists": False,
                "content": None,
                "content_sha256": None,
                "matches_expected": False,
            }
        result = copy.deepcopy(state)
        result["matches_expected"] = (
            result["content_sha256"] == expected_sha256
            and result.get("content_relation_count", 1) == 1
        )
        return result

    def materialise_authority(
        self,
        *,
        kind: str,
        expected: Mapping[str, str],
        scope: Mapping[str, str],
    ) -> dict[str, Any]:
        del scope
        self.write_events.append(f"materialise:{kind}")
        state = {
            "concept_id": expected["concept_id"],
            "exists": True,
            "content": expected["content"],
            "content_sha256": expected["content_sha256"],
            "matches_expected": True,
        }
        self.authority[expected["concept_id"]] = copy.deepcopy(state)
        return state

    def freeze_message_world(
        self, fixture: Mapping[str, Any], *, scope: Mapping[str, str]
    ) -> FrozenMessageWorld:
        del fixture, scope
        self.events.append("freeze_message_world")
        return self.world

    def inspect_code_state(self, *, require_clean: bool) -> dict[str, Any]:
        self.events.append(f"inspect_code_state:{require_clean}")
        return {"code_revision": "test-revision", "worktree_clean": True}

    def _formation_result(self) -> dict[str, str]:
        return {
            "schema_version": FORMATION_SCHEMA_VERSION,
            "outcome": self.formation_outcome,
            "observed_pattern": (
                "A channel-specific failure was treated as global despite another "
                "bounded messaging capability being available."
            ),
            "candidate_body": (
                _CANDIDATE_BODY
                if self.formation_outcome == "capture_one_candidate"
                else ""
            ),
        }

    @staticmethod
    def _formation_generation() -> ModelGeneration:
        return ModelGeneration(
            text="",
            receipt={
                "schema_version": "learning_advice_evaluator_model_call_receipt.v1",
                "call_id": "formation-call",
                "provider": FIXED_PROVIDER,
                "requested_model": FIXED_MODEL_ID,
                "selected_model": FIXED_MODEL_ID,
                "effective_model": FIXED_MODEL_ID,
                "provider_observed_model": FIXED_MODEL_ID,
                "provider_request_sent": True,
                "success": True,
            },
        )

    @staticmethod
    def _formation_record(
        *,
        request_id: str,
        fixture: Mapping[str, Any],
        source_material: Mapping[str, Any],
        formation: Mapping[str, str],
        generation: ModelGeneration,
        scope: Mapping[str, str],
    ) -> dict[str, Any]:
        diagnostic = learning_cycle._build_formation_diagnostic(
            fixture,
            source_material=source_material,
            formation=formation,
            generation=generation,
        )
        prompt_text = (
            "[JVNAUTOSCI-2720 source-only candidate formation; private source "
            f"omitted; fixture_sha256={diagnostic['fixture_sha256']}]"
        )
        response_text = (
            "[candidate formation result; semantic body omitted; "
            f"outcome={formation['outcome']}; "
            f"body_sha256={diagnostic['candidate_body_sha256']}]"
        )
        receipt = copy.deepcopy(generation.receipt)
        return {
            "schema_version": "turn_execution_record.v1",
            "request_id": request_id,
            "session_id": f"jvnautosci-2720-formation-{request_id}",
            "namespace": scope["namespace"],
            "actor_concept_id": scope["actor_user_id"],
            "user_id": scope["actor_user_id"],
            "org_id": scope["organisation_concept_id"],
            "prompt": {"sha256": _text_sha256(prompt_text)},
            "final_response": {"response_sha256": _text_sha256(response_text)},
            "llm_calls": [receipt],
            "aux_llm_calls": [
                {
                    "type": "jvnautosci_2720_candidate_formation",
                    **copy.deepcopy(diagnostic),
                }
            ],
            "execution": {"llm_calls": [copy.deepcopy(receipt)]},
        }

    def generate_luna(
        self,
        prompt: str,
        *,
        scope: Mapping[str, str],
        parameters: Mapping[str, Any],
        purpose: str,
    ) -> ModelGeneration:
        del scope
        assert purpose == "candidate_formation"
        assert parameters == {"temperature": 0.2, "max_output_tokens": 1_200}
        self.events.append("generate_luna")
        self.formation_prompts.append(prompt)
        generation = self._formation_generation()
        generation.text = json.dumps(self._formation_result())
        return generation

    def persist_formation_record(
        self,
        fixture: Mapping[str, Any],
        *,
        source_material: Mapping[str, Any],
        formation: Mapping[str, str],
        generation: ModelGeneration,
        scope: Mapping[str, str],
    ) -> dict[str, Any]:
        from src.backend.services.turn_execution_record_service import (
            turn_execution_record_evidence_sha256,
        )

        self.write_events.append("persist_formation_record")
        request_id = "formation-request"
        self.formation_records[request_id] = self._formation_record(
            request_id=request_id,
            fixture=fixture,
            source_material=source_material,
            formation=formation,
            generation=generation,
            scope=scope,
        )
        return {
            "request_id": request_id,
            "turn_execution_record_sha256": turn_execution_record_evidence_sha256(
                self.formation_records[request_id]
            ),
        }

    def capture_candidate(self, **kwargs: Any) -> dict[str, Any]:
        self.write_events.append("capture_candidate")
        assert kwargs["source"]["include_execution_evidence"] is True
        assert kwargs["body"] == _CANDIDATE_BODY
        self.candidate = self._candidate()
        self.candidate["capture_request_id"] = kwargs["request_id"]
        return copy.deepcopy(self.candidate)

    def verify_candidate_formation(
        self,
        fixture: Mapping[str, Any],
        *,
        candidate: Mapping[str, Any],
        scope: Mapping[str, str],
    ) -> dict[str, Any]:
        request_id = str(candidate.get("capture_request_id") or "")
        current_source = self.load_source_material(fixture, scope=scope)
        return LiveCycleBackend._validate_candidate_formation_record(
            self.formation_records.get(request_id),
            fixture=fixture,
            candidate=candidate,
            source_material=current_source,
            scope=scope,
        )

    def allocate_run_id(self) -> str:
        return "#V#jvnautosci_2720_learning_advice_run_test"

    def create_experiment_spec(self, fixture: Mapping[str, Any], **kwargs: Any) -> None:
        del fixture, kwargs
        self.write_events.append("create_experiment_spec")

    def start_experiment_run(
        self,
        manifest: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        scope: Mapping[str, str],
    ) -> None:
        self.write_events.append("start_experiment_run")
        from scripts.run_learning_advice_experiment import (
            build_learning_advice_experiment_run_binding,
        )

        self.manifest = copy.deepcopy(dict(manifest))
        self.plan = copy.deepcopy(dict(plan))
        self.run = {
            "run_id": manifest["experiment_run_id"],
            "experiment_spec_id": manifest["experiment_spec_id"],
            "namespace": scope["namespace"],
            "user_id": scope["actor_user_id"],
            "org_id": scope["organisation_concept_id"],
            "status": "running",
            "verdict": None,
            "metadata": {
                "learning_advice_experiment": (
                    build_learning_advice_experiment_run_binding(
                        manifest,
                        plan=plan,
                    )
                )
            },
            "observations": [],
            "turn_execution_request_ids": [],
        }

    def execute_experiment(
        self,
        manifest: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.start_experiment_run(
            manifest,
            plan=plan,
            scope=_FIXED_SCOPE,
        )
        self.write_events.append("execute_experiment")
        assert self.run is not None and self.candidate is not None
        assert manifest["model"] == {
            "provider": FIXED_PROVIDER,
            "model_id": FIXED_MODEL_ID,
            "parameters": FIXED_MODEL_PARAMETERS,
        }
        from src.backend.services.experiment_run_service import (
            _normalise_observations,
        )
        from src.backend.services.learning_advice_experiment_service import (
            compute_learning_advice_paired_results,
            evaluate_learning_advice_content_decision,
        )
        from src.backend.services.turn_execution_record_service import (
            turn_execution_record_evidence_sha256,
        )

        runtime_snapshot = copy.deepcopy(dict(kwargs["runtime_snapshot"]))
        acting_support = copy.deepcopy(dict(kwargs["acting_support"]))

        def stored_observation(envelope: Mapping[str, Any]) -> dict[str, Any]:
            rows = _normalise_observations(
                envelope,
                now_iso="2026-09-04T11:59:00+00:00",
            )
            assert len(rows) == 1
            return rows[0]

        trial_observations: list[dict[str, Any]] = []
        trial_envelopes: list[dict[str, Any]] = []
        request_ids: list[str] = []
        observation_ids: list[str] = []
        for index, planned in enumerate(plan["trials"], start=1):
            if planned["case_kind"] == "control":
                verdict = "pass"
            elif self.experiment_decision == "arm_b_content_win":
                verdict = "pass" if planned["arm"] == "B" else "fail"
            else:
                verdict = "pass" if planned["arm"] == "A" else "fail"
            request_id = f"trial-request-{index:03d}"
            execution_identity = {
                "request_id": request_id,
                "session_id": f"trial-session-{index:03d}",
                "turn_id": f"trial-turn-{index:03d}",
            }
            response_text = f"Body-free test response {index}."
            diagnostic = {
                "schema_version": learning_cycle.TRIAL_TER_DIAGNOSTIC_SCHEMA_VERSION,
                "experiment_spec_id": manifest["experiment_spec_id"],
                "experiment_run_id": manifest["experiment_run_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "plan_sha256": plan["plan_sha256"],
                "trial_id": planned["trial_id"],
                "pair_id": planned["pair_id"],
                "case_id": planned["case_id"],
                "repeat": planned["repeat"],
                "arm": planned["arm"],
                "turn_id": execution_identity["turn_id"],
            }
            ter = {
                "schema_version": "turn_execution_record.v1",
                "request_id": request_id,
                "session_id": execution_identity["session_id"],
                "namespace": _FIXED_SCOPE["namespace"],
                "actor_concept_id": _FIXED_SCOPE["actor_user_id"],
                "user_id": _FIXED_SCOPE["actor_user_id"],
                "org_id": _FIXED_SCOPE["organisation_concept_id"],
                "prompt": {"sha256": _text_sha256(planned["prompt"])},
                "final_response": {"response_sha256": _text_sha256(response_text)},
                "aux_llm_calls": [
                    {
                        "type": learning_cycle.TRIAL_TER_BINDING_AUX_TYPE,
                        **copy.deepcopy(diagnostic),
                    }
                ],
            }
            self.turn_execution_records[request_id] = ter
            ter_sha256 = turn_execution_record_evidence_sha256(ter)
            evaluation = {
                "status": "completed",
                "passed": verdict == "pass",
                "verdict": verdict,
                "evaluator_concept_id": planned["evaluator_concept_id"],
                "evaluator_sha256": planned["evaluator_sha256"],
                "rubric_concept_id": planned["rubric_concept_id"],
                "rubric_sha256": planned["rubric_sha256"],
                "material_failure": False,
                "external_availability_changed": False,
            }
            observed_outcome = {
                "passed": evaluation["passed"],
                "evaluation_status": "completed",
                "trial_integrity_valid": True,
            }
            trial = {
                "schema_version": "learning_advice_experiment_observation.v1",
                "observation_kind": "paired_trial",
                "experiment_spec_id": manifest["experiment_spec_id"],
                "experiment_run_id": manifest["experiment_run_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "plan_sha256": plan["plan_sha256"],
                **{
                    field: copy.deepcopy(planned[field])
                    for field in (
                        "trial_id",
                        "pair_id",
                        "case_id",
                        "case_kind",
                        "repeat",
                        "arm",
                    )
                },
                "candidate_ref": copy.deepcopy(manifest["candidate_ref"]),
                "model": copy.deepcopy(manifest["model"]),
                "runtime_snapshot": runtime_snapshot,
                "execution_identity": execution_identity,
                "turn_execution_request_ids": [request_id],
                "trial_integrity_valid": True,
                "integrity_reason_codes": [],
                "execution": {
                    "response_sha256": _text_sha256(response_text),
                    "runner_elapsed_ms": 10,
                    "reported_duration_ms": 8,
                    "model_call_count": 1,
                    "turn_execution_record_sha256": ter_sha256,
                    "acting_support_identity": acting_support,
                },
                "evaluation": evaluation,
                "observed_outcome": observed_outcome,
            }
            observation_id = (
                f"{manifest['experiment_run_id']}:learning-advice-trial:"
                f"{planned['trial_id']}"
            )
            envelope = {
                "schema_version": "learning_advice_experiment_observation.v1",
                "observation_id": observation_id,
                "observation_type": "learning_advice_trial",
                "label": f"Learning-advice paired trial {planned['trial_id']}",
                "verdict": verdict,
                "observed_outcome": copy.deepcopy(observed_outcome),
                "evidence": {"learning_advice_trial": copy.deepcopy(trial)},
                "metrics": {
                    "runner_elapsed_ms": 10,
                    "reported_duration_ms": 8,
                    "model_call_count": 1,
                },
                "execution_provenance": {
                    "manifest_sha256": manifest["manifest_sha256"],
                    "plan_sha256": plan["plan_sha256"],
                    "trial_id": planned["trial_id"],
                    "arm": planned["arm"],
                },
                "turn_execution_request_ids": [request_id],
            }
            trial_observations.append(trial)
            trial_envelopes.append(stored_observation(envelope))
            request_ids.append(request_id)
            observation_ids.append(observation_id)

        paired_results = compute_learning_advice_paired_results(
            trial_observations,
            required_applicable_pair_count=8,
            required_control_pair_count=4,
        )
        decision = evaluate_learning_advice_content_decision(
            paired_results,
            decision_rule=manifest["decision_rule"],
        )
        assert decision["decision"] == self.experiment_decision
        final_payload = {
            "schema_version": "learning_advice_experiment_result.v1",
            "experiment_spec_id": manifest["experiment_spec_id"],
            "experiment_run_id": manifest["experiment_run_id"],
            "manifest_sha256": manifest["manifest_sha256"],
            "plan_sha256": plan["plan_sha256"],
            "candidate_ref": copy.deepcopy(manifest["candidate_ref"]),
            "runtime_snapshot": runtime_snapshot,
            "acting_support_identity": acting_support,
            "planned_trial_count": plan["trial_count"],
            "executed_trial_count": plan["trial_count"],
            "persisted_ter_count": plan["trial_count"],
            "persisted_trial_observation_count": plan["trial_count"],
            "valid_trial_count": plan["trial_count"],
            "completed_evaluation_count": plan["trial_count"],
            "content_decision": decision,
            "paired_results": paired_results,
            "secondary_metrics": {"A": {}, "B": {}},
            "trial_observation_ids": observation_ids,
        }
        final = {**final_payload, "result_evidence_sha256": _sha256(final_payload)}
        final_observation = {
            "schema_version": "learning_advice_experiment_observation.v1",
            "observation_id": (
                f"{manifest['experiment_run_id']}:learning-advice-result:"
                f"{final['result_evidence_sha256'][:24]}"
            ),
            "observation_type": "learning_advice_experiment_result",
            "label": "Frozen learning-advice paired result",
            "verdict": (
                "pass" if self.experiment_decision == "arm_b_content_win" else "fail"
            ),
            "observed_outcome": {
                "decision": self.experiment_decision,
                "result_evidence_sha256": final["result_evidence_sha256"],
                "activation_authorised": False,
            },
            "evidence": {"learning_advice_result": copy.deepcopy(final)},
            "metrics": copy.deepcopy(final["secondary_metrics"]),
            "execution_provenance": {
                "manifest_sha256": manifest["manifest_sha256"],
                "plan_sha256": plan["plan_sha256"],
                "acting_support_sha256": acting_support["acting_support_sha256"],
            },
            "turn_execution_request_ids": request_ids,
        }
        self.run["status"] = "completed"
        self.run["verdict"] = (
            "pass" if self.experiment_decision == "arm_b_content_win" else "partial"
        )
        self.run["completed_at_utc"] = "2026-09-04T12:00:00+00:00"
        self.run["observations"] = [
            *trial_envelopes,
            stored_observation(final_observation),
        ]
        self.run["turn_execution_request_ids"] = request_ids
        disposition = (
            "retained"
            if self.experiment_decision == "arm_b_content_win"
            else "rejected"
        )
        self.candidate["evaluation_disposition"] = disposition
        if disposition == "rejected":
            disposition_request_id = (
                f"learning-advice-disposition:{manifest['experiment_run_id']}:"
                f"{final['result_evidence_sha256'][:32]}"
            )
            disposition_identity = {
                "disposition_request_id": disposition_request_id,
                "evaluation_disposition": "rejected",
                "evidence_verdict": "does_not_support_use",
                "experiment_run_id": manifest["experiment_run_id"],
                "evidence_sha256": final["result_evidence_sha256"],
                "candidate_revision": self.candidate["revision"],
                "candidate_revision_identity_sha256": self.candidate[
                    "revision_identity_sha256"
                ],
            }
            self.candidate["disposition_history"] = [
                {
                    "schema_version": "learning_candidate_disposition.v1",
                    **disposition_identity,
                    "disposition_identity_sha256": _sha256(disposition_identity),
                    "recorded_by_actor_concept_id": "#V#michael_witbrock",
                    "recorded_by_organisation_concept_id": (
                        "#V#the_lu_witbrock_household"
                    ),
                    "recorded_at": "2026-09-04T12:00:00+00:00",
                }
            ]
        return {
            **final,
            "candidate_disposition": {
                "evaluation_disposition": disposition,
                "candidate_id": self.candidate["candidate_id"],
            },
        }

    def execute_post_disposition_checks(
        self, fixture: Mapping[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        del fixture
        self.write_events.append("execute_post_disposition_checks")
        candidate = kwargs["candidate"]
        assert candidate["evaluation_disposition"] == "rejected"
        return {
            "schema_version": "jvnautosci_2720_post_disposition_check.v1",
            "status": "rejected_revision_absence_verified",
            "candidate_id": candidate["candidate_id"],
            "candidate_revision": candidate["revision"],
            "check_count": 2,
            "checks": [
                {
                    "check_kind": kind,
                    "prompt_sha256": f"{index + 7:064x}",
                    "request_id": f"post-{kind}",
                    "turn_execution_record_sha256": f"{index + 9:064x}",
                    "model_request_count": 1,
                    "learning_advice_exposure_count": 0,
                    "candidate_revision_absent": True,
                }
                for index, kind in enumerate(("trigger", "control"))
            ],
        }

    def read_run(
        self, run_id: str, *, scope: Mapping[str, str]
    ) -> dict[str, Any] | None:
        del scope
        if self.run is None or self.run["run_id"] != run_id:
            return None
        return copy.deepcopy(self.run)

    def read_turn_execution_record(
        self, request_id: str, *, scope: Mapping[str, str]
    ) -> dict[str, Any] | None:
        assert scope == _FIXED_SCOPE
        record = self.turn_execution_records.get(request_id)
        return copy.deepcopy(record) if record is not None else None


def test_repository_fixture_is_fixed_to_luna_and_digest_verified(
    tmp_path: Path,
) -> None:
    fixture = load_fixture()
    assert fixture["experiment"]["model"] == {
        "provider": FIXED_PROVIDER,
        "model_id": FIXED_MODEL_ID,
        "parameters": FIXED_MODEL_PARAMETERS,
    }
    assert (
        fixture["candidate_formation"]["capture_source"]["include_execution_evidence"]
        is True
    )

    unsafe = copy.deepcopy(fixture)
    unsafe["experiment"]["model"]["model_id"] = "gpt-5.6-sol"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(LearningCycleError) as exc_info:
        load_fixture(path)
    assert exc_info.value.reason_code == "jvnautosci_2720_model_not_fixed"


def test_source_only_formation_withholds_held_out_and_evaluation_material() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)
    source = backend.load_source_material(fixture, scope={})
    prompt = build_candidate_formation_prompt(fixture, source)

    assert _PRIVATE_SOURCE_TEXT in prompt
    assert "source-request-" in prompt
    assert fixture["cases"][0]["prompt"] not in prompt
    assert fixture["blind_evaluator"]["prompt"] not in prompt
    assert fixture["global_rubric"]["definition_sha256"] not in prompt
    assert "JVNAUTOSCI-2720" not in prompt


def test_source_execution_projection_reads_canonical_nested_ter_body_free() -> None:
    private_summary = "Loaded a private message body that must stay sealed."
    ter = {
        "request_id": "source-request-77",
        "execution": {
            "tool_invocations": [
                {
                    "tool": "gmail_list_messages",
                    "status": "failed",
                    "error_code": "invalid_grant",
                    "result_summary": private_summary,
                    "payload_fingerprint": "a" * 64,
                },
                {
                    "tool": "message_list_direct",
                    "status": "ok",
                    "result_summary": "Found one direct message.",
                    "payload_fingerprint": "b" * 64,
                },
            ],
            "llm_calls": [
                {
                    "provider": FIXED_PROVIDER,
                    "requested_model": FIXED_MODEL_ID,
                    "effective_model": FIXED_MODEL_ID,
                    "success": True,
                }
            ],
            "summary": {"overall_outcome": "successful_completion"},
        },
    }

    projection = LiveCycleBackend._project_source_execution(
        {"turn_execution_record": ter}, history_index=77
    )

    assert projection["terminal_status"] == "successful_completion"
    assert projection["tool_invocations"] == [
        {
            "tool": "gmail_list_messages",
            "success": False,
            "error_code": "invalid_grant",
            "count": None,
            "transport_outcome": "failed",
            "payload_fingerprint": "a" * 64,
            "result_summary_sha256": _text_sha256(private_summary),
        },
        {
            "tool": "message_list_direct",
            "success": True,
            "error_code": None,
            "count": None,
            "transport_outcome": "ok",
            "payload_fingerprint": "b" * 64,
            "result_summary_sha256": _text_sha256("Found one direct message."),
        },
    ]
    assert projection["model_calls"][0]["effective_model"] == FIXED_MODEL_ID
    assert private_summary not in json.dumps(projection)


def test_live_authority_read_treats_exact_concept_absence_as_not_materialised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import concept_service

    def missing(_concept_id: str) -> Any:
        raise concept_service.ConceptNotFoundError("expected absent authority")

    monkeypatch.setattr(concept_service, "get_concept_by_concept_id_exact", missing)
    result = LiveCycleBackend().read_authority(
        "#V#prompt_learning_advice_capability_choice_trial_evaluator",
        scope={
            "actor_user_id": "#V#michael_witbrock",
            "organisation_concept_id": "#V#the_lu_witbrock_household",
            "namespace": "#V#michael_witbrock@the_lu_witbrock_household",
        },
        expected_sha256="0" * 64,
    )

    assert result == {
        "concept_id": "#V#prompt_learning_advice_capability_choice_trial_evaluator",
        "exists": False,
        "content": None,
        "content_sha256": None,
        "matches_expected": False,
    }


def test_live_backend_rejects_caller_supplied_actor_scope() -> None:
    with pytest.raises(LearningCycleError) as exc_info:
        LiveCycleBackend().read_authority(
            "#V#prompt_learning_advice_capability_choice_trial_evaluator",
            scope={
                "actor_user_id": "#V#another_actor",
                "organisation_concept_id": "#V#another_organisation",
                "namespace": "#V#another_actor@another_organisation",
            },
            expected_sha256="0" * 64,
        )

    assert exc_info.value.reason_code == "jvnautosci_2720_trusted_scope_mismatch"


def test_fixture_is_bound_to_exact_source_scope_and_digest(tmp_path: Path) -> None:
    fixture = load_fixture()
    assert _sha256(fixture) == FIXED_FIXTURE_SHA256

    fixture["source_conversation"]["conversation_ref"]["user_concept_id"] = (
        "#V#another_actor"
    )
    altered = tmp_path / "altered-fixture.json"
    altered.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(LearningCycleError) as exc_info:
        load_fixture(altered)

    assert exc_info.value.reason_code == "jvnautosci_2720_source_scope_not_fixed"


def test_live_cli_rejects_a_fixture_copy_outside_canonical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied = tmp_path / "copied-fixture.json"
    copied.write_text(
        DEFAULT_FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(
        learning_cycle,
        "LiveCycleBackend",
        lambda: object(),
    )
    out = io.StringIO()
    err = io.StringIO()

    status = main(["--preflight", "--fixture", str(copied)], stdout=out, stderr=err)

    assert status == 2
    assert out.getvalue() == ""
    assert "jvnautosci_2720_live_fixture_mismatch" in err.getvalue()


def test_live_authority_materialises_absent_content_and_reads_exact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import concept_service, text_value_service

    concept_id = "#V#prompt_learning_advice_capability_choice_trial_evaluator"
    content = "Represented evaluator bytes."
    state: dict[str, Any] = {"concept_created": False, "content": None}
    create_calls: list[dict[str, Any]] = []
    upsert_calls: list[dict[str, Any]] = []

    def read_concept(requested_id: str) -> dict[str, Any]:
        assert requested_id == concept_id
        if not state["concept_created"]:
            raise concept_service.ConceptNotFoundError("not materialised")
        return {
            "concept_id": requested_id,
            "relationships": {
                "#V#specific_to_user": ["#V#michael_witbrock"],
            },
        }

    def create_concept(**kwargs: Any) -> dict[str, Any]:
        create_calls.append(kwargs)
        state["concept_created"] = True
        return {"concept_id": kwargs["concept_id"]}

    def upsert_content(**kwargs: Any) -> dict[str, Any]:
        upsert_calls.append(kwargs)
        state["content"] = kwargs["text"]
        return {"relation_id": "represented-content-relation"}

    def read_texts(
        requested_id: str, *, predicate: str, lang: str, limit: int
    ) -> list[dict[str, Any]]:
        assert (requested_id, predicate, lang, limit) == (
            concept_id,
            "hasContent",
            "en-NZ",
            10,
        )
        return [{"text": state["content"]}] if state["content"] else []

    monkeypatch.setattr(
        concept_service, "get_concept_by_concept_id_exact", read_concept
    )
    monkeypatch.setattr(concept_service, "create_concept", create_concept)
    monkeypatch.setattr(text_value_service, "get_texts_for_concept", read_texts)
    monkeypatch.setattr(
        text_value_service, "upsert_singleton_text_relation", upsert_content
    )
    scope = {
        "actor_user_id": "#V#michael_witbrock",
        "organisation_concept_id": "#V#the_lu_witbrock_household",
        "namespace": "#V#michael_witbrock@the_lu_witbrock_household",
    }
    result = LiveCycleBackend().materialise_authority(
        kind="evaluator",
        expected={
            "concept_id": concept_id,
            "content": content,
            "content_sha256": _text_sha256(content),
        },
        scope=scope,
    )

    assert result["exists"] is True
    assert result["matches_expected"] is True
    assert result["content"] == content
    assert create_calls[0]["concept_id"] == concept_id
    assert create_calls[0]["created_by_concept_id"] == scope["actor_user_id"]
    assert (
        create_calls[0]["organisation_concept_id"] == scope["organisation_concept_id"]
    )
    assert create_calls[0]["event_namespace"] == scope["namespace"]
    assert create_calls[0]["visibility_scope_mode"] == "user_only_default"
    assert upsert_calls[0]["subject_concept_id"] == concept_id
    assert upsert_calls[0]["predicate"] == "hasContent"
    assert upsert_calls[0]["text"] == content
    assert upsert_calls[0]["identity_mode"] == "exact"


def test_live_authority_refuses_preexisting_multiple_content_rows_without_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import concept_service, text_value_service

    concept_id = "#V#prompt_learning_advice_capability_choice_trial_evaluator"
    content = "Represented evaluator bytes."
    upsert_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: {
            "concept_id": concept_id,
            "relationships": {
                "#V#specific_to_user": ["#V#michael_witbrock"],
            },
        },
    )
    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [
            {"text": content},
            {"text": content},
        ],
    )
    monkeypatch.setattr(
        text_value_service,
        "upsert_singleton_text_relation",
        lambda **kwargs: upsert_calls.append(kwargs),
    )

    with pytest.raises(LearningCycleError) as exc_info:
        LiveCycleBackend().materialise_authority(
            kind="evaluator",
            expected={
                "concept_id": concept_id,
                "content": content,
                "content_sha256": _text_sha256(content),
            },
            scope=_FIXED_SCOPE,
        )

    assert exc_info.value.reason_code == "jvnautosci_2720_authority_conflict"
    assert upsert_calls == []


def test_live_authority_refuses_normalised_collision_without_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import concept_service, text_value_service

    concept_id = "#V#prompt_learning_advice_capability_choice_trial_evaluator"
    expected_content = "Represented evaluator bytes."
    existing_content = "  represented   EVALUATOR bytes.  "
    upsert_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: {
            "concept_id": concept_id,
            "relationships": {
                "#V#specific_to_user": ["#V#michael_witbrock"],
            },
        },
    )
    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [{"text": existing_content}],
    )
    monkeypatch.setattr(
        text_value_service,
        "upsert_singleton_text_relation",
        lambda **kwargs: upsert_calls.append(kwargs),
    )

    with pytest.raises(LearningCycleError) as exc_info:
        LiveCycleBackend().materialise_authority(
            kind="evaluator",
            expected={
                "concept_id": concept_id,
                "content": expected_content,
                "content_sha256": _text_sha256(expected_content),
            },
            scope=_FIXED_SCOPE,
        )

    assert exc_info.value.reason_code == "jvnautosci_2720_authority_conflict"
    assert upsert_calls == []


def test_live_authority_refuses_preexisting_org_visible_concept_before_body_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import concept_service, text_value_service

    concept_id = "#V#prompt_learning_advice_capability_choice_trial_evaluator"
    content = "Represented evaluator bytes."
    upsert_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: {
            "concept_id": concept_id,
            "relationships": {
                "#V#specific_to_user": ["#V#michael_witbrock"],
                "#V#specific_to_organisation": ["#V#the_lu_witbrock_household"],
            },
        },
    )
    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        text_value_service,
        "upsert_singleton_text_relation",
        lambda **kwargs: upsert_calls.append(kwargs),
    )

    with pytest.raises(LearningCycleError) as exc_info:
        LiveCycleBackend().materialise_authority(
            kind="evaluator",
            expected={
                "concept_id": concept_id,
                "content": content,
                "content_sha256": _text_sha256(content),
            },
            scope=_FIXED_SCOPE,
        )

    assert exc_info.value.reason_code == (
        "jvnautosci_2720_authority_visibility_conflict"
    )
    assert upsert_calls == []


def test_live_authority_refuses_exact_content_in_preexisting_wider_concept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import concept_service, text_value_service

    concept_id = "#V#prompt_learning_advice_capability_choice_trial_evaluator"
    content = "Represented evaluator bytes."
    upsert_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id_exact",
        lambda _concept_id: {
            "concept_id": concept_id,
            "relationships": {
                "#V#specific_to_user": ["#V#michael_witbrock"],
                "#V#specific_to_organisation": ["#V#the_lu_witbrock_household"],
            },
        },
    )
    monkeypatch.setattr(
        text_value_service,
        "get_texts_for_concept",
        lambda *_args, **_kwargs: [{"text": content}],
    )
    monkeypatch.setattr(
        text_value_service,
        "upsert_singleton_text_relation",
        lambda **kwargs: upsert_calls.append(kwargs),
    )

    with pytest.raises(LearningCycleError) as exc_info:
        LiveCycleBackend().materialise_authority(
            kind="evaluator",
            expected={
                "concept_id": concept_id,
                "content": content,
                "content_sha256": _text_sha256(content),
            },
            scope=_FIXED_SCOPE,
        )

    assert exc_info.value.reason_code == (
        "jvnautosci_2720_authority_visibility_conflict"
    )
    assert upsert_calls == []


def test_live_experiment_spec_creation_is_actor_scoped_and_actor_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id,
    )
    from src.backend.services import experiment_run_service

    calls: list[dict[str, Any]] = []

    def create_spec(**kwargs: Any) -> dict[str, Any]:
        calls.append(
            {
                "kwargs": copy.deepcopy(kwargs),
                "effective_user_id": get_effective_user_concept_id(),
                "effective_org_id": get_effective_organisation_concept_id(),
            }
        )
        return {"success": True}

    monkeypatch.setattr(experiment_run_service, "create_experiment_spec", create_spec)

    LiveCycleBackend().create_experiment_spec(
        load_fixture(),
        candidate_ref={"candidate_id": "#V#actor_private_candidate"},
        scope=_FIXED_SCOPE,
    )

    assert len(calls) == 1
    assert calls[0]["effective_user_id"] == _FIXED_SCOPE["actor_user_id"]
    assert calls[0]["effective_org_id"] == _FIXED_SCOPE["organisation_concept_id"]
    assert calls[0]["kwargs"]["require_actor_only_visibility"] is True


def test_live_experiment_run_start_is_actor_scoped_and_actor_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import run_learning_advice_experiment
    from src.backend.security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id,
    )
    from src.backend.services import experiment_run_service

    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        run_learning_advice_experiment,
        "build_learning_advice_experiment_run_binding",
        lambda _manifest, *, plan: {
            "schema_version": "learning_advice_experiment_run_binding.v1",
            "binding_sha256": _sha256(plan),
        },
    )

    def start_run(**kwargs: Any) -> dict[str, Any]:
        calls.append(
            {
                "kwargs": copy.deepcopy(kwargs),
                "effective_user_id": get_effective_user_concept_id(),
                "effective_org_id": get_effective_organisation_concept_id(),
            }
        )
        return {"success": True}

    monkeypatch.setattr(experiment_run_service, "start_experiment_run", start_run)

    LiveCycleBackend().start_experiment_run(
        {
            "experiment_spec_id": "#V#actor_private_spec",
            "experiment_run_id": "#V#actor_private_run",
        },
        plan={"trial_ids": ["trial-1"]},
        scope=_FIXED_SCOPE,
    )

    assert len(calls) == 1
    assert calls[0]["effective_user_id"] == _FIXED_SCOPE["actor_user_id"]
    assert calls[0]["effective_org_id"] == _FIXED_SCOPE["organisation_concept_id"]
    assert calls[0]["kwargs"]["require_actor_only_visibility"] is True


def test_formation_result_contract_rejects_patch_like_or_oversized_output() -> None:
    valid = normalise_formation_result(
        {
            "schema_version": FORMATION_SCHEMA_VERSION,
            "outcome": "capture_one_candidate",
            "observed_pattern": "A bounded observation.",
            "candidate_body": "A revisable lesson.",
        }
    )
    assert valid["candidate_body"] == "A revisable lesson."

    with pytest.raises(LearningCycleError):
        normalise_formation_result(
            {
                "schema_version": FORMATION_SCHEMA_VERSION,
                "outcome": "capture_one_candidate",
                "observed_pattern": "A bounded observation.",
                "candidate_body": "x" * 901,
            }
        )


@pytest.mark.parametrize("alter_source_prompt", [False, True])
def test_protocol_revision_reuses_original_formation_without_reinduction(
    alter_source_prompt: bool,
) -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    record = backend.formation_records["existing-formation-request"]
    original_digest = learning_cycle._ORIGINAL_FORMATION_FIXTURE_SHA256
    record["aux_llm_calls"][0]["fixture_sha256"] = original_digest
    record["prompt"]["sha256"] = _text_sha256(
        "[JVNAUTOSCI-2720 source-only candidate formation; private source "
        f"omitted; fixture_sha256={original_digest}]"
    )
    if alter_source_prompt:
        record["aux_llm_calls"][0]["source_only_prompt_sha256"] = "0" * 64
        with pytest.raises(LearningCycleError, match="source-only formation"):
            execute_cycle(fixture, backend)
    else:
        result = execute_cycle(fixture, backend)
        assert result["formation"]["outcome"] == "existing_candidate_reused"
        assert not backend.formation_prompts


def test_reused_candidate_requires_its_exact_formation_ter() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    backend.formation_records.clear()

    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert exc_info.value.reason_code == (
        "jvnautosci_2720_candidate_formation_unverified"
    )
    assert not any(event.startswith("materialise:") for event in backend.write_events)


@pytest.mark.parametrize(
    "tamper",
    ("schema", "fixture", "source", "prompt", "body", "model", "scope"),
)
def test_reused_candidate_rejects_tampered_formation_provenance(
    tamper: str,
) -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    record = backend.formation_records["existing-formation-request"]
    diagnostic = record["aux_llm_calls"][0]
    if tamper == "schema":
        record["schema_version"] = "turn_execution_record.v0"
    elif tamper == "fixture":
        diagnostic["fixture_sha256"] = "0" * 64
    elif tamper == "source":
        diagnostic["source_evidence"][0]["content_sha256"] = "0" * 64
    elif tamper == "prompt":
        diagnostic["source_only_prompt_sha256"] = "0" * 64
    elif tamper == "body":
        diagnostic["candidate_body_sha256"] = "0" * 64
    elif tamper == "model":
        diagnostic["model_call_receipt"]["provider_observed_model"] = None
    else:
        record["org_id"] = "#V#another_organisation"

    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert exc_info.value.reason_code == (
        "jvnautosci_2720_candidate_formation_unverified"
    )


def test_fresh_candidate_rechecks_source_after_capture() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)
    original_capture = backend.capture_candidate
    original_load = backend.load_source_material
    source_changed = False

    def capture_then_change_source(**kwargs: Any) -> dict[str, Any]:
        nonlocal source_changed
        result = original_capture(**kwargs)
        source_changed = True
        return result

    def load_current_source(
        loaded_fixture: Mapping[str, Any], *, scope: Mapping[str, str]
    ) -> dict[str, Any]:
        source = original_load(loaded_fixture, scope=scope)
        if source_changed:
            source["messages"][0]["content"] += "-changed-after-capture"
            source["messages"][0]["content_sha256"] = _text_sha256(
                source["messages"][0]["content"]
            )
        return source

    backend.capture_candidate = capture_then_change_source  # type: ignore[method-assign]
    backend.load_source_material = load_current_source  # type: ignore[method-assign]

    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert exc_info.value.reason_code == (
        "jvnautosci_2720_candidate_formation_unverified"
    )
    assert not any(event.startswith("materialise:") for event in backend.write_events)


def test_preflight_is_read_only_and_never_outputs_private_bodies() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)
    summary = preflight_summary(prepare_cycle(fixture, backend))
    serialised = json.dumps(summary)

    assert summary["writes_performed"] is False
    assert summary["ready"] is True
    assert summary["readiness_reason_codes"] == []
    assert summary["model"]["request_sent"] is False
    assert summary["source"]["message_count"] == 12
    assert backend.write_events == []
    assert "generate_luna" not in backend.events
    assert _PRIVATE_SOURCE_TEXT not in serialised
    assert _PRIVATE_DIRECT_MESSAGE not in serialised
    assert _PRIVATE_GMAIL_SNIPPET not in serialised


def test_preflight_is_not_ready_when_the_run_would_reject_a_dirty_tree() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)
    backend.inspect_code_state = lambda *, require_clean: {
        "code_revision": "test-revision",
        "worktree_clean": False,
    }

    summary = preflight_summary(prepare_cycle(fixture, backend))

    assert summary["ready"] is False
    assert summary["readiness_reason_codes"] == ["worktree_not_clean"]


@pytest.mark.parametrize(
    ("direct_success", "direct_count", "expected_reason_codes"),
    [
        (
            False,
            0,
            {
                "message_list_direct_not_successful",
                "message_list_direct_below_minimum_count",
            },
        ),
        (True, 0, {"message_list_direct_below_minimum_count"}),
    ],
)
def test_failed_or_empty_frozen_direct_world_blocks_before_formation_and_writes(
    direct_success: bool,
    direct_count: int,
    expected_reason_codes: set[str],
) -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)
    backend.world.public_summary["message_list_direct_success"] = direct_success
    backend.world.public_summary["message_list_direct_count"] = direct_count

    summary = preflight_summary(prepare_cycle(fixture, backend))

    assert summary["ready"] is False
    assert set(summary["readiness_reason_codes"]) == expected_reason_codes
    assert backend.write_events == []
    assert "generate_luna" not in backend.events

    backend.events.clear()
    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert exc_info.value.reason_code == (
        "jvnautosci_2720_frozen_world_precondition_failed"
    )
    assert backend.write_events == []
    assert "generate_luna" not in backend.events


@pytest.mark.parametrize("authority_state_kind", ["mismatched", "duplicate"])
def test_existing_authority_conflict_blocks_before_formation_and_writes(
    authority_state_kind: str,
) -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)
    evaluator = fixture["blind_evaluator"]
    expected_content = evaluator["prompt"]
    if authority_state_kind == "mismatched":
        content = f"{expected_content}\nforeign"
        relation_count = 1
    else:
        content = expected_content
        relation_count = 2
    backend.authority[evaluator["concept_id"]] = {
        "concept_id": evaluator["concept_id"],
        "exists": True,
        "content": content,
        "content_sha256": _text_sha256(content),
        "content_relation_count": relation_count,
    }

    summary = preflight_summary(prepare_cycle(fixture, backend))

    assert summary["ready"] is False
    assert summary["readiness_reason_codes"] == [
        "represented_evaluator_authority_conflict"
    ]
    assert backend.write_events == []
    assert "generate_luna" not in backend.events

    backend.events.clear()
    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert exc_info.value.reason_code == "jvnautosci_2720_authority_conflict"
    assert backend.write_events == []
    assert "generate_luna" not in backend.events


def test_live_luna_call_rejects_an_unobserved_provider_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.backend.languagemodels import llm_interface

    class _Client:
        last_response_metadata: ClassVar[dict[str, Any]] = {
            "provider": FIXED_PROVIDER,
            "requested_model": FIXED_MODEL_ID,
            "effective_model": FIXED_MODEL_ID,
            "transport_metadata": {
                "provider": FIXED_PROVIDER,
                "requested_model": FIXED_MODEL_ID,
                "effective_model": FIXED_MODEL_ID,
                "provider_observed_model": None,
            },
        }

        def generate(self, **_kwargs: Any) -> str:
            return "{}"

    monkeypatch.setattr(llm_interface, "get_llm_client", lambda **_kwargs: _Client())

    with pytest.raises(LearningCycleError) as exc_info:
        LiveCycleBackend(repo_root=tmp_path).generate_luna(
            "source-only prompt",
            scope={
                "actor_user_id": "#V#michael_witbrock",
                "organisation_concept_id": "#V#the_lu_witbrock_household",
                "namespace": ("#V#michael_witbrock@the_lu_witbrock_household"),
            },
            parameters=FIXED_MODEL_PARAMETERS,
            purpose="candidate_formation",
        )

    assert exc_info.value.reason_code == "jvnautosci_2720_model_identity_mismatch"


def test_live_luna_call_keeps_trusted_actor_scope_active_through_generation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.backend.languagemodels import llm_interface
    from src.backend.security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id,
    )

    observed: list[tuple[str | None, str | None]] = []

    def current_scope() -> tuple[str | None, str | None]:
        return (
            get_effective_user_concept_id(),
            get_effective_organisation_concept_id(),
        )

    class _Client:
        last_response_metadata: ClassVar[dict[str, Any]] = {
            "provider": FIXED_PROVIDER,
            "requested_model": FIXED_MODEL_ID,
            "effective_model": FIXED_MODEL_ID,
            "transport_metadata": {
                "provider": FIXED_PROVIDER,
                "requested_model": FIXED_MODEL_ID,
                "effective_model": FIXED_MODEL_ID,
                "provider_observed_model": FIXED_MODEL_ID,
            },
        }

        def generate(self, **_kwargs: Any) -> str:
            observed.append(current_scope())
            return "{}"

    def get_client(**_kwargs: Any) -> _Client:
        observed.append(current_scope())
        return _Client()

    monkeypatch.setattr(llm_interface, "get_llm_client", get_client)

    generation = LiveCycleBackend(repo_root=tmp_path).generate_luna(
        "source-only prompt",
        scope=_FIXED_SCOPE,
        parameters=FIXED_MODEL_PARAMETERS,
        purpose="candidate_formation",
    )

    assert generation.receipt["provider_observed_model"] == FIXED_MODEL_ID
    assert observed == [
        (_FIXED_SCOPE["actor_user_id"], _FIXED_SCOPE["organisation_concept_id"]),
        (_FIXED_SCOPE["actor_user_id"], _FIXED_SCOPE["organisation_concept_id"]),
    ]


def test_live_experiment_acquires_its_model_client_before_starting_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.languagemodels import llm_interface

    fixture = load_fixture()
    world = _FakeBackend(fixture).world
    backend = LiveCycleBackend()
    started: list[dict[str, Any]] = []

    def unavailable_client(**_kwargs: Any) -> Any:
        raise RuntimeError("provider unavailable before run start")

    monkeypatch.setattr(llm_interface, "get_llm_client", unavailable_client)
    monkeypatch.setattr(
        backend,
        "start_experiment_run",
        lambda *args, **kwargs: started.append({"args": args, "kwargs": kwargs}),
    )

    with pytest.raises(RuntimeError, match="provider unavailable before run start"):
        backend.execute_experiment(
            {},
            plan={},
            frozen_world=world,
            scope=_FIXED_SCOPE,
            runtime_snapshot={},
            acting_support={},
            model_registry_snapshot={},
        )

    assert started == []


def _bound_live_start_test_inputs() -> tuple[
    dict[str, Any], dict[str, Any], FrozenMessageWorld
]:
    fixture = load_fixture()
    fake = _FakeBackend(fixture)
    execute_cycle(fixture, fake)
    assert fake.manifest is not None and fake.plan is not None
    return copy.deepcopy(fake.manifest), copy.deepcopy(fake.plan), fake.world


def test_live_experiment_reconciles_an_exact_run_persisted_before_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.run_learning_advice_experiment import (
        build_learning_advice_experiment_run_binding,
    )
    from src.backend.languagemodels import llm_interface
    from src.backend.services import experiment_run_service

    manifest, plan, world = _bound_live_start_test_inputs()
    binding = build_learning_advice_experiment_run_binding(manifest, plan=plan)
    state: dict[str, Any] = {}
    abort_calls: list[dict[str, Any]] = []

    def fail_after_persisting(
        _manifest: Mapping[str, Any],
        *,
        plan: Mapping[str, Any],
        scope: Mapping[str, str],
    ) -> None:
        assert _manifest["experiment_run_id"] == manifest["experiment_run_id"]
        assert plan["plan_sha256"] == binding["plan_sha256"]
        state.update(
            {
                "run_id": manifest["experiment_run_id"],
                "experiment_spec_id": manifest["experiment_spec_id"],
                "namespace": scope["namespace"],
                "user_id": scope["actor_user_id"],
                "org_id": scope["organisation_concept_id"],
                "status": "running",
                "verdict": None,
                "metadata": {"learning_advice_experiment": binding},
                "observations": [],
                "turn_execution_request_ids": [],
            }
        )
        raise LearningCycleError(
            "jvnautosci_2720_start_projection_failed",
            "The run projection failed after canonical persistence.",
        )

    def persist_abort_observation(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["run_id"] == manifest["experiment_run_id"]
        state["observations"].append(copy.deepcopy(kwargs["observations"]))
        return {"success": True, "run_id": kwargs["run_id"]}

    def abort_run(**kwargs: Any) -> dict[str, Any]:
        abort_calls.append(copy.deepcopy(kwargs))
        state.update(
            {
                "status": "failed",
                "verdict": "inconclusive",
                "completed_at_utc": "2026-09-04T12:30:00+00:00",
                "abort_summary": {
                    "reason_code": kwargs["reason_code"],
                    "binding_sha256": kwargs["expected_binding_sha256"],
                },
            }
        )
        return {"success": True, "experiment_run": copy.deepcopy(state)}

    backend = LiveCycleBackend()
    monkeypatch.setattr(llm_interface, "get_llm_client", lambda **_kwargs: object())
    monkeypatch.setattr(backend, "start_experiment_run", fail_after_persisting)
    monkeypatch.setattr(
        experiment_run_service,
        "get_experiment_run_state",
        lambda _run_id: copy.deepcopy(state) if state else None,
    )
    monkeypatch.setattr(
        experiment_run_service,
        "record_experiment_observation",
        persist_abort_observation,
    )
    monkeypatch.setattr(experiment_run_service, "abort_experiment_run", abort_run)

    with pytest.raises(LearningCycleError) as exc_info:
        backend.execute_experiment(
            manifest,
            plan=plan,
            frozen_world=world,
            scope=_FIXED_SCOPE,
            runtime_snapshot={},
            acting_support={},
            model_registry_snapshot={},
        )

    recovery = exc_info.value.recovery_evidence
    assert recovery["status"] == "canonically_aborted_and_read_back"
    assert recovery["run_id"] == manifest["experiment_run_id"]
    assert recovery["binding_sha256"] == binding["binding_sha256"]
    assert recovery["terminalisation"]["status"] == "confirmed"
    assert state["status"] == "failed"
    assert state["verdict"] == "inconclusive"
    assert len(abort_calls) == 1


@pytest.mark.parametrize("existing_state", ["absent", "foreign_binding"])
def test_live_experiment_does_not_abort_an_unattested_start_failure(
    monkeypatch: pytest.MonkeyPatch,
    existing_state: str,
) -> None:
    from scripts.run_learning_advice_experiment import (
        build_learning_advice_experiment_run_binding,
    )
    from src.backend.languagemodels import llm_interface
    from src.backend.services import experiment_run_service

    manifest, plan, world = _bound_live_start_test_inputs()
    binding = build_learning_advice_experiment_run_binding(manifest, plan=plan)
    state = None
    if existing_state == "foreign_binding":
        foreign_binding = copy.deepcopy(binding)
        foreign_binding["binding_sha256"] = "f" * 64
        state = {
            "run_id": manifest["experiment_run_id"],
            "experiment_spec_id": manifest["experiment_spec_id"],
            "namespace": _FIXED_SCOPE["namespace"],
            "user_id": _FIXED_SCOPE["actor_user_id"],
            "org_id": _FIXED_SCOPE["organisation_concept_id"],
            "status": "running",
            "verdict": None,
            "metadata": {"learning_advice_experiment": foreign_binding},
            "observations": [],
            "turn_execution_request_ids": [],
        }
    abort_calls: list[dict[str, Any]] = []

    backend = LiveCycleBackend()
    monkeypatch.setattr(llm_interface, "get_llm_client", lambda **_kwargs: object())
    monkeypatch.setattr(
        backend,
        "start_experiment_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            LearningCycleError(
                "jvnautosci_2720_start_failed",
                "The run did not start.",
            )
        ),
    )
    monkeypatch.setattr(
        experiment_run_service,
        "get_experiment_run_state",
        lambda _run_id: copy.deepcopy(state),
    )
    monkeypatch.setattr(
        experiment_run_service,
        "record_experiment_observation",
        lambda **_kwargs: pytest.fail("an unattested run must not be mutated"),
    )
    monkeypatch.setattr(
        experiment_run_service,
        "abort_experiment_run",
        lambda **kwargs: abort_calls.append(copy.deepcopy(kwargs)),
    )

    with pytest.raises(LearningCycleError) as exc_info:
        backend.execute_experiment(
            manifest,
            plan=plan,
            frozen_world=world,
            scope=_FIXED_SCOPE,
            runtime_snapshot={},
            acting_support={},
            model_registry_snapshot={},
        )

    assert abort_calls == []
    recovery = exc_info.value.recovery_evidence
    assert recovery["status"] == "terminalisation_unconfirmed"
    assert recovery["terminalisation"] == {
        "status": "unconfirmed",
        "failure_stage": "pre_abort_binding_attestation",
    }


def test_frozen_read_requires_the_exact_post_binding_request() -> None:
    expected = {
        "include_received": True,
        "limit": 5,
        "acting_user_concept_id": "#V#michael_witbrock",
    }
    captured = {"success": True, "count": 5, "messages": ["private"]}
    handler = build_exact_frozen_read_handler(expected, captured)

    assert handler(**expected) == captured
    assert handler(include_received=True, limit=5) == {
        "success": False,
        "error_code": "frozen_world_request_mismatch",
        "message": "The request differs from the predeclared frozen read.",
    }
    assert handler(**expected, thread_id="#V#another_thread")["error_code"] == (
        "frozen_world_request_mismatch"
    )


def test_execute_cycle_forms_captures_and_runs_one_inspectable_candidate() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)
    result = execute_cycle(fixture, backend)
    serialised = json.dumps(result)

    assert result["status"] == "represented_arm_c_required"
    assert result["candidate"]["body"] == _CANDIDATE_BODY
    assert result["candidate"]["evaluation_disposition"] == "retained"
    assert result["experiment"]["planned_trial_count"] == 24
    assert "candidate_disposition" not in result["experiment"]
    assert result["experiment"]["canonical_readback"]["terminal"] is True
    assert result["post_disposition"]["status"] == "represented_arm_c_required"
    assert backend.write_events == [
        "persist_formation_record",
        "capture_candidate",
        "materialise:evaluator",
        "materialise:rubric",
        "create_experiment_spec",
        "start_experiment_run",
        "execute_experiment",
    ]
    assert backend.formation_prompts
    assert fixture["cases"][0]["prompt"] not in backend.formation_prompts[0]
    assert _PRIVATE_SOURCE_TEXT not in serialised
    assert _PRIVATE_DIRECT_MESSAGE not in serialised
    assert _PRIVATE_GMAIL_SNIPPET not in serialised


def test_no_durable_lesson_is_a_terminal_result_not_a_text_placeholder() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, formation_outcome="no_durable_lesson")
    result = execute_cycle(fixture, backend)

    assert result["status"] == "completed_without_candidate"
    assert result["candidate"] is None
    assert result["experiment"] is None
    assert backend.write_events == ["persist_formation_record"]


def test_negative_result_runs_and_persists_two_no_projection_checks() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, experiment_decision="arm_b_not_supported")
    result = execute_cycle(fixture, backend)

    assert result["status"] == "completed_negative_learning_cycle"
    assert result["candidate"]["evaluation_disposition"] == "rejected"
    assert result["post_disposition"]["status"] == (
        "rejected_revision_absence_verified"
    )
    assert result["post_disposition"]["check_count"] == 2
    assert all(
        check["candidate_revision_absent"] is True
        and check["learning_advice_exposure_count"] == 0
        for check in result["post_disposition"]["checks"]
    )
    assert backend.write_events[-1] == "execute_post_disposition_checks"


def test_execute_cycle_refuses_a_runner_result_that_diverges_from_readback() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)
    execute = backend.execute_experiment

    def divergent_result(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = execute(*args, **kwargs)
        result["result_evidence_sha256"] = "f" * 64
        return result

    backend.execute_experiment = divergent_result  # type: ignore[method-assign]

    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert exc_info.value.reason_code == "jvnautosci_2720_result_readback_mismatch"


def test_rejected_candidate_can_resume_an_interrupted_post_check() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, experiment_decision="arm_b_not_supported")
    first = execute_cycle(fixture, backend)
    assert first["status"] == "completed_negative_learning_cycle"
    backend.write_events.clear()

    resumed = execute_cycle(fixture, backend)

    assert resumed["status"] == "completed_negative_learning_cycle"
    assert resumed["experiment"]["resumed_post_disposition_check"] is True
    assert resumed["experiment"]["run_id"] == first["experiment"]["run_id"]
    assert resumed["post_disposition"]["status"] == (
        "rejected_revision_absence_verified"
    )
    assert backend.write_events == ["execute_post_disposition_checks"]


def test_rejected_candidate_resume_reloads_every_trial_ter() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, experiment_decision="arm_b_not_supported")
    first = execute_cycle(fixture, backend)
    assert first["status"] == "completed_negative_learning_cycle"
    assert backend.run is not None
    backend.turn_execution_records.pop(backend.run["turn_execution_request_ids"][0])

    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert exc_info.value.reason_code == ("jvnautosci_2720_trial_ter_readback_mismatch")


def test_rejected_disposition_causally_blocks_the_exact_projection() -> None:
    fixture = load_fixture()
    candidate = _FakeBackend(fixture)._candidate()
    candidate["evaluation_disposition"] = "rejected"

    attestation = attest_rejected_candidate_projection_block(
        candidate,
        experiment_run_id="#V#run_test",
        check_kind="trigger",
    )

    assert attestation["status"] == "blocked_by_canonical_rejected_disposition"
    assert attestation["reason_code"] == "learning_advice_candidate_ineligible"
    assert attestation["candidate_id"] == candidate["candidate_id"]
    assert attestation["candidate_revision"] == candidate["revision"]
    digest = attestation.pop("attestation_sha256")
    assert digest == _sha256(attestation)


def test_post_check_rejects_a_revision_that_remains_projectable() -> None:
    fixture = load_fixture()
    candidate = _FakeBackend(fixture)._candidate()

    with pytest.raises(LearningCycleError) as exc_info:
        attest_rejected_candidate_projection_block(
            candidate,
            experiment_run_id="#V#run_test",
            check_kind="trigger",
        )

    assert exc_info.value.reason_code == "jvnautosci_2720_rejected_revision_projectable"


def test_post_check_does_not_mislabel_other_projection_failures_as_rejection() -> None:
    fixture = load_fixture()
    candidate = _FakeBackend(fixture)._candidate()
    candidate["evaluation_disposition"] = "rejected"
    candidate["body_sha256"] = "0" * 64

    with pytest.raises(LearningCycleError) as exc_info:
        attest_rejected_candidate_projection_block(
            candidate,
            experiment_run_id="#V#run_test",
            check_kind="trigger",
        )

    assert exc_info.value.reason_code == (
        "jvnautosci_2720_rejected_projection_check_failed"
    )


def _post_disposition_ter_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    diagnostic = {
        "schema_version": "jvnautosci_2720_post_disposition_check.v1",
        "experiment_run_id": "#V#jvnautosci_2720_run_test",
        "check_kind": "trigger",
        "candidate_id": "#V#learning_candidate_test_2720",
        "candidate_revision": 1,
        "candidate_body_sha256": "a" * 64,
        "candidate_evaluation_disposition": "rejected",
        "projection_supplied": False,
        "projection_prevention": {
            "status": "blocked_by_canonical_rejected_disposition"
        },
        "model_requests": [
            {
                "model_call_id": "post-turn:llm:1",
                "request_sha256": "b" * 64,
                "candidate_revision_absent": True,
            }
        ],
        "learning_advice_exposure_count": 0,
    }
    record = {
        "schema_version": "turn_execution_record.v1",
        "request_id": "post-trigger-request",
        "session_id": "post-trigger-session",
        "namespace": _FIXED_SCOPE["namespace"],
        "actor_concept_id": _FIXED_SCOPE["actor_user_id"],
        "user_id": _FIXED_SCOPE["actor_user_id"],
        "org_id": _FIXED_SCOPE["organisation_concept_id"],
        "prompt": {"sha256": "c" * 64},
        "final_response": {"response_sha256": "d" * 64},
        "aux_llm_calls": [
            {
                "type": "jvnautosci_2720_post_disposition_check",
                **copy.deepcopy(diagnostic),
            }
        ],
    }
    return record, diagnostic


def test_post_disposition_ter_readback_binds_the_exact_built_record() -> None:
    from src.backend.services.turn_execution_record_service import (
        turn_execution_record_evidence_sha256,
    )

    record, diagnostic = _post_disposition_ter_fixture()
    readback = {
        **copy.deepcopy(record),
        "updated_at_utc": "2026-09-04T12:30:00+00:00",
        "inserted_at": object(),
        "_id": object(),
    }

    digest = learning_cycle._validate_post_disposition_ter_readback(
        readback,
        expected_record=record,
        expected_diagnostic=diagnostic,
        scope=_FIXED_SCOPE,
    )

    expected_persisted = {
        **record,
        "updated_at_utc": readback["updated_at_utc"],
    }
    assert digest == turn_execution_record_evidence_sha256(expected_persisted)


@pytest.mark.parametrize(
    "tamper",
    (
        "schema",
        "scope",
        "session",
        "prompt",
        "response",
        "run",
        "check",
        "candidate",
        "request_attestation",
    ),
)
def test_post_disposition_ter_readback_rejects_any_bound_evidence_change(
    tamper: str,
) -> None:
    record, diagnostic = _post_disposition_ter_fixture()
    readback = copy.deepcopy(record)
    binding = readback["aux_llm_calls"][0]
    if tamper == "schema":
        readback["schema_version"] = "turn_execution_record.v0"
    elif tamper == "scope":
        readback["org_id"] = "#V#foreign_org"
    elif tamper == "session":
        readback["session_id"] = "foreign-session"
    elif tamper == "prompt":
        readback["prompt"]["sha256"] = "e" * 64
    elif tamper == "response":
        readback["final_response"]["response_sha256"] = "e" * 64
    elif tamper == "run":
        binding["experiment_run_id"] = "#V#foreign_run"
    elif tamper == "check":
        binding["check_kind"] = "control"
    elif tamper == "candidate":
        binding["candidate_id"] = "#V#foreign_candidate"
    else:
        binding["model_requests"][0]["request_sha256"] = "e" * 64

    with pytest.raises(LearningCycleError) as exc_info:
        learning_cycle._validate_post_disposition_ter_readback(
            readback,
            expected_record=record,
            expected_diagnostic=diagnostic,
            scope=_FIXED_SCOPE,
        )

    assert exc_info.value.reason_code == "jvnautosci_2720_post_ter_readback_failed"


def test_existing_exact_candidate_is_reused_without_another_formation_call() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    result = execute_cycle(fixture, backend)

    assert result["formation"]["outcome"] == "existing_candidate_reused"
    assert "generate_luna" not in backend.events
    assert "persist_formation_record" not in backend.write_events
    assert "capture_candidate" not in backend.write_events


def test_readback_rejects_a_foreign_observation() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    execute_cycle(fixture, backend)
    assert backend.run is not None
    backend.run["observations"].append(
        {
            "observation_type": "paired_trial",
            "raw_response": _PRIVATE_DIRECT_MESSAGE,
            "raw_result": _PRIVATE_GMAIL_SNIPPET,
        }
    )
    with pytest.raises(LearningCycleError) as exc_info:
        build_readback(
            backend.run,
            backend=backend,
            scope={
                "actor_user_id": "#V#michael_witbrock",
                "organisation_concept_id": "#V#the_lu_witbrock_household",
                "namespace": "#V#michael_witbrock@the_lu_witbrock_household",
            },
        )

    assert exc_info.value.reason_code == "jvnautosci_2720_result_observation_invalid"


def test_readback_accepts_generic_service_normalised_observations() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    execute_cycle(fixture, backend)
    assert backend.run is not None

    readback = build_readback(backend.run, backend=backend, scope=_FIXED_SCOPE)

    assert readback["observation_count"] == 25
    assert readback["decisive"] is True
    assert readback["result"]["evidence_verdict"] == "supports_use"


def test_readback_rejects_a_valid_but_foreign_run_binding() -> None:
    from scripts.run_learning_advice_experiment import (
        build_learning_advice_experiment_run_binding,
    )
    from src.backend.services.learning_advice_experiment_service import (
        build_learning_advice_experiment_plan,
    )

    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    execute_cycle(fixture, backend)
    assert backend.run is not None and backend.manifest is not None
    foreign_manifest = copy.deepcopy(backend.manifest)
    foreign_manifest["experiment_run_id"] = "#V#foreign_learning_advice_run"
    foreign_manifest.pop("manifest_sha256", None)
    foreign_manifest["manifest_sha256"] = _sha256(foreign_manifest)
    foreign_plan = build_learning_advice_experiment_plan(foreign_manifest)
    backend.run["metadata"]["learning_advice_experiment"] = (
        build_learning_advice_experiment_run_binding(
            foreign_manifest,
            plan=foreign_plan,
        )
    )

    with pytest.raises(LearningCycleError) as exc_info:
        build_readback(backend.run, backend=backend, scope=_FIXED_SCOPE)

    assert exc_info.value.reason_code == "jvnautosci_2720_run_binding_mismatch"


def test_readback_rejects_a_bad_result_evidence_digest() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    execute_cycle(fixture, backend)
    assert backend.run is not None
    final = backend.run["observations"][-1]["evidence"]["learning_advice_result"]
    final["result_evidence_sha256"] = "0" * 64

    with pytest.raises(LearningCycleError) as exc_info:
        build_readback(backend.run, backend=backend, scope=_FIXED_SCOPE)

    assert exc_info.value.reason_code == "jvnautosci_2720_result_digest_mismatch"


@pytest.mark.parametrize("tamper", ("missing", "replaced"))
def test_readback_reloads_every_current_canonical_trial_ter(tamper: str) -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    execute_cycle(fixture, backend)
    assert backend.run is not None
    request_id = backend.run["turn_execution_request_ids"][0]
    if tamper == "missing":
        backend.turn_execution_records.pop(request_id)
    else:
        backend.turn_execution_records[request_id]["org_id"] = "#V#foreign_org"

    with pytest.raises(LearningCycleError) as exc_info:
        build_readback(backend.run, backend=backend, scope=_FIXED_SCOPE)

    assert exc_info.value.reason_code == ("jvnautosci_2720_trial_ter_readback_mismatch")


def test_readback_recomputes_and_rejects_a_redigested_paired_result() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    execute_cycle(fixture, backend)
    assert backend.run is not None
    final_observation = backend.run["observations"][-1]
    final = final_observation["evidence"]["learning_advice_result"]
    final["paired_results"]["comparison"] = "tie"
    digest_payload = copy.deepcopy(final)
    digest_payload.pop("result_evidence_sha256")
    redigested = _sha256(digest_payload)
    final["result_evidence_sha256"] = redigested
    final_observation["observation_id"] = (
        f"{backend.run['run_id']}:learning-advice-result:{redigested[:24]}"
    )
    final_observation["observed_outcome"]["result_evidence_sha256"] = redigested

    with pytest.raises(LearningCycleError) as exc_info:
        build_readback(backend.run, backend=backend, scope=_FIXED_SCOPE)

    assert exc_info.value.reason_code == (
        "jvnautosci_2720_result_recomputation_mismatch"
    )


def test_failed_inconclusive_abort_is_terminal_but_never_decisive() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    execute_cycle(fixture, backend)
    assert backend.run is not None
    binding = backend.run["metadata"]["learning_advice_experiment"]
    aborted_at = "2026-09-04T12:01:00+00:00"
    abort_summary = {
        "schema_version": "experiment_run_abort.v1",
        "reason_code": "learning_advice_experiment_request_contaminated",
        "binding_metadata_key": "learning_advice_experiment",
        "binding_sha256": binding["binding_sha256"],
        "preserved_observation_count": len(backend.run["observations"]),
        "preserved_turn_execution_request_count": len(
            backend.run["turn_execution_request_ids"]
        ),
        "aborted_at_utc": aborted_at,
    }
    backend.run.update(
        {
            "status": "failed",
            "verdict": "inconclusive",
            "completed_at_utc": aborted_at,
            "abort_summary": abort_summary,
            "evidence": {"experiment_run_abort": copy.deepcopy(abort_summary)},
        }
    )

    readback = build_readback(
        backend.run,
        backend=backend,
        scope=_FIXED_SCOPE,
    )

    assert readback["terminal"] is True
    assert readback["decisive"] is False
    assert readback["result"] is None
    assert readback["abort_summary"] == abort_summary


def test_failed_fail_run_can_be_a_decisive_content_negative() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(
        fixture,
        existing_candidate=True,
        experiment_decision="arm_b_not_supported",
    )
    execute_cycle(fixture, backend)
    assert backend.run is not None
    backend.run["status"] = "failed"
    backend.run["verdict"] = "fail"

    readback = build_readback(
        backend.run,
        backend=backend,
        scope=_FIXED_SCOPE,
    )

    assert readback["terminal"] is True
    assert readback["decisive"] is True
    assert readback["abort_summary"] is None
    assert readback["result"]["evidence_verdict"] == "does_not_support_use"


def test_readback_resolves_the_exact_frozen_prior_candidate_revision() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    execute_cycle(fixture, backend)
    assert backend.candidate is not None and backend.run is not None
    frozen = copy.deepcopy(backend.candidate)
    backend.candidate["prior_revisions"] = [frozen]
    backend.candidate["revision"] = 2
    backend.candidate["body"] = "A later candidate revision."
    backend.candidate["body_sha256"] = _text_sha256(backend.candidate["body"])
    backend.candidate["revision_identity_sha256"] = "b" * 64
    backend.candidate["evaluation_disposition"] = "undecided"

    readback = build_readback(
        backend.run,
        backend=backend,
        scope=_FIXED_SCOPE,
    )

    assert readback["candidate"]["revision"] == 1
    assert readback["candidate"]["body"] == _CANDIDATE_BODY
    assert readback["candidate"]["revision_identity_sha256"] == "a" * 64


def test_rejected_resume_requires_the_exact_disposition_evidence_digest() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, experiment_decision="arm_b_not_supported")
    execute_cycle(fixture, backend)
    assert backend.candidate is not None
    latest = backend.candidate["disposition_history"][-1]
    latest["evidence_sha256"] = "f" * 64
    identity = {
        field: latest[field]
        for field in (
            "disposition_request_id",
            "evaluation_disposition",
            "evidence_verdict",
            "experiment_run_id",
            "evidence_sha256",
            "candidate_revision",
            "candidate_revision_identity_sha256",
        )
    }
    latest["disposition_identity_sha256"] = _sha256(identity)

    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert (
        exc_info.value.reason_code == "jvnautosci_2720_negative_run_readback_mismatch"
    )


@pytest.mark.parametrize(
    ("tamper", "expected_reason"),
    (
        ("spec", "jvnautosci_2720_fixture_binding_mismatch"),
        ("model", "jvnautosci_2720_fixture_binding_mismatch"),
        ("case", "jvnautosci_2720_fixture_binding_mismatch"),
        ("evaluator", "jvnautosci_2720_fixture_binding_mismatch"),
        ("rubric", "jvnautosci_2720_fixture_binding_mismatch"),
        ("repeats", "jvnautosci_2720_fixture_binding_mismatch"),
        ("seed", "jvnautosci_2720_fixture_binding_mismatch"),
        ("decision_rule", "jvnautosci_2720_fixture_binding_mismatch"),
        ("trusted_scope", "jvnautosci_2720_run_binding_mismatch"),
    ),
)
def test_rejected_resume_requires_the_exact_current_fixture_binding(
    tamper: str,
    expected_reason: str,
) -> None:
    from scripts.run_learning_advice_experiment import (
        build_learning_advice_experiment_run_binding,
    )
    from src.backend.services.learning_advice_experiment_service import (
        build_learning_advice_experiment_manifest,
        build_learning_advice_experiment_plan,
    )

    fixture = load_fixture()
    backend = _FakeBackend(fixture, experiment_decision="arm_b_not_supported")
    execute_cycle(fixture, backend)
    assert backend.manifest is not None and backend.run is not None
    original = backend.manifest
    cases = copy.deepcopy(original["cases"])
    decision_rule = copy.deepcopy(original["decision_rule"])
    experiment_spec_id = original["experiment_spec_id"]
    model_id = original["model"]["model_id"]
    repeats = original["repeats"]
    seed = original["randomisation_seed"]
    trusted_scope = copy.deepcopy(original["trusted_scope"])
    if tamper == "spec":
        experiment_spec_id = "#V#foreign_message_channel_experiment"
        backend.run["experiment_spec_id"] = experiment_spec_id
    elif tamper == "model":
        model_id = "gpt-5.5"
    elif tamper == "case":
        cases[0]["prompt"] += " Foreign held-out wording."
    elif tamper == "evaluator":
        cases[0]["evaluator_concept_id"] = "#V#foreign_evaluator"
    elif tamper == "rubric":
        cases[0]["rubric_sha256"] = "f" * 64
    elif tamper == "repeats":
        repeats = 4
    elif tamper == "seed":
        seed = 2721
    elif tamper == "decision_rule":
        decision_rule["minimum_applicable_pass_delta"] = 1
    else:
        trusted_scope = {
            "actor_user_id": "#V#foreign_actor",
            "organisation_concept_id": "#V#foreign_org",
            "namespace": "#V#foreign_actor@foreign_org",
        }
    foreign_manifest = build_learning_advice_experiment_manifest(
        experiment_spec_id=experiment_spec_id,
        experiment_run_id=original["experiment_run_id"],
        candidate_ref=original["candidate_ref"],
        actor_user_id=trusted_scope["actor_user_id"],
        organisation_concept_id=trusted_scope["organisation_concept_id"],
        namespace=trusted_scope["namespace"],
        provider=original["model"]["provider"],
        model_id=model_id,
        model_parameters=original["model"]["parameters"],
        runtime_snapshot=original["runtime_snapshot"],
        cases=cases,
        decision_rule=decision_rule,
        randomisation_seed=seed,
        repeats=repeats,
    )
    foreign_plan = build_learning_advice_experiment_plan(foreign_manifest)
    backend.run["metadata"]["learning_advice_experiment"] = (
        build_learning_advice_experiment_run_binding(
            foreign_manifest,
            plan=foreign_plan,
        )
    )

    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert exc_info.value.reason_code == expected_reason


@pytest.mark.parametrize(
    ("field", "foreign_value"),
    (
        ("evidence_verdict", "supports_use"),
        ("candidate_revision", 2),
        ("candidate_revision_identity_sha256", "e" * 64),
    ),
)
def test_rejected_resume_rejects_mismatched_disposition_identity_fields(
    field: str,
    foreign_value: Any,
) -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, experiment_decision="arm_b_not_supported")
    execute_cycle(fixture, backend)
    assert backend.candidate is not None
    latest = backend.candidate["disposition_history"][-1]
    latest[field] = foreign_value
    identity = {
        identity_field: latest[identity_field]
        for identity_field in (
            "disposition_request_id",
            "evaluation_disposition",
            "evidence_verdict",
            "experiment_run_id",
            "evidence_sha256",
            "candidate_revision",
            "candidate_revision_identity_sha256",
        )
    }
    latest["disposition_identity_sha256"] = _sha256(identity)

    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert exc_info.value.reason_code == (
        "jvnautosci_2720_rejected_candidate_evidence_mismatch"
    )


def test_rejected_resume_refuses_an_aborted_run_even_with_old_negative_evidence() -> (
    None
):
    fixture = load_fixture()
    backend = _FakeBackend(fixture, experiment_decision="arm_b_not_supported")
    execute_cycle(fixture, backend)
    assert backend.run is not None
    binding = backend.run["metadata"]["learning_advice_experiment"]
    aborted_at = "2026-09-04T12:02:00+00:00"
    abort_summary = {
        "schema_version": "experiment_run_abort.v1",
        "reason_code": "learning_advice_experiment_request_contaminated",
        "binding_metadata_key": "learning_advice_experiment",
        "binding_sha256": binding["binding_sha256"],
        "preserved_observation_count": len(backend.run["observations"]),
        "preserved_turn_execution_request_count": len(
            backend.run["turn_execution_request_ids"]
        ),
        "aborted_at_utc": aborted_at,
    }
    backend.run.update(
        {
            "status": "failed",
            "verdict": "inconclusive",
            "completed_at_utc": aborted_at,
            "abort_summary": abort_summary,
            "evidence": {"experiment_run_abort": copy.deepcopy(abort_summary)},
        }
    )

    with pytest.raises(LearningCycleError) as exc_info:
        execute_cycle(fixture, backend)

    assert (
        exc_info.value.reason_code == "jvnautosci_2720_negative_run_readback_mismatch"
    )


def test_blind_evaluator_prompt_has_authority_and_no_trusted_receipt() -> None:
    prompt = build_blind_evaluator_prompt(
        blind_input={
            "schema_version": "capability_choice_trial_evaluation_input.v1",
            "prompt": "Summarise my messages.",
            "response": "A grounded summary.",
        },
        evaluator_content="represented-evaluator-content",
        rubric_content='{"represented":"rubric"}',
    )
    assert "represented-evaluator-content" in prompt
    assert '"represented":"rubric"' in prompt
    assert "model_call_receipt" not in prompt
    assert "candidate_id" not in prompt
    assert '"arm"' not in prompt


def test_main_preflight_and_readback_emit_only_safe_json() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture, existing_candidate=True)
    out = io.StringIO()
    err = io.StringIO()
    assert (
        main(
            ["--preflight", "--fixture", str(DEFAULT_FIXTURE_PATH)],
            backend=backend,
            stdout=out,
            stderr=err,
        )
        == 0
    )
    assert err.getvalue() == ""
    assert _PRIVATE_SOURCE_TEXT not in out.getvalue()

    execute_cycle(fixture, backend)
    out = io.StringIO()
    assert (
        main(
            [
                "--readback",
                "--run-id",
                "#V#jvnautosci_2720_learning_advice_run_test",
                "--fixture",
                str(DEFAULT_FIXTURE_PATH),
            ],
            backend=backend,
            stdout=out,
            stderr=err,
        )
        == 0
    )
    assert _PRIVATE_DIRECT_MESSAGE not in out.getvalue()


def test_main_redacts_unexpected_exception_text() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"secret={_PRIVATE_SOURCE_TEXT}")

    backend.load_source_material = explode  # type: ignore[method-assign]
    out = io.StringIO()
    err = io.StringIO()
    status = main(
        ["--preflight", "--fixture", str(DEFAULT_FIXTURE_PATH)],
        backend=backend,
        stdout=out,
        stderr=err,
    )

    assert status == 1
    assert out.getvalue() == ""
    assert _PRIVATE_SOURCE_TEXT not in err.getvalue()
    assert "RuntimeError" in err.getvalue()


def test_main_surfaces_typed_unconfirmed_experiment_terminalisation() -> None:
    fixture = load_fixture()
    backend = _FakeBackend(fixture)
    run_id = "#V#jvnautosci_2720_learning_advice_run_test"
    recovery = {
        "schema_version": "learning_advice_experiment_abort_recovery.v1",
        "status": "terminalisation_unconfirmed",
        "run_id": run_id,
        "reason_code": "learning_advice_experiment_request_contaminated",
        "error_class": "LearningAdviceExperimentRunnerError",
        "manifest_sha256": "a" * 64,
        "plan_sha256": "b" * 64,
        "binding_sha256": "c" * 64,
        "abort_observation": {
            "status": "persisted",
            "observation_id": f"{run_id}:learning-advice-abort:abc123",
        },
        "terminalisation": {
            "status": "unconfirmed",
            "failure_stage": "abort_finaliser",
            "error_class": "RuntimeError",
        },
        "candidate_disposition_attempted": False,
    }

    def fail_experiment(*_args: Any, **_kwargs: Any) -> Any:
        error = RuntimeError(f"private={_PRIVATE_SOURCE_TEXT}")
        error.__dict__["recovery_evidence"] = recovery
        raise error

    backend.execute_experiment = fail_experiment  # type: ignore[method-assign]
    out = io.StringIO()
    err = io.StringIO()

    status = main(
        ["--run", "--fixture", str(DEFAULT_FIXTURE_PATH)],
        backend=backend,
        stdout=out,
        stderr=err,
    )
    payload = json.loads(err.getvalue())

    assert status == 1
    assert out.getvalue() == ""
    assert payload["reason_code"] == recovery["reason_code"]
    assert payload["experiment_run_id"] == run_id
    assert payload["recovery_evidence"] == recovery
    assert _PRIVATE_SOURCE_TEXT not in err.getvalue()
