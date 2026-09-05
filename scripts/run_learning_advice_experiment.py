"""Run the frozen Phase-1 learning-advice A/B experiment.

This module intentionally exposes an importable, dependency-injected runner
rather than an HTTP or ordinary-chat control surface.  Its public runner accepts
a body-free frozen manifest, reloads the canonical candidate for every trial,
and invokes the existing ``execute_adaptive_turn`` service directly.  It has no
argument for advice text or an arbitrary retrieval rationale.

The blind outcome projection contains the user prompt and bounded outcome
evidence, never the arm, candidate identity, projection, manifest, evaluator,
rubric, or experiment identifiers.  A trusted evaluator executor separately
receives digest-verified evaluator/rubric bytes and the frozen model config and
must return independent model-call telemetry.  Evaluations are joined back to
arms only after that blind call returns.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import random
import re
import statistics
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.backend.services.adaptive_turn_service import execute_adaptive_turn
from src.backend.services.experiment_run_service import (
    abort_experiment_run,
    compute_experiment_verdict,
    get_experiment_run_state,
    record_experiment_observation,
)
from src.backend.services.learning_advice_experiment_service import (
    LEARNING_ADVICE_EXPERIMENT_CASE_COUNT,
    LEARNING_ADVICE_EXPERIMENT_RUN_BINDING_SCHEMA_VERSION,
    LearningAdviceExperimentError,
    bind_fresh_trial_identities,
    build_learning_advice_experiment_plan,
    build_learning_advice_experiment_run_binding,
    build_learning_advice_projection_for_trial,
    compute_learning_advice_paired_results,
    derive_learning_advice_evaluator_verdict,
    evaluate_learning_advice_content_decision,
    is_sol_family_model_id,
    load_frozen_learning_candidate,
    validate_learning_advice_experiment_manifest,
    validate_learning_advice_experiment_run_binding,
)
from src.backend.services.learning_advice_projection_service import (
    render_learning_advice_projection,
)
from src.backend.services.learning_candidate_vontology_service import (
    get_learning_candidate,
    record_learning_candidate_disposition,
)
from src.backend.services.turn_execution_record_service import (
    TURN_EXECUTION_RECORD_SCHEMA_VERSION,
    build_turn_execution_record,
    extract_learning_advice_exposures,
    get_turn_execution_record_projection,
    turn_execution_record_evidence_sha256,
    upsert_turn_execution_record_projection,
)

LEARNING_ADVICE_EXPERIMENT_RESULT_SCHEMA_VERSION = (
    "learning_advice_experiment_result.v1"
)
LEARNING_ADVICE_EXPERIMENT_OBSERVATION_SCHEMA_VERSION = (
    "learning_advice_experiment_observation.v1"
)
LEARNING_ADVICE_BLIND_EVALUATION_INPUT_SCHEMA_VERSION = (
    "capability_choice_trial_evaluation_input.v1"
)
LEARNING_ADVICE_BLIND_EVALUATION_RESULT_SCHEMA_VERSION = (
    "learning_advice_blind_evaluation_result.v1"
)
LEARNING_ADVICE_EVALUATOR_PROVENANCE_SCHEMA_VERSION = (
    "learning_advice_evaluator_provenance.v1"
)
LEARNING_ADVICE_EVALUATOR_MODEL_CALL_RECEIPT_SCHEMA_VERSION = (
    "learning_advice_evaluator_model_call_receipt.v1"
)
LEARNING_ADVICE_EXPERIMENT_ABORT_SCHEMA_VERSION = "learning_advice_experiment_abort.v1"
LEARNING_ADVICE_EXPERIMENT_ABORT_RECOVERY_SCHEMA_VERSION = (
    "learning_advice_experiment_abort_recovery.v1"
)
_TRIAL_EXECUTION_DIAGNOSTIC_SCHEMA_VERSION = (
    "learning_advice_experiment_trial_execution.v1"
)
_TRIAL_TER_BINDING_AUX_TYPE = "learning_advice_experiment_trial_binding"
_ACTING_SUPPORT_IDENTITY_SCHEMA_VERSION = (
    "learning_advice_experiment_acting_support_identity.v1"
)
_EVALUATOR_RUNTIME_IDENTITY_SCHEMA_VERSION = (
    "learning_advice_evaluator_runtime_identity.v1"
)
_EVALUATOR_MODEL_CONFIG_SCHEMA_VERSION = "learning_advice_evaluator_model_config.v1"
_ADAPTIVE_MODEL_REQUEST_OBSERVATION_SCHEMA_VERSION = (
    "adaptive_turn_model_request_observation.v1"
)
_LEARNING_ADVICE_REQUEST_ATTESTATION_SCHEMA_VERSION = (
    "learning_advice_model_request_attestation.v1"
)
_EFFECT_POLICY = "deny_all_effects_before_dispatch"
_ALLOW_REPRESENTED_WORKFLOW_DISCOVERY = False
_ALLOW_REPRESENTED_TOOL_PROJECTION = False
_TERMINAL_EXPERIMENT_RUN_STATUSES = frozenset({"completed", "failed"})
_SHA256_HEX_CHARS = frozenset("0123456789abcdef")
_OBSERVATION_FORBIDDEN_KEYS = {
    "advice",
    "advice_body",
    "body",
    "candidate_body",
    "response_text",
}
_BLIND_EVALUATION_IDENTITY_FIELDS = {
    "arm",
    "candidate_id",
    "candidate_ref",
    "candidate_revision",
    "candidate_body_sha256",
    "candidate_revision_identity_sha256",
    "candidate_source_locator_sha256",
    "evaluator_concept_id",
    "evaluator_sha256",
    "experiment_run_id",
    "experiment_spec_id",
    "exposure",
    "learning_advice_exposures",
    "manifest_sha256",
    "plan_sha256",
    "projection",
    "projection_sha256",
    "rubric_concept_id",
    "rubric_sha256",
    "trial_id",
}

logger = logging.getLogger(__name__)


class LearningAdviceExperimentRunnerError(RuntimeError):
    """Raised when experiment evidence cannot be completed inspectably."""

    def __init__(self, reason_code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.reason_code = reason_code
        self.safe_message = safe_message


class LearningAdviceExperimentReadOnlyViolation(RuntimeError):
    """Raised before a disallowed capability can reach its handler."""


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
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_runner_not_json",
            "Experiment evidence must contain only JSON values",
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_digest(value: Any, *, field: str) -> str:
    digest = _text(value, max_chars=64).casefold()
    if len(digest) != 64 or any(char not in _SHA256_HEX_CHARS for char in digest):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_identity_invalid",
            f"{field} must be a SHA-256 digest",
        )
    return digest


def _text(value: Any, *, max_chars: int = 1_000) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_chars]


def _field(result: Any, field: str, default: Any = None) -> Any:
    if isinstance(result, Mapping):
        return result.get(field, default)
    return getattr(result, field, default)


def _mapping_sequence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [copy.deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        resolved = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return resolved.astimezone(UTC).isoformat().replace("+00:00", "Z")
    text = _text(value, max_chars=100)
    if not text:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_timestamp_invalid",
            "The runner timestamp factory returned no timestamp",
        )
    return text


def _gateway_method_snapshot(gateway: Any) -> dict[str, dict[str, Any]]:
    describe = getattr(gateway, "describe_methods", None)
    if not callable(describe) or getattr(gateway, "enabled", False) is not True:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_gateway_invalid",
            "The experiment requires an enabled inspectable test gateway",
        )
    raw = describe()
    if not isinstance(raw, Mapping):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_gateway_invalid",
            "The test gateway returned no inspectable capability catalogue",
        )
    snapshot: dict[str, dict[str, Any]] = {}
    for raw_name, raw_metadata in sorted(raw.items(), key=lambda item: str(item[0])):
        name = _text(raw_name, max_chars=300)
        if not name or not isinstance(raw_metadata, Mapping):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_gateway_invalid",
                "The test gateway capability catalogue is malformed",
            )
        metadata = copy.deepcopy(dict(raw_metadata))
        _canonical_bytes(metadata)
        if metadata.get("category") != "read":
            continue
        if metadata.get("ordinary_turn_effect") is True:
            continue
        snapshot[name] = metadata
    if not snapshot:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_gateway_invalid",
            "The test gateway exposes no read-only capability",
        )
    return snapshot


def learning_advice_read_only_gateway_catalogue_sha256(gateway: Any) -> str:
    """Return the canonical identity of the capabilities the runner will expose."""

    return _sha256(_gateway_method_snapshot(gateway))


class _ReadOnlyExperimentGateway:
    """Expose reads while denying every write/effect before handler dispatch."""

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway
        self._methods = _gateway_method_snapshot(gateway)
        self.blocked_effect_attempts: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    def describe_methods(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._methods)

    def _read_definition(self, method_name: str) -> Any:
        if method_name not in self._methods:
            return None
        current = _gateway_method_snapshot(self._gateway).get(method_name)
        if current != self._methods[method_name]:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_gateway_drift",
                "The test gateway capability changed after its frozen projection",
            )
        definition_loader = getattr(self._gateway, "get_method_definition", None)
        definition = (
            definition_loader(method_name) if callable(definition_loader) else None
        )
        if (
            definition is None
            or getattr(definition, "category", None) != "read"
            or getattr(definition, "ordinary_turn_effect", False) is True
        ):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_gateway_drift",
                "The test gateway method definition is not the frozen read capability",
            )
        return definition

    def get_method_definition(self, method_name: str) -> Any:
        return self._read_definition(method_name)

    def get_method_timeout_sec(self, method_name: str) -> float | None:
        if self._read_definition(method_name) is None:
            return None
        loader = getattr(self._gateway, "get_method_timeout_sec", None)
        return loader(method_name) if callable(loader) else None

    def get_method_effect_admission_window_sec(self, method_name: str) -> None:
        del method_name

    def invoke(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        if self._read_definition(method_name) is None:
            self.blocked_effect_attempts.append(str(method_name))
            raise LearningAdviceExperimentReadOnlyViolation(
                "The learning-advice experiment test world denies all effects"
            )
        invoke = getattr(self._gateway, "invoke", None)
        if not callable(invoke):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_gateway_invalid",
                "The test gateway has no capability invocation surface",
            )
        return invoke(method_name, *args, **kwargs)


def _normalise_gateway_snapshot(value: Any) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(value, Mapping) or set(value) != {
        "gateway_identity_sha256",
        "replay_world_sha256",
        "model_visible_texts",
    }:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_gateway_snapshot_invalid",
            "The test gateway snapshot must identify its gateway, replay world, and visible text",
        )
    raw_texts = value.get("model_visible_texts")
    if not isinstance(raw_texts, Sequence) or isinstance(
        raw_texts, (str, bytes, bytearray)
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_gateway_snapshot_invalid",
            "The test gateway visible-text snapshot must be a list",
        )
    texts: list[str] = []
    for item in raw_texts:
        if not isinstance(item, str):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_gateway_snapshot_invalid",
                "Every model-visible replay item must be text",
            )
        texts.append(item)
    identity = {
        "gateway_identity_sha256": _sha256_digest(
            value.get("gateway_identity_sha256"),
            field="gateway_snapshot.gateway_identity_sha256",
        ),
        "replay_world_sha256": _sha256_digest(
            value.get("replay_world_sha256"),
            field="gateway_snapshot.replay_world_sha256",
        ),
        "model_visible_texts_sha256": _sha256(texts),
    }
    return identity, texts


def build_learning_advice_acting_support_identity(
    *,
    context: Sequence[Mapping[str, Any]],
    trusted_argument_values: Mapping[str, Any] | None,
    workflow_launch_inputs: Mapping[str, Any] | None,
    model_registry_snapshot: Any,
    model_parameters: Mapping[str, Any],
    gateway_snapshot_identity: Mapping[str, Any],
    capability_catalogue_sha256: str,
    allow_represented_workflow_discovery: bool,
    allow_represented_tool_projection: bool,
) -> dict[str, Any]:
    """Build the body-free identity frozen as runtime acting support."""

    payload = {
        "schema_version": _ACTING_SUPPORT_IDENTITY_SCHEMA_VERSION,
        "context_sha256": _sha256(list(context)),
        "trusted_argument_values_sha256": _sha256(trusted_argument_values),
        "workflow_launch_inputs_sha256": _sha256(workflow_launch_inputs),
        "model_registry_snapshot_sha256": _sha256(model_registry_snapshot),
        "model_parameters_sha256": _sha256(model_parameters),
        "gateway_identity_sha256": _sha256_digest(
            gateway_snapshot_identity.get("gateway_identity_sha256"),
            field="gateway_snapshot_identity.gateway_identity_sha256",
        ),
        "replay_world_sha256": _sha256_digest(
            gateway_snapshot_identity.get("replay_world_sha256"),
            field="gateway_snapshot_identity.replay_world_sha256",
        ),
        "model_visible_texts_sha256": _sha256_digest(
            gateway_snapshot_identity.get("model_visible_texts_sha256"),
            field="gateway_snapshot_identity.model_visible_texts_sha256",
        ),
        "capability_catalogue_sha256": _sha256_digest(
            capability_catalogue_sha256,
            field="capability_catalogue_sha256",
        ),
        "effect_policy": _EFFECT_POLICY,
        "allow_represented_workflow_discovery": bool(
            allow_represented_workflow_discovery
        ),
        "allow_represented_tool_projection": bool(allow_represented_tool_projection),
    }
    payload["acting_support_sha256"] = _sha256(payload)
    return payload


def build_learning_advice_evaluator_model_config(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the frozen, body-free launch configuration for the evaluator."""

    frozen = validate_learning_advice_experiment_manifest(manifest)
    provider = str(frozen["model"]["provider"])
    model_id = str(frozen["model"]["model_id"])
    runtime_identity = {
        "schema_version": _EVALUATOR_RUNTIME_IDENTITY_SCHEMA_VERSION,
        "provider": provider,
        "requested_model": model_id,
        "selected_model": model_id,
        "effective_model": model_id,
        "model_parameters_sha256": _sha256(frozen["model"]["parameters"]),
        "code_revision": frozen["runtime_snapshot"]["code_revision"],
        "runtime_snapshot_sha256": _sha256(frozen["runtime_snapshot"]),
    }
    return {
        "schema_version": _EVALUATOR_MODEL_CONFIG_SCHEMA_VERSION,
        "provider": provider,
        "model_id": model_id,
        "parameters": copy.deepcopy(frozen["model"]["parameters"]),
        "code_revision": frozen["runtime_snapshot"]["code_revision"],
        "runtime_snapshot_sha256": runtime_identity["runtime_snapshot_sha256"],
        "runtime_code_identity_sha256": _sha256(runtime_identity),
    }


