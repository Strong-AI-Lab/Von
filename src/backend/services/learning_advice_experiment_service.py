"""Freeze and plan the Phase-1 learning-advice A/B comparison.

The manifest in this module is deliberately body-free.  It identifies one
canonically stored ``learning_candidate.v1`` revision and a held-out comparison,
but it cannot carry advice text or a caller-authored retrieval rationale.  A
candidate is loaded again, under the trusted acting scope, before every trial;
only that canonical read may supply the bytes used by Arm B.

This is experiment support, not an activation or selection service.  It knows
how to produce the advice-off Arm A and the presentation-equivalent simple
sidecar Arm B.  It does not construct represented Arm C or make a candidate
available to ordinary turns.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .learning_advice_projection_service import (
    build_learning_advice_experiment_projection,
)
from .learning_candidate_vontology_service import (
    InvalidLearningCandidateData,
    LearningCandidateAccessError,
    LearningCandidateNotFoundError,
    get_learning_candidate,
)

LEARNING_ADVICE_EXPERIMENT_MANIFEST_SCHEMA_VERSION = (
    "learning_advice_experiment_manifest.v1"
)
LEARNING_ADVICE_EXPERIMENT_PLAN_SCHEMA_VERSION = "learning_advice_experiment_plan.v1"
LEARNING_ADVICE_EXPERIMENT_RUN_BINDING_SCHEMA_VERSION = (
    "learning_advice_experiment_run_binding.v1"
)

LEARNING_ADVICE_EXPERIMENT_ARMS = ("A", "B")
LEARNING_ADVICE_EXPERIMENT_CASE_COUNT = 6
LEARNING_ADVICE_EXPERIMENT_APPLICABLE_CASE_COUNT = 4
LEARNING_ADVICE_EXPERIMENT_CONTROL_CASE_COUNT = 2
LEARNING_ADVICE_EXPERIMENT_DEFAULT_REPEATS = 2

_CANDIDATE_SCHEMA_VERSION = "learning_candidate.v1"
_CANDIDATE_LIFECYCLE_STATE = "non_active"
_CANDIDATE_AUTHOR_ID = "#V#von_system"
_ELIGIBLE_CANDIDATE_DISPOSITIONS = {"undecided", "retained"}
_CASE_KINDS = {"applicable", "control"}
_FAILURE_RECOVERY_POLICIES = {
    "report_specific_failure",
    "try_other_plausible_capability",
}
_CLARIFICATION_VERDICTS = {"fail", "partial"}
_ALTERNATIVE_POLICIES = {"allowed_for_recovery", "fail"}
_FORBIDDEN_MANIFEST_KEYS = {
    "advice",
    "advice_body",
    "body",
    "candidate_body",
    "retrieval_reason",
}
_RUN_BINDING_FORBIDDEN_KEYS = {
    *_FORBIDDEN_MANIFEST_KEYS,
    "content",
    "evaluator_content",
    "prompt",
    "response",
    "response_text",
    "rubric_content",
}
_IMPLICIT_MODEL_IDS = {
    "auto",
    "configured",
    "current",
    "default",
    "latest",
    "provider-default",
    "recommended",
}
_EXPLICIT_NON_SOL_MARKER = re.compile(r"(?:^|[^a-z0-9])non[-_.:/ ]+sol(?=$|[^a-z0-9])")
_SOL_MODEL_TOKEN = re.compile(r"(?:^|[^a-z0-9])sol(?:$|[^a-z0-9])")
_SHA256_HEX_CHARS = frozenset("0123456789abcdef")


class LearningAdviceExperimentError(ValueError):
    """Raised when a frozen experiment definition is invalid."""

    def __init__(self, reason_code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.reason_code = reason_code
        self.safe_message = safe_message


class LearningAdviceExperimentAccessError(LearningAdviceExperimentError):
    """Raised when a manifest is used outside its frozen trusted scope."""


class LearningAdviceExperimentDriftError(LearningAdviceExperimentError):
    """Raised when canonical candidate state differs from the frozen identity."""


CandidateLoader = Callable[..., Mapping[str, Any]]


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_not_json",
            "The learning-advice experiment must contain only JSON values",
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _required_text(value: Any, *, field: str, max_chars: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} is required",
        )
    cleaned = value.strip()
    if len(cleaned) > max_chars:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} is too long",
        )
    return cleaned


def _concept_id(value: Any, *, field: str) -> str:
    cleaned = _required_text(value, field=field, max_chars=300)
    if not cleaned.startswith("#V#") or len(cleaned) <= 3:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} must be a #V# concept ID",
        )
    return cleaned


def _positive_int(value: Any, *, field: str, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} must be an integer greater than or equal to {minimum}",
        )
    return value


def _sha256_digest(value: Any, *, field: str) -> str:
    digest = _required_text(value, field=field, max_chars=64).lower()
    if len(digest) != 64 or any(char not in _SHA256_HEX_CHARS for char in digest):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} must be a SHA-256 digest",
        )
    return digest


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    expected: set[str],
    field: str,
) -> None:
    supplied = {str(key) for key in value}
    if supplied == expected:
        return
    missing = sorted(expected - supplied)
    unexpected = sorted(supplied - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing {', '.join(missing)}")
    if unexpected:
        details.append(f"unexpected {', '.join(unexpected)}")
    raise LearningAdviceExperimentError(
        "learning_advice_experiment_invalid",
        f"{field} has invalid fields ({'; '.join(details)})",
    )


def _reject_forbidden_manifest_keys(value: Any, *, path: str = "manifest") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if key_text.casefold() in _FORBIDDEN_MANIFEST_KEYS:
                raise LearningAdviceExperimentError(
                    "learning_advice_experiment_semantic_text_forbidden",
                    f"{path}.{key_text} is not permitted in a frozen manifest",
                )
            _reject_forbidden_manifest_keys(item, path=f"{path}.{key_text}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_forbidden_manifest_keys(item, path=f"{path}[{index}]")


def is_sol_family_model_id(model_id: Any) -> bool:
    """Return whether an explicit model identifier selects the Sol family."""

    if not isinstance(model_id, str):
        return False
    normalised = model_id.strip().casefold()
    without_negative_markers = _EXPLICIT_NON_SOL_MARKER.sub(
        " excluded_model_family ", normalised
    )
    return bool(
        without_negative_markers and _SOL_MODEL_TOKEN.search(without_negative_markers)
    )


def _explicit_non_sol_model_id(value: Any) -> str:
    model_id = _required_text(value, field="model.model_id", max_chars=500)
    if model_id.casefold() in _IMPLICIT_MODEL_IDS:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_explicit_model_required",
            "The experiment requires an explicit model identifier",
        )
    if is_sol_family_model_id(model_id):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_sol_model_forbidden",
            "Sol-family models are not permitted for this experiment",
        )
    return model_id


def _normalise_candidate_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "candidate_ref must be an object",
        )
    _require_exact_keys(
        value,
        expected={
            "candidate_id",
            "revision",
            "evaluation_disposition",
            "body_sha256",
            "revision_identity_sha256",
            "source_locator_sha256",
        },
        field="candidate_ref",
    )
    evaluation_disposition = _required_text(
        value.get("evaluation_disposition"),
        field="candidate_ref.evaluation_disposition",
        max_chars=40,
    ).lower()
    if evaluation_disposition not in _ELIGIBLE_CANDIDATE_DISPOSITIONS:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_candidate_ineligible",
            "The candidate disposition is not eligible for an advice-use experiment",
        )
    return {
        "candidate_id": _concept_id(
            value.get("candidate_id"), field="candidate_ref.candidate_id"
        ),
        "revision": _positive_int(
            value.get("revision"), field="candidate_ref.revision"
        ),
        "evaluation_disposition": evaluation_disposition,
        "body_sha256": _sha256_digest(
            value.get("body_sha256"), field="candidate_ref.body_sha256"
        ),
        "revision_identity_sha256": _sha256_digest(
            value.get("revision_identity_sha256"),
            field="candidate_ref.revision_identity_sha256",
        ),
        "source_locator_sha256": _sha256_digest(
            value.get("source_locator_sha256"),
            field="candidate_ref.source_locator_sha256",
        ),
    }


def frozen_candidate_ref(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return the body-free identity needed to freeze a canonical candidate."""

    if not isinstance(candidate, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "candidate must be an object",
        )
    return _normalise_candidate_ref(
        {
            "candidate_id": candidate.get("candidate_id"),
            "revision": candidate.get("revision"),
            "evaluation_disposition": candidate.get("evaluation_disposition"),
            "body_sha256": candidate.get("body_sha256"),
            "revision_identity_sha256": candidate.get("revision_identity_sha256"),
            "source_locator_sha256": candidate.get("source_locator_sha256"),
        }
    )


