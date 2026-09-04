from __future__ import annotations

import copy
import hashlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.run_learning_advice_experiment import (
    LEARNING_ADVICE_BLIND_EVALUATION_RESULT_SCHEMA_VERSION,
    LEARNING_ADVICE_EVALUATOR_MODEL_CALL_RECEIPT_SCHEMA_VERSION,
    LearningAdviceExperimentReadOnlyViolation,
    LearningAdviceExperimentRunnerError,
    build_learning_advice_acting_support_identity,
    build_learning_advice_evaluator_model_config,
    build_learning_advice_experiment_run_binding,
    learning_advice_read_only_gateway_catalogue_sha256,
    run_learning_advice_experiment,
    validate_learning_advice_experiment_run_binding,
)
from src.backend.services import experiment_run_service
from src.backend.services.adaptive_turn_service import AdaptiveTurnResult
from src.backend.services.learning_advice_experiment_service import (
    LearningAdviceExperimentDriftError,
    build_learning_advice_experiment_manifest,
    build_learning_advice_experiment_plan,
    frozen_candidate_ref,
)
from src.backend.services.learning_advice_projection_service import (
    build_learning_advice_exposure_record,
    prepare_learning_advice_projection,
    render_learning_advice_projection,
)

_ACTOR_ID = "#V#runner_test_actor"
_ORGANISATION_ID = "#V#runner_test_organisation"
_NAMESPACE = "#V#runner_test_actor@runner_test_organisation"
_CANDIDATE_ID = "#V#runner_test_learning_candidate"
_CANDIDATE_BODY = (
    "When a request could use more than one plausible capability, inspect the "
    "shared situation and available evidence before choosing. If the first "
    "bounded read fails, try another plausible bounded read before concluding."
)
_BODY_SHA256 = hashlib.sha256(_CANDIDATE_BODY.encode("utf-8")).hexdigest()
_EVALUATOR_CONTENT = (
    "Evaluate only the supplied held-out outcome against the separate rubric. "
    "Return typed capability-choice and work-product verdicts."
)
_RUBRIC_CONTENT = (
    "A pass requires the requested bounded read work product and a suitable "
    "capability choice under the supplied case contract."
)
_EVALUATOR_SHA256 = hashlib.sha256(_EVALUATOR_CONTENT.encode("utf-8")).hexdigest()
_RUBRIC_SHA256 = hashlib.sha256(_RUBRIC_CONTENT.encode("utf-8")).hexdigest()
_SOURCE_TEXT = (
    "In the earlier discussion Von chose one plausible read and stopped after "
    "its failure even though another bounded source remained available."
)
_CONTEXT = ({"role": "system", "content": "Stable test context."},)
_TRUSTED_ARGUMENTS = {"bounded": True}
_WORKFLOW_INPUTS = {"fixture": "held-out"}
_MODEL_REGISTRY = {"revision": "test-registry"}
_MODEL_PARAMETERS = {"temperature": 0.2, "max_output_tokens": 4_000}
_GATEWAY_IDENTITY_SHA256 = "5" * 64
_REPLAY_WORLD_SHA256 = "6" * 64
_REPLAY_VISIBLE_TEXTS = [
    "A synthetic held-out message that is independent of the learning source."
]


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _FakeGateway:
    enabled = True

    def __init__(self) -> None:
        self.base_invocations: list[str] = []
        self.methods = {
            "primary_bounded_read": {
                "category": "read",
                "ordinary_turn_effect": False,
                "ordinary_turn_public": False,
                "description": "Read one bounded item from the synthetic world.",
            },
            "dangerous_write": {
                "category": "write",
                "ordinary_turn_effect": True,
                "ordinary_turn_public": False,
                "description": "Change the synthetic world.",
            },
        }

    def describe_methods(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.methods)

    def get_method_definition(self, method_name: str) -> Any:
        metadata = self.methods.get(method_name)
        return SimpleNamespace(**metadata) if metadata is not None else None

    def get_method_timeout_sec(self, method_name: str) -> None:
        del method_name

    def invoke(self, method_name: str, *_args: Any, **_kwargs: Any) -> None:
        self.base_invocations.append(method_name)
        raise AssertionError("The fake base gateway should not be invoked in this test")


def _gateway_snapshot() -> dict[str, Any]:
    return {
        "gateway_identity_sha256": _GATEWAY_IDENTITY_SHA256,
        "replay_world_sha256": _REPLAY_WORLD_SHA256,
        "model_visible_texts": list(_REPLAY_VISIBLE_TEXTS),
    }


def _candidate(*, evaluation_disposition: str = "undecided") -> dict[str, Any]:
    return {
        "schema_version": "learning_candidate.v1",
        "lifecycle_state": "non_active",
        "evaluation_disposition": evaluation_disposition,
        "candidate_id": _CANDIDATE_ID,
        "revision": 1,
        "body": _CANDIDATE_BODY,
        "body_sha256": _BODY_SHA256,
        "revision_identity_sha256": "a" * 64,
        "source_locator_sha256": "b" * 64,
        "target_concept_ids": ["#V#ordinary_turn_capability_choice"],
        "authorship": {"author_concept_id": "#V#von_system"},
        "visibility": {
            "scope": "actor",
            "actor_user_id": _ACTOR_ID,
            "organisation_concept_id": _ORGANISATION_ID,
        },
        "namespace": _NAMESPACE,
        "source": {"kind": "conversation"},
    }


def _runtime_snapshot(
    *,
    context: tuple[Mapping[str, Any], ...] = _CONTEXT,
    trusted_argument_values: Mapping[str, Any] | None = _TRUSTED_ARGUMENTS,
    workflow_launch_inputs: Mapping[str, Any] | None = _WORKFLOW_INPUTS,
    model_registry_snapshot: Any = _MODEL_REGISTRY,
    model_parameters: Mapping[str, Any] = _MODEL_PARAMETERS,
    replay_visible_texts: list[str] = _REPLAY_VISIBLE_TEXTS,
) -> dict[str, Any]:
    capability_sha256 = learning_advice_read_only_gateway_catalogue_sha256(
        _FakeGateway()
    )
    gateway_identity = {
        "gateway_identity_sha256": _GATEWAY_IDENTITY_SHA256,
        "replay_world_sha256": _REPLAY_WORLD_SHA256,
        "model_visible_texts_sha256": _canonical_sha256(replay_visible_texts),
    }
    acting_support = build_learning_advice_acting_support_identity(
        context=context,
        trusted_argument_values=trusted_argument_values,
        workflow_launch_inputs=workflow_launch_inputs,
        model_registry_snapshot=model_registry_snapshot,
        model_parameters=model_parameters,
        gateway_snapshot_identity=gateway_identity,
        capability_catalogue_sha256=capability_sha256,
        allow_represented_workflow_discovery=False,
        allow_represented_tool_projection=False,
    )
    return {
        "code_revision": "runner-test-tree-2720",
        "capability_catalogue_sha256": capability_sha256,
        "workflow_catalogue_sha256": "2" * 64,
        "relevant_ontology_sha256": "3" * 64,
        "acting_support_sha256": acting_support["acting_support_sha256"],
        "context_budget_tokens": 32_000,
        "turn_budget_seconds": 180.0,
        "final_synthesis_reserve_seconds": 30.0,
        "final_answer_reserve_seconds": 0.0,
    }


