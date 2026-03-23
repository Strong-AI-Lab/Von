"""First-class experiment specification and experiment-run helpers."""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, cast

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure, PyMongoError

from ..db.mongo_client import get_db
from . import concept_service
from .concept_service import (
    ConceptNotFoundError,
    get_concept_by_concept_id_exact,
)
from .namespace_service import parse_namespace, resolve_canonical_namespace
from .relationship_write_service import add_relationship
from .testing_theory_service import compute_testing_theory_diff
from .testing_workflow_contracts import (
    BELONGS_TO_EXPERIMENT_SUITE_PREDICATE_ID,
    EXPERIMENT_CREATE_SPEC_ACTION_ID,
    EXPERIMENT_RUN_SCHEMA_VERSION,
    EXPERIMENT_RUN_STATUSES,
    EXPERIMENT_RUN_STATUS_COMPLETED,
    EXPERIMENT_RUN_STATUS_FAILED,
    EXPERIMENT_RUN_STATUS_PENDING,
    EXPERIMENT_RUN_STATUS_RUNNING,
    EXPERIMENT_RUNS_COLLECTION,
    EXPERIMENT_RUN_TYPE_ID,
    EXPERIMENT_SPEC_SCHEMA_VERSION,
    EXPERIMENT_SPEC_TYPE_ID,
    EXPERIMENT_START_RUN_ACTION_ID,
    EXPERIMENT_VERDICT_FAIL,
    EXPERIMENT_VERDICT_INCONCLUSIVE,
    EXPERIMENT_VERDICT_PARTIAL,
    EXPERIMENT_VERDICT_PASS,
    EXPERIMENT_VERDICTS,
    HAS_EXPERIMENT_VERDICT_PREDICATE_ID,
    HAS_EXPECTED_OUTCOME_PREDICATE_ID,
    HAS_OBSERVED_OUTCOME_PREDICATE_ID,
    INCLUDES_THEORY_PREDICATE_ID,
    TESTS_CAPABILITY_PREDICATE_ID,
    TESTS_WORKFLOW_PREDICATE_ID,
)
from .text_value_service import upsert_singleton_text_relation, upsert_text_for_concept
from .workflow_selection_experience import finalise_selection_experience
from .workflow_vontology_materialisation_helpers import stable_named_instance_concept_id

logger = logging.getLogger(__name__)

_RUN_INDEXES_READY = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _safe_str(value: Any, *, limit: int | None = None) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        text = ""
    else:
        text = str(value).strip()
    return text[:limit] if limit is not None else text


def _clone_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): copy.deepcopy(item) for key, item in value.items()}


def _clone_sequence(value: Any) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return []
    return copy.deepcopy(list(value))


