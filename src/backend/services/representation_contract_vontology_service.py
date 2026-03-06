"""Vontology-backed helpers for representation required-effects contracts.

This module keeps ontology profile fetch/normalisation separate from the
deterministic turn-execution contract builder.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .text_value_service import get_texts_for_concept
from .text_value_service import upsert_singleton_text_relation

DEFAULT_REPRESENTATION_PROFILE_PREDICATE = "#V#has_representation_contract_profile_json"
REPRESENTATION_CONTRACT_PROFILE_TYPE_ID = "#V#representation_contract_profile"

_DEFAULT_PROFILE_TEXT_PREDICATES: tuple[str, ...] = (
    "#V#has_representation_contract_profile_json",
    "has_representation_contract_profile_json",
    "hasRepresentationContractProfileJson",
    "hasContent",
)

_DEFAULT_DECISION_POLICY: dict[str, bool] = {
    "completion_block_on_unresolved_effects": True,
    "fail_closed_on_missing_requirements": True,
    "auto_apply_low_risk_defaults": True,
    "requires_explicit_user_decision_for_high_risk": True,
}

# JVNAUTOSCI-1377: canonical starter profiles are kept here only for bootstrap.
# Runtime behaviour should read profile semantics from Vontology text relations.
_CANONICAL_REPRESENTATION_PROFILE_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "profile_concept_id": "#V#representation_contract_profile_paper",
        "profile_id": "paper",
        "target_entity_class": "scholarly_paper",
        "effect_type": "scholarly_representation",
        "description": (
            "Ensure the corresponding scholarly-paper concept is materialised from "
            "the supplied artefact context before final response completion."
        ),
        "intent_patterns": [
            r"\b("
            r"paper\s+representation"
            r"|represent(?:ation|ing)?\s+(?:the\s+)?(?:corresponding\s+)?paper"
            r"|fully\s+represent\s+(?:the\s+)?(?:corresponding\s+)?paper"
            r"|scholarly\s+paper\s+representation"
            r")\b"
        ],
        "domain_terms": [
            "paper",
            "scientific paper",
            "arxiv",
            "preprint",
            "doi",
            "manuscript",
            "metadata",
            "abstract",
        ],
        "required_tools_by_source": {
            "file_copy": ["interpret_file_copy"],
            "url": ["download_paper"],
            "mixed": ["download_paper", "interpret_file_copy"],
            "unknown": ["interpret_file_copy"],
        },
        "required_predicates": [
            "#V#computer_file_for_propositional_information_thing",
            "#V#propositional_information_thing_has_computer_file",
        ],
        "default_decision_policy": dict(_DEFAULT_DECISION_POLICY),
    },
    {
        "profile_concept_id": "#V#representation_contract_profile_person",
        "profile_id": "person",
        "target_entity_class": "person",
        "effect_type": "representation_person",
        "description": (
            "Ensure a person representation is materialised from the supplied "
            "artefact context before final response completion."
        ),
        "intent_patterns": [
            r"\b("
            r"(?:business\s+card|cv|curriculum\s+vitae|resume)\b.*\b(?:person|profile|contact)"
            r"|(?:person|profile|contact)\b.*\b(?:business\s+card|cv|curriculum\s+vitae|resume)"
            r")\b"
        ],
        "domain_terms": [
            "person",
            "business card",
            "cv",
            "curriculum vitae",
            "resume",
            "contact",
            "profile",
            "researcher",
        ],
        "required_tools_by_source": {
            "file_copy": ["interpret_file_copy"],
            "url": ["extract_url"],
            "mixed": ["interpret_file_copy", "extract_url"],
            "unknown": ["interpret_file_copy"],
        },
        "required_predicates": ["#V#person"],
        "default_decision_policy": dict(_DEFAULT_DECISION_POLICY),
    },
    {
        "profile_concept_id": "#V#representation_contract_profile_company",
        "profile_id": "company",
        "target_entity_class": "company",
        "effect_type": "representation_company",
        "description": (
            "Ensure a company representation is materialised from the supplied "
            "artefact context before final response completion."
        ),
        "intent_patterns": [
            r"\b("
            r"(?:company|organisation|organization|business|startup)\b.*\b(?:web\s?page|website|url)"
            r"|(?:web\s?page|website|url)\b.*\b(?:company|organisation|organization|business|startup)"
            r")\b"
        ],
        "domain_terms": [
            "company",
            "organisation",
            "organization",
            "business",
            "startup",
            "firm",
            "web page",
            "website",
            "url",
        ],
        "required_tools_by_source": {
            "file_copy": ["interpret_file_copy"],
            "url": ["extract_url"],
            "mixed": ["interpret_file_copy", "extract_url"],
            "unknown": ["extract_url"],
        },
        "required_predicates": ["#V#organisation"],
        "default_decision_policy": dict(_DEFAULT_DECISION_POLICY),
    },
    {
        "profile_concept_id": "#V#representation_contract_profile_meeting",
        "profile_id": "meeting",
        "target_entity_class": "meeting",
        "effect_type": "representation_meeting",
        "description": (
            "Ensure a meeting representation is materialised from the supplied "
            "artefact context before final response completion."
        ),
        "intent_patterns": [
            r"\b("
            r"(?:meeting|calendar\s+event)\b.*\b(?:transcript|calendar|minutes|agenda)"
            r"|(?:transcript|calendar|minutes|agenda)\b.*\b(?:meeting|calendar\s+event)"
            r")\b"
        ],
        "domain_terms": [
            "meeting",
            "calendar event",
            "transcript",
            "calendar",
            "minutes",
            "agenda",
            "attendees",
        ],
        "required_tools_by_source": {
            "file_copy": ["interpret_file_copy"],
            "url": ["extract_url"],
            "mixed": ["interpret_file_copy", "extract_url"],
            "unknown": ["interpret_file_copy"],
        },
        "required_predicates": ["#V#meeting"],
        "default_decision_policy": dict(_DEFAULT_DECISION_POLICY),
    },
)

_CANONICAL_PROFILE_BY_CONCEPT_ID: dict[str, dict[str, Any]] = {
    str(item["profile_concept_id"]): dict(item)
    for item in _CANONICAL_REPRESENTATION_PROFILE_BLUEPRINTS
}


def _profile_display_name(profile: Mapping[str, Any]) -> str:
    profile_id = _safe_str(profile.get("profile_id")) or "representation"
    pretty = profile_id.replace("_", " ").strip()
    return f"{pretty[:1].upper() + pretty[1:] if pretty else 'Representation'} representation contract profile"


def canonical_representation_profile_concept_ids() -> tuple[str, ...]:
    """Return canonical representation profile concept IDs in deterministic order."""
    return tuple(
        str(item["profile_concept_id"])
        for item in _CANONICAL_REPRESENTATION_PROFILE_BLUEPRINTS
    )


def canonical_representation_profile_blueprints() -> tuple[dict[str, Any], ...]:
    """Return copy-on-read canonical representation starter profiles."""
    return tuple(dict(item) for item in _CANONICAL_REPRESENTATION_PROFILE_BLUEPRINTS)


def ensure_canonical_representation_contract_profiles(
    *,
    concept_ids: Sequence[str] | None = None,
    predicate: str = DEFAULT_REPRESENTATION_PROFILE_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    garbage_collect: bool = True,
    create_missing_concepts: bool = True,
) -> dict[str, Any]:
    """Ensure canonical representation-profile concepts exist and carry profile JSON."""

    requested_concept_ids = (
        _normalise_strings(concept_ids) or canonical_representation_profile_concept_ids()
    )
    type_created = False
    created_profile_concept_ids: list[str] = []
    persisted_profile_concept_ids: list[str] = []
    unknown_canonical_profile_concept_ids: list[str] = []
    missing_concept_ids: list[str] = []
    errors_by_concept_id: dict[str, str] = {}

    profile_type = _safe_get_concept(REPRESENTATION_CONTRACT_PROFILE_TYPE_ID)
    if not isinstance(profile_type, Mapping) and create_missing_concepts:
        try:
            concept_service.create_concept(
                name="Representation contract profile",
                concept_id=REPRESENTATION_CONTRACT_PROFILE_TYPE_ID,
                description=(
                    "Type for canonical workflow contract profiles that declare "
                    "required-effects semantics for representation intents."
                ),
                parent_concept_ids=["#V#thing"],
                create_as_instance=False,
            )
            type_created = True
        except Exception as exc:
            errors_by_concept_id[REPRESENTATION_CONTRACT_PROFILE_TYPE_ID] = (
                f"type_create_failed:{exc}"
            )

    for concept_id in requested_concept_ids:
        seed_profile = _CANONICAL_PROFILE_BY_CONCEPT_ID.get(concept_id)
        if not isinstance(seed_profile, Mapping):
            unknown_canonical_profile_concept_ids.append(concept_id)
            continue

        concept = _safe_get_concept(concept_id)
        if not isinstance(concept, Mapping):
            if not create_missing_concepts:
                missing_concept_ids.append(concept_id)
                continue
            try:
                concept_service.create_concept(
                    name=_profile_display_name(seed_profile),
                    concept_id=concept_id,
                    description=_safe_str(seed_profile.get("description")),
                    parent_concept_ids=[REPRESENTATION_CONTRACT_PROFILE_TYPE_ID],
                    create_as_instance=True,
                )
                created_profile_concept_ids.append(concept_id)
                concept = _safe_get_concept(concept_id)
            except Exception as exc:
                errors_by_concept_id[concept_id] = f"create_failed:{exc}"
                continue

        if not isinstance(concept, Mapping):
            missing_concept_ids.append(concept_id)
            continue

        try:
            result = upsert_representation_contract_profile(
                profile_concept_id=concept_id,
                representation_profile=dict(seed_profile),
                predicate=predicate,
                language=language,
                policy=policy,
                provenance=provenance,
                context=context,
                garbage_collect=garbage_collect,
            )
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"profile_upsert_failed:{exc}"
            continue

        if bool(result.get("success")):
            persisted_profile_concept_ids.append(concept_id)
        else:
            errors_by_concept_id[concept_id] = "profile_upsert_unsuccessful"

    return {
        "success": not (
            unknown_canonical_profile_concept_ids
            or missing_concept_ids
            or errors_by_concept_id
        ),
        "requested_profile_concept_ids": list(requested_concept_ids),
        "canonical_profile_concept_ids": list(canonical_representation_profile_concept_ids()),
        "profile_type_id": REPRESENTATION_CONTRACT_PROFILE_TYPE_ID,
        "profile_type_created": type_created,
        "created_profile_concept_ids": created_profile_concept_ids,
        "persisted_profile_concept_ids": persisted_profile_concept_ids,
        "unknown_canonical_profile_concept_ids": unknown_canonical_profile_concept_ids,
        "missing_concept_ids": missing_concept_ids,
        "errors_by_concept_id": errors_by_concept_id,
        "counts": {
            "requested": len(requested_concept_ids),
            "created_concepts": len(created_profile_concept_ids),
            "persisted_profiles": len(persisted_profile_concept_ids),
            "missing_concepts": len(missing_concept_ids),
            "unknown_canonical_ids": len(unknown_canonical_profile_concept_ids),
            "errors": len(errors_by_concept_id),
        },
    }


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalise_strings(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    seen: set[str] = set()
    normalised: list[str] = []
    for value in values:
        text = _safe_str(value)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised.append(text)
    return tuple(normalised)


def _normalise_required_tools_by_source(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, Mapping):
        return {}
    by_source: dict[str, list[str]] = {}
    for key, value in raw.items():
        source = (_safe_str(key) or "").lower()
        if not source:
            continue
        tools = list(_normalise_strings(value))
        if not tools:
            continue
        by_source[source] = tools
    return by_source


def _normalise_decision_policy(raw: Any) -> dict[str, bool]:
    policy = dict(_DEFAULT_DECISION_POLICY)
    if not isinstance(raw, Mapping):
        return policy
    for key in _DEFAULT_DECISION_POLICY:
        if key in raw:
            policy[key] = bool(raw.get(key))
    return policy


def _text_preview(value: Any, *, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _format_parse_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if len(message) > 160:
        message = f"{message[:157]}..."
    return f"{exc.__class__.__name__}: {message}"


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    """Return ``None`` for absent concepts so bootstrap can create them cleanly."""

    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def _parse_profile_json_with_diagnostics(
    raw_text: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse JSON representation profile payloads and return parse diagnostics."""
    if not isinstance(raw_text, str):
        return [], ["profile_text_not_string"]
    text = raw_text.strip()
    if not text:
        return [], ["profile_text_empty"]

    def _coerce_parsed(parsed: Any) -> list[dict[str, Any]]:
        if isinstance(parsed, Mapping):
            return [dict(parsed)]
        if isinstance(parsed, list):
            return [dict(item) for item in parsed if isinstance(item, Mapping)]
        return []

    parse_errors: list[str] = []
    try:
        parsed = json.loads(text)
        coerced = _coerce_parsed(parsed)
        if coerced:
            return coerced, []
        parse_errors.append("json_payload_not_mapping_or_list")
    except Exception as exc:
        parse_errors.append(f"json_decode_failed:{_format_parse_error(exc)}")

    fenced_block_seen = False
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE):
        fenced_block_seen = True
        block = match.group(1).strip()
        if not block:
            continue
        try:
            parsed = json.loads(block)
        except Exception as exc:
            parse_errors.append(f"fenced_json_decode_failed:{_format_parse_error(exc)}")
            continue
        coerced = _coerce_parsed(parsed)
        if coerced:
            return coerced, []
        parse_errors.append("fenced_json_payload_not_mapping_or_list")
    if fenced_block_seen and not parse_errors:
        parse_errors.append("fenced_json_block_empty")
    return [], parse_errors


