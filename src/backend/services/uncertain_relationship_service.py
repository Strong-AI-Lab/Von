"""Canonical uncertain relationship assertion lifecycle helpers.

This module provides a first-class representation for uncertain relation
assertions while preserving compatibility with legacy ``hypothesized_relations``
payloads during migration.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
import uuid

from ..db.repositories.concepts_repository import ConceptsRepository
from .relationship_write_service import add_relationship
from .text_value_service import upsert_text_for_concept

UNCERTAIN_RELATIONSHIP_STATUSES: frozenset[str] = frozenset(
    {"proposed", "promoted", "rejected", "archived"}
)
ACTIVE_UNCERTAIN_RELATIONSHIP_STATUSES: frozenset[str] = frozenset({"proposed"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_confidence(value: Any) -> float:
    return max(0.0, min(1.0, _coerce_float(value, default=0.0)))


def _normalise_status(value: Any, default: str = "proposed") -> str:
    status = str(value or default).strip().lower() or default
    if status not in UNCERTAIN_RELATIONSHIP_STATUSES:
        return default
    return status


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _resolve_target_kind(target: str) -> str:
    return "concept" if isinstance(target, str) and target.startswith("#V#") else "text"


def _build_legacy_assertion_id(
    *,
    source_id: str,
    predicate: str,
    target: str,
    source_marker: str,
    index: int,
) -> str:
    digest_seed = f"{source_id}|{predicate}|{target}|{source_marker}|{index}"
    digest = hashlib.sha1(digest_seed.encode("utf-8")).hexdigest()[:18]
    return f"legacy_hyp_{digest}"


def _build_assertion_id() -> str:
    return f"ura_{uuid.uuid4().hex}"


def _normalise_assertion_record(
    raw: Mapping[str, Any],
    *,
    source_id: str,
) -> dict[str, Any]:
    predicate = _as_text(raw.get("predicate"))
    target = _as_text(raw.get("target"))
    target_kind = _as_text(raw.get("target_kind")) or _resolve_target_kind(target)
    assertion_id = _as_text(raw.get("assertion_id")) or _build_assertion_id()
    status = _normalise_status(raw.get("status"))

    created_at = _as_text(raw.get("created_at_utc")) or _now_iso()
    updated_at = _as_text(raw.get("updated_at_utc")) or created_at
    confidence_score = _clamp_confidence(raw.get("confidence_score"))

    provenance_raw = raw.get("provenance")
    provenance = dict(provenance_raw) if isinstance(provenance_raw, Mapping) else {}

    assertion: dict[str, Any] = {
        "assertion_id": assertion_id,
        "source_id": source_id,
        "predicate": predicate,
        "target": target,
        "target_kind": target_kind,
        "confidence_score": confidence_score,
        "status": status,
        "provenance": provenance,
        "created_at_utc": created_at,
        "updated_at_utc": updated_at,
    }

    for field_name in (
        "promoted_at_utc",
        "rejected_at_utc",
        "rejection_reason",
        "promoted_relation_id",
        "promoted_relation_kind",
        "legacy_hypothesis_ref",
    ):
        if raw.get(field_name) is not None:
            assertion[field_name] = raw.get(field_name)
    return assertion


def _validate_assertion_integrity(assertion: Mapping[str, Any]) -> Optional[str]:
    status = _normalise_status(assertion.get("status"))
    has_promoted = bool(assertion.get("promoted_at_utc"))
    has_rejected = bool(assertion.get("rejected_at_utc")) or bool(
        assertion.get("rejection_reason")
    )
    if has_promoted and has_rejected:
        return "conflicting_terminal_state"
    if status == "promoted" and not has_promoted:
        return "missing_promotion_timestamp"
    if status == "rejected" and not has_rejected:
        return "missing_rejection_reason"
    return None


def _load_canonical_assertions(
    source_doc: Mapping[str, Any],
    *,
    source_id: str,
) -> list[dict[str, Any]]:
    rows = source_doc.get("uncertain_relationship_assertions")
    if not isinstance(rows, list):
        return []
    output: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        record = _normalise_assertion_record(item, source_id=source_id)
        if not record.get("predicate") or not record.get("target"):
            continue
        output.append(record)
    return output


def _extract_legacy_assertions(
    source_doc: Mapping[str, Any],
    *,
    source_id: str,
) -> list[dict[str, Any]]:
    legacy = source_doc.get("hypothesized_relations")
    if not isinstance(legacy, Mapping):
        return []

    assertions: list[dict[str, Any]] = []
    for predicate, payload in legacy.items():
        predicate_value = _as_text(predicate)
        if not predicate_value:
            continue
        hypotheses: list[Any]
        if isinstance(payload, list):
            hypotheses = payload
        elif isinstance(payload, Mapping):
            hypotheses = [payload]
        else:
            continue

        for index, hypothesis in enumerate(hypotheses):
            if not isinstance(hypothesis, Mapping):
                continue
            target = _as_text(hypothesis.get("value"))
            if not target:
                continue

            source_marker = _as_text(hypothesis.get("source_interaction_id")) or _as_text(
                hypothesis.get("source")
            )
            assertion_id = (
                _as_text(hypothesis.get("hypothesis_id"))
                or _as_text(hypothesis.get("id"))
                or _build_legacy_assertion_id(
                    source_id=source_id,
                    predicate=predicate_value,
                    target=target,
                    source_marker=source_marker or "unknown_source",
                    index=index,
                )
            )
            evidence_count = 1
            if isinstance(hypothesis.get("evidence"), list):
                evidence_count = len(hypothesis.get("evidence") or [])
            if isinstance(hypothesis.get("evidence_count"), int):
                evidence_count = int(hypothesis["evidence_count"])

            provenance: dict[str, Any] = {"source": "legacy_hypothesized_relations"}
            if source_marker:
                provenance["source_interaction_id"] = source_marker
            if _as_text(hypothesis.get("source")):
                provenance["legacy_source"] = _as_text(hypothesis.get("source"))

            assertions.append(
                {
                    "assertion_id": assertion_id,
                    "source_id": source_id,
                    "predicate": predicate_value,
                    "target": target,
                    "target_kind": _resolve_target_kind(target),
                    "confidence_score": _clamp_confidence(
                        hypothesis.get("confidence_score")
                    ),
                    "status": "proposed",
                    "provenance": provenance,
                    "created_at_utc": _now_iso(),
                    "updated_at_utc": _now_iso(),
                    "evidence_count": max(1, evidence_count),
                    "legacy_hypothesis_ref": {
                        "predicate": predicate_value,
                        "index": index,
                    },
                    "legacy_only": True,
                }
            )
    return assertions


def list_uncertain_relationship_assertions(
    *,
    source_id: str,
    predicate: Optional[str] = None,
    statuses: Optional[Sequence[str]] = None,
    include_legacy: bool = True,
    source_doc: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """List uncertain assertions for a concept, optionally including legacy-adapted rows."""
    if not isinstance(source_id, str) or not source_id.strip():
        return []
    source_id = source_id.strip()
    filter_predicate = _as_text(predicate)
    status_filter = (
        {_normalise_status(item) for item in statuses if isinstance(item, str)}
        if statuses
        else None
    )

    doc = source_doc
    if doc is None:
        doc = ConceptsRepository.find_one(
            {"concept_id": source_id},
            {
                "concept_id": 1,
                "uncertain_relationship_assertions": 1,
                "hypothesized_relations": 1,
            },
        )
    if not isinstance(doc, Mapping):
        return []

    canonical = _load_canonical_assertions(doc, source_id=source_id)
    by_id: dict[str, dict[str, Any]] = {
        str(item["assertion_id"]): dict(item) for item in canonical
    }

    if include_legacy:
        for legacy_row in _extract_legacy_assertions(doc, source_id=source_id):
            assertion_id = str(legacy_row["assertion_id"])
            if assertion_id not in by_id:
                by_id[assertion_id] = dict(legacy_row)

    rows = list(by_id.values())
    if filter_predicate:
        rows = [r for r in rows if _as_text(r.get("predicate")) == filter_predicate]
    if status_filter:
        rows = [
            r for r in rows if _normalise_status(r.get("status")) in status_filter
        ]

    rows.sort(
        key=lambda item: (
            str(item.get("updated_at_utc") or ""),
            str(item.get("assertion_id") or ""),
        ),
        reverse=True,
    )
    return rows


def collect_uncertain_predicates(
    *,
    source_id: str,
    source_doc: Optional[Mapping[str, Any]] = None,
    include_legacy: bool = True,
) -> set[str]:
    """Return predicate IDs that have uncertain assertions for a concept."""
    assertions = list_uncertain_relationship_assertions(
        source_id=source_id,
        include_legacy=include_legacy,
        source_doc=source_doc,
    )
    return {
        _as_text(item.get("predicate"))
        for item in assertions
        if _as_text(item.get("predicate"))
    }


def upsert_uncertain_relationship_assertion(
    *,
    source_id: str,
    predicate: str,
    target: str,
    confidence_score: float,
    provenance: Optional[Mapping[str, Any]] = None,
    status: str = "proposed",
    assertion_id: Optional[str] = None,
    evidence_count: Optional[int] = None,
) -> dict[str, Any]:
    """Create or update a canonical uncertain relation assertion."""
    if not isinstance(source_id, str) or not source_id.strip():
        return {"success": False, "error": "invalid_source_id"}

    source_id = source_id.strip()
    predicate_value = _as_text(predicate)
    target_value = _as_text(target)
    if not predicate_value:
        return {"success": False, "error": "invalid_predicate"}
    if not target_value:
        return {"success": False, "error": "invalid_target"}

    doc = ConceptsRepository.find_one(
        {"concept_id": source_id},
        {"concept_id": 1, "uncertain_relationship_assertions": 1},
    )
    if not doc:
        return {"success": False, "error": "source_not_found"}

    canonical = _load_canonical_assertions(doc, source_id=source_id)
    resolved_status = _normalise_status(status)
    now_iso = _now_iso()
    target_kind = _resolve_target_kind(target_value)

    incoming_assertion_id = _as_text(assertion_id)
    existing_index = -1
    if incoming_assertion_id:
        for idx, item in enumerate(canonical):
            if _as_text(item.get("assertion_id")) == incoming_assertion_id:
                existing_index = idx
                break
    if existing_index < 0:
        for idx, item in enumerate(canonical):
            if (
                _as_text(item.get("predicate")) == predicate_value
                and _as_text(item.get("target")) == target_value
                and _normalise_status(item.get("status")) in ACTIVE_UNCERTAIN_RELATIONSHIP_STATUSES
            ):
                existing_index = idx
                break

    if existing_index >= 0:
        assertion = dict(canonical[existing_index])
        assertion["confidence_score"] = _clamp_confidence(confidence_score)
        assertion["status"] = resolved_status
        assertion["updated_at_utc"] = now_iso
        assertion["predicate"] = predicate_value
        assertion["target"] = target_value
        assertion["target_kind"] = target_kind
        if isinstance(provenance, Mapping):
            merged = dict(assertion.get("provenance") or {})
            merged.update(dict(provenance))
            assertion["provenance"] = merged
        if isinstance(evidence_count, int):
            assertion["evidence_count"] = max(1, int(evidence_count))
        canonical[existing_index] = assertion
        created = False
    else:
        assertion = {
            "assertion_id": incoming_assertion_id or _build_assertion_id(),
            "source_id": source_id,
            "predicate": predicate_value,
            "target": target_value,
            "target_kind": target_kind,
            "confidence_score": _clamp_confidence(confidence_score),
            "status": resolved_status,
            "provenance": dict(provenance or {}),
            "created_at_utc": now_iso,
            "updated_at_utc": now_iso,
        }
        if isinstance(evidence_count, int):
            assertion["evidence_count"] = max(1, int(evidence_count))
        canonical.append(assertion)
        created = True

    integrity_error = _validate_assertion_integrity(assertion)
    if integrity_error:
        return {"success": False, "error": integrity_error}

    ConceptsRepository.update_one(
        {"concept_id": source_id},
        {"$set": {"uncertain_relationship_assertions": canonical}},
    )
    return {"success": True, "created": created, "assertion": assertion}


def promote_uncertain_relationship_assertion(
    *,
    source_id: str,
    assertion_id: str,
    operator: str = "system",
) -> dict[str, Any]:
    """Promote an uncertain assertion into an asserted relationship/text relation."""
    source_id = _as_text(source_id)
    assertion_id = _as_text(assertion_id)
    if not source_id:
        return {"success": False, "error": "invalid_source_id"}
    if not assertion_id:
        return {"success": False, "error": "invalid_assertion_id"}

    source_doc = ConceptsRepository.find_one(
        {"concept_id": source_id},
        {
            "concept_id": 1,
            "uncertain_relationship_assertions": 1,
            "hypothesized_relations": 1,
        },
    )
    if not source_doc:
        return {"success": False, "error": "source_not_found"}

    canonical = _load_canonical_assertions(source_doc, source_id=source_id)
    idx = -1
    for item_idx, item in enumerate(canonical):
        if _as_text(item.get("assertion_id")) == assertion_id:
            idx = item_idx
            break

    if idx < 0:
        # Materialise a canonical row from legacy view on demand.
        legacy_rows = _extract_legacy_assertions(source_doc, source_id=source_id)
        legacy_row = next(
            (item for item in legacy_rows if _as_text(item.get("assertion_id")) == assertion_id),
            None,
        )
        if legacy_row is None:
            return {"success": False, "error": "assertion_not_found"}
        canonical.append(
            _normalise_assertion_record(legacy_row, source_id=source_id)
        )
        idx = len(canonical) - 1

    assertion = dict(canonical[idx])
    status = _normalise_status(assertion.get("status"))
    if status in {"rejected", "archived"}:
        return {"success": False, "error": "assertion_not_promotable", "status": status}
    if status == "promoted":
        return {"success": True, "already_promoted": True, "assertion": assertion}

    predicate = _as_text(assertion.get("predicate"))
    target = _as_text(assertion.get("target"))
    target_kind = _as_text(assertion.get("target_kind")) or _resolve_target_kind(target)

    relation_result: dict[str, Any]
    relation_kind = "concept_relationship"
    if target_kind == "concept":
        relation_result = add_relationship(source_id, predicate, target)
        if not bool(relation_result.get("success")):
            return {"success": False, "error": "promotion_write_failed", "result": relation_result}
        promoted_relation_id = relation_result.get("relation_id")
    else:
        relation_kind = "text_relation"
        text_result = upsert_text_for_concept(
            subject_concept_id=source_id,
            predicate=predicate,
            text=target,
            lang="en-NZ",
            provenance={
                "source": "uncertain_relationship_promotion",
                "assertion_id": assertion_id,
                "operator": operator,
            },
            context={
                "assertion_id": assertion_id,
                "source": "uncertain_relationship_service",
            },
        )
        relation_result = dict(text_result)
        promoted_relation_id = text_result.get("relation_id")

    now_iso = _now_iso()
    assertion["status"] = "promoted"
    assertion["target_kind"] = target_kind
    assertion["promoted_at_utc"] = now_iso
    assertion["updated_at_utc"] = now_iso
    assertion["promoted_relation_id"] = promoted_relation_id
    assertion["promoted_relation_kind"] = relation_kind
    assertion.pop("rejected_at_utc", None)
    assertion.pop("rejection_reason", None)

    integrity_error = _validate_assertion_integrity(assertion)
    if integrity_error:
        return {"success": False, "error": integrity_error}

    canonical[idx] = assertion
    ConceptsRepository.update_one(
        {"concept_id": source_id},
        {"$set": {"uncertain_relationship_assertions": canonical}},
    )
    return {
        "success": True,
        "assertion": assertion,
        "write_result": relation_result,
    }


def reject_uncertain_relationship_assertion(
    *,
    source_id: str,
    assertion_id: str,
    reason: str,
    operator: str = "system",
) -> dict[str, Any]:
    """Reject an uncertain assertion while preserving provenance and reason."""
    source_id = _as_text(source_id)
    assertion_id = _as_text(assertion_id)
    rejection_reason = _as_text(reason)
    if not source_id:
        return {"success": False, "error": "invalid_source_id"}
    if not assertion_id:
        return {"success": False, "error": "invalid_assertion_id"}
    if not rejection_reason:
        return {"success": False, "error": "invalid_rejection_reason"}

    source_doc = ConceptsRepository.find_one(
        {"concept_id": source_id},
        {
            "concept_id": 1,
            "uncertain_relationship_assertions": 1,
            "hypothesized_relations": 1,
        },
    )
    if not source_doc:
        return {"success": False, "error": "source_not_found"}

    canonical = _load_canonical_assertions(source_doc, source_id=source_id)
    idx = -1
    for item_idx, item in enumerate(canonical):
        if _as_text(item.get("assertion_id")) == assertion_id:
            idx = item_idx
            break

    if idx < 0:
        legacy_rows = _extract_legacy_assertions(source_doc, source_id=source_id)
        legacy_row = next(
            (item for item in legacy_rows if _as_text(item.get("assertion_id")) == assertion_id),
            None,
        )
        if legacy_row is None:
            return {"success": False, "error": "assertion_not_found"}
        canonical.append(
            _normalise_assertion_record(legacy_row, source_id=source_id)
        )
        idx = len(canonical) - 1

    assertion = dict(canonical[idx])
    status = _normalise_status(assertion.get("status"))
    if status == "promoted":
        return {"success": False, "error": "assertion_already_promoted"}
    if status == "rejected":
        return {"success": True, "already_rejected": True, "assertion": assertion}

    now_iso = _now_iso()
    assertion["status"] = "rejected"
    assertion["rejected_at_utc"] = now_iso
    assertion["updated_at_utc"] = now_iso
    assertion["rejection_reason"] = rejection_reason
    prov = dict(assertion.get("provenance") or {})
    prov["rejected_by"] = operator
    assertion["provenance"] = prov
    assertion.pop("promoted_at_utc", None)
    assertion.pop("promoted_relation_id", None)
    assertion.pop("promoted_relation_kind", None)

    integrity_error = _validate_assertion_integrity(assertion)
    if integrity_error:
        return {"success": False, "error": integrity_error}

    canonical[idx] = assertion
    ConceptsRepository.update_one(
        {"concept_id": source_id},
        {"$set": {"uncertain_relationship_assertions": canonical}},
    )
    return {"success": True, "assertion": assertion}


def migrate_legacy_hypothesized_relations(
    *,
    source_id: str,
    predicate: Optional[str] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Persist canonical assertion rows from legacy hypothesized relations."""
    source_id = _as_text(source_id)
    if not source_id:
        return {"success": False, "error": "invalid_source_id"}
    filter_predicate = _as_text(predicate)

    source_doc = ConceptsRepository.find_one(
        {"concept_id": source_id},
        {
            "concept_id": 1,
            "uncertain_relationship_assertions": 1,
            "hypothesized_relations": 1,
        },
    )
    if not source_doc:
        return {"success": False, "error": "source_not_found"}

    canonical = _load_canonical_assertions(source_doc, source_id=source_id)
    existing_ids = {_as_text(item.get("assertion_id")) for item in canonical}
    legacy_rows = _extract_legacy_assertions(source_doc, source_id=source_id)
    if filter_predicate:
        legacy_rows = [
            item
            for item in legacy_rows
            if _as_text(item.get("predicate")) == filter_predicate
        ]

    migrated_rows: list[dict[str, Any]] = []
    for row in legacy_rows:
        assertion_id = _as_text(row.get("assertion_id"))
        if assertion_id and assertion_id in existing_ids:
            continue
        migrated = _normalise_assertion_record(row, source_id=source_id)
        migrated["provenance"] = {
            **dict(migrated.get("provenance") or {}),
            "source": "legacy_hypothesized_relations_migration",
        }
        migrated_rows.append(migrated)
        existing_ids.add(assertion_id)

    if not dry_run and migrated_rows:
        ConceptsRepository.update_one(
            {"concept_id": source_id},
            {"$set": {"uncertain_relationship_assertions": canonical + migrated_rows}},
        )

    return {
        "success": True,
        "dry_run": bool(dry_run),
        "source_id": source_id,
        "migrated_count": len(migrated_rows),
        "migrated_assertion_ids": [row["assertion_id"] for row in migrated_rows],
    }

