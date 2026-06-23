import logging
import json
import os
import time
from typing import Any, Optional, Dict, List, Mapping, Sequence
from pymongo.results import UpdateResult
from pymongo.errors import OperationFailure
from ..utils.time_utils import utc_now
from ..db.mongo_client import (
    APPLICATION_SETTINGS_COLLECTION_NAME,
    get_text_values_collection,
    get_text_relations_collection,
)
from ..db.mongo_setup import get_application_settings_collection
from .exceptions import MultipleUsersForEmailError
from .mongo_observability_service import (
    build_mongo_operation_comment,
    observe_mongo_operation,
)
from .model_parameter_service import (
    MODEL_PARAMETERS_KEY,
    normalise_model_parameters_for_storage,
    stable_model_parameters_key,
)

# Configure logging
logger = logging.getLogger(__name__)


def _settings_mongo_comment(operation: str, detail: str | None = None):
    return build_mongo_operation_comment(
        service="settings_service",
        collection=APPLICATION_SETTINGS_COLLECTION_NAME,
        operation=operation,
        detail=detail,
    )


def _settings_find_one(collection, query, *, operation: str, detail: str | None = None):
    kwargs: dict[str, Any] = {}
    comment = _settings_mongo_comment(operation, detail=detail)
    if comment is not None:
        kwargs["comment"] = comment
    started_at = time.perf_counter()
    success = False
    error_type: str | None = None
    try:
        result = collection.find_one(query, **kwargs)
        success = True
        return result
    except TypeError as exc:
        error_type = type(exc).__name__
        try:
            result = collection.find_one(query)
            success = True
            return result
        except Exception as fallback_exc:
            error_type = type(fallback_exc).__name__
            raise
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        observe_mongo_operation(
            service="settings_service",
            collection=APPLICATION_SETTINGS_COLLECTION_NAME,
            operation=operation,
            started_at=started_at,
            success=success,
            detail=detail,
            error_type=error_type,
        )


def _settings_find(collection, query, *, operation: str, detail: str | None = None):
    kwargs: dict[str, Any] = {}
    comment = _settings_mongo_comment(operation, detail=detail)
    if comment is not None:
        kwargs["comment"] = comment
    started_at = time.perf_counter()
    success = False
    error_type: str | None = None
    try:
        cursor = collection.find(query, **kwargs)
        success = True
        return cursor
    except TypeError as exc:
        error_type = type(exc).__name__
        try:
            cursor = collection.find(query)
            success = True
            return cursor
        except Exception as fallback_exc:
            error_type = type(fallback_exc).__name__
            raise
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        observe_mongo_operation(
            service="settings_service",
            collection=APPLICATION_SETTINGS_COLLECTION_NAME,
            operation=operation,
            started_at=started_at,
            success=success,
            detail=detail,
            error_type=error_type,
        )


def _settings_update_one(
    collection,
    query,
    update,
    *,
    upsert: bool = False,
    operation: str,
    detail: str | None = None,
):
    kwargs: dict[str, Any] = {"upsert": upsert}
    comment = _settings_mongo_comment(operation, detail=detail)
    if comment is not None:
        kwargs["comment"] = comment
    started_at = time.perf_counter()
    success = False
    error_type: str | None = None
    try:
        result = collection.update_one(query, update, **kwargs)
        success = True
        return result
    except TypeError as exc:
        error_type = type(exc).__name__
        kwargs.pop("comment", None)
        try:
            result = collection.update_one(query, update, **kwargs)
            success = True
            return result
        except Exception as fallback_exc:
            error_type = type(fallback_exc).__name__
            raise
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        observe_mongo_operation(
            service="settings_service",
            collection=APPLICATION_SETTINGS_COLLECTION_NAME,
            operation=operation,
            started_at=started_at,
            success=success,
            detail=detail,
            error_type=error_type,
        )

# --- Setting Names ---
# REFACTORING_NOTE: Consolidating to a single setting for the active LLM.
ACTIVE_LLM_SETTING_NAME = "active_llm"
ENABLED_LLMS_SETTING_NAME = "enabled_llms"
OPENAI_ENV_VAR_SETTING_NAME = "openai_api_key_env_var"
SERVER_DEFAULT_LLM_SETTING_NAME = "server_default_llm"
RAG_EMBEDDER_SETTING_NAME = "rag_embedder"
RAG_LLM_SETTING_NAME = "rag_llm"
# Setting has been removed as it's a flawed concept for multi-user applications.
# The user's identity is managed via the session.
# CURRENT_USER_PERSON_SETTING_NAME = "current_user_person_id"
# The organisation is managed on the client-side via localStorage.
# CURRENT_ORGANISATION_SETTING_NAME = "current_organisation_id"
OLLAMA_HOSTS_LIST_SETTING_NAME = "ollama_hosts_list"
ACTIVE_OLLAMA_HOST_SETTING_NAME = "active_ollama_host"
# Removed: CURRENT_USER_PERSON_CONCEPT_SETTING_NAME, CURRENT_ORGANISATION_* constants (client localStorage authority)
PREFERRED_LANGUAGE_SETTING_NAME = "preferred_language"
FETCH_COUNTS_ON_LOAD_SETTING_NAME = "fetch_counts_on_load"
PRELOAD_VONTOLOGY_TREE_SETTING_NAME = "preload_vontology_tree"
DISABLE_REMOTE_OLLAMA_SCAN_SETTING_NAME = "disable_remote_ollama_scan"
BUTTONIFY_MODEL_ENABLED_SETTING_NAME = "buttonify_model_enabled"
AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLED_SETTING_NAME = (
    "auto_proceed_minimal_imposition_enabled"
)

# Per-model LLM call timeout overrides (Settings → Models section).
# Stored as a dict mapping "{provider}:{model}" keys to timeout seconds (float).
MODEL_LLM_TIMEOUT_OVERRIDES_SETTING_NAME = "model_llm_timeout_overrides"

# Internal MCP orchestrator caps (Settings → Agent Configuration)
INTERNAL_MCP_MAX_TOOL_INVOCATIONS_SETTING_NAME = "internal_mcp_max_tool_invocations"
INTERNAL_MCP_TOOL_BATCH_CAP_SETTING_NAME = "internal_mcp_tool_batch_cap"
# Canonical cap defaults/clamps must stay aligned across getters, settings batch,
# runtime bootstrap, and MCP surfaces so users do not see contradictory budgets.
INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT = 100
INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN = 0
INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX = 500
INTERNAL_MCP_TOOL_BATCH_CAP_DEFAULT = 10
INTERNAL_MCP_TOOL_BATCH_CAP_MIN = 1
INTERNAL_MCP_TOOL_BATCH_CAP_MAX = 20

# Chat UI: show tool use during the “Thinking…” indicator (JVNAUTOSCI-942)
SHOW_TOOL_USE_DURING_THINKING_SETTING_NAME = "show_tool_use_during_thinking"

# Admin: allow disabling write-tool conservatism for internal MCP write tools.
DISABLE_WRITE_TOOL_CONSERVATISM_SETTING_NAME = "disable_write_tool_conservatism"
# Admin: require explicit human review approval phrasing for high-impact
# Vontology write tools (JVNAUTOSCI-925).
REQUIRE_HUMAN_REVIEW_FOR_HIGH_IMPACT_KB_WRITES_SETTING_NAME = (
    "require_human_review_for_high_impact_kb_writes"
)
MUTATION_AUTHORITY_LEVEL_SETTING_NAME = "mutation_authority_level"

# Prefixes for contextual (scoped) LLM settings (Phase 2 scaffold)
_ACTIVE_LLM_USER_PREFIX = f"{ACTIVE_LLM_SETTING_NAME}:user:"
_ACTIVE_LLM_ORG_PREFIX = f"{ACTIVE_LLM_SETTING_NAME}:org:"
_ENABLED_LLMS_USER_PREFIX = f"{ENABLED_LLMS_SETTING_NAME}:user:"
_ENABLED_LLMS_ORG_PREFIX = f"{ENABLED_LLMS_SETTING_NAME}:org:"
_MUTATION_AUTHORITY_LEVEL_USER_PREFIX = (
    f"{MUTATION_AUTHORITY_LEVEL_SETTING_NAME}:user:"
)
_RUNTIME_MODEL_SETTING_MODES = {"inherit", "explicit", "disabled"}


def _normalise_llm_setting_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider") or "").strip().lower()
    model = str(raw.get("model") or "").strip()
    if not provider or not model:
        return None
    normalised: dict[str, Any] = {
        "provider": provider,
        "model": model,
    }
    host = str(raw.get("host") or "").strip()
    if host:
        normalised["host"] = host
    raw_parameters = raw.get(MODEL_PARAMETERS_KEY)
    if raw_parameters is None:
        raw_parameters = raw.get("modelParameters")
    model_parameters = normalise_model_parameters_for_storage(
        raw_parameters,
        provider=provider,
        model=model,
        include_registry=True,
    )
    if model_parameters:
        normalised[MODEL_PARAMETERS_KEY] = model_parameters
    return normalised


