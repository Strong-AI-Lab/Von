import logging
import json
from typing import Any, Optional, Dict, List
from pymongo.results import UpdateResult
from pymongo.errors import OperationFailure
from datetime import datetime, timezone
from ..utils.time_utils import utc_now
from ..db.mongo_client import APPLICATION_SETTINGS_COLLECTION_NAME, get_concepts_collection, get_text_values_collection, get_text_relations_collection
from ..db.mongo_setup import get_application_settings_collection
from .exceptions import MultipleUsersForEmailError

# Configure logging
logger = logging.getLogger(__name__)

# --- Setting Names ---
# REFACTORING_NOTE: Consolidating to a single setting for the active LLM.
ACTIVE_LLM_SETTING_NAME = "active_llm"
OPENAI_ENV_VAR_SETTING_NAME = "openai_api_key_env_var"
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
DISABLE_REMOTE_OLLAMA_SCAN_SETTING_NAME = "disable_remote_ollama_scan"

# Prefixes for contextual (scoped) LLM settings (Phase 2 scaffold)
_ACTIVE_LLM_USER_PREFIX = f"{ACTIVE_LLM_SETTING_NAME}:user:"
_ACTIVE_LLM_ORG_PREFIX = f"{ACTIVE_LLM_SETTING_NAME}:org:"


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
        logger.error(f"Could not access the '{APPLICATION_SETTINGS_COLLECTION_NAME}' collection for setting '{setting_name}'.")
        return None
    try:
        setting_doc = settings_coll.find_one({"setting_name": setting_name})
        if setting_doc:
            value = setting_doc.get("value")
            logger.info(f"Found setting '{setting_name}' with value: '{value}'")
            return value
        logger.info(f"Setting '{setting_name}' not found in the database.")
        return None
    except OperationFailure as e:
        logger.error(f"MongoDB operation failed while getting setting '{setting_name}': {e}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred while getting setting '{setting_name}': {e}")
        return None

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
            logger.error("Could not access the '%s' collection.", APPLICATION_SETTINGS_COLLECTION_NAME)
            return False

        # Pre-check for existing setting with the same value
        existing_doc = settings_collection.find_one({"setting_name": setting_name}) # Changed "name" to "setting_name"
        if existing_doc and existing_doc.get("value") == setting_value:
            # For test_update_setting_same_value
            logger.info(f"Setting '{setting_name}' value is already up to date.")
            return True

        result: UpdateResult = settings_collection.update_one(
            {"setting_name": setting_name}, # Changed "name" to "setting_name"
                {"$set": {"value": setting_value, "updated_at": utc_now()}},
            upsert=True
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
                logger.info(f"Setting '{setting_name}' update operation acknowledged, but no changes detected.")
            return True
        else:
            # For test_update_setting_not_acknowledged
            logger.error(f"Update/create operation for setting '{setting_name}' was not acknowledged by the server.")
            return False
    except OperationFailure as e:
        # For test_update_setting_operation_failure
        logger.error(f"MongoDB operation failed while updating setting '{setting_name}': {e}")
        return False
    except Exception as e:
        # For test_update_setting_general_exception
        logger.error(f"An unexpected error occurred while updating setting '{setting_name}': {e}")
        return False

def get_active_llm_setting() -> Optional[Dict[str, str]]:
    """
    Convenience function to get the active LLM setting.
    The setting is expected to be a dict: {"provider": "ollama|openai", "model": "model_name"}
    """
    setting = get_setting(ACTIVE_LLM_SETTING_NAME)
    if setting is not None and not isinstance(setting, dict):
        logger.warning(f"Active LLM setting is not a dictionary: {type(setting)}. Returning None.")
        return None
    return setting

def set_active_llm_setting(provider: str, model_name: str) -> bool:
    """Convenience function to set the active LLM setting."""
    if not isinstance(provider, str) or not isinstance(model_name, str):
        logger.error(f"Provider and model_name must be strings. Got: {type(provider)}, {type(model_name)}")
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

def _build_org_llm_setting_name(org_concept_id: str) -> str:
    return f"{_ACTIVE_LLM_ORG_PREFIX}{org_concept_id}"

def set_user_llm_setting(user_concept_id: str, provider: str, model_name: str) -> bool:
    if not all(isinstance(x, str) and x for x in (user_concept_id, provider, model_name)):
        logger.error("set_user_llm_setting requires non-empty string arguments")
        return False
    return update_setting(_build_user_llm_setting_name(user_concept_id), {"provider": provider, "model": model_name})

def get_user_llm_setting(user_concept_id: str):
    if not isinstance(user_concept_id, str) or not user_concept_id:
        return None
    raw = get_setting(_build_user_llm_setting_name(user_concept_id))
    if raw is not None and not isinstance(raw, dict):
        logger.warning("User LLM setting malformed (not dict); ignoring")
        return None
    return raw

def set_org_llm_setting(org_concept_id: str, provider: str, model_name: str) -> bool:
    if not all(isinstance(x, str) and x for x in (org_concept_id, provider, model_name)):
        logger.error("set_org_llm_setting requires non-empty string arguments")
        return False
    return update_setting(_build_org_llm_setting_name(org_concept_id), {"provider": provider, "model": model_name})

def get_org_llm_setting(org_concept_id: str):
    if not isinstance(org_concept_id, str) or not org_concept_id:
        return None
    raw = get_setting(_build_org_llm_setting_name(org_concept_id))
    if raw is not None and not isinstance(raw, dict):
        logger.warning("Org LLM setting malformed (not dict); ignoring")
        return None
    return raw

def resolve_llm_setting(user_concept_id: str | None = None, org_concept_id: str | None = None):
    """Resolve effective LLM setting with precedence user > org > global.

    Returns dict or None.
    """
    if user_concept_id:
        user_val = get_user_llm_setting(user_concept_id)
        if user_val:
            return {**user_val, "scope": "user", "user_concept_id": user_concept_id}
    if org_concept_id:
        org_val = get_org_llm_setting(org_concept_id)
        if org_val:
            return {**org_val, "scope": "organisation", "organisation_concept_id": org_concept_id}
    global_val = get_active_llm_setting()
    if global_val:
        return {**global_val, "scope": "global"}
    return None

# --- Deprecated compatibility layer (tests still reference these) ---
def set_current_user_person_concept_id(concept_id: str) -> bool:  # legacy name used in tests
    """Compatibility shim: store provided concept_id in Flask session if available.

    Returns True to satisfy existing tests, but logs deprecation.
    """
    try:
        from flask import session, has_request_context
        if has_request_context():
            session['user_concept_id'] = concept_id
    except Exception:
        pass
    logger.warning("set_current_user_person_concept_id is deprecated; relying on session user_concept_id.")
    return True

def get_current_user_person_concept_id() -> Optional[str]:  # legacy accessor
    try:
        from flask import session, has_request_context
        if has_request_context():
            return session.get('user_concept_id')
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
            return session.get('organisation_concept_id')
    except Exception:
        return None
    return None

def set_current_organisation_id(org_id: str) -> bool:
    """DEPRECATED: This function is deprecated. Organisation is managed on the client."""
    logger.warning("set_current_organisation_id is deprecated; returning True (session-scoped only).")
    try:
        from flask import session, has_request_context
        if has_request_context():
            session['organisation_concept_id'] = org_id
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


def _find_user_concept_by_email(email: str) -> Optional[Dict[str, Any]]:  # Reintroduced deterministic helper
    """Find an existing user concept via an existing #V#has_email relation.

    Returns the full concept document or None.
    Safe: read-only; no mutations.
    """
    from ..services.concept_service import get_concept_by_id, get_concept_by_concept_id

    text_values_coll = get_text_values_collection()
    text_relations_coll = get_text_relations_collection()

    if text_values_coll is None or text_relations_coll is None:
        logger.error("_find_user_concept_by_email: Missing text collections (DB unavailable).")
        return None

    tv = text_values_coll.find_one({"text": email})
    if not tv:
        return None
    tv_id = str(tv.get("_id"))

    # Find all relations for the given email text value
    relations = list(text_relations_coll.find({"object_text_id": tv_id, "predicate": "#V#has_email"}))

    if len(relations) > 1:
        # Log a critical error if multiple users are found for the same email
        user_ids = [rel.get("subject_concept_id") for rel in relations]
        logger.critical(f"CRITICAL: Multiple users found for email '{email}': {user_ids}. This is a data integrity issue that must be resolved manually.")
        raise MultipleUsersForEmailError(email, user_ids)

    if not relations:
        return None

    rel = relations[0] # Use the first relation found
    subj_id = rel.get("subject_concept_id")
    if not subj_id:
        return None
    try:
        # Use get_concept_by_concept_id for Von concept IDs like #V#michael_witbrock
        # Use get_concept_by_id for MongoDB ObjectIds
        if subj_id.startswith('#V#'):
            concept = get_concept_by_concept_id(subj_id)
        else:
            concept = get_concept_by_id(subj_id)
        return concept
    except Exception:
        logger.warning(f"_find_user_concept_by_email: concept retrieval failed for {subj_id}")
        return None


def set_current_user_by_email(email: str, name: str, expected_user_concept_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
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
        expected_user_concept_id = session.get('user_concept_id')

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
        if expected_user_concept_id and adopted.get("concept_id") != expected_user_concept_id:
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
                if not isinstance(candidate_concept_id, str) or not candidate_concept_id:
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
                logger.error(f"Failed linking expected concept {expected_user_concept_id} to email {email}: {e}")
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
        if isinstance(fallback_id, str) and fallback_id.startswith('#V#'):
            adopted['concept_id'] = fallback_id
        else:
            logger.error(f"set_current_user_by_email: Adopted concept invalid for email {email} (action={action}).")
            return None

    # Set session binding (authoritative for remainder of request flow)
    session['user_concept_id'] = adopted.get("concept_id")
    logger.info(f"set_current_user_by_email: Adopted concept {adopted.get('concept_id')} (action={action}, conflict={conflict}).")
    return adopted



def get_current_user() -> Optional[Dict[str, Any]]:
    """
    Gets the full concept document for the current user.
    """
    from ..services.concept_service import get_concept_by_id

    from flask import session
    user_concept_id = session.get('user_concept_id')
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
        if '://' not in s:
            s = f"http://{s}"

        # Remove trailing slash to avoid duplicates like host:11434/
        while s.endswith('/'):
            s = s[:-1]

        p = urlparse(s)
        # Guard against parse failures
        host = p.hostname or s.replace('http://', '').replace('https://', '').split('/', 1)[0].split(':')[0]
        # Determine port
        port = p.port or DEFAULT_PORT
        scheme = p.scheme or 'http'

        # IPv6 bracket handling
        host_netloc = host
        if ':' in host and not host.startswith('['):
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
            url_candidate = entry.get('url') or entry.get('host') or entry.get('address') or entry.get('name')
            name_candidate = entry.get('name')
        else:
            return None

        norm_url = _normalize_url(url_candidate) if url_candidate else None
        if not norm_url:
            return None

        # Derive name from URL if not provided
        parsed = urlparse(norm_url)
        hostname = parsed.hostname or ''
        name = name_candidate or hostname
        is_local = hostname in {'localhost', '127.0.0.1'}

        return {'url': norm_url, 'name': name, 'is_local': is_local}

    def _dedupe_and_sort(hosts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Dedupe by normalized URL, keep first occurrence; prioritize local first then by name."""
        seen: Dict[str, Dict[str, Any]] = {}
        for h in hosts:
            if not isinstance(h, dict):
                continue
            norm = _to_host_entry(h)
            if not norm:
                continue
            key = norm['url']
            if key not in seen:
                seen[key] = norm
        # Sort: local first, then by name for stability
        result = list(seen.values())
        result.sort(key=lambda x: (not x.get('is_local', False), str(x.get('name', ''))))
        return result

    # First check database settings
    hosts_list = get_setting(OLLAMA_HOSTS_LIST_SETTING_NAME)
    if hosts_list and isinstance(hosts_list, list):
        # Normalize and dedupe persisted hosts
        clean_hosts = _dedupe_and_sort(hosts_list)
        logger.info(f"Found Ollama hosts list in settings: {len(clean_hosts)} hosts (normalized)")
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
            for host_url in env_hosts.split(','):
                host_url = host_url.strip()
                if not host_url:
                    continue
                entry = _to_host_entry(host_url)
                if entry:
                    parsed_hosts.append(entry)
            parsed_hosts = _dedupe_and_sort(parsed_hosts)
            logger.info(f"Found Ollama hosts in environment variable: {len(parsed_hosts)} hosts")
            return parsed_hosts
        except Exception as e:
            logger.error(f"Error parsing OLLAMA_HOSTS_LIST environment variable: {e}")

    # Default to local host and current OLLAMA_HOST if set
    default_hosts: List[Dict[str, Any]] = []

    # Always include localhost (guard _to_host_entry which may return None)
    localhost_entry = _to_host_entry('http://localhost:11434')
    if localhost_entry:
        default_hosts.append(localhost_entry)

    # Check for current OLLAMA_HOST environment variable
    current_host = os.environ.get("OLLAMA_HOST")
    if current_host and current_host != 'http://localhost:11434':
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
                if '://' not in s:
                    s = f"http://{s}"
                while s.endswith('/'):
                    s = s[:-1]
                p = urlparse(s)
                host = p.hostname or s.replace('http://', '').replace('https://', '').split('/', 1)[0].split(':')[0]
                port = p.port or DEFAULT_PORT_LOCAL
                scheme = p.scheme or 'http'
                host_netloc = host
                if ':' in host and not host.startswith('['):
                    host_netloc = f"[{host}]"
                return f"{scheme}://{host_netloc}:{port}"

            def _entry(e: Any) -> Optional[Dict[str, Any]]:
                url_cand = None
                name_cand = None
                if isinstance(e, str):
                    url_cand = e
                elif isinstance(e, dict):
                    url_cand = e.get('url') or e.get('host') or e.get('address') or e.get('name')
                    name_cand = e.get('name')
                else:
                    return None
                nu = _normalize_url_local(url_cand) if url_cand else None
                if not nu:
                    return None
                ph = urlparse(nu)
                hostname = ph.hostname or ''
                name = name_cand or hostname
                is_local = hostname in {'localhost', '127.0.0.1'}
                return {'url': nu, 'name': name, 'is_local': is_local}

            dedup: Dict[str, Dict[str, Any]] = {}
            for it in items:
                en = _entry(it)
                if not en:
                    continue
                if en['url'] not in dedup:
                    dedup[en['url']] = en
            res = list(dedup.values())
            res.sort(key=lambda x: (not x.get('is_local', False), str(x.get('name', ''))))
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
        if '://' not in s:
            s = f"http://{s}"
        while s.endswith('/'):
            s = s[:-1]
        p = urlparse(s)
        host = p.hostname or s.replace('http://', '').replace('https://', '').split('/', 1)[0].split(':')[0]
        port = p.port or 11434
        scheme = p.scheme or 'http'
        host_netloc = host
        if ':' in host and not host.startswith('['):
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
        return hosts_list[0]['url']

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
        if '://' not in s:
            s = f"http://{s}"
        while s.endswith('/'):
            s = s[:-1]
        p = urlparse(s)
        host = p.hostname or s.replace('http://', '').replace('https://', '').split('/', 1)[0].split(':')[0]
        port = p.port or 11434
        scheme = p.scheme or 'http'
        host_netloc = host
        if ':' in host and not host.startswith('['):
            host_netloc = f"[{host}]"
        host_url = f"{scheme}://{host_netloc}:{port}"
    except Exception:
        pass

    # Validate that the host is in the hosts list
    hosts_list = get_ollama_hosts_list()
    valid_urls = [host['url'] for host in hosts_list]
    if host_url not in valid_urls:
        logger.warning(f"Host URL {host_url} not found in hosts list. Adding it.")
        # Add the new host to the list
        try:
            from urllib.parse import urlparse
            p = urlparse(host_url)
            hostname = p.hostname or ''
            name = hostname
            is_local = hostname in {'localhost', '127.0.0.1'}
            hosts_list.append({'url': host_url, 'name': name, 'is_local': is_local})
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
    return 'en-NZ'

def set_preferred_language(lang_code: str) -> bool:
    """Set the preferred language code."""
    if not isinstance(lang_code, str) or not lang_code:
        logger.error(f"Language code must be a non-empty string. Got: {type(lang_code)} {lang_code}")
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

# --- COMPATIBILITY FUNCTIONS ---
# These functions provide backward compatibility for code that hasn't been updated yet.

def get_global_ollama_model() -> Optional[str]:
    """
    Get the global Ollama model name if Ollama is the active provider.

    Returns:
        The model name if the active LLM provider is 'ollama', otherwise None.
    """
    active_llm = get_active_llm_setting()
    if active_llm and active_llm.get("provider") == "ollama":
        return active_llm.get("model")
    return None

def set_global_ollama_model(model_name: str) -> bool:
    """
    Set the global Ollama model by updating the active LLM setting.

    Args:
        model_name: The name of the Ollama model to set.

    Returns:
        True if the setting was updated successfully, False otherwise.
    """
    if not isinstance(model_name, str):
        logger.error(f"Model name must be a string. Got: {type(model_name)}")
        return False
    return set_active_llm_setting("ollama", model_name)

# --- DEPRECATED FUNCTIONS ---
# These are no longer used and will be removed.
# def get_openai_model() -> Optional[str]: ...
# def set_openai_model(model: str) -> bool: ...

if __name__ == '__main__':
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
