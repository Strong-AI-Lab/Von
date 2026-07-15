"""Canonical Vontology persistence for immutable cohort certification evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import threading
from typing import Any
import uuid

from . import concept_service
from .concept_service import ConceptNotFoundError
from .operational_certification_cohort_aggregate_service import (
    ExperimentRunLoader,
    OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_SCHEMA_VERSION,
    OperationalCertificationCohortAggregateError,
    build_operational_certification_cohort_aggregate,
    operational_certification_cohort_aggregate_concept_id,
    validate_operational_certification_cohort_aggregate,
)
from .operational_certification_contract_service import (
    OperationalCertificationContract,
    json_serialisable_projection,
)
from .text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
)


OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_TYPE_ID = (
    "#V#operational_certification_cohort_aggregate"
)
OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_PREDICATE = "hasContent"

_MANAGED_BY = "operational_certification_cohort_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2575"
_LANGUAGE = "en-NZ"
_WRITER_LEASE_NAME = "operational_certification_cohort_aggregate_writer"
_WRITER_OWNER_TOKEN = f"operational-certification-cohort-{uuid.uuid4().hex}"
_LOCKS_GUARD = threading.Lock()
_AGGREGATE_LOCKS: dict[str, threading.RLock] = {}


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OperationalCertificationCohortAggregateError(code)
    try:
        return json_serialisable_projection(value)
    except TypeError as exc:
        raise OperationalCertificationCohortAggregateError(code) from exc


def _lock_for(concept_id: str) -> threading.RLock:
    with _LOCKS_GUARD:
        return _AGGREGATE_LOCKS.setdefault(concept_id, threading.RLock())


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = concept_service.get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _aggregate_identity_attributes(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    authority = _mapping(
        aggregate.get("authority"),
        code="cohort_aggregate_authority_invalid",
    )
    cohort = _mapping(
        aggregate.get("cohort"),
        code="cohort_aggregate_policy_invalid",
    )
    return {
        "aggregate_identity_sha256": aggregate.get("aggregate_identity_sha256"),
        "aggregate_sha256": aggregate.get("aggregate_sha256"),
        "suite_id": authority.get("suite_id"),
        "suite_concept_id": authority.get("suite_concept_id"),
        "case_set": authority.get("case_set"),
        "suite_source": authority.get("suite_source"),
        "source_definition_sha256": authority.get("source_definition_sha256"),
        "contract_sha256": authority.get("contract_sha256"),
        "organisation_concept_id": cohort.get("organisation_concept_id"),
        "actor_concept_ids": cohort.get("actor_concept_ids"),
        "aggregation_policy": cohort.get("aggregation_policy"),
    }


def _validate_aggregate_concept(
    concept: Mapping[str, Any],
    *,
    concept_id: str,
    aggregate: Mapping[str, Any] | None = None,
) -> None:
    if concept.get("concept_id") != concept_id:
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_concept_id_mismatch"
        )
    attributes = concept.get("attributes")
    represented_identity = (
        attributes.get("operational_certification_cohort_aggregate_identity")
        if isinstance(attributes, Mapping)
        else None
    )
    if not isinstance(represented_identity, Mapping):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_concept_identity_missing"
        )
    if aggregate is not None and represented_identity != _aggregate_identity_attributes(
        aggregate
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_concept_identity_mismatch"
        )


def _ensure_aggregate_concept(
    *,
    aggregate: Mapping[str, Any],
    concept_id: str,
) -> None:
    if _safe_get_concept(OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_TYPE_ID) is None:
        try:
            concept_service.create_concept(
                name="Operational certification cohort aggregate",
                concept_id=OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_TYPE_ID,
                description=(
                    "Type for immutable aggregates of independently certified "
                    "actor campaign dossiers under represented cohort policy."
                ),
                parent_concept_ids=["#V#thing"],
                create_as_instance=False,
                visibility_scope_mode="global_general",
            )
        except Exception:
            if (
                _safe_get_concept(
                    OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_TYPE_ID
                )
                is None
            ):
                raise
    concept = _safe_get_concept(concept_id)
    if concept is None:
        cohort = _mapping(
            aggregate.get("cohort"),
            code="cohort_aggregate_policy_invalid",
        )
        org_id = _clean(cohort.get("organisation_concept_id"))
        try:
            concept_service.create_concept(
                name="Operational certification cohort aggregate",
                concept_id=concept_id,
                description=(
                    "Immutable read-back record binding all independently "
                    "certified actor campaigns required by represented cohort policy."
                ),
                attributes={
                    "managed_by": _MANAGED_BY,
                    "schema_version": (
                        OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_SCHEMA_VERSION
                    ),
                    "operational_certification_cohort_aggregate_identity": (
                        _aggregate_identity_attributes(aggregate)
                    ),
                },
                parent_concept_ids=[
                    OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_TYPE_ID
                ],
                create_as_instance=True,
                organisation_concept_id=org_id,
                visibility_scope_mode="organisation_general",
            )
        except Exception:
            concept = _safe_get_concept(concept_id)
            if concept is None:
                raise
        else:
            concept = _safe_get_concept(concept_id)
    if not isinstance(concept, Mapping):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_concept_create_failed"
        )
    _validate_aggregate_concept(
        concept,
        concept_id=concept_id,
        aggregate=aggregate,
    )


def _load_aggregate_text(concept_id: str) -> dict[str, Any] | None:
    rows = get_texts_for_concept(
        concept_id,
        predicate=OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_PREDICATE,
        lang=_LANGUAGE,
        limit=10,
    )
    texts = [
        _clean(row.get("text"))
        for row in rows
        if isinstance(row, Mapping) and _clean(row.get("text"))
    ]
    if len(texts) > 1:
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_record_ambiguous",
            details={"concept_id": concept_id, "record_count": len(texts)},
        )
    if not texts:
        return None
    try:
        raw = json.loads(texts[0])
    except (TypeError, ValueError) as exc:
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_record_malformed"
        ) from exc
    if not isinstance(raw, Mapping):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_record_malformed"
        )
    return json_serialisable_projection(raw)


def load_operational_certification_cohort_aggregate(
    *,
    concept_id: str,
    contract: OperationalCertificationContract,
) -> dict[str, Any] | None:
    """Load and validate one immutable cohort aggregate from Vontology."""

    resolved_concept_id = _clean(concept_id)
    if not resolved_concept_id.startswith(
        "#V#operational_certification_cohort_aggregate_"
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_concept_id_invalid"
        )
    concept = _safe_get_concept(resolved_concept_id)
    if concept is None:
        return None
    _validate_aggregate_concept(concept, concept_id=resolved_concept_id)
    raw_aggregate = _load_aggregate_text(resolved_concept_id)
    if raw_aggregate is None:
        return None
    aggregate = validate_operational_certification_cohort_aggregate(
        raw_aggregate,
        contract=contract,
    )
    expected_concept_id = operational_certification_cohort_aggregate_concept_id(
        aggregate
    )
    if expected_concept_id != resolved_concept_id:
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_concept_identity_mismatch"
        )
    _validate_aggregate_concept(
        concept,
        concept_id=resolved_concept_id,
        aggregate=aggregate,
    )
    return aggregate


def persist_operational_certification_cohort_aggregate(
    *,
    contract: OperationalCertificationContract,
    actor_campaign_executions: Sequence[Mapping[str, Any]],
    experiment_run_loader: ExperimentRunLoader | None = None,
) -> dict[str, Any]:
    """Validate, persist once, and read back an immutable cohort aggregate."""

    aggregate = build_operational_certification_cohort_aggregate(
        contract=contract,
        actor_campaign_executions=actor_campaign_executions,
        experiment_run_loader=experiment_run_loader,
    )
    concept_id = operational_certification_cohort_aggregate_concept_id(aggregate)
    _ensure_aggregate_concept(aggregate=aggregate, concept_id=concept_id)
    lease = concept_service.acquire_concept_mutation_lease(
        concept_id=concept_id,
        lease_name=_WRITER_LEASE_NAME,
        owner_token=_WRITER_OWNER_TOKEN,
        ttl_seconds=120,
    )
    if lease.get("success") is not True:
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_single_writer_unavailable",
            details={"concept_id": concept_id},
            recovery_affordances=(
                {"action_type": "read_existing_cohort_aggregate"},
                {"action_type": "retry_after_writer_lease"},
            ),
        )
    try:
        with _lock_for(concept_id):
            existing = load_operational_certification_cohort_aggregate(
                concept_id=concept_id,
                contract=contract,
            )
            if existing is not None:
                if existing != aggregate:
                    raise OperationalCertificationCohortAggregateError(
                        "cohort_aggregate_immutable_conflict",
                        details={"concept_id": concept_id},
                        recovery_affordances=(
                            {"action_type": "read_existing_cohort_aggregate"},
                            {
                                "action_type": (
                                    "persist_new_actor_campaign_aggregate_identity"
                                )
                            },
                        ),
                    )
                return {
                    "success": True,
                    "persisted": True,
                    "idempotent": True,
                    "aggregate_concept_id": concept_id,
                    "aggregate": existing,
                }
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate=OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_PREDICATE,
                text=json.dumps(
                    aggregate,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                lang=_LANGUAGE,
                policy="replace_others",
                provenance={
                    "source": _MANAGED_BY,
                    "jira": _SOURCE_TAG,
                    "aggregate_sha256": aggregate["aggregate_sha256"],
                },
                context={
                    "operation": "persist_immutable_cohort_aggregate",
                    "aggregate_identity_sha256": aggregate[
                        "aggregate_identity_sha256"
                    ],
                },
                garbage_collect=True,
            )
            readback = load_operational_certification_cohort_aggregate(
                concept_id=concept_id,
                contract=contract,
            )
            if readback != aggregate:
                raise OperationalCertificationCohortAggregateError(
                    "cohort_aggregate_readback_mismatch",
                    details={"concept_id": concept_id},
                    recovery_affordances=(
                        {"action_type": "read_existing_cohort_aggregate"},
                        {"action_type": "retry_immutable_aggregate_persistence"},
                    ),
                )
            return {
                "success": True,
                "persisted": True,
                "idempotent": False,
                "aggregate_concept_id": concept_id,
                "aggregate": readback,
            }
    finally:
        try:
            concept_service.release_concept_mutation_lease(
                concept_id=concept_id,
                lease_name=_WRITER_LEASE_NAME,
                owner_token=_WRITER_OWNER_TOKEN,
            )
        except Exception:
            pass


__all__ = [
    "OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_PREDICATE",
    "OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_TYPE_ID",
    "load_operational_certification_cohort_aggregate",
    "persist_operational_certification_cohort_aggregate",
]
