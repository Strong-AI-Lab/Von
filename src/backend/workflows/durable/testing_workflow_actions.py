"""Durable action handlers for Testing Workflows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...security.access_control import bypass_access_control
from ...security.visibility_predicates import CANONICAL_SPECIFIC_TO_USER_PREDICATE
from ...services import concept_service
from ...services.arxiv_paper_link_service import (
    extract_arxiv_id_candidates,
    predict_arxiv_paper_concept_id,
)
from ...services.experiment_run_service import (
    _resolve_regression_suite_mode,
    compute_experiment_verdict,
    create_experiment_spec,
    emit_experiment_learning_signal,
    execute_regression_suite,
    get_experiment_run_state,
    get_experiment_spec_state,
    prepare_experiment_spec_from_template,
    prepare_meeting_invitation_experiment_spec,
    record_experiment_observation,
    start_experiment_run,
)
from ...services.arxiv_ingestion_testing_service import (
    cleanup_arxiv_paper_ingestion_test_artifacts,
    prepare_arxiv_paper_ingestion_test_fixture,
    verify_arxiv_paper_ingestion_test_result,
)
from ...services.namespace_service import parse_namespace, resolve_canonical_namespace
from ...services.testing_theory_service import (
    assert_testing_theory_local_claims,
    compute_testing_theory_diff,
    create_testing_theory_slice,
    get_testing_theory_state,
    import_canonical_context_into_theory,
    promote_testing_theory_validated_claims,
    rollback_testing_theory_local_writes,
)
from ...workflows.trace_store import get_workflow_execution_trace
from ...workflows.workflow_studio_service import (
    WorkflowStudioConflictError,
    validate_workflow_candidate,
)
from ...services.testing_workflow_contracts import (
    ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID,
    EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
    EXPERIMENT_CREATE_SPEC_ACTION_ID,
    EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
    EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
    EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
    EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
    EXPERIMENT_START_RUN_ACTION_ID,
    TESTING_CLEANUP_ARXIV_PAPER_INGESTION_ARTIFACTS_ACTION_ID,
    TESTING_PREPARE_ARXIV_PAPER_INGESTION_FIXTURE_ACTION_ID,
    TESTING_PREPARE_EXPERIMENT_SPEC_ACTION_ID,
    TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
    TESTING_VALIDATE_CANDIDATE_WORKFLOW_ACTION_ID,
    TESTING_VERIFY_ARXIV_PAPER_INGESTION_RESULT_ACTION_ID,
    THEORY_ASSERT_LOCAL_CLAIM_ACTION_ID,
    THEORY_COMPUTE_DIFF_ACTION_ID,
    THEORY_CREATE_SLICE_ACTION_ID,
    THEORY_GC_EXPIRED_SLICES_ACTION_ID,
    THEORY_IMPORT_CANONICAL_CONTEXT_ACTION_ID,
    THEORY_PROMOTE_VALIDATED_CLAIMS_ACTION_ID,
    THEORY_ROLLBACK_LOCAL_WRITES_ACTION_ID,
)
from ...services.workflow_vontology_materialisation_helpers import (
    stable_named_instance_concept_id,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..write_tool_policy import (
    WORKFLOW_EXECUTION_SIDE_EFFECT_MODE_THEORY_BOUNDED,
    WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
)
from .execution_observability import (
    await_workflow_terminal_state,
    build_workflow_execution_trace_summary,
    build_workflow_execution_response,
)
from .startup import get_system_status as get_durable_system_status


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_float(
    value: Any,
    *,
    default: float,
    minimum: float = 0.0,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _clone_mapping(value: Any) -> dict[str, Any]:
    return {str(key): value for key, value in dict(value).items()} if isinstance(value, Mapping) else {}


def _clone_sequence(value: Any) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return []
    return list(value)


def _context_mapping(request: WorkflowActionRequest) -> dict[str, Any]:
    return dict(request.data) if isinstance(request.data, dict) else {}


def _derive_actor_context(request: WorkflowActionRequest) -> dict[str, Any]:
    inputs = request.inputs if isinstance(request.inputs, Mapping) else {}
    context = _context_mapping(request)
    environment_namespace = _safe_str(getattr(request.environment, "user_namespace", None))
    environment_user_id = _safe_str(
        getattr(request.environment, "user_concept_id", None)
    )
    environment_org_id = _safe_str(
        getattr(request.environment, "org_concept_id", None)
    )
    context_namespace = _safe_str(context.get("namespace")) or _safe_str(
        context.get("user_namespace")
    )
    context_user_id = _safe_str(context.get("user_concept_id")) or _safe_str(
        context.get("user_id")
    )
    context_org_id = _safe_str(
        context.get("organisation_concept_id")
    ) or _safe_str(context.get("org_concept_id")) or _safe_str(context.get("org_id"))

    # The durable executor reconstructs these fields from the persisted
    # WorkflowInstance before every action.  Treat that environment/context as
    # actor authority and never let authored action inputs replace it.
    has_environment_actor = bool(
        environment_namespace or environment_user_id or environment_org_id
    )
    has_persisted_context_actor = bool(
        context_namespace or context_user_id or context_org_id
    )
    if has_environment_actor:
        namespace = environment_namespace
        user_id = environment_user_id
        org_id = environment_org_id
        source = "workflow_environment"
    elif has_persisted_context_actor:
        namespace = context_namespace
        user_id = context_user_id
        org_id = context_org_id
        source = "persisted_workflow_context"
    else:
        # Kept only for non-authoritative compatibility callers.  Every
        # experiment mutation below requires a workflow environment/context,
        # so payload identity cannot grant access to owned resources.
        namespace = _safe_str(inputs.get("namespace"))
        user_id = _safe_str(inputs.get("user_id"))
        org_id = _safe_str(inputs.get("org_id"))
        source = "action_input_claim"

    if namespace and (not user_id or not org_id):
        try:
            parsed = parse_namespace(namespace)
        except ValueError:
            parsed = {}
        user_slug = _safe_str(parsed.get("user_id"))
        org_slug = _safe_str(parsed.get("org_id"))
        if user_slug and not user_id:
            user_id = f"#V#{user_slug}"
        if org_slug and not org_id:
            org_id = f"#V#{org_slug}"

    return {
        "namespace": namespace or None,
        "user_id": user_id or None,
        "org_id": org_id or None,
        "authoritative": source != "action_input_claim",
        "source": source,
    }


_EXPERIMENT_RESOURCE_NOT_AVAILABLE = "experiment_resource_not_available"
_TESTING_THEORY_NOT_AVAILABLE = "testing_theory_not_available"
_TESTING_CLEANUP_AUTHORITY_REQUIRED = "testing_cleanup_authority_required"


def _failed_action(error: str) -> WorkflowActionResult:
    return WorkflowActionResult(
        status="failed",
        error=error,
        outputs={"success": False, "error": error},
    )


def _canonical_actor_namespace(actor: Mapping[str, Any]) -> str | None:
    return resolve_canonical_namespace(
        actor.get("namespace"),
        actor.get("user_id"),
        actor.get("org_id"),
    )


def _actor_has_durable_authority(actor: Mapping[str, Any]) -> bool:
    return bool(
        actor.get("authoritative")
        and actor.get("user_id")
        and _canonical_actor_namespace(actor)
    )


def _resource_state_owned_by_actor(
    state: Mapping[str, Any] | None,
    actor: Mapping[str, Any],
) -> bool:
    if not isinstance(state, Mapping) or not _actor_has_durable_authority(actor):
        return False
    actor_namespace = _canonical_actor_namespace(actor)
    resource_namespace = resolve_canonical_namespace(
        state.get("namespace"),
        state.get("user_id"),
        state.get("org_id"),
    )
    if not actor_namespace or resource_namespace != actor_namespace:
        return False
    stored_user_id = _safe_str(state.get("user_id"))
    stored_org_id = _safe_str(state.get("org_id"))
    if stored_user_id and stored_user_id != _safe_str(actor.get("user_id")):
        return False
    if stored_org_id and stored_org_id != _safe_str(actor.get("org_id")):
        return False
    return True


def _load_experiment_spec_state_for_authority(
    experiment_spec_id: str,
) -> dict[str, Any] | None:
    with bypass_access_control():
        return get_experiment_spec_state(experiment_spec_id)


def _load_experiment_run_state_for_authority(
    run_id: str,
) -> dict[str, Any] | None:
    with bypass_access_control():
        return get_experiment_run_state(run_id)


def _concept_exists_for_authority(concept_id: str) -> bool:
    if not concept_id:
        return False
    try:
        with bypass_access_control():
            return isinstance(
                concept_service.get_concept_by_concept_id_exact(concept_id),
                Mapping,
            )
    except concept_service.ConceptNotFoundError:
        return False


def _owned_experiment_spec_or_denial(
    *,
    experiment_spec_id: str,
    actor: Mapping[str, Any],
    allow_new: bool,
) -> WorkflowActionResult | None:
    if not _actor_has_durable_authority(actor):
        return _failed_action("workflow_actor_authority_required")
    try:
        state = _load_experiment_spec_state_for_authority(experiment_spec_id)
        concept_exists = _concept_exists_for_authority(experiment_spec_id)
    except Exception:
        return _failed_action(_EXPERIMENT_RESOURCE_NOT_AVAILABLE)
    if state is None:
        if allow_new and not concept_exists:
            return None
        return _failed_action(_EXPERIMENT_RESOURCE_NOT_AVAILABLE)
    if not _resource_state_owned_by_actor(state, actor):
        return _failed_action(_EXPERIMENT_RESOURCE_NOT_AVAILABLE)
    return None


def _owned_experiment_run_or_denial(
    *,
    run_id: str,
    actor: Mapping[str, Any],
    allow_new: bool = False,
) -> WorkflowActionResult | None:
    if not _actor_has_durable_authority(actor):
        return _failed_action("workflow_actor_authority_required")
    try:
        state = _load_experiment_run_state_for_authority(run_id)
        concept_exists = _concept_exists_for_authority(run_id)
    except Exception:
        return _failed_action(_EXPERIMENT_RESOURCE_NOT_AVAILABLE)
    if state is None:
        if allow_new and not concept_exists:
            return None
        return _failed_action(_EXPERIMENT_RESOURCE_NOT_AVAILABLE)
    if not _resource_state_owned_by_actor(state, actor):
        return _failed_action(_EXPERIMENT_RESOURCE_NOT_AVAILABLE)
    return None


def _load_testing_theory_state_for_authority(
    theory_id: str,
) -> dict[str, Any] | None:
    with bypass_access_control():
        return get_testing_theory_state(theory_id)


def _owned_testing_theory_or_denial(
    *,
    theory_id: str,
    actor: Mapping[str, Any],
    allow_new: bool = False,
) -> WorkflowActionResult | None:
    if not _actor_has_durable_authority(actor):
        return _failed_action("workflow_actor_authority_required")
    try:
        state = _load_testing_theory_state_for_authority(theory_id)
        concept_exists = _concept_exists_for_authority(theory_id)
    except Exception:
        return _failed_action(_TESTING_THEORY_NOT_AVAILABLE)
    if state is None:
        if allow_new and not concept_exists:
            return None
        return _failed_action(_TESTING_THEORY_NOT_AVAILABLE)
    if not _resource_state_owned_by_actor(state, actor):
        return _failed_action(_TESTING_THEORY_NOT_AVAILABLE)
    return None


def _referenced_theories_owned_or_denial(
    *,
    theory_ids: Any,
    actor: Mapping[str, Any],
) -> WorkflowActionResult | None:
    for theory_id in _normalise_concept_ids(theory_ids):
        if denial := _owned_testing_theory_or_denial(
            theory_id=theory_id,
            actor=actor,
        ):
            return denial
    return None


def _actor_scoped_experiment_spec_id(
    *,
    actor: Mapping[str, Any],
    label: str,
) -> str:
    namespace = _canonical_actor_namespace(actor) or "unscoped"
    return stable_named_instance_concept_id(
        f"{label.strip() or 'Testing experiment'} [{namespace}]",
        prefix="experiment_spec",
    )


def _actor_scoped_testing_theory_id(
    *,
    actor: Mapping[str, Any],
    label: str,
) -> str:
    namespace = _canonical_actor_namespace(actor) or "unscoped"
    return stable_named_instance_concept_id(
        f"{label.strip() or 'Ephemeral testing theory'} [{namespace}]",
        prefix="ephemeral_theory",
    )


def _normalise_concept_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw_item in value:
        item = _safe_str(raw_item)
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)
    return items


def _load_cleanup_concept_for_authority(
    concept_id: str,
) -> Mapping[str, Any] | None:
    try:
        with bypass_access_control():
            concept = concept_service.get_concept_by_concept_id_exact(concept_id)
    except concept_service.ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _concept_is_owned_by_actor(
    concept: Mapping[str, Any] | None,
    actor: Mapping[str, Any],
) -> bool:
    if not isinstance(concept, Mapping):
        return False
    actor_user_id = _safe_str(actor.get("user_id"))
    if not actor_user_id:
        return False
    if _safe_str(concept.get("created_by_concept_id")) == actor_user_id:
        return True
    relationships = concept.get("relationships")
    if not isinstance(relationships, Mapping):
        return False
    scoped_users = _normalise_concept_ids(
        relationships.get(CANONICAL_SPECIFIC_TO_USER_PREDICATE)
    )
    return actor_user_id in scoped_users


def _validated_arxiv_cleanup_inputs(
    request: WorkflowActionRequest,
    *,
    actor: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, WorkflowActionResult | None]:
    """Validate one cleanup against persisted fixture lineage and ownership.

    The underlying helper is deliberately powerful because certification needs
    to remove temporary Vontology and blob artefacts.  A durable workflow may
    therefore invoke it only from the canonical arXiv testing workflow, with
    targets bounded by that workflow's prepared fixture and with every target
    that will actually be deleted scoped to the persisted workflow actor.
    """

    if (
        request.workflow_id != ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID
        or not _actor_has_durable_authority(actor)
    ):
        return None, _failed_action(_TESTING_CLEANUP_AUTHORITY_REQUIRED)

    inputs = dict(request.inputs or {})
    context = _context_mapping(request)
    arxiv_id = _safe_str(context.get("arxiv_id"))
    fixture_paper_id = _safe_str(context.get("paper_concept_id"))
    requested_paper_id = _safe_str(inputs.get("paper_concept_id"))
    if not arxiv_id or not fixture_paper_id or requested_paper_id != fixture_paper_id:
        return None, _failed_action(_TESTING_CLEANUP_AUTHORITY_REQUIRED)
    if predict_arxiv_paper_concept_id(arxiv_id=arxiv_id) != fixture_paper_id:
        return None, _failed_action(_TESTING_CLEANUP_AUTHORITY_REQUIRED)

    requested_file_copy_ids = _normalise_concept_ids(
        [
            inputs.get("file_copy_concept_id"),
            *_normalise_concept_ids(inputs.get("file_copy_concept_ids")),
        ]
    )
    fixture_file_copy_ids = set(
        _normalise_concept_ids(
            [
                context.get("file_copy_concept_id"),
                *_normalise_concept_ids(context.get("file_copy_concept_ids")),
            ]
        )
    )
    requested_author_ids = _normalise_concept_ids(inputs.get("author_concept_ids"))
    requested_topic_ids = _normalise_concept_ids(inputs.get("topic_concept_ids"))
    fixture_author_ids = set(
        _normalise_concept_ids(context.get("expected_author_concept_ids"))
    )
    fixture_topic_ids = set(
        _normalise_concept_ids(context.get("expected_topic_concept_ids"))
    )
    preexisting_author_ids = set(
        _normalise_concept_ids(inputs.get("preexisting_author_concept_ids"))
    )
    preexisting_topic_ids = set(
        _normalise_concept_ids(inputs.get("preexisting_topic_concept_ids"))
    )
    fixture_preexisting_author_ids = set(
        _normalise_concept_ids(context.get("preexisting_author_concept_ids"))
    )
    fixture_preexisting_topic_ids = set(
        _normalise_concept_ids(context.get("preexisting_topic_concept_ids"))
    )

    if (
        not requested_file_copy_ids
        or not set(requested_file_copy_ids).issubset(fixture_file_copy_ids)
        or not set(requested_author_ids).issubset(fixture_author_ids)
        or not set(requested_topic_ids).issubset(fixture_topic_ids)
        or preexisting_author_ids != fixture_preexisting_author_ids
        or preexisting_topic_ids != fixture_preexisting_topic_ids
    ):
        return None, _failed_action(_TESTING_CLEANUP_AUTHORITY_REQUIRED)

    try:
        paper_concept = _load_cleanup_concept_for_authority(fixture_paper_id)
        paper_relationships = (
            paper_concept.get("relationships")
            if isinstance(paper_concept, Mapping)
            else None
        )
        linked_file_copy_ids = set(
            _normalise_concept_ids(
                paper_relationships.get(
                    "#V#propositional_information_thing_has_computer_file"
                )
                if isinstance(paper_relationships, Mapping)
                else []
            )
        )
        deletion_targets = [
            fixture_paper_id,
            *requested_file_copy_ids,
            *[
                item
                for item in requested_author_ids
                if item not in preexisting_author_ids
            ],
            *[
                item for item in requested_topic_ids if item not in preexisting_topic_ids
            ],
        ]
        owned_targets = all(
            _concept_is_owned_by_actor(
                _load_cleanup_concept_for_authority(concept_id),
                actor,
            )
            for concept_id in deletion_targets
        )
    except Exception:
        return None, _failed_action(_TESTING_CLEANUP_AUTHORITY_REQUIRED)

    if (
        not _concept_is_owned_by_actor(paper_concept, actor)
        or not set(requested_file_copy_ids).issubset(linked_file_copy_ids)
        or not owned_targets
    ):
        return None, _failed_action(_TESTING_CLEANUP_AUTHORITY_REQUIRED)

    return {
        "paper_concept_id": fixture_paper_id,
        "file_copy_concept_id": requested_file_copy_ids[0],
        "file_copy_concept_ids": requested_file_copy_ids,
        "author_concept_ids": requested_author_ids,
        "topic_concept_ids": requested_topic_ids,
        "preexisting_author_concept_ids": sorted(preexisting_author_ids),
        "preexisting_topic_concept_ids": sorted(preexisting_topic_ids),
    }, None


def _arxiv_repair_is_actor_owned(
    *,
    inputs: Mapping[str, Any],
    actor: Mapping[str, Any],
) -> bool:
    candidates = extract_arxiv_id_candidates(
        inputs.get("arxiv_id"),
        inputs.get("arxiv_source"),
        inputs.get("source_uri"),
        inputs.get("prompt_text") or inputs.get("prompt"),
    )
    if not candidates:
        # The fixture service will reject the identifier without mutation.
        return True
    paper_id = predict_arxiv_paper_concept_id(arxiv_id=candidates[0])
    try:
        paper = _load_cleanup_concept_for_authority(paper_id)
    except Exception:
        return False
    if paper is None:
        return True
    if not _concept_is_owned_by_actor(paper, actor):
        return False
    relationships = paper.get("relationships")
    linked_file_copy_ids = _normalise_concept_ids(
        relationships.get("#V#propositional_information_thing_has_computer_file")
        if isinstance(relationships, Mapping)
        else []
    )
    try:
        return all(
            _concept_is_owned_by_actor(
                _load_cleanup_concept_for_authority(concept_id),
                actor,
            )
            for concept_id in linked_file_copy_ids
        )
    except Exception:
        return False


def _result_from_payload(payload: Mapping[str, Any] | None) -> WorkflowActionResult:
    data = dict(payload) if isinstance(payload, Mapping) else {}
    success = bool(data.get("success"))
    error = _safe_str(data.get("error")) or _safe_str(data.get("error_code")) or None
    return WorkflowActionResult(
        status="success" if success else "failed",
        outputs=data,
        error=None if success else error,
    )


def _workflow_execution_verdict_for_status(
    *,
    final_status: str,
    timed_out: bool,
) -> str:
    if timed_out or final_status in {"pending", "running", "paused", ""}:
        return "inconclusive"
    if final_status == "completed":
        return "pass"
    if final_status in {"failed", "cancelled"}:
        return "fail"
    return "inconclusive"


def _compact_workflow_execution_payload(
    workflow_execution_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = (
        dict(workflow_execution_payload)
        if isinstance(workflow_execution_payload, Mapping)
        else {}
    )
    compact: dict[str, Any] = {}
    for key in (
        "workflow_id",
        "instance_id",
        "launch_mode",
        "await_terminal",
        "timeout_seconds",
        "advisory_seconds",
        "configured_timeout_seconds",
        "poll_interval_seconds",
        "poll_count",
        "timed_out",
        "elapsed_time_enforcement",
        "advisory_exceeded",
        "hard_timeout_seconds",
        "early_timeout_reason",
        "current_status",
        "current_state",
        "execution_state",
        "final_status",
        "failure_code",
        "failure_family",
        "failure_reason",
        "error",
        "error_code",
        "error_step",
        "execution_trace_id",
        "step_result_envelope_count",
    ):
        if key in raw:
            compact[key] = raw.get(key)

    queue_diagnostic = raw.get("queue_diagnostic")
    if isinstance(queue_diagnostic, Mapping):
        compact["queue_diagnostic"] = dict(queue_diagnostic)

    durable_system_status = raw.get("durable_system_status")
    if isinstance(durable_system_status, Mapping):
        compact["durable_system_status"] = dict(durable_system_status)

    outputs = raw.get("outputs")
    compact_outputs = _compact_workflow_execution_outputs(outputs)
    if compact_outputs:
        compact["outputs"] = compact_outputs

    workflow_result_envelope = raw.get("workflow_result_envelope")
    if isinstance(workflow_result_envelope, Mapping):
        compact["workflow_result_envelope"] = dict(workflow_result_envelope)

    metadata_validation = raw.get("metadata_validation")
    if isinstance(metadata_validation, Mapping):
        compact_metadata: dict[str, Any] = {}
        summary = metadata_validation.get("summary")
        if isinstance(summary, Mapping):
            compact_metadata["summary"] = dict(summary)
        last_event = metadata_validation.get("last_event")
        if isinstance(last_event, Mapping):
            compact_metadata["last_event"] = dict(last_event)
        if compact_metadata:
            compact["metadata_validation"] = compact_metadata

    return compact


def _build_trace_summary(execution_trace_id: Any) -> dict[str, Any]:
    trace_id = _safe_str(execution_trace_id)
    if not trace_id:
        return {}
    trace_doc = get_workflow_execution_trace(trace_id)
    if not isinstance(trace_doc, Mapping):
        return {}
    return build_workflow_execution_trace_summary(trace_doc)


def _build_side_effect_audit(
    *,
    instance: Any | None,
    allowed_side_effects: Sequence[Any],
    forbidden_side_effects: Sequence[Any],
) -> dict[str, Any]:
    workflow_data = getattr(instance, "workflow_data", None)
    workflow_context = dict(workflow_data) if isinstance(workflow_data, Mapping) else {}
    raw_events = workflow_context.get("mutation_guardrail_events")
    events = [
        {str(key): value for key, value in item.items()}
        for item in list(raw_events)
        if isinstance(item, Mapping)
    ] if isinstance(raw_events, Sequence) and not isinstance(raw_events, (str, bytes, bytearray)) else []

    blocked_event_count = 0
    required_confirmation_count = 0
    decision_counts: dict[str, int] = {}
    for event in events:
        decision = _safe_str(event.get("decision")).lower() or "unknown"
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        if decision not in {"allow", "allowed"}:
            blocked_event_count += 1
        if _coerce_bool(event.get("requires_confirmation"), default=False):
            required_confirmation_count += 1

    return {
        "event_count": len(events),
        "blocked_event_count": blocked_event_count,
        "requires_confirmation_count": required_confirmation_count,
        "decision_counts": decision_counts,
        "allowed_side_effects": [_safe_str(item) for item in allowed_side_effects if _safe_str(item)],
        "forbidden_side_effects": [_safe_str(item) for item in forbidden_side_effects if _safe_str(item)],
    }


def _build_execution_repair_hints(
    *,
    candidate_validation: Mapping[str, Any] | None,
    trace_summary: Mapping[str, Any] | None,
    side_effect_audit: Mapping[str, Any] | None,
    workflow_execution: Mapping[str, Any],
) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    validation_payload = dict(candidate_validation) if isinstance(candidate_validation, Mapping) else {}
    for item in _clone_sequence(validation_payload.get("repair_hints")):
        if isinstance(item, Mapping):
            hints.append({str(key): value for key, value in item.items()})
    if isinstance(trace_summary, Mapping) and _safe_str(trace_summary.get("last_error")):
        hints.append(
            {
                "scope": "workflow_execution",
                "reason_code": "workflow_execution_failed",
                "repair_hint": "Inspect the failed step and trace summary before rerunning the candidate workflow.",
            }
        )
    if isinstance(side_effect_audit, Mapping) and int(side_effect_audit.get("blocked_event_count") or 0) > 0:
        hints.append(
            {
                "scope": "side_effect_policy",
                "reason_code": "mutation_guardrail_blocked",
                "repair_hint": "Reduce write-capable actions or tighten the candidate workflow's mutation profile.",
            }
        )
    metadata_validation = workflow_execution.get("metadata_validation")
    metadata_summary = (
        _clone_mapping(metadata_validation.get("summary"))
        if isinstance(metadata_validation, Mapping)
        else {}
    )
    if int(metadata_summary.get("failed_count") or 0) > 0:
        hints.append(
            {
                "scope": "metadata_validation",
                "reason_code": "metadata_validation_failed",
                "repair_hint": "Repair metadata validation failures before promoting or rerunning the workflow.",
            }
        )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for hint in hints:
        key = (_safe_str(hint.get("scope")), _safe_str(hint.get("reason_code")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(hint)
    return deduped


def _build_execution_quality_signals(
    *,
    workflow_execution: Mapping[str, Any],
    candidate_validation: Mapping[str, Any] | None,
    side_effect_audit: Mapping[str, Any] | None,
    timed_out: bool,
) -> dict[str, Any]:
    metadata_validation = workflow_execution.get("metadata_validation")
    metadata_summary = (
        _clone_mapping(metadata_validation.get("summary"))
        if isinstance(metadata_validation, Mapping)
        else {}
    )
    quality_signals = {
        "timed_out": bool(timed_out),
        "candidate_validation_valid": (
            candidate_validation.get("valid")
            if isinstance(candidate_validation, Mapping)
            else None
        ),
        "metadata_validation_failed_count": int(metadata_summary.get("failed_count") or 0),
        "metadata_validation_enforced_failed_count": int(
            metadata_summary.get("enforced_failed_count") or 0
        ),
        "mutation_guardrail_blocked_count": int(
            (side_effect_audit or {}).get("blocked_event_count") or 0
        )
        if isinstance(side_effect_audit, Mapping)
        else 0,
    }
    quality_signals["requires_follow_up"] = bool(
        quality_signals["timed_out"]
        or _safe_str(workflow_execution.get("current_status"))
        in {"pending", "running", "paused"}
        or bool(_safe_str(workflow_execution.get("failure_code")))
        or quality_signals["candidate_validation_valid"] is False
        or quality_signals["metadata_validation_failed_count"] > 0
        or quality_signals["mutation_guardrail_blocked_count"] > 0
    )
    return quality_signals


def _get_durable_runtime_status() -> Mapping[str, Any] | None:
    try:
        return get_durable_system_status(include_counts=False)
    except TypeError:
        try:
            return get_durable_system_status()
        except Exception:
            return None
    except Exception:
        return None


_OMIT_WORKFLOW_OUTPUT_VALUE = object()


def _compact_workflow_execution_outputs(outputs: Any) -> dict[str, Any]:
    if not isinstance(outputs, Mapping):
        return {}

    compact: dict[str, Any] = {}
    omitted_keys: list[str] = []
    for raw_key, raw_value in outputs.items():
        key = _safe_str(raw_key)
        if not key:
            continue
        compacted = _compact_workflow_execution_output_value(raw_value)
        if compacted is _OMIT_WORKFLOW_OUTPUT_VALUE:
            omitted_keys.append(key)
            continue
        compact[key] = compacted
    if omitted_keys:
        compact["omitted_output_keys"] = omitted_keys
    return compact


def _compact_workflow_execution_output_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        return text[:2048] if len(text) > 2048 else text
    if isinstance(value, Mapping):
        return _OMIT_WORKFLOW_OUTPUT_VALUE
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return _OMIT_WORKFLOW_OUTPUT_VALUE

    compact_items: list[Any] = []
    omitted = False
    for item in list(value)[:25]:
        compacted = _compact_workflow_execution_output_value(item)
        if compacted is _OMIT_WORKFLOW_OUTPUT_VALUE:
            omitted = True
            continue
        compact_items.append(compacted)
    if len(value) > 25:
        omitted = True
    if not compact_items and not omitted:
        return []
    if compact_items:
        return compact_items
    return _OMIT_WORKFLOW_OUTPUT_VALUE


def _summarise_observation_recording_payload(
    observation_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(observation_payload) if isinstance(observation_payload, Mapping) else {}
    recorded = raw.get("recorded_observations")
    recorded_observations = (
        [dict(item) for item in recorded if isinstance(item, Mapping)]
        if isinstance(recorded, list)
        else []
    )
    experiment_run_raw = raw.get("experiment_run")
    aggregated_observations_raw = (
        experiment_run_raw.get("observations")
        if isinstance(experiment_run_raw, Mapping)
        else None
    )
    aggregated_observations = (
        [dict(item) for item in aggregated_observations_raw if isinstance(item, Mapping)]
        if isinstance(aggregated_observations_raw, list)
        else []
    )
    labels = [
        label
        for label in (
            _safe_str(item.get("label")) for item in recorded_observations
        )
        if label
    ]
    summary: dict[str, Any] = {
        "success": bool(raw.get("success")),
        "run_id": _safe_str(raw.get("run_id")) or None,
        "recorded_observation_count": len(recorded_observations),
    }
    if labels:
        summary["recorded_observation_labels"] = labels
    if aggregated_observations:
        summary["observation_count"] = len(aggregated_observations)
        summary["observations"] = aggregated_observations
    elif recorded_observations:
        summary["observation_count"] = len(recorded_observations)
        summary["observations"] = recorded_observations
    error = _safe_str(raw.get("error")) or _safe_str(raw.get("error_code")) or None
    if error:
        summary["error"] = error
    return summary


def _summarise_experiment_start_run_payload(
    start_run_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(start_run_payload) if isinstance(start_run_payload, Mapping) else {}
    experiment_run_raw = raw.get("experiment_run")
    experiment_run = (
        dict(experiment_run_raw) if isinstance(experiment_run_raw, Mapping) else {}
    )
    summary: dict[str, Any] = {
        "success": bool(raw.get("success")),
        "run_id": _safe_str(raw.get("run_id")) or None,
        "experiment_spec_id": _safe_str(experiment_run.get("experiment_spec_id")) or None,
        "status": _safe_str(experiment_run.get("status")) or None,
        "theory_id": _safe_str(experiment_run.get("theory_id")) or None,
    }
    for key in ("target_workflow_ids", "candidate_workflow_ids"):
        value = experiment_run.get(key)
        if isinstance(value, list) and value:
            summary[key] = list(value)
    benchmark_tier = _safe_str(experiment_run.get("benchmark_tier")) or None
    if benchmark_tier:
        summary["benchmark_tier"] = benchmark_tier
    error = _safe_str(raw.get("error")) or _safe_str(raw.get("error_code")) or None
    if error:
        summary["error"] = error
    return summary


def _summarise_experiment_verdict_payload(
    verdict_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(verdict_payload) if isinstance(verdict_payload, Mapping) else {}
    summary: dict[str, Any] = {
        "success": bool(raw.get("success")),
        "run_id": _safe_str(raw.get("run_id")) or None,
        "verdict": _safe_str(raw.get("verdict")) or None,
    }
    verdict_summary = raw.get("verdict_summary")
    if isinstance(verdict_summary, Mapping):
        summary["verdict_summary"] = dict(verdict_summary)
    promotion_recommendation = raw.get("promotion_recommendation")
    if isinstance(promotion_recommendation, Mapping):
        summary["promotion_recommendation"] = dict(promotion_recommendation)
    observations = raw.get("observations")
    if isinstance(observations, list):
        summary["observation_count"] = len(
            [item for item in observations if isinstance(item, Mapping)]
        )
    error = _safe_str(raw.get("error")) or _safe_str(raw.get("error_code")) or None
    if error:
        summary["error"] = error
    return summary


def _summarise_learning_signal_payload(
    learning_signal_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(learning_signal_payload) if isinstance(learning_signal_payload, Mapping) else {}
    learning_signal_raw = raw.get("learning_signal")
    learning_signal = (
        dict(learning_signal_raw) if isinstance(learning_signal_raw, Mapping) else {}
    )
    replay_case_raw = learning_signal.get("replay_case")
    replay_case = dict(replay_case_raw) if isinstance(replay_case_raw, Mapping) else {}
    summary: dict[str, Any] = {
        "success": bool(raw.get("success")),
        "run_id": _safe_str(raw.get("run_id")) or None,
        "learning_signal": {
            "schema_version": _safe_str(learning_signal.get("schema_version")) or None,
            "emitted_at_utc": _safe_str(learning_signal.get("emitted_at_utc")) or None,
            "selection_experience_id": _safe_str(
                learning_signal.get("selection_experience_id")
            )
            or None,
            "selection_outcome": _safe_str(learning_signal.get("selection_outcome"))
            or None,
            "selection_reward": learning_signal.get("selection_reward"),
            "replay_case": {
                "case_id": _safe_str(replay_case.get("case_id")) or None,
                "expected_workflow_id": _safe_str(
                    replay_case.get("expected_workflow_id")
                )
                or None,
                "baseline_workflow_id": _safe_str(
                    replay_case.get("baseline_workflow_id")
                )
                or None,
                "verdict": _safe_str(replay_case.get("verdict")) or None,
            },
        },
    }
    selection_experience = raw.get("selection_experience")
    if isinstance(selection_experience, Mapping):
        summary["selection_experience"] = dict(selection_experience)
    error = _safe_str(raw.get("error")) or _safe_str(raw.get("error_code")) or None
    if error:
        summary["error"] = error
    return summary


def _handle_theory_create_slice(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    name = _safe_str(inputs.get("name")) or "Ephemeral testing theory"
    theory_id = _safe_str(inputs.get("theory_id")) or _actor_scoped_testing_theory_id(
        actor=actor,
        label=name,
    )
    if denial := _owned_testing_theory_or_denial(
        theory_id=theory_id,
        actor=actor,
        allow_new=True,
    ):
        return denial
    if denial := _referenced_theories_owned_or_denial(
        theory_ids=inputs.get("included_theory_ids"),
        actor=actor,
    ):
        return denial
    experiment_spec_id = _safe_str(inputs.get("experiment_spec_id")) or None
    if experiment_spec_id and (
        denial := _owned_experiment_spec_or_denial(
            experiment_spec_id=experiment_spec_id,
            actor=actor,
            allow_new=False,
        )
    ):
        return denial
    result = create_testing_theory_slice(
        name=name,
        theory_id=theory_id,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
        included_canonical_concept_ids=inputs.get("included_canonical_concept_ids") or [],
        included_theory_ids=inputs.get("included_theory_ids") or [],
        expected_observations=inputs.get("expected_observations") or [],
        promotion_policy=inputs.get("promotion_policy"),
        retention_policy=inputs.get("retention_policy"),
        experiment_spec_id=experiment_spec_id,
        ttl_seconds=inputs.get("ttl_seconds"),
        description=_safe_str(inputs.get("description")) or None,
    )
    return _result_from_payload(result)


def _handle_theory_import_context(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    theory_id = _safe_str(inputs.get("theory_id"))
    if denial := _owned_testing_theory_or_denial(
        theory_id=theory_id,
        actor=actor,
    ):
        return denial
    if denial := _referenced_theories_owned_or_denial(
        theory_ids=inputs.get("theory_ids"),
        actor=actor,
    ):
        return denial
    result = import_canonical_context_into_theory(
        theory_id=theory_id,
        concept_ids=inputs.get("concept_ids") or [],
        theory_ids=inputs.get("theory_ids") or [],
    )
    return _result_from_payload(result)


def _handle_theory_assert_local_claim(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    theory_id = _safe_str(inputs.get("theory_id"))
    if denial := _owned_testing_theory_or_denial(
        theory_id=theory_id,
        actor=actor,
    ):
        return denial
    claims = inputs.get("claims")
    if claims is None and "claim" in inputs:
        claims = inputs.get("claim")
    result = assert_testing_theory_local_claims(
        theory_id=theory_id,
        claims=claims or [],
    )
    return _result_from_payload(result)


def _handle_theory_compute_diff(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    theory_id = _safe_str(inputs.get("theory_id"))
    if denial := _owned_testing_theory_or_denial(
        theory_id=theory_id,
        actor=actor,
    ):
        return denial
    return _result_from_payload(
        compute_testing_theory_diff(theory_id=theory_id)
    )


def _handle_theory_rollback(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    theory_id = _safe_str(inputs.get("theory_id"))
    if denial := _owned_testing_theory_or_denial(
        theory_id=theory_id,
        actor=actor,
    ):
        return denial
    result = rollback_testing_theory_local_writes(
        theory_id=theory_id,
        assertion_ids=inputs.get("assertion_ids") or [],
        clear_all=bool(inputs.get("clear_all", False)),
    )
    return _result_from_payload(result)


def _handle_theory_promote(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    theory_id = _safe_str(inputs.get("theory_id"))
    if denial := _owned_testing_theory_or_denial(
        theory_id=theory_id,
        actor=actor,
    ):
        return denial
    experiment_run_id = _safe_str(inputs.get("experiment_run_id")) or None
    if experiment_run_id and (
        denial := _owned_experiment_run_or_denial(
            run_id=experiment_run_id,
            actor=actor,
        )
    ):
        return denial
    result = promote_testing_theory_validated_claims(
        theory_id=theory_id,
        assertion_ids=inputs.get("assertion_ids") or [],
        experiment_run_id=experiment_run_id,
        required_verdict=_safe_str(inputs.get("required_verdict")) or "pass",
    )
    return _result_from_payload(result)


def _handle_theory_gc(request: WorkflowActionRequest) -> WorkflowActionResult:
    # The current service scans every visible org theory and has no exact actor
    # predicate.  Until it accepts an enforced owner filter, durable workflows
    # must fail closed; the trusted operator MCP path remains available for
    # explicit global maintenance.
    del request
    return _failed_action("testing_theory_gc_operator_authority_required")


def _handle_experiment_create_spec(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    name = _safe_str(inputs.get("name")) or "Testing experiment"
    experiment_spec_id = _safe_str(inputs.get("experiment_spec_id")) or (
        _actor_scoped_experiment_spec_id(actor=actor, label=name)
    )
    if denial := _owned_experiment_spec_or_denial(
        experiment_spec_id=experiment_spec_id,
        actor=actor,
        allow_new=True,
    ):
        return denial
    result = create_experiment_spec(
        name=name,
        experiment_spec_id=experiment_spec_id,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
        description=_safe_str(inputs.get("description")) or None,
        target_workflow_ids=inputs.get("target_workflow_ids") or [],
        target_capability_ids=inputs.get("target_capability_ids") or [],
        candidate_workflow_ids=inputs.get("candidate_workflow_ids") or [],
        baseline_workflow_id=_safe_str(inputs.get("baseline_workflow_id")) or None,
        theory_id=_safe_str(inputs.get("theory_id")) or None,
        experiment_suite_id=_safe_str(inputs.get("experiment_suite_id")) or None,
        fixture_payload=inputs.get("fixture_payload"),
        theory_setup=inputs.get("theory_setup"),
        expected_outcomes=inputs.get("expected_outcomes") or [],
        allowed_side_effects=inputs.get("allowed_side_effects") or [],
        forbidden_side_effects=inputs.get("forbidden_side_effects") or [],
        verdict_rules=inputs.get("verdict_rules"),
        replay_policy=inputs.get("replay_policy"),
        promotion_policy=inputs.get("promotion_policy"),
        metadata=inputs.get("metadata"),
    )
    return _result_from_payload(result)


def _handle_experiment_start_run(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    experiment_spec_id = _safe_str(inputs.get("experiment_spec_id"))
    if not experiment_spec_id:
        return _failed_action("experiment_spec_id_required")
    if denial := _owned_experiment_spec_or_denial(
        experiment_spec_id=experiment_spec_id,
        actor=actor,
        allow_new=False,
    ):
        return denial
    run_id = _safe_str(inputs.get("run_id")) or None
    if run_id and (
        denial := _owned_experiment_run_or_denial(
            run_id=run_id,
            actor=actor,
            allow_new=True,
        )
    ):
        return denial
    result = start_experiment_run(
        experiment_spec_id=experiment_spec_id,
        run_id=run_id,
        theory_id=_safe_str(inputs.get("theory_id")) or None,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
        target_workflow_ids=inputs.get("target_workflow_ids") or [],
        candidate_workflow_ids=inputs.get("candidate_workflow_ids") or [],
        baseline_workflow_id=_safe_str(inputs.get("baseline_workflow_id")) or None,
        benchmark_tier=_safe_str(inputs.get("benchmark_tier")) or None,
        benchmark_world_id=_safe_str(inputs.get("benchmark_world_id")) or None,
        selection_experience_id=_safe_str(inputs.get("selection_experience_id")) or None,
        turn_execution_request_ids=inputs.get("turn_execution_request_ids") or [],
        metadata=inputs.get("metadata"),
    )
    return _result_from_payload(_summarise_experiment_start_run_payload(result))


def _handle_experiment_record_observation(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    run_id = _safe_str(inputs.get("run_id"))
    if denial := _owned_experiment_run_or_denial(
        run_id=run_id,
        actor=actor,
    ):
        return denial
    observations = inputs.get("observations")
    if observations is None and "observation" in inputs:
        observations = inputs.get("observation")
    result = record_experiment_observation(
        run_id=run_id,
        observations=observations or [],
        turn_execution_request_ids=inputs.get("turn_execution_request_ids") or [],
    )
    return _result_from_payload(_summarise_observation_recording_payload(result))


def _handle_experiment_compute_verdict(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    run_id = _safe_str(inputs.get("run_id"))
    if denial := _owned_experiment_run_or_denial(
        run_id=run_id,
        actor=actor,
    ):
        return denial
    return _result_from_payload(
        _summarise_experiment_verdict_payload(
            compute_experiment_verdict(run_id=run_id)
        )
    )


def _handle_experiment_emit_learning_signal(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    run_id = _safe_str(inputs.get("run_id"))
    if denial := _owned_experiment_run_or_denial(
        run_id=run_id,
        actor=actor,
    ):
        return denial
    result = emit_experiment_learning_signal(
        run_id=run_id,
        selection_experience_id=_safe_str(inputs.get("selection_experience_id")) or None,
        turn_text=_safe_str(inputs.get("turn_text")) or None,
        expected_workflow_id=_safe_str(inputs.get("expected_workflow_id")) or None,
        baseline_workflow_id=_safe_str(inputs.get("baseline_workflow_id")) or None,
    )
    return _result_from_payload(_summarise_learning_signal_payload(result))


def _handle_experiment_execute_target_workflow(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    from . import WorkflowInstanceManager
    from .workflow_instance_submission_service import (
        submit_verified_workflow_instance,
    )

    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    if not _actor_has_durable_authority(actor):
        return _failed_action("workflow_actor_authority_required")
    workflow_id = _safe_str(inputs.get("workflow_id")) or _safe_str(
        next(iter(inputs.get("target_workflow_ids") or []), None)
    )
    if not workflow_id:
        return WorkflowActionResult(
            status="failed",
            error="workflow_id_required",
            outputs={"success": False, "error": "workflow_id_required"},
        )

    workflow_inputs_raw = inputs.get("workflow_inputs")
    workflow_inputs: dict[str, Any] = (
        {
            str(key): value
            for key, value in workflow_inputs_raw.items()
            if isinstance(key, str) and key.strip()
        }
        if isinstance(workflow_inputs_raw, Mapping)
        else {}
    )
    run_id = _safe_str(inputs.get("run_id")) or None
    if run_id and (
        denial := _owned_experiment_run_or_denial(
            run_id=run_id,
            actor=actor,
        )
    ):
        return denial
    theory_id = _safe_str(inputs.get("theory_id")) or None
    candidate_validation = _clone_mapping(inputs.get("candidate_validation"))
    fail_on_invalid_candidate = _coerce_bool(
        inputs.get("fail_on_invalid_candidate"),
        default=True,
    )
    allowed_side_effects = _clone_sequence(inputs.get("allowed_side_effects"))
    forbidden_side_effects = _clone_sequence(inputs.get("forbidden_side_effects"))
    await_terminal = _coerce_bool(inputs.get("await_terminal"), default=False)
    timeout_seconds = _coerce_float(
        inputs.get("timeout_seconds"),
        default=60.0 if await_terminal else 0.0,
        minimum=0.0,
    )
    poll_interval_seconds = _coerce_float(
        inputs.get("poll_interval_seconds"),
        default=1.0,
        minimum=0.0,
    )
    record_observation = _coerce_bool(
        inputs.get("record_observation"),
        default=await_terminal and run_id is not None,
    )
    observation_label = (
        _safe_str(inputs.get("observation_label")) or "target_workflow_execution"
    )
    expected_final_status = (
        _safe_str(inputs.get("expected_final_status")) or "completed"
        if await_terminal
        else None
    )
    if run_id:
        workflow_inputs.setdefault("experiment_run_id", run_id)
    if theory_id:
        workflow_inputs.setdefault("testing_theory_id", theory_id)
        workflow_inputs.setdefault(
            "workflow_execution_side_effect_policy",
            {
                "schema_version": WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
                "mode": WORKFLOW_EXECUTION_SIDE_EFFECT_MODE_THEORY_BOUNDED,
                "testing_theory_id": theory_id,
                "audit_label": EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
            },
        )

    if (
        fail_on_invalid_candidate
        and candidate_validation
        and candidate_validation.get("valid") is False
    ):
        candidate_assertion_classes = (
            _clone_sequence(candidate_validation.get("assertion_classes"))
            if isinstance(candidate_validation, Mapping)
            else []
        )
        payload: dict[str, Any] = {
            "success": False,
            "error": "candidate_validation_failed",
            "workflow_id": workflow_id,
            "candidate_validation": candidate_validation,
        }
        if record_observation and run_id:
            observation_payload = record_experiment_observation(
                run_id=run_id,
                observations=[
                    {
                        "label": observation_label,
                        "verdict": "fail",
                        "expected_outcome": expected_final_status,
                        "observed_outcome": "candidate_validation_failed",
                        "matched_expected_outcome": False,
                        "assertion_classes": [
                            "workflow_candidate_validation",
                            *candidate_assertion_classes,
                        ],
                        "candidate_validation": candidate_validation,
                        "repair_hints": _clone_sequence(
                            candidate_validation.get("repair_hints")
                        ),
                        "quality_signals": {
                            "candidate_validation_valid": False,
                            "requires_follow_up": True,
                            "timed_out": False,
                        },
                    }
                ],
            )
            payload["observation_recording"] = _summarise_observation_recording_payload(
                observation_payload
            )
        return WorkflowActionResult(
            status="failed",
            error="candidate_validation_failed",
            outputs=payload,
        )

    manager = WorkflowInstanceManager()
    submission = submit_verified_workflow_instance(
        manager=manager,
        workflow_id=workflow_id,
        user_id=actor["user_id"] or "anonymous",
        org_id=actor["org_id"] or "default",
        namespace=actor["namespace"] or "#V#anonymous@default",
        inputs=workflow_inputs,
        max_retries=int(inputs.get("max_retries", 3) or 3),
    )
    payload = build_workflow_execution_response(
        submission,
        workflow_inputs=workflow_inputs,
    )
    instance_id = _safe_str(payload.get("instance_id"))
    if not submission.success or not instance_id:
        payload["candidate_validation"] = candidate_validation or None
        if record_observation and run_id:
            observation_payload = record_experiment_observation(
                run_id=run_id,
                observations=[
                    {
                        "label": observation_label,
                        "verdict": "fail",
                        "expected_outcome": expected_final_status,
                        "observed_outcome": "workflow_submission_failed",
                        "matched_expected_outcome": False,
                        "assertion_classes": ["workflow_submission_failure"],
                        "candidate_validation": candidate_validation,
                        "repair_hints": _clone_sequence(
                            candidate_validation.get("repair_hints")
                        )
                        if isinstance(candidate_validation, Mapping)
                        else [],
                        "quality_signals": {
                            "candidate_validation_valid": (
                                candidate_validation.get("valid")
                                if isinstance(candidate_validation, Mapping)
                                else None
                            ),
                            "requires_follow_up": True,
                            "timed_out": False,
                        },
                        "workflow_execution": _compact_workflow_execution_payload(
                            payload.get("workflow_execution")
                        ),
                    }
                ],
            )
            payload["observation_recording"] = _summarise_observation_recording_payload(
                observation_payload
            )
        return WorkflowActionResult(
            status="failed",
            error=_safe_str(payload.get("error")) or "workflow_submission_failed",
            outputs=payload,
        )

    if not await_terminal:
        return WorkflowActionResult(status="success", outputs=payload)

    configured_timeout_seconds = timeout_seconds
    durable_system_status: Mapping[str, Any] | None = None
    early_timeout_reason: str | None = None
    durable_system_status = _get_durable_runtime_status()
    if (
        isinstance(durable_system_status, Mapping)
        and durable_system_status.get("worker_running") is False
    ):
        timeout_seconds = 0.0
        early_timeout_reason = "durable_worker_not_running"

    wait_result = await_workflow_terminal_state(
        manager,
        instance_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    if wait_result.timed_out and durable_system_status is None:
        try:
            durable_system_status = get_durable_system_status()
        except Exception:
            durable_system_status = None
    advisory_exceeded = bool(wait_result.advisory_exceeded)
    hard_timed_out = bool(wait_result.timed_out and not advisory_exceeded)
    payload = build_workflow_execution_response(
        submission,
        workflow_inputs=workflow_inputs,
        instance=wait_result.instance,
        await_terminal=True,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        poll_count=wait_result.poll_count,
        timed_out=hard_timed_out,
        advisory_exceeded=advisory_exceeded,
        elapsed_time_enforcement=wait_result.elapsed_time_enforcement,
        durable_system_status=durable_system_status,
    )
    workflow_execution = _compact_workflow_execution_payload(
        payload.get("workflow_execution")
    )
    if early_timeout_reason:
        workflow_execution["configured_timeout_seconds"] = configured_timeout_seconds
        workflow_execution["early_timeout_reason"] = early_timeout_reason
    payload["workflow_execution"] = workflow_execution
    payload.pop("workflow_instance", None)
    trace_summary = _build_trace_summary(workflow_execution.get("execution_trace_id"))
    side_effect_audit = _build_side_effect_audit(
        instance=wait_result.instance,
        allowed_side_effects=allowed_side_effects,
        forbidden_side_effects=forbidden_side_effects,
    )
    repair_hints = _build_execution_repair_hints(
        candidate_validation=candidate_validation,
        trace_summary=trace_summary,
        side_effect_audit=side_effect_audit,
        workflow_execution=workflow_execution,
    )
    quality_signals = _build_execution_quality_signals(
        workflow_execution=workflow_execution,
        candidate_validation=candidate_validation,
        side_effect_audit=side_effect_audit,
        timed_out=bool(payload.get("timed_out")),
    )
    payload["candidate_validation"] = candidate_validation or None
    payload["trace_summary"] = trace_summary or None
    payload["side_effect_audit"] = side_effect_audit
    payload["repair_hints"] = repair_hints
    payload["quality_signals"] = quality_signals
    final_status = _safe_str(payload.get("final_status"))
    timed_out = bool(payload.get("timed_out"))
    poll_count = int(workflow_execution.get("poll_count") or 0)

    if record_observation and run_id:
        verdict = _workflow_execution_verdict_for_status(
            final_status=final_status,
            timed_out=timed_out,
        )
        if candidate_validation and candidate_validation.get("valid") is False:
            verdict = "fail"
        if int(side_effect_audit.get("blocked_event_count") or 0) > 0:
            verdict = "fail"
        metadata_validation_summary = (
            dict(workflow_execution.get("metadata_validation", {}).get("summary"))
            if isinstance(workflow_execution.get("metadata_validation"), Mapping)
            and isinstance(workflow_execution.get("metadata_validation", {}).get("summary"), Mapping)
            else {}
        )
        if int(metadata_validation_summary.get("enforced_failed_count") or 0) > 0:
            verdict = "fail"
        assertion_classes = ["workflow_execution"]
        if isinstance(candidate_validation, Mapping):
            assertion_classes.extend(
                _clone_sequence(candidate_validation.get("assertion_classes"))
            )
        if int(side_effect_audit.get("blocked_event_count") or 0) > 0:
            assertion_classes.append("mutation_guardrail")
        if int(trace_summary.get("failed_step_count") or 0) > 0:
            assertion_classes.append("workflow_execution_failure")
        observation_payload = record_experiment_observation(
            run_id=run_id,
            observations=[
                {
                    "label": observation_label,
                    "verdict": verdict,
                    "expected_outcome": expected_final_status,
                    "observed_outcome": final_status or "unknown",
                    "matched_expected_outcome": (
                        final_status == expected_final_status
                        if expected_final_status
                        else None
                    ),
                    "assertion_classes": assertion_classes,
                    "workflow_execution": workflow_execution,
                    "candidate_validation": candidate_validation,
                    "trace_summary": trace_summary,
                    "side_effect_audit": side_effect_audit,
                    "repair_hints": repair_hints,
                    "quality_signals": quality_signals,
                    "metrics": {
                        "await_terminal": True,
                        "timeout_seconds": timeout_seconds,
                        "poll_interval_seconds": poll_interval_seconds,
                        "poll_count": poll_count,
                        "timed_out": timed_out,
                    },
                }
            ],
        )
        payload["observation_recording"] = _summarise_observation_recording_payload(
            observation_payload
        )
        if not observation_payload.get("success"):
            return WorkflowActionResult(
                status="failed",
                outputs=payload,
                error=_safe_str(observation_payload.get("error"))
                or "experiment_observation_record_failed",
            )

    return WorkflowActionResult(status="success", outputs=payload)


def _handle_experiment_execute_regression_suite(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    execution_tier = _safe_str(inputs.get("execution_tier")).lower() or "tier1"
    suite_policy = inputs.get("suite_policy")
    suite_resolution, suite_error = _resolve_regression_suite_mode(
        execution_tier=execution_tier,
        suite_policy=suite_policy if isinstance(suite_policy, Mapping) else None,
    )
    resolved_tier = _safe_str((suite_resolution or {}).get("execution_tier")).lower()
    resolved_mode = _safe_str((suite_resolution or {}).get("suite_mode")).lower()
    if (
        suite_error is None
        and (resolved_tier != "tier1" or resolved_mode != "cases")
    ) or isinstance(inputs.get("benchmark_scenario"), Mapping) or bool(
        _safe_str(inputs.get("output_root"))
    ):
        # Tier 2 clones/drops a database and writes caller-selected archives.
        # Durable actor identity is not operator authority; certification must
        # use the explicitly trusted MCP control plane for that mode.
        return _failed_action("testing_regression_suite_operator_authority_required")
    run_id = _safe_str(inputs.get("run_id")) or None
    if run_id and (
        denial := _owned_experiment_run_or_denial(
            run_id=run_id,
            actor=actor,
        )
    ):
        return denial
    result = execute_regression_suite(
        execution_tier=execution_tier,
        cases=inputs.get("cases") or [],
        benchmark_scenario=inputs.get("benchmark_scenario"),
        output_root=_safe_str(inputs.get("output_root")) or None,
        run_id=run_id,
        suite_policy=suite_policy,
    )
    return _result_from_payload(result)


def _handle_testing_validate_candidate_workflow(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    workflow_id = _safe_str(inputs.get("workflow_id"))
    authoring_spec = inputs.get("authoring_spec")
    if not workflow_id:
        return WorkflowActionResult(
            status="failed",
            error="workflow_id_required",
            outputs={"success": False, "error": "workflow_id_required"},
        )
    if not isinstance(authoring_spec, Mapping):
        return WorkflowActionResult(
            status="failed",
            error="authoring_spec_required",
            outputs={"success": False, "error": "authoring_spec_required"},
        )
    try:
        result = validate_workflow_candidate(
            workflow_id,
            authoring_spec=authoring_spec,
            base_definition_hash=_safe_str(inputs.get("base_definition_hash")) or None,
            validation_profile=_safe_str(inputs.get("validation_profile")) or None,
            include_preview=_coerce_bool(inputs.get("include_preview"), default=False),
        )
    except WorkflowStudioConflictError as exc:
        return WorkflowActionResult(
            status="failed",
            error=str(exc),
            outputs={
                "success": False,
                "error": str(exc),
                "workflow_id": workflow_id,
            },
        )
    except ValueError as exc:
        return WorkflowActionResult(
            status="failed",
            error=str(exc),
            outputs={
                "success": False,
                "error": str(exc),
                "workflow_id": workflow_id,
            },
        )
    return _result_from_payload(result)


def _handle_testing_prepare_experiment_spec(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    scenario_template = inputs.get("scenario_template")
    template_name = (
        _safe_str(scenario_template.get("name"))
        if isinstance(scenario_template, Mapping)
        else ""
    )
    name = _safe_str(inputs.get("name")) or template_name or "Testing experiment"
    experiment_spec_id = _safe_str(inputs.get("experiment_spec_id")) or (
        _actor_scoped_experiment_spec_id(actor=actor, label=name)
    )
    if denial := _owned_experiment_spec_or_denial(
        experiment_spec_id=experiment_spec_id,
        actor=actor,
        allow_new=True,
    ):
        return denial
    result = prepare_experiment_spec_from_template(
        scenario_template=scenario_template,
        template_inputs={
            key: value
            for key, value in inputs.items()
            if key
            not in {
                "scenario_template",
                "experiment_spec_id",
                "name",
                "namespace",
                "user_id",
                "org_id",
            }
        },
        experiment_spec_id=experiment_spec_id,
        name=name,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
    )
    return _result_from_payload(result)


def _handle_testing_prepare_meeting_spec(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    name = _safe_str(inputs.get("name")) or "Meeting invitation testing experiment"
    experiment_spec_id = _safe_str(inputs.get("experiment_spec_id")) or (
        _actor_scoped_experiment_spec_id(actor=actor, label=name)
    )
    if denial := _owned_experiment_spec_or_denial(
        experiment_spec_id=experiment_spec_id,
        actor=actor,
        allow_new=True,
    ):
        return denial
    result = prepare_meeting_invitation_experiment_spec(
        invitation_text=_safe_str(inputs.get("invitation_text")),
        experiment_spec_id=experiment_spec_id,
        name=name,
        namespace=actor["namespace"],
        user_id=actor["user_id"],
        org_id=actor["org_id"],
        candidate_workflow_ids=inputs.get("candidate_workflow_ids") or [],
        expected_meeting_type=_safe_str(inputs.get("expected_meeting_type")) or None,
        expected_structure_fields=inputs.get("expected_structure_fields") or [],
        expected_downstream_actions=inputs.get("expected_downstream_actions") or [],
    )
    return _result_from_payload(result)


def _handle_testing_prepare_arxiv_fixture(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    actor = _derive_actor_context(request)
    if not _actor_has_durable_authority(actor):
        return _failed_action("workflow_actor_authority_required")
    repair_existing_artifacts = _coerce_bool(
        inputs.get("repair_existing_artifacts"),
        default=False,
    )
    if repair_existing_artifacts and not _arxiv_repair_is_actor_owned(
        inputs=inputs,
        actor=actor,
    ):
        return _failed_action(_TESTING_CLEANUP_AUTHORITY_REQUIRED)
    result = prepare_arxiv_paper_ingestion_test_fixture(
        prompt_text=(
            _safe_str(inputs.get("prompt_text"))
            or _safe_str(inputs.get("prompt"))
            or None
        ),
        arxiv_source=_safe_str(inputs.get("arxiv_source")) or None,
        source_uri=_safe_str(inputs.get("source_uri")) or None,
        arxiv_id=_safe_str(inputs.get("arxiv_id")) or None,
        user_concept_id=actor["user_id"],
        timeout_seconds=_coerce_float(
            inputs.get("timeout_seconds"),
            default=15.0,
            minimum=1.0,
        ),
        repair_existing_artifacts=repair_existing_artifacts,
    )
    return _result_from_payload(result)


def _handle_testing_verify_arxiv_ingestion_result(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    inputs = dict(request.inputs or {})
    result = verify_arxiv_paper_ingestion_test_result(
        workflow_execution=inputs.get("workflow_execution"),
        arxiv_id=_safe_str(inputs.get("arxiv_id")) or None,
        source_uri=_safe_str(inputs.get("source_uri")) or None,
        expected_title=_safe_str(inputs.get("expected_title")) or None,
        expected_summary=_safe_str(inputs.get("expected_summary")) or None,
        expected_publication_date=(
            _safe_str(inputs.get("expected_publication_date")) or None
        ),
        expected_author_names=inputs.get("expected_author_names") or [],
        expected_author_concept_ids=inputs.get("expected_author_concept_ids") or [],
        expected_topic_labels=inputs.get("expected_topic_labels") or [],
        expected_topic_concept_ids=inputs.get("expected_topic_concept_ids") or [],
        paper_concept_id=_safe_str(inputs.get("paper_concept_id")) or None,
    )
    return _result_from_payload(result)


def _handle_testing_cleanup_arxiv_ingestion_artifacts(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    actor = _derive_actor_context(request)
    inputs, denial = _validated_arxiv_cleanup_inputs(request, actor=actor)
    if denial is not None:
        return denial
    assert inputs is not None
    result = cleanup_arxiv_paper_ingestion_test_artifacts(
        paper_concept_id=_safe_str(inputs.get("paper_concept_id")) or None,
        file_copy_concept_id=_safe_str(inputs.get("file_copy_concept_id")) or None,
        file_copy_concept_ids=inputs.get("file_copy_concept_ids") or [],
        author_concept_ids=inputs.get("author_concept_ids") or [],
        topic_concept_ids=inputs.get("topic_concept_ids") or [],
        preexisting_author_concept_ids=(
            inputs.get("preexisting_author_concept_ids") or []
        ),
        preexisting_topic_concept_ids=(
            inputs.get("preexisting_topic_concept_ids") or []
        ),
    )
    return _result_from_payload(result)


def register_testing_workflow_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(action_id=THEORY_CREATE_SLICE_ACTION_ID, handler=_handle_theory_create_slice)
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=THEORY_IMPORT_CANONICAL_CONTEXT_ACTION_ID,
            handler=_handle_theory_import_context,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=THEORY_ASSERT_LOCAL_CLAIM_ACTION_ID,
            handler=_handle_theory_assert_local_claim,
        )
    )
    registry.register_if_absent(
        ActionSpec(action_id=THEORY_COMPUTE_DIFF_ACTION_ID, handler=_handle_theory_compute_diff)
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=THEORY_ROLLBACK_LOCAL_WRITES_ACTION_ID,
            handler=_handle_theory_rollback,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=THEORY_PROMOTE_VALIDATED_CLAIMS_ACTION_ID,
            handler=_handle_theory_promote,
        )
    )
    registry.register_if_absent(
        ActionSpec(action_id=THEORY_GC_EXPIRED_SLICES_ACTION_ID, handler=_handle_theory_gc)
    )
    registry.register_if_absent(
        ActionSpec(action_id=EXPERIMENT_CREATE_SPEC_ACTION_ID, handler=_handle_experiment_create_spec)
    )
    registry.register_if_absent(
        ActionSpec(action_id=EXPERIMENT_START_RUN_ACTION_ID, handler=_handle_experiment_start_run)
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
            handler=_handle_experiment_record_observation,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
            handler=_handle_experiment_compute_verdict,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
            handler=_handle_experiment_emit_learning_signal,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
            handler=_handle_experiment_execute_target_workflow,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
            handler=_handle_experiment_execute_regression_suite,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_VALIDATE_CANDIDATE_WORKFLOW_ACTION_ID,
            handler=_handle_testing_validate_candidate_workflow,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_PREPARE_EXPERIMENT_SPEC_ACTION_ID,
            handler=_handle_testing_prepare_experiment_spec,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
            handler=_handle_testing_prepare_meeting_spec,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_PREPARE_ARXIV_PAPER_INGESTION_FIXTURE_ACTION_ID,
            handler=_handle_testing_prepare_arxiv_fixture,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_VERIFY_ARXIV_PAPER_INGESTION_RESULT_ACTION_ID,
            handler=_handle_testing_verify_arxiv_ingestion_result,
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TESTING_CLEANUP_ARXIV_PAPER_INGESTION_ARTIFACTS_ACTION_ID,
            handler=_handle_testing_cleanup_arxiv_ingestion_artifacts,
        )
    )


__all__ = ["register_testing_workflow_actions"]