def _normalise_strings(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if isinstance(values, (bytes, bytearray)) or not isinstance(values, Sequence):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


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


def _coerce_int(
    value: Any,
    *,
    default: int = 0,
    minimum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _normalise_verdict(value: Any) -> str | None:
    verdict = _safe_str(value).lower()
    if verdict in EXPERIMENT_VERDICTS:
        return verdict
    return None


def _coerce_datetime_string(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    text = _safe_str(value)
    return text or None


def _coerce_namespace_context(
    *,
    namespace: Any = None,
    user_id: Any = None,
    org_id: Any = None,
) -> tuple[str | None, str | None, str | None]:
    resolved_namespace = resolve_canonical_namespace(namespace, user_id, org_id)
    resolved_user_id = _safe_str(user_id) or None
    resolved_org_id = _safe_str(org_id) or None
    if resolved_namespace:
        try:
            parsed = parse_namespace(resolved_namespace)
        except ValueError:
            parsed = {}
        user_slug = _safe_str(parsed.get("user_id"))
        org_slug = _safe_str(parsed.get("org_id"))
        if user_slug:
            resolved_user_id = f"#V#{user_slug}"
        if org_slug:
            resolved_org_id = f"#V#{org_slug}"
    return resolved_namespace, resolved_user_id, resolved_org_id


def _get_concept_or_none(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id_exact(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _ensure_experiment_spec_concept(
    *,
    experiment_spec_id: str,
    name: str,
    description: str,
    user_id: str | None,
    org_id: str | None,
    namespace: str | None,
) -> tuple[dict[str, Any], bool]:
    existing = _get_concept_or_none(experiment_spec_id)
    if isinstance(existing, Mapping):
        return dict(existing), False

    created = concept_service.create_concept(
        name=name,
        concept_id=experiment_spec_id,
        description=description,
        parent_concept_ids=[EXPERIMENT_SPEC_TYPE_ID],
        create_as_instance=True,
        created_by_concept_id=user_id,
        organisation_concept_id=org_id,
        event_namespace=namespace,
    )
    upsert_singleton_text_relation(
        subject_concept_id=experiment_spec_id,
        predicate="hasDescription",
        text=description,
        lang="en-NZ",
        context={
            "source": "experiment_run_service",
            "reason": EXPERIMENT_CREATE_SPEC_ACTION_ID,
        },
        garbage_collect=True,
    )
    return created, True


def _ensure_experiment_run_concept(
    *,
    run_id: str,
    name: str,
    description: str,
    user_id: str | None,
    org_id: str | None,
    namespace: str | None,
) -> tuple[dict[str, Any], bool]:
    existing = _get_concept_or_none(run_id)
    if isinstance(existing, Mapping):
        return dict(existing), False

    created = concept_service.create_concept(
        name=name,
        concept_id=run_id,
        description=description,
        parent_concept_ids=[EXPERIMENT_RUN_TYPE_ID],
        create_as_instance=True,
        created_by_concept_id=user_id,
        organisation_concept_id=org_id,
        event_namespace=namespace,
    )
    upsert_singleton_text_relation(
        subject_concept_id=run_id,
        predicate="hasDescription",
        text=description,
        lang="en-NZ",
        context={
            "source": "experiment_run_service",
            "reason": EXPERIMENT_START_RUN_ACTION_ID,
        },
        garbage_collect=True,
    )
    return created, True


def _normalise_spec_state(
    raw: Mapping[str, Any] | None,
    *,
    experiment_spec_id: str,
) -> dict[str, Any]:
    payload = _clone_mapping(raw)
    payload["schema_version"] = EXPERIMENT_SPEC_SCHEMA_VERSION
    payload["experiment_spec_id"] = experiment_spec_id
    payload["target_workflow_ids"] = _normalise_strings(payload.get("target_workflow_ids"))
    payload["target_capability_ids"] = _normalise_strings(
        payload.get("target_capability_ids")
    )
    payload["candidate_workflow_ids"] = _normalise_strings(
        payload.get("candidate_workflow_ids")
    )
    payload["expected_outcomes"] = _clone_sequence(payload.get("expected_outcomes"))
    payload["allowed_side_effects"] = _clone_sequence(payload.get("allowed_side_effects"))
    payload["forbidden_side_effects"] = _clone_sequence(
        payload.get("forbidden_side_effects")
    )
    payload["fixture_payload"] = _clone_mapping(payload.get("fixture_payload"))
    payload["theory_setup"] = _clone_mapping(payload.get("theory_setup"))
    payload["verdict_rules"] = _clone_mapping(payload.get("verdict_rules"))
    payload["replay_policy"] = _clone_mapping(payload.get("replay_policy"))
    payload["promotion_policy"] = _clone_mapping(payload.get("promotion_policy"))
    payload["metadata"] = _clone_mapping(payload.get("metadata"))
    return payload


def _normalise_observation(
    raw: Mapping[str, Any],
    *,
    now_iso: str,
) -> dict[str, Any] | None:
    observation_type = _safe_str(
        raw.get("observation_type") or raw.get("type") or raw.get("label"),
        limit=120,
    )
    if not observation_type:
        observation_type = "observation"

    expected_outcome = copy.deepcopy(raw.get("expected_outcome"))
    observed_outcome = copy.deepcopy(raw.get("observed_outcome"))
    explicit_verdict = _normalise_verdict(raw.get("verdict"))
    matched_expected = raw.get("matched_expected_outcome")
    if not isinstance(matched_expected, bool):
        matched_expected = None

    if explicit_verdict is not None:
        verdict = explicit_verdict
    elif isinstance(matched_expected, bool):
        verdict = EXPERIMENT_VERDICT_PASS if matched_expected else EXPERIMENT_VERDICT_FAIL
    elif "success" in raw:
        verdict = (
            EXPERIMENT_VERDICT_PASS
            if _coerce_bool(raw.get("success"))
            else EXPERIMENT_VERDICT_FAIL
        )
    elif expected_outcome is not None and observed_outcome is not None:
        verdict = (
            EXPERIMENT_VERDICT_PASS
            if expected_outcome == observed_outcome
            else EXPERIMENT_VERDICT_FAIL
        )
        matched_expected = expected_outcome == observed_outcome
    elif _coerce_bool(raw.get("partial")):
        verdict = EXPERIMENT_VERDICT_PARTIAL
    else:
        verdict = EXPERIMENT_VERDICT_INCONCLUSIVE

    return {
        "observation_id": _safe_str(raw.get("observation_id"))
        or f"experiment_obs_{uuid.uuid4().hex[:24]}",
        "observation_type": observation_type,
        "label": _safe_str(raw.get("label"), limit=200) or observation_type,
        "verdict": verdict,
        "expected_outcome": expected_outcome,
        "observed_outcome": observed_outcome,
        "matched_expected_outcome": matched_expected,
        "evidence": _clone_mapping(raw.get("evidence")),
        "metrics": _clone_mapping(raw.get("metrics")),
        "policy_decisions": _clone_sequence(raw.get("policy_decisions")),
        "tool_invocations": _clone_sequence(raw.get("tool_invocations")),
        "workflow_execution": _clone_mapping(raw.get("workflow_execution")),
        "turn_execution_request_ids": _normalise_strings(
            raw.get("turn_execution_request_ids")
        ),
        "created_at_utc": _coerce_datetime_string(raw.get("created_at_utc")) or now_iso,
        "updated_at_utc": now_iso,
    }


def _normalise_observations(raw: Any, *, now_iso: str) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        raw = [raw]
    if isinstance(raw, (str, bytes, bytearray)) or not isinstance(raw, Sequence):
        return []

    observations: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        normalised = _normalise_observation(item, now_iso=now_iso)
        if normalised is not None:
            observations.append(normalised)
    return observations


def _normalise_run_state(
    raw: Mapping[str, Any] | None,
    *,
    run_id: str,
) -> dict[str, Any]:
    payload = _clone_mapping(raw)
    payload["schema_version"] = EXPERIMENT_RUN_SCHEMA_VERSION
    payload["run_id"] = run_id
    status = _safe_str(payload.get("status")).lower() or EXPERIMENT_RUN_STATUS_PENDING
    if status not in EXPERIMENT_RUN_STATUSES:
        status = EXPERIMENT_RUN_STATUS_PENDING
    payload["status"] = status
    payload["candidate_workflow_ids"] = _normalise_strings(
        payload.get("candidate_workflow_ids")
    )
    payload["target_workflow_ids"] = _normalise_strings(payload.get("target_workflow_ids"))
    payload["turn_execution_request_ids"] = _normalise_strings(
        payload.get("turn_execution_request_ids")
    )
    payload["expected_outcomes"] = _clone_sequence(payload.get("expected_outcomes"))
    payload["observations"] = _normalise_observations(
        payload.get("observations"),
        now_iso=_safe_str(payload.get("updated_at_utc")) or _utcnow_iso(),
    )
    payload["metrics"] = _clone_mapping(payload.get("metrics"))
    payload["evidence"] = _clone_mapping(payload.get("evidence"))
    payload["verdict_summary"] = _clone_mapping(payload.get("verdict_summary"))
    payload["promotion_recommendation"] = _clone_mapping(
        payload.get("promotion_recommendation")
    )
    payload["learning_signal"] = _clone_mapping(payload.get("learning_signal"))
    payload["replay_case"] = _clone_mapping(payload.get("replay_case"))
    payload["metadata"] = _clone_mapping(payload.get("metadata"))
    payload["selection_experience_id"] = _safe_str(
        payload.get("selection_experience_id")
    ) or None
    payload["benchmark_tier"] = _safe_str(payload.get("benchmark_tier")) or None
    payload["verdict"] = _normalise_verdict(payload.get("verdict"))
    return payload


def _persist_experiment_spec_state(
    *,
    experiment_spec_id: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    normalised = _normalise_spec_state(state, experiment_spec_id=experiment_spec_id)
    concept_service.update_concept(
        experiment_spec_id,
        {
            "concept_data.experiment_spec": normalised,
            "attributes.experiment_spec.updated_at_utc": normalised.get("updated_at_utc"),
            "attributes.experiment_spec.target_workflow_ids": normalised.get(
                "target_workflow_ids"
            ),
        },
    )
    return normalised


def _persist_experiment_run_state(
    *,
    run_id: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    normalised = _normalise_run_state(state, run_id=run_id)
    concept_service.update_concept(
        run_id,
        {
            "concept_data.experiment_run": normalised,
            "attributes.experiment_run.status": normalised.get("status"),
            "attributes.experiment_run.verdict": normalised.get("verdict"),
            "attributes.experiment_run.updated_at_utc": normalised.get("updated_at_utc"),
            "attributes.experiment_run.experiment_spec_id": normalised.get(
                "experiment_spec_id"
            ),
            "attributes.experiment_run.theory_id": normalised.get("theory_id"),
        },
    )
    return normalised


def _build_experiment_run_projection(state: Mapping[str, Any]) -> dict[str, Any]:
    return _normalise_run_state(state, run_id=_safe_str(state.get("run_id")))


def _ensure_run_indexes(collection: Any) -> None:
    global _RUN_INDEXES_READY
    if _RUN_INDEXES_READY:
        return

    try:
        existing_indexes = [idx.get("name") for idx in collection.list_indexes()]
        if "run_id_unique" not in existing_indexes:
            collection.create_index(
                [("run_id", ASCENDING)],
                unique=True,
                name="run_id_unique",
            )
        if "namespace_created_desc" not in existing_indexes:
            collection.create_index(
                [("namespace", ASCENDING), ("created_at_utc", DESCENDING)],
                name="namespace_created_desc",
            )
        if "spec_created_desc" not in existing_indexes:
            collection.create_index(
                [("experiment_spec_id", ASCENDING), ("created_at_utc", DESCENDING)],
                name="spec_created_desc",
            )
        if "theory_created_desc" not in existing_indexes:
            collection.create_index(
                [("theory_id", ASCENDING), ("created_at_utc", DESCENDING)],
                name="theory_created_desc",
            )
        if "workflow_created_desc" not in existing_indexes:
            collection.create_index(
                [("target_workflow_ids", ASCENDING), ("created_at_utc", DESCENDING)],
                name="workflow_created_desc",
            )
        if "verdict_created_desc" not in existing_indexes:
            collection.create_index(
                [("verdict", ASCENDING), ("created_at_utc", DESCENDING)],
                name="verdict_created_desc",
            )
    except OperationFailure as exc:
        logger.warning("Index creation partially failed for experiment_runs: %s", exc)
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not ensure experiment_runs indexes: %s", exc)
    _RUN_INDEXES_READY = True


def get_experiment_runs_collection():
    db = get_db()
    if db is None:
        return None
    coll = db[EXPERIMENT_RUNS_COLLECTION]
    _ensure_run_indexes(coll)
    return coll


def upsert_experiment_run_projection(
    *,
    record: Mapping[str, Any],
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {"updated": False, "reason": "invalid_record"}

    run_id = _safe_str(record.get("run_id"))
    if not run_id:
        return {"updated": False, "reason": "missing_run_id"}

    coll = get_experiment_runs_collection()
    if coll is None:
        return {"updated": False, "reason": "collection_unavailable"}

    now = _utcnow()
    payload = _build_experiment_run_projection(record)
    payload["run_id"] = run_id
    if _safe_str(namespace):
        payload.setdefault("namespace", _safe_str(namespace))
    if _safe_str(user_id):
        payload.setdefault("user_id", _safe_str(user_id))
    if _safe_str(org_id):
        payload.setdefault("org_id", _safe_str(org_id))
    payload.setdefault("schema_version", EXPERIMENT_RUN_SCHEMA_VERSION)
    payload.setdefault("created_at_utc", now.isoformat())
    payload["updated_at_utc"] = now.isoformat()

    try:
        result = coll.update_one(
            {"run_id": run_id},
            {"$set": payload, "$setOnInsert": {"inserted_at": now}},
            upsert=True,
        )
    except PyMongoError as exc:
        logger.warning(
            "Failed to upsert experiment_runs projection for run_id=%s: %s",
            run_id,
            exc,
        )
        return {"updated": False, "reason": "mongo_error", "run_id": run_id}

    updated = bool(getattr(result, "modified_count", 0) > 0)
    inserted = getattr(result, "upserted_id", None) is not None
    matched = bool(getattr(result, "matched_count", 0) > 0)
    return {
        "updated": updated or inserted,
        "inserted": inserted,
        "matched": matched,
        "run_id": run_id,
    }


def get_experiment_spec_state(experiment_spec_id: str) -> dict[str, Any] | None:
    resolved_spec_id = _safe_str(experiment_spec_id)
    if not resolved_spec_id:
        return None
    concept = _get_concept_or_none(resolved_spec_id)
    if not isinstance(concept, Mapping):
        return None
    concept_data = concept.get("concept_data")
    if not isinstance(concept_data, Mapping):
        return None
    state = concept_data.get("experiment_spec")
    if not isinstance(state, Mapping):
        return None
    return _normalise_spec_state(state, experiment_spec_id=resolved_spec_id)


def get_experiment_run_state(run_id: str) -> dict[str, Any] | None:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return None
    concept = _get_concept_or_none(resolved_run_id)
    if not isinstance(concept, Mapping):
        return None
    concept_data = concept.get("concept_data")
    if not isinstance(concept_data, Mapping):
        return None
    state = concept_data.get("experiment_run")
    if not isinstance(state, Mapping):
        return None
    return _normalise_run_state(state, run_id=resolved_run_id)


def get_experiment_run_projection(run_id: str) -> dict[str, Any] | None:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return None

    coll = get_experiment_runs_collection()
    if coll is not None:
        doc = coll.find_one({"run_id": resolved_run_id}, {"_id": 0})
        if isinstance(doc, Mapping):
            return dict(doc)

    state = get_experiment_run_state(resolved_run_id)
    if state is None:
        return None
    return _build_experiment_run_projection(state)


def _record_relation(
    *,
    source_id: str,
    predicate: str,
    targets: Sequence[str],
) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    for target in _normalise_strings(targets):
        try:
            result = add_relationship(source_id=source_id, predicate=predicate, target=target)
        except Exception as exc:
            logger.warning(
                "[experiment_run] could not add relation %s %s %s: %s",
                source_id,
                predicate,
                target,
                exc,
            )
            continue
        if isinstance(result, Mapping):
            relations.append(dict(result))
    return relations


def _record_text_outcomes(
    *,
    source_id: str,
    predicate: str,
    values: Sequence[Any],
) -> None:
    for value in values:
        text = _safe_str(value, limit=2000)
        if not text:
            continue
        try:
            upsert_text_for_concept(
                subject_concept_id=source_id,
                predicate=predicate,
                text=text,
                lang="en-NZ",
                context={"source": "experiment_run_service", "predicate": predicate},
            )
        except Exception as exc:
            logger.warning(
                "[experiment_run] could not upsert %s text for %s: %s",
                predicate,
                source_id,
                exc,
            )


def create_experiment_spec(
    *,
    name: str,
    experiment_spec_id: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    description: str | None = None,
    target_workflow_ids: Sequence[str] = (),
    target_capability_ids: Sequence[str] = (),
    candidate_workflow_ids: Sequence[str] = (),
    theory_id: str | None = None,
    experiment_suite_id: str | None = None,
    fixture_payload: Mapping[str, Any] | None = None,
    theory_setup: Mapping[str, Any] | None = None,
    expected_outcomes: Sequence[Any] = (),
    allowed_side_effects: Sequence[Any] = (),
    forbidden_side_effects: Sequence[Any] = (),
    verdict_rules: Mapping[str, Any] | None = None,
    replay_policy: Mapping[str, Any] | None = None,
    promotion_policy: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_namespace, resolved_user_id, resolved_org_id = _coerce_namespace_context(
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
    )
    spec_name = _safe_str(name, limit=200) or "Testing experiment"
    resolved_spec_id = _safe_str(experiment_spec_id) or stable_named_instance_concept_id(
        spec_name,
        prefix="experiment_spec",
    )
    description_text = _safe_str(description, limit=1200) or f"Experiment spec for {spec_name}."
    now_iso = _utcnow_iso()

    _ensure_experiment_spec_concept(
        experiment_spec_id=resolved_spec_id,
        name=spec_name,
        description=description_text,
        user_id=resolved_user_id,
        org_id=resolved_org_id,
        namespace=resolved_namespace,
    )

    state = {
        "schema_version": EXPERIMENT_SPEC_SCHEMA_VERSION,
        "experiment_spec_id": resolved_spec_id,
        "label": spec_name,
        "description": description_text,
        "namespace": resolved_namespace,
        "user_id": resolved_user_id,
        "org_id": resolved_org_id,
        "theory_id": _safe_str(theory_id) or None,
        "experiment_suite_id": _safe_str(experiment_suite_id) or None,
        "target_workflow_ids": _normalise_strings(target_workflow_ids),
        "target_capability_ids": _normalise_strings(target_capability_ids),
        "candidate_workflow_ids": _normalise_strings(candidate_workflow_ids),
        "fixture_payload": _clone_mapping(fixture_payload),
        "theory_setup": _clone_mapping(theory_setup),
        "expected_outcomes": _clone_sequence(expected_outcomes),
        "allowed_side_effects": _clone_sequence(allowed_side_effects),
        "forbidden_side_effects": _clone_sequence(forbidden_side_effects),
        "verdict_rules": _clone_mapping(verdict_rules),
        "replay_policy": _clone_mapping(replay_policy),
        "promotion_policy": _clone_mapping(promotion_policy),
        "metadata": _clone_mapping(metadata),
        "created_at_utc": now_iso,
        "updated_at_utc": now_iso,
    }
    persisted = _persist_experiment_spec_state(
        experiment_spec_id=resolved_spec_id,
        state=state,
    )

    linked_relations: list[dict[str, Any]] = []
    linked_relations.extend(
        _record_relation(
            source_id=resolved_spec_id,
            predicate=TESTS_WORKFLOW_PREDICATE_ID,
            targets=persisted.get("target_workflow_ids") or [],
        )
    )
    linked_relations.extend(
        _record_relation(
            source_id=resolved_spec_id,
            predicate=TESTS_CAPABILITY_PREDICATE_ID,
            targets=persisted.get("target_capability_ids") or [],
        )
    )
    if _safe_str(theory_id):
        linked_relations.extend(
            _record_relation(
                source_id=resolved_spec_id,
                predicate=INCLUDES_THEORY_PREDICATE_ID,
                targets=[_safe_str(theory_id)],
            )
        )
    if _safe_str(experiment_suite_id):
        linked_relations.extend(
            _record_relation(
                source_id=resolved_spec_id,
                predicate=BELONGS_TO_EXPERIMENT_SUITE_PREDICATE_ID,
                targets=[_safe_str(experiment_suite_id)],
            )
        )
    _record_text_outcomes(
        source_id=resolved_spec_id,
        predicate=HAS_EXPECTED_OUTCOME_PREDICATE_ID,
        values=persisted.get("expected_outcomes") or [],
    )

    return {
        "success": True,
        "experiment_spec_id": resolved_spec_id,
        "experiment_spec": persisted,
        "linked_relations": linked_relations,
    }


def _build_run_id() -> str:
    return f"#V#experiment_run_{uuid.uuid4().hex[:24]}"


def start_experiment_run(
    *,
    experiment_spec_id: str,
    run_id: str | None = None,
    theory_id: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    target_workflow_ids: Sequence[str] = (),
    candidate_workflow_ids: Sequence[str] = (),
    benchmark_tier: str | None = None,
    benchmark_world_id: str | None = None,
    selection_experience_id: str | None = None,
    turn_execution_request_ids: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_spec_id = _safe_str(experiment_spec_id)
    if not resolved_spec_id:
        return {"success": False, "error": "experiment_spec_id_required"}

    spec_state = get_experiment_spec_state(resolved_spec_id)
    if spec_state is None:
        return {"success": False, "error": "experiment_spec_not_found"}

    resolved_namespace, resolved_user_id, resolved_org_id = _coerce_namespace_context(
        namespace=namespace or spec_state.get("namespace"),
        user_id=user_id or spec_state.get("user_id"),
        org_id=org_id or spec_state.get("org_id"),
    )
    resolved_run_id = _safe_str(run_id) or _build_run_id()
    label = _safe_str(spec_state.get("label")) or resolved_spec_id
    description = f"Experiment run for {label}."
    now_iso = _utcnow_iso()

    _ensure_experiment_run_concept(
        run_id=resolved_run_id,
        name=f"{label} run",
        description=description,
        user_id=resolved_user_id,
        org_id=resolved_org_id,
        namespace=resolved_namespace,
    )

    state = {
        "schema_version": EXPERIMENT_RUN_SCHEMA_VERSION,
        "run_id": resolved_run_id,
        "experiment_spec_id": resolved_spec_id,
        "label": f"{label} run",
        "description": description,
        "namespace": resolved_namespace,
        "user_id": resolved_user_id,
        "org_id": resolved_org_id,
        "theory_id": _safe_str(theory_id) or _safe_str(spec_state.get("theory_id")) or None,
        "benchmark_tier": _safe_str(benchmark_tier) or "tier1",
        "benchmark_world_id": _safe_str(benchmark_world_id) or None,
        "target_workflow_ids": _normalise_strings(
            target_workflow_ids or spec_state.get("target_workflow_ids") or []
        ),
        "candidate_workflow_ids": _normalise_strings(
            candidate_workflow_ids or spec_state.get("candidate_workflow_ids") or []
        ),
        "expected_outcomes": _clone_sequence(spec_state.get("expected_outcomes")),
        "turn_execution_request_ids": _normalise_strings(turn_execution_request_ids),
        "selection_experience_id": _safe_str(selection_experience_id)
        or _safe_str(spec_state.get("selection_experience_id"))
        or None,
        "observations": [],
        "metrics": {},
        "evidence": {
            "tool_invocations": [],
            "workflow_execution": [],
            "policy_decisions": [],
            "suite_runs": [],
        },
        "metadata": _clone_mapping(metadata),
        "status": EXPERIMENT_RUN_STATUS_RUNNING,
        "verdict": None,
        "created_at_utc": now_iso,
        "started_at_utc": now_iso,
        "updated_at_utc": now_iso,
    }
    persisted = _persist_experiment_run_state(run_id=resolved_run_id, state=state)
    projection = upsert_experiment_run_projection(
        record=persisted,
        namespace=resolved_namespace,
        user_id=resolved_user_id,
        org_id=resolved_org_id,
    )

    linked_relations: list[dict[str, Any]] = []
    if persisted.get("theory_id"):
        linked_relations.extend(
            _record_relation(
                source_id=resolved_run_id,
                predicate=INCLUDES_THEORY_PREDICATE_ID,
                targets=[_safe_str(persisted.get("theory_id"))],
            )
        )
    linked_relations.extend(
        _record_relation(
            source_id=resolved_run_id,
            predicate=TESTS_WORKFLOW_PREDICATE_ID,
            targets=persisted.get("target_workflow_ids") or [],
        )
    )

    return {
        "success": True,
        "run_id": resolved_run_id,
        "experiment_run": persisted,
        "projection": projection,
        "linked_relations": linked_relations,
    }


def record_experiment_observation(
    *,
    run_id: str,
    observations: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    turn_execution_request_ids: Sequence[str] = (),
) -> dict[str, Any]:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return {"success": False, "error": "run_id_required"}

    state = get_experiment_run_state(resolved_run_id)
    if state is None:
        return {"success": False, "error": "experiment_run_not_found"}

    now_iso = _utcnow_iso()
    appended = _normalise_observations(observations, now_iso=now_iso)
    if not appended:
        return {"success": False, "error": "no_valid_observations"}

    observed_outcomes: list[Any] = []
    evidence: dict[str, Any] = (
        dict(cast(Mapping[str, Any], state.get("evidence")))
        if isinstance(state.get("evidence"), Mapping)
        else {}
    )
    tool_invocations: list[Any] = list(
        cast(Sequence[Any], evidence.get("tool_invocations") or [])
    )
    workflow_execution: list[dict[str, Any]] = [
        copy.deepcopy(dict(item))
        for item in cast(Sequence[Any], evidence.get("workflow_execution") or [])
        if isinstance(item, Mapping)
    ]
    policy_decisions: list[Any] = list(
        cast(Sequence[Any], evidence.get("policy_decisions") or [])
    )
    turn_ids = _normalise_strings(
        [
            *state.get("turn_execution_request_ids", []),
            *turn_execution_request_ids,
            *[
                request_id
                for item in appended
                for request_id in item.get("turn_execution_request_ids") or []
            ],
        ]
    )

    for item in appended:
        observed_outcome = item.get("observed_outcome")
        if observed_outcome is not None:
            observed_outcomes.append(observed_outcome)
        tool_invocations.extend(_clone_sequence(item.get("tool_invocations")))
        workflow_payload = item.get("workflow_execution")
        if isinstance(workflow_payload, Mapping) and workflow_payload:
            workflow_execution.append(copy.deepcopy(dict(workflow_payload)))
        policy_decisions.extend(_clone_sequence(item.get("policy_decisions")))

    state["observations"] = [*(state.get("observations") or []), *appended]
    state["turn_execution_request_ids"] = turn_ids
    state["evidence"] = {
        **evidence,
        "tool_invocations": tool_invocations,
        "workflow_execution": workflow_execution,
        "policy_decisions": policy_decisions,
    }
    state["updated_at_utc"] = now_iso
    persisted = _persist_experiment_run_state(run_id=resolved_run_id, state=state)
    projection = upsert_experiment_run_projection(
        record=persisted,
        namespace=_safe_str(persisted.get("namespace")) or None,
        user_id=_safe_str(persisted.get("user_id")) or None,
        org_id=_safe_str(persisted.get("org_id")) or None,
    )
    _record_text_outcomes(
        source_id=resolved_run_id,
        predicate=HAS_OBSERVED_OUTCOME_PREDICATE_ID,
        values=observed_outcomes,
    )

    return {
        "success": True,
        "run_id": resolved_run_id,
        "recorded_observations": appended,
        "experiment_run": persisted,
        "projection": projection,
    }


def _compute_observation_verdict_counts(
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    counts = {
        EXPERIMENT_VERDICT_PASS: 0,
        EXPERIMENT_VERDICT_FAIL: 0,
        EXPERIMENT_VERDICT_PARTIAL: 0,
        EXPERIMENT_VERDICT_INCONCLUSIVE: 0,
    }
    for item in observations:
        verdict = _normalise_verdict(item.get("verdict"))
        if verdict is None:
            verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
        counts[verdict] = counts.get(verdict, 0) + 1
    return counts


def _has_forbidden_side_effect(
    *,
    spec_state: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
) -> bool:
    forbidden_terms = {
        _safe_str(item).lower()
        for item in spec_state.get("forbidden_side_effects") or []
        if _safe_str(item)
    }
    if not forbidden_terms:
        return False

    for observation in observations:
        evidence = observation.get("evidence")
        evidence_dict = evidence if isinstance(evidence, Mapping) else {}
        policy_decisions = observation.get("policy_decisions")
        policy_list = policy_decisions if isinstance(policy_decisions, list) else []
        observed = []
        observed.extend(_normalise_strings(evidence_dict.get("side_effects")))
        observed.extend(
            _safe_str(item.get("decision") or item.get("type"))
            for item in policy_list
            if isinstance(item, Mapping)
        )
        for item in observed:
            if item.lower() in forbidden_terms:
                return True
    return False


def compute_experiment_verdict(
    *,
    run_id: str,
) -> dict[str, Any]:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return {"success": False, "error": "run_id_required"}

    state = get_experiment_run_state(resolved_run_id)
    if state is None:
        return {"success": False, "error": "experiment_run_not_found"}

    spec_state = get_experiment_spec_state(_safe_str(state.get("experiment_spec_id")))
    if spec_state is None:
        return {"success": False, "error": "experiment_spec_not_found"}

    observations = [
        item for item in (state.get("observations") or []) if isinstance(item, Mapping)
    ]
    counts = _compute_observation_verdict_counts(observations)
    total = sum(counts.values())
    require_all_expected = _coerce_bool(
        (spec_state.get("verdict_rules") or {}).get("require_all_expected_outcomes"),
        default=False,
    )
    minimum_pass_count = _coerce_int(
        (spec_state.get("verdict_rules") or {}).get("minimum_pass_count"),
        default=1,
        minimum=1,
    )
    expected_outcome_count = len(spec_state.get("expected_outcomes") or [])
    pass_count = counts.get(EXPERIMENT_VERDICT_PASS, 0)
    fail_count = counts.get(EXPERIMENT_VERDICT_FAIL, 0)
    partial_count = counts.get(EXPERIMENT_VERDICT_PARTIAL, 0)
    inconclusive_count = counts.get(EXPERIMENT_VERDICT_INCONCLUSIVE, 0)
    has_forbidden_side_effect = _has_forbidden_side_effect(
        spec_state=spec_state,
        observations=observations,
    )

    if total == 0:
        verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
        reason = "no_observations_recorded"
    elif has_forbidden_side_effect:
        verdict = EXPERIMENT_VERDICT_FAIL
        reason = "forbidden_side_effect_observed"
    elif fail_count == 0 and partial_count == 0 and inconclusive_count == 0:
        if require_all_expected and expected_outcome_count > 0 and pass_count < expected_outcome_count:
            verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
            reason = "expected_outcomes_not_fully_observed"
        elif pass_count >= minimum_pass_count:
            verdict = EXPERIMENT_VERDICT_PASS
            reason = "all_recorded_observations_passed"
        else:
            verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
            reason = "minimum_pass_count_not_met"
    elif fail_count > 0 and pass_count == 0 and partial_count == 0:
        verdict = EXPERIMENT_VERDICT_FAIL
        reason = "all_recorded_observations_failed"
    elif pass_count > 0 or partial_count > 0:
        verdict = EXPERIMENT_VERDICT_PARTIAL
        reason = "mixed_or_partial_observation_outcomes"
    else:
        verdict = EXPERIMENT_VERDICT_INCONCLUSIVE
        reason = "insufficient_observation_signal"

    promotion_ready_assertion_ids: list[str] = []
    theory_id = _safe_str(state.get("theory_id"))
    if theory_id and verdict == EXPERIMENT_VERDICT_PASS:
        try:
            diff_result = compute_testing_theory_diff(theory_id=theory_id)
            diff = diff_result.get("diff") if isinstance(diff_result, Mapping) else None
            if isinstance(diff, Mapping):
                promotion_ready_assertion_ids = _normalise_strings(
                    diff.get("promotion_ready_assertion_ids")
                )
        except Exception as exc:
            logger.warning(
                "[experiment_run] could not compute theory diff for %s: %s",
                theory_id,
                exc,
            )

    summary = {
        "reason": reason,
        "observation_counts": counts,
        "observation_total": total,
        "expected_outcome_count": expected_outcome_count,
        "has_forbidden_side_effect": has_forbidden_side_effect,
    }
    promotion_recommendation = {
        "recommended": verdict == EXPERIMENT_VERDICT_PASS,
        "requires_promotion_gate": True,
        "theory_id": theory_id or None,
        "promotion_ready_assertion_ids": promotion_ready_assertion_ids,
        "reason": (
            "pass_with_promotable_theory_assertions"
            if verdict == EXPERIMENT_VERDICT_PASS and promotion_ready_assertion_ids
            else "explicit_promotion_gate_required"
        ),
    }
    now_iso = _utcnow_iso()

    state["verdict"] = verdict
    state["verdict_summary"] = summary
    state["promotion_recommendation"] = promotion_recommendation
    state["status"] = (
        EXPERIMENT_RUN_STATUS_COMPLETED
        if verdict != EXPERIMENT_VERDICT_FAIL
        else EXPERIMENT_RUN_STATUS_FAILED
    )
    state["completed_at_utc"] = now_iso
    state["updated_at_utc"] = now_iso
    existing_metrics: dict[str, Any] = (
        dict(cast(Mapping[str, Any], state.get("metrics")))
        if isinstance(state.get("metrics"), Mapping)
        else {}
    )
    state["metrics"] = {
        **existing_metrics,
        "observation_total": total,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "partial_count": partial_count,
        "inconclusive_count": inconclusive_count,
        "expected_outcome_count": expected_outcome_count,
    }

    persisted = _persist_experiment_run_state(run_id=resolved_run_id, state=state)
    projection = upsert_experiment_run_projection(
        record=persisted,
        namespace=_safe_str(persisted.get("namespace")) or None,
        user_id=_safe_str(persisted.get("user_id")) or None,
        org_id=_safe_str(persisted.get("org_id")) or None,
    )
    try:
        upsert_singleton_text_relation(
            subject_concept_id=resolved_run_id,
            predicate=HAS_EXPERIMENT_VERDICT_PREDICATE_ID,
            text=verdict,
            lang="en-NZ",
            context={"source": "experiment_run_service", "reason": "compute_verdict"},
            garbage_collect=True,
        )
    except Exception as exc:
        logger.warning(
            "[experiment_run] could not write verdict text relation for %s: %s",
            resolved_run_id,
            exc,
        )

    return {
        "success": True,
        "run_id": resolved_run_id,
        "verdict": verdict,
        "verdict_summary": summary,
        "promotion_recommendation": promotion_recommendation,
        "experiment_run": persisted,
        "projection": projection,
    }


def _build_candidate_workflow_rows(
    *,
    candidate_workflow_ids: Sequence[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for workflow_id in candidate_workflow_ids:
        cleaned = _safe_str(workflow_id)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        rows.append({"concept_id": cleaned, "name": cleaned, "description": ""})
    return rows


def _verdict_to_selection_outcome(verdict: str | None) -> str:
    if verdict == EXPERIMENT_VERDICT_PASS:
        return "completed"
    if verdict == EXPERIMENT_VERDICT_FAIL:
        return "failed"
    if verdict == EXPERIMENT_VERDICT_PARTIAL:
        return "partial"
    return "follow_up_required"


def emit_experiment_learning_signal(
    *,
    run_id: str,
    selection_experience_id: str | None = None,
    turn_text: str | None = None,
    expected_workflow_id: str | None = None,
    baseline_workflow_id: str | None = None,
) -> dict[str, Any]:
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id:
        return {"success": False, "error": "run_id_required"}

    state = get_experiment_run_state(resolved_run_id)
    if state is None:
        return {"success": False, "error": "experiment_run_not_found"}
    spec_state = get_experiment_spec_state(_safe_str(state.get("experiment_spec_id")))
    if spec_state is None:
        return {"success": False, "error": "experiment_spec_not_found"}

    verdict = _normalise_verdict(state.get("verdict"))
    if verdict is None:
        verdict_result = compute_experiment_verdict(run_id=resolved_run_id)
        if not verdict_result.get("success"):
            return verdict_result
        state = verdict_result.get("experiment_run") or state
        verdict = _normalise_verdict((state or {}).get("verdict"))

    resolved_selection_experience_id = (
        _safe_str(selection_experience_id)
        or _safe_str(state.get("selection_experience_id"))
        or None
    )
    candidate_workflow_ids = _normalise_strings(
        [
            *(state.get("candidate_workflow_ids") or []),
            *(state.get("target_workflow_ids") or []),
        ]
    )
    expected_id = _safe_str(expected_workflow_id) or _safe_str(
        next(iter(state.get("target_workflow_ids") or []), None)
    )
    baseline_id = _safe_str(baseline_workflow_id) or (
        candidate_workflow_ids[0] if candidate_workflow_ids else expected_id
    )
    spec_fixture: dict[str, Any] = (
        dict(cast(Mapping[str, Any], spec_state.get("fixture_payload")))
        if isinstance(spec_state.get("fixture_payload"), Mapping)
        else {}
    )
    turn_text_value = _safe_str(turn_text, limit=2000) or _safe_str(
        spec_fixture.get("invitation_text") or spec_fixture.get("turn_text"),
        limit=2000,
    )
    replay_case = {
        "case_id": f"experiment_case_{resolved_run_id[3:] if resolved_run_id.startswith('#V#') else resolved_run_id}",
        "run_id": resolved_run_id,
        "experiment_spec_id": _safe_str(state.get("experiment_spec_id")) or None,
        "turn_text": turn_text_value,
        "expected_workflow_id": expected_id or None,
        "baseline_workflow_id": baseline_id or None,
        "candidate_workflows": _build_candidate_workflow_rows(
            candidate_workflow_ids=candidate_workflow_ids
        ),
        "verdict": verdict,
        "evidence": {
            "turn_execution_request_ids": _normalise_strings(
                state.get("turn_execution_request_ids")
            ),
            "observation_total": _coerce_int(
                (state.get("metrics") or {}).get("observation_total"),
                default=len(state.get("observations") or []),
                minimum=0,
            ),
        },
    }
    outcome_metadata = {
        "experiment_run_id": resolved_run_id,
        "experiment_spec_id": _safe_str(state.get("experiment_spec_id")) or None,
        "experiment_verdict": verdict,
        "completion_gate_requires_follow_up": verdict != EXPERIMENT_VERDICT_PASS,
        "turn_execution_request_ids": replay_case["evidence"]["turn_execution_request_ids"],
    }
    selection_update = None
    if resolved_selection_experience_id:
        selection_update = finalise_selection_experience(
            experience_id=resolved_selection_experience_id,
            outcome=_verdict_to_selection_outcome(verdict),
            outcome_metadata=outcome_metadata,
        )

    now_iso = _utcnow_iso()
    learning_signal = {
        "schema_version": "experiment_learning_signal.v1",
        "emitted_at_utc": now_iso,
        "selection_experience_id": resolved_selection_experience_id,
        "selection_outcome": _verdict_to_selection_outcome(verdict),
        "selection_reward": (
            float(selection_update.reward)
            if selection_update is not None and selection_update.reward is not None
            else None
        ),
        "replay_case": replay_case,
    }

    state["learning_signal"] = learning_signal
    state["replay_case"] = replay_case
    state["updated_at_utc"] = now_iso
    persisted = _persist_experiment_run_state(run_id=resolved_run_id, state=state)
    projection = upsert_experiment_run_projection(
        record=persisted,
        namespace=_safe_str(persisted.get("namespace")) or None,
        user_id=_safe_str(persisted.get("user_id")) or None,
        org_id=_safe_str(persisted.get("org_id")) or None,
    )
    return {
        "success": True,
        "run_id": resolved_run_id,
        "learning_signal": learning_signal,
        "selection_experience": selection_update.to_dict()
        if selection_update is not None
        else None,
        "experiment_run": persisted,
        "projection": projection,
    }


def prepare_meeting_invitation_experiment_spec(
    *,
    invitation_text: str,
    experiment_spec_id: str | None = None,
    name: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    candidate_workflow_ids: Sequence[str] = (),
    expected_meeting_type: str | None = None,
    expected_structure_fields: Sequence[str] = (),
    expected_downstream_actions: Sequence[str] = (),
) -> dict[str, Any]:
    invitation = _safe_str(invitation_text, limit=8000)
    if not invitation:
        return {"success": False, "error": "invitation_text_required"}

    resolved_candidate_workflow_ids = _normalise_strings(candidate_workflow_ids)
    spec_name = (
        _safe_str(name, limit=200)
        or "Meeting invitation workflow derivation and validation"
    )
    expected_outcomes: list[dict[str, Any]] = [
        {
            "label": "meeting_type_classification",
            "expected": _safe_str(expected_meeting_type) or "meeting_workflow_candidate",
        },
        {
            "label": "structured_meeting_fields",
            "expected_fields": _normalise_strings(expected_structure_fields)
            or ["title", "time", "participants"],
        },
        {
            "label": "mutation_safety",
            "expected": "no_canonical_mutations_without_gate",
        },
    ]
    if expected_downstream_actions:
        expected_outcomes.append(
            {
                "label": "downstream_actions",
                "expected_actions": _normalise_strings(expected_downstream_actions),
            }
        )

    theory_setup = {
        "seed_claims": [
            {
                "source_id": "#V#meeting_invitation_testing_workflow",
                "predicate": "#V#has_hypothesis",
                "target": _safe_str(expected_meeting_type)
                or "meeting invitation should resolve to a safe workflow candidate",
                "target_kind": "text",
            },
            {
                "source_id": "#V#meeting_invitation_testing_workflow",
                "predicate": "#V#has_assumption",
                "target": "No canonical calendar or task mutation is permitted during testing.",
                "target_kind": "text",
            },
        ],
        "expected_observations": expected_outcomes,
    }
    result = create_experiment_spec(
        name=spec_name,
        experiment_spec_id=experiment_spec_id,
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
        description=(
            "Testing spec for deriving, selecting, and validating a meeting-invitation workflow."
        ),
        candidate_workflow_ids=resolved_candidate_workflow_ids,
        target_workflow_ids=resolved_candidate_workflow_ids[:1],
        fixture_payload={"invitation_text": invitation},
        theory_setup=theory_setup,
        expected_outcomes=expected_outcomes,
        forbidden_side_effects=[
            "canonical_calendar_mutation",
            "canonical_task_mutation",
            "external_action_without_gate",
        ],
        verdict_rules={
            "require_all_expected_outcomes": True,
            "minimum_pass_count": len(expected_outcomes),
        },
        replay_policy={"retain_failing_cases": True, "retain_passing_cases": False},
        promotion_policy={"requires_manual_gate": True},
        metadata={"scenario": "meeting_invitation_testing"},
    )
    if not result.get("success"):
        return result
    return {
        **result,
        "theory_slice_inputs": {
            "name": f"{spec_name} theory slice",
            "experiment_spec_id": result.get("experiment_spec_id"),
            "expected_observations": expected_outcomes,
            "promotion_policy": {"requires_manual_gate": True},
        },
        "seed_claims": theory_setup["seed_claims"],
    }


def execute_regression_suite(
    *,
    execution_tier: str = "tier1",
    cases: Sequence[Mapping[str, Any]] = (),
    benchmark_scenario: Mapping[str, Any] | None = None,
    output_root: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    tier = _safe_str(execution_tier).lower() or "tier1"
    if tier in {"tier2", "benchmark", "tier_2"}:
        if not isinstance(benchmark_scenario, Mapping):
            return {"success": False, "error": "benchmark_scenario_required_for_tier2"}
        try:
            from .kb_clone_benchmark_service import (
                build_default_benchmark_app,
                run_kb_clone_benchmark,
            )

            db = get_db()
            mongo_client = getattr(db, "client", None) if db is not None else None
            if mongo_client is None:
                return {"success": False, "error": "mongo_client_unavailable"}
            result = run_kb_clone_benchmark(
                scenario=benchmark_scenario,
                output_root=output_root or "data/testing_workflows/benchmarks",
                app=build_default_benchmark_app(),
                mongo_client=mongo_client,
            )
        except Exception as exc:
            logger.warning("[experiment_run] Tier 2 benchmark execution failed: %s", exc)
            return {"success": False, "error": "benchmark_execution_failed", "details": str(exc)}
        aggregate = result.get("metrics", {}).get("aggregate", {})
        result = {
            "success": True,
            "execution_tier": "tier2",
            "suite_result": result,
            "verdict": (
                EXPERIMENT_VERDICT_PASS
                if _coerce_bool(aggregate.get("all_runs_passed"))
                else EXPERIMENT_VERDICT_PARTIAL
            ),
        }
        return _maybe_record_suite_observation(
            result=result,
            run_id=run_id,
        )

    case_rows = [dict(item) for item in cases if isinstance(item, Mapping)]
    if not case_rows:
        return {"success": False, "error": "cases_required_for_tier1_suite"}

    pass_count = 0
    fail_count = 0
    for row in case_rows:
        expected = _normalise_verdict(row.get("expected_verdict")) or EXPERIMENT_VERDICT_PASS
        observed = _normalise_verdict(row.get("observed_verdict") or row.get("verdict"))
        if observed is None:
            observed = EXPERIMENT_VERDICT_INCONCLUSIVE
        row["expected_verdict"] = expected
        row["observed_verdict"] = observed
        row["case_passed"] = observed == expected
        if row["case_passed"]:
            pass_count += 1
        else:
            fail_count += 1

    verdict = (
        EXPERIMENT_VERDICT_PASS
        if fail_count == 0
        else EXPERIMENT_VERDICT_FAIL
        if pass_count == 0
        else EXPERIMENT_VERDICT_PARTIAL
    )
    result = {
        "success": True,
        "execution_tier": "tier1",
        "suite_result": {
            "case_count": len(case_rows),
            "pass_count": pass_count,
            "fail_count": fail_count,
            "pass_rate": round(pass_count / max(1, len(case_rows)), 4),
            "cases": case_rows,
        },
        "verdict": verdict,
    }
    return _maybe_record_suite_observation(
        result=result,
        run_id=run_id,
    )


def _maybe_record_suite_observation(
    *,
    result: Mapping[str, Any],
    run_id: str | None,
) -> dict[str, Any]:
    payload = dict(result)
    resolved_run_id = _safe_str(run_id)
    if not resolved_run_id or not payload.get("success"):
        return payload

    execution_tier = _safe_str(payload.get("execution_tier")) or "tier1"
    verdict = _normalise_verdict(payload.get("verdict")) or EXPERIMENT_VERDICT_INCONCLUSIVE
    suite_result = payload.get("suite_result")
    suite_metrics: Mapping[str, Any] = {}
    if isinstance(suite_result, Mapping):
        nested_metrics = suite_result.get("metrics")
        if isinstance(nested_metrics, Mapping):
            suite_metrics = nested_metrics
        else:
            suite_metrics = suite_result
    recorded = record_experiment_observation(
        run_id=resolved_run_id,
        observations=[
            {
                "label": f"regression_suite_{execution_tier}",
                "verdict": verdict,
                "observed_outcome": verdict,
                "evidence": {
                    "suite_result": copy.deepcopy(suite_result),
                    "execution_tier": execution_tier,
                },
                "metrics": dict(suite_metrics),
            }
        ],
    )
    if not recorded.get("success"):
        return {
            **payload,
            "success": False,
            "error": "experiment_run_observation_record_failed",
            "observation_recording": recorded,
        }

    return {
        **payload,
        "observation_recording": recorded,
    }


__all__ = [
    "compute_experiment_verdict",
    "create_experiment_spec",
    "emit_experiment_learning_signal",
    "execute_regression_suite",
    "get_experiment_run_projection",
    "get_experiment_run_state",
    "get_experiment_runs_collection",
    "get_experiment_spec_state",
    "prepare_meeting_invitation_experiment_spec",
    "record_experiment_observation",
    "start_experiment_run",
    "upsert_experiment_run_projection",
]
