"""Model registry access for workflow-driven model routing and runtime policies."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional, Sequence

from .settings_service import resolve_enabled_llm_settings, resolve_llm_setting

logger = logging.getLogger(__name__)

_DEFAULT_REGISTRY_NAME = "default_model_registry"
_LEGACY_REGISTRY_JSON_PREDICATE_NAME = "has_model_registry_json"

PRED_HAS_MODEL_ENTRY = "#V#has_model_entry"
PRED_REFERS_TO_MODEL = "#V#refers_to_model"
PRED_HAS_PROVIDER = "#V#has_provider"
PRED_HAS_MODEL_API_PROFILE = "#V#has_model_api_profile"
PRED_HAS_MODEL_PARAMETER_CONSTRAINT = "#V#has_model_parameter_constraint"
PRED_CONSTRAINS_MODEL_PARAMETER = "#V#constrains_model_parameter"
PRED_HAS_MODEL_ID = "#V#has_model_id"
PRED_HAS_API_SURFACE = "#V#has_api_surface"
PRED_HAS_PARAMETER_ACTION = "#V#has_parameter_action"
PRED_HAS_FIXED_PARAMETER_VALUE = "#V#has_fixed_parameter_value"
PRED_HAS_ALLOWED_PARAMETER_VALUE = "#V#has_allowed_parameter_value"

MODEL_STAGE_SUITABILITY_EVIDENCE_SCHEMA_VERSION = "model_stage_suitability_evidence.v1"
MODEL_STAGE_CERTIFICATION_DECISION_SCHEMA_VERSION = (
    "model_stage_certification_decision.v1"
)
DEFAULT_MINIMUM_REPLAY_CASES_FOR_CERTIFICATION = 2

KNOWN_PROVIDER_PREFIXES = frozenset(
    {"openai", "anthropic", "gemini", "ollama", "deepseek"}
)
PARAMETER_ACTION_OMIT = "omit"
PARAMETER_ACTION_FIXED_VALUE = "fixed_value"


def _parse_registry_json(raw_text: str) -> Optional[Mapping[str, Any]]:
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None
    try:
        parsed = json.loads(raw_text)
    except Exception:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _safe_evidence_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _normalise_lookup_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace(": ", ":")


def _normalise_provider_name(value: Any) -> str:
    token = _normalise_lookup_token(value)
    if not token:
        return ""
    if token.startswith("#v#"):
        token = token[3:]
    token = token.replace("-", "_").replace(" ", "_")
    if token.endswith("_provider"):
        token = token[: -len("_provider")]
    for provider_name in KNOWN_PROVIDER_PREFIXES:
        if provider_name in token:
            return provider_name
    return token


def _normalise_parameter_name(value: Any) -> str:
    token = _normalise_lookup_token(value)
    if not token:
        return ""
    if token.startswith("#v#"):
        token = token[3:]
    token = token.replace("-", "_").replace(" ", "_")
    if token.endswith("_parameter"):
        token = token[: -len("_parameter")]
    return token


def _normalise_parameter_action(value: Any) -> str:
    return _normalise_lookup_token(value).replace("-", "_").replace(" ", "_")


def _resolve_concept_id_by_name(
    name: str, *, preferred_language: str | None = None
) -> str | None:
    try:
        from .concept_resolution_service import resolve_concept_by_name
    except Exception:
        return None

    resolution = resolve_concept_by_name(
        name=name,
        preferred_languages=[preferred_language] if preferred_language else None,
        match_code_strings=True,
    )
    if not isinstance(resolution, Mapping) or resolution.get("status") != "resolved":
        return None
    concept_id = resolution.get("resolved_concept_id")
    return concept_id if isinstance(concept_id, str) and concept_id.strip() else None


def _get_text_rows(
    concept_id: str,
    *,
    predicate: str | None = None,
    limit: int = 50,
) -> list[Mapping[str, Any]]:
    try:
        from .text_value_service import get_texts_for_concept
    except Exception:
        return []

    try:
        rows = get_texts_for_concept(concept_id, predicate=predicate, limit=limit)
    except Exception:
        return []

    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _get_first_text(
    concept_id: str,
    *,
    predicate: str,
    limit: int = 10,
) -> str | None:
    for row in _get_text_rows(concept_id, predicate=predicate, limit=limit):
        raw_text = row.get("text")
        if isinstance(raw_text, str) and raw_text.strip():
            return raw_text.strip()
    return None


def _get_related_concept_ids(concept_id: str, predicate: str) -> list[str]:
    try:
        from ..db.repositories.concepts_repository import ConceptsRepository
    except Exception:
        return []

    concept = ConceptsRepository.find_one({"concept_id": concept_id})
    if not isinstance(concept, Mapping):
        return []

    relationships = concept.get("relationships")
    if isinstance(relationships, Mapping):
        direct_targets = relationships.get(predicate)
        if isinstance(direct_targets, str) and direct_targets.strip():
            return [direct_targets.strip()]
        if isinstance(direct_targets, Sequence):
            targets = [
                str(item).strip()
                for item in direct_targets
                if isinstance(item, str) and str(item).strip()
            ]
            if targets:
                return targets

        linked_to = relationships.get("linked_to")
        if isinstance(linked_to, Sequence):
            linked_targets = []
            for item in linked_to:
                if not isinstance(item, Mapping):
                    continue
                if item.get("predicate") != predicate:
                    continue
                target = item.get("target") or item.get("concept_id")
                if isinstance(target, str) and target.strip():
                    linked_targets.append(target.strip())
            if linked_targets:
                return linked_targets

    return [
        str(row.get("text")).strip()
        for row in _get_text_rows(concept_id, predicate=predicate, limit=100)
        if isinstance(row.get("text"), str) and str(row.get("text")).strip().startswith("#V#")
    ]


def _strip_provider_prefix(model_id: str, provider: str | None = None) -> str:
    cleaned = _normalise_lookup_token(model_id)
    if not cleaned:
        return ""

    provider_prefixes = (
        [provider] if isinstance(provider, str) and provider.strip() else KNOWN_PROVIDER_PREFIXES
    )
    for provider_name in provider_prefixes:
        provider_token = _normalise_provider_name(provider_name)
        if not provider_token:
            continue
        prefix = f"{provider_token}:"
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :].strip()
    return cleaned


def _looks_like_machine_model_name(value: str) -> bool:
    cleaned = value.strip().replace(": ", ":")
    if not cleaned or cleaned.startswith("#V#"):
        return False
    if " " in cleaned:
        return False
    return True


def _iter_machine_model_aliases(
    *, concept_id: str | None, provider: str | None = None
) -> list[str]:
    if not isinstance(concept_id, str) or not concept_id.strip():
        return []

    aliases: list[str] = []
    seen: set[str] = set()
    for row in _get_text_rows(concept_id, predicate="hasName", limit=50):
        raw_text = row.get("text")
        if not isinstance(raw_text, str):
            continue
        candidate = raw_text.strip().replace(": ", ":")
        if not _looks_like_machine_model_name(candidate):
            continue
        for alias in (candidate, _strip_provider_prefix(candidate, provider)):
            alias_key = _normalise_lookup_token(alias)
            if not alias_key or alias_key in seen:
                continue
            seen.add(alias_key)
            aliases.append(alias.strip())
    return aliases


def _derive_model_id_from_aliases(
    *, aliases: Sequence[str], provider: str | None = None
) -> str | None:
    for alias in aliases:
        stripped = _strip_provider_prefix(alias, provider)
        if stripped:
            return stripped
    return None


def _resolve_provider_for_model(
    model_concept_id: str,
    *,
    preferred_language: str | None = None,
) -> str | None:
    provider_ids = _get_related_concept_ids(model_concept_id, PRED_HAS_PROVIDER)
    if not provider_ids:
        return None

    provider_id = provider_ids[0]
    for row in _get_text_rows(provider_id, predicate="hasName", limit=20):
        raw_text = row.get("text")
        if not isinstance(raw_text, str):
            continue
        provider_name = _normalise_provider_name(raw_text)
        if provider_name:
            return provider_name
    return _normalise_provider_name(provider_id)


def _resolve_parameter_constraint_from_graph(
    *, constraint_concept_id: str
) -> Mapping[str, Any]:
    parameter_concept_id = None
    parameter_ids = _get_related_concept_ids(
        constraint_concept_id, PRED_CONSTRAINS_MODEL_PARAMETER
    )
    if parameter_ids:
        parameter_concept_id = parameter_ids[0]

    return {
        "constraint_concept_id": constraint_concept_id,
        "parameter_concept_id": parameter_concept_id,
        "parameter": _normalise_parameter_name(parameter_concept_id),
        "action": _get_first_text(
            constraint_concept_id, predicate=PRED_HAS_PARAMETER_ACTION
        ),
        "fixed_value": _get_first_text(
            constraint_concept_id, predicate=PRED_HAS_FIXED_PARAMETER_VALUE
        ),
        "allowed_values": [
            str(row.get("text")).strip()
            for row in _get_text_rows(
                constraint_concept_id,
                predicate=PRED_HAS_ALLOWED_PARAMETER_VALUE,
                limit=50,
            )
            if isinstance(row.get("text"), str) and str(row.get("text")).strip()
        ],
    }


def _resolve_model_entry_from_graph(
    *, registry_entry_id: str, preferred_language: str | None = None
) -> Mapping[str, Any] | None:
    model_ids = _get_related_concept_ids(registry_entry_id, PRED_REFERS_TO_MODEL)
    model_concept_id = model_ids[0] if model_ids else None
    if not isinstance(model_concept_id, str) or not model_concept_id.strip():
        return None

    provider = _resolve_provider_for_model(
        model_concept_id, preferred_language=preferred_language
    )
    model_aliases = _iter_machine_model_aliases(
        concept_id=model_concept_id,
        provider=provider,
    )
    model_id = _get_first_text(registry_entry_id, predicate=PRED_HAS_MODEL_ID)
    if not model_id:
        model_id = _derive_model_id_from_aliases(aliases=model_aliases, provider=provider)

    api_profiles = []
    for profile_concept_id in _get_related_concept_ids(
        registry_entry_id, PRED_HAS_MODEL_API_PROFILE
    ):
        parameter_constraints = [
            _resolve_parameter_constraint_from_graph(
                constraint_concept_id=constraint_concept_id
            )
            for constraint_concept_id in _get_related_concept_ids(
                profile_concept_id, PRED_HAS_MODEL_PARAMETER_CONSTRAINT
            )
        ]
        api_profiles.append(
            {
                "profile_concept_id": profile_concept_id,
                "api_surface": _get_first_text(
                    profile_concept_id, predicate=PRED_HAS_API_SURFACE
                ),
                "parameter_constraints": parameter_constraints,
            }
        )

    return {
        "model_id": model_id,
        "model_aliases": model_aliases,
        "provider": provider,
        "locality": "local" if provider == "ollama" else "external",
        "concept_id": model_concept_id,
        "registry_entry_id": registry_entry_id,
        "api_profiles": api_profiles,
    }


def _load_registry_from_vontology_graph(
    *, preferred_language: str | None = None
) -> Optional[Mapping[str, Any]]:
    registry_concept_id = _resolve_concept_id_by_name(
        _DEFAULT_REGISTRY_NAME,
        preferred_language=preferred_language,
    )
    if not registry_concept_id:
        return None

    models = []
    seen_entry_ids: set[str] = set()
    for registry_entry_id in _get_related_concept_ids(
        registry_concept_id, PRED_HAS_MODEL_ENTRY
    ):
        if registry_entry_id in seen_entry_ids:
            continue
        seen_entry_ids.add(registry_entry_id)
        entry = _resolve_model_entry_from_graph(
            registry_entry_id=registry_entry_id,
            preferred_language=preferred_language,
        )
        if entry is None:
            continue
        models.append(entry)

    if not models:
        return None

    return {
        "registry_concept_id": registry_concept_id,
        "models": models,
    }


def _load_registry_from_vontology_json(
    *, preferred_language: str | None = None
) -> Optional[Mapping[str, Any]]:
    registry_concept_id = _resolve_concept_id_by_name(
        _DEFAULT_REGISTRY_NAME,
        preferred_language=preferred_language,
    )
    if not registry_concept_id:
        return None

    predicate_id = _resolve_concept_id_by_name(
        _LEGACY_REGISTRY_JSON_PREDICATE_NAME,
        preferred_language=preferred_language,
    )
    if not predicate_id:
        return None

    for row in _get_text_rows(registry_concept_id, predicate=predicate_id, limit=5):
        raw_text = row.get("text")
        parsed = _parse_registry_json(raw_text if isinstance(raw_text, str) else "")
        if parsed is not None:
            return parsed

    return None


def _build_registry_from_settings() -> Mapping[str, Any]:
    # Get user/org context from session for resolved LLM setting
    user_concept_id = None
    org_concept_id = None
    try:
        from flask import session, has_request_context

        if has_request_context():
            user_concept_id = session.get("user_concept_id")
            org_concept_id = session.get("organisation_concept_id")
    except Exception:
        pass

    models = []
    enabled = resolve_enabled_llm_settings(
        user_concept_id=user_concept_id,
        org_concept_id=org_concept_id,
    )
    for entry in enabled:
        if not isinstance(entry, Mapping):
            continue
        provider = entry.get("provider")
        model = entry.get("model")
        locality = "external"
        if isinstance(provider, str) and provider.lower() == "ollama":
            locality = "local"
        if not isinstance(model, str) or not model.strip():
            continue
        models.append(
            {
                "model_id": model.strip(),
                "provider": provider or "unknown",
                "locality": locality,
                "source": "settings",
                "scope": entry.get("scope"),
                "host": entry.get("host"),
            }
        )

    if not models:
        active = (
            resolve_llm_setting(
                user_concept_id=user_concept_id, org_concept_id=org_concept_id
            )
            or {}
        )
        provider = active.get("provider") if isinstance(active, dict) else None
        model = active.get("model") if isinstance(active, dict) else None
        locality = "external"
        if isinstance(provider, str) and provider.lower() == "ollama":
            locality = "local"
        if isinstance(model, str) and model.strip():
            models.append(
                {
                    "model_id": model.strip(),
                    "provider": provider or "unknown",
                    "locality": locality,
                    "source": "settings",
                    "scope": active.get("scope") if isinstance(active, Mapping) else None,
                }
            )

    return {
        "source": "settings",
        "models": models,
    }


def get_model_registry_snapshot(
    *, preferred_language: str | None = None
) -> Mapping[str, Any]:
    registry = _load_registry_from_vontology_graph(
        preferred_language=preferred_language
    )
    if registry is not None:
        return {
            "source": "vontology_graph",
            **registry,
        }

    registry = _load_registry_from_vontology_json(
        preferred_language=preferred_language
    )
    if registry is not None:
        return {
            "source": "vontology_json",
            **registry,
        }

    return _build_registry_from_settings()


def _entry_matches_model(
    entry: Mapping[str, Any],
    *,
    model: str,
    provider: str | None = None,
) -> bool:
    def _matches_token(candidate: str, requested: str) -> bool:
        if not candidate or not requested:
            return False
        if candidate == requested:
            return True
        return requested.startswith(f"{candidate}-") or requested.startswith(
            f"{candidate}."
        )

    model_key = _normalise_lookup_token(model)
    if not model_key:
        return False

    provider_key = _normalise_provider_name(provider)
    entry_provider = _normalise_provider_name(entry.get("provider"))
    if provider_key and entry_provider and provider_key != entry_provider:
        return False

    entry_model_id = _normalise_lookup_token(entry.get("model_id"))
    candidate_keys = {
        entry_model_id,
        _normalise_lookup_token(entry.get("concept_id")),
        _normalise_lookup_token(entry.get("registry_entry_id")),
    }

    model_aliases = entry.get("model_aliases")
    if isinstance(model_aliases, Sequence) and not isinstance(model_aliases, str):
        candidate_keys.update(
            _normalise_lookup_token(alias)
            for alias in model_aliases
            if isinstance(alias, str)
        )
        candidate_keys.update(
            _normalise_lookup_token(
                _strip_provider_prefix(alias, entry_provider or provider_key or None)
            )
            for alias in model_aliases
            if isinstance(alias, str)
        )

    bare_model_key = _strip_provider_prefix(model_key, provider_key or None)
    stripped_entry_model_id = _strip_provider_prefix(
        entry_model_id,
        entry_provider or provider_key or None,
    )
    if stripped_entry_model_id:
        candidate_keys.add(_normalise_lookup_token(stripped_entry_model_id))

    if entry_provider:
        if entry_model_id:
            candidate_keys.add(f"{entry_provider}:{entry_model_id}")

    for candidate_key in candidate_keys:
        if _matches_token(candidate_key, model_key):
            return True
        if bare_model_key and _matches_token(candidate_key, bare_model_key):
            return True
    return False


def _iter_parameter_constraints_for_entry(
    entry: Mapping[str, Any], *, api_surface: str | None = None
) -> list[Mapping[str, Any]]:
    profiles = entry.get("api_profiles")
    if not isinstance(profiles, Sequence) or isinstance(profiles, str):
        return []

    requested_surface = _normalise_lookup_token(api_surface)
    matching_constraints: list[Mapping[str, Any]] = []
    generic_constraints: list[Mapping[str, Any]] = []

    for profile in profiles:
        if not isinstance(profile, Mapping):
            continue
        constraints = profile.get("parameter_constraints")
        if not isinstance(constraints, Sequence) or isinstance(constraints, str):
            continue

        profile_surface = _normalise_lookup_token(profile.get("api_surface"))
        if not requested_surface:
            matching_constraints.extend(
                constraint
                for constraint in constraints
                if isinstance(constraint, Mapping)
            )
            continue

        if not profile_surface:
            generic_constraints.extend(
                constraint
                for constraint in constraints
                if isinstance(constraint, Mapping)
            )
            continue

        if profile_surface == requested_surface:
            matching_constraints.extend(
                constraint
                for constraint in constraints
                if isinstance(constraint, Mapping)
            )

    return matching_constraints or generic_constraints


def resolve_model_parameter_policy(
    *,
    model: str,
    parameter: str,
    provider: str | None = None,
    api_surface: str | None = None,
    preferred_language: str | None = None,
) -> Mapping[str, Any] | None:
    registry_snapshot = get_model_registry_snapshot(preferred_language=preferred_language)
    models = registry_snapshot.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str):
        return None

    requested_parameter = _normalise_parameter_name(parameter)
    if not requested_parameter:
        return None

    for entry in models:
        if not isinstance(entry, Mapping):
            continue
        if not _entry_matches_model(entry, model=model, provider=provider):
            continue

        for constraint in _iter_parameter_constraints_for_entry(
            entry, api_surface=api_surface
        ):
            parameter_name = _normalise_parameter_name(
                constraint.get("parameter") or constraint.get("parameter_concept_id")
            )
            if parameter_name != requested_parameter:
                continue

            return {
                "parameter": parameter_name,
                "action": constraint.get("action"),
                "fixed_value": constraint.get("fixed_value"),
                "allowed_values": list(constraint.get("allowed_values") or []),
                "constraint_concept_id": constraint.get("constraint_concept_id"),
                "parameter_concept_id": constraint.get("parameter_concept_id"),
                "profile_concept_id": constraint.get("profile_concept_id"),
                "registry_entry_id": entry.get("registry_entry_id"),
                "concept_id": entry.get("concept_id"),
                "provider": entry.get("provider"),
                "model_id": entry.get("model_id"),
                "source": registry_snapshot.get("source"),
            }

    return None


def _coerce_fixed_parameter_value(
    fixed_value: Any, original_value: Any
) -> Any:
    if not isinstance(fixed_value, str) or not fixed_value.strip():
        return original_value

    raw_value = fixed_value.strip()
    if isinstance(original_value, bool):
        return raw_value.lower() in {"1", "true", "yes", "on"}
    if isinstance(original_value, int) and not isinstance(original_value, bool):
        try:
            return int(float(raw_value))
        except (TypeError, ValueError):
            return original_value
    if isinstance(original_value, float):
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return original_value
    return raw_value


def sanitise_model_parameter_value(
    *,
    model: str,
    parameter: str,
    value: Any,
    provider: str | None = None,
    api_surface: str | None = None,
    preferred_language: str | None = None,
) -> Any:
    if value is None:
        return None

    policy = resolve_model_parameter_policy(
        model=model,
        parameter=parameter,
        provider=provider,
        api_surface=api_surface,
        preferred_language=preferred_language,
    )
    if not isinstance(policy, Mapping):
        return value

    action = _normalise_parameter_action(policy.get("action"))
    if action == PARAMETER_ACTION_OMIT:
        return None
    if action == PARAMETER_ACTION_FIXED_VALUE:
        return _coerce_fixed_parameter_value(policy.get("fixed_value"), value)
    allowed_values = policy.get("allowed_values")
    if isinstance(allowed_values, Sequence) and not isinstance(allowed_values, str):
        allowed = {
            str(item).strip().lower()
            for item in allowed_values
            if isinstance(item, str) and str(item).strip()
        }
        if allowed and str(value).strip().lower() not in allowed:
            return None
    return value


def build_model_stage_suitability_evidence(
    *,
    model: str | None,
    stage: str,
    replay_set_id: str,
    replay_case_id: str,
    request_id: str | None = None,
    workflow_id: str | None = None,
    prompt_id: str | None = None,
    prompt_variant_id: str | None = None,
    verdict: str,
    metrics: Mapping[str, Any] | None = None,
    rationale: str | None = None,
    evidence_artifact: Mapping[str, Any] | None = None,
    promotion_blockers: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build a Vontology-ready model/stage suitability evidence payload.

    This helper deliberately does not decide semantic routing policy or mutate
    Vontology. It shapes replay-derived observations into a stable payload that
    a represented policy workflow can review, persist, promote, expire, or
    reject with provenance.
    """

    metrics_payload = {
        str(key): value
        for key, value in (metrics or {}).items()
        if isinstance(key, str)
    }
    blockers = [
        cleaned
        for item in promotion_blockers or ()
        if (cleaned := _safe_evidence_text(item))
    ]
    payload: dict[str, Any] = {
        "schema_version": MODEL_STAGE_SUITABILITY_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": "#V#model_stage_suitability_evidence",
        "model": _safe_evidence_text(model) or None,
        "workflow_stage": _safe_evidence_text(stage),
        "workflow_id": _safe_evidence_text(workflow_id) or None,
        "prompt_id": _safe_evidence_text(prompt_id) or None,
        "prompt_variant_id": _safe_evidence_text(prompt_variant_id) or None,
        "replay_set_id": _safe_evidence_text(replay_set_id),
        "replay_case_id": _safe_evidence_text(replay_case_id),
        "request_id": _safe_evidence_text(request_id) or None,
        "verdict": _normalise_lookup_token(verdict) or "unknown",
        "metrics": metrics_payload,
        "rationale": _safe_evidence_text(rationale) or None,
        "promotion_eligible": False,
        "promotion_blockers": blockers,
    }
    if isinstance(evidence_artifact, Mapping):
        payload["evidence_artifact"] = {
            str(key): value
            for key, value in evidence_artifact.items()
            if isinstance(key, str)
        }
    return payload