def _cases() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(1, 7):
        applicable = index <= 4
        rows.append(
            {
                "case_id": f"runner-case-{index}",
                "kind": "applicable" if applicable else "control",
                "prompt": (
                    f"Applicable held-out request {index}"
                    if applicable
                    else f"Control held-out request {index}"
                ),
                "evaluator_concept_id": "#V#runner_blind_evaluator",
                "evaluator_sha256": _EVALUATOR_SHA256,
                "rubric_concept_id": "#V#runner_case_rubric",
                "rubric_sha256": _RUBRIC_SHA256,
                "evaluation_contract": {
                    "allowed_first_capabilities": [
                        "primary_bounded_read",
                        "alternative_bounded_read",
                    ],
                    "required_success_capability": (
                        "primary_bounded_read" if applicable else None
                    ),
                    "after_capability_failure": "try_other_plausible_capability",
                    "focused_clarification_verdict": "partial",
                    "unsolicited_alternative_policy": "allowed_for_recovery",
                    "read_only": True,
                },
            }
        )
    return rows


def _manifest(
    *,
    runtime_snapshot: Mapping[str, Any] | None = None,
    cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return build_learning_advice_experiment_manifest(
        experiment_spec_id="#V#runner_experiment_spec",
        experiment_run_id="#V#runner_experiment_run",
        candidate_ref=frozen_candidate_ref(_candidate()),
        actor_user_id=_ACTOR_ID,
        organisation_concept_id=_ORGANISATION_ID,
        namespace=_NAMESPACE,
        provider="openai",
        model_id="gpt-5.6-luna",
        model_parameters=_MODEL_PARAMETERS,
        runtime_snapshot=runtime_snapshot or _runtime_snapshot(),
        cases=cases or _cases(),
        decision_rule={
            "minimum_applicable_b_passes": 6,
            "minimum_applicable_pass_delta": 2,
            "require_more_paired_improvements_than_regressions": True,
            "forbid_b_only_material_failure": True,
            "require_control_non_inferiority": True,
        },
        randomisation_seed=2_720,
    )


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, Mapping):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.01
        return self.value


