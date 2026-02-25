"""Canonical relationship removal service with preview, bulk, undo, and audit.

This module is the single write-path authority for relationship removals.
Both MCP and HTTP entry points should route through these helpers so removal
behaviour stays deterministic and drift-free.
"""

from __future__ import annotations

import base64
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from flask import has_request_context, session

from ..db.mongo_client import get_db
from ..db.repositories.concepts_repository import ConceptsRepository, RELATIONSHIP_KINDS
from ..security.access_control import can_access_concept, get_effective_user_concept_id
from ..vontology.code_concepts_registry import build_virtual_concept_doc, is_code_concept_id
from ..vontology.utils_vontology import invalidate_vontology_caches
from .relationship_write_service import (
    STRUCTURAL_INVERSE_MAP,
    normalise_structural_predicate,
    validate_predicate_concept,
)

logger = logging.getLogger(__name__)

RELATIONSHIP_REMOVAL_AUDIT_COLLECTION = "relationship_removal_audit"
RELATIONSHIP_REMOVAL_TOMBSTONE_COLLECTION = "relationship_removal_tombstones"

REMOVAL_MODES = {"soft_delete", "hard_delete"}
CASCADE_POLICIES = {"none", "warn", "delete_dependent"}
TEXT_PREDICATE_NAMES = {"hasContent", "hasDescription", "hasName"}

_INDEXES_READY = False
_INDEX_LOCK = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _normalise_mode(mode: Any) -> str:
    candidate = str(mode).strip().lower() if mode is not None else "soft_delete"
    return candidate if candidate in REMOVAL_MODES else "soft_delete"


def _normalise_cascade(cascade: Any) -> str:
    candidate = str(cascade).strip().lower() if cascade is not None else "warn"
    return candidate if candidate in CASCADE_POLICIES else "warn"


def _b64_encode(value: str) -> str:
    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def _b64_decode(value: str) -> Optional[str]:
    try:
        pad = "=" * ((4 - len(value) % 4) % 4)
        return base64.urlsafe_b64decode((value + pad).encode("ascii")).decode("utf-8")
    except Exception:
        return None


def build_relationship_relation_id(source_id: str, predicate: str, target: str) -> str:
    """Build a deterministic, reversible relation identifier from a triple."""
    return f"rel:{_b64_encode(source_id)}.{_b64_encode(predicate)}.{_b64_encode(target)}"


def parse_relationship_relation_id(relation_id: Any) -> Optional[Dict[str, str]]:
    """Parse a relation identifier back into source/predicate/target."""
    if not isinstance(relation_id, str):
        return None
    relation_id = relation_id.strip()
    if not relation_id.startswith("rel:"):
        return None
    raw = relation_id[4:]
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    source_id = _b64_decode(parts[0])
    predicate = _b64_decode(parts[1])
    target = _b64_decode(parts[2])
    if not source_id or not predicate or not target:
        return None
    return {
        "source_id": source_id,
        "predicate": predicate,
        "target": target,
        "relation_id": relation_id,
    }


def _ensure_indexes() -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    with _INDEX_LOCK:
        if _INDEXES_READY:
            return
        db = get_db()
        if db is None:
            return
        try:
            audit_coll = db[RELATIONSHIP_REMOVAL_AUDIT_COLLECTION]
            tombstone_coll = db[RELATIONSHIP_REMOVAL_TOMBSTONE_COLLECTION]
            audit_coll.create_index("operation_id")
            audit_coll.create_index("request_id")
            audit_coll.create_index("timestamp")
            tombstone_coll.create_index("undo_token")
            tombstone_coll.create_index("operation_id")
            tombstone_coll.create_index("removed_at")
        except Exception:
            logger.debug("Failed to ensure relationship removal indexes", exc_info=True)
        _INDEXES_READY = True


def _resolve_actor_context(
    *,
    actor_id: Optional[str] = None,
    actor_role: Optional[str] = None,
) -> tuple[Optional[str], str]:
    resolved_actor_id = actor_id
    resolved_role = actor_role

    if not resolved_actor_id:
        try:
            resolved_actor_id = get_effective_user_concept_id()
        except Exception:
            resolved_actor_id = None

    if not resolved_role:
        if has_request_context():
            session_role = session.get("role_in_org") or session.get("role")
            if isinstance(session_role, str) and session_role.strip():
                resolved_role = session_role.strip()
        else:
            resolved_role = "system"

    if not resolved_role:
        resolved_role = "anonymous" if resolved_actor_id is None else "user"

    return resolved_actor_id, resolved_role


def _has_elevated_privileges(actor_role: str) -> bool:
    role = (actor_role or "").strip().lower()
    if role in {"admin", "owner", "operator", "system"}:
        return True
    if not has_request_context():
        return True
    role_fields = [
        session.get("role"),
        session.get("role_in_org"),
    ]
    for value in role_fields:
        if isinstance(value, str) and value.strip().lower() in {
            "admin",
            "owner",
            "operator",
        }:
            return True
    for key in ("is_admin", "admin", "is_owner", "is_operator"):
        if _as_bool(session.get(key), False):
            return True
    return False


