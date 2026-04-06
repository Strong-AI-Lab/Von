"""First-class testing-theory and theory-slice helpers for Testing Workflows."""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from . import concept_service
from .concept_service import (
    ConceptNotFoundError,
    get_concept_by_concept_id_exact,
)
from .relationship_write_service import add_relationship
from .testing_workflow_contracts import (
    DERIVED_FROM_CANONICAL_CONCEPT_PREDICATE_ID,
    EPHEMERAL_THEORY_TYPE_ID,
    EXPERIMENT_VERDICT_PASS,
    TESTING_THEORY_SCHEMA_VERSION,
    THEORY_ASSERTION_STATUSES,
    THEORY_ASSERTION_STATUS_EXPIRED,
    THEORY_ASSERTION_STATUS_PROMOTED,
    THEORY_ASSERTION_STATUS_PROPOSED,
    THEORY_ASSERTION_STATUS_ROLLED_BACK,
    THEORY_LOCAL_TARGET_KIND_CONCEPT,
    THEORY_LOCAL_TARGET_KIND_TEXT,
)
from .text_value_service import (
    get_texts_for_concept,
    upsert_singleton_text_relation,
    upsert_text_for_concept,
)
from .workflow_vontology_materialisation_helpers import stable_named_instance_concept_id

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 60 * 60


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