def _derive_profile_id(profile_concept_id: str | None) -> str | None:
    concept_id = _safe_str(profile_concept_id)
    if not concept_id:
        return None
    if concept_id.startswith("#V#"):
        concept_id = concept_id[3:]
    concept_id = concept_id.strip().lower()
    if concept_id.startswith("representation_contract_profile_"):
        concept_id = concept_id[len("representation_contract_profile_") :]
    return concept_id or None


def _serialise_representation_profile(
    profile: Mapping[str, Any],
    *,
    profile_concept_id: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(profile, Mapping):
        return None, ["profile_not_mapping"]

    errors: list[str] = []
    resolved_profile_concept_id = _safe_str(profile.get("profile_concept_id")) or _safe_str(
        profile_concept_id
    )
    resolved_profile_id = _safe_str(profile.get("profile_id")) or _derive_profile_id(
        resolved_profile_concept_id
    )
    target_entity_class = _safe_str(profile.get("target_entity_class"))
    effect_type = _safe_str(profile.get("effect_type"))
    description = _safe_str(profile.get("description")) or (
        "Ensure the requested representation is materialised from the artefact context."
    )

    intent_patterns = list(_normalise_strings(profile.get("intent_patterns")))
    for pattern in intent_patterns:
        try:
            re.compile(pattern, flags=re.IGNORECASE)
        except Exception as exc:
            errors.append(f"invalid_intent_pattern:{pattern}:{_format_parse_error(exc)}")

    domain_terms = list(_normalise_strings(profile.get("domain_terms")))
    if not intent_patterns and not domain_terms:
        errors.append("missing_intent_patterns_and_domain_terms")

    required_tools_by_source = _normalise_required_tools_by_source(
        profile.get("required_tools_by_source")
    )
    required_predicates = list(_normalise_strings(profile.get("required_predicates")))
    default_decision_policy = _normalise_decision_policy(
        profile.get("default_decision_policy")
    )

    if not resolved_profile_id:
        errors.append("missing_profile_id")
    if not target_entity_class:
        errors.append("missing_target_entity_class")
    if not effect_type:
        errors.append("missing_effect_type")

    if errors:
        return None, errors

    serialised: dict[str, Any] = {
        "profile_id": resolved_profile_id,
        "target_entity_class": target_entity_class,
        "effect_type": effect_type,
        "description": description,
        "intent_patterns": intent_patterns,
        "domain_terms": domain_terms,
        "required_tools_by_source": required_tools_by_source,
        "required_predicates": required_predicates,
        "default_decision_policy": default_decision_policy,
    }
    if resolved_profile_concept_id:
        serialised["profile_concept_id"] = resolved_profile_concept_id
    return serialised, []


def _hash_profile_catalogue(profiles: Sequence[Mapping[str, Any]]) -> str | None:
    if not profiles:
        return None
    try:
        payload = json.dumps(list(profiles), sort_keys=True, separators=(",", ":"))
    except Exception:
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_representation_contract_profiles_from_concept_ids(
    concept_ids: Sequence[str],
    *,
    profile_text_predicates: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load representation contract profiles from Vontology concept text relations."""
    predicates = (
        _normalise_strings(profile_text_predicates) or _DEFAULT_PROFILE_TEXT_PREDICATES
    )
    ordered_concept_ids = _normalise_strings(concept_ids)

    loaded_profiles: list[dict[str, Any]] = []
    loaded_concept_ids: list[str] = []
    unresolved_concept_ids: list[str] = []
    missing_profile_concept_ids: list[str] = []
    malformed_profile_concept_ids: list[str] = []
    malformed_profile_entries: list[dict[str, Any]] = []

    for concept_id in ordered_concept_ids:
        try:
            concept = _safe_get_concept(concept_id)
        except Exception:
            concept = None

        if not isinstance(concept, Mapping):
            unresolved_concept_ids.append(concept_id)
            continue

        concept_profiles: list[dict[str, Any]] = []
        concept_malformed_entries: list[dict[str, Any]] = []
        profile_rows_found = False

        for predicate in predicates:
            try:
                text_rows = get_texts_for_concept(concept_id, predicate=predicate, limit=20)
            except Exception:
                text_rows = []
            if not text_rows:
                continue
            profile_rows_found = True

            for row in text_rows:
                parsed_profiles, parse_errors = _parse_profile_json_with_diagnostics(
                    row.get("text")
                )
                if parse_errors:
                    concept_malformed_entries.append(
                        {
                            "concept_id": concept_id,
                            "predicate": predicate,
                            "error": "; ".join(parse_errors),
                            "text_preview": _text_preview(row.get("text")),
                        }
                    )
                    continue

                for raw_profile in parsed_profiles:
                    serialised, validation_errors = _serialise_representation_profile(
                        raw_profile,
                        profile_concept_id=concept_id,
                    )
                    if validation_errors or not isinstance(serialised, Mapping):
                        concept_malformed_entries.append(
                            {
                                "concept_id": concept_id,
                                "predicate": predicate,
                                "error": "; ".join(validation_errors or ["invalid_profile"]),
                                "profile_preview": {
                                    key: raw_profile.get(key)
                                    for key in sorted(
                                        set(raw_profile.keys())
                                        & {
                                            "profile_id",
                                            "target_entity_class",
                                            "effect_type",
                                            "required_predicates",
                                            "required_tools_by_source",
                                        }
                                    )
                                },
                            }
                        )
                        continue
                    normalised = dict(serialised)
                    normalised["_source_concept_id"] = concept_id
                    normalised["_source_predicate"] = predicate
                    concept_profiles.append(normalised)
            if concept_profiles:
                break

        if concept_profiles:
            loaded_profiles.extend(concept_profiles)
            loaded_concept_ids.append(concept_id)
        elif concept_malformed_entries:
            malformed_profile_concept_ids.append(concept_id)
            malformed_profile_entries.extend(concept_malformed_entries)
        elif profile_rows_found:
            malformed_profile_concept_ids.append(concept_id)
        else:
            missing_profile_concept_ids.append(concept_id)

    deduped_profiles: list[dict[str, Any]] = []
    seen_profile_ids: set[str] = set()
    duplicate_profile_ids: list[str] = []
    for profile in loaded_profiles:
        profile_id = (_safe_str(profile.get("profile_id")) or "").lower()
        if not profile_id:
            continue
        if profile_id in seen_profile_ids:
            duplicate_profile_ids.append(profile_id)
            continue
        seen_profile_ids.add(profile_id)
        deduped_profiles.append(profile)

    profile_version_hash = _hash_profile_catalogue(deduped_profiles)
    diagnostics = {
        "representation_profile_source": "vontology_concept_text_relations",
        "requested_concept_ids": list(ordered_concept_ids),
        "profile_text_predicates_checked": list(predicates),
        "loaded_concept_ids": loaded_concept_ids,
        "unresolved_concept_ids": unresolved_concept_ids,
        "missing_profile_concept_ids": missing_profile_concept_ids,
        "malformed_profile_concept_ids": malformed_profile_concept_ids,
        "malformed_profile_count": len(malformed_profile_entries),
        "malformed_profile_entries": malformed_profile_entries[:25],
        "loaded_profile_count": len(deduped_profiles),
        "duplicate_profile_ids": sorted(set(duplicate_profile_ids)),
        "profile_version_hash": profile_version_hash,
    }
    return deduped_profiles, diagnostics


def upsert_representation_contract_profile(
    *,
    profile_concept_id: str,
    representation_profile: Mapping[str, Any],
    predicate: str = DEFAULT_REPRESENTATION_PROFILE_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    garbage_collect: bool = True,
) -> dict[str, Any]:
    """Validate and persist one representation profile as a singleton text relation."""
    concept_id = _safe_str(profile_concept_id)
    if not concept_id:
        raise ValueError("profile_concept_id is required")

    concept = _safe_get_concept(concept_id)
    if not isinstance(concept, Mapping):
        raise ValueError(f"Profile concept not found: {concept_id}")

    if not isinstance(representation_profile, Mapping):
        raise ValueError("representation_profile must be a mapping")

    serialised_profile, validation_errors = _serialise_representation_profile(
        dict(representation_profile),
        profile_concept_id=concept_id,
    )
    if validation_errors or not isinstance(serialised_profile, Mapping):
        raise ValueError(
            "representation_profile validation failed: "
            + "; ".join(validation_errors or ["invalid_profile"])
        )

    json_text = json.dumps(serialised_profile, ensure_ascii=True, sort_keys=True)
    write_result = upsert_singleton_text_relation(
        subject_concept_id=concept_id,
        predicate=str(predicate or DEFAULT_REPRESENTATION_PROFILE_PREDICATE),
        text=json_text,
        lang=str(language or "en-NZ"),
        policy=str(policy or "replace_others"),
        provenance=dict(provenance or {}),
        context=dict(context or {}),
        garbage_collect=bool(garbage_collect),
    )

    return {
        "success": bool(write_result.get("success")),
        "profile_concept_id": concept_id,
        "predicate": write_result.get("predicate"),
        "language": write_result.get("language"),
        "representation_profile": dict(serialised_profile),
        "text_relation": write_result,
    }


def bootstrap_canonical_representation_contract_profiles(
    *,
    concept_ids: Sequence[str] | None = None,
    predicate: str = DEFAULT_REPRESENTATION_PROFILE_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    provenance: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    garbage_collect: bool = True,
) -> dict[str, Any]:
    """Persist canonical representation profiles onto existing profile concepts."""
    requested_concept_ids = _normalise_strings(concept_ids) or canonical_representation_profile_concept_ids()

    persisted_profile_concept_ids: list[str] = []
    unknown_canonical_profile_concept_ids: list[str] = []
    missing_concept_ids: list[str] = []
    errors_by_concept_id: dict[str, str] = {}

    for concept_id in requested_concept_ids:
        seed_profile = _CANONICAL_PROFILE_BY_CONCEPT_ID.get(concept_id)
        if not isinstance(seed_profile, Mapping):
            unknown_canonical_profile_concept_ids.append(concept_id)
            continue
        try:
            concept = _safe_get_concept(concept_id)
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"concept_lookup_failed:{exc}"
            continue

        if not isinstance(concept, Mapping):
            missing_concept_ids.append(concept_id)
            continue

        try:
            result = upsert_representation_contract_profile(
                profile_concept_id=concept_id,
                representation_profile=dict(seed_profile),
                predicate=predicate,
                language=language,
                policy=policy,
                provenance=provenance,
                context=context,
                garbage_collect=garbage_collect,
            )
        except Exception as exc:
            errors_by_concept_id[concept_id] = f"profile_upsert_failed:{exc}"
            continue

        if bool(result.get("success")):
            persisted_profile_concept_ids.append(concept_id)
        else:
            errors_by_concept_id[concept_id] = "profile_upsert_unsuccessful"

    return {
        "success": not (
            unknown_canonical_profile_concept_ids
            or missing_concept_ids
            or errors_by_concept_id
        ),
        "requested_profile_concept_ids": list(requested_concept_ids),
        "canonical_profile_concept_ids": list(canonical_representation_profile_concept_ids()),
        "persisted_profile_concept_ids": persisted_profile_concept_ids,
        "unknown_canonical_profile_concept_ids": unknown_canonical_profile_concept_ids,
        "missing_concept_ids": missing_concept_ids,
        "errors_by_concept_id": errors_by_concept_id,
        "counts": {
            "requested": len(requested_concept_ids),
            "persisted": len(persisted_profile_concept_ids),
            "missing_concepts": len(missing_concept_ids),
            "unknown_canonical_ids": len(unknown_canonical_profile_concept_ids),
            "errors": len(errors_by_concept_id),
        },
    }