def _normalise_relationship_values(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _validate_predicate(predicate: Any) -> Dict[str, Any]:
    if not isinstance(predicate, str) or not predicate.strip():
        return {
            "ok": False,
            "error_code": "missing_parameter",
            "error": "Missing 'predicate' parameter",
        }

    predicate_input = predicate.strip()
    canonical = normalise_structural_predicate(predicate_input)
    predicate_normalised = (
        predicate_input[3:] if predicate_input.startswith("#V#") else predicate_input
    )
    is_text_predicate = predicate_normalised in TEXT_PREDICATE_NAMES
    if is_text_predicate:
        return {
            "ok": True,
            "predicate": predicate_normalised,
            "predicate_input": predicate_input,
            "is_structural": False,
            "is_text_predicate": True,
        }

    if canonical in RELATIONSHIP_KINDS:
        return {
            "ok": True,
            "predicate": canonical,
            "predicate_input": predicate_input,
            "is_structural": True,
            "is_text_predicate": False,
        }

    if isinstance(canonical, str) and canonical.startswith("#V#"):
        valid, error_code, error_details = validate_predicate_concept(canonical)
        if not valid:
            return {
                "ok": False,
                "error_code": error_code or "invalid_relationship_predicate",
                "error": (
                    f"Invalid relationship predicate '{predicate_input}'. "
                    f"Details: {error_details or {}}"
                ),
                "error_details": error_details or {},
            }

        pred_doc = ConceptsRepository.find_one(
            {"concept_id": canonical}, {"relationships.is_an_instance_of": 1}
        )
        if pred_doc is None and is_code_concept_id(canonical):
            pred_doc = build_virtual_concept_doc(canonical)
        if isinstance(pred_doc, dict):
            instance_of = pred_doc.get("relationships", {}).get("is_an_instance_of", [])
            if isinstance(instance_of, str):
                instance_of = [instance_of]
            if isinstance(instance_of, list) and "#V#binary_text_predicate" in instance_of:
                is_text_predicate = True

        return {
            "ok": True,
            "predicate": canonical,
            "predicate_input": predicate_input,
            "is_structural": False,
            "is_text_predicate": is_text_predicate,
        }

    return {
        "ok": False,
        "error_code": "invalid_relationship_predicate",
        "error": (
            "Invalid relationship predicate. Use a structural predicate "
            "(for example 'typeOf' or 'instance_of') or a '#V#...' predicate concept ID."
        ),
        "error_details": {
            "predicate_input": predicate_input,
            "predicate_canonical": canonical,
        },
    }


def _resolve_selector(
    *,
    relation_id: Any = None,
    source_id: Any = None,
    predicate: Any = None,
    target: Any = None,
    allow_text_predicates: bool = False,
    require_target: bool = True,
) -> Dict[str, Any]:
    decoded = parse_relationship_relation_id(relation_id)
    if decoded:
        source_id = decoded["source_id"]
        predicate = decoded["predicate"]
        target = decoded["target"]
        relation_id = decoded["relation_id"]

    if not isinstance(source_id, str) or not source_id.strip():
        return {
            "ok": False,
            "status": "error",
            "error_code": "missing_parameter",
            "error": "Missing 'source_id' parameter",
        }
    source_id = source_id.strip()

    predicate_result = _validate_predicate(predicate)
    if not predicate_result.get("ok"):
        return {
            "ok": False,
            "status": "error",
            "error_code": predicate_result.get("error_code", "invalid_relationship_predicate"),
            "error": predicate_result.get("error", "Invalid predicate"),
            "error_details": predicate_result.get("error_details", {}),
        }

    canonical_predicate = str(predicate_result["predicate"])
    is_structural = bool(predicate_result.get("is_structural"))
    is_text_predicate = bool(predicate_result.get("is_text_predicate"))
    if is_text_predicate and not allow_text_predicates:
        return {
            "ok": False,
            "status": "error",
            "error_code": "unsupported_text_relation_removal",
            "error": "Text relation removal is not supported by remove_relationship",
            "error_details": {"predicate": canonical_predicate},
        }

    if target is not None and not isinstance(target, str):
        return {
            "ok": False,
            "status": "error",
            "error_code": "invalid_parameter",
            "error": "target must be a string when provided",
        }
    target_value = target.strip() if isinstance(target, str) else None
    if require_target and not target_value:
        return {
            "ok": False,
            "status": "error",
            "error_code": "missing_parameter",
            "error": "Missing 'target' parameter",
        }

    if not can_access_concept(source_id):
        return {
            "ok": False,
            "status": "forbidden",
            "error_code": "forbidden",
            "error": "Cannot access source concept",
            "restricted_visibility": True,
            "source_id": source_id,
            "predicate": canonical_predicate,
            "target": target_value,
        }
    if target_value and target_value.startswith("#V#") and not can_access_concept(target_value):
        return {
            "ok": False,
            "status": "forbidden",
            "error_code": "forbidden",
            "error": "Cannot access target concept",
            "restricted_visibility": True,
            "source_id": source_id,
            "predicate": canonical_predicate,
            "target": target_value,
        }

    source_doc = ConceptsRepository.find_one(
        {"concept_id": source_id},
        {"concept_id": 1, f"relationships.{canonical_predicate}": 1},
    )
    if not source_doc:
        return {
            "ok": False,
            "status": "not_found",
            "error_code": "source_concept_not_found",
            "error": f"Source concept '{source_id}' not found",
            "error_details": {"role": "source", "concept_id": source_id},
            "source_id": source_id,
            "predicate": canonical_predicate,
            "target": target_value,
        }

    relationship_values = _normalise_relationship_values(
        (source_doc.get("relationships") or {}).get(canonical_predicate)
    )

    if target_value:
        if target_value not in relationship_values:
            return {
                "ok": False,
                "status": "not_found",
                "error_code": "not_found",
                "error": "Relationship not present",
                "source_id": source_id,
                "predicate": canonical_predicate,
                "target": target_value,
                "relation_id": build_relationship_relation_id(
                    source_id, canonical_predicate, target_value
                ),
            }
        candidates = [target_value]
    else:
        candidates = relationship_values
        if len(candidates) > 1:
            return {
                "ok": False,
                "status": "ambiguous",
                "error_code": "ambiguous_selector",
                "error": (
                    "Multiple relationships match this selector. Provide target "
                    "or relation_id to disambiguate."
                ),
                "source_id": source_id,
                "predicate": canonical_predicate,
                "candidate_count": len(candidates),
                "candidates": [
                    {
                        "target": item,
                        "relation_id": build_relationship_relation_id(
                            source_id, canonical_predicate, item
                        ),
                    }
                    for item in candidates[:25]
                ],
            }
        if not candidates:
            return {
                "ok": False,
                "status": "not_found",
                "error_code": "not_found",
                "error": "Relationship not present",
                "source_id": source_id,
                "predicate": canonical_predicate,
            }
        target_value = candidates[0]

    relation_id_value = (
        relation_id
        if isinstance(relation_id, str) and relation_id.strip()
        else build_relationship_relation_id(source_id, canonical_predicate, target_value)
    )
    target_exists = bool(
        isinstance(target_value, str)
        and target_value.startswith("#V#")
        and ConceptsRepository.find_one({"concept_id": target_value}, {"concept_id": 1})
    )

    return {
        "ok": True,
        "source_id": source_id,
        "predicate": canonical_predicate,
        "target": target_value,
        "relation_id": relation_id_value,
        "is_structural": is_structural,
        "is_text_predicate": is_text_predicate,
        "target_exists": target_exists,
    }


def _summarise_dependencies(
    *,
    source_id: str,
    predicate: str,
    target: str,
    is_structural: bool,
    restricted_visibility: bool = False,
) -> Dict[str, Any]:
    inverse_predicate = STRUCTURAL_INVERSE_MAP.get(predicate) if is_structural else None
    source_outgoing_same_predicate = 0
    try:
        source_doc = ConceptsRepository.find_one(
            {"concept_id": source_id}, {f"relationships.{predicate}": 1}
        )
        source_relationships_raw = (
            source_doc.get("relationships") if isinstance(source_doc, dict) else None
        )
        source_relationships: dict[str, Any] = (
            source_relationships_raw
            if isinstance(source_relationships_raw, dict)
            else {}
        )
        source_outgoing_same_predicate = len(
            _normalise_relationship_values(source_relationships.get(predicate))
        )
    except Exception:
        source_outgoing_same_predicate = 0

    inverse_present = False
    if inverse_predicate and target.startswith("#V#"):
        try:
            target_doc = ConceptsRepository.find_one(
                {"concept_id": target}, {f"relationships.{inverse_predicate}": 1}
            )
            target_relationships_raw = (
                target_doc.get("relationships") if isinstance(target_doc, dict) else None
            )
            target_relationships: dict[str, Any] = (
                target_relationships_raw
                if isinstance(target_relationships_raw, dict)
                else {}
            )
            inverse_values = _normalise_relationship_values(
                target_relationships.get(inverse_predicate)
            )
            inverse_present = source_id in inverse_values
        except Exception:
            inverse_present = False

    warning_messages: list[str] = []
    if is_structural and predicate in {"is_a_type_of", "is_an_instance_of"}:
        warning_messages.append(
            "Removing taxonomy edges can change inferred type/instance semantics."
        )
    if is_structural and inverse_predicate and not inverse_present:
        warning_messages.append(
            "Inverse edge is already absent; canonical removal will reconcile asymmetric state."
        )
    if restricted_visibility:
        warning_messages.append(
            "Restricted visibility: dependency details are redacted to counts/flags."
        )

    return {
        "direct_edge": {
            "source_id": source_id,
            "predicate": predicate,
            "target": target,
        },
        "inverse_effect": {
            "predicate": inverse_predicate,
            "inverse_present_before": inverse_present if not restricted_visibility else None,
            "restricted_visibility": restricted_visibility,
        },
        "dependent_references": {
            "source_outgoing_same_predicate": source_outgoing_same_predicate,
            "restricted_visibility": restricted_visibility,
        },
        "warnings": warning_messages,
    }


def _audit_collection():
    db = get_db()
    if db is None:
        raise RuntimeError("Database unavailable for relationship removal audit")
    return db[RELATIONSHIP_REMOVAL_AUDIT_COLLECTION]


def _tombstone_collection():
    db = get_db()
    if db is None:
        raise RuntimeError("Database unavailable for relationship removal tombstones")
    return db[RELATIONSHIP_REMOVAL_TOMBSTONE_COLLECTION]


def _append_audit_record(record: Mapping[str, Any]) -> str:
    _ensure_indexes()
    payload = dict(record)
    payload.setdefault("audit_id", str(uuid.uuid4()))
    payload.setdefault("timestamp", _iso_utc(_utc_now()))
    _audit_collection().insert_one(payload)
    return str(payload["audit_id"])


def _persist_tombstone(tombstone: Mapping[str, Any]) -> str:
    _ensure_indexes()
    payload = dict(tombstone)
    payload.setdefault("tombstone_id", str(uuid.uuid4()))
    payload.setdefault("removed_at", _iso_utc(_utc_now()))
    _tombstone_collection().insert_one(payload)
    return str(payload["tombstone_id"])


def preview_remove_relationship(
    *,
    relation_id: Any = None,
    source_id: Any = None,
    predicate: Any = None,
    target: Any = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a no-mutation impact preview for a relationship removal."""
    correlation_id = request_id.strip() if isinstance(request_id, str) and request_id.strip() else str(uuid.uuid4())
    resolved = _resolve_selector(
        relation_id=relation_id,
        source_id=source_id,
        predicate=predicate,
        target=target,
        allow_text_predicates=False,
        require_target=False,
    )

    if not resolved.get("ok"):
        status = str(resolved.get("status") or "error")
        payload = {
            "success": status in {"not_found", "ambiguous"},
            "status": status,
            "dry_run": True,
            "correlation_id": correlation_id,
            "request_id": request_id,
            "source_id": resolved.get("source_id"),
            "predicate": resolved.get("predicate"),
            "target": resolved.get("target"),
            "relation_id": resolved.get("relation_id"),
            "error": resolved.get("error"),
            "error_code": resolved.get("error_code"),
            "error_details": resolved.get("error_details", {}),
            "restricted_visibility": bool(resolved.get("restricted_visibility")),
            "warnings": [],
        }
        if status == "not_found":
            payload["impact"] = {
                "direct_edge": None,
                "inverse_effect": {"predicate": None, "inverse_present_before": False},
                "dependent_references": {"source_outgoing_same_predicate": 0},
                "warnings": [],
            }
        if status == "ambiguous":
            payload["candidates"] = resolved.get("candidates", [])
            payload["candidate_count"] = resolved.get("candidate_count", 0)
            payload["warnings"] = ["Provide relation_id or target to disambiguate."]
        return payload

    impact = _summarise_dependencies(
        source_id=str(resolved["source_id"]),
        predicate=str(resolved["predicate"]),
        target=str(resolved["target"]),
        is_structural=bool(resolved.get("is_structural")),
        restricted_visibility=False,
    )
    return {
        "success": True,
        "status": "ok",
        "dry_run": True,
        "correlation_id": correlation_id,
        "request_id": request_id,
        "source_id": resolved["source_id"],
        "predicate": resolved["predicate"],
        "target": resolved["target"],
        "relation_id": resolved["relation_id"],
        "impact": impact,
        "warnings": impact.get("warnings", []),
    }


def _should_require_strict_confirmation(
    *,
    mode: str,
    cascade: str,
    bulk_operation: bool,
) -> bool:
    return mode == "hard_delete" or cascade == "delete_dependent" or bulk_operation


def remove_relationship(
    *,
    relation_id: Any = None,
    source_id: Any = None,
    predicate: Any = None,
    target: Any = None,
    mode: Any = "soft_delete",
    cascade: Any = "warn",
    dry_run: Any = False,
    confirmed: Any = False,
    operator_override: Any = False,
    reason: Any = None,
    request_id: Any = None,
    actor_id: Optional[str] = None,
    actor_role: Optional[str] = None,
    bulk_operation: bool = False,
    undo_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Remove one relationship edge through the canonical repository mutation path."""
    mode_value = _normalise_mode(mode)
    cascade_value = _normalise_cascade(cascade)
    dry_run_value = _as_bool(dry_run, False)
    confirmed_value = _as_bool(confirmed, False)
    operator_override_value = _as_bool(operator_override, False)
    reason_value = reason.strip() if isinstance(reason, str) and reason.strip() else None
    request_id_value = (
        request_id.strip() if isinstance(request_id, str) and request_id.strip() else None
    )
    correlation_id = request_id_value or str(uuid.uuid4())
    resolved_actor_id, resolved_actor_role = _resolve_actor_context(
        actor_id=actor_id, actor_role=actor_role
    )

    if dry_run_value:
        preview = preview_remove_relationship(
            relation_id=relation_id,
            source_id=source_id,
            predicate=predicate,
            target=target,
            request_id=request_id_value,
        )
        preview["mode_returned"] = mode_value
        preview["cascade_policy"] = cascade_value
        preview["deletion_mode_requires_confirmation"] = _should_require_strict_confirmation(
            mode=mode_value,
            cascade=cascade_value,
            bulk_operation=bulk_operation,
        )
        return preview

    strict_confirmation_required = _should_require_strict_confirmation(
        mode=mode_value,
        cascade=cascade_value,
        bulk_operation=bulk_operation,
    )
    if strict_confirmation_required and not (confirmed_value or operator_override_value):
        return {
            "success": False,
            "status": "confirmation_required",
            "error": "Explicit confirmation required for this deletion mode/policy.",
            "error_code": "confirmation_required",
            "mode_returned": mode_value,
            "cascade_policy": cascade_value,
            "correlation_id": correlation_id,
            "request_id": request_id_value,
        }

    if mode_value == "hard_delete" or cascade_value == "delete_dependent":
        if not (confirmed_value and operator_override_value):
            return {
                "success": False,
                "status": "forbidden",
                "error": (
                    "Hard delete or delete_dependent cascade requires confirmed=true "
                    "and operator_override=true."
                ),
                "error_code": "elevated_confirmation_required",
                "mode_returned": mode_value,
                "cascade_policy": cascade_value,
                "correlation_id": correlation_id,
                "request_id": request_id_value,
            }
        if not _has_elevated_privileges(resolved_actor_role):
            return {
                "success": False,
                "status": "forbidden",
                "error": "Elevated privileges are required for hard delete/cascade delete_dependent.",
                "error_code": "elevated_privileges_required",
                "mode_returned": mode_value,
                "cascade_policy": cascade_value,
                "correlation_id": correlation_id,
                "request_id": request_id_value,
            }

    resolved = _resolve_selector(
        relation_id=relation_id,
        source_id=source_id,
        predicate=predicate,
        target=target,
        allow_text_predicates=False,
        require_target=True,
    )

    def _audit_and_return(payload: Dict[str, Any], *, phase: str = "result") -> Dict[str, Any]:
        record = {
            "operation": "remove_relationship",
            "phase": phase,
            "status": payload.get("status"),
            "operation_id": correlation_id,
            "request_id": request_id_value,
            "actor_id": resolved_actor_id,
            "actor_role": resolved_actor_role,
            "source_id": payload.get("source_id"),
            "predicate": payload.get("predicate"),
            "target": payload.get("target"),
            "relation_id": payload.get("relation_id"),
            "mode": mode_value,
            "cascade": cascade_value,
            "confirmed": confirmed_value,
            "operator_override": operator_override_value,
            "reason": reason_value,
            "before": payload.get("before"),
            "after": payload.get("after"),
            "warnings": payload.get("warnings", []),
            "error_code": payload.get("error_code"),
            "error_message": payload.get("error"),
        }
        audit_id = _append_audit_record(record)
        payload["audit_record_id"] = audit_id
        return payload

    if not resolved.get("ok"):
        status = str(resolved.get("status") or "error")
        error_code = resolved.get("error_code")
        is_not_present_noop = status == "not_found" and error_code == "not_found"
        result_payload: Dict[str, Any] = {
            "success": is_not_present_noop,
            "status": "no_op" if is_not_present_noop else status,
            "source_id": resolved.get("source_id"),
            "predicate": resolved.get("predicate"),
            "target": resolved.get("target"),
            "relation_id": resolved.get("relation_id"),
            "removed": False,
            "already_absent": is_not_present_noop,
            "error": None if is_not_present_noop else resolved.get("error"),
            "error_code": None if is_not_present_noop else error_code,
            "error_details": (
                {} if is_not_present_noop else resolved.get("error_details", {})
            ),
            "mode_returned": mode_value,
            "cascade_policy": cascade_value,
            "correlation_id": correlation_id,
            "request_id": request_id_value,
            "warnings": [],
        }
        if is_not_present_noop:
            result_payload["message"] = "Relationship not present"
        try:
            return _audit_and_return(result_payload)
        except Exception:
            # Pre-mutation outcomes remain deterministic even if audit storage is
            # unavailable; fail-closed policy applies to actual mutation writes.
            result_payload.setdefault("warnings", []).append("audit_unavailable")
            return result_payload

    source_id_value = str(resolved["source_id"])
    predicate_value = str(resolved["predicate"])
    target_value = str(resolved["target"])
    relation_id_value = str(resolved["relation_id"])
    is_structural = bool(resolved.get("is_structural"))
    target_exists = bool(resolved.get("target_exists"))
    maintain_inverse = bool(is_structural and target_exists)

    warnings: list[str] = []
    if is_structural and not target_exists:
        warnings.append(
            "Target concept is absent; removed forward edge only while preserving asymmetric-state safety."
        )
    if cascade_value == "delete_dependent":
        warnings.append(
            "delete_dependent cascade requested; document model currently removes direct edge and structural inverse only."
        )

    before_snapshot = {
        "forward_present": True,
        "maintain_inverse": maintain_inverse,
    }

    try:
        _append_audit_record(
            {
                "operation": "remove_relationship",
                "phase": "attempt",
                "status": "attempt",
                "operation_id": correlation_id,
                "request_id": request_id_value,
                "actor_id": resolved_actor_id,
                "actor_role": resolved_actor_role,
                "source_id": source_id_value,
                "predicate": predicate_value,
                "target": target_value,
                "relation_id": relation_id_value,
                "mode": mode_value,
                "cascade": cascade_value,
                "confirmed": confirmed_value,
                "operator_override": operator_override_value,
                "reason": reason_value,
                "before": before_snapshot,
            }
        )
    except Exception as exc:
        return {
            "success": False,
            "status": "error",
            "source_id": source_id_value,
            "predicate": predicate_value,
            "target": target_value,
            "relation_id": relation_id_value,
            "removed": False,
            "error": "Audit persistence failed; mutation was not executed.",
            "error_code": "audit_persistence_failed",
            "error_details": {"exception_type": type(exc).__name__},
            "mode_returned": mode_value,
            "cascade_policy": cascade_value,
            "correlation_id": correlation_id,
            "request_id": request_id_value,
            "warnings": warnings,
        }

    changed = False
    try:
        changed = ConceptsRepository.mutate_relationship_edge(
            source_id_value,
            predicate_value,
            target_value,
            action="remove",
            maintain_inverse=maintain_inverse,
        )
    except Exception as exc:
        result_payload = {
            "success": False,
            "status": "error",
            "source_id": source_id_value,
            "predicate": predicate_value,
            "target": target_value,
            "relation_id": relation_id_value,
            "removed": False,
            "error": f"Failed to remove relationship: {exc}",
            "error_code": "mutation_failed",
            "error_details": {"exception_type": type(exc).__name__},
            "mode_returned": mode_value,
            "cascade_policy": cascade_value,
            "correlation_id": correlation_id,
            "request_id": request_id_value,
            "warnings": warnings,
            "before": before_snapshot,
            "after": {"forward_present": True},
        }
        try:
            return _audit_and_return(result_payload)
        except Exception as audit_exc:
            result_payload["error"] = "Audit persistence failed after mutation failure."
            result_payload["error_code"] = "audit_persistence_failed"
            result_payload["error_details"] = {"exception_type": type(audit_exc).__name__}
            return result_payload

    removed = bool(changed)
    result_status = "ok" if removed else "no_op"
    result_payload = {
        "success": True,
        "status": result_status,
        "source_id": source_id_value,
        "predicate": predicate_value,
        "target": target_value,
        "relation_id": relation_id_value,
        "removed": removed,
        "already_absent": not removed,
        "message": "Relationship removed" if removed else "Relationship not present",
        "mode_returned": mode_value,
        "cascade_policy": cascade_value,
        "correlation_id": correlation_id,
        "request_id": request_id_value,
        "warnings": warnings,
        "before": before_snapshot,
        "after": {"forward_present": not removed},
    }

    undo_token_value: Optional[str] = None
    if removed and mode_value == "soft_delete":
        undo_token_value = undo_token or f"undo:{correlation_id}"
        try:
            _persist_tombstone(
                {
                    "operation_id": correlation_id,
                    "request_id": request_id_value,
                    "undo_token": undo_token_value,
                    "source_id": source_id_value,
                    "predicate": predicate_value,
                    "target": target_value,
                    "relation_id": relation_id_value,
                    "actor_id": resolved_actor_id,
                    "actor_role": resolved_actor_role,
                    "reason": reason_value,
                    "mode": mode_value,
                }
            )
        except Exception as exc:
            rollback_changed = False
            try:
                rollback_changed = ConceptsRepository.mutate_relationship_edge(
                    source_id_value,
                    predicate_value,
                    target_value,
                    action="add",
                    maintain_inverse=maintain_inverse,
                )
            except Exception:
                rollback_changed = False
            return {
                "success": False,
                "status": "error",
                "source_id": source_id_value,
                "predicate": predicate_value,
                "target": target_value,
                "relation_id": relation_id_value,
                "removed": False,
                "error": "Soft-delete tombstone persistence failed; mutation rolled back.",
                "error_code": "tombstone_persistence_failed",
                "error_details": {
                    "exception_type": type(exc).__name__,
                    "rollback_changed": rollback_changed,
                },
                "mode_returned": mode_value,
                "cascade_policy": cascade_value,
                "correlation_id": correlation_id,
                "request_id": request_id_value,
                "warnings": warnings,
            }

    if undo_token_value:
        result_payload["undo_token"] = undo_token_value

    try:
        audited_payload = _audit_and_return(result_payload)
    except Exception as exc:
        rollback_changed = False
        try:
            rollback_changed = ConceptsRepository.mutate_relationship_edge(
                source_id_value,
                predicate_value,
                target_value,
                action="add",
                maintain_inverse=maintain_inverse,
            )
        except Exception:
            rollback_changed = False
        return {
            "success": False,
            "status": "error",
            "source_id": source_id_value,
            "predicate": predicate_value,
            "target": target_value,
            "relation_id": relation_id_value,
            "removed": False,
            "error": "Audit persistence failed; mutation rolled back.",
            "error_code": "audit_persistence_failed",
            "error_details": {
                "exception_type": type(exc).__name__,
                "rollback_changed": rollback_changed,
            },
            "mode_returned": mode_value,
            "cascade_policy": cascade_value,
            "correlation_id": correlation_id,
            "request_id": request_id_value,
            "warnings": warnings,
        }

    if removed:
        try:
            invalidate_vontology_caches(
                [source_id_value, target_value], correlation_id=correlation_id
            )
        except Exception:
            logger.debug("Relationship cache invalidation failed", exc_info=True)

        try:
            from .workflow_event_integration_service import (
                EVENT_TYPE_RELATIONSHIP_REMOVED,
                maybe_launch_vontology_mutation_workflow,
            )

            maybe_launch_vontology_mutation_workflow(
                mutation_event_type=EVENT_TYPE_RELATIONSHIP_REMOVED,
                mutation_id=f"{source_id_value}:{predicate_value}:{target_value}",
                user_id=resolved_actor_id,
                org_id=None,
                event_payload={
                    "source_id": source_id_value,
                    "predicate": predicate_value,
                    "target_id": target_value,
                    "mode": mode_value,
                    "cascade": cascade_value,
                    "request_id": request_id_value,
                },
                inputs={
                    "source_id": source_id_value,
                    "predicate": predicate_value,
                    "target_id": target_value,
                },
            )
        except Exception:
            logger.debug("Relationship removal event emission failed", exc_info=True)

    return audited_payload


def _collect_bulk_targets_from_filter(filter_spec: Mapping[str, Any]) -> Dict[str, Any]:
    source_id = filter_spec.get("source_id")
    predicate = filter_spec.get("predicate")
    target = filter_spec.get("target")
    limit_raw = filter_spec.get("limit", 200)
    try:
        limit_value = max(1, min(int(limit_raw), 2000))
    except Exception:
        limit_value = 200

    resolved = _resolve_selector(
        source_id=source_id,
        predicate=predicate,
        target=target,
        require_target=False,
    )
    if not resolved.get("ok"):
        if resolved.get("status") == "ambiguous":
            candidates = resolved.get("candidates", [])
            candidates = candidates[:limit_value]
            return {
                "ok": True,
                "targets": candidates,
                "source_id": source_id,
                "predicate": predicate,
            }
        return {"ok": False, "error": resolved}

    if target:
        return {
            "ok": True,
            "targets": [
                {
                    "target": resolved.get("target"),
                    "relation_id": resolved.get("relation_id"),
                }
            ],
            "source_id": resolved.get("source_id"),
            "predicate": resolved.get("predicate"),
        }

    selector_source = str(resolved.get("source_id") or "")
    selector_predicate = str(resolved.get("predicate") or "")
    if not selector_source or not selector_predicate:
        return {
            "ok": False,
            "error": {
                "status": "error",
                "error_code": "invalid_filter_selector",
                "error": "Filter selector did not resolve source/predicate",
            },
        }
    source_doc = ConceptsRepository.find_one(
        {"concept_id": selector_source},
        {f"relationships.{selector_predicate}": 1},
    )
    source_relationships_raw = (
        source_doc.get("relationships") if isinstance(source_doc, dict) else None
    )
    source_relationships: dict[str, Any] = (
        source_relationships_raw if isinstance(source_relationships_raw, dict) else {}
    )
    values = _normalise_relationship_values(
        source_relationships.get(selector_predicate)
    )
    targets = [
        {
            "target": value,
            "relation_id": build_relationship_relation_id(
                str(selector_source), str(selector_predicate), value
            ),
        }
        for value in values[:limit_value]
    ]
    return {
        "ok": True,
        "targets": targets,
        "source_id": selector_source,
        "predicate": selector_predicate,
    }


def remove_relationships_bulk(
    *,
    relation_ids: Any = None,
    relations: Any = None,
    filter: Any = None,
    mode: Any = "soft_delete",
    cascade: Any = "warn",
    dry_run: Any = False,
    confirmed: Any = False,
    operator_override: Any = False,
    reason: Any = None,
    request_id: Any = None,
    stop_on_error: Any = False,
) -> Dict[str, Any]:
    """Bulk relationship removal with deterministic partial-failure reporting."""
    correlation_id = (
        request_id.strip() if isinstance(request_id, str) and request_id.strip() else str(uuid.uuid4())
    )
    dry_run_value = _as_bool(dry_run, False)
    stop_on_error_value = _as_bool(stop_on_error, False)
    relation_id_list = (
        [item for item in relation_ids if isinstance(item, str) and item.strip()]
        if isinstance(relation_ids, list)
        else []
    )
    relation_specs = (
        [item for item in relations if isinstance(item, dict)] if isinstance(relations, list) else []
    )
    filter_spec = filter if isinstance(filter, dict) else None

    candidates: list[dict[str, Any]] = []
    seen_relation_ids: set[str] = set()

    for raw_relation_id in relation_id_list:
        parsed = parse_relationship_relation_id(raw_relation_id)
        if not parsed:
            candidates.append(
                {
                    "relation_id": raw_relation_id,
                    "error": "Invalid relation_id format",
                    "error_code": "invalid_relation_id",
                }
            )
            continue
        rid = parsed["relation_id"]
        if rid in seen_relation_ids:
            continue
        seen_relation_ids.add(rid)
        candidates.append(parsed)

    for spec in relation_specs:
        parsed = parse_relationship_relation_id(spec.get("relation_id"))
        if parsed:
            rid = parsed["relation_id"]
            if rid not in seen_relation_ids:
                seen_relation_ids.add(rid)
                candidates.append(parsed)
            continue
        source_id = spec.get("source_id")
        predicate = spec.get("predicate")
        target = spec.get("target")
        selector = _resolve_selector(
            source_id=source_id,
            predicate=predicate,
            target=target,
            require_target=True,
        )
        if selector.get("ok"):
            rid = str(selector["relation_id"])
            if rid not in seen_relation_ids:
                seen_relation_ids.add(rid)
                candidates.append(
                    {
                        "source_id": selector["source_id"],
                        "predicate": selector["predicate"],
                        "target": selector["target"],
                        "relation_id": selector["relation_id"],
                    }
                )
        else:
            candidates.append(
                {
                    "source_id": source_id,
                    "predicate": predicate,
                    "target": target,
                    "relation_id": spec.get("relation_id"),
                    "error": selector.get("error"),
                    "error_code": selector.get("error_code"),
                    "status": selector.get("status"),
                }
            )

    if filter_spec:
        filtered = _collect_bulk_targets_from_filter(filter_spec)
        if not filtered.get("ok"):
            filtered_error = filtered.get("error")
            filtered_error_dict = (
                filtered_error if isinstance(filtered_error, dict) else {}
            )
            candidates.append(
                {
                    "error": filtered_error_dict.get("error"),
                    "error_code": filtered_error_dict.get("error_code"),
                    "status": filtered_error_dict.get("status"),
                }
            )
        else:
            source_id = filtered.get("source_id")
            predicate = filtered.get("predicate")
            for target_info in filtered.get("targets", []):
                target_value = target_info.get("target")
                relation_id_value = target_info.get("relation_id")
                if not (
                    isinstance(source_id, str)
                    and isinstance(predicate, str)
                    and isinstance(target_value, str)
                    and isinstance(relation_id_value, str)
                ):
                    continue
                if relation_id_value in seen_relation_ids:
                    continue
                seen_relation_ids.add(relation_id_value)
                candidates.append(
                    {
                        "source_id": source_id,
                        "predicate": predicate,
                        "target": target_value,
                        "relation_id": relation_id_value,
                    }
                )

    valid_candidates = [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and isinstance(candidate.get("source_id"), str)
        and isinstance(candidate.get("predicate"), str)
        and isinstance(candidate.get("target"), str)
    ]
    validation_failures = [
        candidate
        for candidate in candidates
        if candidate not in valid_candidates
    ]

    if not valid_candidates and not dry_run_value:
        return {
            "success": False,
            "status": "error",
            "error": "No valid relationships resolved for bulk removal.",
            "error_code": "no_valid_targets",
            "correlation_id": correlation_id,
            "request_id": request_id,
            "results": validation_failures,
            "summary": {
                "total_requested": len(candidates),
                "total_valid": 0,
                "removed_count": 0,
                "already_absent_count": 0,
                "error_count": len(validation_failures),
            },
        }

    results: list[dict[str, Any]] = []
    undo_token = f"undo:{correlation_id}" if _normalise_mode(mode) == "soft_delete" else None
    removed_count = 0
    already_absent_count = 0
    error_count = len(validation_failures)
    results.extend(validation_failures)

    for candidate in valid_candidates:
        item_result = remove_relationship(
            relation_id=candidate.get("relation_id"),
            source_id=candidate.get("source_id"),
            predicate=candidate.get("predicate"),
            target=candidate.get("target"),
            mode=mode,
            cascade=cascade,
            dry_run=dry_run,
            confirmed=confirmed,
            operator_override=operator_override,
            reason=reason,
            request_id=correlation_id,
            bulk_operation=True,
            undo_token=undo_token,
        )
        item_result["bulk_item"] = {
            "source_id": candidate.get("source_id"),
            "predicate": candidate.get("predicate"),
            "target": candidate.get("target"),
            "relation_id": candidate.get("relation_id"),
        }
        results.append(item_result)
        if item_result.get("removed") is True:
            removed_count += 1
        elif item_result.get("already_absent") is True:
            already_absent_count += 1
        elif item_result.get("success") is False:
            error_count += 1
        if stop_on_error_value and item_result.get("success") is False:
            break

    status = "ok"
    if error_count > 0 and removed_count > 0:
        status = "partial"
    elif error_count > 0:
        status = "error"
    elif removed_count == 0:
        status = "no_op"

    payload: Dict[str, Any] = {
        "success": status in {"ok", "partial", "no_op"},
        "status": status,
        "dry_run": dry_run_value,
        "mode_returned": _normalise_mode(mode),
        "cascade_policy": _normalise_cascade(cascade),
        "correlation_id": correlation_id,
        "request_id": request_id,
        "results": results,
        "summary": {
            "total_requested": len(candidates),
            "total_valid": len(valid_candidates),
            "removed_count": removed_count,
            "already_absent_count": already_absent_count,
            "error_count": error_count,
        },
        "document_model_note": (
            "Document-model storage removes edges from relationship arrays. "
            "Soft-delete mode uses tombstone snapshots for undo."
        ),
    }
    if undo_token and removed_count > 0 and not dry_run_value:
        payload["undo_token"] = undo_token
    return payload


def undo_relationship_removal(
    *,
    undo_token: Any,
    request_id: Any = None,
    confirmed: Any = True,
) -> Dict[str, Any]:
    """Restore relationships captured by a soft-delete undo token."""
    if not isinstance(undo_token, str) or not undo_token.strip():
        return {
            "success": False,
            "status": "error",
            "error": "Missing undo_token",
            "error_code": "missing_parameter",
        }

    if not _as_bool(confirmed, True):
        return {
            "success": False,
            "status": "confirmation_required",
            "error": "Set confirmed=true to execute undo.",
            "error_code": "confirmation_required",
        }

    undo_token_value = undo_token.strip()
    correlation_id = (
        request_id.strip() if isinstance(request_id, str) and request_id.strip() else str(uuid.uuid4())
    )
    actor_id, actor_role = _resolve_actor_context()

    try:
        tombstones = list(
            _tombstone_collection().find(
                {"undo_token": undo_token_value},
                {
                    "_id": 0,
                    "source_id": 1,
                    "predicate": 1,
                    "target": 1,
                    "relation_id": 1,
                    "operation_id": 1,
                    "request_id": 1,
                },
            )
        )
    except Exception as exc:
        return {
            "success": False,
            "status": "error",
            "error": f"Failed to load tombstones: {exc}",
            "error_code": "undo_lookup_failed",
            "error_details": {"exception_type": type(exc).__name__},
        }

    if not tombstones:
        return {
            "success": False,
            "status": "not_found",
            "error": "undo_token not found",
            "error_code": "undo_token_not_found",
            "undo_token": undo_token_value,
        }

    restored_count = 0
    already_restored_count = 0
    errors: list[dict[str, Any]] = []

    for tombstone in tombstones:
        source_id = tombstone.get("source_id")
        predicate = tombstone.get("predicate")
        target = tombstone.get("target")
        if not (
            isinstance(source_id, str)
            and isinstance(predicate, str)
            and isinstance(target, str)
        ):
            errors.append(
                {
                    "source_id": source_id,
                    "predicate": predicate,
                    "target": target,
                    "error": "Invalid tombstone triple",
                    "error_code": "invalid_tombstone",
                }
            )
            continue
        try:
            changed = ConceptsRepository.mutate_relationship_edge(
                source_id,
                predicate,
                target,
                action="add",
                maintain_inverse=True,
            )
            if changed:
                restored_count += 1
            else:
                already_restored_count += 1
        except Exception as exc:
            errors.append(
                {
                    "source_id": source_id,
                    "predicate": predicate,
                    "target": target,
                    "error": f"Failed to restore: {exc}",
                    "error_code": "undo_mutation_failed",
                    "error_details": {"exception_type": type(exc).__name__},
                }
            )

    status = "ok"
    if errors and restored_count > 0:
        status = "partial"
    elif errors:
        status = "error"
    elif restored_count == 0:
        status = "no_op"

    result = {
        "success": status in {"ok", "partial", "no_op"},
        "status": status,
        "undo_token": undo_token_value,
        "correlation_id": correlation_id,
        "request_id": request_id,
        "restored_count": restored_count,
        "already_restored_count": already_restored_count,
        "error_count": len(errors),
        "errors": errors,
    }

    try:
        _append_audit_record(
            {
                "operation": "undo_relationship_removal",
                "phase": "result",
                "status": status,
                "operation_id": correlation_id,
                "request_id": request_id,
                "actor_id": actor_id,
                "actor_role": actor_role,
                "undo_token": undo_token_value,
                "restored_count": restored_count,
                "already_restored_count": already_restored_count,
                "error_count": len(errors),
            }
        )
    except Exception:
        logger.warning(
            "Failed to persist undo audit record for token %s", undo_token_value, exc_info=True
        )

    return result


__all__ = [
    "build_relationship_relation_id",
    "parse_relationship_relation_id",
    "preview_remove_relationship",
    "remove_relationship",
    "remove_relationships_bulk",
    "undo_relationship_removal",
]