def _normalise_scope(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "trusted_scope must be an object",
        )
    _require_exact_keys(
        value,
        expected={"actor_user_id", "organisation_concept_id", "namespace"},
        field="trusted_scope",
    )
    return {
        "actor_user_id": _concept_id(
            value.get("actor_user_id"), field="trusted_scope.actor_user_id"
        ),
        "organisation_concept_id": _concept_id(
            value.get("organisation_concept_id"),
            field="trusted_scope.organisation_concept_id",
        ),
        "namespace": _required_text(
            value.get("namespace"), field="trusted_scope.namespace", max_chars=600
        ),
    }


def _normalise_model(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "model must be an object",
        )
    _require_exact_keys(
        value,
        expected={"provider", "model_id", "parameters"},
        field="model",
    )
    parameters = value.get("parameters")
    if not isinstance(parameters, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "model.parameters must be an object",
        )
    parameters = copy.deepcopy(dict(parameters))
    _canonical_bytes(parameters)
    return {
        "provider": _required_text(
            value.get("provider"), field="model.provider", max_chars=300
        ),
        "model_id": _explicit_non_sol_model_id(value.get("model_id")),
        "parameters": parameters,
    }


def _positive_number(value: Any, *, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} must be a finite positive number",
        )
    return float(value)


def _non_negative_number(value: Any, *, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} must be a finite non-negative number",
        )
    return float(value)


def _normalise_runtime_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "runtime_snapshot must be an object",
        )
    _require_exact_keys(
        value,
        expected={
            "code_revision",
            "capability_catalogue_sha256",
            "workflow_catalogue_sha256",
            "relevant_ontology_sha256",
            "acting_support_sha256",
            "context_budget_tokens",
            "turn_budget_seconds",
            "final_synthesis_reserve_seconds",
            "final_answer_reserve_seconds",
        },
        field="runtime_snapshot",
    )
    turn_budget = _positive_number(
        value.get("turn_budget_seconds"),
        field="runtime_snapshot.turn_budget_seconds",
    )
    final_reserve = _positive_number(
        value.get("final_synthesis_reserve_seconds"),
        field="runtime_snapshot.final_synthesis_reserve_seconds",
    )
    if final_reserve >= turn_budget:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "runtime_snapshot.final_synthesis_reserve_seconds must be smaller "
            "than the turn budget",
        )
    final_answer_reserve = _non_negative_number(
        value.get("final_answer_reserve_seconds"),
        field="runtime_snapshot.final_answer_reserve_seconds",
    )
    if final_answer_reserve > final_reserve:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "runtime_snapshot.final_answer_reserve_seconds must not exceed the "
            "final synthesis reserve",
        )
    return {
        "code_revision": _required_text(
            value.get("code_revision"),
            field="runtime_snapshot.code_revision",
            max_chars=200,
        ),
        **{
            field: _sha256_digest(
                value.get(field),
                field=f"runtime_snapshot.{field}",
            )
            for field in (
                "capability_catalogue_sha256",
                "workflow_catalogue_sha256",
                "relevant_ontology_sha256",
                "acting_support_sha256",
            )
        },
        "context_budget_tokens": _positive_int(
            value.get("context_budget_tokens"),
            field="runtime_snapshot.context_budget_tokens",
        ),
        "turn_budget_seconds": turn_budget,
        "final_synthesis_reserve_seconds": final_reserve,
        "final_answer_reserve_seconds": final_answer_reserve,
    }


def _normalise_capability_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} must be a list",
        )
    result: list[str] = []
    for index, item in enumerate(value):
        capability = _required_text(item, field=f"{field}[{index}]", max_chars=100)
        if capability not in result:
            result.append(capability)
    if not result:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} requires at least one capability",
        )
    return result


def _normalise_evaluation_contract(value: Any, *, field: str) -> dict[str, Any]:
    """Validate evaluator-only expectations that never enter the acting turn."""

    if not isinstance(value, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field} must be an object",
        )
    _require_exact_keys(
        value,
        expected={
            "allowed_first_capabilities",
            "required_success_capability",
            "after_capability_failure",
            "focused_clarification_verdict",
            "unsolicited_alternative_policy",
            "read_only",
        },
        field=field,
    )
    allowed_first = _normalise_capability_list(
        value.get("allowed_first_capabilities"),
        field=f"{field}.allowed_first_capabilities",
    )
    required_success_raw = value.get("required_success_capability")
    required_success = (
        None
        if required_success_raw is None
        else _required_text(
            required_success_raw,
            field=f"{field}.required_success_capability",
            max_chars=100,
        )
    )
    recovery = _required_text(
        value.get("after_capability_failure"),
        field=f"{field}.after_capability_failure",
        max_chars=100,
    )
    if recovery not in _FAILURE_RECOVERY_POLICIES:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field}.after_capability_failure is unsupported",
        )
    clarification = _required_text(
        value.get("focused_clarification_verdict"),
        field=f"{field}.focused_clarification_verdict",
        max_chars=20,
    )
    if clarification not in _CLARIFICATION_VERDICTS:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field}.focused_clarification_verdict is unsupported",
        )
    alternative_policy = _required_text(
        value.get("unsolicited_alternative_policy"),
        field=f"{field}.unsolicited_alternative_policy",
        max_chars=100,
    )
    if alternative_policy not in _ALTERNATIVE_POLICIES:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field}.unsolicited_alternative_policy is unsupported",
        )
    if value.get("read_only") is not True:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"{field}.read_only must be true",
        )
    return {
        "allowed_first_capabilities": allowed_first,
        "required_success_capability": required_success,
        "after_capability_failure": recovery,
        "focused_clarification_verdict": clarification,
        "unsolicited_alternative_policy": alternative_policy,
        "read_only": True,
    }


def _normalise_case(value: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            f"cases[{index}] must be an object",
        )
    _require_exact_keys(
        value,
        expected={
            "case_id",
            "kind",
            "prompt",
            "evaluator_concept_id",
            "evaluator_sha256",
            "rubric_concept_id",
            "rubric_sha256",
            "evaluation_contract",
        },
        field=f"cases[{index}]",
    )
    kind = _required_text(value.get("kind"), field=f"cases[{index}].kind", max_chars=20)
    if kind not in _CASE_KINDS:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid_case_distribution",
            f"cases[{index}].kind must be applicable or control",
        )
    return {
        "case_id": _required_text(
            value.get("case_id"), field=f"cases[{index}].case_id", max_chars=300
        ),
        "kind": kind,
        "prompt": _required_text(
            value.get("prompt"), field=f"cases[{index}].prompt", max_chars=12_000
        ),
        "evaluator_concept_id": _concept_id(
            value.get("evaluator_concept_id"),
            field=f"cases[{index}].evaluator_concept_id",
        ),
        "evaluator_sha256": _sha256_digest(
            value.get("evaluator_sha256"),
            field=f"cases[{index}].evaluator_sha256",
        ),
        "rubric_concept_id": _concept_id(
            value.get("rubric_concept_id"),
            field=f"cases[{index}].rubric_concept_id",
        ),
        "rubric_sha256": _sha256_digest(
            value.get("rubric_sha256"),
            field=f"cases[{index}].rubric_sha256",
        ),
        "evaluation_contract": _normalise_evaluation_contract(
            value.get("evaluation_contract"),
            field=f"cases[{index}].evaluation_contract",
        ),
    }