class _Harness:
    def __init__(self, manifest: Mapping[str, Any]) -> None:
        self.manifest = copy.deepcopy(dict(manifest))
        self.candidate_reads = 0
        self.source_text_reads = 0
        self.runtime_reads = 0
        self.gateway_snapshot_reads = 0
        self.authority_reads: list[tuple[str, str, dict[str, Any]]] = []
        self.executor_calls: list[dict[str, Any]] = []
        self.provider_requests: list[dict[str, Any]] = []
        self.ter_records: list[dict[str, Any]] = []
        self.ter_store: dict[str, dict[str, Any]] = {}
        self.ter_persist_calls: list[dict[str, Any]] = []
        self.evaluator_inputs: list[dict[str, Any]] = []
        self.evaluator_executor_configs: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.identity_number = 0
        self.gateway = _FakeGateway()
        self.current_disposition = str(
            self.manifest["candidate_ref"]["evaluation_disposition"]
        )
        self.disposition_calls: list[dict[str, Any]] = []
        self.run_status = "running"
        self.run_verdict: str | None = None
        self.run_completed_at: str | None = None
        self.run_abort_summary: dict[str, Any] | None = None
        self.abort_calls: list[dict[str, Any]] = []
        self.run_binding = build_learning_advice_experiment_run_binding(self.manifest)
        self.run_turn_execution_request_ids: list[str] = []

    def identity_factory(self, kind: str) -> str:
        self.identity_number += 1
        return f"{kind}-{self.identity_number}"

    def candidate_loader(self, candidate_id: str, **scope: Any) -> dict[str, Any]:
        self.candidate_reads += 1
        assert candidate_id == _CANDIDATE_ID
        assert scope == {
            "actor_user_id": _ACTOR_ID,
            "organisation_concept_id": _ORGANISATION_ID,
            "namespace": _NAMESPACE,
        }
        return _candidate(evaluation_disposition=self.current_disposition)

    def runtime_loader(self, **scope: Any) -> dict[str, Any]:
        self.runtime_reads += 1
        assert scope == {
            "actor_user_id": _ACTOR_ID,
            "organisation_concept_id": _ORGANISATION_ID,
            "namespace": _NAMESPACE,
        }
        return copy.deepcopy(self.manifest["runtime_snapshot"])

    def source_text_loader(
        self, source: Mapping[str, Any], **scope: Any
    ) -> dict[str, Any]:
        self.source_text_reads += 1
        assert source["kind"] == "conversation"
        assert scope == {
            "actor_user_id": _ACTOR_ID,
            "organisation_concept_id": _ORGANISATION_ID,
            "namespace": _NAMESPACE,
        }
        return {
            "source_locator_sha256": _candidate()["source_locator_sha256"],
            "texts": [
                {
                    "content": _SOURCE_TEXT,
                    "content_sha256": hashlib.sha256(
                        _SOURCE_TEXT.encode("utf-8")
                    ).hexdigest(),
                }
            ],
        }

    def gateway_snapshot_loader(self, gateway: Any, **scope: Any) -> dict[str, Any]:
        self.gateway_snapshot_reads += 1
        assert gateway is self.gateway
        assert scope == {
            "actor_user_id": _ACTOR_ID,
            "organisation_concept_id": _ORGANISATION_ID,
            "namespace": _NAMESPACE,
        }
        return _gateway_snapshot()

    def authority_loader(
        self, concept_id: str, expected_sha256: str, **scope: Any
    ) -> dict[str, Any]:
        self.authority_reads.append((concept_id, expected_sha256, dict(scope)))
        assert scope == {
            "actor_user_id": _ACTOR_ID,
            "organisation_concept_id": _ORGANISATION_ID,
            "namespace": _NAMESPACE,
        }
        content = {
            "#V#runner_blind_evaluator": _EVALUATOR_CONTENT,
            "#V#runner_case_rubric": _RUBRIC_CONTENT,
        }[concept_id]
        return {
            "concept_id": concept_id,
            "content": content,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    def execute(self, **kwargs: Any) -> AdaptiveTurnResult:
        self.executor_calls.append(copy.deepcopy(kwargs))
        projection = prepare_learning_advice_projection(
            kwargs["learning_advice_projection"],
            actor_user_id=kwargs["user_concept_id"],
            organisation_concept_id=kwargs["org_concept_id"],
            namespace=kwargs["user_namespace"],
        )
        arm = projection["arm"]
        exposure = build_learning_advice_exposure_record(projection)
        model_call_id = f"model-call-{len(self.executor_calls)}"
        rendered = render_learning_advice_projection(projection)
        if arm == "A":
            assert rendered is None
            assert projection["items"] == []
        else:
            assert rendered is not None
            assert _CANDIDATE_BODY in rendered
            exposure.update(
                {
                    "exposure_status": "exposed",
                    "model_visible": True,
                    "model_visible_call_ids": [model_call_id],
                    "completed_model_visible_call_ids": [model_call_id],
                }
            )

        system_message = "Stable held-out acting support."
        if rendered is not None:
            system_message += "\n\n" + rendered
        provider_request = {
            "schema_version": "adaptive_turn_model_request_observation.v1",
            "model_call_id": model_call_id,
            "stage": "adaptive_research",
            "prompt": kwargs["prompt"],
            "tools": [
                {
                    "name": name,
                    "description": definition.get("description"),
                    "input_schema": definition.get("input_schema"),
                    "output_schema": definition.get("output_schema"),
                }
                for name, definition in kwargs["gateway"].describe_methods().items()
            ],
            "context": copy.deepcopy(kwargs["context"]),
            "model": kwargs["model"],
            "system_message": system_message,
            "model_parameters": copy.deepcopy(kwargs["model_parameters"]),
            "continuation": None,
            "tool_results": [],
        }
        kwargs["model_request_observer"](provider_request)
        self.provider_requests.append(copy.deepcopy(provider_request))

        applicable = kwargs["prompt"].startswith("Applicable")
        successful = arm == "B" or not applicable
        response = (
            "Successful bounded work product."
            if successful
            else "The requested work product was not obtained."
        )
        return AdaptiveTurnResult(
            response_text=response,
            extra_messages=(),
            tool_invocations=(
                {
                    "tool_name": "primary_bounded_read",
                    "status": "succeeded" if successful else "failed",
                    "semantic_effect": False,
                    "evidence": {
                        "success": successful,
                        "summary": response,
                        "count": 1 if successful else 0,
                    },
                },
            ),
            aux_llm_calls=(exposure,),
            llm_calls=(
                {
                    "call_id": model_call_id,
                    "stage": "adaptive_research",
                    "provider": "openai",
                    "requested_model": "gpt-5.6-luna",
                    "selected_model": "gpt-5.6-luna",
                    "effective_model": "gpt-5.6-luna",
                    "provider_observed_model": "gpt-5.6-luna",
                    "provider_request_sent": True,
                    "success": True,
                    "duration_ms": 7.0,
                },
            ),
            duration_ms=20.0 if arm == "B" else 10.0,
            terminal_status="completed",
        )

    def build_ter(self, **kwargs: Any) -> dict[str, Any]:
        record = {
            "schema_version": "turn_execution_record.v1",
            "request_id": kwargs["request_id"],
            "session_id": kwargs["session_id"],
            "namespace": kwargs["namespace"],
            "actor_concept_id": kwargs["actor_concept_id"],
            "user_id": kwargs["user_id"],
            "org_id": kwargs["org_id"],
            "prompt": {
                "sha256": hashlib.sha256(
                    kwargs["prompt_text"].encode("utf-8")
                ).hexdigest()
            },
            "final_response": {
                "response_sha256": hashlib.sha256(
                    kwargs["response_text"].encode("utf-8")
                ).hexdigest()
            },
            "aux_llm_calls": copy.deepcopy(kwargs["aux_llm_calls"]),
            "diagnostics": copy.deepcopy(kwargs["turn_execution_diagnostics"]),
        }
        self.ter_records.append(record)
        return record

    def persist_ter(self, **kwargs: Any) -> dict[str, Any]:
        self.ter_persist_calls.append(copy.deepcopy(kwargs))
        self.ter_store[kwargs["record"]["request_id"]] = copy.deepcopy(kwargs["record"])
        return {"updated": True, "request_id": kwargs["record"]["request_id"]}

    def load_ter(self, *, request_id: str, namespace: str) -> dict[str, Any] | None:
        assert namespace == _NAMESPACE
        record = self.ter_store.get(request_id)
        return copy.deepcopy(record) if record is not None else None

    def evaluate(
        self,
        value: Mapping[str, Any],
        *,
        evaluator_content: str,
        rubric_content: str,
        model_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        projection = copy.deepcopy(dict(value))
        self.evaluator_inputs.append(projection)
        self.evaluator_executor_configs.append(
            {
                "evaluator_content": evaluator_content,
                "rubric_content": rubric_content,
                "model_config": copy.deepcopy(dict(model_config)),
            }
        )
        assert evaluator_content == _EVALUATOR_CONTENT
        assert rubric_content == _RUBRIC_CONTENT
        assert model_config == build_learning_advice_evaluator_model_config(
            self.manifest
        )
        for forbidden in (
            "arm",
            "candidate_id",
            "candidate_ref",
            "experiment_run_id",
            "experiment_spec_id",
            "evaluator_concept_id",
            "evaluator_sha256",
            "learning_advice_exposures",
            "projection_sha256",
            "rubric_concept_id",
            "rubric_sha256",
        ):
            assert not _contains_key(projection, forbidden)
        encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        for identity in self.manifest["candidate_ref"].values():
            if isinstance(identity, str):
                assert identity not in encoded
        for hidden_identity in (
            self.manifest["experiment_spec_id"],
            self.manifest["experiment_run_id"],
            "#V#runner_blind_evaluator",
            _EVALUATOR_SHA256,
            "#V#runner_case_rubric",
            _RUBRIC_SHA256,
        ):
            assert hidden_identity not in encoded
        assert projection["evaluation_contract"]["read_only"] is True
        assert projection["bounded_tool_results"]
        passed = projection["response"].startswith("Successful")
        return {
            "schema_version": LEARNING_ADVICE_BLIND_EVALUATION_RESULT_SCHEMA_VERSION,
            "verdict": "pass" if passed else "fail",
            "capability_choice": "pass" if passed else "fail",
            "work_product": "pass" if passed else "fail",
            "material_failure": False,
            "external_availability_changed": False,
            "reason_codes": [
                "work_product_observed" if passed else "work_product_absent"
            ],
            "model_call_receipt": {
                "schema_version": (
                    LEARNING_ADVICE_EVALUATOR_MODEL_CALL_RECEIPT_SCHEMA_VERSION
                ),
                "call_id": f"evaluator-call-{len(self.evaluator_inputs)}",
                "provider": model_config["provider"],
                "requested_model": model_config["model_id"],
                "selected_model": model_config["model_id"],
                "effective_model": model_config["model_id"],
                "provider_observed_model": model_config["model_id"],
                "provider_request_sent": True,
                "success": True,
            },
        }

    def persist_observation(self, **kwargs: Any) -> dict[str, Any]:
        self.observations.append(copy.deepcopy(dict(kwargs["observations"])))
        for request_id in kwargs.get("turn_execution_request_ids") or []:
            if request_id not in self.run_turn_execution_request_ids:
                self.run_turn_execution_request_ids.append(request_id)
        return {"success": True, "run_id": kwargs["run_id"]}

    def finalise_run(self, *, run_id: str) -> dict[str, Any]:
        assert run_id == self.manifest["experiment_run_id"]
        verdicts = [item.get("verdict") for item in self.observations]
        if verdicts and all(verdict == "fail" for verdict in verdicts):
            self.run_verdict = "fail"
            self.run_status = "failed"
        elif verdicts and all(verdict == "inconclusive" for verdict in verdicts):
            self.run_verdict = "inconclusive"
            self.run_status = "completed"
        else:
            # The generic run verdict deliberately remains distinct from the
            # paired content decision.
            self.run_verdict = "partial"
            self.run_status = "completed"
        self.run_completed_at = "2026-09-04T18:00:00Z"
        return {"success": True, "run_id": run_id, "verdict": self.run_verdict}

    def abort_run(self, **kwargs: Any) -> dict[str, Any]:
        self.abort_calls.append(copy.deepcopy(kwargs))
        assert kwargs == {
            "run_id": self.manifest["experiment_run_id"],
            "reason_code": kwargs["reason_code"],
            "binding_metadata_key": "learning_advice_experiment",
            "expected_binding_sha256": self.run_binding["binding_sha256"],
        }
        self.run_status = "failed"
        self.run_verdict = "inconclusive"
        self.run_completed_at = "2026-09-04T18:00:01Z"
        self.run_abort_summary = {
            "schema_version": "experiment_run_abort.v1",
            "reason_code": kwargs["reason_code"],
            "binding_metadata_key": "learning_advice_experiment",
            "binding_sha256": self.run_binding["binding_sha256"],
            "preserved_observation_count": len(self.observations),
            "preserved_turn_execution_request_count": len(
                self.run_turn_execution_request_ids
            ),
            "aborted_at_utc": self.run_completed_at,
        }
        return {
            "success": True,
            "run_id": kwargs["run_id"],
            "status": self.run_status,
            "verdict": self.run_verdict,
            "abort_summary": copy.deepcopy(self.run_abort_summary),
        }

    def record_disposition(self, candidate_id: str, **kwargs: Any) -> dict[str, Any]:
        self.disposition_calls.append(
            {"candidate_id": candidate_id, **copy.deepcopy(kwargs)}
        )
        final = self.observations[-1]["evidence"]["learning_advice_result"]
        decision = final["content_decision"]["decision"]
        self.current_disposition = {
            "arm_b_content_win": "retained",
            "arm_b_not_supported": "rejected",
        }[decision]
        return {
            **_candidate(evaluation_disposition=self.current_disposition),
            "disposition_recorded": True,
            "idempotent": False,
        }

    def load_run(self, run_id: str) -> dict[str, Any]:
        assert run_id == self.manifest["experiment_run_id"]
        return {
            "run_id": run_id,
            "experiment_spec_id": self.manifest["experiment_spec_id"],
            "namespace": _NAMESPACE,
            "user_id": _ACTOR_ID,
            "org_id": _ORGANISATION_ID,
            "metadata": {"learning_advice_experiment": copy.deepcopy(self.run_binding)},
            "status": self.run_status,
            "verdict": self.run_verdict,
            "completed_at_utc": self.run_completed_at,
            "abort_summary": copy.deepcopy(self.run_abort_summary),
            "turn_execution_request_ids": copy.deepcopy(
                self.run_turn_execution_request_ids
            ),
            "observations": copy.deepcopy(self.observations),
        }


def _run(
    manifest: Mapping[str, Any], harness: _Harness, **overrides: Any
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "gateway": harness.gateway,
        "llm_client": object(),
        "evaluate_blind_trial": harness.evaluate,
        "evaluator_authority_loader": harness.authority_loader,
        "runtime_snapshot_loader": harness.runtime_loader,
        "gateway_snapshot_loader": harness.gateway_snapshot_loader,
        "candidate_source_text_loader": harness.source_text_loader,
        "context": _CONTEXT,
        "trusted_argument_values": _TRUSTED_ARGUMENTS,
        "workflow_launch_inputs": _WORKFLOW_INPUTS,
        "model_registry_snapshot": _MODEL_REGISTRY,
        "candidate_loader": harness.candidate_loader,
        "candidate_disposition_recorder": harness.record_disposition,
        "candidate_disposition_loader": harness.candidate_loader,
        "adaptive_turn_executor": harness.execute,
        "turn_record_builder": harness.build_ter,
        "turn_record_persister": harness.persist_ter,
        "turn_record_loader": harness.load_ter,
        "experiment_observation_persister": harness.persist_observation,
        "experiment_run_loader": harness.load_run,
        "experiment_run_finaliser": harness.finalise_run,
        "experiment_run_abort_finaliser": harness.abort_run,
        "identity_factory": harness.identity_factory,
        "timestamp_factory": lambda: datetime(2026, 9, 4, tzinfo=UTC),
        "monotonic": _Clock(),
    }
    values.update(overrides)
    return run_learning_advice_experiment(manifest, **values)


def _assert_confirmed_abort(
    error: Exception, harness: _Harness, *, reason_code: str
) -> None:
    recovery = error.__dict__["recovery_evidence"]
    assert recovery["status"] == "canonically_aborted_and_read_back"
    assert recovery["run_id"] == harness.manifest["experiment_run_id"]
    assert recovery["reason_code"] == reason_code
    assert recovery["binding_sha256"] == harness.run_binding["binding_sha256"]
    assert recovery["terminalisation"]["status"] == "confirmed"
    assert recovery["terminalisation"]["terminal_status"] == "failed"
    assert recovery["terminalisation"]["verdict"] == "inconclusive"
    assert recovery["candidate_disposition_attempted"] is False
    assert harness.run_status == "failed"
    assert harness.run_verdict == "inconclusive"
    assert len(harness.abort_calls) == 1
    assert harness.disposition_calls == []


def test_runner_executes_persists_blindly_evaluates_and_pairs_all_24_trials() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    result = _run(manifest, harness)

    assert result["status"] == "completed"
    assert result["planned_trial_count"] == 24
    assert result["executed_trial_count"] == 24
    assert result["persisted_ter_count"] == 24
    assert result["persisted_trial_observation_count"] == 24
    assert result["persisted_observation_count"] == 25
    assert result["valid_trial_count"] == 24
    assert result["completed_evaluation_count"] == 24
    assert harness.candidate_reads == 26
    assert harness.source_text_reads == 1
    assert harness.runtime_reads == 24
    assert harness.gateway_snapshot_reads == 24
    assert len(harness.executor_calls) == 24
    assert len(harness.ter_records) == 24
    assert len(harness.ter_persist_calls) == 24
    assert len(harness.evaluator_inputs) == 24
    assert len(harness.evaluator_executor_configs) == 24
    assert len(harness.authority_reads) == 50
    assert len(harness.provider_requests) == 24
    assert len(harness.observations) == 25

    assert result["paired_results"]["pair_count"] == 12
    assert result["paired_results"]["valid_pair_count"] == 12
    assert result["paired_results"]["applicable"] == {
        "pair_count": 8,
        "required_comparable_pair_count": 8,
        "comparable_pair_count": 8,
        "availability_discounted_pair_count": 0,
        "a_pass_count": 0,
        "b_pass_count": 8,
        "b_better_count": 8,
        "a_better_count": 0,
    }
    assert result["paired_results"]["controls"]["negative_transfer_count"] == 0
    assert result["paired_results"]["controls"]["required_comparable_pair_count"] == 4
    assert result["content_decision"]["decision"] == "arm_b_content_win"
    assert result["content_decision"]["activation_authorised"] is False
    assert result["candidate_ref"]["evaluation_disposition"] == "undecided"
    assert result["candidate_disposition"]["evaluation_disposition"] == "retained"
    assert result["candidate_disposition"]["status"] == (
        "canonically_recorded_and_read_back"
    )
    assert len(harness.disposition_calls) == 1
    assert "verdict" not in harness.disposition_calls[0]
    assert "evidence_sha256" not in harness.disposition_calls[0]
    assert result["secondary_metrics"]["A"]["pass_count"] == 4
    assert result["secondary_metrics"]["B"]["pass_count"] == 12
    assert result["secondary_metrics"]["A"]["model_calls"]["total"] == 12
    assert result["secondary_metrics"]["B"]["model_calls"]["total"] == 12

    calls_by_arm: dict[str, list[Mapping[str, Any]]] = {"A": [], "B": []}
    for call in harness.executor_calls:
        calls_by_arm[call["learning_advice_projection"]["arm"]].append(call)
        assert call["model"] == "gpt-5.6-luna"
        assert call["model_parameters"] == manifest["model"]["parameters"]
        assert call["turn_budget_seconds"] == 180.0
        assert call["final_synthesis_reserve_seconds"] == 30.0
        assert call["final_answer_reserve_seconds"] == 0.0
        assert call["allow_represented_workflow_discovery"] is False
        assert call["allow_represented_tool_projection"] is False
        assert call["user_concept_id"] == _ACTOR_ID
        assert call["org_concept_id"] == _ORGANISATION_ID
        assert call["user_namespace"] == _NAMESPACE
    assert len(calls_by_arm["A"]) == len(calls_by_arm["B"]) == 12
    assert all(
        set(call["gateway"].describe_methods()) == {"primary_bounded_read"}
        for call in harness.executor_calls
    )

    request_ids = {call["record"]["request_id"] for call in harness.ter_persist_calls}
    session_ids = {record["session_id"] for record in harness.ter_records}
    turn_ids = {
        record["diagnostics"]["learning_advice_experiment"]["turn_id"]
        for record in harness.ter_records
    }
    assert len(request_ids) == len(session_ids) == len(turn_ids) == 24

    encoded_result = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert _CANDIDATE_BODY not in encoded_result
    assert _EVALUATOR_CONTENT not in encoded_result
    assert _RUBRIC_CONTENT not in encoded_result
    assert '"body"' not in encoded_result
    assert all(
        observation["execution"]["runtime_attestation"]["status"]
        == "canonically_attested"
        for observation in result["observations"]
    )
    assert all(
        len(observation["execution"]["model_request_attestations"]) == 1
        for observation in result["observations"]
    )
    assert result["canonical_persistence"]["readback"]["status"] == (
        "canonically_terminal_and_read_back"
    )
    assert result["canonical_persistence"]["readback"]["terminal_status"] == (
        "completed"
    )
    assert result["canonical_persistence"]["readback"]["generic_verdict"] == ("partial")
    assert len(result["trial_observation_ids"]) == 24
    trial_envelopes = [
        item
        for item in harness.observations
        if item["observation_type"] == "learning_advice_trial"
    ]
    final_envelopes = [
        item
        for item in harness.observations
        if item["observation_type"] == "learning_advice_experiment_result"
    ]
    assert len(trial_envelopes) == 24
    assert len(final_envelopes) == 1
    persisted_result = final_envelopes[0]["evidence"]["learning_advice_result"]
    digest_payload = copy.deepcopy(persisted_result)
    supplied_digest = digest_payload.pop("result_evidence_sha256")
    assert supplied_digest == _canonical_sha256(digest_payload)
    assert persisted_result["content_decision"] == result["content_decision"]
    assert persisted_result["trial_observation_ids"] == result["trial_observation_ids"]
    assert "turn_execution_request_ids" not in persisted_result
    for envelope in (trial_envelopes[0], final_envelopes[0]):
        normalised = experiment_run_service._normalise_observation(
            envelope,
            now_iso="2026-09-04T00:00:00Z",
        )
        assert normalised is not None
        assert normalised["observation_type"] == envelope["observation_type"]
        assert normalised["evidence"] == envelope["evidence"]
    assert all(
        len(observation["evaluation"]["authority_attestations"]) == 2
        for observation in result["observations"]
    )
    assert all(
        observation["evaluation"]["evaluation_provenance"]["effective_model"]
        == manifest["model"]["model_id"]
        for observation in result["observations"]
    )
    assert all(
        observation["evaluation"]["model_call_receipt"]["provider_observed_model"]
        == manifest["model"]["model_id"]
        and observation["evaluation"]["evaluation_provenance"][
            "model_call_receipt_sha256"
        ]
        == _canonical_sha256(observation["evaluation"]["model_call_receipt"])
        for observation in result["observations"]
    )


def test_runner_requires_a_fresh_canonical_run_bound_to_manifest_and_plan() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def wrong_binding(run_id: str) -> dict[str, Any]:
        state = harness.load_run(run_id)
        state["metadata"]["learning_advice_experiment"]["plan_sha256"] = "0" * 64
        return state

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(manifest, harness, experiment_run_loader=wrong_binding)

    assert error.value.reason_code == "learning_advice_experiment_run_preflight_invalid"
    assert harness.candidate_reads == 0
    assert harness.executor_calls == []
    assert harness.observations == []
    assert harness.abort_calls == []
    recovery = error.value.__dict__["recovery_evidence"]
    assert recovery["status"] == "terminalisation_unconfirmed"
    assert recovery["terminalisation"] == {
        "status": "unconfirmed",
        "failure_stage": "pre_abort_binding_attestation",
    }


def test_runner_aborts_a_bound_run_that_is_not_fresh_at_preflight() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)
    harness.observations.append(
        {
            "schema_version": "synthetic_preflight_observation.v1",
            "observation_id": "pre-existing-observation",
            "observation_type": "synthetic_preflight_observation",
            "verdict": "inconclusive",
            "evidence": {},
        }
    )

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(manifest, harness)

    assert error.value.reason_code == "learning_advice_experiment_run_preflight_invalid"
    _assert_confirmed_abort(
        error.value,
        harness,
        reason_code="learning_advice_experiment_run_preflight_invalid",
    )


def test_run_binding_persists_inspectable_preimages_without_semantic_bodies() -> None:
    manifest = _manifest()
    plan = build_learning_advice_experiment_plan(manifest)
    binding = build_learning_advice_experiment_run_binding(manifest, plan=plan)
    assert validate_learning_advice_experiment_run_binding(binding) == binding

    manifest_preimage = binding["body_free_manifest_preimage"]
    plan_preimage = binding["body_free_plan_preimage"]
    assert len(manifest_preimage["cases"]) == 6
    assert len(plan_preimage["trials"]) == 24
    assert [trial["order_index"] for trial in plan_preimage["trials"]] == list(
        range(1, 25)
    )
    assert all(
        len(case["prompt_utf8_sha256"]) == 64 for case in manifest_preimage["cases"]
    )
    assert all(
        len(trial["prompt_utf8_sha256"]) == 64 for trial in plan_preimage["trials"]
    )
    assert binding["body_free_manifest_preimage_sha256"] == _canonical_sha256(
        manifest_preimage
    )
    assert binding["body_free_plan_preimage_sha256"] == _canonical_sha256(plan_preimage)
    digest_payload = copy.deepcopy(binding)
    supplied_digest = digest_payload.pop("binding_sha256")
    assert supplied_digest == _canonical_sha256(digest_payload)
    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True)
    assert _CANDIDATE_BODY not in encoded
    assert _EVALUATOR_CONTENT not in encoded
    assert _RUBRIC_CONTENT not in encoded
    assert all(case["prompt"] not in encoded for case in manifest["cases"])
    for forbidden_key in ("prompt", "body", "content", "evaluator_content"):
        assert not _contains_key(binding, forbidden_key)


