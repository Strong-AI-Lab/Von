"""Generic actions for academic-roster reconciliation and appointment claims.

The represented workflows own source discovery, cohort interpretation, and
semantic extraction.  These actions deliberately know nothing about particular
institutions or academic-title taxonomies: they validate a source snapshot,
write exact source-provenanced records idempotently, and reconcile terminal
per-row outcomes.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from ...services.scoped_assertion_service import upsert_scoped_assertion
from ...services.workflow_event_integration_service import resolve_event_actor_context
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)

ACADEMIC_ROSTER_VALIDATE_ACTION_ID = "academic_roster.validate_snapshot"
ACADEMIC_APPOINTMENT_ASSERT_ACTION_ID = "academic_appointment.assert_record"
ACADEMIC_APPOINTMENT_FINALISE_NON_INCLUDED_ACTION_ID = (
    "academic_appointment.finalise_non_included"
)
ACADEMIC_ROSTER_RECONCILE_ACTION_ID = "academic_roster.reconcile_results"

ACADEMIC_APPOINTMENT_PREDICATE_ID = "#V#has_academic_appointment_record"
GRADUATE_SUPERVISION_PREDICATE_ID = "#V#has_graduate_supervision_accreditation_record"

_SCHEMA_VERSION = "academic_roster_snapshot.v1"
_APPOINTMENT_SCHEMA_VERSION = "academic_appointment_record.v1"
_SUPERVISION_SCHEMA_VERSION = "graduate_supervision_accreditation_record.v1"
_MAX_RECORDS = 512
_CLASSIFICATIONS = frozenset({"included", "excluded", "unresolved"})


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item)
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        result.append(text)
    return result


def _value(request: WorkflowActionRequest, key: str) -> Any:
    if key in request.inputs:
        return request.inputs.get(key)
    return request.data.get(key)


def _failure(code: str, details: Sequence[str]) -> WorkflowActionResult:
    errors = list(details)
    return WorkflowActionResult(
        status="failed",
        error=code,
        outputs={
            "academic_roster_validation_status": "invalid",
            "academic_roster_validation_errors": errors,
            "response_text": "Academic roster snapshot is invalid: "
            + "; ".join(errors[:8]),
        },
    )


def _handle_validate_snapshot(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    snapshot = _mapping(
        _value(request, "academic_roster_snapshot") or _value(request, "snapshot")
    )
    errors: list[str] = []
    if snapshot.get("schema_version") != _SCHEMA_VERSION:
        errors.append(f"schema_version must be {_SCHEMA_VERSION}")

    source = _mapping(snapshot.get("source"))
    for key in ("source_id", "source_url", "retrieved_at", "source_kind"):
        if not _clean_text(source.get(key)):
            errors.append(f"source.{key} is required")

    cohort = _mapping(snapshot.get("cohort"))
    for key in ("label", "intended_scope", "membership_claim"):
        if not _clean_text(cohort.get(key)):
            errors.append(f"cohort.{key} is required")
    filters = cohort.get("filters")
    if not isinstance(filters, Sequence) or isinstance(filters, (str, bytes)):
        errors.append("cohort.filters must be an array")
        filters = []
    for index, raw_filter in enumerate(filters):
        item = _mapping(raw_filter)
        if not _clean_text(item.get("dimension")) or not _clean_text(item.get("value")):
            errors.append(f"cohort.filters[{index}] requires dimension and value")

    records = snapshot.get("records")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        return _failure(
            "academic_roster_records_missing",
            [*errors, "records must be an array"],
        )
    records = list(records)
    if not records:
        errors.append("records must not be empty")
    if len(records) > _MAX_RECORDS:
        errors.append(f"records is limited to {_MAX_RECORDS} rows")

    declared_total = cohort.get("declared_total")
    if isinstance(declared_total, bool) or not isinstance(declared_total, int):
        errors.append("cohort.declared_total must be an integer")
    elif declared_total != len(records):
        errors.append("cohort.declared_total must equal the number of supplied records")

    pagination = _mapping(cohort.get("pagination"))
    pages = pagination.get("pages")
    declared_page_counts: Counter[int] = Counter()
    if pages is not None:
        if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)):
            errors.append("cohort.pagination.pages must be an array")
        else:
            for index, raw_page in enumerate(pages):
                page = _mapping(raw_page)
                page_index = page.get("page_index")
                observed_count = page.get("observed_count")
                if (
                    isinstance(page_index, bool)
                    or not isinstance(page_index, int)
                    or page_index < 1
                ):
                    errors.append(
                        f"cohort.pagination.pages[{index}].page_index is invalid"
                    )
                    continue
                if (
                    isinstance(observed_count, bool)
                    or not isinstance(observed_count, int)
                    or observed_count < 0
                ):
                    errors.append(
                        f"cohort.pagination.pages[{index}].observed_count is invalid"
                    )
                    continue
                if page_index in declared_page_counts:
                    errors.append(f"page_index {page_index} is duplicated")
                declared_page_counts[page_index] = observed_count
            if sum(declared_page_counts.values()) != len(records):
                errors.append("pagination observed counts must sum to the record count")

    record_keys: set[str] = set()
    classification_by_record_key: dict[str, str] = {}
    observed_page_counts: Counter[int] = Counter()
    counts: Counter[str] = Counter()
    normalised_records: list[dict[str, Any]] = []
    for index, raw_record in enumerate(records):
        record = _mapping(raw_record)
        record_key = _clean_text(record.get("record_key"))
        if not record_key:
            errors.append(f"records[{index}].record_key is required")
        elif record_key in record_keys:
            errors.append(f"record_key {record_key!r} is duplicated")
        else:
            record_keys.add(record_key)

        classification = _clean_text(record.get("classification")).lower()
        if classification not in _CLASSIFICATIONS:
            errors.append(f"records[{index}].classification is invalid")
        else:
            counts[classification] += 1
            if record_key and record_key not in classification_by_record_key:
                classification_by_record_key[record_key] = classification
        if not _clean_text(record.get("classification_reason")):
            errors.append(f"records[{index}].classification_reason is required")

        evidence = _mapping(record.get("source_evidence"))
        if not _clean_text(evidence.get("source_url")):
            errors.append(f"records[{index}].source_evidence.source_url is required")
        if not (
            _clean_text(evidence.get("locator"))
            or _clean_text(evidence.get("source_text"))
        ):
            errors.append(
                f"records[{index}].source_evidence needs locator or source_text"
            )

        page_index = record.get("page_index")
        if page_index is not None:
            if (
                isinstance(page_index, bool)
                or not isinstance(page_index, int)
                or page_index < 1
            ):
                errors.append(f"records[{index}].page_index is invalid")
            else:
                observed_page_counts[page_index] += 1

        if classification == "included":
            person = _mapping(record.get("person"))
            organisation = _mapping(record.get("organisation"))
            unit = _mapping(record.get("unit"))
            roles = _string_list(record.get("role_titles"))
            leadership = _string_list(record.get("leadership_roles"))
            if not (
                _clean_text(person.get("name"))
                or _clean_text(person.get("display_name"))
            ):
                errors.append(
                    f"records[{index}].person.name or display_name is required "
                    "when included"
                )
            if not _clean_text(organisation.get("name")):
                errors.append(
                    f"records[{index}].organisation.name is required when included"
                )
            if not _clean_text(unit.get("name")):
                errors.append(f"records[{index}].unit.name is required when included")
            if not roles and not leadership:
                errors.append(f"records[{index}] needs a role title or leadership role")
            supervision = record.get("graduate_supervision")
            if supervision is not None:
                supervision = _mapping(supervision)
                if not _clean_text(supervision.get("status")):
                    errors.append(
                        f"records[{index}].graduate_supervision.status is required"
                    )
                if not isinstance(supervision.get("assertion_required"), bool):
                    errors.append(
                        f"records[{index}].graduate_supervision.assertion_required "
                        "must be boolean"
                    )

        normalised_records.append(record)

    if declared_page_counts and observed_page_counts != declared_page_counts:
        errors.append(
            "record page membership does not match declared pagination counts"
        )
    if errors:
        return _failure("academic_roster_snapshot_invalid", errors)

    validation = {
        "schema_version": "academic_roster_validation.v1",
        "status": "valid",
        "record_count": len(normalised_records),
        "classification_counts": {
            key: int(counts.get(key, 0))
            for key in ("included", "excluded", "unresolved")
        },
        "record_keys": [
            _clean_text(record.get("record_key")) for record in normalised_records
        ],
        "classification_by_record_key": classification_by_record_key,
        "page_counts": {
            str(key): value for key, value in sorted(observed_page_counts.items())
        },
        "coverage_claim": "bounded_source_cohort",
        "all_academic_staff_claimed": False,
        "source_id": source["source_id"],
        "cohort_label": cohort["label"],
        "cohort_membership_claim": cohort["membership_claim"],
    }
    return WorkflowActionResult(
        status="success",
        outputs={
            "academic_roster_validation_status": "valid",
            "academic_roster_validation": validation,
            "academic_roster_records": normalised_records,
            "academic_roster_record_count": len(normalised_records),
        },
    )


def _actor_context(request: WorkflowActionRequest) -> tuple[str | None, str | None]:
    user_id = _clean_text(
        _value(request, "user_concept_id")
        or getattr(request.environment, "user_concept_id", None)
    )
    org_id = _clean_text(
        _value(request, "org_concept_id")
        or getattr(request.environment, "org_concept_id", None)
    )
    resolved_user, resolved_org = resolve_event_actor_context(
        user_id=user_id or None,
        org_id=org_id or None,
        namespace=_clean_text(getattr(request.environment, "user_namespace", None))
        or None,
    )
    return _clean_text(resolved_user) or None, _clean_text(resolved_org) or None


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _assertion_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "academic_roster_assertion_evidence.v1",
        "record_key": record.get("record_key"),
        "source_evidence": _mapping(record.get("source_evidence")),
        "classification_reason": record.get("classification_reason"),
    }


def _terminal_payload(
    *,
    record: Mapping[str, Any],
    status: str,
    person_concept_id: str = "",
    receipts: Sequence[Mapping[str, Any]] = (),
    error: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": "academic_roster_item_result.v1",
        "record_key": _clean_text(record.get("record_key")),
        "classification": _clean_text(record.get("classification")).lower(),
        "terminal_status": status,
        "person_concept_id": person_concept_id,
        "effect_receipts": [dict(item) for item in receipts],
        "error": error,
    }


def _handle_assert_appointment(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    record = _mapping(
        _value(request, "current_academic_roster_record") or _value(request, "record")
    )
    record_key = _clean_text(record.get("record_key"))
    person_concept_id = _clean_text(
        _value(request, "person_concept_id")
        or _value(request, "entity_representation_concept_id")
    )
    if _clean_text(record.get("classification")).lower() != "included":
        return WorkflowActionResult(
            status="failed",
            error="academic_appointment_record_not_included",
        )
    if not record_key or not person_concept_id.startswith("#V#"):
        return WorkflowActionResult(
            status="failed",
            error="academic_appointment_subject_missing",
        )

    user_id, org_id = _actor_context(request)
    if not user_id:
        return WorkflowActionResult(
            status="failed",
            error="academic_appointment_authenticated_actor_required",
        )
    source = _mapping(
        _value(request, "academic_roster_source") or _value(request, "source")
    )
    cohort = _mapping(
        _value(request, "academic_roster_cohort") or _value(request, "cohort")
    )
    appointment = {
        "schema_version": _APPOINTMENT_SCHEMA_VERSION,
        "record_key": record_key,
        "person": _mapping(record.get("person")),
        "organisation": _mapping(record.get("organisation")),
        "unit": _mapping(record.get("unit")),
        "role_titles": _string_list(record.get("role_titles")),
        "leadership_roles": _string_list(record.get("leadership_roles")),
        "source": source,
        "cohort": cohort,
        "source_evidence": _mapping(record.get("source_evidence")),
    }
    receipts: list[dict[str, Any]] = []
    try:
        appointment_receipt = upsert_scoped_assertion(
            subject_concept_id=person_concept_id,
            predicate=ACADEMIC_APPOINTMENT_PREDICATE_ID,
            target_text=_canonical_json(appointment),
            language="en-NZ",
            scope_mode="organisation" if org_id else "user",
            evidence=_assertion_evidence(record),
            acting_user_concept_id=user_id,
            organisation_concept_id=org_id,
            namespace=_clean_text(getattr(request.environment, "user_namespace", None))
            or None,
            turn_id=_clean_text(_value(request, "turn_id")) or None,
            canonical_publication=False,
        )
        if not appointment_receipt.get("canonical_read_back"):
            raise RuntimeError("appointment assertion read-back missing")
        receipts.append(
            {
                "effect": "academic_appointment_assertion",
                "effect_status": appointment_receipt.get("effect_status"),
                "changed": bool(appointment_receipt.get("changed")),
                "assertion_id": appointment_receipt.get("assertion_id"),
                "canonical_read_back": True,
            }
        )

        supervision = _mapping(record.get("graduate_supervision"))
        if supervision.get("assertion_required") is True:
            supervision_record = {
                "schema_version": _SUPERVISION_SCHEMA_VERSION,
                "record_key": record_key,
                "person": _mapping(record.get("person")),
                "organisation": _mapping(record.get("organisation")),
                "unit": _mapping(record.get("unit")),
                "status": supervision.get("status"),
                "labels": _string_list(supervision.get("labels")),
                "source": source,
                "cohort": cohort,
                "source_evidence": _mapping(record.get("source_evidence")),
            }
            supervision_receipt = upsert_scoped_assertion(
                subject_concept_id=person_concept_id,
                predicate=GRADUATE_SUPERVISION_PREDICATE_ID,
                target_text=_canonical_json(supervision_record),
                language="en-NZ",
                scope_mode="organisation" if org_id else "user",
                evidence=_assertion_evidence(record),
                acting_user_concept_id=user_id,
                organisation_concept_id=org_id,
                namespace=_clean_text(
                    getattr(request.environment, "user_namespace", None)
                )
                or None,
                turn_id=_clean_text(_value(request, "turn_id")) or None,
                canonical_publication=False,
            )
            if not supervision_receipt.get("canonical_read_back"):
                raise RuntimeError("supervision assertion read-back missing")
            receipts.append(
                {
                    "effect": "graduate_supervision_assertion",
                    "effect_status": supervision_receipt.get("effect_status"),
                    "changed": bool(supervision_receipt.get("changed")),
                    "assertion_id": supervision_receipt.get("assertion_id"),
                    "canonical_read_back": True,
                }
            )
    except Exception as exc:  # noqa: BLE001 - preserve a retryable item receipt
        payload = _terminal_payload(
            record=record,
            status="failed",
            person_concept_id=person_concept_id,
            receipts=receipts,
            error=str(exc),
        )
        return WorkflowActionResult(
            status="failed",
            error=f"academic_appointment_assertion_failed:{exc}",
            outputs={"academic_roster_item_result": payload},
        )

    payload = _terminal_payload(
        record=record,
        status="represented",
        person_concept_id=person_concept_id,
        receipts=receipts,
    )
    return WorkflowActionResult(
        status="success",
        outputs={
            "academic_roster_item_result": payload,
            "control_signal": "return",
            "return_payload": payload,
        },
    )


def _handle_finalise_non_included(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    record = _mapping(
        _value(request, "current_academic_roster_record") or _value(request, "record")
    )
    classification = _clean_text(record.get("classification")).lower()
    if classification not in {"excluded", "unresolved"}:
        return WorkflowActionResult(
            status="failed",
            error="academic_roster_non_included_classification_required",
        )
    payload = _terminal_payload(record=record, status=classification)
    return WorkflowActionResult(
        status="success",
        outputs={
            "academic_roster_item_result": payload,
            "control_signal": "return",
            "return_payload": payload,
        },
    )


def _iteration_payload(raw_result: Any) -> dict[str, Any]:
    result = _mapping(raw_result)
    for key in (
        "result",
        "declared_output_payload",
        "return_payload",
        "outputs",
    ):
        candidate = _mapping(result.get(key))
        if candidate.get("schema_version") == "academic_roster_item_result.v1":
            return candidate
        nested = _mapping(candidate.get("academic_roster_item_result"))
        if nested:
            return nested
    nested = _mapping(result.get("academic_roster_item_result"))
    return nested


def _handle_reconcile_results(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    validation = _mapping(_value(request, "academic_roster_validation"))
    raw_results = _value(request, "academic_roster_iteration_results")
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
        return WorkflowActionResult(
            status="failed", error="academic_roster_iteration_results_missing"
        )
    expected = validation.get("record_count")
    results = [_iteration_payload(item) for item in raw_results]
    incomplete_indexes = [
        index
        for index, raw_result in enumerate(raw_results)
        if isinstance(raw_result, Mapping) and raw_result.get("completed") is False
    ]
    terminal_statuses = Counter(
        _clean_text(item.get("terminal_status")) for item in results
    )
    missing = [index for index, item in enumerate(results) if not item]
    invalid_schema_indexes = [
        index
        for index, item in enumerate(results)
        if item and item.get("schema_version") != "academic_roster_item_result.v1"
    ]
    failed = [item for item in results if item.get("terminal_status") == "failed"]
    expected_keys = [_clean_text(item) for item in validation.get("record_keys") or ()]
    observed_keys = [_clean_text(item.get("record_key")) for item in results]
    expected_key_counts = Counter(expected_keys)
    observed_key_counts = Counter(observed_keys)
    missing_record_keys = list((expected_key_counts - observed_key_counts).elements())
    unexpected_record_keys = list(
        (observed_key_counts - expected_key_counts).elements()
    )
    duplicate_record_keys = sorted(
        key for key, count in observed_key_counts.items() if key and count > 1
    )
    classification_by_key = _mapping(validation.get("classification_by_record_key"))
    required_status_by_classification = {
        "included": "represented",
        "excluded": "excluded",
        "unresolved": "unresolved",
    }
    classification_mismatches = [
        item.get("record_key")
        for item in results
        if item
        and (
            _clean_text(item.get("classification"))
            != _clean_text(classification_by_key.get(item.get("record_key")))
            or _clean_text(item.get("terminal_status"))
            != required_status_by_classification.get(
                _clean_text(classification_by_key.get(item.get("record_key")))
            )
        )
    ]
    if (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or len(results) != expected
        or missing
        or invalid_schema_indexes
        or incomplete_indexes
        or failed
        or terminal_statuses.get("", 0)
        or missing_record_keys
        or unexpected_record_keys
        or duplicate_record_keys
        or classification_mismatches
    ):
        return WorkflowActionResult(
            status="failed",
            error="academic_roster_aggregate_reconciliation_failed",
            outputs={
                "academic_roster_reconciliation": {
                    "schema_version": "academic_roster_reconciliation.v1",
                    "status": "failed",
                    "expected_count": expected,
                    "observed_count": len(results),
                    "missing_result_indexes": missing,
                    "invalid_result_schema_indexes": invalid_schema_indexes,
                    "incomplete_result_indexes": incomplete_indexes,
                    "failed_record_keys": [item.get("record_key") for item in failed],
                    "missing_record_keys": missing_record_keys,
                    "unexpected_record_keys": unexpected_record_keys,
                    "duplicate_record_keys": duplicate_record_keys,
                    "classification_mismatch_record_keys": (classification_mismatches),
                    "terminal_status_counts": dict(terminal_statuses),
                    "coverage_claim": "bounded_source_cohort",
                    "all_academic_staff_claimed": False,
                }
            },
        )
    reconciliation = {
        "schema_version": "academic_roster_reconciliation.v1",
        "status": "completed",
        "expected_count": expected,
        "observed_count": len(results),
        "terminal_status_counts": dict(terminal_statuses),
        "classification_counts": validation.get("classification_counts"),
        "coverage_claim": "bounded_source_cohort",
        "all_academic_staff_claimed": False,
        "all_rows_terminal": True,
        "item_results": results,
    }
    return WorkflowActionResult(
        status="success",
        outputs={
            "academic_roster_reconciliation": reconciliation,
            "response_text": (
                f"Reconciled {len(results)} rows from the bounded source cohort: "
                f"{terminal_statuses.get('represented', 0)} represented, "
                f"{terminal_statuses.get('excluded', 0)} excluded, and "
                f"{terminal_statuses.get('unresolved', 0)} unresolved."
            ),
        },
    )


def register_academic_roster_actions(registry: ActionRegistry) -> None:
    actions = (
        ActionSpec(
            action_id=ACADEMIC_ROSTER_VALIDATE_ACTION_ID,
            handler=_handle_validate_snapshot,
            description=(
                "Validate a source-neutral academic-roster snapshot, including "
                "cohort, pagination, classification, and evidence invariants."
            ),
            side_effects="none",
        ),
        ActionSpec(
            action_id=ACADEMIC_APPOINTMENT_ASSERT_ACTION_ID,
            handler=_handle_assert_appointment,
            description=(
                "Idempotently assert an exact source-provenanced appointment "
                "record and separately asserted graduate-supervision record, "
                "with canonical read-back."
            ),
            side_effects="write",
        ),
        ActionSpec(
            action_id=ACADEMIC_APPOINTMENT_FINALISE_NON_INCLUDED_ACTION_ID,
            handler=_handle_finalise_non_included,
            description=(
                "Return an explicit terminal excluded or unresolved outcome "
                "without mutating represented knowledge."
            ),
            side_effects="none",
        ),
        ActionSpec(
            action_id=ACADEMIC_ROSTER_RECONCILE_ACTION_ID,
            handler=_handle_reconcile_results,
            description=(
                "Require one non-failed terminal result per validated source row "
                "before reporting aggregate completion."
            ),
            side_effects="none",
        ),
    )
    for spec in actions:
        try:
            registry.register(spec)
        except ValueError:
            pass


__all__ = [
    "ACADEMIC_APPOINTMENT_ASSERT_ACTION_ID",
    "ACADEMIC_APPOINTMENT_FINALISE_NON_INCLUDED_ACTION_ID",
    "ACADEMIC_APPOINTMENT_PREDICATE_ID",
    "ACADEMIC_ROSTER_RECONCILE_ACTION_ID",
    "ACADEMIC_ROSTER_VALIDATE_ACTION_ID",
    "GRADUATE_SUPERVISION_PREDICATE_ID",
    "register_academic_roster_actions",
]
