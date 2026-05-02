"""Diagnostic helpers for workflow/testing concept materialisation parity."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Iterable

from .testing_workflow_contracts import (
    ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID,
    CAPABILITY_TEST_EXECUTION_WORKFLOW_ID,
    EPHEMERAL_THEORY_GC_WORKFLOW_ID,
    EPHEMERAL_THEORY_TYPE_ID,
    EXPERIMENT_OBSERVATION_TYPE_ID,
    EXPERIMENT_RUN_TYPE_ID,
    EXPERIMENT_SPEC_TYPE_ID,
    EXPERIMENT_SUITE_TYPE_ID,
    MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
    MEETING_INVITATION_TESTING_WORKFLOW_ID,
    PROMOTION_DECISION_TYPE_ID,
    PROMOTION_GATE_WORKFLOW_ID,
    SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID,
    TESTING_THEORY_TYPE_ID,
    THEORY_TYPE_ID,
)

TESTING_TYPE_CONCEPT_IDS: tuple[str, ...] = (
    THEORY_TYPE_ID,
    TESTING_THEORY_TYPE_ID,
    EPHEMERAL_THEORY_TYPE_ID,
    EXPERIMENT_SPEC_TYPE_ID,
    EXPERIMENT_RUN_TYPE_ID,
    EXPERIMENT_SUITE_TYPE_ID,
    EXPERIMENT_OBSERVATION_TYPE_ID,
    PROMOTION_DECISION_TYPE_ID,
)

CRITICAL_TESTING_TYPE_CONCEPT_IDS: tuple[str, ...] = (
    EPHEMERAL_THEORY_TYPE_ID,
    EXPERIMENT_SPEC_TYPE_ID,
    EXPERIMENT_RUN_TYPE_ID,
)

CANONICAL_TESTING_WORKFLOW_IDS: tuple[str, ...] = (
    MEETING_INVITATION_TESTING_WORKFLOW_ID,
    MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
    ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID,
    PROMOTION_GATE_WORKFLOW_ID,
    SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID,
    EPHEMERAL_THEORY_GC_WORKFLOW_ID,
    CAPABILITY_TEST_EXECUTION_WORKFLOW_ID,
)

DEFAULT_REQUIRED_CONCEPT_IDS: tuple[str, ...] = (
    *TESTING_TYPE_CONCEPT_IDS,
    *CANONICAL_TESTING_WORKFLOW_IDS,
)

WORKFLOW_BOOTSTRAP_REPORT_KEYS: tuple[str, ...] = (
    "entity_workflow_bootstrap",
    "represented_artefact_creation_workflow_bootstrap",
    "conversation_turn_workflow_bootstrap",
    "paper_workflow_bootstrap",
    "episode_evaluation_workflow_bootstrap",
    "paper_recommendation_workflow_bootstrap",
    "talk_workflow_bootstrap",
    "testing_workflow_bootstrap",
    "workflow_authority_bootstrap",
    "workflow_authoring_prompt_bootstrap",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_deepcopy(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _copy_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    copied = _safe_deepcopy(value)
    return copied if isinstance(copied, dict) else dict(value)


def _clean_string_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        values: Iterable[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        return []
    cleaned: list[str] = []
    for item in values:
        text = str(item).strip()
        if text:
            cleaned.append(text)
    return list(dict.fromkeys(cleaned))


def _normalise_required_concept_ids(raw: Any) -> list[str]:
    cleaned = _clean_string_list(raw)
    return cleaned or list(DEFAULT_REQUIRED_CONCEPT_IDS)


def _classify_concept_kind(concept_id: str) -> str:
    if concept_id in TESTING_TYPE_CONCEPT_IDS:
        return "testing_type_concept"
    if concept_id.endswith("_workflow") or concept_id in CANONICAL_TESTING_WORKFLOW_IDS:
        return "workflow_concept"
    return "concept"


def _extract_instance_of(concept_doc: dict[str, Any]) -> list[str]:
    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, dict):
        return []
    raw_instance_of = relationships.get("instance_of")
    if isinstance(raw_instance_of, str):
        value = raw_instance_of.strip()
        return [value] if value else []
    if isinstance(raw_instance_of, (list, tuple, set)):
        return _clean_string_list(raw_instance_of)
    return []


def _get_runtime_durable_workflow_snapshots() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available": False,
        "startup_status": None,
        "workflow_components": None,
    }
    try:
        from ..server import utils_flask

        payload["startup_status"] = (
            utils_flask.get_durable_workflow_startup_status_snapshot()
        )
        payload["workflow_components"] = (
            utils_flask.get_durable_workflow_components_snapshot()
        )
        payload["available"] = bool(
            payload["startup_status"] is not None
            or payload["workflow_components"] is not None
        )
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload


def _get_environment_provenance() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "configured_database_name": None,
        "running_under_pytest": None,
        "looks_like_test_database_name": None,
        "looks_like_noncanonical_database": None,
        "concept_collection_available": False,
        "concept_collection_count": None,
        "concept_collection_empty": None,
        "reason_codes": [],
    }
    try:
        from ..db.mongo_client import (
            _is_running_under_pytest,
            get_concepts_collection,
            get_configured_database_name,
        )

        db_name = get_configured_database_name()
        running_under_pytest = bool(_is_running_under_pytest())
        looks_like_test_name = db_name.lower().startswith("test")
        looks_like_noncanonical = db_name != "von_db"

        payload["configured_database_name"] = db_name
        payload["running_under_pytest"] = running_under_pytest
        payload["looks_like_test_database_name"] = looks_like_test_name
        payload["looks_like_noncanonical_database"] = looks_like_noncanonical

        collection = get_concepts_collection()
        if collection is not None:
            payload["concept_collection_available"] = True
            try:
                concept_count = int(collection.count_documents({}))
                payload["concept_collection_count"] = concept_count
                payload["concept_collection_empty"] = concept_count == 0
            except Exception as exc:
                payload["count_error"] = f"{type(exc).__name__}: {exc}"

        reason_codes: list[str] = []
        if running_under_pytest:
            reason_codes.append("running_under_pytest")
        if looks_like_noncanonical:
            reason_codes.append("noncanonical_database_name")
        if looks_like_test_name:
            reason_codes.append("test_database_name")
        if payload.get("concept_collection_empty") is True:
            reason_codes.append("concept_collection_empty")
        payload["reason_codes"] = list(dict.fromkeys(reason_codes))
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["reason_codes"] = ["environment_provenance_unavailable"]
    return payload


def _get_parity_inventory_snapshot() -> dict[str, Any]:
    payload: dict[str, Any] = {"available": False, "snapshot": None}
    try:
        from ..workflows.durable.registry_factory import (
            get_or_build_workflow_registry_inventory_snapshot,
            get_shared_workflow_registry_read_only,
        )

        registry = get_shared_workflow_registry_read_only(defer_parity_work=True)
        snapshot = get_or_build_workflow_registry_inventory_snapshot(
            registry=registry,
            allow_sync_build=False,
        )
        payload["snapshot"] = _safe_deepcopy(snapshot)
        payload["available"] = isinstance(snapshot, dict)
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
    return payload


def _lookup_exact_concept(concept_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "concept_id": concept_id,
        "exists": False,
        "lookup_mode": "exact_concept_id",
        "instance_of": [],
        "reason_codes": [],
    }
    try:
        from .concept_service import (
            ConceptNotFoundError,
            get_concept_by_concept_id_exact,
        )

        concept_doc = get_concept_by_concept_id_exact(concept_id)
        payload["exists"] = True
        payload["instance_of"] = _extract_instance_of(concept_doc or {})
    except ConceptNotFoundError:
        payload["reason_codes"] = ["concept_missing"]
    except Exception as exc:
        payload["lookup_error"] = f"{type(exc).__name__}: {exc}"
        payload["reason_codes"] = ["concept_lookup_error"]
    return payload


def _build_testing_type_parity(
    *,
    concept_lookup_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    present_ids = [
        concept_id
        for concept_id in TESTING_TYPE_CONCEPT_IDS
        if bool((concept_lookup_by_id.get(concept_id) or {}).get("exists"))
    ]
    missing_ids = [
        concept_id
        for concept_id in TESTING_TYPE_CONCEPT_IDS
        if concept_id not in present_ids
    ]
    critical_missing_ids = [
        concept_id
        for concept_id in CRITICAL_TESTING_TYPE_CONCEPT_IDS
        if concept_id in missing_ids
    ]
    return {
        "required_concept_ids": list(TESTING_TYPE_CONCEPT_IDS),
        "critical_concept_ids": list(CRITICAL_TESTING_TYPE_CONCEPT_IDS),
        "present_concept_ids": present_ids,
        "missing_concept_ids": missing_ids,
        "critical_missing_concept_ids": critical_missing_ids,
        "counts": {
            "required": len(TESTING_TYPE_CONCEPT_IDS),
            "present": len(present_ids),
            "missing": len(missing_ids),
            "critical_missing": len(critical_missing_ids),
        },
        "all_present": len(missing_ids) == 0,
    }


def _build_required_concept_record(
    *,
    concept_id: str,
    concept_lookup: dict[str, Any],
    environment: dict[str, Any],
    startup_status: dict[str, Any],
    parity_inventory: dict[str, Any],
    testing_type_parity: dict[str, Any],
    workflow_bootstrap_reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    kind = _classify_concept_kind(concept_id)
    exists = bool(concept_lookup.get("exists"))
    startup_state = str(startup_status.get("state") or "").strip().lower()
    parity_build_state = str(parity_inventory.get("build_state") or "").strip().lower()
    authority_report = parity_inventory.get("workflow_authority")
    authority_report = authority_report if isinstance(authority_report, dict) else {}
    missing_concept_workflow_ids = set(
        _clean_string_list(authority_report.get("missing_concept_workflow_ids"))
    )
    missing_required_type_by_workflow_id = authority_report.get(
        "missing_required_type_by_workflow_id"
    )
    if not isinstance(missing_required_type_by_workflow_id, dict):
        missing_required_type_by_workflow_id = {}

    authority_status = "not_tracked"
    if kind == "workflow_concept":
        if concept_id in missing_concept_workflow_ids:
            authority_status = "missing_concept"
        elif concept_id in missing_required_type_by_workflow_id:
            authority_status = "missing_required_type"
        else:
            authority_status = "current"

    diagnostic_state = "present"
    reason_codes = _clean_string_list(concept_lookup.get("reason_codes"))
    explanation = "Required concept is present."

    environment_reason_codes = _clean_string_list(environment.get("reason_codes"))
    environment_noncanonical = bool(
        startup_state == "skipped_pytest"
        or environment.get("running_under_pytest")
        or environment.get("looks_like_noncanonical_database")
        or environment.get("concept_collection_empty")
    )
    testing_bootstrap = (
        workflow_bootstrap_reports.get("testing_workflow_bootstrap") or {}
    )

    if exists and authority_status == "missing_required_type":
        diagnostic_state = "authority_drift"
        reason_codes.append("workflow_authority_missing_required_type")
        explanation = (
            "Concept exists, but workflow authority diagnostics report missing "
            "required typing metadata."
        )
    elif not exists and concept_lookup.get("lookup_error"):
        diagnostic_state = "lookup_error"
        explanation = (
            "Concept lookup could not complete, so materialisation state is "
            "inconclusive."
        )
    elif not exists and environment_noncanonical:
        diagnostic_state = "absent_environment_state"
        reason_codes.extend(environment_reason_codes)
        explanation = (
            "Concept appears absent while the queried environment looks like a "
            "fresh, test, or otherwise non-canonical database."
        )
    elif not exists and startup_state in {"pending", "initialising"}:
        diagnostic_state = "absent_startup_pending"
        reason_codes.append(f"durable_workflow_startup_{startup_state}")
        explanation = (
            "Concept appears absent while durable workflow startup is still "
            "initialising in this process."
        )
    elif not exists and startup_state == "not_started":
        diagnostic_state = "absent_startup_not_started"
        reason_codes.append("durable_workflow_startup_not_started")
        explanation = (
            "Concept appears absent because durable workflow startup did not run "
            "in this process."
        )
    elif not exists and startup_state == "failed":
        diagnostic_state = "absent_startup_failed"
        reason_codes.append("durable_workflow_startup_failed")
        explanation = (
            "Concept appears absent because durable workflow startup failed in "
            "this process."
        )
    elif not exists and parity_build_state == "pending_background_build":
        diagnostic_state = "absent_parity_pending"
        reason_codes.append("inventory_pending_background_build")
        explanation = (
            "Concept appears absent while workflow parity publication is still "
            "pending in the background."
        )
    elif (
        not exists
        and kind == "testing_type_concept"
        and bool(testing_bootstrap.get("success"))
        and concept_id in set(testing_type_parity.get("missing_concept_ids") or [])
    ):
        diagnostic_state = "absent_partial_bootstrap"
        reason_codes.extend(
            ["testing_type_concepts_missing", "testing_substrate_partial_bootstrap"]
        )
        explanation = (
            "Workflow-family bootstrap succeeded, but testing substrate type "
            "concept parity is still incomplete."
        )
    elif not exists and authority_status == "missing_concept":
        diagnostic_state = "absent_missing_authority"
        reason_codes.append("workflow_authority_missing_concept")
        explanation = (
            "Concept remains absent after startup/parity checks, which suggests "
            "a genuine workflow authority gap."
        )
    elif not exists:
        diagnostic_state = "absent_missing_authority"
        reason_codes.append("true_missing_authority_suspected")
        explanation = (
            "No environment or bootstrap explanation was detected, so the "
            "concept likely reflects a true missing-authority state."
        )

    if authority_status == "current" and kind != "workflow_concept":
        authority_status = "not_applicable"

    return {
        "concept_id": concept_id,
        "kind": kind,
        "exists": exists,
        "diagnostic_state": diagnostic_state,
        "authority_status": authority_status,
        "instance_of": _clean_string_list(concept_lookup.get("instance_of")),
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "explanation": explanation,
        "lookup_error": concept_lookup.get("lookup_error"),
    }


def _build_classification_summary(state: str) -> str:
    summaries = {
        "fresh_or_test_db": (
            "Required concepts appear absent because the queried environment "
            "looks like a fresh, test, or non-canonical database."
        ),
        "bootstrap_pending": (
            "Required concepts may appear absent because durable workflow "
            "startup has not finished yet."
        ),
        "bootstrap_not_started": (
            "Required concepts may appear absent because durable workflow "
            "startup did not run in this process."
        ),
        "bootstrap_failed": (
            "Required concepts may appear absent because durable workflow "
            "startup failed in this process."
        ),
        "parity_pending": (
            "Required concepts may appear absent because workflow parity "
            "publication is still pending."
        ),
        "partial_bootstrap": (
            "Workflow-family bootstrap succeeded, but testing substrate type "
            "parity is still incomplete."
        ),
        "missing_authority": (
            "The environment looks canonical and startup/parity explanations are "
            "exhausted, so the remaining gaps likely reflect true missing "
            "authority."
        ),
        "healthy": (
            "Required workflow/testing concepts are present and no blocking "
            "environment/bootstrap problem was detected."
        ),
    }
    return summaries.get(state, "Workflow materialisation diagnostics completed.")


def build_workflow_materialisation_diagnostics(
    *,
    required_concept_ids: Any = None,
    include_present_concepts: bool = True,
    startup_status: dict[str, Any] | None = None,
    workflow_components: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Diagnose why required workflow/testing concepts appear missing."""

    required_ids = _normalise_required_concept_ids(required_concept_ids)
    runtime_snapshots = _get_runtime_durable_workflow_snapshots()
    startup_status_payload = _copy_dict(startup_status) or _copy_dict(
        runtime_snapshots.get("startup_status")
    )
    workflow_components_payload = _copy_dict(workflow_components) or _copy_dict(
        runtime_snapshots.get("workflow_components")
    )

    environment = _get_environment_provenance()
    parity_payload = _get_parity_inventory_snapshot()
    parity_inventory = _copy_dict(parity_payload.get("snapshot"))

    workflow_bootstrap_reports = {
        key: _copy_dict(workflow_components_payload.get(key))
        for key in WORKFLOW_BOOTSTRAP_REPORT_KEYS
        if isinstance(workflow_components_payload.get(key), dict)
    }
    workflow_bootstrap_summary = _copy_dict(
        startup_status_payload.get("workflow_bootstrap_summary")
    )

    lookup_ids = list(
        dict.fromkeys(list(TESTING_TYPE_CONCEPT_IDS) + list(required_ids))
    )
    concept_lookup_by_id = {
        concept_id: _lookup_exact_concept(concept_id) for concept_id in lookup_ids
    }
    testing_type_parity = _build_testing_type_parity(
        concept_lookup_by_id=concept_lookup_by_id
    )

    required_concepts = [
        _build_required_concept_record(
            concept_id=concept_id,
            concept_lookup=concept_lookup_by_id.get(concept_id) or {},
            environment=environment,
            startup_status=startup_status_payload,
            parity_inventory=parity_inventory,
            testing_type_parity=testing_type_parity,
            workflow_bootstrap_reports=workflow_bootstrap_reports,
        )
        for concept_id in required_ids
    ]
    if not include_present_concepts:
        required_concepts = [
            item
            for item in required_concepts
            if not bool(item.get("exists"))
            or str(item.get("diagnostic_state") or "") == "authority_drift"
        ]

    missing_required_ids = [
        item["concept_id"] for item in required_concepts if not bool(item.get("exists"))
    ]
    authority_drift_required_ids = [
        item["concept_id"]
        for item in required_concepts
        if str(item.get("diagnostic_state") or "") == "authority_drift"
    ]

    startup_state = str(startup_status_payload.get("state") or "").strip().lower()
    parity_build_state = str(parity_inventory.get("build_state") or "").strip().lower()
    testing_bootstrap = (
        workflow_bootstrap_reports.get("testing_workflow_bootstrap") or {}
    )

    environment_reason_codes = _clean_string_list(environment.get("reason_codes"))
    diagnostics_payload = parity_inventory.get("diagnostics") or {}
    parity_reason_codes = _clean_string_list(diagnostics_payload.get("reason_codes"))

    primary_state = "healthy"
    classification_reason_codes: list[str] = []
    if missing_required_ids and (
        startup_state == "skipped_pytest"
        or environment.get("running_under_pytest")
        or environment.get("looks_like_noncanonical_database")
        or environment.get("concept_collection_empty")
    ):
        primary_state = "fresh_or_test_db"
        classification_reason_codes.extend(environment_reason_codes)
    elif missing_required_ids and startup_state in {"pending", "initialising"}:
        primary_state = "bootstrap_pending"
        classification_reason_codes.append(f"durable_workflow_startup_{startup_state}")
    elif missing_required_ids and startup_state == "not_started":
        primary_state = "bootstrap_not_started"
        classification_reason_codes.append("durable_workflow_startup_not_started")
    elif missing_required_ids and startup_state == "failed":
        primary_state = "bootstrap_failed"
        classification_reason_codes.append("durable_workflow_startup_failed")
    elif missing_required_ids and parity_build_state == "pending_background_build":
        primary_state = "parity_pending"
        classification_reason_codes.append("inventory_pending_background_build")
    elif testing_type_parity.get("critical_missing_concept_ids") and bool(
        testing_bootstrap.get("success")
    ):
        primary_state = "partial_bootstrap"
        classification_reason_codes.extend(
            ["testing_type_concepts_missing", "testing_substrate_partial_bootstrap"]
        )
    elif missing_required_ids or authority_drift_required_ids:
        primary_state = "missing_authority"
        classification_reason_codes.extend(
            ["true_missing_authority_suspected"]
            if missing_required_ids
            else ["workflow_authority_missing_required_type"]
        )

    errors = [
        value
        for value in (
            runtime_snapshots.get("error"),
            environment.get("error"),
            parity_payload.get("error"),
        )
        if isinstance(value, str) and value.strip()
    ]
    if errors:
        classification_reason_codes.append("diagnostic_sources_partial")

    return {
        "success": True,
        "schema_version": "workflow_materialisation_diagnostics.v1",
        "generated_at_utc": _utc_now_iso(),
        "required_concept_ids": required_ids,
        "classification": {
            "state": primary_state,
            "summary": _build_classification_summary(primary_state),
            "reason_codes": list(
                dict.fromkeys(
                    classification_reason_codes
                    + (
                        parity_reason_codes
                        if primary_state in {"parity_pending", "missing_authority"}
                        else []
                    )
                )
            ),
            "missing_required_concept_ids": missing_required_ids,
            "authority_drift_required_concept_ids": authority_drift_required_ids,
            "ready_for_authoritative_checks": primary_state
            in {"healthy", "missing_authority"},
        },
        "environment": environment,
        "durable_workflow_startup": startup_status_payload,
        "workflow_bootstrap": {
            "summary": workflow_bootstrap_summary,
            "reports": workflow_bootstrap_reports,
        },
        "parity_inventory": parity_inventory,
        "testing_type_parity": testing_type_parity,
        "required_concepts": required_concepts,
        "errors": errors,
    }