def _normalise_cases(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "cases must be a list",
        )
    cases = [_normalise_case(item, index=index) for index, item in enumerate(value)]
    if len(cases) != LEARNING_ADVICE_EXPERIMENT_CASE_COUNT:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid_case_distribution",
            "The experiment requires exactly six cases",
        )
    case_ids = [item["case_id"] for item in cases]
    if len(set(case_ids)) != len(case_ids):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid_case_distribution",
            "Experiment case IDs must be unique",
        )
    prompts = [item["prompt"] for item in cases]
    if len(set(prompts)) != len(prompts):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid_case_distribution",
            "Experiment case prompts must be unique",
        )
    applicable_count = sum(item["kind"] == "applicable" for item in cases)
    control_count = sum(item["kind"] == "control" for item in cases)
    if (
        applicable_count != LEARNING_ADVICE_EXPERIMENT_APPLICABLE_CASE_COUNT
        or control_count != LEARNING_ADVICE_EXPERIMENT_CONTROL_CASE_COUNT
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid_case_distribution",
            "The experiment requires four applicable cases and two controls",
        )
    return cases


def _normalise_decision_rule(
    value: Any,
    *,
    applicable_trial_count: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "decision_rule must be an object",
        )
    _require_exact_keys(
        value,
        expected={
            "minimum_applicable_b_passes",
            "minimum_applicable_pass_delta",
            "require_more_paired_improvements_than_regressions",
            "forbid_b_only_material_failure",
            "require_control_non_inferiority",
        },
        field="decision_rule",
    )
    minimum_passes = _positive_int(
        value.get("minimum_applicable_b_passes"),
        field="decision_rule.minimum_applicable_b_passes",
    )
    if minimum_passes > applicable_trial_count:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "decision_rule.minimum_applicable_b_passes exceeds the applicable trials",
        )
    minimum_delta = _positive_int(
        value.get("minimum_applicable_pass_delta"),
        field="decision_rule.minimum_applicable_pass_delta",
    )
    if minimum_delta > applicable_trial_count:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "decision_rule.minimum_applicable_pass_delta exceeds the applicable trials",
        )
    booleans: dict[str, bool] = {}
    for field in (
        "require_more_paired_improvements_than_regressions",
        "forbid_b_only_material_failure",
        "require_control_non_inferiority",
    ):
        if not isinstance(value.get(field), bool):
            raise LearningAdviceExperimentError(
                "learning_advice_experiment_invalid",
                f"decision_rule.{field} must be a boolean",
            )
        booleans[field] = bool(value[field])
    return {
        "minimum_applicable_b_passes": minimum_passes,
        "minimum_applicable_pass_delta": minimum_delta,
        **booleans,
    }


def _manifest_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(manifest))
    payload.pop("manifest_sha256", None)
    return payload


def build_learning_advice_experiment_manifest(
    *,
    experiment_spec_id: str,
    experiment_run_id: str,
    candidate_ref: Mapping[str, Any],
    actor_user_id: str,
    organisation_concept_id: str,
    namespace: str,
    provider: str,
    model_id: str,
    model_parameters: Mapping[str, Any],
    runtime_snapshot: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    decision_rule: Mapping[str, Any],
    randomisation_seed: int,
    repeats: int = LEARNING_ADVICE_EXPERIMENT_DEFAULT_REPEATS,
) -> dict[str, Any]:
    """Build a canonical, body-free manifest for one frozen A/B experiment."""

    payload: dict[str, Any] = {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_MANIFEST_SCHEMA_VERSION,
        "experiment_spec_id": experiment_spec_id,
        "experiment_run_id": experiment_run_id,
        "candidate_ref": copy.deepcopy(candidate_ref),
        "trusted_scope": {
            "actor_user_id": actor_user_id,
            "organisation_concept_id": organisation_concept_id,
            "namespace": namespace,
        },
        "model": {
            "provider": provider,
            "model_id": model_id,
            "parameters": copy.deepcopy(model_parameters),
        },
        "runtime_snapshot": copy.deepcopy(runtime_snapshot),
        "arms": list(LEARNING_ADVICE_EXPERIMENT_ARMS),
        "repeats": repeats,
        "randomisation_seed": randomisation_seed,
        "cases": copy.deepcopy(cases),
        "decision_rule": copy.deepcopy(decision_rule),
    }
    _reject_forbidden_manifest_keys(payload)
    normalised = _normalise_manifest_payload(payload)
    normalised["manifest_sha256"] = _sha256(normalised)
    return normalised


def _normalise_manifest_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    _require_exact_keys(
        value,
        expected={
            "schema_version",
            "experiment_spec_id",
            "experiment_run_id",
            "candidate_ref",
            "trusted_scope",
            "model",
            "runtime_snapshot",
            "arms",
            "repeats",
            "randomisation_seed",
            "cases",
            "decision_rule",
        },
        field="manifest",
    )
    if (
        value.get("schema_version")
        != LEARNING_ADVICE_EXPERIMENT_MANIFEST_SCHEMA_VERSION
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_schema_unsupported",
            "The learning-advice experiment manifest schema is unsupported",
        )
    arms = value.get("arms")
    normalised_arms = list(arms) if isinstance(arms, list) else None
    if normalised_arms != list(LEARNING_ADVICE_EXPERIMENT_ARMS):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid_arms",
            "The Phase-1 experiment arms must be exactly A and B",
        )
    repeats = _positive_int(value.get("repeats"), field="repeats", minimum=2)
    seed = value.get("randomisation_seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "randomisation_seed must be an integer",
        )
    cases = _normalise_cases(value.get("cases"))
    return {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_MANIFEST_SCHEMA_VERSION,
        "experiment_spec_id": _concept_id(
            value.get("experiment_spec_id"), field="experiment_spec_id"
        ),
        "experiment_run_id": _concept_id(
            value.get("experiment_run_id"), field="experiment_run_id"
        ),
        "candidate_ref": _normalise_candidate_ref(value.get("candidate_ref")),
        "trusted_scope": _normalise_scope(value.get("trusted_scope")),
        "model": _normalise_model(value.get("model")),
        "runtime_snapshot": _normalise_runtime_snapshot(value.get("runtime_snapshot")),
        "arms": list(LEARNING_ADVICE_EXPERIMENT_ARMS),
        "repeats": repeats,
        "randomisation_seed": seed,
        "cases": cases,
        "decision_rule": _normalise_decision_rule(
            value.get("decision_rule"),
            applicable_trial_count=(
                sum(item["kind"] == "applicable" for item in cases) * repeats
            ),
        ),
    }