def test_runner_rejects_a_tampered_supplied_plan_before_any_trial() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)
    plan = build_learning_advice_experiment_plan(manifest)
    plan["trials"][0]["prompt"] = "A post-freeze prompt"

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(manifest, harness, plan=plan)

    assert error.value.reason_code == "learning_advice_experiment_plan_mismatch"
    assert harness.runtime_reads == 0
    assert harness.candidate_reads == 0
    assert harness.executor_calls == []


def test_runner_aborts_on_candidate_drift_before_blind_evaluation() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def drifting_loader(candidate_id: str, **scope: Any) -> dict[str, Any]:
        candidate = harness.candidate_loader(candidate_id, **scope)
        if harness.candidate_reads == 6:
            candidate["revision"] = 2
        return candidate

    with pytest.raises(LearningAdviceExperimentDriftError) as error:
        _run(manifest, harness, candidate_loader=drifting_loader)

    assert (
        error.value.reason_code == "learning_advice_experiment_candidate_identity_drift"
    )
    assert harness.candidate_reads == 6
    assert harness.runtime_reads == 5
    assert len(harness.executor_calls) == 4
    assert harness.evaluator_inputs == []
    assert len(harness.observations) == 1
    assert harness.observations[0]["observation_type"] == (
        "learning_advice_experiment_abort"
    )
    assert (
        harness.observations[0]["evidence"]["learning_advice_abort"]["observation_kind"]
        == "runner_abort"
    )
    assert _CANDIDATE_BODY not in json.dumps(harness.observations[0])
    _assert_confirmed_abort(
        error.value,
        harness,
        reason_code="learning_advice_experiment_candidate_identity_drift",
    )