def build_learning_advice_evaluator_provenance_identity(
    manifest: Mapping[str, Any],
    *,
    evaluator_concept_id: str,
    evaluator_sha256: str,
    rubric_concept_id: str,
    rubric_sha256: str,
    model_call_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive inspectable provenance from authority and provider telemetry."""

    config = build_learning_advice_evaluator_model_config(manifest)
    return {
        "schema_version": LEARNING_ADVICE_EVALUATOR_PROVENANCE_SCHEMA_VERSION,
        "model_call_id": model_call_receipt["call_id"],
        "provider": model_call_receipt["provider"],
        "requested_model": model_call_receipt["requested_model"],
        "selected_model": model_call_receipt["selected_model"],
        "effective_model": model_call_receipt["effective_model"],
        "provider_observed_model": model_call_receipt["provider_observed_model"],
        "model_call_receipt_sha256": _sha256(model_call_receipt),
        "model_parameters_sha256": _sha256(config["parameters"]),
        "code_revision": config["code_revision"],
        "runtime_snapshot_sha256": config["runtime_snapshot_sha256"],
        "runtime_code_identity_sha256": config["runtime_code_identity_sha256"],
        "evaluator_concept_id": _text(evaluator_concept_id, max_chars=500),
        "evaluator_sha256": _sha256_digest(evaluator_sha256, field="evaluator_sha256"),
        "rubric_concept_id": _text(rubric_concept_id, max_chars=500),
        "rubric_sha256": _sha256_digest(rubric_sha256, field="rubric_sha256"),
    }


def _load_gateway_snapshot(
    *,
    loader: Callable[..., Mapping[str, Any]],
    gateway: Any,
    scope: Mapping[str, str],
) -> tuple[dict[str, Any], list[str]]:
    try:
        loaded = loader(
            gateway,
            actor_user_id=scope["actor_user_id"],
            organisation_concept_id=scope["organisation_concept_id"],
            namespace=scope["namespace"],
        )
    except Exception as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_gateway_snapshot_unavailable",
            "The test gateway and replay world could not be canonically re-attested",
        ) from exc
    return _normalise_gateway_snapshot(loaded)


def _prepare_acting_gateway(
    *,
    gateway: Any,
    gateway_snapshot_loader: Callable[..., Mapping[str, Any]],
    scope: Mapping[str, str],
    runtime_snapshot: Mapping[str, Any],
    frozen_inputs: Mapping[str, Any],
) -> tuple[_ReadOnlyExperimentGateway, dict[str, Any], list[str]]:
    read_only_gateway = _ReadOnlyExperimentGateway(gateway)
    catalogue_sha256 = _sha256(read_only_gateway.describe_methods())
    if catalogue_sha256 != runtime_snapshot["capability_catalogue_sha256"]:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_capability_catalogue_drift",
            "The read-only acting catalogue differs from the frozen runtime",
        )
    gateway_identity, model_visible_texts = _load_gateway_snapshot(
        loader=gateway_snapshot_loader,
        gateway=gateway,
        scope=scope,
    )
    acting_support_identity = build_learning_advice_acting_support_identity(
        context=frozen_inputs["context"],
        trusted_argument_values=frozen_inputs["trusted_argument_values"],
        workflow_launch_inputs=frozen_inputs["workflow_launch_inputs"],
        model_registry_snapshot=frozen_inputs["model_registry_snapshot"],
        model_parameters=frozen_inputs["model_parameters"],
        gateway_snapshot_identity=gateway_identity,
        capability_catalogue_sha256=catalogue_sha256,
        allow_represented_workflow_discovery=(_ALLOW_REPRESENTED_WORKFLOW_DISCOVERY),
        allow_represented_tool_projection=_ALLOW_REPRESENTED_TOOL_PROJECTION,
    )
    if (
        acting_support_identity["acting_support_sha256"]
        != runtime_snapshot["acting_support_sha256"]
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_acting_support_drift",
            "The acting inputs or replay world differ from the frozen runtime",
        )
    return read_only_gateway, acting_support_identity, model_visible_texts


def _default_identity_factory(kind: str) -> str:
    return f"{kind}-{uuid.uuid4()}"


def _next_fresh_identity(
    *,
    kind: str,
    identity_factory: Callable[[str], str],
    used: set[str],
) -> str:
    value = _text(identity_factory(kind), max_chars=500)
    if not value:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_identity_invalid",
            f"The identity factory returned no {kind} identity",
        )
    if value in used:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_identity_reuse",
            f"The identity factory reused a {kind} identity",
        )
    used.add(value)
    return value


def _load_candidate_source_texts(
    *,
    loader: Callable[..., Mapping[str, Any]],
    candidate: Mapping[str, Any],
    scope: Mapping[str, str],
) -> list[str]:
    try:
        loaded = loader(
            copy.deepcopy(candidate["source"]),
            actor_user_id=scope["actor_user_id"],
            organisation_concept_id=scope["organisation_concept_id"],
            namespace=scope["namespace"],
        )
    except Exception as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_source_text_unavailable",
            "The candidate's learning source text could not be canonically loaded",
        ) from exc
    if not isinstance(loaded, Mapping) or set(loaded) != {
        "source_locator_sha256",
        "texts",
    }:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_source_text_invalid",
            "The candidate source-text loader returned an invalid projection",
        )
    if loaded.get("source_locator_sha256") != candidate.get("source_locator_sha256"):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_source_text_drift",
            "The candidate source text differs from the frozen source locator",
        )
    raw_texts = loaded.get("texts")
    if not isinstance(raw_texts, Sequence) or isinstance(
        raw_texts, (str, bytes, bytearray)
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_source_text_invalid",
            "The candidate source-text projection must contain a list",
        )
    texts: list[str] = []
    observed_digests: list[str] = []
    for item in raw_texts:
        if not isinstance(item, Mapping) or set(item) != {"content", "content_sha256"}:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_source_text_invalid",
                "Every candidate source-text item must carry exact content and digest",
            )
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_source_text_invalid",
                "Every candidate source-text item must contain text",
            )
        digest = _sha256_digest(
            item.get("content_sha256"), field="source_text.content_sha256"
        )
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_source_text_drift",
                "Candidate source text does not match its canonical content digest",
            )
        texts.append(content)
        observed_digests.append(digest)
    if not texts:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_source_text_invalid",
            "At least one canonical learning-source text is required",
        )

    source = candidate.get("source")
    expected_digests: list[str] = []
    if isinstance(source, Mapping):
        evidence = source.get("message_evidence")
        if isinstance(evidence, Sequence) and not isinstance(
            evidence, (str, bytes, bytearray)
        ):
            expected_digests = [
                str(item["content_sha256"])
                for item in evidence
                if isinstance(item, Mapping)
                and isinstance(item.get("content_sha256"), str)
            ]
    if expected_digests and sorted(observed_digests) != sorted(expected_digests):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_source_text_drift",
            "The loaded source texts do not match every frozen source-message digest",
        )
    return texts


def _source_fragments(texts: Sequence[str]) -> list[str]:
    fragments: list[str] = []
    for text in texts:
        candidates = [
            text,
            *text.splitlines(),
            *re.split(r"(?<=[.!?])\s+", text),
        ]
        for candidate in candidates:
            cleaned = " ".join(candidate.split()).casefold()
            if len(cleaned) >= 40 and cleaned not in fragments:
                fragments.append(cleaned)
    return fragments


def _iter_text_values(value: Any, *, path: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, Mapping):
        result: list[tuple[str, str]] = []
        for key, item in value.items():
            if isinstance(key, str):
                result.append((f"{path}.<key>", key))
            result.extend(_iter_text_values(item, path=f"{path}.{key}"))
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = []
        for index, item in enumerate(value):
            result.extend(_iter_text_values(item, path=f"{path}[{index}]"))
        return result
    return []


def _assert_no_learning_text_contamination(
    *,
    candidate_body: str,
    source_texts: Sequence[str],
    acting_inputs: Mapping[str, Any],
) -> None:
    learning_fragments = _source_fragments([candidate_body, *source_texts])
    normalised_body = " ".join(candidate_body.split()).casefold()
    if normalised_body and normalised_body not in learning_fragments:
        learning_fragments.insert(0, normalised_body)
    for path, raw_text in _iter_text_values(acting_inputs, path="acting_inputs"):
        acting_text = " ".join(raw_text.split()).casefold()
        if any(fragment in acting_text for fragment in learning_fragments):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_control_contaminated",
                f"Frozen candidate or learning-source text appears in {path}",
            )


def _learning_text_echo_path(
    *,
    learning_fragments: Sequence[str],
    outcome_projection: Mapping[str, Any],
) -> str | None:
    """Return the first acting-output path that would reveal the treatment."""

    for path, raw_text in _iter_text_values(
        outcome_projection,
        path="evaluator_outcome",
    ):
        normalised = " ".join(raw_text.split()).casefold()
        if any(fragment in normalised for fragment in learning_fragments):
            return path
    return None


def _build_model_request_observer(
    *,
    trial: Mapping[str, Any],
    projection: Mapping[str, Any],
    manifest: Mapping[str, Any],
    learning_fragments: Sequence[str],
) -> tuple[Callable[[Mapping[str, Any]], None], list[dict[str, Any]]]:
    """Build a fail-before-submit observer and its body-free attestations."""

    expected_keys = {
        "schema_version",
        "model_call_id",
        "stage",
        "prompt",
        "tools",
        "context",
        "model",
        "system_message",
        "model_parameters",
        "continuation",
        "tool_results",
    }
    expected_sidecar = render_learning_advice_projection(projection)
    if (trial["arm"] == "A" and expected_sidecar is not None) or (
        trial["arm"] == "B" and not expected_sidecar
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_request_projection_invalid",
            "The frozen arm does not produce its expected model request sidecar",
        )
    attestations: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()

    def observe(request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping) or set(request) != expected_keys:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_request_observation_invalid",
                "The adaptive turn exposed no exact structured provider request",
            )
        if request.get("schema_version") != (
            _ADAPTIVE_MODEL_REQUEST_OBSERVATION_SCHEMA_VERSION
        ):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_request_observation_invalid",
                "The adaptive-turn request observation schema is unsupported",
            )
        call_id = _text(request.get("model_call_id"), max_chars=500)
        if not call_id or call_id in seen_call_ids:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_request_observation_invalid",
                "Every provider request must expose one unique model call identity",
            )
        if (
            request.get("model") != manifest["model"]["model_id"]
            or request.get("model_parameters") != manifest["model"]["parameters"]
        ):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_request_model_drift",
                "The exact outbound request differs from the frozen model configuration",
            )
        stage = request.get("stage")
        if stage not in {"adaptive_research", "final_synthesis"}:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_request_observation_invalid",
                "The exact outbound request has no recognised adaptive-turn stage",
            )
        try:
            encoded = _canonical_bytes(request)
        except LearningAdviceExperimentRunnerError:
            raise
        except Exception as exc:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_request_observation_invalid",
                "The exact outbound request is not canonically inspectable",
            ) from exc

        sidecar_occurrences = 0
        sidecar_paths: list[str] = []
        for path, raw_text in _iter_text_values(request, path="model_request"):
            residual = raw_text
            if expected_sidecar:
                occurrences = raw_text.count(expected_sidecar)
                sidecar_occurrences += occurrences
                if occurrences:
                    sidecar_paths.extend([path] * occurrences)
                    residual = raw_text.replace(expected_sidecar, "")
            normalised = " ".join(residual.split()).casefold()
            if any(fragment in normalised for fragment in learning_fragments):
                raise LearningAdviceExperimentRunnerError(
                    "learning_advice_experiment_request_contaminated",
                    f"Frozen candidate or source text appears outside its sidecar in {path}",
                )
        expected_sidecar_occurrences = int(
            trial["arm"] == "B" and stage == "adaptive_research"
        )
        if sidecar_occurrences != expected_sidecar_occurrences or (
            sidecar_occurrences == 1
            and sidecar_paths != ["model_request.system_message"]
        ):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_request_contaminated",
                "The outbound request does not contain exactly its bounded advice sidecar",
            )
        if trial["arm"] == "A" and sidecar_occurrences:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_request_contaminated",
                "Arm A contains an advice sidecar",
            )
        seen_call_ids.add(call_id)
        attestations.append(
            {
                "schema_version": _LEARNING_ADVICE_REQUEST_ATTESTATION_SCHEMA_VERSION,
                "model_call_id": call_id,
                "stage": stage,
                "request_sha256": hashlib.sha256(encoded).hexdigest(),
                "request_utf8_byte_count": len(encoded),
                "candidate_text_policy": (
                    "exact_frozen_sidecar_only"
                    if sidecar_occurrences == 1
                    else "candidate_and_source_text_absent"
                ),
                "status": "verified_before_provider_submission",
            }
        )

    return observe, attestations


def _validate_model_request_attestations(
    *,
    trial: Mapping[str, Any],
    model_call_trace: Sequence[Mapping[str, Any]],
    exposures: Sequence[Mapping[str, Any]],
    attestations: Sequence[Mapping[str, Any]],
) -> tuple[bool, list[str]]:
    trace_ids = [_text(call.get("call_id"), max_chars=500) for call in model_call_trace]
    attested_ids = [
        _text(item.get("model_call_id"), max_chars=500) for item in attestations
    ]
    reasons: list[str] = []
    if (
        not trace_ids
        or any(not call_id for call_id in trace_ids)
        or len(set(trace_ids)) != len(trace_ids)
        or trace_ids != attested_ids
    ):
        reasons.append("model_request_attestation_coverage_mismatch")
    elif any(
        call.get("stage") != attestation.get("stage")
        for call, attestation in zip(model_call_trace, attestations, strict=True)
    ):
        reasons.append("model_request_attestation_stage_mismatch")
    submitted_ids = {
        _text(call.get("call_id"), max_chars=500)
        for call in model_call_trace
        if call.get("provider_request_sent") is True
    }
    if not submitted_ids.issubset(set(attested_ids)):
        reasons.append("provider_submission_not_attested")
    sidecar_ids = {
        _text(item.get("model_call_id"), max_chars=500)
        for item in attestations
        if item.get("candidate_text_policy") == "exact_frozen_sidecar_only"
    }
    if trial["arm"] == "A" and sidecar_ids:
        reasons.append("control_request_contains_sidecar")
    if trial["arm"] == "B":
        visible_ids: set[str] = set()
        if len(exposures) == 1:
            visible_ids = {
                _text(item, max_chars=500)
                for item in exposures[0].get("model_visible_call_ids") or []
            }
        if not sidecar_ids or sidecar_ids != visible_ids:
            reasons.append("sidecar_request_exposure_mismatch")
    return not reasons, reasons


def _validate_execution_plan(
    manifest: Mapping[str, Any],
    supplied_plan: Mapping[str, Any] | None,
) -> dict[str, Any]:
    canonical = build_learning_advice_experiment_plan(manifest)
    if supplied_plan is not None and (
        not isinstance(supplied_plan, Mapping) or dict(supplied_plan) != canonical
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_plan_mismatch",
            "The supplied execution plan differs from the canonical seeded plan",
        )

    trials = canonical.get("trials")
    repeats = int(manifest["repeats"])
    expected_trial_count = LEARNING_ADVICE_EXPERIMENT_CASE_COUNT * repeats * 2
    expected_pair_count = LEARNING_ADVICE_EXPERIMENT_CASE_COUNT * repeats
    if (
        not isinstance(trials, list)
        or canonical.get("trial_count") != expected_trial_count
        or len(trials) != expected_trial_count
        or canonical.get("pair_count") != expected_pair_count
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_plan_invalid",
            "The execution plan has an invalid trial or pair count",
        )

    trial_ids = [_text(item.get("trial_id")) for item in trials]
    if not all(trial_ids) or len(set(trial_ids)) != len(trial_ids):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_plan_invalid",
            "Every planned trial must have a unique identity",
        )

    pairs: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    placeholders: list[str] = []
    for trial in trials:
        pair_id = _text(trial.get("pair_id"))
        pairs[pair_id].append(trial)
        raw_placeholders = trial.get("fresh_identity_placeholders")
        if not isinstance(raw_placeholders, Mapping):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_plan_invalid",
                "Every trial must require fresh request, session, and turn identities",
            )
        placeholders.extend(_text(value) for value in raw_placeholders.values())
    if (
        len(pairs) != expected_pair_count
        or not all(placeholders)
        or len(placeholders) != expected_trial_count * 3
        or len(set(placeholders)) != len(placeholders)
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_plan_invalid",
            "The plan does not isolate every paired trial with unique placeholders",
        )

    first_arm_counts: Counter[str] = Counter()
    for pair in pairs.values():
        if (
            len(pair) != 2
            or {item.get("arm") for item in pair} != {"A", "B"}
            or len({item.get("case_id") for item in pair}) != 1
            or len({item.get("repeat") for item in pair}) != 1
            or [item.get("pair_position") for item in pair] != [1, 2]
        ):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_plan_invalid",
                "Each case repeat must be one adjacent paired A/B comparison",
            )
        first_arm_counts[str(pair[0]["arm"])] += 1
    if first_arm_counts != {
        "A": expected_pair_count // 2,
        "B": expected_pair_count // 2,
    }:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_plan_invalid",
            "The paired order is not balanced between A-first and B-first trials",
        )
    return canonical


def _attest_fresh_bound_experiment_run(
    *,
    loader: Callable[[str], Mapping[str, Any] | None],
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        state = loader(str(manifest["experiment_run_id"]))
    except Exception as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_run_preflight_unavailable",
            "The frozen experiment run could not be canonically read before execution",
        ) from exc
    scope = manifest["trusted_scope"]
    expected_scope = {
        "run_id": manifest["experiment_run_id"],
        "experiment_spec_id": manifest["experiment_spec_id"],
        "namespace": scope["namespace"],
        "user_id": scope["actor_user_id"],
        "org_id": scope["organisation_concept_id"],
    }
    if not isinstance(state, Mapping) or any(
        state.get(field) != expected for field, expected in expected_scope.items()
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_run_preflight_scope_mismatch",
            "The canonical experiment run is absent or outside the frozen scope",
        )
    metadata = state.get("metadata")
    expected_binding = build_learning_advice_experiment_run_binding(
        manifest,
        plan=plan,
    )
    try:
        observed_binding = validate_learning_advice_experiment_run_binding(
            metadata.get("learning_advice_experiment")
            if isinstance(metadata, Mapping)
            else None
        )
    except LearningAdviceExperimentError as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_run_preflight_invalid",
            "The experiment run has no valid inspectable manifest and plan binding",
        ) from exc
    if (
        state.get("status") != "running"
        or state.get("verdict") is not None
        or list(state.get("observations") or [])
        or list(state.get("turn_execution_request_ids") or [])
        or observed_binding != expected_binding
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_run_preflight_invalid",
            "The experiment requires a fresh running run bound to this manifest and plan",
        )
    return {
        "status": "canonically_attested_fresh_and_bound",
        "run_id": manifest["experiment_run_id"],
        "binding_sha256": expected_binding["binding_sha256"],
    }


def _tool_trace(tool_invocations: Any) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for invocation in _mapping_sequence(tool_invocations):
        tool_name = _text(
            invocation.get("tool")
            or invocation.get("tool_name")
            or invocation.get("name"),
            max_chars=300,
        )
        status = _text(
            invocation.get("status")
            or invocation.get("effect_status")
            or invocation.get("terminal_status"),
            max_chars=100,
        )
        error_code = _text(
            invocation.get("error_code") or invocation.get("failure_code"),
            max_chars=200,
        )
        item: dict[str, Any] = {
            "tool_name": tool_name or None,
            "status": status or None,
        }
        if error_code:
            item["error_code"] = error_code
        for field in ("changed", "semantic_effect"):
            if isinstance(invocation.get(field), bool):
                item[field] = invocation[field]
        trace.append(item)
    return trace


def _bounded_tool_results(tool_invocations: Any) -> list[dict[str, Any]]:
    """Project model-visible outcome evidence for evaluation, not persistence.

    In particular, ``turn_read_evidence`` returns the slice the acting model
    saw in ``content``.  Omitting that field leaves the blind evaluator unable
    to distinguish a grounded answer from a fabrication even though the
    acting model had the evidence.  The projection remains bounded and is not
    copied into the persisted experiment observation.
    """

    results: list[dict[str, Any]] = []
    for invocation in _mapping_sequence(tool_invocations)[:40]:
        evidence = invocation.get("evidence")
        if not isinstance(evidence, Mapping):
            evidence = invocation.get("effective_payload")
        evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
        bounded_evidence: dict[str, Any] = {}
        for field in (
            "success",
            "status",
            "error_code",
            "failure_code",
            "message",
            "summary",
            "preview",
            "preview_truncated",
            "sha256",
            "source_sha256",
            "content",
            "content_format",
            "selected_value_kind",
            "returned_chars",
            "total_chars",
            "has_more",
            "count",
            "total",
        ):
            value = evidence.get(field)
            if isinstance(value, str):
                bounded_evidence[field] = value[:4_000]
            elif isinstance(value, (int, float, bool)) and not (
                isinstance(value, float) and not math.isfinite(value)
            ):
                bounded_evidence[field] = value
        results.append(
            {
                "tool_name": _text(
                    invocation.get("tool")
                    or invocation.get("tool_name")
                    or invocation.get("name"),
                    max_chars=300,
                )
                or None,
                "status": _text(
                    invocation.get("status")
                    or invocation.get("effect_status")
                    or invocation.get("terminal_status"),
                    max_chars=100,
                )
                or None,
                "evidence": bounded_evidence,
            }
        )
    return results


def _model_call_trace(llm_calls: Any) -> tuple[list[dict[str, Any]], int | None]:
    if not isinstance(llm_calls, Sequence) or isinstance(
        llm_calls, (str, bytes, bytearray)
    ):
        return [], None
    trace: list[dict[str, Any]] = []
    for call in llm_calls:
        if not isinstance(call, Mapping):
            continue
        item = {
            field: copy.deepcopy(call[field])
            for field in (
                "call_id",
                "stage",
                "provider",
                "requested_model",
                "selected_model",
                "effective_model",
                "provider_observed_model",
                "provider_request_sent",
                "success",
                "duration_ms",
            )
            if field in call
            and isinstance(call[field], (str, int, float, bool, type(None)))
        }
        trace.append(item)
    return trace, len(trace)


def _compact_persistence_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"acknowledged": False, "result_type": type(value).__name__}
    compact = {
        field: copy.deepcopy(value[field])
        for field in (
            "success",
            "updated",
            "inserted",
            "matched",
            "request_id",
            "run_id",
            "reason",
            "error",
        )
        if field in value
        and isinstance(value[field], (str, int, float, bool, type(None)))
    }
    compact["acknowledged"] = bool(
        value.get("success") is True
        or value.get("updated") is True
        or value.get("inserted") is True
        or value.get("matched") is True
    )
    return compact


def _read_back_trial_turn_execution_record(
    *,
    loader: Callable[..., Mapping[str, Any] | None],
    request_id: str,
    session_id: str,
    scope: Mapping[str, str],
    prompt_sha256: str,
    response_sha256: str,
    diagnostic: Mapping[str, Any],
) -> dict[str, Any]:
    """Read and bind the exact persisted TER before using its observation."""

    try:
        record = loader(request_id=request_id, namespace=scope["namespace"])
    except Exception as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_ter_readback_failed",
            "The canonical turn execution record could not be read back",
        ) from exc
    expected_scope = {
        "request_id": request_id,
        "session_id": session_id,
        "namespace": scope["namespace"],
        "user_id": scope["actor_user_id"],
        "org_id": scope["organisation_concept_id"],
    }
    prompt = record.get("prompt") if isinstance(record, Mapping) else None
    final_response = (
        record.get("final_response") if isinstance(record, Mapping) else None
    )
    aux_calls = record.get("aux_llm_calls") if isinstance(record, Mapping) else None
    bindings = [
        item
        for item in aux_calls or []
        if isinstance(item, Mapping) and item.get("type") == _TRIAL_TER_BINDING_AUX_TYPE
    ]
    expected_binding = {"type": _TRIAL_TER_BINDING_AUX_TYPE, **dict(diagnostic)}
    if (
        not isinstance(record, Mapping)
        or record.get("schema_version") != TURN_EXECUTION_RECORD_SCHEMA_VERSION
        or any(
            record.get(field) != expected for field, expected in expected_scope.items()
        )
        or not isinstance(prompt, Mapping)
        or prompt.get("sha256") != prompt_sha256
        or not isinstance(final_response, Mapping)
        or final_response.get("response_sha256") != response_sha256
        or len(bindings) != 1
        or _canonical_bytes(dict(bindings[0])) != _canonical_bytes(expected_binding)
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_ter_readback_mismatch",
            "The canonical turn execution record differs from the exact trial evidence",
        )
    return {
        "status": "canonically_read_back",
        "request_id": request_id,
        "turn_execution_record_sha256": turn_execution_record_evidence_sha256(record),
    }


def _redact_candidate_identity(value: Any, *, candidate_ref: Mapping[str, Any]) -> Any:
    identity_strings = [
        str(item) for item in candidate_ref.values() if isinstance(item, str) and item
    ]
    if isinstance(value, str):
        redacted = value
        for identity in identity_strings:
            redacted = redacted.replace(identity, "[candidate-identity-redacted]")
        return redacted
    if isinstance(value, Mapping):
        return {
            str(key): _redact_candidate_identity(item, candidate_ref=candidate_ref)
            for key, item in value.items()
            if str(key) not in _BLIND_EVALUATION_IDENTITY_FIELDS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _redact_candidate_identity(item, candidate_ref=candidate_ref)
            for item in value
        ]
    return copy.deepcopy(value)


def _blind_evaluation_input(
    *,
    trial: Mapping[str, Any],
    response_text: str,
    terminal_status: str,
    tool_trace: Sequence[Mapping[str, Any]],
    bounded_tool_results: Sequence[Mapping[str, Any]],
    candidate_ref: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_version": LEARNING_ADVICE_BLIND_EVALUATION_INPUT_SCHEMA_VERSION,
        "prompt": trial["prompt"],
        "response": response_text,
        "terminal_status": terminal_status,
        "tool_trace": [dict(item) for item in tool_trace],
        "bounded_tool_results": [dict(item) for item in bounded_tool_results],
        "evaluation_contract": copy.deepcopy(trial["evaluation_contract"]),
    }
    redacted = _redact_candidate_identity(payload, candidate_ref=candidate_ref)
    if not isinstance(redacted, dict):  # pragma: no cover - structural invariant
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_evaluator_projection_invalid",
            "The blind evaluator projection is not an object",
        )
    identity_json = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
    leaked_key = next(
        (field for field in _BLIND_EVALUATION_IDENTITY_FIELDS if field in redacted),
        None,
    )
    leaked_value = next(
        (
            str(value)
            for value in candidate_ref.values()
            if isinstance(value, str) and value and str(value) in identity_json
        ),
        None,
    )
    if leaked_key is not None or leaked_value is not None:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_evaluator_blinding_failed",
            "The evaluator projection contains candidate or arm identity",
        )
    return redacted


def _invalid_evaluator_result(
    reason_code: str,
    *,
    output_sha256: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": LEARNING_ADVICE_BLIND_EVALUATION_RESULT_SCHEMA_VERSION,
        "status": "invalid",
        "passed": None,
        "verdict": None,
        "reason_code": reason_code,
    }
    if output_sha256 is not None:
        result["evaluator_output_sha256"] = output_sha256
    return result


def _normalise_evaluator_model_call_receipt(
    value: Any,
    *,
    model_config: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    expected_fields = {
        "schema_version",
        "call_id",
        "provider",
        "requested_model",
        "selected_model",
        "effective_model",
        "provider_observed_model",
        "provider_request_sent",
        "success",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        return None, "evaluator_model_call_receipt_required"
    call_id = _text(value.get("call_id"), max_chars=300)
    if not call_id or any(
        not (character.isalnum() or character in "_-.:/") for character in call_id
    ):
        return None, "evaluator_model_call_receipt_invalid"
    receipt = {
        "schema_version": value.get("schema_version"),
        "call_id": call_id,
        "provider": value.get("provider"),
        "requested_model": value.get("requested_model"),
        "selected_model": value.get("selected_model"),
        "effective_model": value.get("effective_model"),
        "provider_observed_model": value.get("provider_observed_model"),
        "provider_request_sent": value.get("provider_request_sent"),
        "success": value.get("success"),
    }
    if receipt["schema_version"] != (
        LEARNING_ADVICE_EVALUATOR_MODEL_CALL_RECEIPT_SCHEMA_VERSION
    ):
        return None, "evaluator_model_call_receipt_schema_invalid"
    model_fields = (
        "requested_model",
        "selected_model",
        "effective_model",
        "provider_observed_model",
    )
    if any(is_sol_family_model_id(receipt[field]) for field in model_fields):
        return None, "evaluator_sol_model_observed"
    if (
        receipt["provider"] != model_config["provider"]
        or any(receipt[field] != model_config["model_id"] for field in model_fields)
        or receipt["provider_request_sent"] is not True
        or receipt["success"] is not True
    ):
        return None, "evaluator_model_call_receipt_mismatch"
    return receipt, None


def _normalise_evaluator_result(
    value: Any,
    *,
    manifest: Mapping[str, Any],
    trial: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return _invalid_evaluator_result("evaluator_result_not_object")
    try:
        output_sha256 = _sha256(value)
    except LearningAdviceExperimentRunnerError:
        return _invalid_evaluator_result("evaluator_result_not_json")
    if value.get("schema_version") != (
        LEARNING_ADVICE_BLIND_EVALUATION_RESULT_SCHEMA_VERSION
    ):
        return _invalid_evaluator_result(
            "evaluator_result_schema_required",
            output_sha256=output_sha256,
        )
    verdict = _text(value.get("verdict"), max_chars=30).casefold()
    if verdict not in {"pass", "partial", "fail", "inconclusive"}:
        return _invalid_evaluator_result(
            "evaluator_typed_verdict_required",
            output_sha256=output_sha256,
        )
    dimensions: dict[str, str] = {}
    for dimension in ("capability_choice", "work_product"):
        dimension_verdict = _text(value.get(dimension), max_chars=30).casefold()
        if dimension_verdict not in {"pass", "partial", "fail", "inconclusive"}:
            return _invalid_evaluator_result(
                f"evaluator_{dimension}_verdict_required",
                output_sha256=output_sha256,
            )
        dimensions[dimension] = dimension_verdict
    if not isinstance(value.get("material_failure"), bool):
        return _invalid_evaluator_result(
            "evaluator_boolean_outcomes_required",
            output_sha256=output_sha256,
        )
    expected_verdict = derive_learning_advice_evaluator_verdict(
        capability_choice=dimensions["capability_choice"],
        work_product=dimensions["work_product"],
        material_failure=value["material_failure"],
    )
    if verdict != expected_verdict:
        return _invalid_evaluator_result(
            "evaluator_verdict_aggregation_mismatch",
            output_sha256=output_sha256,
        )
    reason_codes = value.get("reason_codes")
    if not isinstance(reason_codes, Sequence) or isinstance(
        reason_codes, (str, bytes, bytearray)
    ):
        return _invalid_evaluator_result(
            "evaluator_reason_codes_required",
            output_sha256=output_sha256,
        )
    model_config = build_learning_advice_evaluator_model_config(manifest)
    model_call_receipt, receipt_error = _normalise_evaluator_model_call_receipt(
        value.get("model_call_receipt"),
        model_config=model_config,
    )
    if model_call_receipt is None:
        return _invalid_evaluator_result(
            receipt_error or "evaluator_model_call_receipt_invalid",
            output_sha256=output_sha256,
        )
    provenance = build_learning_advice_evaluator_provenance_identity(
        manifest,
        evaluator_concept_id=trial["evaluator_concept_id"],
        evaluator_sha256=trial["evaluator_sha256"],
        rubric_concept_id=trial["rubric_concept_id"],
        rubric_sha256=trial["rubric_sha256"],
        model_call_receipt=model_call_receipt,
    )
    result: dict[str, Any] = {
        "schema_version": LEARNING_ADVICE_BLIND_EVALUATION_RESULT_SCHEMA_VERSION,
        "status": "completed",
        "passed": verdict == "pass",
        "verdict": verdict,
        "evaluator_output_sha256": output_sha256,
        **dimensions,
        "material_failure": value["material_failure"],
        # The evaluator sees one blinded trial and cannot compare the external
        # world across arms.  The runner has already re-attested the exact
        # frozen replay world before every trial, so availability drift is a
        # harness-owned fact rather than model-authored judgement.
        "external_availability_changed": False,
        "model_call_receipt": model_call_receipt,
        "evaluation_provenance": provenance,
    }
    score = value.get("score")
    if (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and math.isfinite(float(score))
    ):
        result["score"] = float(score)
    result["reason_codes"] = [
        code
        for item in reason_codes[:20]
        if (code := _text(item, max_chars=100))
        and all(char.isalnum() or char in "_-.:" for char in code)
    ]
    material_failure_codes = value.get("material_failure_codes")
    if isinstance(material_failure_codes, Sequence) and not isinstance(
        material_failure_codes, (str, bytes, bytearray)
    ):
        result["material_failure_codes"] = [
            code
            for item in material_failure_codes[:20]
            if (code := _text(item, max_chars=100))
            and all(char.isalnum() or char in "_-.:" for char in code)
        ]
    return result


def _validate_model_identity(
    *, manifest: Mapping[str, Any], model_call_trace: Sequence[Mapping[str, Any]]
) -> tuple[bool, list[str]]:
    if not model_call_trace:
        return False, ["model_call_trace_missing"]
    expected_provider = manifest["model"]["provider"]
    expected_model = manifest["model"]["model_id"]
    reasons: list[str] = []
    successful_call_seen = False
    for call in model_call_trace:
        call_id = _text(call.get("call_id"), max_chars=300) or "unknown"
        if call.get("provider") != expected_provider:
            reasons.append(f"provider_mismatch:{call_id}")
        for field in (
            "requested_model",
            "selected_model",
            "effective_model",
            "provider_observed_model",
        ):
            if is_sol_family_model_id(call.get(field)):
                reasons.append(f"sol_model_observed:{field}:{call_id}")
        for field in ("requested_model", "selected_model"):
            if call.get(field) != expected_model:
                reasons.append(f"{field}_mismatch:{call_id}")
        if call.get("success") is True:
            successful_call_seen = True
            if call.get("effective_model") != expected_model:
                reasons.append(f"effective_model_mismatch:{call_id}")
            if call.get("provider_observed_model") != expected_model:
                reasons.append(f"provider_observed_model_mismatch:{call_id}")
    if not successful_call_seen:
        reasons.append("successful_model_call_missing")
    return not reasons, reasons


def _attest_evaluation_authority(
    *,
    loader: Callable[..., Mapping[str, Any]],
    concept_id: str,
    expected_sha256: str,
    kind: str,
    scope: Mapping[str, str],
) -> tuple[dict[str, Any], str]:
    try:
        loaded = loader(
            concept_id,
            expected_sha256,
            actor_user_id=scope["actor_user_id"],
            organisation_concept_id=scope["organisation_concept_id"],
            namespace=scope["namespace"],
        )
    except Exception as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_evaluator_authority_unavailable",
            f"The represented {kind} could not be canonically re-attested",
        ) from exc
    if not isinstance(loaded, Mapping) or set(loaded) != {
        "concept_id",
        "content",
        "content_sha256",
    }:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_evaluator_authority_invalid",
            f"The represented {kind} loader returned no exact canonical content",
        )
    observed_concept_id = loaded.get("concept_id")
    content = loaded.get("content")
    observed_sha256 = loaded.get("content_sha256")
    if (
        observed_concept_id != concept_id
        or not isinstance(content, str)
        or not content.strip()
        or len(content) > 200_000
        or observed_sha256 != expected_sha256
        or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_sha256
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_evaluator_authority_drift",
            f"The represented {kind} bytes differ from their frozen identity",
        )
    return (
        {
            "kind": kind,
            "concept_id": concept_id,
            "sha256": expected_sha256,
            "utf8_byte_count": len(content.encode("utf-8")),
            "status": "canonical_bytes_attested",
        },
        content,
    )


def _preflight_evaluation_authorities(
    *,
    manifest: Mapping[str, Any],
    loader: Callable[..., Mapping[str, Any]],
    scope: Mapping[str, str],
    learning_fragments: Sequence[str],
) -> None:
    """Reject evaluator or rubric bytes learned from the candidate/source."""

    identities: dict[tuple[str, str], str] = {}
    ordered: list[tuple[str, str, str]] = []
    for case in manifest["cases"]:
        for kind in ("evaluator", "rubric"):
            concept_id = str(case[f"{kind}_concept_id"])
            digest = str(case[f"{kind}_sha256"])
            identity_key = (kind, concept_id)
            prior_digest = identities.get(identity_key)
            if prior_digest is not None and prior_digest != digest:
                raise LearningAdviceExperimentRunnerError(
                    "learning_advice_experiment_evaluator_authority_inconsistent",
                    f"One represented {kind} has more than one frozen revision",
                )
            identities[identity_key] = digest
            item = (kind, concept_id, digest)
            if item not in ordered:
                ordered.append(item)
    for kind, concept_id, digest in ordered:
        _attestation, content = _attest_evaluation_authority(
            loader=loader,
            concept_id=concept_id,
            expected_sha256=digest,
            kind=kind,
            scope=scope,
        )
        contamination_path = _learning_text_echo_path(
            learning_fragments=learning_fragments,
            outcome_projection={f"{kind}_content": content},
        )
        if contamination_path is not None:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_evaluator_authority_contaminated",
                "Frozen candidate or source text contaminates evaluator authority",
            )


def _attest_runtime_snapshot(
    *,
    loader: Callable[..., Mapping[str, Any]],
    expected: Mapping[str, Any],
    scope: Mapping[str, str],
) -> dict[str, Any]:
    """Re-read the independently produced held-constant runtime identity."""

    try:
        loaded = loader(
            actor_user_id=scope["actor_user_id"],
            organisation_concept_id=scope["organisation_concept_id"],
            namespace=scope["namespace"],
        )
    except Exception as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_runtime_snapshot_unavailable",
            "The frozen experiment runtime could not be canonically re-attested",
        ) from exc
    if not isinstance(loaded, Mapping):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_runtime_snapshot_invalid",
            "The runtime snapshot loader returned no inspectable projection",
        )
    observed = copy.deepcopy(dict(loaded))
    if _canonical_bytes(observed) != _canonical_bytes(expected):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_runtime_snapshot_drift",
            "The acting runtime differs from the frozen experiment snapshot",
        )
    return {
        "status": "canonically_attested",
        "runtime_snapshot_sha256": _sha256(expected),
    }


def _validate_exposure(
    *,
    trial: Mapping[str, Any],
    manifest: Mapping[str, Any],
    exposures: Sequence[Mapping[str, Any]],
) -> tuple[bool, list[str]]:
    if len(exposures) != 1:
        return False, ["exactly_one_learning_advice_exposure_required"]
    exposure = exposures[0]
    expected_ref = manifest["candidate_ref"]
    reasons: list[str] = []
    expected = {
        "arm": trial["arm"],
        "experiment_id": manifest["experiment_run_id"],
        "case_id": trial["case_id"],
        "candidate_id": expected_ref["candidate_id"],
        "candidate_revision": expected_ref["revision"],
        "candidate_body_sha256": expected_ref["body_sha256"],
        "candidate_revision_identity_sha256": expected_ref["revision_identity_sha256"],
        "candidate_source_locator_sha256": expected_ref["source_locator_sha256"],
    }
    for field, expected_value in expected.items():
        if exposure.get(field) != expected_value:
            reasons.append(f"{field}_mismatch")
    if trial["arm"] == "A":
        if exposure.get("exposure_status") != "withheld":
            reasons.append("control_not_withheld")
        if exposure.get("model_visible") is not False:
            reasons.append("control_became_model_visible")
    else:
        if exposure.get("exposure_status") != "exposed":
            reasons.append("sidecar_not_exposed")
        if exposure.get("model_visible") is not True:
            reasons.append("sidecar_not_model_visible")
        if not exposure.get("model_visible_call_ids"):
            reasons.append("sidecar_has_no_visible_model_call")
    return not reasons, reasons


def _assert_body_free_observation(observation: Mapping[str, Any]) -> None:
    def visit(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key)
                if key_text.casefold() in _OBSERVATION_FORBIDDEN_KEYS:
                    raise LearningAdviceExperimentRunnerError(
                        "learning_advice_experiment_observation_contains_text",
                        f"The body-free observation contains {path}.{key_text}",
                    )
                visit(item, f"{path}.{key_text}")
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")

    visit(observation, "observation")
    _canonical_bytes(observation)


def _trial_persistence_envelope(observation: Mapping[str, Any]) -> dict[str, Any]:
    evaluation = observation.get("evaluation")
    verdict = (
        evaluation.get("verdict")
        if isinstance(evaluation, Mapping)
        and evaluation.get("verdict") in {"pass", "partial", "fail"}
        else "inconclusive"
    )
    trial_id = str(observation["trial_id"])
    envelope = {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_OBSERVATION_SCHEMA_VERSION,
        "observation_id": (
            f"{observation['experiment_run_id']}:learning-advice-trial:{trial_id}"
        ),
        "observation_type": "learning_advice_trial",
        "label": f"Learning-advice paired trial {trial_id}",
        "verdict": verdict,
        "observed_outcome": copy.deepcopy(observation["observed_outcome"]),
        "evidence": {"learning_advice_trial": copy.deepcopy(dict(observation))},
        "metrics": {
            "runner_elapsed_ms": observation["execution"]["runner_elapsed_ms"],
            "reported_duration_ms": observation["execution"]["reported_duration_ms"],
            "model_call_count": observation["execution"]["model_call_count"],
        },
        "execution_provenance": {
            "manifest_sha256": observation["manifest_sha256"],
            "plan_sha256": observation["plan_sha256"],
            "trial_id": trial_id,
            "arm": observation["arm"],
        },
        "turn_execution_request_ids": copy.deepcopy(
            observation["turn_execution_request_ids"]
        ),
    }
    _assert_body_free_observation(envelope)
    return envelope


def _final_persistence_envelope(
    result_evidence: Mapping[str, Any],
    *,
    turn_execution_request_ids: Sequence[str],
) -> dict[str, Any]:
    decision = result_evidence["content_decision"]["decision"]
    verdict = {
        "arm_b_content_win": "pass",
        "arm_b_not_supported": "fail",
    }.get(decision, "inconclusive")
    result_digest = result_evidence["result_evidence_sha256"]
    envelope = {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_OBSERVATION_SCHEMA_VERSION,
        "observation_id": (
            f"{result_evidence['experiment_run_id']}:learning-advice-result:"
            f"{str(result_digest)[:24]}"
        ),
        "observation_type": "learning_advice_experiment_result",
        "label": "Frozen learning-advice paired result",
        "verdict": verdict,
        "observed_outcome": {
            "decision": decision,
            "result_evidence_sha256": result_digest,
            "activation_authorised": False,
        },
        "evidence": {
            "learning_advice_result": copy.deepcopy(dict(result_evidence)),
        },
        "metrics": copy.deepcopy(result_evidence["secondary_metrics"]),
        "execution_provenance": {
            "manifest_sha256": result_evidence["manifest_sha256"],
            "plan_sha256": result_evidence["plan_sha256"],
            "acting_support_sha256": result_evidence["acting_support_identity"][
                "acting_support_sha256"
            ],
        },
        "turn_execution_request_ids": copy.deepcopy(list(turn_execution_request_ids)),
    }
    _assert_body_free_observation(envelope)
    return envelope


def _canonical_result_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(payload))
    result.pop("result_evidence_sha256", None)
    result["result_evidence_sha256"] = _sha256(result)
    return result


def _finalise_experiment_run(
    *,
    finaliser: Callable[..., Mapping[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    """Invoke canonical generic terminalisation without reusing its verdict."""

    try:
        result = finaliser(run_id=run_id)
    except Exception as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_run_finalisation_failed",
            "The canonical experiment run could not be terminalised",
        ) from exc
    if not isinstance(result, Mapping) or result.get("success") is not True:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_run_finalisation_failed",
            "The canonical experiment finaliser did not acknowledge terminalisation",
        )
    return {
        "status": "canonical_finaliser_acknowledged",
        "run_id": run_id,
        "generic_verdict": result.get("verdict"),
    }


def _read_back_experiment_result(
    *,
    loader: Callable[[str], Mapping[str, Any] | None],
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
    trial_observations: Sequence[Mapping[str, Any]],
    result_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        state = loader(str(manifest["experiment_run_id"]))
    except Exception as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_result_readback_unavailable",
            "The persisted experiment result could not be canonically read back",
        ) from exc
    scope = manifest["trusted_scope"]
    expected_scope = {
        "run_id": manifest["experiment_run_id"],
        "experiment_spec_id": manifest["experiment_spec_id"],
        "namespace": scope["namespace"],
        "user_id": scope["actor_user_id"],
        "org_id": scope["organisation_concept_id"],
    }
    if not isinstance(state, Mapping) or any(
        state.get(field) != expected for field, expected in expected_scope.items()
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_result_readback_scope_mismatch",
            "The canonical experiment run is absent or outside the frozen scope",
        )
    terminal_status = state.get("status")
    generic_verdict = state.get("verdict")
    completed_at = _text(state.get("completed_at_utc"), max_chars=100)
    if (
        terminal_status not in _TERMINAL_EXPERIMENT_RUN_STATUSES
        or not completed_at
        or (terminal_status == "failed" and generic_verdict != "fail")
        or (
            terminal_status == "completed"
            and generic_verdict not in {"pass", "partial", "inconclusive"}
        )
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_run_not_terminal",
            "The canonical experiment run did not read back in a valid terminal state",
        )
    metadata = state.get("metadata")
    try:
        observed_binding = validate_learning_advice_experiment_run_binding(
            metadata.get("learning_advice_experiment")
            if isinstance(metadata, Mapping)
            else None
        )
    except LearningAdviceExperimentError as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_run_binding_mismatch",
            "The terminal experiment run has no valid inspectable binding",
        ) from exc
    if observed_binding != build_learning_advice_experiment_run_binding(
        manifest, plan=plan
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_run_binding_mismatch",
            "The terminal experiment run no longer matches its frozen binding",
        )
    raw_observations = state.get("observations")
    if not isinstance(raw_observations, Sequence) or isinstance(
        raw_observations, (str, bytes, bytearray)
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_result_readback_invalid",
            "The canonical experiment run has no observations",
        )
    if not all(isinstance(item, Mapping) for item in raw_observations):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_result_readback_invalid",
            "The canonical experiment run contains a malformed observation",
        )
    observations = list(raw_observations)
    expected_by_id = {str(item["trial_id"]): item for item in trial_observations}
    if len(observations) != len(expected_by_id) + 1:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_foreign_observation",
            "The frozen run contains foreign, missing, or duplicate observations",
        )
    trial_rows: list[tuple[str, str, Mapping[str, Any]]] = []
    persisted_results: list[tuple[str, Mapping[str, Any]]] = []
    for item in observations:
        evidence = item.get("evidence")
        if not isinstance(evidence, Mapping):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_foreign_observation",
                "The frozen run contains an observation outside the experiment contract",
            )
        trial = evidence.get("learning_advice_trial")
        observation_id = item.get("observation_id")
        if (
            item.get("observation_type") == "learning_advice_trial"
            and isinstance(observation_id, str)
            and isinstance(trial, Mapping)
            and isinstance(trial.get("trial_id"), str)
        ):
            trial_rows.append((str(trial["trial_id"]), observation_id, trial))
            continue
        persisted_result = evidence.get("learning_advice_result")
        if (
            item.get("observation_type") == "learning_advice_experiment_result"
            and isinstance(observation_id, str)
            and isinstance(persisted_result, Mapping)
        ):
            persisted_results.append((observation_id, persisted_result))
            continue
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_foreign_observation",
            "The frozen run contains an observation outside the experiment contract",
        )
    trial_ids = [trial_id for trial_id, _observation_id, _trial in trial_rows]
    observation_ids = [
        observation_id for _trial_id, observation_id, _trial in trial_rows
    ]
    trial_by_id = {trial_id: trial for trial_id, _observation_id, trial in trial_rows}
    if (
        len(trial_rows) != len(expected_by_id)
        or len(set(trial_ids)) != len(trial_ids)
        or len(set(observation_ids)) != len(observation_ids)
        or set(expected_by_id) != set(trial_by_id)
        or set(observation_ids) != set(result_evidence["trial_observation_ids"])
        or any(
            _canonical_bytes(trial_by_id[trial_id]) != _canonical_bytes(expected)
            for trial_id, expected in expected_by_id.items()
        )
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_trial_readback_mismatch",
            "The canonical run does not contain every exact paired-trial observation",
        )
    matching_results = [
        (observation_id, item)
        for observation_id, item in persisted_results
        if item.get("result_evidence_sha256")
        == result_evidence["result_evidence_sha256"]
    ]
    expected_final_id = (
        f"{manifest['experiment_run_id']}:learning-advice-result:"
        f"{result_evidence['result_evidence_sha256'][:24]}"
    )
    if (
        len(persisted_results) != 1
        or len(matching_results) != 1
        or matching_results[0][0] != expected_final_id
        or _canonical_bytes(matching_results[0][1]) != _canonical_bytes(result_evidence)
    ):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_result_readback_mismatch",
            "The canonical run does not contain the exact final paired result",
        )
    digest_payload = copy.deepcopy(dict(matching_results[0][1]))
    supplied_digest = digest_payload.pop("result_evidence_sha256", None)
    if supplied_digest != _sha256(digest_payload):
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_result_digest_mismatch",
            "The canonical final result does not match its evidence digest",
        )
    expected_request_ids = [
        str(item["execution_identity"]["request_id"]) for item in trial_observations
    ]
    if list(state.get("turn_execution_request_ids") or []) != expected_request_ids:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_request_index_mismatch",
            "The terminal run does not index every exact trial execution record",
        )
    return {
        "status": "canonically_terminal_and_read_back",
        "run_id": manifest["experiment_run_id"],
        "trial_observation_count": len(expected_by_id),
        "result_observation_count": 1,
        "result_evidence_sha256": supplied_digest,
        "terminal_status": terminal_status,
        "generic_verdict": generic_verdict,
        "completed_at_utc": completed_at,
    }


def _record_and_verify_candidate_disposition(
    *,
    manifest: Mapping[str, Any],
    result_evidence: Mapping[str, Any],
    recorder: Callable[..., Mapping[str, Any]],
    loader: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    decision = result_evidence["content_decision"]["decision"]
    expected_disposition = {
        "arm_b_content_win": "retained",
        "arm_b_not_supported": "rejected",
    }.get(decision)
    scope = manifest["trusted_scope"]
    candidate_ref = manifest["candidate_ref"]
    disposition_request_id: str | None = None
    if expected_disposition is not None:
        disposition_request_id = (
            f"learning-advice-disposition:{manifest['experiment_run_id']}:"
            f"{result_evidence['result_evidence_sha256'][:32]}"
        )
        try:
            recorded = recorder(
                candidate_ref["candidate_id"],
                experiment_run_id=manifest["experiment_run_id"],
                disposition_request_id=disposition_request_id,
                actor_user_id=scope["actor_user_id"],
                organisation_concept_id=scope["organisation_concept_id"],
                namespace=scope["namespace"],
            )
        except Exception as exc:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_disposition_not_recorded",
                "The decisive canonical result could not update candidate disposition",
            ) from exc
        if (
            not isinstance(recorded, Mapping)
            or recorded.get("candidate_id") != candidate_ref["candidate_id"]
            or recorded.get("evaluation_disposition") != expected_disposition
            or not (
                recorded.get("disposition_recorded") is True
                or recorded.get("idempotent") is True
            )
        ):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_disposition_not_recorded",
                "The candidate disposition recorder did not acknowledge the derived result",
            )
    else:
        expected_disposition = candidate_ref["evaluation_disposition"]

    try:
        candidate = loader(
            candidate_ref["candidate_id"],
            actor_user_id=scope["actor_user_id"],
            organisation_concept_id=scope["organisation_concept_id"],
            namespace=scope["namespace"],
        )
    except Exception as exc:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_disposition_readback_unavailable",
            "The candidate was not readable after the experiment disposition decision",
        ) from exc
    expected_after = {
        **candidate_ref,
        "evaluation_disposition": expected_disposition,
    }
    observed_after = {
        field: candidate.get(field) if isinstance(candidate, Mapping) else None
        for field in expected_after
    }
    if observed_after != expected_after:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_disposition_readback_mismatch",
            "The candidate disposition did not canonically read back as derived",
        )
    return {
        "status": (
            "canonically_recorded_and_read_back"
            if disposition_request_id is not None
            else "unchanged_after_inconclusive_result"
        ),
        "decision": decision,
        "evaluation_disposition": expected_disposition,
        "disposition_request_id": disposition_request_id,
        "experiment_run_id": manifest["experiment_run_id"],
        "evidence_sha256": result_evidence["result_evidence_sha256"],
        "candidate_ref": expected_after,
        "activation_authorised": False,
    }


def _mean(values: Sequence[float]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def _median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _arm_metrics(
    observations: Sequence[Mapping[str, Any]], *, arm: str
) -> dict[str, Any]:
    selected = [item for item in observations if item.get("arm") == arm]
    completed_evaluations = [
        item
        for item in selected
        if isinstance(item.get("evaluation"), Mapping)
        and isinstance(item["evaluation"].get("passed"), bool)
    ]
    passes = sum(item["evaluation"]["passed"] is True for item in completed_evaluations)
    runner_latencies = [
        float(item["execution"]["runner_elapsed_ms"])
        for item in selected
        if isinstance(item.get("execution"), Mapping)
        and isinstance(item["execution"].get("runner_elapsed_ms"), (int, float))
    ]
    reported_latencies = [
        float(item["execution"]["reported_duration_ms"])
        for item in selected
        if isinstance(item.get("execution"), Mapping)
        and isinstance(item["execution"].get("reported_duration_ms"), (int, float))
    ]
    model_call_counts = [
        int(item["execution"]["model_call_count"])
        for item in selected
        if isinstance(item.get("execution"), Mapping)
        and isinstance(item["execution"].get("model_call_count"), int)
    ]
    return {
        "trial_count": len(selected),
        "evaluated_trial_count": len(completed_evaluations),
        "pass_count": passes,
        "pass_rate": (
            passes / len(completed_evaluations) if completed_evaluations else None
        ),
        "runner_latency_ms": {
            "known_count": len(runner_latencies),
            "unknown_count": len(selected) - len(runner_latencies),
            "mean": _mean(runner_latencies),
            "median": _median(runner_latencies),
        },
        "reported_turn_latency_ms": {
            "known_count": len(reported_latencies),
            "unknown_count": len(selected) - len(reported_latencies),
            "mean": _mean(reported_latencies),
            "median": _median(reported_latencies),
        },
        "model_calls": {
            "known_count": len(model_call_counts),
            "unknown_count": len(selected) - len(model_call_counts),
            "total": sum(model_call_counts) if model_call_counts else None,
            "mean": _mean([float(value) for value in model_call_counts]),
        },
    }


def _safe_failure_reason_code(error: Exception) -> str:
    raw = getattr(error, "reason_code", None)
    reason_code = _text(raw, max_chars=200).casefold()
    if reason_code and all(
        char in "abcdefghijklmnopqrstuvwxyz0123456789_.:-" for char in reason_code
    ):
        return reason_code
    return "learning_advice_experiment_unexpected_failure"


def _abort_observation_envelope(
    *,
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    reason_code: str,
    error_class: str,
) -> dict[str, Any]:
    prior_observations = state.get("observations")
    prior_observation_count = (
        len(prior_observations)
        if isinstance(prior_observations, Sequence)
        and not isinstance(prior_observations, (str, bytes, bytearray))
        else 0
    )
    prior_request_ids = state.get("turn_execution_request_ids")
    prior_request_count = (
        len(prior_request_ids)
        if isinstance(prior_request_ids, Sequence)
        and not isinstance(prior_request_ids, (str, bytes, bytearray))
        else 0
    )
    binding = build_learning_advice_experiment_run_binding(manifest, plan=plan)
    observation = {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_ABORT_SCHEMA_VERSION,
        "observation_kind": "runner_abort",
        "experiment_spec_id": manifest["experiment_spec_id"],
        "experiment_run_id": manifest["experiment_run_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "binding_sha256": binding["binding_sha256"],
        "reason_code": reason_code,
        "error_class": error_class,
        "preserved_observation_count": prior_observation_count,
        "preserved_turn_execution_request_count": prior_request_count,
        "turn_execution_request_ids": [],
    }
    _assert_body_free_observation(observation)
    observation_digest = _sha256(observation)
    envelope = {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_OBSERVATION_SCHEMA_VERSION,
        "observation_id": (
            f"{manifest['experiment_run_id']}:learning-advice-abort:"
            f"{observation_digest[:24]}"
        ),
        "observation_type": "learning_advice_experiment_abort",
        "label": "Learning-advice experiment invalidated",
        "verdict": "inconclusive",
        "observed_outcome": {
            "status": "aborted",
            "reason_code": reason_code,
        },
        "evidence": {"learning_advice_abort": observation},
        "execution_provenance": {
            "manifest_sha256": manifest["manifest_sha256"],
            "plan_sha256": plan["plan_sha256"],
            "binding_sha256": binding["binding_sha256"],
        },
        "turn_execution_request_ids": [],
    }
    _assert_body_free_observation(envelope)
    return envelope


def _abort_and_reconcile_experiment_run(
    *,
    manifest: Mapping[str, Any],
    plan: Mapping[str, Any],
    error: Exception,
    observation_persister: Callable[..., Any],
    run_loader: Callable[[str], Mapping[str, Any] | None],
    abort_finaliser: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    reason_code = _safe_failure_reason_code(error)
    error_class = type(error).__name__
    binding = build_learning_advice_experiment_run_binding(manifest, plan=plan)
    recovery: dict[str, Any] = {
        "schema_version": LEARNING_ADVICE_EXPERIMENT_ABORT_RECOVERY_SCHEMA_VERSION,
        "status": "terminalisation_unconfirmed",
        "run_id": manifest["experiment_run_id"],
        "reason_code": reason_code,
        "error_class": error_class,
        "manifest_sha256": manifest["manifest_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "binding_sha256": binding["binding_sha256"],
        "abort_observation": {"status": "not_attempted"},
        "terminalisation": {"status": "not_attempted"},
    }

    try:
        state = run_loader(str(manifest["experiment_run_id"]))
    except Exception as read_exc:  # noqa: BLE001 - recovery must preserve the cause
        recovery["terminalisation"] = {
            "status": "unconfirmed",
            "failure_stage": "pre_abort_readback",
            "error_class": type(read_exc).__name__,
        }
        _assert_body_free_observation(recovery)
        return recovery

    scope = manifest["trusted_scope"]
    expected_scope = {
        "run_id": manifest["experiment_run_id"],
        "experiment_spec_id": manifest["experiment_spec_id"],
        "namespace": scope["namespace"],
        "user_id": scope["actor_user_id"],
        "org_id": scope["organisation_concept_id"],
    }
    metadata = state.get("metadata") if isinstance(state, Mapping) else None
    try:
        observed_binding = validate_learning_advice_experiment_run_binding(
            metadata.get("learning_advice_experiment")
            if isinstance(metadata, Mapping)
            else None
        )
    except LearningAdviceExperimentError:
        observed_binding = None
    if (
        not isinstance(state, Mapping)
        or any(
            state.get(field) != expected for field, expected in expected_scope.items()
        )
        or observed_binding != binding
    ):
        recovery["terminalisation"] = {
            "status": "unconfirmed",
            "failure_stage": "pre_abort_binding_attestation",
        }
        _assert_body_free_observation(recovery)
        return recovery

    if state.get("status") != "running":
        existing_abort = state.get("abort_summary")
        if (
            state.get("status") == "failed"
            and state.get("verdict") == "inconclusive"
            and isinstance(existing_abort, Mapping)
            and existing_abort.get("reason_code") == reason_code
            and existing_abort.get("binding_sha256") == binding["binding_sha256"]
            and _text(state.get("completed_at_utc"), max_chars=100)
        ):
            recovery.update(
                {
                    "status": "canonically_aborted_and_read_back",
                    "candidate_disposition_attempted": False,
                    "terminalisation": {
                        "status": "confirmed",
                        "terminal_status": "failed",
                        "verdict": "inconclusive",
                    },
                }
            )
        else:
            recovery["terminalisation"] = {
                "status": "terminal_state_preserved",
                "terminal_status": state.get("status"),
                "verdict": state.get("verdict"),
            }
        _assert_body_free_observation(recovery)
        return recovery

    recovery["candidate_disposition_attempted"] = False
    envelope = _abort_observation_envelope(
        manifest=manifest,
        plan=plan,
        state=state,
        reason_code=reason_code,
        error_class=error_class,
    )
    observation_persisted = False
    try:
        persistence_raw = observation_persister(
            run_id=manifest["experiment_run_id"],
            observations=envelope,
            turn_execution_request_ids=(),
        )
    except Exception as persistence_exc:  # noqa: BLE001
        recovery["abort_observation"] = {
            "status": "persistence_unconfirmed",
            "observation_id": envelope["observation_id"],
            "error_class": type(persistence_exc).__name__,
        }
    else:
        persistence = _compact_persistence_receipt(persistence_raw)
        observation_persisted = persistence.get("acknowledged") is True
        recovery["abort_observation"] = {
            "status": "persisted"
            if observation_persisted
            else "persistence_unconfirmed",
            "observation_id": envelope["observation_id"],
        }

    try:
        terminalisation_raw = abort_finaliser(
            run_id=manifest["experiment_run_id"],
            reason_code=reason_code,
            binding_metadata_key="learning_advice_experiment",
            expected_binding_sha256=binding["binding_sha256"],
        )
    except Exception as finalise_exc:  # noqa: BLE001
        recovery["terminalisation"] = {
            "status": "unconfirmed",
            "failure_stage": "abort_finaliser",
            "error_class": type(finalise_exc).__name__,
        }
        _assert_body_free_observation(recovery)
        return recovery
    if (
        not isinstance(terminalisation_raw, Mapping)
        or terminalisation_raw.get("success") is not True
    ):
        recovery["terminalisation"] = {
            "status": "unconfirmed",
            "failure_stage": "abort_finaliser_acknowledgement",
        }
        _assert_body_free_observation(recovery)
        return recovery

    try:
        readback = run_loader(str(manifest["experiment_run_id"]))
    except Exception as read_exc:  # noqa: BLE001
        recovery["terminalisation"] = {
            "status": "unconfirmed",
            "failure_stage": "terminal_readback",
            "error_class": type(read_exc).__name__,
        }
        _assert_body_free_observation(recovery)
        return recovery
    readback_metadata = (
        readback.get("metadata") if isinstance(readback, Mapping) else None
    )
    try:
        readback_binding = validate_learning_advice_experiment_run_binding(
            readback_metadata.get("learning_advice_experiment")
            if isinstance(readback_metadata, Mapping)
            else None
        )
    except LearningAdviceExperimentError:
        readback_binding = None
    abort_summary = (
        readback.get("abort_summary") if isinstance(readback, Mapping) else None
    )
    if (
        not isinstance(readback, Mapping)
        or any(
            readback.get(field) != expected
            for field, expected in expected_scope.items()
        )
        or readback_binding != binding
        or readback.get("status") != "failed"
        or readback.get("verdict") != "inconclusive"
        or not _text(readback.get("completed_at_utc"), max_chars=100)
        or not isinstance(abort_summary, Mapping)
        or abort_summary.get("reason_code") != reason_code
        or abort_summary.get("binding_sha256") != binding["binding_sha256"]
    ):
        recovery["terminalisation"] = {
            "status": "unconfirmed",
            "failure_stage": "terminal_readback_validation",
        }
        _assert_body_free_observation(recovery)
        return recovery

    if observation_persisted:
        persisted_abort_ids = [
            item.get("observation_id")
            for item in readback.get("observations") or []
            if isinstance(item, Mapping)
            and item.get("observation_type") == "learning_advice_experiment_abort"
        ]
        if envelope["observation_id"] not in persisted_abort_ids:
            recovery["abort_observation"]["status"] = "readback_unconfirmed"

    recovery.update(
        {
            "status": "canonically_aborted_and_read_back",
            "terminalisation": {
                "status": "confirmed",
                "terminal_status": "failed",
                "verdict": "inconclusive",
                "completed_at_utc": readback["completed_at_utc"],
            },
        }
    )
    _assert_body_free_observation(recovery)
    return recovery


def _attach_abort_recovery(error: Exception, recovery: Mapping[str, Any]) -> None:
    try:
        error.__dict__["experiment_run_id"] = recovery["run_id"]
        error.__dict__["recovery_evidence"] = copy.deepcopy(dict(recovery))
    except Exception:  # pragma: no cover - built-in exceptions used here are mutable
        logger.exception("Could not attach experiment abort recovery evidence")


def _run_learning_advice_experiment_implementation(
    manifest: Mapping[str, Any],
    *,
    gateway: Any,
    llm_client: Any,
    evaluate_blind_trial: Callable[..., Any],
    evaluator_authority_loader: Callable[..., Mapping[str, Any]],
    runtime_snapshot_loader: Callable[..., Mapping[str, Any]],
    gateway_snapshot_loader: Callable[..., Mapping[str, Any]],
    candidate_source_text_loader: Callable[..., Mapping[str, Any]],
    context: Sequence[Mapping[str, Any]] = (),
    trusted_argument_values: Mapping[str, Any] | None = None,
    workflow_launch_inputs: Mapping[str, Any] | None = None,
    model_registry_snapshot: Any = None,
    plan: Mapping[str, Any] | None = None,
    candidate_loader: Callable[..., Mapping[str, Any]] | None = None,
    candidate_disposition_recorder: Callable[..., Mapping[str, Any]] | None = None,
    candidate_disposition_loader: Callable[..., Mapping[str, Any]] | None = None,
    adaptive_turn_executor: Callable[..., Any] | None = None,
    turn_record_builder: Callable[..., Mapping[str, Any]] | None = None,
    turn_record_persister: Callable[..., Any] | None = None,
    turn_record_loader: Callable[..., Mapping[str, Any] | None] | None = None,
    experiment_observation_persister: Callable[..., Any] | None = None,
    experiment_run_loader: Callable[[str], Mapping[str, Any] | None] | None = None,
    experiment_run_finaliser: Callable[..., Mapping[str, Any]] | None = None,
    identity_factory: Callable[[str], str] | None = None,
    timestamp_factory: Callable[[], Any] | None = None,
    monotonic: Callable[[], float] = time.perf_counter,
    _preflight_attestation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute, persist, blindly evaluate, and pair one frozen A/B plan."""

    frozen = validate_learning_advice_experiment_manifest(manifest)
    canonical_plan = _validate_execution_plan(frozen, plan)
    executor = adaptive_turn_executor or execute_adaptive_turn
    ter_builder = turn_record_builder or build_turn_execution_record
    ter_persister = turn_record_persister or upsert_turn_execution_record_projection
    ter_loader = turn_record_loader or get_turn_execution_record_projection
    observation_persister = (
        experiment_observation_persister or record_experiment_observation
    )
    run_loader = experiment_run_loader or get_experiment_run_state
    run_finaliser = experiment_run_finaliser or compute_experiment_verdict
    disposition_recorder = (
        candidate_disposition_recorder or record_learning_candidate_disposition
    )
    disposition_loader = candidate_disposition_loader or candidate_loader
    disposition_loader = disposition_loader or get_learning_candidate
    allocate_identity = identity_factory or _default_identity_factory
    now = timestamp_factory or (lambda: datetime.now(UTC))
    used_identities: set[str] = set()
    scope = frozen["trusted_scope"]
    runtime_snapshot = frozen["runtime_snapshot"]
    frozen_inputs = {
        "context": copy.deepcopy(list(context)),
        "trusted_argument_values": copy.deepcopy(trusted_argument_values),
        "workflow_launch_inputs": copy.deepcopy(workflow_launch_inputs),
        "model_registry_snapshot": copy.deepcopy(model_registry_snapshot),
        "model_parameters": copy.deepcopy(frozen["model"]["parameters"]),
    }
    _canonical_bytes(frozen_inputs)
    run_preflight = (
        copy.deepcopy(dict(_preflight_attestation))
        if isinstance(_preflight_attestation, Mapping)
        else _attest_fresh_bound_experiment_run(
            loader=run_loader,
            manifest=frozen,
            plan=canonical_plan,
        )
    )

    preflight_candidate = load_frozen_learning_candidate(
        frozen,
        actor_user_id=scope["actor_user_id"],
        organisation_concept_id=scope["organisation_concept_id"],
        namespace=scope["namespace"],
        candidate_loader=candidate_loader,
    )
    source_texts = _load_candidate_source_texts(
        loader=candidate_source_text_loader,
        candidate=preflight_candidate,
        scope=scope,
    )
    first_gateway, acting_support_identity, model_visible_texts = (
        _prepare_acting_gateway(
            gateway=gateway,
            gateway_snapshot_loader=gateway_snapshot_loader,
            scope=scope,
            runtime_snapshot=runtime_snapshot,
            frozen_inputs=frozen_inputs,
        )
    )
    _assert_no_learning_text_contamination(
        candidate_body=str(preflight_candidate["body"]),
        source_texts=source_texts,
        acting_inputs={
            "case_prompts": [item["prompt"] for item in frozen["cases"]],
            **frozen_inputs,
            "read_only_capability_catalogue": first_gateway.describe_methods(),
            "replay_model_visible_texts": model_visible_texts,
        },
    )
    learning_fragments = _source_fragments(
        [str(preflight_candidate["body"]), *source_texts]
    )
    normalised_candidate_body = " ".join(
        str(preflight_candidate["body"]).split()
    ).casefold()
    if (
        normalised_candidate_body
        and normalised_candidate_body not in learning_fragments
    ):
        learning_fragments.insert(0, normalised_candidate_body)
    _preflight_evaluation_authorities(
        manifest=frozen,
        loader=evaluator_authority_loader,
        scope=scope,
        learning_fragments=learning_fragments,
    )
    del preflight_candidate, source_texts, model_visible_texts

    executions: list[dict[str, Any]] = []
    for trial_index, trial in enumerate(canonical_plan["trials"]):
        runtime_attestation = _attest_runtime_snapshot(
            loader=runtime_snapshot_loader,
            expected=runtime_snapshot,
            scope=scope,
        )
        if trial_index == 0:
            read_only_gateway = first_gateway
            trial_acting_support = acting_support_identity
        else:
            read_only_gateway, trial_acting_support, _visible_texts = (
                _prepare_acting_gateway(
                    gateway=gateway,
                    gateway_snapshot_loader=gateway_snapshot_loader,
                    scope=scope,
                    runtime_snapshot=runtime_snapshot,
                    frozen_inputs=frozen_inputs,
                )
            )
            del _visible_texts
        if trial_acting_support != acting_support_identity:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_acting_support_drift",
                "The acting inputs or replay world changed between paired trials",
            )
        request_id = _next_fresh_identity(
            kind="request", identity_factory=allocate_identity, used=used_identities
        )
        session_id = _next_fresh_identity(
            kind="session", identity_factory=allocate_identity, used=used_identities
        )
        turn_id = _next_fresh_identity(
            kind="turn", identity_factory=allocate_identity, used=used_identities
        )
        evaluation_id = _next_fresh_identity(
            kind="evaluation", identity_factory=allocate_identity, used=used_identities
        )
        bound_trial = bind_fresh_trial_identities(
            trial,
            request_id=request_id,
            session_id=session_id,
            turn_id=turn_id,
        )
        projection = build_learning_advice_projection_for_trial(
            frozen,
            trial,
            actor_user_id=scope["actor_user_id"],
            organisation_concept_id=scope["organisation_concept_id"],
            namespace=scope["namespace"],
            candidate_loader=candidate_loader,
        )

        model_request_observer, model_request_attestations = (
            _build_model_request_observer(
                trial=trial,
                projection=projection,
                manifest=frozen,
                learning_fragments=learning_fragments,
            )
        )

        execute_kwargs: dict[str, Any] = {
            "gateway": read_only_gateway,
            "prompt": trial["prompt"],
            "context": copy.deepcopy(frozen_inputs["context"]),
            "llm_client": llm_client,
            "model": frozen["model"]["model_id"],
            "model_parameters": copy.deepcopy(frozen_inputs["model_parameters"]),
            "user_namespace": scope["namespace"],
            "user_concept_id": scope["actor_user_id"],
            "org_concept_id": scope["organisation_concept_id"],
            "trusted_argument_values": copy.deepcopy(
                frozen_inputs["trusted_argument_values"]
            ),
            "workflow_launch_inputs": copy.deepcopy(
                frozen_inputs["workflow_launch_inputs"]
            ),
            "turn_id": turn_id,
            "conversation_id": session_id,
            "conversation_history_owner_user_id": scope["actor_user_id"],
            "conversation_history_namespace": scope["namespace"],
            "model_registry_snapshot": copy.deepcopy(
                frozen_inputs["model_registry_snapshot"]
            ),
            "learning_advice_projection": projection,
            "model_request_observer": model_request_observer,
            "allow_represented_workflow_discovery": (
                _ALLOW_REPRESENTED_WORKFLOW_DISCOVERY
            ),
            "allow_represented_tool_projection": _ALLOW_REPRESENTED_TOOL_PROJECTION,
            "turn_budget_seconds": runtime_snapshot["turn_budget_seconds"],
            "final_synthesis_reserve_seconds": runtime_snapshot[
                "final_synthesis_reserve_seconds"
            ],
            "final_answer_reserve_seconds": runtime_snapshot[
                "final_answer_reserve_seconds"
            ],
        }

        started = monotonic()
        execution_error: dict[str, Any] | None = None
        try:
            adaptive_result = executor(**execute_kwargs)
        except Exception as exc:
            if isinstance(exc, LearningAdviceExperimentRunnerError):
                raise
            adaptive_result = None
            execution_error = {
                "error_class": type(exc).__name__,
                "error_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
            }
        runner_elapsed_ms = max(0.0, (monotonic() - started) * 1000.0)
        blocked_effect_attempts = list(read_only_gateway.blocked_effect_attempts)

        response_text = _text(
            _field(adaptive_result, "response_text", ""), max_chars=200_000
        )
        terminal_status = (
            _text(
                _field(
                    adaptive_result,
                    "terminal_status",
                    "execution_exception" if execution_error else "unknown",
                ),
                max_chars=100,
            )
            or "unknown"
        )
        tool_invocations = _mapping_sequence(
            _field(adaptive_result, "tool_invocations", ())
        )
        aux_llm_calls = _mapping_sequence(_field(adaptive_result, "aux_llm_calls", ()))
        llm_calls = _mapping_sequence(_field(adaptive_result, "llm_calls", ()))
        tool_trace = _tool_trace(tool_invocations)
        bounded_tool_results = _bounded_tool_results(tool_invocations)
        model_call_trace, model_call_count = _model_call_trace(
            _field(adaptive_result, "llm_calls", None)
        )
        model_identity_valid, model_identity_reasons = _validate_model_identity(
            manifest=frozen,
            model_call_trace=model_call_trace,
        )
        reported_duration = _field(adaptive_result, "duration_ms", None)
        reported_duration_ms = (
            float(reported_duration)
            if isinstance(reported_duration, (int, float))
            and not isinstance(reported_duration, bool)
            and math.isfinite(float(reported_duration))
            and float(reported_duration) >= 0.0
            else None
        )
        exposures = extract_learning_advice_exposures(aux_llm_calls)
        exposure_valid, exposure_reasons = _validate_exposure(
            trial=trial,
            manifest=frozen,
            exposures=exposures,
        )
        request_attestation_valid, request_attestation_reasons = (
            _validate_model_request_attestations(
                trial=trial,
                model_call_trace=model_call_trace,
                exposures=exposures,
                attestations=model_request_attestations,
            )
        )

        diagnostic = {
            "schema_version": _TRIAL_EXECUTION_DIAGNOSTIC_SCHEMA_VERSION,
            "experiment_spec_id": frozen["experiment_spec_id"],
            "experiment_run_id": frozen["experiment_run_id"],
            "manifest_sha256": frozen["manifest_sha256"],
            "plan_sha256": canonical_plan["plan_sha256"],
            "trial_id": trial["trial_id"],
            "pair_id": trial["pair_id"],
            "case_id": trial["case_id"],
            "repeat": trial["repeat"],
            "arm": trial["arm"],
            "turn_id": turn_id,
            "runtime_attestation": copy.deepcopy(runtime_attestation),
            "acting_support_identity": copy.deepcopy(trial_acting_support),
            "read_only_effect_attempts_blocked": blocked_effect_attempts,
            "execution_error": execution_error,
            "exposure_valid": exposure_valid,
            "exposure_reason_codes": exposure_reasons,
            "model_identity_valid": model_identity_valid,
            "model_identity_reason_codes": model_identity_reasons,
            "model_request_attestation_valid": request_attestation_valid,
            "model_request_attestation_reason_codes": request_attestation_reasons,
            "model_request_attestations": copy.deepcopy(model_request_attestations),
        }
        ter_aux_llm_calls = [
            *aux_llm_calls,
            {"type": _TRIAL_TER_BINDING_AUX_TYPE, **copy.deepcopy(diagnostic)},
        ]
        ter = ter_builder(
            request_id=request_id,
            session_id=session_id,
            namespace=scope["namespace"],
            actor_concept_id=scope["actor_user_id"],
            user_id=scope["actor_user_id"],
            org_id=scope["organisation_concept_id"],
            prompt_text=trial["prompt"],
            response_text=response_text,
            interaction_timestamp_utc=_timestamp(now()),
            tool_invocations=tool_invocations,
            turn_execution_diagnostics={
                "learning_advice_experiment": diagnostic,
            },
            aux_llm_calls=ter_aux_llm_calls,
            llm_calls=llm_calls,
        )
        if (
            not isinstance(ter, Mapping)
            or ter.get("schema_version") != TURN_EXECUTION_RECORD_SCHEMA_VERSION
            or ter.get("request_id") != request_id
        ):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_ter_invalid",
                "The canonical turn-record builder returned an invalid record",
            )
        ter_receipt_raw = ter_persister(
            record=ter,
            user_id=scope["actor_user_id"],
            session_id=session_id,
            namespace=scope["namespace"],
            org_id=scope["organisation_concept_id"],
        )
        ter_receipt = _compact_persistence_receipt(ter_receipt_raw)
        if ter_receipt.get("acknowledged") is not True:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_ter_not_persisted",
                "The canonical turn execution record was not acknowledged",
            )
        ter_readback = _read_back_trial_turn_execution_record(
            loader=ter_loader,
            request_id=request_id,
            session_id=session_id,
            scope=scope,
            prompt_sha256=hashlib.sha256(trial["prompt"].encode("utf-8")).hexdigest(),
            response_sha256=hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
            diagnostic=diagnostic,
        )
        if any(
            reason.startswith("sol_model_observed:")
            for reason in model_identity_reasons
        ):
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_sol_model_observed",
                "The acting runtime selected a forbidden Sol-family model",
            )

        learning_text_echo_path = _learning_text_echo_path(
            learning_fragments=learning_fragments,
            outcome_projection={
                "response": response_text,
                "tool_trace": tool_trace,
                "bounded_tool_results": bounded_tool_results,
            },
        )
        blind_input = (
            None
            if learning_text_echo_path is not None
            else _blind_evaluation_input(
                trial=trial,
                response_text=response_text,
                terminal_status=terminal_status,
                tool_trace=tool_trace,
                bounded_tool_results=bounded_tool_results,
                candidate_ref=frozen["candidate_ref"],
            )
        )
        executions.append(
            {
                "trial": copy.deepcopy(bound_trial),
                "evaluation_id": evaluation_id,
                "blind_input": blind_input,
                "learning_text_echo_path": learning_text_echo_path,
                "response_sha256": hashlib.sha256(
                    response_text.encode("utf-8")
                ).hexdigest(),
                "response_chars": len(response_text),
                "terminal_status": terminal_status,
                "tool_trace": tool_trace,
                "model_call_trace": model_call_trace,
                "model_call_count": model_call_count,
                "reported_duration_ms": reported_duration_ms,
                "runner_elapsed_ms": runner_elapsed_ms,
                "learning_advice_exposures": exposures,
                "exposure_valid": exposure_valid,
                "exposure_reason_codes": exposure_reasons,
                "model_identity_valid": model_identity_valid,
                "model_identity_reason_codes": model_identity_reasons,
                "model_request_attestation_valid": request_attestation_valid,
                "model_request_attestation_reason_codes": (request_attestation_reasons),
                "model_request_attestations": model_request_attestations,
                "runtime_attestation": runtime_attestation,
                "acting_support_identity": trial_acting_support,
                "read_only_effect_attempts_blocked": blocked_effect_attempts,
                "execution_error": execution_error,
                "ter_receipt": ter_receipt,
                "ter_readback": ter_readback,
            }
        )
        # Do not retain the canonical body-bearing projection in experiment
        # observations or pass it to the evaluator.
        del projection

    evaluation_order = list(range(len(executions)))
    random.Random(int(frozen["randomisation_seed"]) ^ 0xB11D_EA7E).shuffle(
        evaluation_order
    )
    evaluations: dict[str, dict[str, Any]] = {}
    for index in evaluation_order:
        execution = executions[index]
        trial = execution["trial"]
        blind_input = copy.deepcopy(execution["blind_input"])
        if blind_input is None:
            execution["evaluation_authority"] = []
            evaluations[execution["evaluation_id"]] = _invalid_evaluator_result(
                "candidate_or_source_text_echoed"
            )
            continue
        evaluator_attestation, evaluator_content = _attest_evaluation_authority(
            loader=evaluator_authority_loader,
            concept_id=trial["evaluator_concept_id"],
            expected_sha256=trial["evaluator_sha256"],
            kind="evaluator",
            scope=scope,
        )
        rubric_attestation, rubric_content = _attest_evaluation_authority(
            loader=evaluator_authority_loader,
            concept_id=trial["rubric_concept_id"],
            expected_sha256=trial["rubric_sha256"],
            kind="rubric",
            scope=scope,
        )
        evaluation_authority = [evaluator_attestation, rubric_attestation]
        evaluator_model_config = build_learning_advice_evaluator_model_config(frozen)
        try:
            raw_evaluation = evaluate_blind_trial(
                blind_input,
                evaluator_content=evaluator_content,
                rubric_content=rubric_content,
                model_config=copy.deepcopy(evaluator_model_config),
            )
        except Exception as exc:  # noqa: BLE001 - incomplete evaluation is evidence
            evaluation = _invalid_evaluator_result("evaluator_exception")
            evaluation["error_class"] = type(exc).__name__
        else:
            evaluation = _normalise_evaluator_result(
                raw_evaluation,
                manifest=frozen,
                trial=trial,
            )
        execution["evaluation_authority"] = evaluation_authority
        evaluations[execution["evaluation_id"]] = evaluation

    observations: list[dict[str, Any]] = []
    persistence_receipts: list[dict[str, Any]] = []
    trial_observation_ids: list[str] = []
    for execution in executions:
        trial = execution["trial"]
        identities = trial["execution_identity"]
        evaluation = evaluations[execution["evaluation_id"]]
        observation = {
            "schema_version": LEARNING_ADVICE_EXPERIMENT_OBSERVATION_SCHEMA_VERSION,
            "observation_kind": "paired_trial",
            "experiment_spec_id": frozen["experiment_spec_id"],
            "experiment_run_id": frozen["experiment_run_id"],
            "manifest_sha256": frozen["manifest_sha256"],
            "plan_sha256": canonical_plan["plan_sha256"],
            "trial_id": trial["trial_id"],
            "pair_id": trial["pair_id"],
            "case_id": trial["case_id"],
            "case_kind": trial["case_kind"],
            "repeat": trial["repeat"],
            "arm": trial["arm"],
            "candidate_ref": copy.deepcopy(frozen["candidate_ref"]),
            "model": copy.deepcopy(frozen["model"]),
            "runtime_snapshot": copy.deepcopy(runtime_snapshot),
            "execution_identity": copy.deepcopy(identities),
            "turn_execution_request_ids": [identities["request_id"]],
            "trial_integrity_valid": bool(
                execution["exposure_valid"]
                and execution["model_identity_valid"]
                and execution["model_request_attestation_valid"]
                and execution["execution_error"] is None
                and execution["learning_text_echo_path"] is None
            ),
            "integrity_reason_codes": copy.deepcopy(execution["exposure_reason_codes"])
            + copy.deepcopy(execution["model_identity_reason_codes"])
            + copy.deepcopy(execution["model_request_attestation_reason_codes"])
            + (["adaptive_turn_exception"] if execution["execution_error"] else [])
            + (
                ["candidate_or_source_text_echoed"]
                if execution["learning_text_echo_path"] is not None
                else []
            ),
            "execution": {
                "terminal_status": execution["terminal_status"],
                "response_sha256": execution["response_sha256"],
                "response_chars": execution["response_chars"],
                "tool_trace": copy.deepcopy(execution["tool_trace"]),
                "model_call_trace": copy.deepcopy(execution["model_call_trace"]),
                "model_call_count": execution["model_call_count"],
                "model_request_attestations": copy.deepcopy(
                    execution["model_request_attestations"]
                ),
                "reported_duration_ms": execution["reported_duration_ms"],
                "runner_elapsed_ms": execution["runner_elapsed_ms"],
                "learning_advice_exposures": copy.deepcopy(
                    execution["learning_advice_exposures"]
                ),
                "execution_error": copy.deepcopy(execution["execution_error"]),
                "evaluation_input_blocked_reason": (
                    "candidate_or_source_text_echoed"
                    if execution["learning_text_echo_path"] is not None
                    else None
                ),
                "ter_persistence": copy.deepcopy(execution["ter_receipt"]),
                "turn_execution_record_sha256": execution["ter_readback"][
                    "turn_execution_record_sha256"
                ],
                "runtime_attestation": copy.deepcopy(execution["runtime_attestation"]),
                "acting_support_identity": copy.deepcopy(
                    execution["acting_support_identity"]
                ),
                "read_only_effect_policy": _EFFECT_POLICY,
                "read_only_effect_attempts_blocked": copy.deepcopy(
                    execution["read_only_effect_attempts_blocked"]
                ),
            },
            "evaluation": {
                "evaluation_id": execution["evaluation_id"],
                "evaluator_concept_id": trial["evaluator_concept_id"],
                "evaluator_sha256": trial["evaluator_sha256"],
                "rubric_concept_id": trial["rubric_concept_id"],
                "rubric_sha256": trial["rubric_sha256"],
                "authority_attestations": copy.deepcopy(
                    execution["evaluation_authority"]
                ),
                **copy.deepcopy(evaluation),
            },
            "observed_outcome": {
                "passed": evaluation.get("passed"),
                "evaluation_status": evaluation.get("status"),
                "trial_integrity_valid": bool(
                    execution["exposure_valid"]
                    and execution["model_identity_valid"]
                    and execution["model_request_attestation_valid"]
                    and execution["execution_error"] is None
                    and execution["learning_text_echo_path"] is None
                ),
            },
        }
        _assert_body_free_observation(observation)
        persistence_envelope = _trial_persistence_envelope(observation)
        persistence_raw = observation_persister(
            run_id=frozen["experiment_run_id"],
            observations=persistence_envelope,
            turn_execution_request_ids=[identities["request_id"]],
        )
        persistence = _compact_persistence_receipt(persistence_raw)
        if persistence.get("acknowledged") is not True:
            raise LearningAdviceExperimentRunnerError(
                "learning_advice_experiment_observation_not_persisted",
                "A body-free experiment observation was not acknowledged",
            )
        persistence_receipts.append(persistence)
        trial_observation_ids.append(persistence_envelope["observation_id"])
        observations.append(observation)

    paired = compute_learning_advice_paired_results(
        observations,
        required_applicable_pair_count=(
            sum(case["kind"] == "applicable" for case in frozen["cases"])
            * int(frozen["repeats"])
        ),
        required_control_pair_count=(
            sum(case["kind"] == "control" for case in frozen["cases"])
            * int(frozen["repeats"])
        ),
    )
    content_decision = evaluate_learning_advice_content_decision(
        paired,
        decision_rule=frozen["decision_rule"],
    )
    valid_trial_count = sum(
        item["trial_integrity_valid"] is True for item in observations
    )
    completed_evaluation_count = sum(
        item["evaluation"].get("status") == "completed" for item in observations
    )
    turn_execution_request_ids = [
        item["execution_identity"]["request_id"] for item in observations
    ]
    result_status = (
        "completed"
        if valid_trial_count == len(observations)
        and completed_evaluation_count == len(observations)
        and content_decision["decision"] != "inconclusive"
        else "inconclusive"
    )
    result_evidence = _canonical_result_evidence(
        {
            "schema_version": LEARNING_ADVICE_EXPERIMENT_RESULT_SCHEMA_VERSION,
            "experiment_spec_id": frozen["experiment_spec_id"],
            "experiment_run_id": frozen["experiment_run_id"],
            "manifest_sha256": frozen["manifest_sha256"],
            "plan_sha256": canonical_plan["plan_sha256"],
            "candidate_ref": copy.deepcopy(frozen["candidate_ref"]),
            "runtime_snapshot": copy.deepcopy(runtime_snapshot),
            "acting_support_identity": copy.deepcopy(acting_support_identity),
            "planned_trial_count": canonical_plan["trial_count"],
            "executed_trial_count": len(executions),
            "persisted_ter_count": len(executions),
            "persisted_trial_observation_count": len(persistence_receipts),
            "valid_trial_count": valid_trial_count,
            "completed_evaluation_count": completed_evaluation_count,
            "paired_results": paired,
            "content_decision": content_decision,
            "secondary_metrics": {
                "A": _arm_metrics(observations, arm="A"),
                "B": _arm_metrics(observations, arm="B"),
            },
            "trial_observation_ids": trial_observation_ids,
        }
    )
    final_envelope = _final_persistence_envelope(
        result_evidence,
        turn_execution_request_ids=turn_execution_request_ids,
    )
    final_persistence_raw = observation_persister(
        run_id=frozen["experiment_run_id"],
        observations=final_envelope,
        turn_execution_request_ids=turn_execution_request_ids,
    )
    final_persistence = _compact_persistence_receipt(final_persistence_raw)
    if final_persistence.get("acknowledged") is not True:
        raise LearningAdviceExperimentRunnerError(
            "learning_advice_experiment_result_not_persisted",
            "The canonical final paired result was not acknowledged",
        )
    terminalisation = _finalise_experiment_run(
        finaliser=run_finaliser,
        run_id=frozen["experiment_run_id"],
    )
    canonical_readback = _read_back_experiment_result(
        loader=run_loader,
        manifest=frozen,
        plan=canonical_plan,
        trial_observations=observations,
        result_evidence=result_evidence,
    )
    candidate_disposition = _record_and_verify_candidate_disposition(
        manifest=frozen,
        result_evidence=result_evidence,
        recorder=disposition_recorder,
        loader=disposition_loader,
    )
    result: dict[str, Any] = {
        **result_evidence,
        "status": result_status,
        "persisted_observation_count": len(persistence_receipts) + 1,
        "canonical_persistence": {
            "preflight": run_preflight,
            "final_observation_id": final_envelope["observation_id"],
            "final_persistence": final_persistence,
            "terminalisation": terminalisation,
            "readback": canonical_readback,
        },
        "candidate_disposition": candidate_disposition,
        "observations": observations,
    }
    _assert_body_free_observation(result)
    return result