def validate_learning_advice_experiment_manifest(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and return the canonical frozen manifest."""

    if not isinstance(manifest, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_invalid",
            "manifest must be an object",
        )
    _reject_forbidden_manifest_keys(manifest)
    _require_exact_keys(
        manifest,
        expected={
            "schema_version",
            "experiment_spec_id",
            "experiment_run_id",
            "candidate_ref",
            "trusted_scope",
            "model",
            "runtime_snapshot",
            "arms",
            "repeats",
            "randomisation_seed",
            "cases",
            "decision_rule",
            "manifest_sha256",
        },
        field="manifest",
    )
    supplied_digest = _sha256_digest(
        manifest.get("manifest_sha256"), field="manifest_sha256"
    )
    normalised = _normalise_manifest_payload(_manifest_payload(manifest))
    expected_digest = _sha256(normalised)
    if supplied_digest != expected_digest:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_manifest_digest_mismatch",
            "The experiment manifest does not match its canonical digest",
        )
    normalised["manifest_sha256"] = expected_digest
    return normalised


def _stable_trial_id(
    *, manifest_sha256: str, case_id: str, repeat: int, arm: str
) -> str:
    digest = _sha256(
        {
            "manifest_sha256": manifest_sha256,
            "case_id": case_id,
            "repeat": repeat,
            "arm": arm,
        }
    )
    return f"trial-{digest[:24]}"


def _fresh_identity_placeholders(trial_id: str) -> dict[str, str]:
    return {
        "request_id": f"$fresh:{trial_id}:request",
        "session_id": f"$fresh:{trial_id}:session",
        "turn_id": f"$fresh:{trial_id}:turn",
    }


def build_learning_advice_experiment_plan(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a deterministic balanced paired A/B order from the frozen seed."""

    frozen = validate_learning_advice_experiment_manifest(manifest)
    repeats = int(frozen["repeats"])
    pair_inputs = [
        (case, repeat) for case in frozen["cases"] for repeat in range(1, repeats + 1)
    ]
    rng = random.Random(int(frozen["randomisation_seed"]))
    rng.shuffle(pair_inputs)
    orientation_count = len(pair_inputs) // 2
    orientations = [("A", "B")] * orientation_count + [("B", "A")] * orientation_count
    rng.shuffle(orientations)

    trials: list[dict[str, Any]] = []
    for pair_index, ((case, repeat), arms) in enumerate(
        zip(pair_inputs, orientations, strict=True), start=1
    ):
        pair_id = f"pair-{pair_index:03d}-{case['case_id']}-r{repeat}"
        for pair_position, arm in enumerate(arms, start=1):
            trial_id = _stable_trial_id(
                manifest_sha256=frozen["manifest_sha256"],
                case_id=case["case_id"],
                repeat=repeat,
                arm=arm,
            )
            trials.append(
                {
                    "trial_id": trial_id,
                    "pair_id": pair_id,
                    "order_index": len(trials) + 1,
                    "pair_position": pair_position,
                    "case_id": case["case_id"],
                    "case_kind": case["kind"],
                    "repeat": repeat,
                    "arm": arm,
                    "prompt": case["prompt"],
                    "evaluator_concept_id": case["evaluator_concept_id"],
                    "rubric_concept_id": case["rubric_concept_id"],
                    "rubric_sha256": case["rubric_sha256"],
                    "evaluator_sha256": case["evaluator_sha256"],
                    "evaluation_contract": copy.deepcopy(case["evaluation_contract"]),
                    "fresh_identity_placeholders": _fresh_identity_placeholders(
                        trial_id
                    ),
                }
            )
    payload: dict[str, Any] = {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_PLAN_SCHEMA_VERSION,
        "manifest_sha256": frozen["manifest_sha256"],
        "experiment_spec_id": frozen["experiment_spec_id"],
        "experiment_run_id": frozen["experiment_run_id"],
        "randomisation_seed": frozen["randomisation_seed"],
        "runtime_snapshot": copy.deepcopy(frozen["runtime_snapshot"]),
        "decision_rule": copy.deepcopy(frozen["decision_rule"]),
        "pair_count": len(pair_inputs),
        "trial_count": len(trials),
        "trials": trials,
    }
    payload["plan_sha256"] = _sha256(payload)
    return payload


def _prompt_utf8_sha256(value: Any, *, field: str) -> str:
    prompt = _required_text(value, field=field, max_chars=12_000)
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _assert_run_binding_has_no_semantic_text(value: Mapping[str, Any]) -> None:
    def visit(item: Any, path: str) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                key_text = str(key)
                if key_text.casefold() in _RUN_BINDING_FORBIDDEN_KEYS:
                    raise LearningAdviceExperimentError(
                        "learning_advice_experiment_run_binding_contains_text",
                        f"The body-free run binding contains {path}.{key_text}",
                    )
                visit(nested, f"{path}.{key_text}")
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")

    visit(value, "run_binding")
    _canonical_bytes(value)


def _body_free_manifest_preimage(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    preimage = _manifest_payload(manifest)
    preimage["cases"] = [
        {
            **{
                key: copy.deepcopy(value)
                for key, value in case.items()
                if key != "prompt"
            },
            "prompt_utf8_sha256": _prompt_utf8_sha256(
                case.get("prompt"), field=f"cases[{index}].prompt"
            ),
        }
        for index, case in enumerate(manifest["cases"])
    ]
    return preimage


def _normalise_body_free_manifest_preimage(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_invalid",
            "The body-free manifest preimage must be an object",
        )
    _require_exact_keys(
        value,
        expected={
            "schema_version",
            "experiment_spec_id",
            "experiment_run_id",
            "candidate_ref",
            "trusted_scope",
            "model",
            "runtime_snapshot",
            "arms",
            "repeats",
            "randomisation_seed",
            "cases",
            "decision_rule",
        },
        field="body_free_manifest_preimage",
    )
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, Sequence) or isinstance(
        raw_cases, (str, bytes, bytearray)
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_invalid",
            "The body-free manifest cases must be a list",
        )
    prompt_digests: list[str] = []
    synthetic_cases: list[dict[str, Any]] = []
    expected_case_keys = {
        "case_id",
        "kind",
        "prompt_utf8_sha256",
        "evaluator_concept_id",
        "evaluator_sha256",
        "rubric_concept_id",
        "rubric_sha256",
        "evaluation_contract",
    }
    for index, case in enumerate(raw_cases):
        if not isinstance(case, Mapping):
            raise LearningAdviceExperimentError(
                "learning_advice_experiment_run_binding_invalid",
                "A body-free manifest case is invalid",
            )
        _require_exact_keys(
            case,
            expected=expected_case_keys,
            field=f"body_free_manifest_preimage.cases[{index}]",
        )
        prompt_digest = _sha256_digest(
            case.get("prompt_utf8_sha256"),
            field=(f"body_free_manifest_preimage.cases[{index}].prompt_utf8_sha256"),
        )
        prompt_digests.append(prompt_digest)
        synthetic_cases.append(
            {
                **{
                    key: copy.deepcopy(nested)
                    for key, nested in case.items()
                    if key != "prompt_utf8_sha256"
                },
                "prompt": f"private-prompt-sha256:{prompt_digest}",
            }
        )
    if len(set(prompt_digests)) != len(prompt_digests):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_invalid",
            "Private held-out prompt commitments must be unique",
        )
    synthetic_manifest = copy.deepcopy(dict(value))
    synthetic_manifest["cases"] = synthetic_cases
    normalised = _normalise_manifest_payload(synthetic_manifest)
    normalised["cases"] = [
        {
            **{
                key: copy.deepcopy(nested)
                for key, nested in case.items()
                if key != "prompt"
            },
            "prompt_utf8_sha256": prompt_digests[index],
        }
        for index, case in enumerate(normalised["cases"])
    ]
    if _canonical_bytes(normalised) != _canonical_bytes(value):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_invalid",
            "The body-free manifest preimage is not canonical",
        )
    return normalised