@pytest.mark.parametrize("readback_kind", ["missing", "different"])
def test_runner_aborts_when_acknowledged_trial_ter_does_not_read_back_exactly(
    readback_kind: str,
) -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def invalid_readback(*, request_id: str, namespace: str):
        if readback_kind == "missing":
            return None
        record = harness.load_ter(request_id=request_id, namespace=namespace)
        assert record is not None
        record["final_response"]["response_sha256"] = "0" * 64
        return record

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(manifest, harness, turn_record_loader=invalid_readback)

    assert error.value.reason_code == "learning_advice_experiment_ter_readback_mismatch"
    assert len(harness.executor_calls) == 1
    assert len(harness.ter_persist_calls) == 1
    assert harness.evaluator_inputs == []
    _assert_confirmed_abort(
        error.value,
        harness,
        reason_code="learning_advice_experiment_ter_readback_mismatch",
    )


def test_runner_aborts_on_runtime_or_evaluator_authority_drift() -> None:
    manifest = _manifest()
    runtime_harness = _Harness(manifest)

    def drifting_runtime(**scope: Any) -> dict[str, Any]:
        snapshot = runtime_harness.runtime_loader(**scope)
        snapshot["code_revision"] = "different-tree"
        return snapshot

    with pytest.raises(LearningAdviceExperimentRunnerError) as runtime_error:
        _run(manifest, runtime_harness, runtime_snapshot_loader=drifting_runtime)

    assert runtime_error.value.reason_code == (
        "learning_advice_experiment_runtime_snapshot_drift"
    )
    assert runtime_harness.runtime_reads == 1
    assert runtime_harness.candidate_reads == 1
    assert runtime_harness.source_text_reads == 1
    assert runtime_harness.gateway_snapshot_reads == 1
    assert runtime_harness.executor_calls == []
    assert len(runtime_harness.observations) == 1
    _assert_confirmed_abort(
        runtime_error.value,
        runtime_harness,
        reason_code="learning_advice_experiment_runtime_snapshot_drift",
    )

    authority_harness = _Harness(manifest)

    def drifting_authority(
        concept_id: str, expected_sha256: str, **scope: Any
    ) -> dict[str, Any]:
        authority_harness.authority_reads.append(
            (concept_id, expected_sha256, dict(scope))
        )
        content = (
            _EVALUATOR_CONTENT
            if concept_id == "#V#runner_blind_evaluator"
            else _RUBRIC_CONTENT
        )
        return {
            "concept_id": concept_id,
            "content": content,
            "content_sha256": "0" * 64,
        }

    with pytest.raises(LearningAdviceExperimentRunnerError) as authority_error:
        _run(
            manifest,
            authority_harness,
            evaluator_authority_loader=drifting_authority,
        )

    assert authority_error.value.reason_code == (
        "learning_advice_experiment_evaluator_authority_drift"
    )
    assert authority_harness.executor_calls == []
    assert authority_harness.ter_persist_calls == []
    assert authority_harness.evaluator_inputs == []
    assert len(authority_harness.observations) == 1
    _assert_confirmed_abort(
        authority_error.value,
        authority_harness,
        reason_code="learning_advice_experiment_evaluator_authority_drift",
    )


