"""Model registry access for workflow-driven model routing and runtime policies."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

from .settings_service import resolve_enabled_llm_settings, resolve_llm_setting

logger = logging.getLogger(__name__)

_DEFAULT_REGISTRY_NAME = "default_model_registry"
_DEFAULT_REGISTRY_CONCEPT_ID = "#V#default_model_registry"
_LEGACY_REGISTRY_JSON_PREDICATE_NAME = "has_model_registry_json"
_MODEL_REGISTRY_GRAPH_LOADER_SCHEMA_VERSION = "model_registry_graph_loader.v2-batched"

PRED_HAS_MODEL_ENTRY = "#V#has_model_entry"
PRED_REFERS_TO_MODEL = "#V#refers_to_model"
PRED_HAS_PROVIDER = "#V#has_provider"
PRED_HAS_MODEL_API_PROFILE = "#V#has_model_api_profile"
PRED_HAS_MODEL_PARAMETER_CONSTRAINT = "#V#has_model_parameter_constraint"
PRED_CONSTRAINS_MODEL_PARAMETER = "#V#constrains_model_parameter"
PRED_HAS_MODEL_ID = "#V#has_model_id"
PRED_HAS_MODEL_PRICING_JSON = "#V#has_model_pricing_json"
PRED_HAS_MODEL_CAPABILITIES_JSON = "#V#has_model_capabilities_json"
PRED_HAS_API_SURFACE = "#V#has_api_surface"
PRED_HAS_STRUCTURED_TOOL_CALLING = "#V#has_structured_tool_calling_support"
PRED_HAS_TOOL_CONTINUATION_MODE = "#V#has_tool_continuation_mode"
PRED_HAS_RESPONSE_STORAGE_POLICY = "#V#has_response_storage_policy"
PRED_HAS_PROVIDER_CONNECTION_ID = "#V#has_provider_connection_id"
PRED_HAS_DEPLOYMENT_ID = "#V#has_deployment_id"
PRED_HAS_PARAMETER_ACTION = "#V#has_parameter_action"
PRED_HAS_FIXED_PARAMETER_VALUE = "#V#has_fixed_parameter_value"
PRED_HAS_ALLOWED_PARAMETER_VALUE = "#V#has_allowed_parameter_value"

MODEL_STAGE_SUITABILITY_EVIDENCE_SCHEMA_VERSION = "model_stage_suitability_evidence.v1"
MODEL_STAGE_CERTIFICATION_DECISION_SCHEMA_VERSION = (
    "model_stage_certification_decision.v1"
)
DEFAULT_MINIMUM_REPLAY_CASES_FOR_CERTIFICATION = 2

KNOWN_PROVIDER_PREFIXES = frozenset(
    {"openai", "openrouter", "anthropic", "gemini", "meta", "ollama", "deepseek"}
)
PARAMETER_ACTION_OMIT = "omit"
PARAMETER_ACTION_FIXED_VALUE = "fixed_value"

_MODEL_REGISTRY_SNAPSHOT_CACHE: dict[str, dict[str, Any]] = {}
_MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK = threading.Lock()
_MODEL_REGISTRY_SNAPSHOT_REFRESH_INFLIGHT: set[str] = set()
_MODEL_REGISTRY_SNAPSHOT_REFRESH_STATE: dict[str, dict[str, Any]] = {}
_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_SCHEMA_VERSION = "model_registry_snapshot_cache.v2"
_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_SOURCES = frozenset(
    {"vontology_graph", "vontology_json"}
)
_DEFAULT_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH = (
    Path(__file__).resolve().parents[3]
    / "data"
    / "model_registry_cache"
    / "model_registry_snapshot.json"
)


def _truthy_env(var_name: str, *, default: str = "0") -> bool:
    return os.getenv(var_name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_float_seconds(var_name: str, *, default: float) -> float:
    raw_value = os.getenv(var_name, str(default))
    try:
        parsed = float(str(raw_value).strip())
    except Exception:
        parsed = default
    return max(0.0, parsed)


def _registry_snapshot_cache_ttl_seconds() -> float:
    return _env_float_seconds(
        "VON_MODEL_REGISTRY_SNAPSHOT_CACHE_TTL_SECONDS",
        default=300.0,
    )


def _registry_snapshot_disk_cache_enabled() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH"
    ):
        # Tests that intentionally exercise the disk cache provide an isolated
        # path.  All other pytest processes must not overwrite the live
        # workspace snapshot with mocked or partial Vontology authority.
        return False
    return _truthy_env(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_ENABLED",
        default="1",
    )


def _registry_snapshot_disk_cache_ttl_seconds() -> float:
    return _env_float_seconds(
        "VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_TTL_SECONDS",
        default=3600.0,
    )


def _registry_snapshot_max_stale_seconds() -> float:
    # The one-hour default matches one extra default refresh interval. Operators
    # may configure the refresh and stale-grace intervals independently.
    return _env_float_seconds(
        "VON_MODEL_REGISTRY_SNAPSHOT_MAX_STALE_SECONDS",
        default=3600.0,
    )


def _registry_snapshot_disk_cache_path() -> Path:
    override = os.getenv("VON_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH")
    if isinstance(override, str) and override.strip():
        return Path(override.strip())
    return _DEFAULT_MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_PATH


def _registry_snapshot_cache_key(preferred_language: str | None) -> str:
    return (
        preferred_language.strip().lower()
        if isinstance(preferred_language, str) and preferred_language.strip()
        else ""
    )


def _registry_snapshot_refresh_token(
    *, cache_key: str, authority_fingerprint: str
) -> str:
    return f"{cache_key}:{authority_fingerprint}"


def _utc_iso_from_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _mongo_principal_fingerprint(uri: Any) -> str | None:
    """Return a non-reversible identity for Mongo role-scoped authority."""

    if not isinstance(uri, str) or not uri.strip():
        return None
    try:
        parsed = urlsplit(uri.strip())
        username = unquote(parsed.username or "").strip()
        query_options = {
            str(key).strip().lower(): str(value).strip()
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        }
        authority_identity = {
            "username": username,
            "auth_source": query_options.get("authsource", ""),
            "auth_mechanism": query_options.get("authmechanism", ""),
        }
        if not any(authority_identity.values()):
            return None
        encoded = json.dumps(
            authority_identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except Exception:
        return None


def _model_registry_authority_fingerprint() -> str:
    """Identify the represented authority without persisting connection secrets."""

    namespace = str(os.getenv("VON_DEFAULT_NAMESPACE") or "").strip()
    try:
        from ..db.mongo_client import (
            MONGO_URI,
            get_configured_database_name,
            get_effective_mongo_uri,
            get_mongo_fallback_policy_state,
            is_using_fallback_uri,
        )
        from ..db.mongo_uri_redaction import build_safe_mongo_connection_location

        effective_mongo_uri = get_effective_mongo_uri()
        fallback_policy = get_mongo_fallback_policy_state()
        authority = {
            "database_name": get_configured_database_name(),
            "mongo_location": build_safe_mongo_connection_location(
                effective_mongo_uri,
                using_fallback=is_using_fallback_uri(),
                fallback_kind=fallback_policy.get("active_fallback_kind"),
                fallback_target_uri=MONGO_URI,
            ),
            "mongo_principal_fingerprint": _mongo_principal_fingerprint(
                effective_mongo_uri
            ),
            "namespace": namespace,
            "graph_loader_schema_version": (
                _MODEL_REGISTRY_GRAPH_LOADER_SCHEMA_VERSION
            ),
        }
    except Exception:
        # The fallback remains secret-free and separates configured databases.
        # A later successful refresh recomputes the fingerprint from the
        # effective, sanitised connection location before it persists.
        authority = {
            "database_name": str(os.getenv("VON_DB_NAME") or "von_db").strip(),
            "mongo_location": {"available": False},
            "namespace": namespace,
            "graph_loader_schema_version": (
                _MODEL_REGISTRY_GRAPH_LOADER_SCHEMA_VERSION
            ),
        }
    encoded = json.dumps(
        authority,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _represented_registry_snapshot(snapshot: Any) -> bool:
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("source") not in _MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_SOURCES
    ):
        return False
    models = snapshot.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str) or not models:
        return False
    for model in models:
        if not isinstance(model, Mapping):
            return False
        if not any(
            isinstance(model.get(field), str) and str(model.get(field)).strip()
            for field in ("model_id", "concept_id", "registry_entry_id")
        ):
            return False
    return True


def _registry_snapshot_refresh_seconds() -> float:
    if _registry_snapshot_disk_cache_enabled():
        disk_ttl_seconds = _registry_snapshot_disk_cache_ttl_seconds()
        if disk_ttl_seconds > 0:
            return disk_ttl_seconds
    return _registry_snapshot_cache_ttl_seconds()


def _store_registry_snapshot_in_memory(
    *,
    cache_key: str,
    snapshot: Mapping[str, Any],
    authority_fingerprint: str,
    created_at: float,
    refresh_after: float,
    stale_until: float,
) -> None:
    if stale_until <= time.time() or not _represented_registry_snapshot(snapshot):
        return
    with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
        _MODEL_REGISTRY_SNAPSHOT_CACHE[cache_key] = {
            "authority_fingerprint": authority_fingerprint,
            "created_at": created_at,
            "refresh_after": refresh_after,
            "stale_until": stale_until,
            "snapshot": snapshot,
        }


def _load_registry_snapshot_from_disk(
    *,
    cache_key: str,
    authority_fingerprint: str,
    now: float,
) -> Mapping[str, Any] | None:
    if not _registry_snapshot_disk_cache_enabled():
        return None
    try:
        cache_path = _registry_snapshot_disk_cache_path()
        if not cache_path.exists():
            return None
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return None
        if (
            payload.get("schema_version")
            != _MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_SCHEMA_VERSION
        ):
            return None
        entries = payload.get("entries")
        entry: Any
        if isinstance(entries, Mapping):
            entry = entries.get(cache_key)
        elif payload.get("cache_key") == cache_key:
            entry = payload
        else:
            return None
        if not isinstance(entry, Mapping):
            return None
        if entry.get("authority_fingerprint") != authority_fingerprint:
            return None
        created_at = entry.get("created_at")
        refresh_after = entry.get("refresh_after")
        stale_until = entry.get("stale_until")
        if not all(
            isinstance(value, (int, float))
            for value in (created_at, refresh_after, stale_until)
        ):
            return None
        configured_stale_until = (
            float(refresh_after) + _registry_snapshot_max_stale_seconds()
        )
        effective_stale_until = min(float(stale_until), configured_stale_until)
        if not (
            float(created_at) <= float(refresh_after) <= float(stale_until)
            and effective_stale_until > now
        ):
            return None
        source = entry.get("source")
        if source not in _MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_SOURCES:
            return None
        snapshot = entry.get("snapshot")
        if (
            not _represented_registry_snapshot(snapshot)
            or snapshot.get("source") != source
        ):
            return None
        return {
            "authority_fingerprint": authority_fingerprint,
            "created_at": float(created_at),
            "refresh_after": float(refresh_after),
            "stale_until": effective_stale_until,
            "snapshot": snapshot,
        }
    except Exception as exc:
        logger.debug("Could not load model registry snapshot disk cache: %s", exc)
        return None


def _persist_registry_snapshot_to_disk(
    *,
    cache_key: str,
    preferred_language: str | None,
    snapshot: Mapping[str, Any],
    authority_fingerprint: str,
    created_at: float,
    refresh_after: float,
    stale_until: float,
    hydrate_duration_ms: int | None,
) -> None:
    if not _registry_snapshot_disk_cache_enabled():
        return
    if _registry_snapshot_disk_cache_ttl_seconds() <= 0:
        return
    source = snapshot.get("source")
    if source not in _MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_SOURCES:
        return
    if stale_until <= created_at:
        return

    entry = {
        "cache_key": cache_key,
        "preferred_language": preferred_language or "",
        "authority_fingerprint": authority_fingerprint,
        "source": source,
        "created_at": created_at,
        "created_at_utc": _utc_iso_from_timestamp(created_at),
        "refresh_after": refresh_after,
        "refresh_after_utc": _utc_iso_from_timestamp(refresh_after),
        "stale_until": stale_until,
        "stale_until_utc": _utc_iso_from_timestamp(stale_until),
        "metadata": {
            "hydrate_duration_ms": hydrate_duration_ms,
        },
        "snapshot": snapshot,
    }

    cache_path = _registry_snapshot_disk_cache_path()
    temp_path: str | None = None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        entries: dict[str, Any] = {}
        try:
            existing_payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            existing_payload = None
        if (
            isinstance(existing_payload, Mapping)
            and existing_payload.get("schema_version")
            == _MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_SCHEMA_VERSION
        ):
            existing_entries = existing_payload.get("entries")
            if isinstance(existing_entries, Mapping):
                entries = {
                    str(existing_key): existing_entry
                    for existing_key, existing_entry in existing_entries.items()
                    if isinstance(existing_key, str)
                    and isinstance(existing_entry, Mapping)
                }
            elif isinstance(existing_payload.get("cache_key"), str):
                entries[str(existing_payload["cache_key"])] = existing_payload
        entries[cache_key] = entry
        payload = {
            "schema_version": _MODEL_REGISTRY_SNAPSHOT_DISK_CACHE_SCHEMA_VERSION,
            "updated_at": created_at,
            "updated_at_utc": _utc_iso_from_timestamp(created_at),
            "entries": entries,
        }
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{cache_path.name}.",
            suffix=".tmp",
            dir=str(cache_path.parent),
        )
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp_path, cache_path)
        temp_path = None
        try:
            os.chmod(cache_path, 0o600)
        except OSError:
            pass
    except Exception as exc:
        logger.debug("Could not persist model registry snapshot disk cache: %s", exc)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def clear_model_registry_snapshot_caches(*, remove_disk: bool = False) -> None:
    with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
        _MODEL_REGISTRY_SNAPSHOT_CACHE.clear()
        _MODEL_REGISTRY_SNAPSHOT_REFRESH_INFLIGHT.clear()
        _MODEL_REGISTRY_SNAPSHOT_REFRESH_STATE.clear()
    if not remove_disk:
        return
    try:
        _registry_snapshot_disk_cache_path().unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("Could not remove model registry snapshot disk cache: %s", exc)


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
        if isinstance(row.get("text"), str)
        and str(row.get("text")).strip().startswith("#V#")
    ]


def _related_concept_ids_from_doc(
    concept: Mapping[str, Any] | None,
    predicate: str,
) -> list[str]:
    """Read canonical relationship targets from an already-fetched concept."""

    if not isinstance(concept, Mapping):
        return []
    relationships = concept.get("relationships")
    if not isinstance(relationships, Mapping):
        return []
    direct_targets = relationships.get(predicate)
    if isinstance(direct_targets, str) and direct_targets.strip():
        return [direct_targets.strip()]
    if isinstance(direct_targets, Sequence) and not isinstance(
        direct_targets, (str, bytes, bytearray)
    ):
        targets = [
            str(item).strip()
            for item in direct_targets
            if isinstance(item, str) and str(item).strip()
        ]
        if targets:
            return targets
    linked_to = relationships.get("linked_to")
    if isinstance(linked_to, Sequence) and not isinstance(
        linked_to, (str, bytes, bytearray)
    ):
        return [
            str(item.get("target") or item.get("concept_id") or "").strip()
            for item in linked_to
            if isinstance(item, Mapping)
            and item.get("predicate") == predicate
            and str(item.get("target") or item.get("concept_id") or "").strip()
        ]
    return []


def _first_batched_text(
    rows_by_concept: Mapping[str, Sequence[Mapping[str, Any]]],
    concept_id: str,
    predicate: str,
) -> str | None:
    for row in rows_by_concept.get(concept_id, ()):
        if row.get("predicate") != predicate:
            continue
        raw_text = row.get("text")
        if isinstance(raw_text, str) and raw_text.strip():
            return raw_text.strip()
    return None


def _batched_text_relationship_targets(
    rows_by_concept: Mapping[str, Sequence[Mapping[str, Any]]],
    concept_id: str,
    predicate: str,
) -> list[str]:
    return [
        str(row.get("text")).strip()
        for row in rows_by_concept.get(concept_id, ())
        if row.get("predicate") == predicate
        and isinstance(row.get("text"), str)
        and str(row.get("text")).strip().startswith("#V#")
    ]


def _load_registry_from_vontology_graph_batched(
    *, preferred_language: str | None = None
) -> Optional[Mapping[str, Any]]:
    """Hydrate the canonical registry in bounded graph waves.

    The represented graph remains authority.  This function changes only the
    physical read shape: related concept documents are fetched in batches and
    all semantic text predicates are resolved in one joined text read.  Legacy
    text-encoded relationship graphs fall back to the compatibility loader.
    """

    del preferred_language  # Existing graph text selection is insertion ordered.
    from .concept_service import get_concepts_by_concept_ids_exact
    from .text_value_service import get_texts_for_concepts

    root_docs = get_concepts_by_concept_ids_exact([_DEFAULT_REGISTRY_CONCEPT_ID])
    root = root_docs.get(_DEFAULT_REGISTRY_CONCEPT_ID)
    entry_ids = _related_concept_ids_from_doc(root, PRED_HAS_MODEL_ENTRY)
    if not entry_ids:
        return None

    entry_docs = get_concepts_by_concept_ids_exact(entry_ids)
    model_ids: list[str] = []
    profile_ids: list[str] = []
    for entry_id in entry_ids:
        entry_doc = entry_docs.get(entry_id)
        referred_models = _related_concept_ids_from_doc(entry_doc, PRED_REFERS_TO_MODEL)
        if not referred_models:
            # A model entry without a direct/linked model edge may be using the
            # legacy text-relation encoding. Let the compatibility loader decide.
            return None
        model_ids.extend(referred_models[:1])
        profile_ids.extend(
            _related_concept_ids_from_doc(entry_doc, PRED_HAS_MODEL_API_PROFILE)
        )

    model_and_profile_docs = get_concepts_by_concept_ids_exact(
        [*model_ids, *profile_ids]
    )
    provider_ids: list[str] = []
    for model_id in model_ids:
        provider_ids.extend(
            _related_concept_ids_from_doc(
                model_and_profile_docs.get(model_id), PRED_HAS_PROVIDER
            )[:1]
        )
    constraint_ids: list[str] = []
    for profile_id in profile_ids:
        constraint_ids.extend(
            _related_concept_ids_from_doc(
                model_and_profile_docs.get(profile_id),
                PRED_HAS_MODEL_PARAMETER_CONSTRAINT,
            )
        )

    provider_and_constraint_docs = get_concepts_by_concept_ids_exact(
        [*provider_ids, *constraint_ids]
    )
    parameter_ids: list[str] = []
    for constraint_id in constraint_ids:
        parameter_ids.extend(
            _related_concept_ids_from_doc(
                provider_and_constraint_docs.get(constraint_id),
                PRED_CONSTRAINS_MODEL_PARAMETER,
            )[:1]
        )
    parameter_docs = get_concepts_by_concept_ids_exact(parameter_ids)
    concept_docs: dict[str, Mapping[str, Any]] = {
        **root_docs,
        **entry_docs,
        **model_and_profile_docs,
        **provider_and_constraint_docs,
        **parameter_docs,
    }

    text_subject_ids = [
        *entry_ids,
        *model_ids,
        *provider_ids,
        *profile_ids,
        *constraint_ids,
    ]
    text_predicates = [
        "hasName",
        PRED_HAS_MODEL_API_PROFILE,
        PRED_HAS_PROVIDER,
        PRED_HAS_MODEL_PARAMETER_CONSTRAINT,
        PRED_CONSTRAINS_MODEL_PARAMETER,
        PRED_HAS_MODEL_ID,
        PRED_HAS_MODEL_PRICING_JSON,
        PRED_HAS_MODEL_CAPABILITIES_JSON,
        PRED_HAS_API_SURFACE,
        PRED_HAS_STRUCTURED_TOOL_CALLING,
        PRED_HAS_TOOL_CONTINUATION_MODE,
        PRED_HAS_RESPONSE_STORAGE_POLICY,
        PRED_HAS_PROVIDER_CONNECTION_ID,
        PRED_HAS_DEPLOYMENT_ID,
        PRED_HAS_PARAMETER_ACTION,
        PRED_HAS_FIXED_PARAMETER_VALUE,
        PRED_HAS_ALLOWED_PARAMETER_VALUE,
    ]
    rows_by_concept = get_texts_for_concepts(
        text_subject_ids,
        predicates=text_predicates,
        limit_per_concept=100,
    )

    # Relationship targets historically stored as text assertions remain
    # supported by the legacy loader. If one is present where this bounded
    # loader saw no canonical concept edge, delegate the whole snapshot to the
    # compatibility path rather than silently returning a partial registry.
    relationship_subjects = [
        *[
            (entry_id, entry_docs.get(entry_id), PRED_HAS_MODEL_API_PROFILE)
            for entry_id in entry_ids
        ],
        *[
            (model_id, model_and_profile_docs.get(model_id), PRED_HAS_PROVIDER)
            for model_id in model_ids
        ],
        *[
            (
                profile_id,
                model_and_profile_docs.get(profile_id),
                PRED_HAS_MODEL_PARAMETER_CONSTRAINT,
            )
            for profile_id in profile_ids
        ],
        *[
            (
                constraint_id,
                provider_and_constraint_docs.get(constraint_id),
                PRED_CONSTRAINS_MODEL_PARAMETER,
            )
            for constraint_id in constraint_ids
        ],
    ]
    for subject_id, subject_doc, predicate in relationship_subjects:
        if _related_concept_ids_from_doc(subject_doc, predicate):
            continue
        if _batched_text_relationship_targets(
            rows_by_concept, subject_id, predicate
        ):
            return None

    models: list[dict[str, Any]] = []
    for registry_entry_id in entry_ids:
        entry_doc = entry_docs.get(registry_entry_id)
        referred_models = _related_concept_ids_from_doc(entry_doc, PRED_REFERS_TO_MODEL)
        if not referred_models:
            continue
        model_concept_id = referred_models[0]
        model_doc = concept_docs.get(model_concept_id)
        entry_provider_ids = _related_concept_ids_from_doc(model_doc, PRED_HAS_PROVIDER)
        provider_concept_id = entry_provider_ids[0] if entry_provider_ids else None
        provider = None
        if provider_concept_id:
            provider = _normalise_provider_name(
                _first_batched_text(rows_by_concept, provider_concept_id, "hasName")
                or provider_concept_id
            )

        aliases: list[str] = []
        seen_aliases: set[str] = set()
        for row in rows_by_concept.get(model_concept_id, ()):
            if row.get("predicate") != "hasName":
                continue
            raw_text = row.get("text")
            if not isinstance(raw_text, str):
                continue
            candidate = raw_text.strip().replace(": ", ":")
            if not _looks_like_machine_model_name(candidate):
                continue
            for alias in (candidate, _strip_provider_prefix(candidate, provider)):
                alias_key = _normalise_lookup_token(alias)
                if alias_key and alias_key not in seen_aliases:
                    seen_aliases.add(alias_key)
                    aliases.append(alias.strip())

        model_id = _first_batched_text(
            rows_by_concept, registry_entry_id, PRED_HAS_MODEL_ID
        ) or _derive_model_id_from_aliases(aliases=aliases, provider=provider)

        api_profiles: list[dict[str, Any]] = []
        for profile_concept_id in _related_concept_ids_from_doc(
            entry_doc, PRED_HAS_MODEL_API_PROFILE
        ):
            constraints: list[dict[str, Any]] = []
            for constraint_concept_id in _related_concept_ids_from_doc(
                concept_docs.get(profile_concept_id),
                PRED_HAS_MODEL_PARAMETER_CONSTRAINT,
            ):
                parameter_ids_for_constraint = _related_concept_ids_from_doc(
                    concept_docs.get(constraint_concept_id),
                    PRED_CONSTRAINS_MODEL_PARAMETER,
                )
                parameter_concept_id = (
                    parameter_ids_for_constraint[0]
                    if parameter_ids_for_constraint
                    else None
                )
                allowed_values = [
                    str(row.get("text")).strip()
                    for row in rows_by_concept.get(constraint_concept_id, ())
                    if row.get("predicate") == PRED_HAS_ALLOWED_PARAMETER_VALUE
                    and isinstance(row.get("text"), str)
                    and str(row.get("text")).strip()
                ]
                constraints.append(
                    {
                        "constraint_concept_id": constraint_concept_id,
                        "parameter_concept_id": parameter_concept_id,
                        "parameter": _normalise_parameter_name(parameter_concept_id),
                        "action": _first_batched_text(
                            rows_by_concept,
                            constraint_concept_id,
                            PRED_HAS_PARAMETER_ACTION,
                        ),
                        "fixed_value": _first_batched_text(
                            rows_by_concept,
                            constraint_concept_id,
                            PRED_HAS_FIXED_PARAMETER_VALUE,
                        ),
                        "allowed_values": allowed_values,
                        "profile_concept_id": profile_concept_id,
                    }
                )
            api_profiles.append(
                {
                    "profile_concept_id": profile_concept_id,
                    "api_surface": _first_batched_text(
                        rows_by_concept, profile_concept_id, PRED_HAS_API_SURFACE
                    ),
                    "structured_tool_calling": _first_batched_text(
                        rows_by_concept,
                        profile_concept_id,
                        PRED_HAS_STRUCTURED_TOOL_CALLING,
                    ),
                    "tool_continuation_mode": _first_batched_text(
                        rows_by_concept,
                        profile_concept_id,
                        PRED_HAS_TOOL_CONTINUATION_MODE,
                    ),
                    "response_storage_policy": _first_batched_text(
                        rows_by_concept,
                        profile_concept_id,
                        PRED_HAS_RESPONSE_STORAGE_POLICY,
                    ),
                    "connection_id": _first_batched_text(
                        rows_by_concept,
                        profile_concept_id,
                        PRED_HAS_PROVIDER_CONNECTION_ID,
                    ),
                    "deployment_id": _first_batched_text(
                        rows_by_concept,
                        profile_concept_id,
                        PRED_HAS_DEPLOYMENT_ID,
                    ),
                    "parameter_constraints": constraints,
                }
            )

        entry: dict[str, Any] = {
            "model_id": model_id,
            "model_aliases": aliases,
            "provider": provider,
            "locality": "local" if provider == "ollama" else "external",
            "concept_id": model_concept_id,
            "registry_entry_id": registry_entry_id,
            "api_profiles": api_profiles,
        }
        pricing = _parse_registry_json(
            _first_batched_text(
                rows_by_concept, registry_entry_id, PRED_HAS_MODEL_PRICING_JSON
            )
            or ""
        )
        capabilities = _parse_registry_json(
            _first_batched_text(
                rows_by_concept,
                registry_entry_id,
                PRED_HAS_MODEL_CAPABILITIES_JSON,
            )
            or ""
        )
        if pricing is not None:
            entry["pricing"] = dict(pricing)
        if capabilities is not None:
            entry["capabilities"] = dict(capabilities)
        models.append(entry)

    if not models:
        return None
    return {
        "registry_concept_id": _DEFAULT_REGISTRY_CONCEPT_ID,
        "models": models,
        "read_strategy": "batched_graph",
        "read_phases": 6,
        "canonical_read_batches": {"concepts": 5, "text_assertions": 1},
        "loader_schema_version": _MODEL_REGISTRY_GRAPH_LOADER_SCHEMA_VERSION,
    }


def _strip_provider_prefix(model_id: str, provider: str | None = None) -> str:
    cleaned = _normalise_lookup_token(model_id)
    if not cleaned:
        return ""

    provider_prefixes = (
        [provider]
        if isinstance(provider, str) and provider.strip()
        else KNOWN_PROVIDER_PREFIXES
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
        model_id = _derive_model_id_from_aliases(
            aliases=model_aliases, provider=provider
        )

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
        parameter_constraints = [
            {**dict(constraint), "profile_concept_id": profile_concept_id}
            for constraint in parameter_constraints
        ]
        api_profiles.append(
            {
                "profile_concept_id": profile_concept_id,
                "api_surface": _get_first_text(
                    profile_concept_id, predicate=PRED_HAS_API_SURFACE
                ),
                "structured_tool_calling": _get_first_text(
                    profile_concept_id,
                    predicate=PRED_HAS_STRUCTURED_TOOL_CALLING,
                ),
                "tool_continuation_mode": _get_first_text(
                    profile_concept_id,
                    predicate=PRED_HAS_TOOL_CONTINUATION_MODE,
                ),
                "response_storage_policy": _get_first_text(
                    profile_concept_id,
                    predicate=PRED_HAS_RESPONSE_STORAGE_POLICY,
                ),
                "connection_id": _get_first_text(
                    profile_concept_id,
                    predicate=PRED_HAS_PROVIDER_CONNECTION_ID,
                ),
                "deployment_id": _get_first_text(
                    profile_concept_id,
                    predicate=PRED_HAS_DEPLOYMENT_ID,
                ),
                "parameter_constraints": parameter_constraints,
            }
        )

    pricing = _parse_registry_json(
        _get_first_text(
            registry_entry_id,
            predicate=PRED_HAS_MODEL_PRICING_JSON,
        )
        or ""
    )
    capabilities = _parse_registry_json(
        _get_first_text(
            registry_entry_id,
            predicate=PRED_HAS_MODEL_CAPABILITIES_JSON,
        )
        or ""
    )
    entry = {
        "model_id": model_id,
        "model_aliases": model_aliases,
        "provider": provider,
        "locality": "local" if provider == "ollama" else "external",
        "concept_id": model_concept_id,
        "registry_entry_id": registry_entry_id,
        "api_profiles": api_profiles,
    }
    if pricing is not None:
        entry["pricing"] = dict(pricing)
    if capabilities is not None:
        entry["capabilities"] = dict(capabilities)
    return entry


def _load_registry_from_vontology_graph_legacy(
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


def _load_registry_from_vontology_graph(
    *, preferred_language: str | None = None
) -> Optional[Mapping[str, Any]]:
    """Use the bounded batch loader with compatibility fallback."""

    registry = _load_registry_from_vontology_graph_batched(
        preferred_language=preferred_language
    )
    if registry is not None:
        return registry
    return _load_registry_from_vontology_graph_legacy(
        preferred_language=preferred_language
    )


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
                    "scope": active.get("scope")
                    if isinstance(active, Mapping)
                    else None,
                }
            )

    return {
        "source": "settings",
        "models": models,
    }


def _usable_registry_memory_entry(
    *,
    cache_key: str,
    authority_fingerprint: str,
    now: float,
) -> Mapping[str, Any] | None:
    with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
        cached = _MODEL_REGISTRY_SNAPSHOT_CACHE.get(cache_key)
        if not isinstance(cached, Mapping):
            return None
        if cached.get("authority_fingerprint") != authority_fingerprint:
            return None
        refresh_after = cached.get("refresh_after")
        stale_until = cached.get("stale_until")
        snapshot = cached.get("snapshot")
        if (
            not isinstance(refresh_after, (int, float))
            or not isinstance(stale_until, (int, float))
            or not _represented_registry_snapshot(snapshot)
        ):
            _MODEL_REGISTRY_SNAPSHOT_CACHE.pop(cache_key, None)
            return None
        effective_stale_until = min(
            float(stale_until),
            float(refresh_after) + _registry_snapshot_max_stale_seconds(),
        )
        if effective_stale_until <= now:
            _MODEL_REGISTRY_SNAPSHOT_CACHE.pop(cache_key, None)
            return None
        return {
            **dict(cached),
            "stale_until": effective_stale_until,
        }


def _hydrate_represented_registry_snapshot(
    *, preferred_language: str | None = None
) -> tuple[Mapping[str, Any] | None, int]:
    started_at = time.time()
    try:
        registry = _load_registry_from_vontology_graph(
            preferred_language=preferred_language
        )
    except Exception as exc:
        logger.warning("Model registry graph hydration failed: %s", exc)
        registry = None
    if registry is not None:
        return (
            {
                "source": "vontology_graph",
                **registry,
            },
            int((time.time() - started_at) * 1000),
        )

    try:
        registry = _load_registry_from_vontology_json(
            preferred_language=preferred_language
        )
    except Exception as exc:
        logger.warning("Model registry JSON hydration failed: %s", exc)
        registry = None
    if registry is not None:
        return (
            {
                "source": "vontology_json",
                **registry,
            },
            int((time.time() - started_at) * 1000),
        )
    return None, int((time.time() - started_at) * 1000)


def _commit_represented_registry_snapshot(
    *,
    cache_key: str,
    preferred_language: str | None,
    snapshot: Mapping[str, Any],
    authority_fingerprint: str,
    hydrate_duration_ms: int | None,
) -> bool:
    if not _represented_registry_snapshot(snapshot):
        return False
    if _model_registry_authority_fingerprint() != authority_fingerprint:
        return False
    created_at = time.time()
    refresh_after = created_at + _registry_snapshot_refresh_seconds()
    stale_until = refresh_after + _registry_snapshot_max_stale_seconds()
    _store_registry_snapshot_in_memory(
        cache_key=cache_key,
        snapshot=snapshot,
        authority_fingerprint=authority_fingerprint,
        created_at=created_at,
        refresh_after=refresh_after,
        stale_until=stale_until,
    )
    _persist_registry_snapshot_to_disk(
        cache_key=cache_key,
        preferred_language=preferred_language,
        snapshot=snapshot,
        authority_fingerprint=authority_fingerprint,
        created_at=created_at,
        refresh_after=refresh_after,
        stale_until=stale_until,
        hydrate_duration_ms=hydrate_duration_ms,
    )
    refresh_token = _registry_snapshot_refresh_token(
        cache_key=cache_key,
        authority_fingerprint=authority_fingerprint,
    )
    with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
        _MODEL_REGISTRY_SNAPSHOT_REFRESH_STATE[refresh_token] = {
            "last_refresh_succeeded": True,
            "last_refresh_completed_at": created_at,
        }
    return True


def _refresh_represented_registry_snapshot(
    *,
    cache_key: str,
    preferred_language: str | None,
    authority_fingerprint: str,
) -> None:
    refresh_token = _registry_snapshot_refresh_token(
        cache_key=cache_key,
        authority_fingerprint=authority_fingerprint,
    )
    try:
        snapshot, hydrate_duration_ms = _hydrate_represented_registry_snapshot(
            preferred_language=preferred_language
        )
        if snapshot is None:
            with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
                _MODEL_REGISTRY_SNAPSHOT_REFRESH_STATE[refresh_token] = {
                    "last_refresh_succeeded": False,
                    "last_refresh_completed_at": time.time(),
                    "failure_reason": "represented_registry_unavailable",
                }
            return
        committed = _commit_represented_registry_snapshot(
            cache_key=cache_key,
            preferred_language=preferred_language,
            snapshot=snapshot,
            authority_fingerprint=authority_fingerprint,
            hydrate_duration_ms=hydrate_duration_ms,
        )
        if not committed:
            with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
                _MODEL_REGISTRY_SNAPSHOT_REFRESH_STATE[refresh_token] = {
                    "last_refresh_succeeded": False,
                    "last_refresh_completed_at": time.time(),
                    "failure_reason": "authority_changed_during_refresh",
                }
    except Exception:
        logger.exception("Model registry background refresh failed")
        with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
            _MODEL_REGISTRY_SNAPSHOT_REFRESH_STATE[refresh_token] = {
                "last_refresh_succeeded": False,
                "last_refresh_completed_at": time.time(),
                "failure_reason": "refresh_exception",
            }
    finally:
        with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
            _MODEL_REGISTRY_SNAPSHOT_REFRESH_INFLIGHT.discard(refresh_token)


def _start_registry_snapshot_background_refresh(
    *,
    cache_key: str,
    preferred_language: str | None,
    authority_fingerprint: str,
) -> bool:
    refresh_token = _registry_snapshot_refresh_token(
        cache_key=cache_key,
        authority_fingerprint=authority_fingerprint,
    )
    with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
        if refresh_token in _MODEL_REGISTRY_SNAPSHOT_REFRESH_INFLIGHT:
            return False
        _MODEL_REGISTRY_SNAPSHOT_REFRESH_INFLIGHT.add(refresh_token)
    try:
        threading.Thread(
            target=_refresh_represented_registry_snapshot,
            kwargs={
                "cache_key": cache_key,
                "preferred_language": preferred_language,
                "authority_fingerprint": authority_fingerprint,
            },
            name="model-registry-refresh",
            daemon=True,
        ).start()
        return True
    except Exception:
        with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
            _MODEL_REGISTRY_SNAPSHOT_REFRESH_INFLIGHT.discard(refresh_token)
        logger.exception("Could not start model registry background refresh")
        return False


def get_model_registry_snapshot(
    *, preferred_language: str | None = None
) -> Mapping[str, Any]:
    cache_key = _registry_snapshot_cache_key(preferred_language)
    for attempt in range(2):
        authority_fingerprint = _model_registry_authority_fingerprint()
        now = time.time()
        cached = _usable_registry_memory_entry(
            cache_key=cache_key,
            authority_fingerprint=authority_fingerprint,
            now=now,
        )
        if cached is not None:
            refresh_after = cached.get("refresh_after")
            if isinstance(refresh_after, (int, float)) and refresh_after <= now:
                _start_registry_snapshot_background_refresh(
                    cache_key=cache_key,
                    preferred_language=preferred_language,
                    authority_fingerprint=authority_fingerprint,
                )
            return cached["snapshot"]

        cached = _load_registry_snapshot_from_disk(
            cache_key=cache_key,
            authority_fingerprint=authority_fingerprint,
            now=now,
        )
        if cached is not None:
            _store_registry_snapshot_in_memory(
                cache_key=cache_key,
                snapshot=cached["snapshot"],
                authority_fingerprint=authority_fingerprint,
                created_at=float(cached["created_at"]),
                refresh_after=float(cached["refresh_after"]),
                stale_until=float(cached["stale_until"]),
            )
            refresh_after = cached.get("refresh_after")
            if isinstance(refresh_after, (int, float)) and refresh_after <= now:
                _start_registry_snapshot_background_refresh(
                    cache_key=cache_key,
                    preferred_language=preferred_language,
                    authority_fingerprint=authority_fingerprint,
                )
            return cached["snapshot"]

        snapshot, hydrate_duration_ms = _hydrate_represented_registry_snapshot(
            preferred_language=preferred_language
        )
        if snapshot is None:
            break
        committed = _commit_represented_registry_snapshot(
            cache_key=cache_key,
            preferred_language=preferred_language,
            snapshot=snapshot,
            authority_fingerprint=authority_fingerprint,
            hydrate_duration_ms=hydrate_duration_ms,
        )
        if committed:
            return snapshot
        if attempt == 0:
            continue
        break

    # Settings remain an availability fallback, never a durable or stale
    # representation of model API-profile authority. In particular, a
    # represented snapshot rejected after both bounded authority-stable
    # hydration attempts is not returned to the caller.
    return _build_registry_from_settings()


def get_model_registry_snapshot_status(
    *, preferred_language: str | None = None
) -> Mapping[str, Any]:
    """Return a secret-free, non-hydrating registry readiness projection."""

    cache_key = _registry_snapshot_cache_key(preferred_language)
    authority_fingerprint = _model_registry_authority_fingerprint()
    now = time.time()
    cached = _usable_registry_memory_entry(
        cache_key=cache_key,
        authority_fingerprint=authority_fingerprint,
        now=now,
    )
    refresh_token = _registry_snapshot_refresh_token(
        cache_key=cache_key,
        authority_fingerprint=authority_fingerprint,
    )
    with _MODEL_REGISTRY_SNAPSHOT_CACHE_LOCK:
        refresh_in_progress = refresh_token in _MODEL_REGISTRY_SNAPSHOT_REFRESH_INFLIGHT
        refresh_state = dict(
            _MODEL_REGISTRY_SNAPSHOT_REFRESH_STATE.get(refresh_token) or {}
        )
    if cached is None:
        return {
            "schema_version": "model_registry_snapshot_status.v1",
            "ready": False,
            "source": None,
            "cache_state": "unavailable",
            "age_seconds": None,
            "refresh_in_progress": refresh_in_progress,
            "last_refresh_succeeded": refresh_state.get("last_refresh_succeeded"),
        }

    created_at = float(cached["created_at"])
    refresh_after = float(cached["refresh_after"])
    stale = refresh_after <= now
    return {
        "schema_version": "model_registry_snapshot_status.v1",
        "ready": True,
        "source": cached["snapshot"].get("source"),
        "cache_state": (
            "stale_refreshing"
            if stale and refresh_in_progress
            else ("stale" if stale else "fresh")
        ),
        "age_seconds": round(max(0.0, now - created_at), 3),
        "refresh_in_progress": refresh_in_progress,
        "last_refresh_succeeded": refresh_state.get("last_refresh_succeeded", True),
    }


def preload_model_registry_snapshot(
    *, preferred_language: str | None = None
) -> Mapping[str, Any]:
    """Load represented profile authority before the HTTP server accepts calls."""

    started_at = time.time()
    snapshot = get_model_registry_snapshot(preferred_language=preferred_language)
    status = get_model_registry_snapshot_status(preferred_language=preferred_language)
    return {
        **status,
        "read_strategy": snapshot.get("read_strategy"),
        "read_phases": snapshot.get("read_phases"),
        "loader_schema_version": snapshot.get("loader_schema_version"),
        "model_count": len(snapshot.get("models") or ()),
        "preload_duration_ms": int((time.time() - started_at) * 1000),
    }


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


def _entry_model_match_score(
    entry: Mapping[str, Any],
    *,
    model: str,
    provider: str | None = None,
) -> tuple[int, int]:
    """Prefer exact model/deployment IDs, then the longest family match."""

    if not _entry_matches_model(entry, model=model, provider=provider):
        return (-1, -1)
    provider_key = _normalise_provider_name(provider)
    requested = _normalise_lookup_token(model)
    bare_requested = _normalise_lookup_token(
        _strip_provider_prefix(requested, provider_key or None)
    )
    candidates = {
        _normalise_lookup_token(entry.get("model_id")),
    }
    aliases = entry.get("model_aliases")
    if isinstance(aliases, Sequence) and not isinstance(aliases, str):
        candidates.update(
            _normalise_lookup_token(alias)
            for alias in aliases
            if isinstance(alias, str)
        )
    entry_provider = _normalise_provider_name(entry.get("provider"))
    candidates.update(
        _normalise_lookup_token(
            _strip_provider_prefix(candidate, entry_provider or provider_key or None)
        )
        for candidate in tuple(candidates)
        if candidate
    )
    candidates.discard("")
    exact_lengths = [
        len(candidate)
        for candidate in candidates
        if candidate in {requested, bare_requested}
    ]
    if exact_lengths:
        return (2, max(exact_lengths))
    prefix_lengths = [
        len(candidate)
        for candidate in candidates
        if requested.startswith(f"{candidate}-")
        or requested.startswith(f"{candidate}.")
        or bare_requested.startswith(f"{candidate}-")
        or bare_requested.startswith(f"{candidate}.")
    ]
    if prefix_lengths:
        return (1, max(prefix_lengths))
    return (0, 0)


def _iter_parameter_constraints_for_entry(
    entry: Mapping[str, Any],
    *,
    api_surface: str | None = None,
    profile_concept_id: str | None = None,
) -> list[Mapping[str, Any]]:
    profiles = entry.get("api_profiles")
    if not isinstance(profiles, Sequence) or isinstance(profiles, str):
        return []

    requested_surface = _normalise_lookup_token(api_surface)
    requested_profile_id = (
        profile_concept_id.strip()
        if isinstance(profile_concept_id, str) and profile_concept_id.strip()
        else None
    )
    matching_constraints: list[Mapping[str, Any]] = []
    generic_constraints: list[Mapping[str, Any]] = []

    for profile in profiles:
        if not isinstance(profile, Mapping):
            continue
        if requested_profile_id is not None and (
            str(profile.get("profile_concept_id") or "").strip() != requested_profile_id
        ):
            continue
        constraints = profile.get("parameter_constraints")
        if not isinstance(constraints, Sequence) or isinstance(constraints, str):
            continue
        projected_constraints = [
            {
                **constraint,
                "profile_concept_id": (
                    constraint.get("profile_concept_id")
                    or profile.get("profile_concept_id")
                ),
            }
            for constraint in constraints
            if isinstance(constraint, Mapping)
        ]

        profile_surface = _normalise_lookup_token(profile.get("api_surface"))
        if not requested_surface:
            matching_constraints.extend(projected_constraints)
            continue

        if not profile_surface:
            generic_constraints.extend(projected_constraints)
            continue

        if profile_surface == requested_surface:
            matching_constraints.extend(projected_constraints)

    if requested_profile_id is not None:
        return matching_constraints
    return matching_constraints or generic_constraints


def resolve_model_parameter_policy(
    *,
    model: str,
    parameter: str,
    provider: str | None = None,
    api_surface: str | None = None,
    profile_concept_id: str | None = None,
    preferred_language: str | None = None,
) -> Mapping[str, Any] | None:
    registry_snapshot = get_model_registry_snapshot(
        preferred_language=preferred_language
    )
    models = registry_snapshot.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str):
        return None

    requested_parameter = _normalise_parameter_name(parameter)
    if not requested_parameter:
        return None

    matching_entries = [
        entry
        for entry in models
        if isinstance(entry, Mapping)
        and _entry_matches_model(entry, model=model, provider=provider)
    ]
    if not matching_entries:
        return None
    entry = max(
        matching_entries,
        key=lambda candidate: _entry_model_match_score(
            candidate,
            model=model,
            provider=provider,
        ),
    )

    for constraint in _iter_parameter_constraints_for_entry(
        entry,
        api_surface=api_surface,
        profile_concept_id=profile_concept_id,
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


def resolve_model_api_profiles(
    *,
    model: str,
    provider: str | None = None,
    preferred_language: str | None = None,
) -> Mapping[str, Any] | None:
    """Resolve represented API profiles for a concrete model/deployment.

    This is a provenance-preserving registry read.  It deliberately does not
    choose an API surface: transport selection belongs to the caller that also
    knows whether tools are present and which client/connection is in use.
    """

    registry_snapshot = get_model_registry_snapshot(
        preferred_language=preferred_language
    )
    models = registry_snapshot.get("models")
    if not isinstance(models, Sequence) or isinstance(models, str):
        return None

    matching_entries = [
        entry
        for entry in models
        if isinstance(entry, Mapping)
        and _entry_matches_model(entry, model=model, provider=provider)
    ]
    if not matching_entries:
        return None
    entry = max(
        matching_entries,
        key=lambda candidate: _entry_model_match_score(
            candidate,
            model=model,
            provider=provider,
        ),
    )

    raw_profiles = entry.get("api_profiles")
    profiles = (
        [
            dict(profile)
            for profile in raw_profiles or []
            if isinstance(profile, Mapping)
        ]
        if isinstance(raw_profiles, Sequence) and not isinstance(raw_profiles, str)
        else []
    )
    return {
        "model_id": entry.get("model_id"),
        "model_aliases": list(entry.get("model_aliases") or []),
        "provider": entry.get("provider"),
        "concept_id": entry.get("concept_id"),
        "registry_entry_id": entry.get("registry_entry_id"),
        "api_profiles": profiles,
        "source": registry_snapshot.get("source"),
        "registry_concept_id": registry_snapshot.get("registry_concept_id"),
    }


def _coerce_fixed_parameter_value(fixed_value: Any, original_value: Any) -> Any:
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
    profile_concept_id: str | None = None,
    preferred_language: str | None = None,
) -> Any:
    if value is None:
        return None

    policy = resolve_model_parameter_policy(
        model=model,
        parameter=parameter,
        provider=provider,
        api_surface=api_surface,
        profile_concept_id=profile_concept_id,
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

    usable_entries = [entry for entry in evidence_entries if isinstance(entry, Mapping)]
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
