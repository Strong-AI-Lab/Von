"""Load and evaluate inheritable constitutive relation requirements.

Constitutive requirements are represented on type concepts as JSON text values.
They are deliberately separate from salient predicates: salience says that a
relation is useful to ask about, while a constitutive requirement says that an
instance remains incomplete until a qualifying relation is represented.

Runtime evaluation is read-only.  It reports missing requirements for
elicitation but does not reject concept creation or write a relation from free
text. The versioned seed bundle is the single repository source for the
temporary compatibility fallback and explicit release materialisation; a valid
live Vontology profile remains authoritative and is never overwritten.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from ..vontology.utils_vontology import get_vontology_node_and_descendant_ids
from . import concept_service
from . import startup_seed_freshness_service as seed_freshness
from .text_value_service import (
    get_texts_for_concepts,
    upsert_singleton_text_relation,
)

CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION = "constitutive_relation_requirements.v1"
CONSTITUTIVE_REQUIREMENT_SEED_SCHEMA_VERSION = (
    "constitutive_relation_requirement_seed_bundle.v1"
)
CONSTITUTIVE_REQUIREMENT_STARTUP_FAMILY_ID = "constitutive_relation_requirements"
CONSTITUTIVE_REQUIREMENT_STARTUP_PRODUCER_SCHEMA_VERSION = (
    "constitutive_relation_requirement_startup_materialisation.v1"
)
CONSTITUTIVE_REQUIREMENTS_PREDICATE = "#V#has_constitutive_relation_requirements_json"
CONSTITUTIVE_REQUIREMENTS_TEXT_PREDICATES: tuple[str, ...] = (
    CONSTITUTIVE_REQUIREMENTS_PREDICATE,
    "has_constitutive_relation_requirements_json",
    "hasConstitutiveRelationRequirementsJson",
)

VON_USER_ORGANISATION_TYPE_ID = "#V#von_user_organisation"
VON_USER_TYPE_ID = "#V#von_user"
MEMBER_OF_PREDICATE_ID = "#V#memberOfVonOrg"

_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "vontology"
    / "seed_bundles"
    / "constitutive_relation_requirements_seed_bundle.json"
)


def _load_seed_bundle(asset_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(asset_path or _SEED_ASSET_PATH).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("constitutive_requirement_seed_bundle_not_mapping")
    if _text(payload.get("schema_version")) != (
        CONSTITUTIVE_REQUIREMENT_SEED_SCHEMA_VERSION
    ):
        raise ValueError("constitutive_requirement_seed_bundle_schema_unsupported")
    profiles = payload.get("profiles")
    if not isinstance(profiles, Sequence) or isinstance(profiles, (str, bytes)):
        raise TypeError("constitutive_requirement_seed_profiles_invalid")
    seen_type_ids: set[str] = set()
    for spec in profiles:
        if not isinstance(spec, Mapping):
            raise TypeError("constitutive_requirement_seed_profile_not_mapping")
        type_id = _text(spec.get("declaring_type_concept_id"))
        if not type_id:
            raise ValueError("constitutive_requirement_seed_type_missing")
        if type_id in seen_type_ids:
            raise ValueError("constitutive_requirement_seed_type_duplicate")
        seen_type_ids.add(type_id)
        _normalise_profile(spec.get("payload"))
    return {**dict(payload), "asset_path": str(path)}


def _profile_blueprints_from_bundle(
    bundle: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        str(spec["declaring_type_concept_id"]): json.loads(json.dumps(spec["payload"]))
        for spec in bundle["profiles"]
    }


def canonical_constitutive_requirement_profile_blueprints(
    *, asset_path: str | Path | None = None
) -> dict[str, dict[str, Any]]:
    """Load copy-on-read starter profiles from the versioned release input."""

    return _profile_blueprints_from_bundle(_load_seed_bundle(asset_path))


def _startup_freshness_scope(bundle: Mapping[str, Any]) -> dict[str, list[str]]:
    type_ids = [
        str(spec.get("declaring_type_concept_id") or "").strip()
        for spec in bundle.get("profiles") or ()
        if isinstance(spec, Mapping)
        and str(spec.get("declaring_type_concept_id") or "").strip()
    ]
    return {
        "concept_ids": sorted(set(type_ids)),
        "text_relation_subject_ids": sorted(set(type_ids)),
        "text_relation_predicates": sorted(
            set(CONSTITUTIVE_REQUIREMENTS_TEXT_PREDICATES)
        ),
    }


def _startup_source_digest(bundle: Mapping[str, Any]) -> str:
    return seed_freshness.build_startup_seed_source_digest(
        asset_path=str(bundle.get("asset_path") or ""),
        producer_paths=[Path(__file__), Path(seed_freshness.__file__)],
        material_configuration={
            "seed_schema_version": CONSTITUTIVE_REQUIREMENT_SEED_SCHEMA_VERSION,
            "profile_schema_version": CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION,
            "producer_schema_version": (
                CONSTITUTIVE_REQUIREMENT_STARTUP_PRODUCER_SCHEMA_VERSION
            ),
        },
    )


def ensure_canonical_constitutive_relation_requirement_profiles(
    *,
    type_concept_ids: Sequence[str] | None = None,
    predicate: str = CONSTITUTIVE_REQUIREMENTS_PREDICATE,
    language: str = "en-NZ",
    provenance: Mapping[str, Any] | None = None,
    asset_path: str | Path | None = None,
    freshness_context: Mapping[str, Any] | None = None,
    concept_getter: Callable[[str], Any] | None = None,
    text_writer: Callable[..., Mapping[str, Any]] | None = None,
    text_reader: Callable[..., Mapping[str, list[Mapping[str, Any]]]] | None = None,
) -> dict[str, Any]:
    """Materialise starter profiles and verify their exact represented read-back.

    This is an explicit seed/maintenance operation, not an import-time write.
    Missing declaring types are reported rather than implicitly created.
    """

    started_at = time.perf_counter()
    bundle = _load_seed_bundle(asset_path)
    source_tag = _text(bundle.get("source_tag")) or "JVNAUTOSCI-1035"
    managed_by = (
        _text(bundle.get("managed_by")) or "constitutive_relation_requirement_service"
    )
    getter = concept_getter or concept_service.get_concept_by_concept_id
    writer = text_writer or upsert_singleton_text_relation
    reader = text_reader or get_texts_for_concepts
    blueprints = _profile_blueprints_from_bundle(bundle)
    requested = list(
        dict.fromkeys(
            type_id
            for type_id in (type_concept_ids or tuple(blueprints))
            if isinstance(type_id, str) and type_id
        )
    )
    persisted: list[str] = []
    read_back: list[str] = []
    represented_overrides: list[str] = []
    changed_type_ids: list[str] = []
    missing: list[str] = []
    unknown: list[str] = []
    errors_by_type: dict[str, str] = {}
    concepts_by_type: dict[str, Mapping[str, Any]] = {}
    tracked_rows_by_type: dict[str, list[Mapping[str, Any]]] = {}
    read_predicates = list(
        dict.fromkeys([predicate, *CONSTITUTIVE_REQUIREMENTS_TEXT_PREDICATES])
    )
    text_query_metadata: dict[str, Any] = {}

    def read_rows(type_ids: Sequence[str]) -> Mapping[str, list[Mapping[str, Any]]]:
        return reader(
            list(type_ids),
            predicates=read_predicates,
            limit_per_concept=10,
            recent_first=True,
            query_metadata=text_query_metadata,
        )

    def select_profile(
        rows: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]] | None, Mapping[str, Any] | None, bool]:
        found = False
        for selected_predicate in read_predicates:
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                if _text(row.get("predicate")) != selected_predicate:
                    continue
                found = True
                try:
                    represented = json.loads(str(row.get("text") or ""))
                    return _normalise_profile(represented), row, found
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        return None, None, found

    for type_id in requested:
        profile = blueprints.get(type_id)
        if profile is None:
            unknown.append(type_id)
            continue
        try:
            concept = getter(type_id)
        except Exception:  # noqa: BLE001 - maintenance reports dependency failure
            concept = None
        if not isinstance(concept, Mapping):
            missing.append(type_id)
            continue
        concepts_by_type[type_id] = concept

    readable_type_ids = [
        type_id
        for type_id in requested
        if type_id in concepts_by_type and type_id not in unknown
    ]
    try:
        initial_rows_by_type = read_rows(readable_type_ids)
        if not isinstance(initial_rows_by_type, Mapping):
            raise TypeError("profile_read_result_not_mapping")
    except Exception as exc:  # noqa: BLE001 - maintenance returns typed receipt
        initial_rows_by_type = {}
        for type_id in readable_type_ids:
            errors_by_type[type_id] = f"profile_read_failed:{exc}"

    for type_id in readable_type_ids:
        if type_id in errors_by_type:
            continue
        profile = blueprints[type_id]
        rows = [
            row
            for row in initial_rows_by_type.get(type_id, [])
            if isinstance(row, Mapping)
        ]
        normalised_existing, _selected_row, represented_found = select_profile(rows)
        if represented_found and normalised_existing is None:
            # Never overwrite a malformed represented policy with a repo
            # fallback.  It needs an explicit Vontology repair.
            errors_by_type[type_id] = "represented_profile_malformed"
            tracked_rows_by_type[type_id] = rows
            continue
        if normalised_existing is not None:
            if normalised_existing != _normalise_profile(profile):
                represented_overrides.append(type_id)
            tracked_rows_by_type[type_id] = rows
            read_back.append(type_id)
            continue

        try:
            _normalise_profile(profile)
            result = writer(
                subject_concept_id=type_id,
                predicate=predicate,
                text=json.dumps(profile, ensure_ascii=True, sort_keys=True),
                lang=language,
                policy="replace_others",
                provenance={
                    "source": managed_by,
                    "source_tag": source_tag,
                    **dict(provenance or {}),
                },
                context={},
                garbage_collect=True,
            )
        except Exception as exc:  # noqa: BLE001 - maintenance returns typed receipt
            errors_by_type[type_id] = f"profile_write_failed:{exc}"
            continue
        if not bool(result.get("success")):
            errors_by_type[type_id] = "profile_write_unsuccessful"
            continue
        persisted.append(type_id)
        changed_type_ids.append(type_id)

        try:
            rows_by_type = read_rows([type_id])
            rows = rows_by_type.get(type_id, [])
            normalised_represented, _selected_row, _found = select_profile(rows)
            if normalised_represented != _normalise_profile(profile):
                raise ValueError("profile_read_back_mismatch")
        except Exception as exc:  # noqa: BLE001 - read-back failure is reported
            errors_by_type[type_id] = f"profile_read_back_failed:{exc}"
            continue
        tracked_rows_by_type[type_id] = [
            row for row in rows if isinstance(row, Mapping)
        ]
        read_back.append(type_id)

    success = not (missing or unknown or errors_by_type) and len(read_back) == len(
        requested
    )
    report: dict[str, Any] = {
        "success": success,
        "profile_source": "vontology_type_text_relations",
        "asset_path": bundle.get("asset_path"),
        "seed_schema_version": bundle.get("schema_version"),
        "seed_version": bundle.get("seed_version"),
        "source_tag": source_tag,
        "managed_by": managed_by,
        "predicate": predicate,
        "requested_type_concept_ids": requested,
        "persisted_type_concept_ids": persisted,
        "read_back_type_concept_ids": read_back,
        "represented_override_type_concept_ids": represented_overrides,
        "changed_type_concept_ids": changed_type_ids,
        "changed": bool(changed_type_ids),
        "missing_type_concept_ids": missing,
        "unknown_type_concept_ids": unknown,
        "errors_by_type_concept_id": errors_by_type,
        "read_strategy": "bounded_canonical_state",
        "read_phases": 2 if changed_type_ids else 1,
        "canonical_read_batches": {
            "concepts": len(requested),
            "text_assertions": 1 + len(changed_type_ids),
        },
        "text_query_metadata": text_query_metadata,
        "duration_ms": int((time.perf_counter() - started_at) * 1000),
        "counts": {
            "profiles_requested": len(requested),
            "profiles_written": len(persisted),
            "profiles_changed": len(changed_type_ids),
            "represented_overrides_preserved": len(represented_overrides),
            "profiles_read_back": len(read_back),
            "errors": len(errors_by_type),
        },
    }

    if freshness_context is not None:
        canonical_read_complete = not bool(
            text_query_metadata.get("relation_query_truncated")
        )
        if success and not changed_type_ids and canonical_read_complete:
            scope = _startup_freshness_scope(bundle)
            receipt_result = seed_freshness.record_startup_seed_freshness(
                family_id=CONSTITUTIVE_REQUIREMENT_STARTUP_FAMILY_ID,
                source_digest=_text(freshness_context.get("source_digest")) or "",
                producer_schema_version=(
                    CONSTITUTIVE_REQUIREMENT_STARTUP_PRODUCER_SCHEMA_VERSION
                ),
                concept_ids=scope["concept_ids"],
                text_relation_subject_ids=scope["text_relation_subject_ids"],
                text_relation_predicates=scope["text_relation_predicates"],
                tracked_concept_document_ids=[
                    concept.get("_id") or concept.get("id")
                    for concept in concepts_by_type.values()
                    if concept.get("_id") is not None or concept.get("id") is not None
                ],
                tracked_text_relation_document_ids=[
                    row.get("relation_id") or row.get("_id")
                    for rows in tracked_rows_by_type.values()
                    for row in rows
                    if row.get("relation_id") is not None or row.get("_id") is not None
                ],
                tracked_text_value_document_ids=[
                    row.get("text_value_id") or row.get("object_text_id")
                    for rows in tracked_rows_by_type.values()
                    for row in rows
                    if row.get("text_value_id") is not None
                    or row.get("object_text_id") is not None
                ],
                observation=freshness_context.get("observation") or {},
                metadata={
                    "seed_schema_version": report.get("seed_schema_version"),
                    "seed_version": report.get("seed_version"),
                    "source_tag": source_tag,
                    "managed_by": managed_by,
                    "counts": report.get("counts"),
                },
            )
        else:
            receipt_result = {
                "persisted": False,
                "reason": (
                    "canonical_verification_failed"
                    if not success
                    else (
                        "canonical_read_truncated"
                        if not canonical_read_complete
                        else "canonical_state_changed"
                    )
                ),
            }
        report["freshness_receipt"] = (
            seed_freshness.public_startup_seed_freshness_result(receipt_result)
        )
    return report


def reconcile_canonical_constitutive_relation_requirement_profiles(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Apply and verify the release seed, then publish a freshness receipt.

    This is an explicit release/maintenance write. Ordinary process startup
    must call the read-only freshness check below instead.
    """

    started_at = time.perf_counter()
    bundle = _load_seed_bundle(asset_path)
    source_digest = _startup_source_digest(bundle)
    scope = _startup_freshness_scope(bundle)
    passes: list[dict[str, Any]] = []

    def run_canonical_pass() -> dict[str, Any]:
        observation = seed_freshness.begin_startup_seed_freshness_observation(
            concept_ids=scope["concept_ids"],
            text_relation_subject_ids=scope["text_relation_subject_ids"],
            text_relation_predicates=scope["text_relation_predicates"],
        )
        report = ensure_canonical_constitutive_relation_requirement_profiles(
            asset_path=asset_path,
            freshness_context={
                "source_digest": source_digest,
                "observation": observation,
            },
        )
        passes.append(report)
        return report

    final_report = run_canonical_pass()
    final_receipt = final_report.get("freshness_receipt")
    receipt_persisted = bool(
        isinstance(final_receipt, Mapping) and final_receipt.get("persisted")
    )
    if final_report.get("success") and (
        final_report.get("changed") or not receipt_persisted
    ):
        # A mutating pass cannot establish its own quiet verification window.
        final_report = run_canonical_pass()
        final_receipt = final_report.get("freshness_receipt")
        receipt_persisted = bool(
            isinstance(final_receipt, Mapping) and final_receipt.get("persisted")
        )

    ready = bool(
        final_report.get("success")
        and not final_report.get("changed")
        and receipt_persisted
    )
    receipt_reason = (
        _text(final_receipt.get("reason"))
        if isinstance(final_receipt, Mapping)
        else None
    )
    return {
        "schema_version": "startup_seed_reconciliation.v1",
        "family_id": CONSTITUTIVE_REQUIREMENT_STARTUP_FAMILY_ID,
        "success": ready,
        "ready": ready,
        "state": "ready" if ready else "unavailable",
        "reason": (
            "canonical_reconciliation_verified"
            if ready
            else receipt_reason or "canonical_reconciliation_unverified"
        ),
        "changed": any(bool(report.get("changed")) for report in passes),
        "reconciliation_pass_count": len(passes),
        "duration_ms": int((time.perf_counter() - started_at) * 1000),
        "freshness_receipt": (
            dict(final_receipt) if isinstance(final_receipt, Mapping) else {}
        ),
        "passes": passes,
    }