def test_runner_rejects_candidate_text_in_evaluator_authority_at_preflight() -> None:
    contaminated_evaluator = _EVALUATOR_CONTENT + "\n" + _CANDIDATE_BODY
    contaminated_digest = hashlib.sha256(
        contaminated_evaluator.encode("utf-8")
    ).hexdigest()
    cases = _cases()
    for case in cases:
        case["evaluator_sha256"] = contaminated_digest
    manifest = _manifest(cases=cases)
    harness = _Harness(manifest)

    def contaminated_authority(
        concept_id: str, expected_sha256: str, **scope: Any
    ) -> dict[str, Any]:
        content = (
            contaminated_evaluator
            if concept_id == "#V#runner_blind_evaluator"
            else _RUBRIC_CONTENT
        )
        assert expected_sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {
            "concept_id": concept_id,
            "content": content,
            "content_sha256": expected_sha256,
        }

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(
            manifest,
            harness,
            evaluator_authority_loader=contaminated_authority,
        )

    assert error.value.reason_code == (
        "learning_advice_experiment_evaluator_authority_contaminated"
    )
    assert harness.executor_calls == []
    assert harness.provider_requests == []
    assert harness.ter_persist_calls == []


def test_runner_has_no_free_text_advice_or_retrieval_control_surface() -> None:
    parameters = inspect.signature(run_learning_advice_experiment).parameters

    assert "body" not in parameters
    assert "advice" not in parameters
    assert "retrieval_reason" not in parameters
    assert "model" not in parameters
    assert "turn_budget_seconds" not in parameters


def test_runner_stops_if_runtime_telemetry_reports_a_sol_family_model() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def sol_routed_executor(**kwargs: Any) -> AdaptiveTurnResult:
        result = harness.execute(**kwargs)
        call = dict(result.llm_calls[0])
        call["selected_model"] = "gpt-5.6-sol"
        call["effective_model"] = "openai/gpt-5.6-sol-preview"
        return replace(result, llm_calls=(call,))

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(manifest, harness, adaptive_turn_executor=sol_routed_executor)

    assert error.value.reason_code == "learning_advice_experiment_sol_model_observed"
    assert harness.runtime_reads == 1
    assert harness.candidate_reads == 2
    assert len(harness.executor_calls) == 1
    assert len(harness.ter_persist_calls) == 1
    assert harness.evaluator_inputs == []


def test_runner_invalidates_unfrozen_provider_observed_model_identity() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def aliased_executor(**kwargs: Any) -> AdaptiveTurnResult:
        result = harness.execute(**kwargs)
        call = dict(result.llm_calls[0])
        call["provider_observed_model"] = "gpt-5.6-luna-unfrozen-alias"
        return replace(result, llm_calls=(call,))

    result = _run(manifest, harness, adaptive_turn_executor=aliased_executor)

    assert result["status"] == "inconclusive"
    assert result["valid_trial_count"] == 0
    assert result["content_decision"]["decision"] == "inconclusive"
    assert harness.disposition_calls == []
    assert all(
        any(
            reason.startswith("provider_observed_model_mismatch:")
            for reason in observation["integrity_reason_codes"]
        )
        for observation in result["observations"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "context",
            ({"role": "system", "content": f"Leaked advice: {_CANDIDATE_BODY}"},),
        ),
        (
            "model_registry_snapshot",
            {"fixture_note": f"Leaked source: {_SOURCE_TEXT}"},
        ),
    ],
)
def test_runner_rejects_candidate_or_source_text_in_any_acting_input(
    field: str, value: Any
) -> None:
    inputs = {
        "context": _CONTEXT,
        "trusted_argument_values": _TRUSTED_ARGUMENTS,
        "workflow_launch_inputs": _WORKFLOW_INPUTS,
        "model_registry_snapshot": _MODEL_REGISTRY,
        "model_parameters": _MODEL_PARAMETERS,
    }
    inputs[field] = value
    manifest = _manifest(
        runtime_snapshot=_runtime_snapshot(
            context=inputs["context"],
            trusted_argument_values=inputs["trusted_argument_values"],
            workflow_launch_inputs=inputs["workflow_launch_inputs"],
            model_registry_snapshot=inputs["model_registry_snapshot"],
            model_parameters=inputs["model_parameters"],
        )
    )
    harness = _Harness(manifest)

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(manifest, harness, **{field: value})

    assert error.value.reason_code == "learning_advice_experiment_control_contaminated"
    assert harness.candidate_reads == 1
    assert harness.executor_calls == []