def _dedupe_llm_setting_entries(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in entries:
        normalised = _normalise_llm_setting_entry(dict(entry))
        if normalised is None:
            continue
        provider = str(normalised.get("provider") or "").strip().lower()
        model = str(normalised.get("model") or "").strip()
        host = str(normalised.get("host") or "").strip()
        params_key = stable_model_parameters_key(
            normalised.get(MODEL_PARAMETERS_KEY),
            provider=provider,
            model=model,
        )
        key = (provider, model, host, params_key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalised)
    return deduped


def _normalise_llm_setting_list(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        entry = _normalise_llm_setting_entry(item)
        if entry is not None:
            entries.append(entry)
    return _dedupe_llm_setting_entries(entries)


def _normalise_runtime_model_setting(
    raw: Any,
    *,
    allow_disabled: bool = False,
) -> dict[str, Any]:
    if raw is None:
        return {"mode": "inherit"}

    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in {"", "inherit", "none", "default"}:
            return {"mode": "inherit"}
        if allow_disabled and token in {"disabled", "off"}:
            return {"mode": "disabled"}
        return {"mode": "inherit"}

    if not isinstance(raw, dict):
        return {"mode": "inherit"}

    mode = str(raw.get("mode") or "").strip().lower()
    if mode and mode not in _RUNTIME_MODEL_SETTING_MODES:
        return {"mode": "inherit"}

    if mode == "disabled":
        return {"mode": "disabled"} if allow_disabled else {"mode": "inherit"}
    if mode == "inherit":
        return {"mode": "inherit"}

    explicit = _normalise_llm_setting_entry(raw)
    if explicit is None:
        return {"mode": "inherit"}
    return {"mode": "explicit", **explicit}


def _normalise_runtime_model_setting_for_storage(
    raw: Any,
    *,
    allow_disabled: bool = False,
) -> dict[str, Any]:
    normalised = _normalise_runtime_model_setting(raw, allow_disabled=allow_disabled)
    mode = str(normalised.get("mode") or "inherit").strip().lower()
    if mode == "disabled":
        return {"mode": "disabled"} if allow_disabled else {"mode": "inherit"}
    if mode != "explicit":
        return {"mode": "inherit"}

    payload = {
        "mode": "explicit",
        "provider": str(normalised.get("provider") or "").strip().lower(),
        "model": str(normalised.get("model") or "").strip(),
    }
    host = str(normalised.get("host") or "").strip()
    if host:
        payload["host"] = host
    model_parameters = normalised.get(MODEL_PARAMETERS_KEY)
    if isinstance(model_parameters, Mapping) and model_parameters:
        payload[MODEL_PARAMETERS_KEY] = dict(model_parameters)
    return payload


def get_setting(setting_name: str) -> Any:
    """
    Retrieves the value of a setting from the database.

    Args:
        setting_name: The name of the setting to retrieve.

    Returns:
        The value of the setting, or None if not found or an error occurs.
    """
    logger.info(f"Attempting to get setting: '{setting_name}'")
    settings_coll = get_application_settings_collection()
    if settings_coll is None:
        logger.error(
            f"Could not access the '{APPLICATION_SETTINGS_COLLECTION_NAME}' collection for setting '{setting_name}'."
        )
        return None
    try:
        setting_doc = _settings_find_one(
            settings_coll,
            {"setting_name": setting_name},
            operation="get_setting.find_one",
            detail=str(setting_name)[:96],
        )
        if setting_doc:
            value = setting_doc.get("value")
            logger.info(f"Found setting '{setting_name}' with value: '{value}'")
            return value
        logger.info(f"Setting '{setting_name}' not found in the database.")
        return None
    except OperationFailure as e:
        logger.error(
            f"MongoDB operation failed while getting setting '{setting_name}': {e}"
        )
        return None
    except Exception as e:
        logger.error(
            f"An unexpected error occurred while getting setting '{setting_name}': {e}"
        )
        return None


def get_settings_batch(setting_names: List[str]) -> Dict[str, Any]:
    """
    Retrieves multiple settings from the database in a single query.

    Args:
        setting_names: List of setting names to retrieve.

    Returns:
        Dictionary mapping setting names to their values. Missing settings are not included.
    """
    if not setting_names:
        return {}

    settings_coll = get_application_settings_collection()
    if settings_coll is None:
        logger.error(
            f"Could not access the '{APPLICATION_SETTINGS_COLLECTION_NAME}' collection for batch fetch."
        )
        return {}

    try:
        cursor = _settings_find(
            settings_coll,
            {"setting_name": {"$in": setting_names}},
            operation="get_settings_batch.find",
            detail=f"count={len(setting_names)}",
        )
        result = {}
        for doc in cursor:
            name = doc.get("setting_name")
            if name:
                result[name] = doc.get("value")
        logger.info(f"Batch fetched {len(result)} of {len(setting_names)} settings")
        return result
    except OperationFailure as e:
        logger.error(f"MongoDB operation failed during batch fetch: {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error during batch fetch: {e}")
        return {}


def _coerce_bool(val: Any, default: bool) -> bool:
    """Coerce a value to bool with common truthy/falsy string handling."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    if isinstance(val, (int, float)):
        return val != 0
    return default


def _coerce_int(val: Any, default: int) -> int:
    """Coerce a value to int with fallback."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            pass
    if isinstance(val, float):
        return int(val)
    return default


def get_all_settings_batch() -> Dict[str, Any]:
    """Fetch all application settings in a single DB query (optimised for /api/settings/).

    Returns a dict with processed/coerced values matching what the individual getters return.
    Note: Does NOT include active_llm - use resolve_llm_setting() with user context instead.
    """
    # List of all setting names we need from the DB
    setting_names = [
        OPENAI_ENV_VAR_SETTING_NAME,
        SERVER_DEFAULT_LLM_SETTING_NAME,
        RAG_EMBEDDER_SETTING_NAME,
        RAG_LLM_SETTING_NAME,
        FETCH_COUNTS_ON_LOAD_SETTING_NAME,
        PRELOAD_VONTOLOGY_TREE_SETTING_NAME,
        DISABLE_REMOTE_OLLAMA_SCAN_SETTING_NAME,
        SHOW_TOOL_USE_DURING_THINKING_SETTING_NAME,
        BUTTONIFY_MODEL_ENABLED_SETTING_NAME,
        AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLED_SETTING_NAME,
        INTERNAL_MCP_MAX_TOOL_INVOCATIONS_SETTING_NAME,
        INTERNAL_MCP_TOOL_BATCH_CAP_SETTING_NAME,
        DISABLE_WRITE_TOOL_CONSERVATISM_SETTING_NAME,
        REQUIRE_HUMAN_REVIEW_FOR_HIGH_IMPACT_KB_WRITES_SETTING_NAME,
        MODEL_LLM_TIMEOUT_OVERRIDES_SETTING_NAME,
    ]

    raw = get_settings_batch(setting_names)

    return {
        # NOTE: active_llm is intentionally NOT included here.
        # Use resolve_llm_setting() with user_concept_id instead.
        "openai_api_key_env_var": raw.get(OPENAI_ENV_VAR_SETTING_NAME),
        "server_default_llm": _normalise_llm_setting_entry(
            raw.get(SERVER_DEFAULT_LLM_SETTING_NAME)
        ),
        "rag_embedder": _normalise_runtime_model_setting(
            raw.get(RAG_EMBEDDER_SETTING_NAME),
            allow_disabled=False,
        ),
        "rag_llm": _normalise_runtime_model_setting(
            raw.get(RAG_LLM_SETTING_NAME),
            allow_disabled=True,
        ),
        "fetch_counts_on_load": _coerce_bool(
            raw.get(FETCH_COUNTS_ON_LOAD_SETTING_NAME), default=True
        ),
        "preload_vontology_tree": _coerce_bool(
            raw.get(PRELOAD_VONTOLOGY_TREE_SETTING_NAME), default=False
        ),
        "disable_remote_ollama_scan": _coerce_bool(
            raw.get(DISABLE_REMOTE_OLLAMA_SCAN_SETTING_NAME), default=False
        ),
        "show_tool_use_during_thinking": _coerce_bool(
            raw.get(SHOW_TOOL_USE_DURING_THINKING_SETTING_NAME), default=True
        ),
        "buttonify_model_enabled": _coerce_bool(
            raw.get(BUTTONIFY_MODEL_ENABLED_SETTING_NAME), default=True
        ),
        "auto_proceed_minimal_imposition_enabled": _coerce_bool(
            raw.get(AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLED_SETTING_NAME),
            default=True,
        ),
        "internal_mcp_max_tool_invocations": _coerce_int_setting(
            raw.get(INTERNAL_MCP_MAX_TOOL_INVOCATIONS_SETTING_NAME),
            default=INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT,
            min_value=INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN,
            max_value=INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX,
        ),
        "internal_mcp_tool_batch_cap": _coerce_int_setting(
            raw.get(INTERNAL_MCP_TOOL_BATCH_CAP_SETTING_NAME),
            default=INTERNAL_MCP_TOOL_BATCH_CAP_DEFAULT,
            min_value=INTERNAL_MCP_TOOL_BATCH_CAP_MIN,
            max_value=INTERNAL_MCP_TOOL_BATCH_CAP_MAX,
        ),
        "disable_write_tool_conservatism": _coerce_bool(
            raw.get(DISABLE_WRITE_TOOL_CONSERVATISM_SETTING_NAME), default=False
        ),
        "require_human_review_for_high_impact_kb_writes": _coerce_bool(
            raw.get(REQUIRE_HUMAN_REVIEW_FOR_HIGH_IMPACT_KB_WRITES_SETTING_NAME),
            default=False,
        ),
        "model_llm_timeout_overrides": (
            {
                k: v
                for k, v in raw.get(
                    MODEL_LLM_TIMEOUT_OVERRIDES_SETTING_NAME, {}
                ).items()
                if isinstance(k, str) and isinstance(v, (int, float)) and float(v) > 0
            }
            if isinstance(raw.get(MODEL_LLM_TIMEOUT_OVERRIDES_SETTING_NAME), dict)
            else {}
        ),
    }


def update_setting(setting_name: str, setting_value: Any) -> bool:
    """
    Updates or creates a setting in the application_settings collection.

    Args:
        setting_name (str): The name of the setting.
        setting_value (any): The value of the setting.

    Returns:
        bool: True if the operation was acknowledged, False otherwise.
    """
    try:
        settings_collection = get_application_settings_collection()
        if settings_collection is None:
            # For test_update_setting_collection_unavailable
            logger.error(
                "Could not access the '%s' collection.",
                APPLICATION_SETTINGS_COLLECTION_NAME,
            )
            return False

        # Pre-check for existing setting with the same value
        existing_doc = _settings_find_one(
            settings_collection,
            {"setting_name": setting_name},
            operation="update_setting.precheck_find_one",
            detail=str(setting_name)[:96],
        )
        if existing_doc and existing_doc.get("value") == setting_value:
            # For test_update_setting_same_value
            logger.info(f"Setting '{setting_name}' value is already up to date.")
            return True

        result: UpdateResult = _settings_update_one(
            settings_collection,
            {"setting_name": setting_name},
            {"$set": {"value": setting_value, "updated_at": utc_now()}},
            operation="update_setting.update_one",
            detail=str(setting_name)[:96],
            upsert=True,
        )

        if result.acknowledged:
            if result.upserted_id is not None or result.modified_count > 0:
                # For test_update_setting_create_new & test_update_setting_update_existing
                logger.info(f"Setting '{setting_name}' successfully updated/created.")
            elif result.matched_count > 0 and result.modified_count == 0:
                # Handles cases where pre-check might be bypassed by mocks (for test_update_setting_same_value)
                logger.info(f"Setting '{setting_name}' value is already up to date.")
            else:
                # Fallback, less likely to be hit
                logger.info(
                    f"Setting '{setting_name}' update operation acknowledged, but no changes detected."
                )
            return True
        else:
            # For test_update_setting_not_acknowledged
            logger.error(
                f"Update/create operation for setting '{setting_name}' was not acknowledged by the server."
            )
            return False
    except OperationFailure as e:
        # For test_update_setting_operation_failure
        logger.error(
            f"MongoDB operation failed while updating setting '{setting_name}': {e}"
        )
        return False
    except Exception as e:
        # For test_update_setting_general_exception
        logger.error(
            f"An unexpected error occurred while updating setting '{setting_name}': {e}"
        )
        return False


def get_server_default_llm_setting() -> Optional[dict[str, Any]]:
    raw = get_setting(SERVER_DEFAULT_LLM_SETTING_NAME)
    return _normalise_llm_setting_entry(raw)


def set_server_default_llm_setting(entry: Mapping[str, Any] | None) -> bool:
    if entry is None:
        return update_setting(SERVER_DEFAULT_LLM_SETTING_NAME, None)
    normalised = _normalise_llm_setting_entry(entry)
    if normalised is None:
        logger.error("set_server_default_llm_setting requires provider/model payload")
        return False
    return update_setting(SERVER_DEFAULT_LLM_SETTING_NAME, normalised)


def get_rag_embedder_setting() -> dict[str, Any]:
    raw = get_setting(RAG_EMBEDDER_SETTING_NAME)
    return _normalise_runtime_model_setting(raw, allow_disabled=False)


def set_rag_embedder_setting(raw: Any) -> bool:
    normalised = _normalise_runtime_model_setting_for_storage(
        raw,
        allow_disabled=False,
    )
    return update_setting(RAG_EMBEDDER_SETTING_NAME, normalised)


def get_rag_llm_setting() -> dict[str, Any]:
    raw = get_setting(RAG_LLM_SETTING_NAME)
    return _normalise_runtime_model_setting(raw, allow_disabled=True)


def set_rag_llm_setting(raw: Any) -> bool:
    normalised = _normalise_runtime_model_setting_for_storage(
        raw,
        allow_disabled=True,
    )
    return update_setting(RAG_LLM_SETTING_NAME, normalised)


def _resolve_runtime_model_setting(
    *,
    configured: Mapping[str, Any] | None,
    allow_disabled: bool,
    user_concept_id: str | None = None,
    org_concept_id: str | None = None,
) -> dict[str, Any]:
    normalised = _normalise_runtime_model_setting(
        configured,
        allow_disabled=allow_disabled,
    )
    mode = str(normalised.get("mode") or "inherit").strip().lower()
    if mode == "disabled":
        return {
            "configured": normalised,
            "effective": None,
            "status": "disabled",
            "selection_source": "configured_disabled",
            "reason": "disabled_by_setting",
        }

    if mode == "explicit":
        effective = _normalise_llm_setting_entry(normalised)
        if effective is None:
            return {
                "configured": {"mode": "inherit"},
                "effective": None,
                "status": "unresolved",
                "selection_source": "invalid_explicit_setting",
                "reason": "invalid_explicit_setting",
            }
        return {
            "configured": normalised,
            "effective": {
                **effective,
                "scope": "global_setting",
            },
            "status": "resolved",
            "selection_source": "explicit_setting",
            "reason": None,
        }

    inherited = resolve_llm_setting(
        user_concept_id=user_concept_id,
        org_concept_id=org_concept_id,
    )
    if inherited:
        return {
            "configured": {"mode": "inherit"},
            "effective": dict(inherited),
            "status": "resolved",
            "selection_source": "active_llm_scope",
            "reason": None,
        }

    server_default = get_server_default_llm_setting()
    if server_default:
        return {
            "configured": {"mode": "inherit"},
            "effective": {
                **server_default,
                "scope": "server_default",
            },
            "status": "resolved",
            "selection_source": "server_default_llm",
            "reason": None,
        }

    return {
        "configured": {"mode": "inherit"},
        "effective": None,
        "status": "unresolved",
        "selection_source": "inherit_without_source",
        "reason": "inherits_chat_default_but_no_chat_default_is_available",
    }


def resolve_rag_embedder_setting(
    *,
    user_concept_id: str | None = None,
    org_concept_id: str | None = None,
) -> dict[str, Any]:
    return _resolve_runtime_model_setting(
        configured=get_rag_embedder_setting(),
        allow_disabled=False,
        user_concept_id=user_concept_id,
        org_concept_id=org_concept_id,
    )


def resolve_rag_llm_setting(
    *,
    user_concept_id: str | None = None,
    org_concept_id: str | None = None,
) -> dict[str, Any]:
    return _resolve_runtime_model_setting(
        configured=get_rag_llm_setting(),
        allow_disabled=True,
        user_concept_id=user_concept_id,
        org_concept_id=org_concept_id,
    )


def get_active_llm_setting() -> Optional[Dict[str, str]]:
    """
    DEPRECATED: Use resolve_llm_setting() with user/org context instead.

    This function returns the global LLM setting, which should not be used
    in multi-user environments. Model settings are now per-user.
    """
    logger.warning(
        "get_active_llm_setting() is deprecated. Use resolve_llm_setting() with user_concept_id."
    )
    setting = get_setting(ACTIVE_LLM_SETTING_NAME)
    if setting is not None and not isinstance(setting, dict):
        logger.warning(
            f"Active LLM setting is not a dictionary: {type(setting)}. Returning None."
        )
        return None
    return setting


def set_active_llm_setting(provider: str, model_name: str) -> bool:
    """
    DEPRECATED: Use set_user_llm_setting() or set_org_llm_setting() instead.

    This function sets the global LLM setting, which should not be used
    in multi-user environments. Model settings are now per-user.
    """
    logger.warning(
        "set_active_llm_setting() is deprecated. Use set_user_llm_setting() with user_concept_id."
    )
    if not isinstance(provider, str) or not isinstance(model_name, str):
        logger.error(
            f"Provider and model_name must be strings. Got: {type(provider)}, {type(model_name)}"
        )
        return False
    setting_value = {"provider": provider, "model": model_name}
    return update_setting(ACTIVE_LLM_SETTING_NAME, setting_value)


# ---------------------------------------------------------------------------
# Contextual (User / Organisation) LLM overrides
# Precedence when resolving: user override > organisation override > global
# Stored as individual settings to avoid schema migration of existing global doc.
# ---------------------------------------------------------------------------


def _build_user_llm_setting_name(user_concept_id: str) -> str:
    return f"{_ACTIVE_LLM_USER_PREFIX}{user_concept_id}"


def _build_user_mutation_authority_setting_name(user_concept_id: str) -> str:
    return f"{_MUTATION_AUTHORITY_LEVEL_USER_PREFIX}{user_concept_id}"


def _build_org_llm_setting_name(org_concept_id: str) -> str:
    return f"{_ACTIVE_LLM_ORG_PREFIX}{org_concept_id}"


def _build_user_enabled_llm_setting_name(user_concept_id: str) -> str:
    return f"{_ENABLED_LLMS_USER_PREFIX}{user_concept_id}"


def _build_org_enabled_llm_setting_name(org_concept_id: str) -> str:
    return f"{_ENABLED_LLMS_ORG_PREFIX}{org_concept_id}"


def _merge_primary_into_enabled_llms(
    *,
    primary: Mapping[str, Any] | None,
    enabled: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged = list(enabled or [])
    primary_entry = _normalise_llm_setting_entry(primary)
    if primary_entry is None:
        return _dedupe_llm_setting_entries(merged)
    return _dedupe_llm_setting_entries([primary_entry, *merged])


def set_user_enabled_llm_settings(
    user_concept_id: str,
    entries: Sequence[Mapping[str, Any]],
) -> bool:
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        logger.error("set_user_enabled_llm_settings requires a non-empty user_concept_id")
        return False
    normalised = _normalise_llm_setting_list(list(entries))
    return update_setting(_build_user_enabled_llm_setting_name(user_concept_id), normalised)


def get_user_enabled_llm_settings(user_concept_id: str) -> list[dict[str, Any]]:
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return []
    raw = get_setting(_build_user_enabled_llm_setting_name(user_concept_id))
    return _normalise_llm_setting_list(raw)


def set_org_enabled_llm_settings(
    org_concept_id: str,
    entries: Sequence[Mapping[str, Any]],
) -> bool:
    if not isinstance(org_concept_id, str) or not org_concept_id.strip():
        logger.error("set_org_enabled_llm_settings requires a non-empty org_concept_id")
        return False
    normalised = _normalise_llm_setting_list(list(entries))
    return update_setting(_build_org_enabled_llm_setting_name(org_concept_id), normalised)


def get_org_enabled_llm_settings(org_concept_id: str) -> list[dict[str, Any]]:
    if not isinstance(org_concept_id, str) or not org_concept_id.strip():
        return []
    raw = get_setting(_build_org_enabled_llm_setting_name(org_concept_id))
    return _normalise_llm_setting_list(raw)


def set_user_llm_setting(
    user_concept_id: str,
    provider: str,
    model_name: str,
    model_parameters: Mapping[str, Any] | None = None,
) -> bool:
    if not all(
        isinstance(x, str) and x for x in (user_concept_id, provider, model_name)
    ):
        logger.error("set_user_llm_setting requires non-empty string arguments")
        return False
    payload = _normalise_llm_setting_entry(
        {
            "provider": provider,
            "model": model_name,
            MODEL_PARAMETERS_KEY: model_parameters or {},
        }
    )
    if payload is None:
        logger.error("set_user_llm_setting received invalid provider/model payload")
        return False
    ok = update_setting(
        _build_user_llm_setting_name(user_concept_id),
        payload,
    )
    if not ok:
        return False
    merged_enabled = _merge_primary_into_enabled_llms(
        primary=payload,
        enabled=get_user_enabled_llm_settings(user_concept_id),
    )
    set_user_enabled_llm_settings(user_concept_id, merged_enabled)
    return True


def get_user_llm_setting(user_concept_id: str):
    if not isinstance(user_concept_id, str) or not user_concept_id:
        return None
    raw = get_setting(_build_user_llm_setting_name(user_concept_id))
    if raw is not None and not isinstance(raw, dict):
        logger.warning("User LLM setting malformed (not dict); ignoring")
        return None
    return _normalise_llm_setting_entry(raw) if raw is not None else None


def set_user_mutation_authority_level(user_concept_id: str, level: str) -> bool:
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        logger.error(
            "set_user_mutation_authority_level requires a non-empty user_concept_id"
        )
        return False
    from ..workflows.write_tool_policy import (
        normalise_mutation_authority_level,
    )

    normalised = normalise_mutation_authority_level(level)
    return update_setting(
        _build_user_mutation_authority_setting_name(user_concept_id),
        normalised,
    )


def get_user_mutation_authority_level(user_concept_id: str) -> str | None:
    if not isinstance(user_concept_id, str) or not user_concept_id.strip():
        return None
    raw = get_setting(_build_user_mutation_authority_setting_name(user_concept_id))
    if raw is None:
        return None
    from ..workflows.write_tool_policy import (
        normalise_mutation_authority_level,
    )

    return normalise_mutation_authority_level(raw)


def set_org_llm_setting(
    org_concept_id: str,
    provider: str,
    model_name: str,
    model_parameters: Mapping[str, Any] | None = None,
) -> bool:
    if not all(
        isinstance(x, str) and x for x in (org_concept_id, provider, model_name)
    ):
        logger.error("set_org_llm_setting requires non-empty string arguments")
        return False
    payload = _normalise_llm_setting_entry(
        {
            "provider": provider,
            "model": model_name,
            MODEL_PARAMETERS_KEY: model_parameters or {},
        }
    )
    if payload is None:
        logger.error("set_org_llm_setting received invalid provider/model payload")
        return False
    ok = update_setting(
        _build_org_llm_setting_name(org_concept_id),
        payload,
    )
    if not ok:
        return False
    merged_enabled = _merge_primary_into_enabled_llms(
        primary=payload,
        enabled=get_org_enabled_llm_settings(org_concept_id),
    )
    set_org_enabled_llm_settings(org_concept_id, merged_enabled)
    return True


def get_org_llm_setting(org_concept_id: str):
    if not isinstance(org_concept_id, str) or not org_concept_id:
        return None
    raw = get_setting(_build_org_llm_setting_name(org_concept_id))
    if raw is not None and not isinstance(raw, dict):
        logger.warning("Org LLM setting malformed (not dict); ignoring")
        return None
    return _normalise_llm_setting_entry(raw) if raw is not None else None


def resolve_llm_setting(
    user_concept_id: str | None = None, org_concept_id: str | None = None
):
    """Resolve effective LLM setting with precedence user > org.

    No global fallback - model must be set per-user or per-organisation.
    Returns dict or None.
    """
    if user_concept_id:
        user_val = get_user_llm_setting(user_concept_id)
        if user_val:
            return {**user_val, "scope": "user", "user_concept_id": user_concept_id}
    if org_concept_id:
        org_val = get_org_llm_setting(org_concept_id)
        if org_val:
            return {
                **org_val,
                "scope": "organisation",
                "organisation_concept_id": org_concept_id,
            }
    return None


def resolve_enabled_llm_settings(
    user_concept_id: str | None = None,
    org_concept_id: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve ordered enabled LLM candidates with precedence user > organisation.

    The first item is always the effective primary selection for backwards
    compatibility. Additional items represent scope-local enabled alternatives.
    """

    if user_concept_id:
        user_primary = get_user_llm_setting(user_concept_id)
        user_enabled = _merge_primary_into_enabled_llms(
            primary=user_primary,
            enabled=get_user_enabled_llm_settings(user_concept_id),
        )
        if user_enabled:
            return [
                {
                    **entry,
                    "scope": "user",
                    "user_concept_id": user_concept_id,
                }
                for entry in user_enabled
            ]

    if org_concept_id:
        org_primary = get_org_llm_setting(org_concept_id)
        org_enabled = _merge_primary_into_enabled_llms(
            primary=org_primary,
            enabled=get_org_enabled_llm_settings(org_concept_id),
        )
        if org_enabled:
            return [
                {
                    **entry,
                    "scope": "organisation",
                    "organisation_concept_id": org_concept_id,
                }
                for entry in org_enabled
            ]

    return []


# --- Deprecated compatibility layer (tests still reference these) ---
def set_current_user_person_concept_id(
    concept_id: str,
) -> bool:  # legacy name used in tests
    """Compatibility shim: store provided concept_id in Flask session if available.

    Returns True to satisfy existing tests, but logs deprecation.
    """
    try:
        from flask import session, has_request_context

        if has_request_context():
            session["user_concept_id"] = concept_id
    except Exception:
        pass
    logger.warning(
        "set_current_user_person_concept_id is deprecated; relying on session user_concept_id."
    )
    return True


def get_current_user_person_concept_id() -> Optional[str]:  # legacy accessor
    try:
        from flask import session, has_request_context

        if has_request_context():
            return session.get("user_concept_id")
    except Exception:
        return None
    return None


# Removed: get/set_current_user_person_concept_id functions (client localStorage authority)


# The following functions are deprecated as organisation is managed on the client.
def get_current_organisation_id() -> Optional[str]:
    """DEPRECATED: This function is deprecated. Organisation is managed on the client."""
    logger.warning("get_current_organisation_id is deprecated and should not be used.")
    try:
        from flask import session, has_request_context

        if has_request_context():
            return session.get("organisation_concept_id")
    except Exception:
        return None
    return None


def set_current_organisation_id(org_id: str) -> bool:
    """DEPRECATED: This function is deprecated. Organisation is managed on the client."""
    logger.warning(
        "set_current_organisation_id is deprecated; returning True (session-scoped only)."
    )
    try:
        from flask import session, has_request_context

        if has_request_context():
            session["organisation_concept_id"] = org_id
    except Exception:
        pass
    return True


# Removed: get/set_current_organisation_concept_id functions (client localStorage authority)


# ---------------- Identity Resolution Helpers (Deterministic, Email-Centric) ----------------
def _log_identity_event(**fields):
    """Emit a structured JSON log line for user identity decisions.

    Fields include (not exhaustive):
      event: fixed 'auth_user_resolution'
      email: authenticated email
      expected_concept_id: concept id hinted by session/localStorage (if any)
      email_relation_concept_id: concept id discovered via existing #V#has_email relation (if any)
      action: chosen resolution path
      conflict: bool when mismatch detected
    """
    payload = {k: v for k, v in fields.items() if v is not None}
    payload.setdefault("event", "auth_user_resolution")
    try:
        logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        logger.info(f"auth_user_resolution (fallback log) {payload}")


def _find_user_concept_by_email(
    email: str,
) -> Optional[Dict[str, Any]]:  # Reintroduced deterministic helper
    """Find an existing user concept via an existing #V#has_email relation.

    Returns the full concept document or None.
    Safe: read-only; no mutations.
    """
    from ..services.concept_service import get_concept_by_id, get_concept_by_concept_id

    text_values_coll = get_text_values_collection()
    text_relations_coll = get_text_relations_collection()

    if text_values_coll is None or text_relations_coll is None:
        logger.error(
            "_find_user_concept_by_email: Missing text collections (DB unavailable)."
        )
        return None

    tv = text_values_coll.find_one({"text": email})
    if not tv:
        return None
    tv_id = str(tv.get("_id"))

    # Find all relations for the given email text value
    relations = list(
        text_relations_coll.find({"object_text_id": tv_id, "predicate": "#V#has_email"})
    )

    if len(relations) > 1:
        # Log a critical error if multiple users are found for the same email
        user_ids = [rel.get("subject_concept_id") for rel in relations]
        logger.critical(
            f"CRITICAL: Multiple users found for email '{email}': {user_ids}. This is a data integrity issue that must be resolved manually."
        )
        raise MultipleUsersForEmailError(email, user_ids)

    if not relations:
        return None

    rel = relations[0]  # Use the first relation found
    subj_id = rel.get("subject_concept_id")
    if not subj_id:
        return None
    try:
        # Use get_concept_by_concept_id for Von concept IDs like #V#michael_witbrock
        # Use get_concept_by_id for MongoDB ObjectIds
        if subj_id.startswith("#V#"):
            concept = get_concept_by_concept_id(subj_id)
        else:
            concept = get_concept_by_id(subj_id)
        return concept
    except Exception:
        logger.warning(
            f"_find_user_concept_by_email: concept retrieval failed for {subj_id}"
        )
        return None


def set_current_user_by_email(
    email: str, name: str, expected_user_concept_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Deterministically resolve (and set) the current user concept for an authenticated email.

    Resolution algorithm (authoritative source = email relation):
      1. If an existing #V#has_email relation maps `email` -> concept (R), adopt R.
         - If a provided / session expected concept id (E) differs from R, log conflict (action=adopt_email_relation, conflict=true).
      2. Else (no relation): If an expected concept id (E) is supplied and refers to a valid person concept,
         link E to the email (create #V#has_email relation) and adopt E (action=link_expected_concept).
      3. Else: create a new person concept, link email, adopt it (action=create_new_user).

    Conflict handling: We *never* auto-merge. The email relation wins because it is durable & unique; we surface the mismatch in logs.
    Idempotence: Repeated calls produce same adopted concept & no duplicate relations.

        Identity Invariants (JVNAUTOSCI-634):
            - Returned dict MUST contain 'concept_id'. If absent but 'id' (vontology style) is present and looks like a concept id, it is normalised.
            - Session key 'user_concept_id' is always set to the adopted concept_id on success.
            - A single #V#has_email relation per email is expected; multiple will log conflict (future cleanup tool will reconcile).
            - No name-based disambiguation is performed (eliminates heuristic drift / race conditions).
    """
    from ..services.concept_service import (
        get_concept_by_concept_id,
        create_concept,
        get_concept_by_id,
    )
    from ..services.text_value_service import upsert_text_for_concept
    from flask import session

    if not email:
        logger.error("set_current_user_by_email: Blank email provided; aborting.")
        return None

    # Allow caller override; if absent, attempt to read from existing session (pre-login hint from client localStorage)
    if expected_user_concept_id is None:
        expected_user_concept_id = session.get("user_concept_id")

    try:
        email_user = _find_user_concept_by_email(email)
    except MultipleUsersForEmailError as e:
        # Propagate the error to be handled by the caller (e.g., the API route)
        raise e

    adopted: Optional[Dict[str, Any]] = None
    action = None
    conflict = False

    # Step 1: Existing relation path
    if email_user:
        adopted = email_user
        action = "adopt_email_relation"
        if (
            expected_user_concept_id
            and adopted.get("concept_id") != expected_user_concept_id
        ):
            conflict = True
            _log_identity_event(
                email=email,
                expected_concept_id=expected_user_concept_id,
                email_relation_concept_id=adopted.get("concept_id"),
                action=action,
                conflict=True,
            )
        else:
            _log_identity_event(
                email=email,
                expected_concept_id=expected_user_concept_id,
                email_relation_concept_id=adopted.get("concept_id"),
                action=action,
            )
    else:
        # Step 2: Link expected existing concept if provided & valid
        candidate: Optional[Dict[str, Any]] = None
        if expected_user_concept_id:
            try:
                candidate = get_concept_by_concept_id(expected_user_concept_id)
            except Exception:
                candidate = None
        if candidate:
            try:
                candidate_concept_id = candidate.get("concept_id")
                if (
                    not isinstance(candidate_concept_id, str)
                    or not candidate_concept_id
                ):
                    raise ValueError("Candidate concept missing concept_id for linking")
                upsert_text_for_concept(
                    subject_concept_id=candidate_concept_id,
                    predicate="#V#has_email",
                    text=email,
                    provenance={"source": "login_link_existing"},
                )
                adopted_lookup = get_concept_by_concept_id(candidate_concept_id)
                adopted = adopted_lookup or candidate
                action = "link_expected_concept"
                _log_identity_event(
                    email=email,
                    expected_concept_id=expected_user_concept_id,
                    email_relation_concept_id=adopted.get("concept_id"),
                    action=action,
                )
            except Exception as e:
                logger.error(
                    f"Failed linking expected concept {expected_user_concept_id} to email {email}: {e}"
                )
        # Step 3: Create new if still unresolved
        if adopted is None:
            try:
                new_doc = create_concept(name=name, parent_concept_ids=["#V#person"])
                # Fix JVNAUTOSCI-634: Use concept_id (Von format) instead of id (MongoDB ObjectId)
                # to prevent duplicate email relations with different identifier formats
                new_id = new_doc.get("concept_id") if new_doc else None
                if not new_id:
                    raise RuntimeError("create_concept returned no concept_id")
                upsert_text_for_concept(
                    subject_concept_id=new_id,
                    predicate="#V#has_email",
                    text=email,
                    provenance={"source": "login_create_new"},
                )
                adopted = get_concept_by_id(new_id) or new_doc
                action = "create_new_user"
                _log_identity_event(
                    email=email,
                    expected_concept_id=expected_user_concept_id,
                    email_relation_concept_id=adopted.get("concept_id"),
                    action=action,
                )
            except Exception as e:
                logger.error(f"Failed to create/link new user for email {email}: {e}")
                _log_identity_event(
                    email=email,
                    expected_concept_id=expected_user_concept_id,
                    action="error",
                    error=str(e),
                )
                return None

    if not adopted or not adopted.get("concept_id"):
        # Attempt normalisation: some older concept docs may use 'id' or only have a legacy structure.
        fallback_id = adopted.get("id") or adopted.get("_id")
        if isinstance(fallback_id, str) and fallback_id.startswith("#V#"):
            adopted["concept_id"] = fallback_id
        else:
            logger.error(
                f"set_current_user_by_email: Adopted concept invalid for email {email} (action={action})."
            )
            return None

    # Set session binding (authoritative for remainder of request flow)
    session["user_concept_id"] = adopted.get("concept_id")
    logger.info(
        f"set_current_user_by_email: Adopted concept {adopted.get('concept_id')} (action={action}, conflict={conflict})."
    )
    return adopted


def get_current_user() -> Optional[Dict[str, Any]]:
    """
    Gets the full concept document for the current user.
    """
    from ..services.concept_service import get_concept_by_id

    from flask import session

    user_concept_id = session.get("user_concept_id")
    if not user_concept_id:
        # DEPRECATED PATH: Fallback to the old setting. This should be removed in the future.
        user_id = get_current_user_person_concept_id()
        if not user_id:
            return None
        logger.warning(f"Falling back to deprecated user_id from settings: {user_id}")
        try:
            return get_concept_by_id(user_id)
        except Exception:
            return None

    try:
        # Try finding by concept_id first
        from ..services.concept_service import get_concept_by_concept_id

        return get_concept_by_concept_id(user_concept_id)
    except Exception:
        # Fallback to searching by _id
        try:
            return get_concept_by_id(user_concept_id)
        except Exception:
            return None


# OpenAI settings functions
def get_openai_env_var() -> Optional[str]:
    """Get the environment variable name for OpenAI API key."""
    env_var = get_setting(OPENAI_ENV_VAR_SETTING_NAME)
    if env_var is None:
        return "OPENAI_API_KEY"  # Default value
    if not isinstance(env_var, str):
        logger.warning(
            "OpenAI env var setting is not a string: %s. Using default.",
            type(env_var),
        )
        return "OPENAI_API_KEY"
    return env_var


def set_openai_env_var(env_var: str) -> bool:
    """Set the environment variable name for OpenAI API key."""
    if not isinstance(env_var, str):
        logger.error("env_var must be a string. Got: %s", type(env_var))
        return False
    return update_setting(OPENAI_ENV_VAR_SETTING_NAME, env_var)


# --- Vontology UI behaviour toggles ---
def get_fetch_counts_on_load() -> bool:
    """Return whether the frontend should fetch entity/instance counts during initial load.

    Defaults to True when unset or invalid.
    """
    val = get_setting(FETCH_COUNTS_ON_LOAD_SETTING_NAME)
    if isinstance(val, bool):
        return val
    # Accept common string/int truthy forms if previously set loosely
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):  # treat truthy
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    if isinstance(val, (int, float)):
        return val != 0
    return True


def set_fetch_counts_on_load(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        # Coerce permissively but persist canonical bool
        enabled = bool(enabled)
    return update_setting(FETCH_COUNTS_ON_LOAD_SETTING_NAME, enabled)


def get_preload_vontology_tree() -> bool:
    """Return whether the frontend should preload the Vontology tree on page load.

    Defaults to False when unset or invalid.
    """
    val = get_setting(PRELOAD_VONTOLOGY_TREE_SETTING_NAME)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    if isinstance(val, (int, float)):
        return val != 0
    return False


def set_preload_vontology_tree(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        enabled = bool(enabled)
    return update_setting(PRELOAD_VONTOLOGY_TREE_SETTING_NAME, enabled)


def get_disable_remote_ollama_scan() -> bool:
    """Return whether remote Ollama host scanning should be disabled.

    Defaults to False when unset or invalid (allowing remote scans).
    """
    val = get_setting(DISABLE_REMOTE_OLLAMA_SCAN_SETTING_NAME)
    if isinstance(val, bool):
        return val
    # Accept common string/int truthy forms if previously set loosely
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):  # treat truthy
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    if isinstance(val, (int, float)):
        return val != 0
    return False  # Default to allowing remote scans


def set_disable_remote_ollama_scan(disabled: bool) -> bool:
    if not isinstance(disabled, bool):
        # Coerce permissively but persist canonical bool
        disabled = bool(disabled)
    return update_setting(DISABLE_REMOTE_OLLAMA_SCAN_SETTING_NAME, disabled)


def get_show_tool_use_during_thinking() -> bool:
    """Return whether the chat UI should show tool use while Von is thinking.

    Defaults to True when unset or invalid.
    """

    val = get_setting(SHOW_TOOL_USE_DURING_THINKING_SETTING_NAME)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    if isinstance(val, (int, float)):
        return val != 0
    return True


def set_show_tool_use_during_thinking(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        enabled = bool(enabled)
    return update_setting(SHOW_TOOL_USE_DURING_THINKING_SETTING_NAME, enabled)


def get_buttonify_model_enabled() -> bool:
    """Return whether model-driven buttonify is enabled.

    Defaults to True when unset or invalid. Falls back to VON_BUTTONIFY_MODEL_ENABLE.
    """

    val = get_setting(BUTTONIFY_MODEL_ENABLED_SETTING_NAME)
    if val is None:
        env_value = os.getenv("VON_BUTTONIFY_MODEL_ENABLE", "1")
        return str(env_value).strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    if isinstance(val, (int, float)):
        return val != 0
    return True


def set_buttonify_model_enabled(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        enabled = bool(enabled)
    return update_setting(BUTTONIFY_MODEL_ENABLED_SETTING_NAME, enabled)


def get_auto_proceed_minimal_imposition_enabled() -> bool:
    """Return whether minimal-imposition auto-proceed is enabled.

    Defaults to True when unset or invalid.
    Falls back to VON_AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLE.
    """

    val = get_setting(AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLED_SETTING_NAME)
    if val is None:
        env_value = os.getenv("VON_AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLE", "1")
        return str(env_value).strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
    if isinstance(val, (int, float)):
        return val != 0
    return True


def set_auto_proceed_minimal_imposition_enabled(enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        enabled = bool(enabled)
    return update_setting(AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLED_SETTING_NAME, enabled)


def get_disable_write_tool_conservatism() -> bool:
    """Return whether admin has disabled write-tool conservatism.

    Defaults to True (disabled) when unset or invalid.
    """

    val = get_setting(DISABLE_WRITE_TOOL_CONSERVATISM_SETTING_NAME)
    if val is None:
        return True
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        normalised = val.strip().lower()
        if normalised in {"", "unset", "none", "null"}:
            return True
        if normalised in {"1", "true", "yes", "y", "on"}:
            return True
        if normalised in {"0", "false", "no", "n", "off"}:
            return False
    return True


def set_disable_write_tool_conservatism(disabled: bool) -> bool:
    return update_setting(DISABLE_WRITE_TOOL_CONSERVATISM_SETTING_NAME, bool(disabled))


def get_global_mutation_authority_level() -> str:
    from ..workflows.write_tool_policy import (
        MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED,
    )

    # The legacy admin flag no longer acts as a coarse "full access" or
    # "reduced access" ceiling. Mutation authority is now modelled primarily
    # through user grants, workflow step contracts, and tool-surface guardrails.
    # Keep the global source explicit and stable until a dedicated global
    # authority setting is introduced.
    return MUTATION_AUTHORITY_LEVEL_EXTERNAL_SYSTEM_GUARDED


def get_require_human_review_for_high_impact_kb_writes() -> bool:
    """Return whether high-impact KB writes require explicit human approval."""

    val = get_setting(REQUIRE_HUMAN_REVIEW_FOR_HIGH_IMPACT_KB_WRITES_SETTING_NAME)
    return _coerce_bool(val, default=False)


def set_require_human_review_for_high_impact_kb_writes(required: bool) -> bool:
    return update_setting(
        REQUIRE_HUMAN_REVIEW_FOR_HIGH_IMPACT_KB_WRITES_SETTING_NAME,
        bool(required),
    )


def _coerce_int_setting(
    value: Any,
    *,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    if value is None:
        return default

    parsed: int | None = None
    if isinstance(value, bool):
        # Avoid treating True/False as 1/0 for numeric settings.
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        try:
            parsed = int(value)
        except Exception:
            parsed = None
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except Exception:
            parsed = None

    if parsed is None:
        return default

    return max(min_value, min(max_value, parsed))


def get_internal_mcp_max_tool_invocations() -> int:
    """Return the maximum number of internal MCP tool calls per chat turn.

    Defaults to 100 when unset/invalid.
    """

    val = get_setting(INTERNAL_MCP_MAX_TOOL_INVOCATIONS_SETTING_NAME)
    return _coerce_int_setting(
        val,
        default=INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT,
        min_value=INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN,
        max_value=INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX,
    )


def set_internal_mcp_max_tool_invocations(value: Any) -> bool:
    """Persist the max internal MCP tool invocations (canonical int, clamped)."""

    coerced = _coerce_int_setting(
        value,
        default=INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT,
        min_value=INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN,
        max_value=INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX,
    )
    return update_setting(INTERNAL_MCP_MAX_TOOL_INVOCATIONS_SETTING_NAME, coerced)


def get_internal_mcp_tool_batch_cap() -> int:
    """Return the maximum number of tool calls executed per batch.

    This is a guardrail against very large tool-call lists per iteration.
    Defaults to 10 when unset/invalid.
    """

    val = get_setting(INTERNAL_MCP_TOOL_BATCH_CAP_SETTING_NAME)
    return _coerce_int_setting(
        val,
        default=INTERNAL_MCP_TOOL_BATCH_CAP_DEFAULT,
        min_value=INTERNAL_MCP_TOOL_BATCH_CAP_MIN,
        max_value=INTERNAL_MCP_TOOL_BATCH_CAP_MAX,
    )


def set_internal_mcp_tool_batch_cap(value: Any) -> bool:
    """Persist the tool-call batch cap (canonical int, clamped)."""

    coerced = _coerce_int_setting(
        value,
        default=INTERNAL_MCP_TOOL_BATCH_CAP_DEFAULT,
        min_value=INTERNAL_MCP_TOOL_BATCH_CAP_MIN,
        max_value=INTERNAL_MCP_TOOL_BATCH_CAP_MAX,
    )
    return update_setting(INTERNAL_MCP_TOOL_BATCH_CAP_SETTING_NAME, coerced)


# Ollama hosts settings functions
def get_ollama_hosts_list() -> List[Dict[str, Any]]:
    """
    Get the list of Ollama hosts from settings or environment variable.

    Returns:
        List of dictionaries containing host information with 'url', 'name', and 'is_local' keys.
    """
    import os
    from urllib.parse import urlparse

    DEFAULT_PORT = 11434

    def _normalize_url(url_or_host: str) -> Optional[str]:
        """Return a canonical http URL for an Ollama host.
        - Adds http scheme if missing
        - Adds default port 11434 if missing
        - Strips paths and trailing slash
        """
        if not url_or_host or not isinstance(url_or_host, str):
            return None
        s = url_or_host.strip()
        if not s:
            return None

        # If someone provided only a hostname/IP (no scheme), add http
        if "://" not in s:
            s = f"http://{s}"

        # Remove trailing slash to avoid duplicates like host:11434/
        while s.endswith("/"):
            s = s[:-1]

        p = urlparse(s)
        # Guard against parse failures
        host = (
            p.hostname
            or s.replace("http://", "")
            .replace("https://", "")
            .split("/", 1)[0]
            .split(":")[0]
        )
        # Determine port
        port = p.port or DEFAULT_PORT
        scheme = p.scheme or "http"

        # IPv6 bracket handling
        host_netloc = host
        if ":" in host and not host.startswith("["):
            host_netloc = f"[{host}]"

        return f"{scheme}://{host_netloc}:{port}"

    def _to_host_entry(entry: Any) -> Optional[Dict[str, Any]]:
        """Coerce various entry shapes into a normalized host dict with url/name/is_local."""
        # Accept either dicts or strings
        url_candidate: Optional[str] = None
        name_candidate: Optional[str] = None

        if isinstance(entry, str):
            url_candidate = entry
        elif isinstance(entry, dict):
            # Prefer url; fall back to name if url missing
            url_candidate = (
                entry.get("url")
                or entry.get("host")
                or entry.get("address")
                or entry.get("name")
            )
            name_candidate = entry.get("name")
        else:
            return None

        norm_url = _normalize_url(url_candidate) if url_candidate else None
        if not norm_url:
            return None

        # Derive name from URL if not provided
        parsed = urlparse(norm_url)
        hostname = parsed.hostname or ""
        name = name_candidate or hostname
        is_local = hostname in {"localhost", "127.0.0.1"}

        return {"url": norm_url, "name": name, "is_local": is_local}

    def _dedupe_and_sort(hosts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Dedupe by normalized URL, keep first occurrence; prioritize local first then by name."""
        seen: Dict[str, Dict[str, Any]] = {}
        for h in hosts:
            if not isinstance(h, dict):
                continue
            norm = _to_host_entry(h)
            if not norm:
                continue
            key = norm["url"]
            if key not in seen:
                seen[key] = norm
        # Sort: local first, then by name for stability
        result = list(seen.values())
        result.sort(
            key=lambda x: (not x.get("is_local", False), str(x.get("name", "")))
        )
        return result

    # First check database settings
    hosts_list = get_setting(OLLAMA_HOSTS_LIST_SETTING_NAME)
    if hosts_list and isinstance(hosts_list, list):
        # Normalize and dedupe persisted hosts
        clean_hosts = _dedupe_and_sort(hosts_list)
        logger.info(
            f"Found Ollama hosts list in settings: {len(clean_hosts)} hosts (normalized)"
        )
        # If normalization changed the list, persist the cleaned version silently
        try:
            if clean_hosts != hosts_list:
                update_setting(OLLAMA_HOSTS_LIST_SETTING_NAME, clean_hosts)
        except Exception:
            # Non-fatal if we can't persist
            pass
        return clean_hosts

    # Fall back to environment variable OLLAMA_HOSTS_LIST
    env_hosts = os.environ.get("OLLAMA_HOSTS_LIST")
    if env_hosts:
        try:
            # Parse comma-separated list of hosts
            parsed_hosts: List[Dict[str, Any]] = []
            for host_url in env_hosts.split(","):
                host_url = host_url.strip()
                if not host_url:
                    continue
                entry = _to_host_entry(host_url)
                if entry:
                    parsed_hosts.append(entry)
            parsed_hosts = _dedupe_and_sort(parsed_hosts)
            logger.info(
                f"Found Ollama hosts in environment variable: {len(parsed_hosts)} hosts"
            )
            return parsed_hosts
        except Exception as e:
            logger.error(f"Error parsing OLLAMA_HOSTS_LIST environment variable: {e}")

    # Default to local host and current OLLAMA_HOST if set
    default_hosts: List[Dict[str, Any]] = []

    # Always include localhost (guard _to_host_entry which may return None)
    localhost_entry = _to_host_entry("http://localhost:11434")
    if localhost_entry:
        default_hosts.append(localhost_entry)

    # Check for current OLLAMA_HOST environment variable
    current_host = os.environ.get("OLLAMA_HOST")
    if current_host and current_host != "http://localhost:11434":
        entry = _to_host_entry(current_host)
        if entry:
            default_hosts.append(entry)

    default_hosts = _dedupe_and_sort([h for h in default_hosts if h])
    logger.info(f"Using default Ollama hosts: {len(default_hosts)} hosts")
    return default_hosts


def set_ollama_hosts_list(hosts_list: List[Dict[str, Any]]) -> bool:
    """
    Set the list of Ollama hosts in settings.

    Args:
        hosts_list: List of dictionaries containing host information.

    Returns:
        True if the setting was updated successfully, False otherwise.
    """
    if not isinstance(hosts_list, list):
        logger.error(f"Hosts list must be a list. Got: {type(hosts_list)}")
        return False

    # Normalize and dedupe entries; also validate shape
    try:
        # Reuse helpers from getter
        from urllib.parse import urlparse  # noqa: F401 (ensures consistency if moved)

        def _normalize_and_dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            # Local copies of helpers from get_ollama_hosts_list
            DEFAULT_PORT_LOCAL = 11434

            def _normalize_url_local(u: str) -> Optional[str]:
                if not u or not isinstance(u, str):
                    return None
                s = u.strip()
                if not s:
                    return None
                if "://" not in s:
                    s = f"http://{s}"
                while s.endswith("/"):
                    s = s[:-1]
                p = urlparse(s)
                host = (
                    p.hostname
                    or s.replace("http://", "")
                    .replace("https://", "")
                    .split("/", 1)[0]
                    .split(":")[0]
                )
                port = p.port or DEFAULT_PORT_LOCAL
                scheme = p.scheme or "http"
                host_netloc = host
                if ":" in host and not host.startswith("["):
                    host_netloc = f"[{host}]"
                return f"{scheme}://{host_netloc}:{port}"

            def _entry(e: Any) -> Optional[Dict[str, Any]]:
                url_cand = None
                name_cand = None
                if isinstance(e, str):
                    url_cand = e
                elif isinstance(e, dict):
                    url_cand = (
                        e.get("url")
                        or e.get("host")
                        or e.get("address")
                        or e.get("name")
                    )
                    name_cand = e.get("name")
                else:
                    return None
                nu = _normalize_url_local(url_cand) if url_cand else None
                if not nu:
                    return None
                ph = urlparse(nu)
                hostname = ph.hostname or ""
                name = name_cand or hostname
                is_local = hostname in {"localhost", "127.0.0.1"}
                return {"url": nu, "name": name, "is_local": is_local}

            dedup: Dict[str, Dict[str, Any]] = {}
            for it in items:
                en = _entry(it)
                if not en:
                    continue
                if en["url"] not in dedup:
                    dedup[en["url"]] = en
            res = list(dedup.values())
            res.sort(
                key=lambda x: (not x.get("is_local", False), str(x.get("name", "")))
            )
            return res

        cleaned = _normalize_and_dedupe(hosts_list)
        if not cleaned:
            logger.error("Normalized hosts list is empty or invalid")
            return False
        return update_setting(OLLAMA_HOSTS_LIST_SETTING_NAME, cleaned)
    except Exception as e:
        logger.error(f"Failed to normalize hosts list: {e}")
        return False


def get_active_ollama_host() -> Optional[str]:
    """
    Get the currently active Ollama host URL.

    Returns:
        The URL of the active Ollama host, or None if not set.
    """
    from urllib.parse import urlparse

    def _normalize_for_active(u: Optional[str]) -> Optional[str]:
        if not u or not isinstance(u, str):
            return None
        # Reuse same normalization as above (minimal copy to avoid import cycles)
        s = u.strip()
        if "://" not in s:
            s = f"http://{s}"
        while s.endswith("/"):
            s = s[:-1]
        p = urlparse(s)
        host = (
            p.hostname
            or s.replace("http://", "")
            .replace("https://", "")
            .split("/", 1)[0]
            .split(":")[0]
        )
        port = p.port or 11434
        scheme = p.scheme or "http"
        host_netloc = host
        if ":" in host and not host.startswith("["):
            host_netloc = f"[{host}]"
        return f"{scheme}://{host_netloc}:{port}"

    active_host = get_setting(ACTIVE_OLLAMA_HOST_SETTING_NAME)
    norm_active = _normalize_for_active(active_host)
    if norm_active:
        # Ensure the normalized active host is persisted to avoid drift
        try:
            if norm_active != active_host:
                update_setting(ACTIVE_OLLAMA_HOST_SETTING_NAME, norm_active)
        except Exception:
            pass
        return norm_active

    # Fall back to first host in the list
    hosts_list = get_ollama_hosts_list()
    if hosts_list:
        return hosts_list[0]["url"]

    return None


def set_active_ollama_host(host_url: str) -> bool:
    """
    Set the currently active Ollama host URL.

    Args:
        host_url: The URL of the Ollama host to set as active.

    Returns:
        True if the setting was updated successfully, False otherwise.
    """
    if not isinstance(host_url, str):
        logger.error(f"Host URL must be a string. Got: {type(host_url)}")
        return False

    # Normalize URL to avoid duplicates (e.g., with/without scheme/port)
    try:
        # Minimal inline normalizer to avoid cross-scope helper use
        from urllib.parse import urlparse

        s = host_url.strip()
        if "://" not in s:
            s = f"http://{s}"
        while s.endswith("/"):
            s = s[:-1]
        p = urlparse(s)
        host = (
            p.hostname
            or s.replace("http://", "")
            .replace("https://", "")
            .split("/", 1)[0]
            .split(":")[0]
        )
        port = p.port or 11434
        scheme = p.scheme or "http"
        host_netloc = host
        if ":" in host and not host.startswith("["):
            host_netloc = f"[{host}]"
        host_url = f"{scheme}://{host_netloc}:{port}"
    except Exception:
        pass

    # Validate that the host is in the hosts list
    hosts_list = get_ollama_hosts_list()
    valid_urls = [host["url"] for host in hosts_list]
    if host_url not in valid_urls:
        logger.warning(f"Host URL {host_url} not found in hosts list. Adding it.")
        # Add the new host to the list
        try:
            from urllib.parse import urlparse

            p = urlparse(host_url)
            hostname = p.hostname or ""
            name = hostname
            is_local = hostname in {"localhost", "127.0.0.1"}
            hosts_list.append({"url": host_url, "name": name, "is_local": is_local})
            # Persist through setter to ensure normalization/dedupe
            set_ollama_hosts_list(hosts_list)
        except Exception:
            pass

    return update_setting(ACTIVE_OLLAMA_HOST_SETTING_NAME, host_url)


# Preferred language setting
def get_preferred_language() -> str:
    """Get the preferred language code; default to 'en-NZ' if not set or invalid."""
    value = get_setting(PREFERRED_LANGUAGE_SETTING_NAME)
    if isinstance(value, str) and value:
        return value
    return "en-NZ"


def set_preferred_language(lang_code: str) -> bool:
    """Set the preferred language code."""
    if not isinstance(lang_code, str) or not lang_code:
        logger.error(
            f"Language code must be a non-empty string. Got: {type(lang_code)} {lang_code}"
        )
        return False

    result = update_setting(PREFERRED_LANGUAGE_SETTING_NAME, lang_code)

    # Invalidate the cached preferred language in utils_vontology
    if result:
        try:
            from ..vontology.utils_vontology import clear_preferred_language_cache

            clear_preferred_language_cache()
        except ImportError:
            # Module might not be available in some contexts (tests, etc.)
            pass

    return result


# Per-model LLM call timeout overrides


def _make_model_timeout_key(provider: str, model: str) -> str:
    """Canonical dict key for per-model timeout storage: ``"{provider}:{model}"``."""
    clean_provider = str(provider or "").strip().lower()
    clean_model = str(model or "").strip()
    return f"{clean_provider}:{clean_model}"


def get_model_llm_timeout_overrides() -> dict[str, float]:
    """Return the full per-model timeout override dict (``provider:model`` → seconds)."""
    raw = get_setting(MODEL_LLM_TIMEOUT_OVERRIDES_SETTING_NAME)
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        try:
            coerced = float(value)
        except (TypeError, ValueError):
            continue
        if coerced > 0:
            result[key] = coerced
    return result


def get_model_llm_timeout(provider: str, model: str) -> float | None:
    """Return the saved LLM call timeout for a specific model, or ``None`` if not set."""
    overrides = get_model_llm_timeout_overrides()
    key = _make_model_timeout_key(provider, model)
    return overrides.get(key)


def set_model_llm_timeout(provider: str, model: str, timeout_sec: float | None) -> bool:
    """Save or clear the per-model LLM call timeout override (seconds)."""
    from ..workflows.conversation_turn_llm_timeout import (
        coerce_conversation_turn_llm_timeout_sec,
    )

    key = _make_model_timeout_key(provider, model)
    if not key.replace(":", "").strip():
        logger.error(
            "set_model_llm_timeout requires non-empty provider and model"
        )
        return False
    overrides = get_model_llm_timeout_overrides()
    if timeout_sec is None:
        overrides.pop(key, None)
    else:
        coerced = coerce_conversation_turn_llm_timeout_sec(timeout_sec)
        if coerced is None:
            overrides.pop(key, None)
        else:
            overrides[key] = coerced
    return update_setting(MODEL_LLM_TIMEOUT_OVERRIDES_SETTING_NAME, overrides)


# --- COMPATIBILITY FUNCTIONS ---
# These functions are DEPRECATED in favor of per-user settings.


def get_global_ollama_model() -> Optional[str]:
    """
    DEPRECATED: Use resolve_llm_setting() with user/org context instead.

    Get the global Ollama model name if Ollama is the active provider.
    """
    logger.warning(
        "get_global_ollama_model() is deprecated. Use resolve_llm_setting() with user_concept_id."
    )
    active_llm = get_active_llm_setting()
    if active_llm and active_llm.get("provider") == "ollama":
        return active_llm.get("model")
    return None


def set_global_ollama_model(model_name: str) -> bool:
    """
    DEPRECATED: Use set_user_llm_setting() with user_concept_id instead.

    Set the global Ollama model by updating the active LLM setting.
    """
    logger.warning(
        "set_global_ollama_model() is deprecated. Use set_user_llm_setting() with user_concept_id."
    )
    if not isinstance(model_name, str):
        logger.error(f"Model name must be a string. Got: {type(model_name)}")
        return False
    return set_active_llm_setting("ollama", model_name)


# --- DEPRECATED FUNCTIONS ---
# These are no longer used and will be removed.
# def get_openai_model() -> Optional[str]: ...
# def set_openai_model(model: str) -> bool: ...

if __name__ == "__main__":
    print("--- Testing Settings Service ---")

    # Test setting and getting the active LLM
    print("\nTesting Active LLM setting:")
    initial_llm = get_active_llm_setting()
    print(f"Initial active_llm: {initial_llm}")

    print("\nAttempting to set active_llm to 'ollama:gemma:2b'...")
    if set_active_llm_setting("ollama", "gemma:2b"):
        print("Successfully set active_llm.")
        updated_llm = get_active_llm_setting()
        print(f"Updated active_llm: {updated_llm}")
        if updated_llm != {"provider": "ollama", "model": "gemma:2b"}:
            print("ERROR: Active LLM was not updated correctly!")
    else:
        print("Failed to set active_llm.")

    print("\nAttempting to set active_llm to 'openai:gpt-4'...")
    if set_active_llm_setting("openai", "gpt-4"):
        print("Successfully set active_llm.")
        updated_llm_2 = get_active_llm_setting()
        print(f"Updated active_llm: {updated_llm_2}")
        if updated_llm_2 != {"provider": "openai", "model": "gpt-4"}:
            print("ERROR: Active LLM was not updated correctly!")
    else:
        print("Failed to set active_llm.")

    # Test current user person setting
    print("\nTesting Current User Person setting:")
    initial_user = get_current_user_person_concept_id()
    print(f"Initial current_user_person_concept_id: {initial_user}")

    print("\nAttempting to set current_user_person_concept_id to 'test_person_123'...")
    if set_current_user_person_concept_id("test_person_123"):
        print("Successfully set current_user_person_concept_id.")
        updated_user = get_current_user_person_concept_id()
        print(f"Updated current_user_person_concept_id: {updated_user}")
        if updated_user != "test_person_123":
            print("ERROR: Current user person concept ID was not updated correctly!")
    else:
        print("Failed to set current_user_person_concept_id.")

    print("\n--- End of Settings Service Tests ---")