def _normalise_strings(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return []
    normalised: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        normalised.append(text)
    return normalised


def _clone_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): copy.deepcopy(item) for key, item in value.items()}


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _coerce_int(value: Any, *, default: int = 0, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _coerce_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_concept_or_none(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id_exact(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _normalise_target_kind(target: str, target_kind: Any) -> str:
    explicit = _safe_str(target_kind).lower()
    if explicit in {THEORY_LOCAL_TARGET_KIND_CONCEPT, THEORY_LOCAL_TARGET_KIND_TEXT}:
        return explicit
    if target.startswith("#V#"):
        return THEORY_LOCAL_TARGET_KIND_CONCEPT
    return THEORY_LOCAL_TARGET_KIND_TEXT


def _build_assertion_id() -> str:
    return f"theory_assert_{uuid.uuid4().hex[:24]}"


def _normalise_assertion(
    raw: Mapping[str, Any],
    *,
    now_iso: str,
) -> dict[str, Any] | None:
    source_id = _safe_str(raw.get("source_id") or raw.get("subject_id"))
    predicate = _safe_str(raw.get("predicate"))
    target = _safe_str(raw.get("target") or raw.get("value"))
    if not source_id or not predicate or not target:
        return None

    status = _safe_str(raw.get("status")).lower() or THEORY_ASSERTION_STATUS_PROPOSED
    if status not in THEORY_ASSERTION_STATUSES:
        status = THEORY_ASSERTION_STATUS_PROPOSED

    created_at = _safe_str(raw.get("created_at_utc")) or now_iso
    updated_at = _safe_str(raw.get("updated_at_utc")) or now_iso
    return {
        "assertion_id": _safe_str(raw.get("assertion_id")) or _build_assertion_id(),
        "source_id": source_id,
        "predicate": predicate,
        "target": target,
        "target_kind": _normalise_target_kind(target, raw.get("target_kind")),
        "confidence_score": max(0.0, min(1.0, _coerce_float(raw.get("confidence_score")))),
        "status": status,
        "rationale": _safe_str(raw.get("rationale"), limit=2000) or None,
        "provenance": _clone_mapping(raw.get("provenance")),
        "created_at_utc": created_at,
        "updated_at_utc": updated_at,
        "promoted_at_utc": _safe_str(raw.get("promoted_at_utc")) or None,
        "promoted_relation_id": _safe_str(raw.get("promoted_relation_id")) or None,
        "promoted_relation_kind": _safe_str(raw.get("promoted_relation_kind")) or None,
    }


def _normalise_assertions(raw: Any, *, now_iso: str) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []

    assertions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        normalised = _normalise_assertion(item, now_iso=now_iso)
        if normalised is not None:
            assertions.append(normalised)
    return assertions


def _normalise_theory_state(
    raw: Mapping[str, Any] | None,
    *,
    theory_id: str,
) -> dict[str, Any]:
    payload = _clone_mapping(raw)
    payload["schema_version"] = TESTING_THEORY_SCHEMA_VERSION
    payload["theory_id"] = theory_id
    payload["theory_kind"] = _safe_str(payload.get("theory_kind")) or "ephemeral"
    payload["lifecycle_state"] = _safe_str(payload.get("lifecycle_state")) or "active"
    payload["included_canonical_concept_ids"] = _normalise_strings(
        payload.get("included_canonical_concept_ids")
    )
    payload["included_theory_ids"] = _normalise_strings(payload.get("included_theory_ids"))
    payload["expected_observations"] = copy.deepcopy(
        list(payload.get("expected_observations") or [])
    )
    payload["promotion_policy"] = _clone_mapping(payload.get("promotion_policy"))
    payload["retention_policy"] = _clone_mapping(payload.get("retention_policy"))
    payload["local_assertions"] = _normalise_assertions(
        payload.get("local_assertions"),
        now_iso=_safe_str(payload.get("updated_at_utc")) or _utcnow_iso(),
    )
    return payload


def _persist_theory_state(
    *,
    theory_id: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    normalised = _normalise_theory_state(state, theory_id=theory_id)
    concept_service.update_concept(
        theory_id,
        {
            "concept_data.testing_theory": normalised,
            "attributes.testing_theory.lifecycle_state": normalised.get(
                "lifecycle_state"
            ),
            "attributes.testing_theory.expires_at_utc": normalised.get("expires_at_utc"),
            "attributes.testing_theory.updated_at_utc": normalised.get("updated_at_utc"),
        },
    )
    return normalised


def _ensure_testing_theory_concept(
    *,
    theory_id: str,
    name: str,
    description: str,
    user_id: str | None,
    org_id: str | None,
    namespace: str | None,
) -> tuple[dict[str, Any], bool]:
    existing = _get_concept_or_none(theory_id)
    if isinstance(existing, Mapping):
        return dict(existing), False

    created = concept_service.create_concept(
        name=name,
        concept_id=theory_id,
        description=description,
        parent_concept_ids=[EPHEMERAL_THEORY_TYPE_ID],
        create_as_instance=True,
        created_by_concept_id=user_id,
        organisation_concept_id=org_id,
        event_namespace=namespace,
    )
    upsert_singleton_text_relation(
        subject_concept_id=theory_id,
        predicate="hasDescription",
        text=description,
        lang="en-NZ",
        context={
            "source": "testing_theory_service",
            "reason": "testing_theory_create_slice",
        },
        garbage_collect=True,
    )
    return created, True


def get_testing_theory_state(theory_id: str) -> dict[str, Any] | None:
    concept_id = _safe_str(theory_id)
    if not concept_id:
        return None
    concept = _get_concept_or_none(concept_id)
    if not isinstance(concept, Mapping):
        return None
    concept_data = concept.get("concept_data")
    if not isinstance(concept_data, Mapping):
        return None
    state = concept_data.get("testing_theory")
    if not isinstance(state, Mapping):
        return None
    return _normalise_theory_state(state, theory_id=concept_id)


def create_testing_theory_slice(
    *,
    name: str,
    theory_id: str | None = None,
    namespace: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    included_canonical_concept_ids: Sequence[str] = (),
    included_theory_ids: Sequence[str] = (),
    expected_observations: Sequence[Any] = (),
    promotion_policy: Mapping[str, Any] | None = None,
    retention_policy: Mapping[str, Any] | None = None,
    experiment_spec_id: str | None = None,
    ttl_seconds: int | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    try:
        from .testing_workflow_vontology_service import (
            ensure_testing_type_concept_support,
        )

        ensure_testing_type_concept_support()
    except Exception:
        pass

    theory_name = _safe_str(name, limit=200) or "Ephemeral testing theory"
    resolved_theory_id = _safe_str(theory_id) or stable_named_instance_concept_id(
        theory_name,
        prefix="ephemeral_theory",
    )
    ttl_value = _coerce_int(ttl_seconds, default=_DEFAULT_TTL_SECONDS, minimum=1)
    now = _utcnow()
    now_iso = now.isoformat()
    expires_at_iso = (now + timedelta(seconds=ttl_value)).isoformat()
    description_text = (
        _safe_str(description, limit=1000)
        or f"Ephemeral testing theory slice for {theory_name}."
    )

    _ensure_testing_theory_concept(
        theory_id=resolved_theory_id,
        name=theory_name,
        description=description_text,
        user_id=_safe_str(user_id) or None,
        org_id=_safe_str(org_id) or None,
        namespace=_safe_str(namespace) or None,
    )

    state = {
        "schema_version": TESTING_THEORY_SCHEMA_VERSION,
        "theory_id": resolved_theory_id,
        "theory_label": theory_name,
        "theory_kind": "ephemeral",
        "lifecycle_state": "active",
        "namespace": _safe_str(namespace) or None,
        "user_id": _safe_str(user_id) or None,
        "org_id": _safe_str(org_id) or None,
        "experiment_spec_id": _safe_str(experiment_spec_id) or None,
        "included_canonical_concept_ids": _normalise_strings(
            included_canonical_concept_ids
        ),
        "included_theory_ids": _normalise_strings(included_theory_ids),
        "expected_observations": copy.deepcopy(list(expected_observations or [])),
        "promotion_policy": _clone_mapping(promotion_policy),
        "retention_policy": _clone_mapping(retention_policy),
        "local_assertions": [],
        "created_at_utc": now_iso,
        "updated_at_utc": now_iso,
        "expires_at_utc": expires_at_iso,
        "ttl_seconds": ttl_value,
    }
    persisted = _persist_theory_state(theory_id=resolved_theory_id, state=state)
    return {
        "success": True,
        "theory_id": resolved_theory_id,
        "created": True,
        "testing_theory": persisted,
    }


def import_canonical_context_into_theory(
    *,
    theory_id: str,
    concept_ids: Sequence[str],
    theory_ids: Sequence[str] = (),
) -> dict[str, Any]:
    resolved_theory_id = _safe_str(theory_id)
    if not resolved_theory_id:
        return {"success": False, "error": "theory_id_required"}

    state = get_testing_theory_state(resolved_theory_id)
    if state is None:
        return {"success": False, "error": "theory_not_found"}

    now_iso = _utcnow_iso()
    imported_concept_ids = _normalise_strings(concept_ids)
    imported_theory_ids = _normalise_strings(theory_ids)
    merged_concepts = _normalise_strings(
        [*state.get("included_canonical_concept_ids", []), *imported_concept_ids]
    )
    merged_theories = _normalise_strings(
        [*state.get("included_theory_ids", []), *imported_theory_ids]
    )
    state["included_canonical_concept_ids"] = merged_concepts
    state["included_theory_ids"] = merged_theories
    state["updated_at_utc"] = now_iso
    persisted = _persist_theory_state(theory_id=resolved_theory_id, state=state)

    linked_relations: list[dict[str, Any]] = []
    for concept_id in imported_concept_ids:
        try:
            result = add_relationship(
                source_id=resolved_theory_id,
                predicate=DERIVED_FROM_CANONICAL_CONCEPT_PREDICATE_ID,
                target=concept_id,
            )
        except Exception as exc:
            logger.warning(
                "[testing_theory] could not link canonical context %s -> %s: %s",
                resolved_theory_id,
                concept_id,
                exc,
            )
            continue
        if isinstance(result, Mapping):
            linked_relations.append(dict(result))

    return {
        "success": True,
        "theory_id": resolved_theory_id,
        "testing_theory": persisted,
        "imported_concept_ids": imported_concept_ids,
        "imported_theory_ids": imported_theory_ids,
        "linked_relations": linked_relations,
    }


def assert_testing_theory_local_claims(
    *,
    theory_id: str,
    claims: Sequence[Mapping[str, Any]] | Mapping[str, Any],
) -> dict[str, Any]:
    resolved_theory_id = _safe_str(theory_id)
    if not resolved_theory_id:
        return {"success": False, "error": "theory_id_required"}

    state = get_testing_theory_state(resolved_theory_id)
    if state is None:
        return {"success": False, "error": "theory_not_found"}

    now_iso = _utcnow_iso()
    local_assertions = list(state.get("local_assertions") or [])
    appended = _normalise_assertions(claims, now_iso=now_iso)
    if not appended:
        return {"success": False, "error": "no_valid_claims"}

    local_assertions.extend(appended)
    state["local_assertions"] = local_assertions
    state["updated_at_utc"] = now_iso
    persisted = _persist_theory_state(theory_id=resolved_theory_id, state=state)
    return {
        "success": True,
        "theory_id": resolved_theory_id,
        "asserted_claims": appended,
        "testing_theory": persisted,
    }


def _canonical_relation_exists(
    *,
    source_id: str,
    predicate: str,
    target: str,
) -> bool:
    source = _get_concept_or_none(source_id)
    if not isinstance(source, Mapping):
        return False
    relationships = source.get("relationships")
    if not isinstance(relationships, Mapping):
        return False
    targets = _normalise_strings(relationships.get(predicate))
    return target in targets


def _canonical_text_exists(
    *,
    source_id: str,
    predicate: str,
    target_text: str,
) -> bool:
    rows = get_texts_for_concept(source_id, predicate=predicate, limit=100)
    expected = target_text.strip()
    return any(_safe_str(row.get("text")) == expected for row in rows if isinstance(row, Mapping))


def compute_testing_theory_diff(
    *,
    theory_id: str,
) -> dict[str, Any]:
    resolved_theory_id = _safe_str(theory_id)
    if not resolved_theory_id:
        return {"success": False, "error": "theory_id_required"}

    state = get_testing_theory_state(resolved_theory_id)
    if state is None:
        return {"success": False, "error": "theory_not_found"}

    entries: list[dict[str, Any]] = []
    promotable_ids: list[str] = []
    already_canonical_ids: list[str] = []
    inactive_ids: list[str] = []

    for assertion in state.get("local_assertions") or []:
        if not isinstance(assertion, Mapping):
            continue
        assertion_id = _safe_str(assertion.get("assertion_id"))
        status = _safe_str(assertion.get("status")).lower()
        if not assertion_id:
            continue
        if status != THEORY_ASSERTION_STATUS_PROPOSED:
            inactive_ids.append(assertion_id)
            entries.append(
                {
                    "assertion_id": assertion_id,
                    "status": status or "unknown",
                    "diff_status": "inactive",
                }
            )
            continue

        source_id = _safe_str(assertion.get("source_id"))
        predicate = _safe_str(assertion.get("predicate"))
        target = _safe_str(assertion.get("target"))
        target_kind = _safe_str(assertion.get("target_kind")).lower()
        if not source_id or not predicate or not target:
            inactive_ids.append(assertion_id)
            entries.append(
                {
                    "assertion_id": assertion_id,
                    "status": status,
                    "diff_status": "invalid",
                }
            )
            continue

        if target_kind == THEORY_LOCAL_TARGET_KIND_TEXT:
            exists = _canonical_text_exists(
                source_id=source_id,
                predicate=predicate,
                target_text=target,
            )
        else:
            exists = _canonical_relation_exists(
                source_id=source_id,
                predicate=predicate,
                target=target,
            )

        entry = {
            "assertion_id": assertion_id,
            "source_id": source_id,
            "predicate": predicate,
            "target": target,
            "target_kind": target_kind or THEORY_LOCAL_TARGET_KIND_CONCEPT,
            "status": status,
            "diff_status": "already_canonical" if exists else "promotion_ready",
        }
        entries.append(entry)
        if exists:
            already_canonical_ids.append(assertion_id)
        else:
            promotable_ids.append(assertion_id)

    diff = {
        "schema_version": TESTING_THEORY_SCHEMA_VERSION,
        "theory_id": resolved_theory_id,
        "assertions": entries,
        "promotion_ready_assertion_ids": promotable_ids,
        "already_canonical_assertion_ids": already_canonical_ids,
        "inactive_assertion_ids": inactive_ids,
        "counts": {
            "assertions": len(entries),
            "promotion_ready": len(promotable_ids),
            "already_canonical": len(already_canonical_ids),
            "inactive": len(inactive_ids),
        },
    }
    return {"success": True, "theory_id": resolved_theory_id, "diff": diff}


def rollback_testing_theory_local_writes(
    *,
    theory_id: str,
    assertion_ids: Sequence[str] = (),
    clear_all: bool = False,
) -> dict[str, Any]:
    resolved_theory_id = _safe_str(theory_id)
    if not resolved_theory_id:
        return {"success": False, "error": "theory_id_required"}

    state = get_testing_theory_state(resolved_theory_id)
    if state is None:
        return {"success": False, "error": "theory_not_found"}

    selected_ids = set(_normalise_strings(assertion_ids))
    if clear_all and not selected_ids:
        selected_ids = {
            _safe_str(item.get("assertion_id"))
            for item in state.get("local_assertions") or []
            if isinstance(item, Mapping)
        }

    rolled_back_ids: list[str] = []
    now_iso = _utcnow_iso()
    updated_assertions: list[dict[str, Any]] = []
    for assertion in state.get("local_assertions") or []:
        if not isinstance(assertion, Mapping):
            continue
        item = dict(assertion)
        assertion_id = _safe_str(item.get("assertion_id"))
        status = _safe_str(item.get("status")).lower()
        if (
            assertion_id
            and assertion_id in selected_ids
            and status == THEORY_ASSERTION_STATUS_PROPOSED
        ):
            item["status"] = THEORY_ASSERTION_STATUS_ROLLED_BACK
            item["updated_at_utc"] = now_iso
            rolled_back_ids.append(assertion_id)
        updated_assertions.append(item)

    state["local_assertions"] = updated_assertions
    state["updated_at_utc"] = now_iso
    persisted = _persist_theory_state(theory_id=resolved_theory_id, state=state)
    return {
        "success": True,
        "theory_id": resolved_theory_id,
        "rolled_back_assertion_ids": rolled_back_ids,
        "testing_theory": persisted,
    }


def promote_testing_theory_validated_claims(
    *,
    theory_id: str,
    assertion_ids: Sequence[str] = (),
    experiment_run_id: str | None = None,
    required_verdict: str = EXPERIMENT_VERDICT_PASS,
) -> dict[str, Any]:
    resolved_theory_id = _safe_str(theory_id)
    if not resolved_theory_id:
        return {"success": False, "error": "theory_id_required"}

    state = get_testing_theory_state(resolved_theory_id)
    if state is None:
        return {"success": False, "error": "theory_not_found"}

    resolved_run_id = _safe_str(experiment_run_id)
    if resolved_run_id:
        try:
            from .experiment_run_service import get_experiment_run_projection

            run_projection = get_experiment_run_projection(run_id=resolved_run_id)
        except Exception:
            run_projection = None
        run_verdict = _safe_str((run_projection or {}).get("verdict")).lower()
        if required_verdict and run_verdict != required_verdict:
            return {
                "success": False,
                "error": "experiment_verdict_not_promotion_safe",
                "required_verdict": required_verdict,
                "observed_verdict": run_verdict or None,
            }

    selected_ids = set(_normalise_strings(assertion_ids))
    if not selected_ids:
        diff_result = compute_testing_theory_diff(theory_id=resolved_theory_id)
        diff = diff_result.get("diff") if isinstance(diff_result, Mapping) else None
        if isinstance(diff, Mapping):
            selected_ids = set(diff.get("promotion_ready_assertion_ids") or [])

    promoted_assertions: list[dict[str, Any]] = []
    updated_assertions: list[dict[str, Any]] = []
    now_iso = _utcnow_iso()
    for assertion in state.get("local_assertions") or []:
        if not isinstance(assertion, Mapping):
            continue
        item = dict(assertion)
        assertion_id = _safe_str(item.get("assertion_id"))
        status = _safe_str(item.get("status")).lower()
        if (
            not assertion_id
            or assertion_id not in selected_ids
            or status != THEORY_ASSERTION_STATUS_PROPOSED
        ):
            updated_assertions.append(item)
            continue

        source_id = _safe_str(item.get("source_id"))
        predicate = _safe_str(item.get("predicate"))
        target = _safe_str(item.get("target"))
        target_kind = _safe_str(item.get("target_kind")).lower()
        if not source_id or not predicate or not target:
            updated_assertions.append(item)
            continue

        try:
            if target_kind == THEORY_LOCAL_TARGET_KIND_TEXT:
                relation = upsert_text_for_concept(
                    subject_concept_id=source_id,
                    predicate=predicate,
                    text=target,
                    lang="en-NZ",
                    context={
                        "source": "testing_theory_service",
                        "theory_id": resolved_theory_id,
                        "experiment_run_id": resolved_run_id,
                    },
                )
                relation_kind = "text_relation"
                relation_id = _safe_str((relation or {}).get("relation_id"))
            else:
                relation = add_relationship(
                    source_id=source_id,
                    predicate=predicate,
                    target=target,
                )
                relation_kind = "concept_relationship"
                relation_id = _safe_str((relation or {}).get("relation_id"))
        except Exception as exc:
            logger.warning(
                "[testing_theory] could not promote assertion %s in %s: %s",
                assertion_id,
                resolved_theory_id,
                exc,
            )
            updated_assertions.append(item)
            continue

        item["status"] = THEORY_ASSERTION_STATUS_PROMOTED
        item["updated_at_utc"] = now_iso
        item["promoted_at_utc"] = now_iso
        item["promoted_relation_kind"] = relation_kind
        item["promoted_relation_id"] = relation_id
        updated_assertions.append(item)
        promoted_assertions.append(
            {
                "assertion_id": assertion_id,
                "relation_kind": relation_kind,
                "relation_id": relation_id,
            }
        )

    state["local_assertions"] = updated_assertions
    state["updated_at_utc"] = now_iso
    state["last_promotion_run_id"] = resolved_run_id or None
    persisted = _persist_theory_state(theory_id=resolved_theory_id, state=state)
    return {
        "success": True,
        "theory_id": resolved_theory_id,
        "promoted_assertions": promoted_assertions,
        "testing_theory": persisted,
    }


def garbage_collect_expired_testing_theories(
    *,
    now_utc: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    from .concept_search_service import search_concepts

    cutoff = (
        datetime.fromisoformat(now_utc.replace("Z", "+00:00"))
        if isinstance(now_utc, str) and now_utc.strip()
        else _utcnow()
    )
    search_result = search_concepts(
        query="",
        instance_of=EPHEMERAL_THEORY_TYPE_ID,
        limit=max(1, min(int(limit), 500)),
    )
    expired_theory_ids: list[str] = []
    retained_theory_ids: list[str] = []
    seen_theory_ids: set[str] = set()
    for item in search_result.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        theory_id = _safe_str(item.get("concept_id"))
        if not theory_id or theory_id in seen_theory_ids:
            continue
        seen_theory_ids.add(theory_id)
        state = get_testing_theory_state(theory_id)
        if state is None:
            continue
        expires_at_raw = _safe_str(state.get("expires_at_utc"))
        if not expires_at_raw:
            retained_theory_ids.append(theory_id)
            continue
        try:
            expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
        except ValueError:
            retained_theory_ids.append(theory_id)
            continue
        if expires_at > cutoff:
            retained_theory_ids.append(theory_id)
            continue

        now_iso = cutoff.isoformat()
        updated_assertions: list[dict[str, Any]] = []
        for assertion in state.get("local_assertions") or []:
            if not isinstance(assertion, Mapping):
                continue
            item_state = dict(assertion)
            if (
                _safe_str(item_state.get("status")).lower()
                == THEORY_ASSERTION_STATUS_PROPOSED
            ):
                item_state["status"] = THEORY_ASSERTION_STATUS_EXPIRED
                item_state["updated_at_utc"] = now_iso
            updated_assertions.append(item_state)

        state["local_assertions"] = updated_assertions
        state["lifecycle_state"] = "expired"
        state["updated_at_utc"] = now_iso
        _persist_theory_state(theory_id=theory_id, state=state)
        expired_theory_ids.append(theory_id)

    return {
        "success": True,
        "expired_theory_ids": expired_theory_ids,
        "retained_theory_ids": retained_theory_ids,
        "counts": {
            "expired": len(expired_theory_ids),
            "retained": len(retained_theory_ids),
        },
    }


__all__ = [
    "assert_testing_theory_local_claims",
    "compute_testing_theory_diff",
    "create_testing_theory_slice",
    "garbage_collect_expired_testing_theories",
    "get_testing_theory_state",
    "import_canonical_context_into_theory",
    "promote_testing_theory_validated_claims",
    "rollback_testing_theory_local_writes",
]