def build_workflow_concept_parity_audit(
    *,
    concept_ids: Any = None,
    include_present_concepts: bool = True,
) -> dict[str, Any]:
    """Audit workflow-related concept parity with summary counts and provenance."""

    diagnostics = build_workflow_materialisation_diagnostics(
        required_concept_ids=concept_ids,
        include_present_concepts=include_present_concepts,
    )
    audited_concepts = [
        item
        for item in diagnostics.get("required_concepts", [])
        if isinstance(item, dict)
    ]
    diagnostic_state_counts: dict[str, int] = {}
    present_count = 0
    missing_count = 0
    authority_drift_count = 0
    for row in audited_concepts:
        state = str(row.get("diagnostic_state") or "unknown").strip() or "unknown"
        diagnostic_state_counts[state] = diagnostic_state_counts.get(state, 0) + 1
        if bool(row.get("exists")):
            present_count += 1
        else:
            missing_count += 1
        if state == "authority_drift":
            authority_drift_count += 1

    classification = diagnostics.get("classification")
    classification_map = classification if isinstance(classification, dict) else {}
    return {
        "success": True,
        "schema_version": "workflow_concept_parity_audit.v1",
        "generated_at_utc": diagnostics.get("generated_at_utc"),
        "concept_ids": diagnostics.get("required_concept_ids", []),
        "summary": {
            "audited_count": len(audited_concepts),
            "present_count": present_count,
            "missing_count": missing_count,
            "authority_drift_count": authority_drift_count,
            "diagnostic_state_counts": diagnostic_state_counts,
            "classification_state": classification_map.get("state"),
            "ready_for_authoritative_checks": bool(
                classification_map.get("ready_for_authoritative_checks")
            ),
        },
        "classification": classification_map,
        "environment": diagnostics.get("environment"),
        "durable_workflow_startup": diagnostics.get("durable_workflow_startup"),
        "workflow_bootstrap": diagnostics.get("workflow_bootstrap"),
        "parity_inventory": diagnostics.get("parity_inventory"),
        "testing_type_parity": diagnostics.get("testing_type_parity"),
        "concepts": audited_concepts,
        "errors": diagnostics.get("errors", []),
    }


__all__ = [
    "CANONICAL_TESTING_WORKFLOW_IDS",
    "CRITICAL_TESTING_TYPE_CONCEPT_IDS",
    "DEFAULT_REQUIRED_CONCEPT_IDS",
    "TESTING_TYPE_CONCEPT_IDS",
    "build_workflow_concept_parity_audit",
    "build_workflow_materialisation_diagnostics",
]