def run_learning_advice_experiment(
    manifest: Mapping[str, Any],
    *,
    gateway: Any,
    llm_client: Any,
    evaluate_blind_trial: Callable[..., Any],
    evaluator_authority_loader: Callable[..., Mapping[str, Any]],
    runtime_snapshot_loader: Callable[..., Mapping[str, Any]],
    gateway_snapshot_loader: Callable[..., Mapping[str, Any]],
    candidate_source_text_loader: Callable[..., Mapping[str, Any]],
    context: Sequence[Mapping[str, Any]] = (),
    trusted_argument_values: Mapping[str, Any] | None = None,
    workflow_launch_inputs: Mapping[str, Any] | None = None,
    model_registry_snapshot: Any = None,
    plan: Mapping[str, Any] | None = None,
    candidate_loader: Callable[..., Mapping[str, Any]] | None = None,
    candidate_disposition_recorder: Callable[..., Mapping[str, Any]] | None = None,
    candidate_disposition_loader: Callable[..., Mapping[str, Any]] | None = None,
    adaptive_turn_executor: Callable[..., Any] | None = None,
    turn_record_builder: Callable[..., Mapping[str, Any]] | None = None,
    turn_record_persister: Callable[..., Any] | None = None,
    turn_record_loader: Callable[..., Mapping[str, Any] | None] | None = None,
    experiment_observation_persister: Callable[..., Any] | None = None,
    experiment_run_loader: Callable[[str], Mapping[str, Any] | None] | None = None,
    experiment_run_finaliser: Callable[..., Mapping[str, Any]] | None = None,
    experiment_run_abort_finaliser: Callable[..., Mapping[str, Any]] | None = None,
    identity_factory: Callable[[str], str] | None = None,
    timestamp_factory: Callable[[], Any] | None = None,
    monotonic: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Run one frozen plan and terminalise any post-attestation abort."""

    frozen = validate_learning_advice_experiment_manifest(manifest)
    canonical_plan = _validate_execution_plan(frozen, plan)
    observation_persister = (
        experiment_observation_persister or record_experiment_observation
    )
    run_loader = experiment_run_loader or get_experiment_run_state
    abort_finaliser = experiment_run_abort_finaliser or abort_experiment_run
    try:
        run_preflight = _attest_fresh_bound_experiment_run(
            loader=run_loader,
            manifest=frozen,
            plan=canonical_plan,
        )
        return _run_learning_advice_experiment_implementation(
            frozen,
            gateway=gateway,
            llm_client=llm_client,
            evaluate_blind_trial=evaluate_blind_trial,
            evaluator_authority_loader=evaluator_authority_loader,
            runtime_snapshot_loader=runtime_snapshot_loader,
            gateway_snapshot_loader=gateway_snapshot_loader,
            candidate_source_text_loader=candidate_source_text_loader,
            context=context,
            trusted_argument_values=trusted_argument_values,
            workflow_launch_inputs=workflow_launch_inputs,
            model_registry_snapshot=model_registry_snapshot,
            plan=canonical_plan,
            candidate_loader=candidate_loader,
            candidate_disposition_recorder=candidate_disposition_recorder,
            candidate_disposition_loader=candidate_disposition_loader,
            adaptive_turn_executor=adaptive_turn_executor,
            turn_record_builder=turn_record_builder,
            turn_record_persister=turn_record_persister,
            turn_record_loader=turn_record_loader,
            experiment_observation_persister=observation_persister,
            experiment_run_loader=run_loader,
            experiment_run_finaliser=experiment_run_finaliser,
            identity_factory=identity_factory,
            timestamp_factory=timestamp_factory,
            monotonic=monotonic,
            _preflight_attestation=run_preflight,
        )
    except Exception as exc:
        recovery = _abort_and_reconcile_experiment_run(
            manifest=frozen,
            plan=canonical_plan,
            error=exc,
            observation_persister=observation_persister,
            run_loader=run_loader,
            abort_finaliser=abort_finaliser,
        )
        _attach_abort_recovery(exc, recovery)
        raise


__all__ = [
    "LEARNING_ADVICE_BLIND_EVALUATION_INPUT_SCHEMA_VERSION",
    "LEARNING_ADVICE_BLIND_EVALUATION_RESULT_SCHEMA_VERSION",
    "LEARNING_ADVICE_EVALUATOR_MODEL_CALL_RECEIPT_SCHEMA_VERSION",
    "LEARNING_ADVICE_EVALUATOR_PROVENANCE_SCHEMA_VERSION",
    "LEARNING_ADVICE_EXPERIMENT_ABORT_RECOVERY_SCHEMA_VERSION",
    "LEARNING_ADVICE_EXPERIMENT_ABORT_SCHEMA_VERSION",
    "LEARNING_ADVICE_EXPERIMENT_OBSERVATION_SCHEMA_VERSION",
    "LEARNING_ADVICE_EXPERIMENT_RESULT_SCHEMA_VERSION",
    "LEARNING_ADVICE_EXPERIMENT_RUN_BINDING_SCHEMA_VERSION",
    "LearningAdviceExperimentReadOnlyViolation",
    "LearningAdviceExperimentRunnerError",
    "build_learning_advice_acting_support_identity",
    "build_learning_advice_evaluator_model_config",
    "build_learning_advice_evaluator_provenance_identity",
    "build_learning_advice_experiment_run_binding",
    "learning_advice_read_only_gateway_catalogue_sha256",
    "run_learning_advice_experiment",
    "validate_learning_advice_experiment_run_binding",
]