def _build_body_free_plan_preimage(
    manifest_preimage: Mapping[str, Any],
    *,
    manifest_sha256: str,
) -> dict[str, Any]:
    repeats = int(manifest_preimage["repeats"])
    pair_inputs = [
        (case, repeat)
        for case in manifest_preimage["cases"]
        for repeat in range(1, repeats + 1)
    ]
    rng = random.Random(int(manifest_preimage["randomisation_seed"]))
    rng.shuffle(pair_inputs)
    orientation_count = len(pair_inputs) // 2
    orientations = [("A", "B")] * orientation_count + [("B", "A")] * orientation_count
    rng.shuffle(orientations)
    trials: list[dict[str, Any]] = []
    for pair_index, ((case, repeat), arms) in enumerate(
        zip(pair_inputs, orientations, strict=True), start=1
    ):
        pair_id = f"pair-{pair_index:03d}-{case['case_id']}-r{repeat}"
        for pair_position, arm in enumerate(arms, start=1):
            trial_id = _stable_trial_id(
                manifest_sha256=manifest_sha256,
                case_id=case["case_id"],
                repeat=repeat,
                arm=arm,
            )
            trials.append(
                {
                    "trial_id": trial_id,
                    "pair_id": pair_id,
                    "order_index": len(trials) + 1,
                    "pair_position": pair_position,
                    "case_id": case["case_id"],
                    "case_kind": case["kind"],
                    "repeat": repeat,
                    "arm": arm,
                    "prompt_utf8_sha256": case["prompt_utf8_sha256"],
                    "evaluator_concept_id": case["evaluator_concept_id"],
                    "rubric_concept_id": case["rubric_concept_id"],
                    "rubric_sha256": case["rubric_sha256"],
                    "evaluator_sha256": case["evaluator_sha256"],
                    "evaluation_contract": copy.deepcopy(case["evaluation_contract"]),
                    "fresh_identity_placeholders": _fresh_identity_placeholders(
                        trial_id
                    ),
                }
            )
    return {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_PLAN_SCHEMA_VERSION,
        "manifest_sha256": manifest_sha256,
        "experiment_spec_id": manifest_preimage["experiment_spec_id"],
        "experiment_run_id": manifest_preimage["experiment_run_id"],
        "randomisation_seed": manifest_preimage["randomisation_seed"],
        "runtime_snapshot": copy.deepcopy(manifest_preimage["runtime_snapshot"]),
        "decision_rule": copy.deepcopy(manifest_preimage["decision_rule"]),
        "pair_count": len(pair_inputs),
        "trial_count": len(trials),
        "trials": trials,
    }


def build_learning_advice_experiment_run_binding(
    manifest: Mapping[str, Any],
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the inspectable body-free metadata bound to a fresh run."""

    frozen = validate_learning_advice_experiment_manifest(manifest)
    canonical_plan = build_learning_advice_experiment_plan(frozen)
    if plan is not None and (
        not isinstance(plan, Mapping) or dict(plan) != canonical_plan
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_plan_mismatch",
            "The supplied run-binding plan is not the canonical seeded plan",
        )
    manifest_preimage = _body_free_manifest_preimage(frozen)
    plan_preimage = copy.deepcopy(canonical_plan)
    plan_preimage.pop("plan_sha256", None)
    plan_preimage["trials"] = [
        {
            **{
                key: copy.deepcopy(value)
                for key, value in trial.items()
                if key != "prompt"
            },
            "prompt_utf8_sha256": _prompt_utf8_sha256(
                trial.get("prompt"), field=f"plan.trials[{index}].prompt"
            ),
        }
        for index, trial in enumerate(canonical_plan["trials"])
    ]
    payload = {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_RUN_BINDING_SCHEMA_VERSION,
        "experiment_spec_id": frozen["experiment_spec_id"],
        "experiment_run_id": frozen["experiment_run_id"],
        "manifest_sha256": frozen["manifest_sha256"],
        "plan_sha256": canonical_plan["plan_sha256"],
        "body_free_manifest_preimage": manifest_preimage,
        "body_free_manifest_preimage_sha256": _sha256(manifest_preimage),
        "body_free_plan_preimage": plan_preimage,
        "body_free_plan_preimage_sha256": _sha256(plan_preimage),
        "candidate_ref": copy.deepcopy(frozen["candidate_ref"]),
        "trusted_scope_sha256": _sha256(frozen["trusted_scope"]),
        "runtime_snapshot_sha256": _sha256(frozen["runtime_snapshot"]),
        "required_applicable_pair_count": (
            sum(case["kind"] == "applicable" for case in frozen["cases"])
            * int(frozen["repeats"])
        ),
        "required_control_pair_count": (
            sum(case["kind"] == "control" for case in frozen["cases"])
            * int(frozen["repeats"])
        ),
        "decision_rule": copy.deepcopy(frozen["decision_rule"]),
    }
    _assert_run_binding_has_no_semantic_text(payload)
    payload["binding_sha256"] = _sha256(payload)
    return validate_learning_advice_experiment_run_binding(payload)


def validate_learning_advice_experiment_run_binding(
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and rebuild all inspectable evidence in a stored run binding."""

    if not isinstance(binding, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_invalid",
            "The learning-advice run binding must be an object",
        )
    expected_keys = {
        "schema_version",
        "experiment_spec_id",
        "experiment_run_id",
        "manifest_sha256",
        "plan_sha256",
        "body_free_manifest_preimage",
        "body_free_manifest_preimage_sha256",
        "body_free_plan_preimage",
        "body_free_plan_preimage_sha256",
        "candidate_ref",
        "trusted_scope_sha256",
        "runtime_snapshot_sha256",
        "required_applicable_pair_count",
        "required_control_pair_count",
        "decision_rule",
        "binding_sha256",
    }
    _require_exact_keys(binding, expected=expected_keys, field="run_binding")
    if binding.get("schema_version") != (
        LEARNING_ADVICE_EXPERIMENT_RUN_BINDING_SCHEMA_VERSION
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_invalid",
            "The learning-advice run binding schema is unsupported",
        )
    _assert_run_binding_has_no_semantic_text(binding)
    supplied_binding_sha256 = _sha256_digest(
        binding.get("binding_sha256"), field="run_binding.binding_sha256"
    )
    binding_payload = copy.deepcopy(dict(binding))
    binding_payload.pop("binding_sha256", None)
    if supplied_binding_sha256 != _sha256(binding_payload):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_digest_mismatch",
            "The stored run binding does not match its canonical digest",
        )
    manifest_sha256 = _sha256_digest(
        binding.get("manifest_sha256"), field="run_binding.manifest_sha256"
    )
    plan_sha256 = _sha256_digest(
        binding.get("plan_sha256"), field="run_binding.plan_sha256"
    )
    manifest_preimage = _normalise_body_free_manifest_preimage(
        binding.get("body_free_manifest_preimage")
    )
    if _sha256(manifest_preimage) != _sha256_digest(
        binding.get("body_free_manifest_preimage_sha256"),
        field="run_binding.body_free_manifest_preimage_sha256",
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_digest_mismatch",
            "The body-free manifest preimage digest does not match",
        )
    canonical_plan_preimage = _build_body_free_plan_preimage(
        manifest_preimage,
        manifest_sha256=manifest_sha256,
    )
    supplied_plan_preimage = binding.get("body_free_plan_preimage")
    if (
        not isinstance(supplied_plan_preimage, Mapping)
        or _canonical_bytes(supplied_plan_preimage)
        != _canonical_bytes(canonical_plan_preimage)
        or _sha256(canonical_plan_preimage)
        != _sha256_digest(
            binding.get("body_free_plan_preimage_sha256"),
            field="run_binding.body_free_plan_preimage_sha256",
        )
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_plan_mismatch",
            "The body-free plan does not rebuild from the frozen manifest evidence",
        )
    required_applicable = sum(
        case["kind"] == "applicable" for case in manifest_preimage["cases"]
    ) * int(manifest_preimage["repeats"])
    required_control = sum(
        case["kind"] == "control" for case in manifest_preimage["cases"]
    ) * int(manifest_preimage["repeats"])
    expected_duplicates = {
        "experiment_spec_id": manifest_preimage["experiment_spec_id"],
        "experiment_run_id": manifest_preimage["experiment_run_id"],
        "candidate_ref": manifest_preimage["candidate_ref"],
        "trusted_scope_sha256": _sha256(manifest_preimage["trusted_scope"]),
        "runtime_snapshot_sha256": _sha256(manifest_preimage["runtime_snapshot"]),
        "required_applicable_pair_count": required_applicable,
        "required_control_pair_count": required_control,
        "decision_rule": manifest_preimage["decision_rule"],
    }
    if any(
        binding.get(field) != expected
        for field, expected in expected_duplicates.items()
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_duplicate_mismatch",
            "A duplicated run-binding identity differs from its manifest evidence",
        )
    canonical = {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_RUN_BINDING_SCHEMA_VERSION,
        "experiment_spec_id": manifest_preimage["experiment_spec_id"],
        "experiment_run_id": manifest_preimage["experiment_run_id"],
        "manifest_sha256": manifest_sha256,
        "plan_sha256": plan_sha256,
        "body_free_manifest_preimage": manifest_preimage,
        "body_free_manifest_preimage_sha256": _sha256(manifest_preimage),
        "body_free_plan_preimage": canonical_plan_preimage,
        "body_free_plan_preimage_sha256": _sha256(canonical_plan_preimage),
        **expected_duplicates,
    }
    canonical["binding_sha256"] = _sha256(canonical)
    if canonical["binding_sha256"] != supplied_binding_sha256:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_run_binding_digest_mismatch",
            "The stored run binding is not canonical",
        )
    return canonical