def test_runner_blocks_contaminated_outbound_request_before_provider_submission() -> (
    None
):
    manifest = _manifest()
    harness = _Harness(manifest)

    def contaminated_request_executor(**kwargs: Any) -> AdaptiveTurnResult:
        kwargs["model_request_observer"](
            {
                "schema_version": "adaptive_turn_model_request_observation.v1",
                "model_call_id": "must-not-submit",
                "stage": "adaptive_research",
                "prompt": kwargs["prompt"],
                "tools": [],
                "context": copy.deepcopy(kwargs["context"]),
                "model": kwargs["model"],
                "system_message": "Leaked learning source: " + _SOURCE_TEXT,
                "model_parameters": copy.deepcopy(kwargs["model_parameters"]),
                "continuation": None,
                "tool_results": [],
            }
        )
        raise AssertionError("observer should have blocked the provider request")

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(
            manifest,
            harness,
            adaptive_turn_executor=contaminated_request_executor,
        )

    assert error.value.reason_code == "learning_advice_experiment_request_contaminated"
    assert harness.provider_requests == []
    assert harness.ter_persist_calls == []
    assert len(harness.observations) == 1
    _assert_confirmed_abort(
        error.value,
        harness,
        reason_code="learning_advice_experiment_request_contaminated",
    )


def test_abort_observation_persistence_failure_still_terminalises_run() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def contaminated_request_executor(**kwargs: Any) -> AdaptiveTurnResult:
        kwargs["model_request_observer"](
            {
                "schema_version": "adaptive_turn_model_request_observation.v1",
                "model_call_id": "must-not-submit",
                "stage": "adaptive_research",
                "prompt": kwargs["prompt"],
                "tools": [],
                "context": copy.deepcopy(kwargs["context"]),
                "model": kwargs["model"],
                "system_message": "Leaked learning source: " + _SOURCE_TEXT,
                "model_parameters": copy.deepcopy(kwargs["model_parameters"]),
                "continuation": None,
                "tool_results": [],
            }
        )
        raise AssertionError("observer should have blocked the provider request")

    def unavailable_persister(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("storage unavailable")

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(
            manifest,
            harness,
            adaptive_turn_executor=contaminated_request_executor,
            experiment_observation_persister=unavailable_persister,
        )

    assert error.value.reason_code == "learning_advice_experiment_request_contaminated"
    _assert_confirmed_abort(
        error.value,
        harness,
        reason_code="learning_advice_experiment_request_contaminated",
    )
    recovery = error.value.recovery_evidence
    assert recovery["abort_observation"]["status"] == "persistence_unconfirmed"
    assert recovery["abort_observation"]["error_class"] == "RuntimeError"
    assert harness.observations == []


def test_abort_finaliser_failure_surfaces_typed_unconfirmed_recovery() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def contaminated_request_executor(**kwargs: Any) -> AdaptiveTurnResult:
        kwargs["model_request_observer"](
            {
                "schema_version": "adaptive_turn_model_request_observation.v1",
                "model_call_id": "must-not-submit",
                "stage": "adaptive_research",
                "prompt": kwargs["prompt"],
                "tools": [],
                "context": copy.deepcopy(kwargs["context"]),
                "model": kwargs["model"],
                "system_message": "Leaked learning source: " + _SOURCE_TEXT,
                "model_parameters": copy.deepcopy(kwargs["model_parameters"]),
                "continuation": None,
                "tool_results": [],
            }
        )
        raise AssertionError("observer should have blocked the provider request")

    def unavailable_finaliser(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("terminal store unavailable")

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(
            manifest,
            harness,
            adaptive_turn_executor=contaminated_request_executor,
            experiment_run_abort_finaliser=unavailable_finaliser,
        )

    recovery = error.value.recovery_evidence
    assert recovery["schema_version"] == (
        "learning_advice_experiment_abort_recovery.v1"
    )
    assert recovery["status"] == "terminalisation_unconfirmed"
    assert recovery["run_id"] == manifest["experiment_run_id"]
    assert recovery["abort_observation"]["status"] == "persisted"
    assert recovery["terminalisation"] == {
        "status": "unconfirmed",
        "failure_stage": "abort_finaliser",
        "error_class": "RuntimeError",
    }
    assert harness.run_status == "running"
    assert harness.abort_calls == []
    assert harness.disposition_calls == []


def test_runner_blocks_missing_arm_b_sidecar_before_provider_submission() -> None:
    manifest = _manifest()
    assert build_learning_advice_experiment_plan(manifest)["trials"][0]["arm"] == "B"
    harness = _Harness(manifest)

    def missing_sidecar_executor(**kwargs: Any) -> AdaptiveTurnResult:
        kwargs["model_request_observer"](
            {
                "schema_version": "adaptive_turn_model_request_observation.v1",
                "model_call_id": "must-not-submit-without-sidecar",
                "stage": "adaptive_research",
                "prompt": kwargs["prompt"],
                "tools": [],
                "context": copy.deepcopy(kwargs["context"]),
                "model": kwargs["model"],
                "system_message": "Stable held-out acting support.",
                "model_parameters": copy.deepcopy(kwargs["model_parameters"]),
                "continuation": None,
                "tool_results": [],
            }
        )
        raise AssertionError("observer should require the frozen Arm B sidecar")

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(
            manifest,
            harness,
            adaptive_turn_executor=missing_sidecar_executor,
        )

    assert error.value.reason_code == "learning_advice_experiment_request_contaminated"
    assert harness.provider_requests == []
    assert harness.ter_persist_calls == []


def test_unobserved_provider_submissions_invalidate_every_trial() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def unobserved_executor(**kwargs: Any) -> AdaptiveTurnResult:
        kwargs["model_request_observer"] = lambda _request: None
        return harness.execute(**kwargs)

    result = _run(
        manifest,
        harness,
        adaptive_turn_executor=unobserved_executor,
    )

    assert result["status"] == "inconclusive"
    assert result["valid_trial_count"] == 0
    assert result["content_decision"]["decision"] == "inconclusive"
    assert harness.disposition_calls == []
    assert all(
        "model_request_attestation_coverage_mismatch"
        in observation["integrity_reason_codes"]
        for observation in result["observations"]
    )


def test_runner_denies_effects_before_the_base_gateway_handler() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def effect_attempting_executor(**kwargs: Any) -> AdaptiveTurnResult:
        with pytest.raises(LearningAdviceExperimentReadOnlyViolation):
            kwargs["gateway"].invoke("dangerous_write", {"value": "changed"})
        return harness.execute(**kwargs)

    result = _run(
        manifest,
        harness,
        adaptive_turn_executor=effect_attempting_executor,
    )

    assert result["status"] == "completed"
    assert harness.gateway.base_invocations == []
    assert all(
        item["execution"]["read_only_effect_attempts_blocked"] == ["dangerous_write"]
        for item in result["observations"]
    )


def test_runner_refuses_to_return_without_exact_canonical_result_readback() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def missing_observations(run_id: str) -> dict[str, Any]:
        state = harness.load_run(run_id)
        state["observations"] = []
        return state

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(manifest, harness, experiment_run_loader=missing_observations)

    assert error.value.reason_code == "learning_advice_experiment_foreign_observation"
    assert len(harness.ter_persist_calls) == 24
    assert len(harness.observations) == 25


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_receipt", "evaluator_model_call_receipt_required"),
        ("mismatched_receipt", "evaluator_model_call_receipt_mismatch"),
        ("sol_receipt", "evaluator_sol_model_observed"),
        ("missing_dimension", "evaluator_work_product_verdict_required"),
        ("forged_pass", "evaluator_verdict_aggregation_mismatch"),
    ],
)
def test_evaluator_identity_and_typed_contract_are_required_for_a_valid_result(
    mutation: str,
    expected_reason: str,
) -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def invalid_evaluator(value: Mapping[str, Any], **config: Any) -> dict[str, Any]:
        result = harness.evaluate(value, **config)
        if mutation == "missing_receipt":
            result.pop("model_call_receipt")
        elif mutation == "mismatched_receipt":
            result["model_call_receipt"]["provider_observed_model"] = "gpt-5.6-terra"
        elif mutation == "sol_receipt":
            result["model_call_receipt"]["provider_observed_model"] = "gpt-5.6-sol"
        elif mutation == "missing_dimension":
            result.pop("work_product")
        else:
            result.update(
                {
                    "verdict": "pass",
                    "capability_choice": "fail",
                    "work_product": "fail",
                }
            )
        return result

    result = _run(manifest, harness, evaluate_blind_trial=invalid_evaluator)

    assert result["status"] == "inconclusive"
    assert result["completed_evaluation_count"] == 0
    assert result["paired_results"]["valid_pair_count"] == 0
    assert result["content_decision"]["decision"] == "inconclusive"
    assert harness.disposition_calls == []
    assert all(
        observation["evaluation"]["reason_code"] == expected_reason
        for observation in result["observations"]
    )