def assess_model_stage_certification(
    evidence_entries: Sequence[Mapping[str, Any]],
    *,
    minimum_replay_cases: int = DEFAULT_MINIMUM_REPLAY_CASES_FOR_CERTIFICATION,
) -> dict[str, Any]:
    """Assess whether replay evidence is sufficient to certify a model/stage.

    The function enforces only generic promotion guardrails: enough distinct
    replay cases, successful evidence verdicts, and no per-entry blockers. It
    does not encode which workflow, prompt, or domain should use a model.
    """

    usable_entries = [
        entry for entry in evidence_entries if isinstance(entry, Mapping)
    ]
    replay_case_ids = {
        case_id
        for entry in usable_entries
        if (case_id := _safe_evidence_text(entry.get("replay_case_id")))
    }
    verdict_counts: dict[str, int] = {}
    blocker_counts: dict[str, int] = {}
    for entry in usable_entries:
        verdict = _normalise_lookup_token(entry.get("verdict")) or "unknown"
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        for blocker in entry.get("promotion_blockers") or ():
            cleaned_blocker = _safe_evidence_text(blocker)
            if cleaned_blocker:
                blocker_counts[cleaned_blocker] = (
                    blocker_counts.get(cleaned_blocker, 0) + 1
                )

    minimum_cases = max(int(minimum_replay_cases), 1)
    blockers: list[str] = []
    if len(replay_case_ids) < minimum_cases:
        blockers.append("insufficient_distinct_replay_cases")
    if not usable_entries:
        blockers.append("no_suitability_evidence")
    non_pass_verdicts = {
        verdict: count
        for verdict, count in verdict_counts.items()
        if verdict not in {"passed", "pass"}
    }
    if non_pass_verdicts:
        blockers.append("non_passing_evidence_present")
    if blocker_counts:
        blockers.append("evidence_entry_promotion_blockers_present")

    promotion_authorised = not blockers
    return {
        "schema_version": MODEL_STAGE_CERTIFICATION_DECISION_SCHEMA_VERSION,
        "promotion_authorised": promotion_authorised,
        "certification_status": "eligible" if promotion_authorised else "not_eligible",
        "minimum_replay_cases": minimum_cases,
        "distinct_replay_case_count": len(replay_case_ids),
        "evidence_entry_count": len(usable_entries),
        "verdict_counts": verdict_counts,
        "promotion_blocker_counts": blocker_counts,
        "promotion_blockers": blockers,
    }