def _canonical_trial_for_manifest(
    manifest: Mapping[str, Any], trial: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    frozen = validate_learning_advice_experiment_manifest(manifest)
    if not isinstance(trial, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_trial_invalid",
            "trial must be an object",
        )
    trial_id = _required_text(trial.get("trial_id"), field="trial.trial_id")
    plan = build_learning_advice_experiment_plan(frozen)
    expected = next(
        (item for item in plan["trials"] if item["trial_id"] == trial_id), None
    )
    if expected is None or dict(trial) != expected:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_trial_invalid",
            "The trial does not match the canonical seeded plan",
        )
    return frozen, copy.deepcopy(expected)


def _assert_trusted_scope(
    frozen: Mapping[str, Any],
    *,
    actor_user_id: str,
    organisation_concept_id: str,
    namespace: str,
) -> dict[str, str]:
    supplied = _normalise_scope(
        {
            "actor_user_id": actor_user_id,
            "organisation_concept_id": organisation_concept_id,
            "namespace": namespace,
        }
    )
    expected = frozen.get("trusted_scope")
    if supplied != expected:
        raise LearningAdviceExperimentAccessError(
            "learning_advice_experiment_scope_mismatch",
            "The experiment manifest is outside the trusted acting scope",
        )
    return supplied


def _verify_candidate_scope(
    candidate: Mapping[str, Any], *, trusted_scope: Mapping[str, str]
) -> None:
    if candidate.get("namespace") != trusted_scope["namespace"]:
        raise LearningAdviceExperimentAccessError(
            "learning_advice_experiment_candidate_scope_mismatch",
            "The canonical candidate namespace differs from the frozen scope",
        )
    visibility = candidate.get("visibility")
    if not isinstance(visibility, Mapping):
        raise LearningAdviceExperimentAccessError(
            "learning_advice_experiment_candidate_scope_mismatch",
            "The canonical candidate has no inspectable visibility scope",
        )
    visibility_scope = str(visibility.get("scope") or "").strip()
    if visibility_scope == "actor":
        visible = (
            visibility.get("actor_user_id") == trusted_scope["actor_user_id"]
            and visibility.get("organisation_concept_id")
            == trusted_scope["organisation_concept_id"]
        )
    elif visibility_scope == "organisation":
        visible = (
            visibility.get("organisation_concept_id")
            == trusted_scope["organisation_concept_id"]
        )
    else:
        visible = False
    if not visible:
        raise LearningAdviceExperimentAccessError(
            "learning_advice_experiment_candidate_scope_mismatch",
            "The canonical candidate is outside the frozen trusted scope",
        )


def _verify_frozen_candidate_identity(
    candidate: Mapping[str, Any], *, frozen_ref: Mapping[str, Any]
) -> None:
    if (
        candidate.get("schema_version") != _CANDIDATE_SCHEMA_VERSION
        or candidate.get("lifecycle_state") != _CANDIDATE_LIFECYCLE_STATE
    ):
        raise LearningAdviceExperimentDriftError(
            "learning_advice_experiment_candidate_state_drift",
            "The canonical candidate is no longer the frozen non-active candidate",
        )
    authorship = candidate.get("authorship")
    if not isinstance(authorship, Mapping) or (
        authorship.get("author_concept_id") != _CANDIDATE_AUTHOR_ID
    ):
        raise LearningAdviceExperimentDriftError(
            "learning_advice_experiment_candidate_authorship_drift",
            "The canonical candidate is no longer attributed to Von",
        )
    observed_ref = frozen_candidate_ref(candidate)
    if observed_ref != frozen_ref:
        raise LearningAdviceExperimentDriftError(
            "learning_advice_experiment_candidate_identity_drift",
            "The canonical candidate revision differs from the frozen identity",
        )
    body = candidate.get("body")
    if not isinstance(body, str) or not body.strip():
        raise LearningAdviceExperimentDriftError(
            "learning_advice_experiment_candidate_body_drift",
            "The canonical candidate body is unavailable",
        )
    if _body_sha256(body) != frozen_ref["body_sha256"]:
        raise LearningAdviceExperimentDriftError(
            "learning_advice_experiment_candidate_body_drift",
            "The canonical candidate body differs from the frozen digest",
        )


def load_frozen_learning_candidate(
    manifest: Mapping[str, Any],
    *,
    actor_user_id: str,
    organisation_concept_id: str,
    namespace: str,
    candidate_loader: CandidateLoader | None = None,
) -> dict[str, Any]:
    """Canonically reload and attest the exact candidate under trusted scope.

    Runners must call this for every trial rather than retaining a candidate
    body in memory across the comparison.  A supplied loader is a test seam;
    production defaults to ``get_learning_candidate``.
    """

    frozen = validate_learning_advice_experiment_manifest(manifest)
    trusted_scope = _assert_trusted_scope(
        frozen,
        actor_user_id=actor_user_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
    )
    loader = candidate_loader or get_learning_candidate
    try:
        candidate = loader(
            frozen["candidate_ref"]["candidate_id"],
            actor_user_id=trusted_scope["actor_user_id"],
            organisation_concept_id=trusted_scope["organisation_concept_id"],
            namespace=trusted_scope["namespace"],
        )
    except (LearningCandidateAccessError, LearningCandidateNotFoundError) as exc:
        raise LearningAdviceExperimentAccessError(
            "learning_advice_experiment_candidate_unavailable",
            "The frozen candidate is unavailable in the trusted acting scope",
        ) from exc
    except InvalidLearningCandidateData as exc:
        raise LearningAdviceExperimentDriftError(
            "learning_advice_experiment_candidate_invalid",
            "The canonical candidate no longer passes source and identity validation",
        ) from exc
    if not isinstance(candidate, Mapping):
        raise LearningAdviceExperimentDriftError(
            "learning_advice_experiment_candidate_invalid",
            "The canonical candidate loader returned an invalid projection",
        )
    candidate_copy = copy.deepcopy(dict(candidate))
    _verify_candidate_scope(candidate_copy, trusted_scope=trusted_scope)
    _verify_frozen_candidate_identity(
        candidate_copy, frozen_ref=frozen["candidate_ref"]
    )
    return candidate_copy