@pytest.mark.parametrize("model_output", [True, "omitted"])
def test_blind_evaluator_cannot_author_external_availability_drift(
    model_output: bool | str,
) -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def evaluator(value: Mapping[str, Any], **config: Any) -> dict[str, Any]:
        result = harness.evaluate(value, **config)
        if model_output is True:
            result["external_availability_changed"] = True
        else:
            result.pop("external_availability_changed")
        return result

    result = _run(manifest, harness, evaluate_blind_trial=evaluator)

    applicable = result["paired_results"]["applicable"]
    controls = result["paired_results"]["controls"]
    assert applicable["comparable_pair_count"] == 8
    assert applicable["required_comparable_pair_count"] == 8
    assert controls["comparable_pair_count"] == 4
    assert controls["required_comparable_pair_count"] == 4
    assert (
        result["paired_results"]["outcome_counts"].get("availability_changed", 0) == 0
    )
    assert result["paired_results"]["comparison"] == "favours_b"
    assert result["content_decision"]["decision"] == "arm_b_content_win"
    assert len(harness.disposition_calls) == 1
    assert all(
        observation["evaluation"]["external_availability_changed"] is False
        for observation in result["observations"]
    )


def test_echoed_candidate_text_is_never_shown_to_the_blind_evaluator() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def echoing_executor(**kwargs: Any) -> AdaptiveTurnResult:
        result = harness.execute(**kwargs)
        return replace(result, response_text=f"Internal guidance: {_CANDIDATE_BODY}")

    result = _run(manifest, harness, adaptive_turn_executor=echoing_executor)

    assert result["status"] == "inconclusive"
    assert result["content_decision"]["decision"] == "inconclusive"
    assert harness.evaluator_inputs == []
    assert len(harness.authority_reads) == 2
    assert harness.disposition_calls == []
    assert _CANDIDATE_BODY not in json.dumps(result, ensure_ascii=False)
    assert all(
        observation["evaluation"]["reason_code"] == "candidate_or_source_text_echoed"
        for observation in result["observations"]
    )


def test_disposition_is_blocked_until_canonical_run_is_terminal() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def acknowledge_without_terminalising(*, run_id: str) -> dict[str, Any]:
        return {"success": True, "run_id": run_id, "verdict": "partial"}

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(
            manifest,
            harness,
            experiment_run_finaliser=acknowledge_without_terminalising,
        )

    assert error.value.reason_code == "learning_advice_experiment_run_not_terminal"
    assert len(harness.observations) == 26
    _assert_confirmed_abort(
        error.value,
        harness,
        reason_code="learning_advice_experiment_run_not_terminal",
    )
    assert harness.disposition_calls == []


def test_foreign_observation_after_preflight_invalidates_canonical_readback() -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def load_with_late_foreign_observation(run_id: str) -> dict[str, Any]:
        state = harness.load_run(run_id)
        if state["status"] in {"completed", "failed"}:
            state["observations"].append(
                {
                    "observation_id": "foreign",
                    "observation_type": "other",
                    "evidence": {},
                }
            )
        return state

    with pytest.raises(LearningAdviceExperimentRunnerError) as error:
        _run(
            manifest,
            harness,
            experiment_run_loader=load_with_late_foreign_observation,
        )

    assert error.value.reason_code == "learning_advice_experiment_foreign_observation"
    assert harness.disposition_calls == []


@pytest.mark.parametrize(
    ("evaluator_verdict", "expected_decision", "expected_disposition", "recorded"),
    [
        ("fail", "arm_b_not_supported", "rejected", True),
        ("inconclusive", "inconclusive", "undecided", False),
    ],
)
def test_candidate_disposition_is_derived_only_after_canonical_result_readback(
    evaluator_verdict: str,
    expected_decision: str,
    expected_disposition: str,
    recorded: bool,
) -> None:
    manifest = _manifest()
    harness = _Harness(manifest)

    def fixed_evaluator(value: Mapping[str, Any], **config: Any) -> dict[str, Any]:
        result = harness.evaluate(value, **config)
        result.update(
            {
                "verdict": evaluator_verdict,
                "capability_choice": evaluator_verdict,
                "work_product": evaluator_verdict,
                "reason_codes": ["fixed_test_verdict"],
            }
        )
        return result

    result = _run(manifest, harness, evaluate_blind_trial=fixed_evaluator)

    assert result["content_decision"]["decision"] == expected_decision
    assert result["candidate_disposition"]["evaluation_disposition"] == (
        expected_disposition
    )
    assert bool(harness.disposition_calls) is recorded
    if recorded:
        call = harness.disposition_calls[0]
        assert call["candidate_id"] == _CANDIDATE_ID
        assert call["experiment_run_id"] == manifest["experiment_run_id"]
        assert call["disposition_request_id"].startswith("learning-advice-disposition:")
        assert "verdict" not in call
        assert "evidence_sha256" not in call
    else:
        assert result["candidate_disposition"]["disposition_request_id"] is None
        assert result["status"] == "inconclusive"