def ensure_constitutive_relation_requirement_profiles_current_for_startup(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Check freshness without running reconciliation or canonical writes."""

    started_at = time.perf_counter()
    bundle = _load_seed_bundle(asset_path)
    scope = _startup_freshness_scope(bundle)
    freshness_check = seed_freshness.check_startup_seed_freshness(
        family_id=CONSTITUTIVE_REQUIREMENT_STARTUP_FAMILY_ID,
        source_digest=_startup_source_digest(bundle),
        producer_schema_version=(
            CONSTITUTIVE_REQUIREMENT_STARTUP_PRODUCER_SCHEMA_VERSION
        ),
        concept_ids=scope["concept_ids"],
        text_relation_subject_ids=scope["text_relation_subject_ids"],
        text_relation_predicates=scope["text_relation_predicates"],
    )
    public_check = seed_freshness.public_startup_seed_freshness_result(freshness_check)
    read_strategy = (
        "dependency_snapshot_receipt"
        if freshness_check.get("verification_mode") == "dependency_snapshot"
        else "dependency_receipt"
    )
    common = {
        "skipped": True,
        "changed": False,
        "asset_path": bundle.get("asset_path"),
        "schema_version": bundle.get("schema_version"),
        "seed_version": bundle.get("seed_version"),
        "read_strategy": read_strategy,
        "read_phases": 1,
        "canonical_read_batches": {"concepts": 0, "text_assertions": 0},
        "duration_ms": int((time.perf_counter() - started_at) * 1000),
        "freshness_receipt": public_check,
        "errors": [],
    }
    if freshness_check.get("fresh"):
        metadata = freshness_check.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        return {
            "success": True,
            "ready": True,
            "state": "ready",
            "reason": "dependency_receipt_current",
            "reconciliation_required": False,
            "source_tag": metadata.get("source_tag") or bundle.get("source_tag"),
            "managed_by": metadata.get("managed_by") or bundle.get("managed_by"),
            "counts": dict(metadata.get("counts") or {}),
            **common,
        }
    return {
        "success": False,
        "ready": False,
        "state": "unavailable",
        "reason": "startup_seed_reconciliation_required",
        "reconciliation_required": True,
        **common,
    }


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in values:
        normalised = _text(item)
        if normalised and normalised not in output:
            output.append(normalised)
    return output


def _identifier_variants(concept_id: str) -> list[str]:
    variants = [concept_id]
    if concept_id.startswith("#V#"):
        variants.append(concept_id[3:])
    return list(dict.fromkeys(item for item in variants if item))


def _predicate_storage_keys(predicate_concept_id: str) -> list[str]:
    keys = [predicate_concept_id]
    if predicate_concept_id.startswith("#V#"):
        keys.append(predicate_concept_id[3:])
    return list(dict.fromkeys(key for key in keys if key))


def _human_label(concept_id: str) -> str:
    value = concept_id.replace("#V#", "").replace("_", " ")
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
    return value.strip().lower()


def _normalise_requirement(raw: Mapping[str, Any]) -> dict[str, Any]:
    requirement_id = _text(raw.get("requirement_id"))
    predicate_id = _text(raw.get("predicate_concept_id"))
    focal_argument = _text(raw.get("focal_argument"))
    other_type_id = _text(raw.get("other_argument_type_concept_id"))
    if not requirement_id:
        raise ValueError("requirement_id_missing")
    if not predicate_id:
        raise ValueError("predicate_concept_id_missing")
    if focal_argument not in {"subject", "object"}:
        raise ValueError("focal_argument_invalid")
    if not other_type_id:
        raise ValueError("other_argument_type_concept_id_missing")

    raw_minimum = raw.get("minimum_cardinality", 1)
    if isinstance(raw_minimum, bool):
        raise TypeError("minimum_cardinality_invalid")
    try:
        minimum_cardinality = int(raw_minimum)
    except (TypeError, ValueError) as exc:
        raise ValueError("minimum_cardinality_invalid") from exc
    if minimum_cardinality < 1:
        raise ValueError("minimum_cardinality_invalid")

    auto_formalisation_allowed = raw.get("auto_formalisation_allowed", False)
    if not isinstance(auto_formalisation_allowed, bool):
        raise TypeError("auto_formalisation_allowed_invalid")
    confirmation_required = raw.get(
        "confirmation_required_for_activation",
        bool(auto_formalisation_allowed and raw.get("activation_relevance")),
    )
    if not isinstance(confirmation_required, bool):
        raise TypeError("confirmation_required_for_activation_invalid")
    confirmation_creates_new_claim = raw.get("confirmation_creates_new_claim", False)
    if not isinstance(confirmation_creates_new_claim, bool):
        raise TypeError("confirmation_creates_new_claim_invalid")
    activation_minimum_status = (
        _text(raw.get("activation_minimum_status")) or "asserted"
    )
    activation_satisfying_statuses = _normalise_values(
        raw.get("activation_satisfying_statuses") or activation_minimum_status
    )

    return {
        "requirement_id": requirement_id,
        "predicate_concept_id": predicate_id,
        "predicate_label": _text(raw.get("predicate_label"))
        or _human_label(predicate_id),
        "focal_argument": focal_argument,
        "other_argument_type_concept_id": other_type_id,
        "other_argument_type_label": _text(raw.get("other_argument_type_label"))
        or _human_label(other_type_id),
        "minimum_cardinality": minimum_cardinality,
        "reason": _text(raw.get("reason")),
        "activation_relevance": _text(raw.get("activation_relevance")),
        "authority_relevance": _text(raw.get("authority_relevance")),
        "auto_formalisation_allowed": auto_formalisation_allowed,
        "auto_formalisation_target_status": _text(
            raw.get("auto_formalisation_target_status")
        )
        or ("tentative" if auto_formalisation_allowed else None),
        "activation_minimum_status": activation_minimum_status,
        "activation_satisfying_statuses": activation_satisfying_statuses,
        "confirmation_required_for_activation": confirmation_required,
        "confirmation_elicitation_priority": _text(
            raw.get("confirmation_elicitation_priority")
        ),
        "confirmation_effect": _text(raw.get("confirmation_effect")),
        "confirmation_creates_new_claim": confirmation_creates_new_claim,
        "question_template": _text(raw.get("question_template")),
        "confirmation_question_template": _text(
            raw.get("confirmation_question_template")
        ),
    }


def _normalise_profile(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raise TypeError("profile_not_object")
    if raw.get("schema_version") != CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION:
        raise ValueError("profile_schema_version_invalid")
    raw_requirements = raw.get("requirements")
    if not isinstance(raw_requirements, Sequence) or isinstance(
        raw_requirements, (str, bytes)
    ):
        raise TypeError("profile_requirements_invalid")

    requirements: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_requirement in raw_requirements:
        if not isinstance(raw_requirement, Mapping):
            raise TypeError("profile_requirement_not_object")
        requirement = _normalise_requirement(raw_requirement)
        requirement_id = requirement["requirement_id"]
        if requirement_id in seen_ids:
            raise ValueError("profile_requirement_id_duplicate")
        seen_ids.add(requirement_id)
        requirements.append(requirement)
    return requirements


class ConstitutiveRelationRequirementService:
    """Resolve represented requirements and report their missing witnesses."""

    def __init__(
        self,
        *,
        text_reader: Callable[..., Mapping[str, list[Mapping[str, Any]]]] | None = None,
        concept_finder: Callable[..., Any] | None = None,
        descendant_resolver: Callable[..., Any] | None = None,
    ) -> None:
        self._text_reader = text_reader or get_texts_for_concepts
        self._concept_finder = concept_finder or ConceptsRepository.find
        self._descendant_resolver = (
            descendant_resolver or get_vontology_node_and_descendant_ids
        )

    def load_requirements(
        self,
        *,
        type_depth_by_id: Mapping[str, int],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Load nearest-first inherited requirements for an instance's types."""

        ordered_type_ids = sorted(
            (
                type_id
                for type_id in type_depth_by_id
                if isinstance(type_id, str) and type_id
            ),
            key=lambda type_id: (int(type_depth_by_id[type_id]), type_id),
        )
        diagnostics: dict[str, Any] = {
            "represented_profile_type_ids": [],
            "builtin_profile_type_ids": [],
            "fallback_suppressed_type_ids": [],
            "malformed_profile_type_ids": [],
            "profile_read_failed": False,
            "compatibility_profile_load_failed": False,
            "compatibility_profile_load_error_type": None,
        }
        try:
            rows_by_type = self._text_reader(
                ordered_type_ids,
                predicates=list(CONSTITUTIVE_REQUIREMENTS_TEXT_PREDICATES),
                limit_per_concept=10,
                recent_first=True,
            )
            if not isinstance(rows_by_type, Mapping):
                rows_by_type = {}
        except Exception:  # noqa: BLE001 - represented policy failure is fail-soft
            rows_by_type = {}
            diagnostics["profile_read_failed"] = True

        requirements: list[dict[str, Any]] = []
        seen_requirement_ids: set[str] = set()
        compatibility_profiles: dict[str, dict[str, Any]] | None = None
        for type_id in ordered_type_ids:
            represented_requirements: list[dict[str, Any]] | None = None
            selected_predicate: str | None = None
            represented_profile_found = False
            profile_rows = rows_by_type.get(type_id) or []
            for predicate in CONSTITUTIVE_REQUIREMENTS_TEXT_PREDICATES:
                predicate_rows = [
                    row
                    for row in profile_rows
                    if isinstance(row, Mapping)
                    and _text(row.get("predicate")) == predicate
                ]
                represented_profile_found = (
                    bool(predicate_rows) or represented_profile_found
                )
                for row in predicate_rows:
                    try:
                        parsed = json.loads(str(row.get("text") or ""))
                        represented_requirements = _normalise_profile(parsed)
                        selected_predicate = predicate
                        break
                    except (TypeError, ValueError, json.JSONDecodeError):
                        if type_id not in diagnostics["malformed_profile_type_ids"]:
                            diagnostics["malformed_profile_type_ids"].append(type_id)
                if represented_requirements is not None:
                    break

            declaration_source = "represented"
            profile_requirements = represented_requirements
            if profile_requirements is None:
                if represented_profile_found or diagnostics["profile_read_failed"]:
                    # A malformed or unreadable live declaration is not evidence
                    # that the represented policy is absent.  Continue without
                    # the requirement rather than laundering code fallback as
                    # the active represented meaning.
                    diagnostics["fallback_suppressed_type_ids"].append(type_id)
                    continue
                if compatibility_profiles is None:
                    try:
                        compatibility_profiles = (
                            canonical_constitutive_requirement_profile_blueprints()
                        )
                    except Exception as exc:  # noqa: BLE001 - fail-soft fallback
                        compatibility_profiles = {}
                        diagnostics["compatibility_profile_load_failed"] = True
                        diagnostics["compatibility_profile_load_error_type"] = type(
                            exc
                        ).__name__
                builtin_profile = compatibility_profiles.get(type_id)
                if builtin_profile is None:
                    continue
                profile_requirements = _normalise_profile(builtin_profile)
                declaration_source = "builtin_compatibility"
                diagnostics["builtin_profile_type_ids"].append(type_id)
            else:
                diagnostics["represented_profile_type_ids"].append(type_id)

            depth = max(0, int(type_depth_by_id.get(type_id, 0)))
            for requirement in profile_requirements:
                requirement_id = str(requirement["requirement_id"])
                if requirement_id in seen_requirement_ids:
                    continue
                seen_requirement_ids.add(requirement_id)
                requirements.append(
                    {
                        **requirement,
                        "declared_on_type_concept_id": type_id,
                        "declaration_depth": depth,
                        "inherited": depth > 0,
                        "declaration_source": declaration_source,
                        "profile_predicate": selected_predicate,
                        "profile_schema_version": (
                            CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION
                        ),
                    }
                )

        return requirements, diagnostics

    def get_missing_requirements(
        self,
        *,
        instance: Mapping[str, Any],
        type_depth_by_id: Mapping[str, int],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return missing requirement plan entries without mutating the instance."""

        requirements, diagnostics = self.load_requirements(
            type_depth_by_id=type_depth_by_id
        )
        instance_id = _text(instance.get("concept_id")) or _text(instance.get("id"))
        if not instance_id:
            return [], diagnostics
        instance_name = _text(instance.get("name")) or "this concept"

        type_extent_cache: dict[str, set[str]] = {}
        missing: list[dict[str, Any]] = []
        for requirement in requirements:
            counterpart_ids = self._matching_counterpart_ids(
                instance=instance,
                instance_id=instance_id,
                requirement=requirement,
                type_extent_cache=type_extent_cache,
            )
            current_cardinality = len(counterpart_ids)
            minimum_cardinality = int(requirement["minimum_cardinality"])
            if current_cardinality >= minimum_cardinality:
                continue
            question = self._question_for_requirement(
                requirement=requirement,
                instance_name=instance_name,
            )
            auto_formalisation_allowed = bool(
                requirement.get("auto_formalisation_allowed")
            )
            missing.append(
                {
                    **requirement,
                    "requirement_kind": "constitutive_relation",
                    "priority_class": "constitutive",
                    "current_cardinality": current_cardinality,
                    "matching_counterpart_concept_ids": counterpart_ids,
                    "status": "missing",
                    "gap_status": "asserted_relation_missing",
                    "question": question,
                    "creation_blocking": False,
                    "tentative_counts_as_satisfied": False,
                    "formalisation_mode": (
                        "tentative_candidate"
                        if auto_formalisation_allowed
                        else "candidate_only"
                    ),
                }
            )
        return missing, diagnostics

    def _matching_counterpart_ids(
        self,
        *,
        instance: Mapping[str, Any],
        instance_id: str,
        requirement: Mapping[str, Any],
        type_extent_cache: dict[str, set[str]],
    ) -> list[str]:
        predicate_id = str(requirement["predicate_concept_id"])
        storage_keys = _predicate_storage_keys(predicate_id)
        other_type_id = str(requirement["other_argument_type_concept_id"])
        qualifying_types = type_extent_cache.get(other_type_id)
        if qualifying_types is None:
            try:
                raw_types = self._descendant_resolver(other_type_id) or []
            except Exception:  # noqa: BLE001 - fall back to exact type only
                raw_types = []
            qualifying_types = {
                type_id for type_id in raw_types if isinstance(type_id, str) and type_id
            }
            qualifying_types.add(other_type_id)
            type_extent_cache[other_type_id] = qualifying_types

        if requirement["focal_argument"] == "subject":
            relationships = instance.get("relationships")
            relationships = relationships if isinstance(relationships, Mapping) else {}
            target_ids: list[str] = []
            for storage_key in storage_keys:
                target_ids.extend(_normalise_values(relationships.get(storage_key)))
            target_ids = list(dict.fromkeys(target_ids))
            if not target_ids:
                return []
            query_ids: list[str] = []
            for target_id in target_ids:
                query_ids.extend(_identifier_variants(target_id))
            docs = self._find_docs(
                {"concept_id": {"$in": list(dict.fromkeys(query_ids))}}
            )
        else:
            focal_variants = _identifier_variants(instance_id)
            docs = self._find_docs(
                {
                    "$or": [
                        {f"relationships.{storage_key}": {"$in": focal_variants}}
                        for storage_key in storage_keys
                    ]
                }
            )

        counterpart_ids: list[str] = []
        for doc in docs:
            if not self._has_qualifying_type(doc, qualifying_types):
                continue
            counterpart_id = _text(doc.get("concept_id"))
            if counterpart_id and counterpart_id not in counterpart_ids:
                counterpart_ids.append(counterpart_id)
        return sorted(counterpart_ids)

    def _find_docs(self, query: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        projection = {
            "concept_id": 1,
            "relationships.is_an_instance_of": 1,
            "relationships.#V#is_an_instance_of": 1,
        }
        try:
            rows = self._concept_finder(dict(query), projection)
            return [row for row in rows if isinstance(row, Mapping)]
        except Exception:  # noqa: BLE001 - an unreadable witness remains missing
            return []

    @staticmethod
    def _has_qualifying_type(
        concept: Mapping[str, Any], qualifying_types: set[str]
    ) -> bool:
        relationships = concept.get("relationships")
        if not isinstance(relationships, Mapping):
            return False
        direct_types: list[str] = []
        for predicate in ("is_an_instance_of", "#V#is_an_instance_of"):
            direct_types.extend(_normalise_values(relationships.get(predicate)))
        return bool(set(direct_types).intersection(qualifying_types))

    @staticmethod
    def _question_for_requirement(
        *, requirement: Mapping[str, Any], instance_name: str
    ) -> str:
        template = _text(requirement.get("question_template"))
        format_values = {
            "instance_name": instance_name,
            "predicate_label": requirement.get("predicate_label") or "relation",
            "other_argument_type_label": requirement.get("other_argument_type_label")
            or "concept",
        }
        if template:
            try:
                return template.format(**format_values)
            except (KeyError, ValueError):
                pass
        if requirement.get("focal_argument") == "object":
            return (
                f"Which {format_values['other_argument_type_label']} has "
                f"{format_values['predicate_label']} {instance_name}?"
            )
        return f"What is {format_values['predicate_label']} for {instance_name}?"


__all__ = [
    "CONSTITUTIVE_REQUIREMENTS_PREDICATE",
    "CONSTITUTIVE_REQUIREMENTS_SCHEMA_VERSION",
    "CONSTITUTIVE_REQUIREMENTS_TEXT_PREDICATES",
    "CONSTITUTIVE_REQUIREMENT_SEED_SCHEMA_VERSION",
    "CONSTITUTIVE_REQUIREMENT_STARTUP_FAMILY_ID",
    "CONSTITUTIVE_REQUIREMENT_STARTUP_PRODUCER_SCHEMA_VERSION",
    "MEMBER_OF_PREDICATE_ID",
    "VON_USER_ORGANISATION_TYPE_ID",
    "VON_USER_TYPE_ID",
    "ConstitutiveRelationRequirementService",
    "canonical_constitutive_requirement_profile_blueprints",
    "ensure_canonical_constitutive_relation_requirement_profiles",
    "ensure_constitutive_relation_requirement_profiles_current_for_startup",
    "reconcile_canonical_constitutive_relation_requirement_profiles",
]