def build_learning_advice_projection_for_trial(
    manifest: Mapping[str, Any],
    trial: Mapping[str, Any],
    *,
    actor_user_id: str,
    organisation_concept_id: str,
    namespace: str,
    candidate_loader: CandidateLoader | None = None,
) -> dict[str, Any]:
    """Build one A/B projection solely from the frozen canonical candidate."""

    frozen, canonical_trial = _canonical_trial_for_manifest(manifest, trial)
    candidate = load_frozen_learning_candidate(
        frozen,
        actor_user_id=actor_user_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
        candidate_loader=candidate_loader,
    )
    candidate_body = str(candidate.get("body") or "")
    if candidate_body and candidate_body in canonical_trial["prompt"]:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_control_contaminated",
            "The acting prompt contains the frozen candidate body",
        )
    return build_learning_advice_experiment_projection(
        candidate,
        arm=canonical_trial["arm"],
        experiment_id=frozen["experiment_run_id"],
        case_id=canonical_trial["case_id"],
    )


def bind_fresh_trial_identities(
    trial: Mapping[str, Any],
    *,
    request_id: str,
    session_id: str,
    turn_id: str,
) -> dict[str, Any]:
    """Purely replace a trial's placeholders with runner-allocated identities."""

    if not isinstance(trial, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_trial_invalid",
            "trial must be an object",
        )
    placeholders = trial.get("fresh_identity_placeholders")
    if not isinstance(placeholders, Mapping) or set(placeholders) != {
        "request_id",
        "session_id",
        "turn_id",
    }:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_trial_invalid",
            "The trial has no complete fresh-identity requirement",
        )
    identities = {
        "request_id": _required_text(request_id, field="request_id", max_chars=500),
        "session_id": _required_text(session_id, field="session_id", max_chars=500),
        "turn_id": _required_text(turn_id, field="turn_id", max_chars=500),
    }
    if len(set(identities.values())) != len(identities):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_identity_reuse",
            "Request, session, and turn identities must be distinct",
        )
    bound = copy.deepcopy(dict(trial))
    bound.pop("fresh_identity_placeholders", None)
    bound["execution_identity"] = identities
    return bound


def derive_learning_advice_evaluator_verdict(
    *,
    capability_choice: str,
    work_product: str,
    material_failure: bool,
) -> str:
    """Derive the only consistent overall verdict from evaluator dimensions."""

    allowed = {"pass", "partial", "fail", "inconclusive"}
    dimensions: list[str] = []
    for field, value in (
        ("capability_choice", capability_choice),
        ("work_product", work_product),
    ):
        if not isinstance(value, str) or value.casefold() not in allowed:
            raise LearningAdviceExperimentError(
                "learning_advice_experiment_evaluator_verdict_invalid",
                f"{field} is not a supported evaluator verdict",
            )
        dimensions.append(value.casefold())
    if not isinstance(material_failure, bool):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_evaluator_verdict_invalid",
            "material_failure must be boolean",
        )
    if material_failure:
        return "fail"
    if "inconclusive" in dimensions:
        return "inconclusive"
    verdict_rank = {"fail": 0, "partial": 1, "pass": 2}
    return min(dimensions, key=verdict_rank.__getitem__)


def compute_learning_advice_paired_results(
    trial_observations: Sequence[Mapping[str, Any]],
    *,
    required_applicable_pair_count: int,
    required_control_pair_count: int,
) -> dict[str, Any]:
    """Recompute the paired A/B evidence from canonical trial observations."""

    for field, value in (
        ("required_applicable_pair_count", required_applicable_pair_count),
        ("required_control_pair_count", required_control_pair_count),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise LearningAdviceExperimentError(
                "learning_advice_experiment_paired_result_invalid",
                f"{field} must be a positive integer",
            )
    if not isinstance(trial_observations, Sequence) or isinstance(
        trial_observations, (str, bytes, bytearray)
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_paired_result_invalid",
            "Trial observations must be a sequence",
        )
    expected_trial_count = (
        required_applicable_pair_count + required_control_pair_count
    ) * 2
    if len(trial_observations) != expected_trial_count or not all(
        isinstance(item, Mapping) for item in trial_observations
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_paired_result_invalid",
            "Trial observations do not match the predeclared pair counts",
        )

    verdict_rank = {"fail": 0, "partial": 1, "pass": 2}
    by_pair: dict[tuple[str, int], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    trial_ids: set[str] = set()
    for observation in trial_observations:
        case_id = observation.get("case_id")
        repeat = observation.get("repeat")
        arm = observation.get("arm")
        case_kind = observation.get("case_kind")
        trial_id = observation.get("trial_id")
        if (
            not isinstance(case_id, str)
            or not case_id.strip()
            or not isinstance(repeat, int)
            or isinstance(repeat, bool)
            or repeat < 1
            or arm not in LEARNING_ADVICE_EXPERIMENT_ARMS
            or case_kind not in _CASE_KINDS
            or not isinstance(trial_id, str)
            or not trial_id.strip()
            or trial_id in trial_ids
        ):
            raise LearningAdviceExperimentError(
                "learning_advice_experiment_paired_result_invalid",
                "A trial observation has an invalid or duplicate identity",
            )
        trial_ids.add(trial_id)
        pair = by_pair[(case_id, repeat)]
        if arm in pair:
            raise LearningAdviceExperimentError(
                "learning_advice_experiment_paired_result_invalid",
                "A case repeat contains a duplicate experiment arm",
            )
        pair[str(arm)] = observation

    required_pair_count = required_applicable_pair_count + required_control_pair_count
    if len(by_pair) != required_pair_count:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_paired_result_invalid",
            "Trial observations do not form the predeclared number of pairs",
        )

    pairs: list[dict[str, Any]] = []
    for (case_id, repeat), arms in sorted(by_pair.items()):
        if set(arms) != set(LEARNING_ADVICE_EXPERIMENT_ARMS):
            raise LearningAdviceExperimentError(
                "learning_advice_experiment_paired_result_invalid",
                "Every case repeat must contain exactly one A and one B trial",
            )
        a = arms["A"]
        b = arms["B"]
        if a.get("case_kind") != b.get("case_kind"):
            raise LearningAdviceExperimentError(
                "learning_advice_experiment_paired_result_invalid",
                "The two arms of a pair disagree about case kind",
            )
        case_kind = str(a["case_kind"])
        a_evaluation = a.get("evaluation")
        b_evaluation = b.get("evaluation")
        valid = bool(
            a.get("trial_integrity_valid") is True
            and b.get("trial_integrity_valid") is True
            and isinstance(a_evaluation, Mapping)
            and isinstance(b_evaluation, Mapping)
            and a_evaluation.get("verdict") in verdict_rank
            and b_evaluation.get("verdict") in verdict_rank
        )
        a_verdict = a_evaluation["verdict"] if valid else None
        b_verdict = b_evaluation["verdict"] if valid else None
        availability_discounted = bool(
            valid
            and (
                a_evaluation.get("external_availability_changed") is True
                or b_evaluation.get("external_availability_changed") is True
            )
        )
        if not valid:
            outcome = "inconclusive"
        elif availability_discounted:
            outcome = "availability_changed"
        elif verdict_rank[str(b_verdict)] > verdict_rank[str(a_verdict)]:
            outcome = "b_better"
        elif verdict_rank[str(a_verdict)] > verdict_rank[str(b_verdict)]:
            outcome = "a_better"
        else:
            outcome = f"tie_{a_verdict}"
        pairs.append(
            {
                "case_id": case_id,
                "case_kind": case_kind,
                "repeat": repeat,
                "valid": valid,
                "a_verdict": a_verdict,
                "b_verdict": b_verdict,
                "a_passed": a_verdict == "pass" if valid else None,
                "b_passed": b_verdict == "pass" if valid else None,
                "availability_discounted": availability_discounted,
                "b_only_material_failure": bool(
                    valid
                    and not availability_discounted
                    and b_evaluation.get("material_failure") is True
                    and a_evaluation.get("material_failure") is not True
                ),
                "outcome": outcome,
            }
        )

    all_applicable_pairs = [pair for pair in pairs if pair["case_kind"] == "applicable"]
    all_control_pairs = [pair for pair in pairs if pair["case_kind"] == "control"]
    if (
        len(all_applicable_pairs) != required_applicable_pair_count
        or len(all_control_pairs) != required_control_pair_count
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_paired_result_invalid",
            "Observed case kinds do not match the predeclared pair counts",
        )
    valid_pairs = [pair for pair in pairs if pair["valid"]]
    applicable_pairs = [pair for pair in all_applicable_pairs if pair["valid"]]
    control_pairs = [pair for pair in all_control_pairs if pair["valid"]]
    comparable_applicable_pairs = [
        pair for pair in applicable_pairs if not pair["availability_discounted"]
    ]
    comparable_control_pairs = [
        pair for pair in control_pairs if not pair["availability_discounted"]
    ]
    outcome_counts = Counter(pair["outcome"] for pair in pairs)
    all_pairs_valid = len(valid_pairs) == len(pairs)
    all_pairs_comparable = (
        len(comparable_applicable_pairs) == required_applicable_pair_count
        and len(comparable_control_pairs) == required_control_pair_count
    )
    if not all_pairs_valid or not all_pairs_comparable:
        comparison = "inconclusive"
    elif outcome_counts["b_better"] > outcome_counts["a_better"]:
        comparison = "favours_b"
    elif outcome_counts["a_better"] > outcome_counts["b_better"]:
        comparison = "favours_a"
    else:
        comparison = "tie"
    comparable_pairs = [*comparable_applicable_pairs, *comparable_control_pairs]
    return {
        "pair_count": len(pairs),
        "valid_pair_count": len(valid_pairs),
        "comparison": comparison,
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "applicable": {
            "pair_count": len(applicable_pairs),
            "required_comparable_pair_count": required_applicable_pair_count,
            "comparable_pair_count": len(comparable_applicable_pairs),
            "availability_discounted_pair_count": (
                len(applicable_pairs) - len(comparable_applicable_pairs)
            ),
            "a_pass_count": sum(
                pair["a_verdict"] == "pass" for pair in comparable_applicable_pairs
            ),
            "b_pass_count": sum(
                pair["b_verdict"] == "pass" for pair in comparable_applicable_pairs
            ),
            "b_better_count": sum(
                pair["outcome"] == "b_better" for pair in comparable_applicable_pairs
            ),
            "a_better_count": sum(
                pair["outcome"] == "a_better" for pair in comparable_applicable_pairs
            ),
        },
        "controls": {
            "pair_count": len(control_pairs),
            "required_comparable_pair_count": required_control_pair_count,
            "comparable_pair_count": len(comparable_control_pairs),
            "availability_discounted_pair_count": (
                len(control_pairs) - len(comparable_control_pairs)
            ),
            "negative_transfer_count": sum(
                pair["outcome"] == "a_better" for pair in comparable_control_pairs
            ),
        },
        "b_only_material_failure_count": sum(
            pair["b_only_material_failure"] for pair in comparable_pairs
        ),
        "pairs": pairs,
    }


def evaluate_learning_advice_content_decision(
    paired_results: Mapping[str, Any],
    *,
    decision_rule: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the frozen decision rule to independently recomputed pair evidence."""

    if not isinstance(paired_results, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_paired_result_invalid",
            "Paired results must be an object",
        )
    applicable = paired_results.get("applicable")
    controls = paired_results.get("controls")
    if not isinstance(applicable, Mapping) or not isinstance(controls, Mapping):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_paired_result_invalid",
            "Paired results have no applicable and control summaries",
        )
    required_applicable = applicable.get("required_comparable_pair_count")
    if not isinstance(required_applicable, int) or isinstance(
        required_applicable, bool
    ):
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_paired_result_invalid",
            "Paired results have no predeclared applicable count",
        )
    frozen_rule = _normalise_decision_rule(
        decision_rule,
        applicable_trial_count=required_applicable,
    )
    try:
        b_passes = int(applicable["b_pass_count"])
        pass_delta = b_passes - int(applicable["a_pass_count"])
        gates = [
            {
                "gate": "minimum_applicable_b_passes",
                "passed": b_passes >= int(frozen_rule["minimum_applicable_b_passes"]),
                "observed": b_passes,
                "required": int(frozen_rule["minimum_applicable_b_passes"]),
            },
            {
                "gate": "minimum_applicable_pass_delta",
                "passed": pass_delta
                >= int(frozen_rule["minimum_applicable_pass_delta"]),
                "observed": pass_delta,
                "required": int(frozen_rule["minimum_applicable_pass_delta"]),
            },
            {
                "gate": "more_paired_improvements_than_regressions",
                "passed": (
                    int(applicable["b_better_count"])
                    > int(applicable["a_better_count"])
                    if frozen_rule["require_more_paired_improvements_than_regressions"]
                    else True
                ),
                "b_improvements": int(applicable["b_better_count"]),
                "b_regressions": int(applicable["a_better_count"]),
            },
            {
                "gate": "no_b_only_material_failure",
                "passed": (
                    int(paired_results["b_only_material_failure_count"]) == 0
                    if frozen_rule["forbid_b_only_material_failure"]
                    else True
                ),
                "observed": int(paired_results["b_only_material_failure_count"]),
            },
            {
                "gate": "control_non_inferiority",
                "passed": (
                    int(controls["negative_transfer_count"]) == 0
                    if frozen_rule["require_control_non_inferiority"]
                    else True
                ),
                "comparable_pair_count": int(controls["comparable_pair_count"]),
                "availability_discounted_pair_count": int(
                    controls["availability_discounted_pair_count"]
                ),
                "negative_transfer_count": int(controls["negative_transfer_count"]),
            },
            {
                "gate": "all_predeclared_applicable_pairs_comparable",
                "passed": (
                    int(applicable["comparable_pair_count"])
                    == int(applicable["required_comparable_pair_count"])
                ),
                "observed": int(applicable["comparable_pair_count"]),
                "required": int(applicable["required_comparable_pair_count"]),
            },
            {
                "gate": "all_predeclared_control_pairs_comparable",
                "passed": (
                    int(controls["comparable_pair_count"])
                    == int(controls["required_comparable_pair_count"])
                ),
                "observed": int(controls["comparable_pair_count"]),
                "required": int(controls["required_comparable_pair_count"]),
            },
            {
                "gate": "all_pairs_complete_and_valid",
                "passed": paired_results["valid_pair_count"]
                == paired_results["pair_count"],
                "observed": int(paired_results["valid_pair_count"]),
                "required": int(paired_results["pair_count"]),
            },
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise LearningAdviceExperimentError(
            "learning_advice_experiment_paired_result_invalid",
            "Paired results cannot be evaluated by the frozen decision rule",
        ) from exc
    inconclusive_gate_names = {
        "all_predeclared_applicable_pairs_comparable",
        "all_predeclared_control_pairs_comparable",
        "all_pairs_complete_and_valid",
    }
    if any(
        gate["passed"] is not True and gate["gate"] in inconclusive_gate_names
        for gate in gates
    ):
        decision = "inconclusive"
    elif all(gate["passed"] is True for gate in gates):
        decision = "arm_b_content_win"
    else:
        decision = "arm_b_not_supported"
    return {
        "decision": decision,
        "activation_authorised": False,
        "decision_rule": copy.deepcopy(frozen_rule),
        "gates": gates,
    }
