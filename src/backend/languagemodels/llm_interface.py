from abc import ABC, abstractmethod
from collections import defaultdict
import json
import logging
import os
import subprocess
import threading
import time
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from typing import (
    Optional,
    List,
    Dict,
    Any,
    TypedDict,
    Literal,
    Mapping,
    Sequence,
    cast,
)

import openai
import requests

from ..services.llm_api_key_resolution import get_gemini_api_key
from ..services.settings_service import get_openai_env_var, resolve_llm_setting
from ..services.model_parameter_service import (
    openai_responses_kwargs_from_model_parameters,
)
from .model_defaults import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OPENAI_MODEL,
)
from .structured_tool_calling import (
    LLMClientConfig,
    LLMResponse,
    ToolDefinition,
    get_llm_client as get_structured_client,
)
from .structured_tool_calling.client import resolve_safe_temperature_for_model
from ..utils.runtime_env import load_secret_from_env_or_file

try:  # Optional dependency (local model runtime) — imported lazily to avoid
    # the module-level Client() creation in ollama/__init__.py which calls
    # platform.machine() → WMI on Windows and can stall or fail when resources
    # are constrained.  We just probe availability here.
    import importlib.util as _ilu

    _ollama_available = _ilu.find_spec("ollama") is not None
    del _ilu
    ollama = None  # loaded on first use via _import_ollama()
except Exception:  # pragma: no cover
    _ollama_available = False
    ollama = None  # type: ignore


def _import_ollama():
    """Lazily import the ollama package on first use."""
    global ollama
    if ollama is None and _ollama_available:
        import ollama as _ollama  # type: ignore

        ollama = _ollama
    return ollama


try:
    from google import genai  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    genai = None  # type: ignore

if genai is not None:
    # Cast to Any so Pyright doesn't complain about dynamic attrs
    genai = cast(Any, genai)

# Configure logging
logger = logging.getLogger(__name__)

_RAW_LLM_IO_LOGGING_SUPPRESSED: ContextVar[bool] = ContextVar(
    "raw_llm_io_logging_suppressed",
    default=False,
)


@contextmanager
def suppress_raw_llm_io_logging(enabled: bool = True):
    """Suppress raw prompt/response persistence within a sensitive model call.

    This covers both debug logging and the exact-response replay cache.  The
    cache stores complete model output, so treating it as safe merely because
    logging is disabled would leak private document-derived content into a
    global test/replay collection.
    """

    token = _RAW_LLM_IO_LOGGING_SUPPRESSED.set(
        _RAW_LLM_IO_LOGGING_SUPPRESSED.get() or bool(enabled)
    )
    try:
        yield
    finally:
        _RAW_LLM_IO_LOGGING_SUPPRESSED.reset(token)


def _should_log_llm_io() -> bool:
    """Return True if LLM prompt/response debug logging is enabled."""

    if _RAW_LLM_IO_LOGGING_SUPPRESSED.get():
        return False
    value = os.environ.get("VON_DEBUG_LLM_IO", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _truncate_for_log(text: str, *, max_chars: int = 2000) -> str:
    if not isinstance(text, str):
        return str(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"


def resolve_llm_client_default_model(client: Any) -> Optional[str]:
    """Return the concrete default model a client uses when call model is omitted."""

    for attribute_name in ("default_model", "DEFAULT_MODEL"):
        value = getattr(client, attribute_name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_effective_llm_model_for_client(
    client: Any,
    model: Optional[str],
) -> Optional[str]:
    """Resolve the model that should be recorded for a call on this client."""

    if isinstance(model, str) and model.strip():
        return model.strip()
    return resolve_llm_client_default_model(client)


def infer_llm_client_provider(client: Any) -> Optional[str]:
    """Infer provider from a known LLM client instance for diagnostics."""

    if client is None:
        return None
    class_ref = f"{type(client).__module__}.{type(client).__name__}".lower()
    if "ollama" in class_ref:
        return "ollama"
    if "openai" in class_ref:
        return "openai"
    if "gemini" in class_ref or "google" in class_ref:
        return "gemini"
    return None


#############################################
# Global client and settings state
#############################################
_ollama_client = None
_openai_client = None
_last_openai_env_var = None
_last_openai_key = None

# Model list caching/backoff
_MODEL_CACHE: Dict[str, Dict[str, Any]] = {}
_MODEL_FAIL_BACKOFF: Dict[str, float] = defaultdict(float)
_MODEL_CACHE_TTL = int(os.getenv("LLM_MODEL_CACHE_TTL", "300"))  # seconds
_MODEL_FAIL_BACKOFF_SECONDS = int(os.getenv("LLM_MODEL_FAIL_BACKOFF", "60"))


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    token = str(raw).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, int(default))
    try:
        value = int(str(raw).strip())
    except Exception:
        return max(minimum, int(default))
    return max(minimum, value)


def _float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, float(default))
    try:
        value = float(str(raw).strip())
    except Exception:
        return max(minimum, float(default))
    return max(minimum, value)


def _resolve_ollama_auto_pull_config() -> tuple[bool, str]:
    raw_enabled = os.getenv("VON_OLLAMA_AUTO_PULL_ENABLED")
    if raw_enabled is not None:
        return _bool_env("VON_OLLAMA_AUTO_PULL_ENABLED", True), "explicit_env"
    if _bool_env("VON_AGENT_TEST_INSTANCE", False):
        return False, "agent_test_instance"
    return True, "default"


_OLLAMA_AUTO_PULL_ENABLED, _OLLAMA_AUTO_PULL_ENABLED_REASON = (
    _resolve_ollama_auto_pull_config()
)
_OLLAMA_AUTO_PULL_COOLDOWN_SECONDS = _float_env(
    "VON_OLLAMA_AUTO_PULL_COOLDOWN_SECONDS", 300.0
)
_OLLAMA_AUTO_PULL_TIMEOUT_SECONDS = _float_env(
    "VON_OLLAMA_AUTO_PULL_TIMEOUT_SECONDS",
    600.0,
    minimum=1.0,
)
_OLLAMA_AUTO_PULL_RETRY_BUDGET = _int_env(
    "VON_OLLAMA_AUTO_PULL_RETRY_BUDGET",
    1,
    minimum=0,
)

_OLLAMA_AUTO_PULL_LOCK = threading.Lock()
_OLLAMA_AUTO_PULL_STATE: Dict[str, Dict[str, Any]] = {}


def _resolve_secret_env_value(env_var: Optional[str]) -> Optional[str]:
    """Resolve an environment secret value, accepting the conventional *_FILE form."""

    if not env_var:
        return None
    return load_secret_from_env_or_file(env_var, f"{env_var}_FILE")


def _is_ollama_model_not_found_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if "not found" not in text:
        return False
    status_code = getattr(exc, "status_code", None)
    if status_code == 404:
        return True
    return "model '" in text or 'model "' in text


def _build_auto_pull_error_message(
    *,
    model_name: str,
    base_error: Exception,
    auto_pull_result: Mapping[str, Any],
) -> str:
    details = json.dumps(dict(auto_pull_result), ensure_ascii=True, sort_keys=True)
    return (
        f"Ollama model '{model_name}' is unavailable: {base_error}. "
        f"Auto-pull outcome: {details}"
    )


def _split_request_timeout_from_llm_params(
    llm_params: Optional[Mapping[str, Any]],
) -> tuple[Dict[str, Any], float | None]:
    """Separate provider options from the caller-owned request budget."""

    if not isinstance(llm_params, Mapping):
        return {}, None
    params = dict(llm_params)
    raw_timeout = params.pop("timeout_seconds", None)
    if raw_timeout is None:
        raw_timeout = params.pop("request_timeout_seconds", None)
    try:
        timeout_seconds = float(raw_timeout) if raw_timeout is not None else None
    except (TypeError, ValueError):
        timeout_seconds = None
    if timeout_seconds is not None:
        timeout_seconds = max(1.0, min(600.0, timeout_seconds))
    return params, timeout_seconds


def resolve_openai_responses_max_output_tokens(
    llm_params: Optional[Mapping[str, Any]] = None,
) -> int:
    """Return a bounded Responses output budget for ordinary LLM calls.

    The Responses API may reserve against the model's full default output
    allowance when no limit is supplied.  That can produce a misleading
    ``insufficient_quota`` failure for a large prompt even though the same key
    and model succeed with a bounded output.  Callers can override the default
    per request or with ``VON_OPENAI_RESPONSES_MAX_OUTPUT_TOKENS``.
    """

    raw_value: Any = None
    if isinstance(llm_params, Mapping):
        raw_value = llm_params.get("max_output_tokens")
        nested = llm_params.get("model_parameters")
        if raw_value is None and isinstance(nested, Mapping):
            raw_value = nested.get("max_output_tokens")
    if raw_value is None:
        raw_value = os.environ.get("VON_OPENAI_RESPONSES_MAX_OUTPUT_TOKENS", "4096")
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        parsed = 4096
    return max(16, min(100_000, parsed))


def _openai_model_defaults_to_responses(model: str | None) -> bool:
    """Return whether ordinary generation should use the Responses API.

    The 5.6 family is probed and exposed through Responses. Routing it to Chat
    Completions merely because no optional Responses parameter was supplied
    makes readiness and runtime exercise different provider surfaces.
    """

    resolved = str(resolve_openai_model_name(model) or model or "").strip().lower()
    return resolved.startswith("gpt-5.6")


def get_ollama_auto_pull_state_snapshot() -> Dict[str, Any]:
    """Return diagnostics for recent Ollama model auto-pull activity."""
    with _OLLAMA_AUTO_PULL_LOCK:
        models: Dict[str, Any] = {}
        for model_name, state in _OLLAMA_AUTO_PULL_STATE.items():
            models[str(model_name)] = {
                "in_flight": bool(state.get("in_flight", False)),
                "last_status": state.get("last_status"),
                "last_error": state.get("last_error"),
                "last_elapsed_seconds": state.get("last_elapsed_seconds"),
                "last_attempt_at_monotonic": state.get("last_attempt_at_monotonic"),
                "window_started_at_monotonic": state.get("window_started_at_monotonic"),
                "attempts_in_window": int(state.get("attempts_in_window", 0) or 0),
                "last_waiter_count": int(state.get("last_waiter_count", 0) or 0),
                "last_caller_wait_timeout_seconds": state.get(
                    "last_caller_wait_timeout_seconds"
                ),
                "caller_wait_timeout_count": int(
                    state.get("caller_wait_timeout_count", 0) or 0
                ),
            }
    return {
        "enabled": bool(_OLLAMA_AUTO_PULL_ENABLED),
        "enabled_reason": str(_OLLAMA_AUTO_PULL_ENABLED_REASON),
        "cooldown_seconds": float(_OLLAMA_AUTO_PULL_COOLDOWN_SECONDS),
        "timeout_seconds": float(_OLLAMA_AUTO_PULL_TIMEOUT_SECONDS),
        "retry_budget": int(_OLLAMA_AUTO_PULL_RETRY_BUDGET),
        "models": models,
    }


#############################################
# Internal message schema & adapters
#############################################

AllowedRole = Literal["system", "user", "assistant", "tool", "model"]


class LLMMessage(TypedDict):
    role: AllowedRole
    content: str


def _coerce_context(raw: Optional[Sequence[Dict[str, Any]]]) -> List[LLMMessage]:
    messages: List[LLMMessage] = []
    if not raw:
        return messages
    for msg in raw:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if not isinstance(content, str) or content == "":
            continue
        if role not in ("system", "user", "assistant", "tool", "model"):
            role = "user"
        messages.append({"role": role, "content": content})
    return messages


def build_conversation(
    prompt: str, context: Optional[Sequence[Dict[str, Any]]]
) -> List[LLMMessage]:
    base = _coerce_context(context)
    base.append({"role": "user", "content": prompt})
    return base


def to_openai_messages(conv: Sequence[LLMMessage]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in conv:
        role = m["role"]
        # Map 'model' to 'assistant'; 'tool' currently downgraded to 'assistant'
        if role == "model":
            role = "assistant"
        if role == "tool":
            role = "assistant"
        if role not in ("system", "user", "assistant"):
            role = "user"
        out.append({"role": role, "content": m["content"]})
    return out


def _read_response_field(value: Any, field_name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(field_name)
    return getattr(value, field_name, None)


def _extract_openai_responses_text(response: Any) -> Optional[str]:
    direct_text = _read_response_field(response, "output_text")
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text.strip()

    output = _read_response_field(response, "output")
    if not isinstance(output, Sequence) or isinstance(output, (str, bytes)):
        return None
    chunks: list[str] = []
    for item in output:
        content = _read_response_field(item, "content")
        if isinstance(content, str) and content.strip():
            chunks.append(content.strip())
            continue
        if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
            continue
        for part in content:
            text = _read_response_field(part, "text")
            if not isinstance(text, str):
                text = _read_response_field(part, "output_text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip() or None


def to_ollama_messages(conv: Sequence[LLMMessage]) -> List[Dict[str, str]]:
    # Ollama accepts the same shape; just normalize roles ('model'→'assistant')
    out: List[Dict[str, str]] = []
    for m in conv:
        role = m["role"]
        if role == "model":
            role = "assistant"
        out.append({"role": role, "content": m["content"]})
    return out


def to_gemini_history(conv: Sequence[LLMMessage]) -> List[Dict[str, Any]]:
    history: List[Dict[str, Any]] = []
    for m in conv:
        role = m["role"]
        if role in ("assistant", "model", "tool"):
            gem_role = "model"
        elif role in ("system", "user"):
            gem_role = "user" if role != "system" else "user"
        else:
            gem_role = "user"
        history.append({"role": gem_role, "parts": [{"text": m["content"]}]})
    return history


_OPENAI_MODEL_PREFIXES = (
    "gpt-",
    "o1-",
    "o1",
    "o3-",
    "o3",
    "o4-",
    "o4",
    "chatgpt-",
    "text-",
    "davinci",
    "curie",
    "babbage",
    "ada",
)


def _looks_like_openai_model(name: str) -> bool:
    return isinstance(name, str) and name.startswith(_OPENAI_MODEL_PREFIXES)


def _looks_like_browser_object_model_reference(value: str) -> bool:
    """Detect accidental DOM/Event object stringification in model settings."""

    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    for prefix in ("openai:", "ollama:", "gemini:"):
        if lowered.startswith(prefix):
            cleaned = cleaned.split(":", 1)[1].strip()
            break
    return cleaned.startswith("[object ") and cleaned.endswith("]")


def _extract_openai_model_id(value: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower().startswith("openai:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if _looks_like_browser_object_model_reference(cleaned):
        return None
    return cleaned or None


def _extract_ollama_model_id(value: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower().startswith("ollama:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    if _looks_like_browser_object_model_reference(cleaned):
        return None
    return cleaned or None


def _looks_like_ollama_model(name: str) -> bool:
    if not isinstance(name, str):
        return False
    raw = name.strip()
    if not raw:
        return False
    if raw.startswith("#V#"):
        return False
    if raw.lower().startswith(("openai:", "ft:")):
        return False
    direct = _extract_openai_model_id(raw)
    if direct and _looks_like_openai_model(direct):
        return False
    return ":" in raw


def resolve_provider_from_model_concept(model: Optional[str]) -> Optional[str]:
    """Resolve provider from a model concept's #V#has_provider relation."""
    if not isinstance(model, str):
        return None
    model_id = model.strip()
    if not model_id or not model_id.startswith("#V#"):
        return None

    try:
        from ..services.concept_service import get_concept_by_concept_id
        from ..services.text_value_service import get_texts_for_concept
        from ..security.access_control import bypass_access_control

        with bypass_access_control():
            concept = get_concept_by_concept_id(model_id)
        relationships = (
            concept.get("relationships") if isinstance(concept, dict) else None
        )
        if not isinstance(relationships, dict):
            return None
        providers = relationships.get("#V#has_provider")
        if not isinstance(providers, list) or not providers:
            return None
        provider_id = providers[0]
        if not isinstance(provider_id, str):
            return None

        with bypass_access_control():
            names = get_texts_for_concept(provider_id, predicate="hasName", limit=10)
    except Exception:
        return None

    for row in names:
        if not isinstance(row, dict):
            continue
        text = row.get("text")
        if not isinstance(text, str):
            continue
        lowered = text.lower()
        if "openai" in lowered:
            return "openai"
        if "ollama" in lowered:
            return "ollama"
        if "gemini" in lowered or "google" in lowered:
            return "gemini"

    return None


def resolve_ollama_model_name(model: Optional[str]) -> Optional[str]:
    """Resolve Ollama model IDs from Vontology concept IDs or prefixed names."""
    if not isinstance(model, str):
        return model
    raw = model.strip()
    if not raw:
        return raw
    if _looks_like_browser_object_model_reference(raw):
        return None

    direct = _extract_ollama_model_id(raw)
    if direct and _looks_like_ollama_model(direct):
        return direct

    if raw.startswith("#V#"):
        try:
            from ..services.text_value_service import get_texts_for_concept
            from ..security.access_control import bypass_access_control

            with bypass_access_control():
                rows = get_texts_for_concept(raw, predicate="hasName", limit=20)
        except Exception:
            rows = []

        for row in rows:
            if not isinstance(row, dict):
                continue
            text = row.get("text")
            if not isinstance(text, str):
                continue
            candidate = _extract_ollama_model_id(text)
            if candidate and _looks_like_ollama_model(candidate):
                return candidate

        for row in rows:
            if not isinstance(row, dict):
                continue
            text = row.get("text")
            if not isinstance(text, str):
                continue
            candidate = _extract_ollama_model_id(text)
            if candidate:
                return candidate

    return direct or raw


def resolve_openai_model_name(model: Optional[str]) -> Optional[str]:
    """Resolve OpenAI model IDs from Vontology concept IDs or prefixed names."""
    if not isinstance(model, str):
        return model
    raw = model.strip()
    if not raw:
        return raw
    if _looks_like_browser_object_model_reference(raw):
        return None

    direct = _extract_openai_model_id(raw)
    if direct and _looks_like_openai_model(direct):
        return direct

    if raw.startswith("#V#"):
        try:
            from ..services.text_value_service import get_texts_for_concept
            from ..security.access_control import bypass_access_control

            with bypass_access_control():
                rows = get_texts_for_concept(raw, predicate="hasName", limit=20)
        except Exception:
            rows = []

        for row in rows:
            if not isinstance(row, dict):
                continue
            text = row.get("text")
            if not isinstance(text, str):
                continue
            candidate = _extract_openai_model_id(text)
            if candidate and _looks_like_openai_model(candidate):
                return candidate

        for row in rows:
            if not isinstance(row, dict):
                continue
            text = row.get("text")
            if not isinstance(text, str):
                continue
            candidate = _extract_openai_model_id(text)
            if candidate:
                return candidate

    return direct or raw


def _resolve_effective_llm_actor_scope(
    user_concept_id: Optional[str] = None,
    org_concept_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve actor scope in HTTP and background workflow contexts alike."""

    resolved_user = user_concept_id
    resolved_org = org_concept_id
    if resolved_user and resolved_org:
        return resolved_user, resolved_org
    try:
        from ..security.access_control import (
            get_effective_organisation_concept_id,
            get_effective_user_concept_id,
        )

        if not resolved_user:
            resolved_user = get_effective_user_concept_id()
        if not resolved_org:
            resolved_org = get_effective_organisation_concept_id()
    except Exception:
        pass
    return resolved_user, resolved_org


def initialize_clients(force: bool = False):
    """
    Initialize LLM clients based on settings.
    Args:
        force: If True, reinitialize clients even if they exist
    """
    global _ollama_client, _openai_client, _last_openai_env_var, _last_openai_key

    current_env_var = get_openai_env_var()
    current_key = _resolve_secret_env_value(current_env_var)

    # Check if OpenAI settings have changed
    env_var_changed = current_env_var != _last_openai_env_var
    key_changed = current_key != _last_openai_key

    openai_settings_changed = force or env_var_changed or key_changed

    if openai_settings_changed:
        logger.info("OpenAI configuration has changed, re-evaluating client.")

    # Initialize or reinitialize OpenAI client if needed
    if _openai_client is None or openai_settings_changed:
        try:
            if current_env_var and current_key:
                # Validate against the same actor-scoped model that a durable
                # workflow will use. Background execution carries this scope in
                # access-control ContextVars rather than a Flask session.
                user_concept_id, org_concept_id = _resolve_effective_llm_actor_scope()
                active_llm = resolve_llm_setting(
                    user_concept_id=user_concept_id, org_concept_id=org_concept_id
                )
                current_model = active_llm.get("model", "") if active_llm else ""
                resolved_model = resolve_openai_model_name(current_model)

                # Validate configuration and emit warnings
                is_valid, error_messages = validate_openai_config(
                    current_env_var, resolved_model or current_model, current_key
                )
                for message in error_messages:
                    # Avoid false-positive model name warnings when the model clearly matches allowed prefixes
                    if message.startswith("Unusual OpenAI model name format"):
                        try:
                            safe_model = current_model or ""
                            if isinstance(safe_model, str) and safe_model.startswith(
                                _OPENAI_MODEL_PREFIXES
                            ):
                                # Skip this noisy warning
                                continue
                        except Exception:
                            pass
                    warnings.warn(message, UserWarning)
                    logger.warning(message)

                _openai_client = OpenAIClient(
                    api_key=current_key,
                    api_key_env_var=current_env_var,
                )
                logger.info("OpenAI client initialized/reinitialized.")
                _last_openai_env_var = current_env_var
                _last_openai_key = current_key
            else:
                # Emit warning for missing API key
                if not current_key:
                    warnings.warn("No API key found", UserWarning)
                    logger.warning("No API key found")
                _openai_client = None
                logger.info("OpenAI client not initialized (no API key found).")
        except Exception as e:
            _openai_client = None
            logger.warning(f"Could not initialize OpenAI client: {e}")

    # Ollama client is created lazily by _ensure_ollama_client() when first
    # needed (i.e. when get_llm_client selects the Ollama provider).  This
    # avoids a 30 s+ hang at startup if Ollama is not running locally.
    if force and _ollama_client is not None:
        _ollama_client = None  # force re-creation on next access


def _ensure_ollama_client() -> Optional["OllamaClient"]:
    """Lazily create the global OllamaClient on first access."""
    global _ollama_client
    if _ollama_client is None:
        try:
            _ollama_client = OllamaClient()
            logger.info("Ollama client initialized successfully (lazy).")
        except Exception as e:
            logger.error(f"Failed to initialize Ollama client: {e}")
            _ollama_client = None
    return _ollama_client


class LLMInterface(ABC):
    """Abstract Base Class for Language Model interactions."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        context: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        llm_params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate a response based on the prompt and optional context and parameters."""
        pass

    def generate_with_tools(
        self,
        prompt: str,
        available_tools: List[ToolDefinition],
        context: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        system_message: Optional[str] = None,
        llm_params: Optional[Dict[str, Any]] = None,
        **provider_options: Any,
    ) -> LLMResponse:
        """Generate response with structured tool calling support (JVNAUTOSCI-799).

        This method provides provider-native tool calling when supported, with
        graceful fallback to JSON-in-text parsing for unsupported providers.

        Args:
            prompt: User query or instruction
            available_tools: List of tools the model can invoke
            context: Prior conversation history
            model: Model name override
            system_message: System-level instructions
            llm_params: Model-parameter bundle for provider-boundary translation
            **provider_options: Provider-specific structured-call options

        Returns:
            LLMResponse with text and/or structured tool calls

        Note:
            Default implementation uses structured_tool_calling module.
            Subclasses can override for provider-specific optimisations.
        """
        # Feature flag check
        if not self._should_use_structured_calling():
            # Fallback to legacy JSON-in-text
            raise NotImplementedError(
                "generate_with_tools() requires VON_INTERNAL_MCP_STRUCTURED_TOOL_CALLING=1"
            )

        # Default implementation delegates to structured_tool_calling module
        config = self._get_structured_client_config(model)
        client = get_structured_client(config)
        structured_options = dict(provider_options)
        if llm_params:
            structured_options["llm_params"] = dict(llm_params)
        return client.generate_with_tools_sync(
            prompt=prompt,
            available_tools=available_tools,
            system_message=system_message,
            context=self._convert_context_for_structured_client(context),
            **structured_options,
        )

    def _should_use_structured_calling(self) -> bool:
        """Check if structured tool calling is enabled via feature flag."""
        return os.environ.get("VON_INTERNAL_MCP_STRUCTURED_TOOL_CALLING", "1") == "1"

    def _get_structured_client_config(self, model: Optional[str]) -> LLMClientConfig:
        """Get configuration for structured tool calling client.

        Subclasses should override to provide provider-specific config.
        """
        raise NotImplementedError(
            "Subclass must implement _get_structured_client_config()"
        )

    def _convert_context_for_structured_client(
        self, context: Optional[List[Dict[str, Any]]]
    ) -> Optional[List[Dict[str, Any]]]:
        """Convert LLMInterface context format to structured client format.

        Args:
            context: List of message dicts with 'role' and 'content' keys

        Returns:
            List suitable for structured client (sequence of message dicts)
        """
        # Default: pass through as-is (structured clients expect list of messages)
        return context

    @abstractmethod
    def list_models(self) -> List[str]:
        """List available models for this interface."""
        pass

    @abstractmethod
    def get_embedding(self, text: str, model: Optional[str] = None) -> List[float]:
        """Generate an embedding for the given text."""
        pass


class OllamaClient(LLMInterface):
    """Client for interacting with local Ollama models."""

    def __init__(
        self, host: Optional[str] = None, default_model: str = DEFAULT_OLLAMA_MODEL
    ):
        _ollama_mod = _import_ollama()
        if _ollama_mod is None:
            raise RuntimeError(
                "The 'ollama' package is not installed. Install with 'pip install ollama' or disable Ollama usage in settings."
            )
        # Import inside method to avoid circular import
        from ..services.settings_service import get_active_ollama_host

        # Resolve the host for direct API calls (e.g., list_models) and logging
        # Prioritize explicit host, then active host from settings, then OLLAMA_HOST env var, then default
        resolved_host = host
        if resolved_host is None:  # If no host is explicitly passed
            # Try to get active host from settings
            resolved_host = get_active_ollama_host()
            if resolved_host is None:  # If not in settings, try environment variable
                resolved_host = os.environ.get("OLLAMA_HOST")
                if resolved_host is None:  # If not in env either, use default
                    resolved_host = "http://localhost:11434"

        # Normalize the host URL to ensure proper format for API calls
        self.host = self._normalize_host_url(
            resolved_host
        )  # Store the normalized host for direct API calls or logging
        self.default_model = default_model

        # Instantiate the ollama.Client.
        # The official ollama-python library client will automatically use
        # the OLLAMA_HOST environment variable if set, or its own internal default
        # if no host is provided. We now explicitly pass the resolved host.
        try:
            self.client = _ollama_mod.Client(host=self.host)
            logger.info(f"OllamaClient initialized for host: {self.host}")
        except TypeError as e:
            logger.error(
                f"Error initializing ollama.Client for host {self.host}: {e}. This might indicate an issue with the ollama library version or environment setup."
            )
            # Potentially re-raise or handle as a critical failure
            raise
        except Exception as e:  # Catch other potential exceptions during client init
            logger.error(
                f"An unexpected error occurred during OllamaClient initialization for host {self.host}: {e}"
            )
            raise

    def _get_structured_client_config(self, model: Optional[str]) -> LLMClientConfig:
        """Get configuration for structured tool calling client (JVNAUTOSCI-799)."""
        return LLMClientConfig(
            model=model or self.default_model,
            provider="ollama",
            base_url=self.host,
            temperature=0.7,
        )

    def _normalize_host_url(self, host: str) -> str:
        """
        Normalize a host URL to ensure proper format for API calls.

        Args:
            host: Host URL in various formats (IP, hostname, with/without protocol/port)

        Returns:
            Properly formatted URL with protocol and default port if needed
        """
        if not host:
            return "http://localhost:11434"

        # If already has protocol, use as-is
        if host.startswith(("http://", "https://")):
            return host

        # If it's just an IP or hostname, add protocol and default port
        if ":" not in host:
            # Just IP/hostname, add default port
            return f"http://{host}:11434"
        elif "://" not in host:
            # Has port but no protocol
            return f"http://{host}"

        return host

    def generate(
        self,
        prompt: str,
        context: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
        llm_params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate a response using an Ollama model. Implements retry with backoff for specific errors."""
        # REFACTORING_NOTE: Model is now determined by the get_llm_client factory.
        # This method just uses the model passed to it.
        target_model = model or self.default_model

        # Handle global model format that might include host information
        if target_model and " - " in target_model:
            # Extract just the model name if it's in "host - modelname" format
            parts = target_model.split(" - ", 1)
            if len(parts) == 2:
                potential_host = parts[0].strip()
                # Validate that it looks like a host (IP or hostname)
                if (
                    "." in potential_host and not potential_host.startswith("http")
                ) or ":" in potential_host:
                    target_model = parts[1].strip()
                    logger.info(
                        f"Extracted model name '{target_model}' from global model format"
                    )

        if not target_model:
            error_msg = "No Ollama model specified."
            logger.error(error_msg)
            raise ValueError(error_msg)

        ollama_params, request_timeout_seconds = _split_request_timeout_from_llm_params(
            llm_params
        )
        request_started = time.monotonic()
        request_deadline = (
            request_started + request_timeout_seconds
            if request_timeout_seconds is not None
            else None
        )

        def _remaining_request_seconds(*, reserve_seconds: float = 0.0) -> float | None:
            if request_deadline is None:
                return None
            return max(0.0, request_deadline - time.monotonic() - reserve_seconds)

        # JVNAUTOSCI-2506: opt-in exact-match response cache for test/replay
        # loops. Enabled only via VON_LLM_RESPONSE_CACHE or on agent-test
        # instances; hits are logged and counted so telemetry can expose them.
        from ..services.llm_response_cache_service import (
            build_llm_prompt_key,
            build_llm_response_cache_key,
            get_cached_llm_response,
            is_llm_response_cache_enabled,
            store_llm_response,
        )

        response_cache_key: Optional[str] = None
        response_prompt_key: Optional[str] = None
        if (
            is_llm_response_cache_enabled()
            and not _RAW_LLM_IO_LOGGING_SUPPRESSED.get()
        ):
            _cache_conv = build_conversation(prompt, context)
            _cache_messages = to_ollama_messages(_cache_conv)
            response_cache_key = build_llm_response_cache_key(
                provider="ollama",
                host=self.host,
                model=target_model,
                messages=_cache_messages,
                options=ollama_params,
            )
            response_prompt_key = build_llm_prompt_key(
                messages=_cache_messages,
                options=llm_params,
            )
            cached_entry = get_cached_llm_response(response_cache_key)
            if cached_entry is not None:
                logger.info(
                    "Returning cached Ollama response for model %s "
                    "(llm_response_cache, original_duration_ms=%s)",
                    target_model,
                    cached_entry.get("original_duration_ms"),
                )
                return str(cached_entry.get("response_text") or "")

        # JVNAUTOSCI-2384: preflight whether this local model plausibly fits the
        # host before attempting to load it.  Fails closed with a clear reason
        # rather than letting the runtime crash under memory pressure.  Never
        # substitutes a different model.
        from .local_model_preflight import enforce_local_model_preflight

        enforce_local_model_preflight(
            target_model,
            ollama_client=self.client,
            host=self.host,
        )

        logger.info(f"Generating response using Ollama model: {target_model}")
        if _should_log_llm_io():
            logger.debug(
                "[LLM PROMPT][Ollama][%s]: %s", target_model, _truncate_for_log(prompt)
            )

        conv = build_conversation(prompt, context)
        messages = to_ollama_messages(conv)

        ollama_options = {}
        if ollama_params:
            # Only pass parameters that are valid for ollama.Client.chat options
            valid_ollama_options = [
                "mirostat",
                "mirostat_eta",
                "mirostat_tau",
                "num_ctx",
                "num_gpu",
                "num_thread",
                "repeat_last_n",
                "repeat_penalty",
                "temperature",
                "seed",
                "stop",
                "tfs_z",
                "num_predict",
                "top_k",
                "top_p",
            ]
            for key, value in ollama_params.items():
                if key in valid_ollama_options:
                    ollama_options[key] = value
                else:
                    logger.warning(
                        f"Ignoring unknown llm_param: {key} for Ollama client"
                    )

        max_retries = 3
        base_delay = 1  # seconds
        auto_pull_retry_consumed = False

        for attempt in range(max_retries):
            try:
                # Ensure model is available locally (optional, can be slow)
                # self._ensure_model_pulled(target_model)

                _generate_started = time.perf_counter()
                remaining_request_seconds = _remaining_request_seconds()
                if (
                    remaining_request_seconds is not None
                    and remaining_request_seconds <= 0
                ):
                    raise TimeoutError(
                        "ollama_request_timeout: caller request budget exhausted"
                    )
                request_client = self.client
                if remaining_request_seconds is not None:
                    _ollama_mod = _import_ollama()
                    client_type = (
                        getattr(_ollama_mod, "Client", None)
                        if _ollama_mod is not None
                        else None
                    )
                    if callable(client_type):
                        request_client = client_type(
                            host=self.host,
                            timeout=max(0.1, remaining_request_seconds),
                        )
                response = request_client.chat(
                    model=target_model,
                    messages=messages,
                    options=(
                        ollama_options if ollama_options else None
                    ),  # Pass options if any
                )
                if _should_log_llm_io():
                    logger.debug("Ollama raw response: %s", response)
                content = response["message"]["content"]
                if _should_log_llm_io():
                    logger.debug(
                        "[LLM RESPONSE][Ollama][%s]: %s",
                        target_model,
                        _truncate_for_log(content),
                    )
                if response_cache_key is not None:
                    store_llm_response(
                        response_cache_key,
                        provider="ollama",
                        host=self.host,
                        model=target_model,
                        response_text=content,
                        duration_ms=(
                            (time.perf_counter() - _generate_started) * 1000.0
                        ),
                        prompt_key=response_prompt_key,
                    )
                return content
            except Exception as e:
                # Handle known Ollama ResponseError distinctly if library present
                _ollama_mod = _import_ollama()
                ollama_resp_err_cls = (
                    getattr(_ollama_mod, "ResponseError", None)
                    if _ollama_mod is not None
                    else None
                )
                if ollama_resp_err_cls and isinstance(e, ollama_resp_err_cls):
                    logger.warning(
                        f"Ollama ResponseError on attempt {attempt + 1}/{max_retries}: {e} (Status: {getattr(e, 'status_code', 'n/a')})"
                    )
                    if (
                        _is_ollama_model_not_found_error(e)
                        and not auto_pull_retry_consumed
                    ):
                        auto_pull_retry_consumed = True
                        auto_pull_result = self._attempt_model_auto_pull(
                            target_model,
                            wait_timeout_seconds=_remaining_request_seconds(
                                reserve_seconds=0.5
                            ),
                        )
                        if bool(auto_pull_result.get("succeeded")):
                            logger.info(
                                "Ollama auto-pull recovered missing model for generation model=%s details=%s",
                                target_model,
                                json.dumps(
                                    dict(auto_pull_result),
                                    ensure_ascii=True,
                                    sort_keys=True,
                                ),
                            )
                            continue
                        logger.error(
                            "Ollama auto-pull failed for generation model=%s details=%s",
                            target_model,
                            json.dumps(
                                dict(auto_pull_result),
                                ensure_ascii=True,
                                sort_keys=True,
                            ),
                        )
                        raise RuntimeError(
                            _build_auto_pull_error_message(
                                model_name=target_model,
                                base_error=e,
                                auto_pull_result=auto_pull_result,
                            )
                        ) from e
                    if (
                        "CUDA error" in str(e)
                        and getattr(e, "status_code", None) == 500
                        and attempt < max_retries - 1
                    ):
                        delay = base_delay * (2**attempt)
                        remaining_request_seconds = _remaining_request_seconds()
                        if (
                            remaining_request_seconds is not None
                            and remaining_request_seconds <= delay
                        ):
                            raise RuntimeError(
                                "ollama_request_timeout: retry backoff would exceed "
                                "the caller request budget"
                            ) from e
                        logger.info(
                            f"Retrying after {delay} seconds due to CUDA error (500)..."
                        )
                        time.sleep(delay)
                        continue
                    logger.error(
                        f"Failed to generate response with Ollama model {target_model} after {attempt + 1} attempts due to ResponseError: {e}",
                        exc_info=True,
                    )
                    raise RuntimeError(f"Ollama ResponseError: {str(e)}") from e
                # Generic unexpected error path
                logger.error(
                    f"Unexpected error on attempt {attempt + 1}/{max_retries} with Ollama model {target_model}: {e}",
                    exc_info=True,
                )
                if attempt < max_retries - 1:
                    delay = base_delay * (2**attempt)
                    remaining_request_seconds = _remaining_request_seconds()
                    if (
                        remaining_request_seconds is not None
                        and remaining_request_seconds <= delay
                    ):
                        raise RuntimeError(
                            "ollama_request_timeout: retry backoff would exceed "
                            "the caller request budget"
                        ) from e
                    logger.info(
                        f"Retrying after {delay} seconds due to unexpected error..."
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Ollama unexpected error: {str(e)}") from e

        # This line should only be reached if all retries fail and an error wasn't re-raised properly inside the loop.
        # It acts as a final fallback.
        logger.error(
            f"Failed to generate response with Ollama model {target_model} after all {max_retries} retries."
        )
        raise RuntimeError(
            f"Ollama error: Failed after {max_retries} retries for model {target_model}."
        )

    def get_embedding(self, text: str, model: Optional[str] = None) -> List[float]:
        """Generate an embedding using an Ollama model."""
        target_model = model or self.default_model
        # Handle global model format
        if target_model and " - " in target_model:
            parts = target_model.split(" - ", 1)
            if len(parts) == 2:
                potential_host = parts[0].strip()
                if (
                    "." in potential_host and not potential_host.startswith("http")
                ) or ":" in potential_host:
                    target_model = parts[1].strip()

        if not target_model:
            raise ValueError("No Ollama model specified for embedding.")

        auto_pull_retry_consumed = False
        while True:
            try:
                response = self.client.embeddings(model=target_model, prompt=text)
                return response["embedding"]
            except Exception as e:
                if _is_ollama_model_not_found_error(e) and not auto_pull_retry_consumed:
                    auto_pull_retry_consumed = True
                    auto_pull_result = self._attempt_model_auto_pull(target_model)
                    if bool(auto_pull_result.get("succeeded")):
                        logger.info(
                            "Ollama auto-pull recovered missing model for embedding model=%s details=%s",
                            target_model,
                            json.dumps(
                                dict(auto_pull_result),
                                ensure_ascii=True,
                                sort_keys=True,
                            ),
                        )
                        continue
                    logger.error(
                        "Ollama auto-pull failed for embedding model=%s details=%s",
                        target_model,
                        json.dumps(
                            dict(auto_pull_result),
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                    )
                    raise RuntimeError(
                        _build_auto_pull_error_message(
                            model_name=target_model,
                            base_error=e,
                            auto_pull_result=auto_pull_result,
                        )
                    ) from e

                logger.error(
                    f"Failed to generate embedding with Ollama model {target_model}: {e}"
                )
                raise RuntimeError(f"Ollama embedding error: {str(e)}") from e

    def list_models(self, force_refresh: bool = False) -> List[str]:
        """List available Ollama models (cached with backoff)."""
        now = time.time()
        cache_key = f"ollama|{self.host}"
        if now < _MODEL_FAIL_BACKOFF.get(cache_key, 0):
            cached = _MODEL_CACHE.get(cache_key)
            if cached:
                logger.debug(
                    "Serving stale Ollama model list due to backoff host=%s", self.host
                )
                return cached["models"]
            return []
        cached = _MODEL_CACHE.get(cache_key)
        if not force_refresh and cached and (now - cached["ts"]) < _MODEL_CACHE_TTL:
            logger.debug(
                "Ollama model cache hit host=%s age=%.1fs",
                self.host,
                now - cached["ts"],
            )
            return cached["models"]
        url = f"{self.host.rstrip('/')}/api/tags"
        headers = {"Accept": "application/json"}
        t0 = time.time()
        try:
            logger.info("Fetching Ollama models host=%s", self.host)
            resp = requests.get(url, headers=headers, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            models: List[str] = []
            if isinstance(data, dict):
                raw_models = data.get("models") or data.get("data") or []
                if isinstance(raw_models, list):
                    for m in raw_models:
                        if isinstance(m, dict):
                            name = m.get("name") or m.get("model") or m.get("id")
                            if isinstance(name, str):
                                models.append(name)
            build_sec = time.time() - t0
            _MODEL_CACHE[cache_key] = {
                "models": models,
                "ts": now,
                "build_sec": build_sec,
            }
            logger.info(
                "Ollama models fetched host=%s count=%d build=%.2fs",
                self.host,
                len(models),
                build_sec,
            )
            return models
        except Exception as e:
            logger.error("Error contacting Ollama API host=%s err=%s", self.host, e)
            _MODEL_FAIL_BACKOFF[cache_key] = time.time() + _MODEL_FAIL_BACKOFF_SECONDS
            return cached["models"] if cached else []

    def list_models_with_host_info(self) -> List[Dict[str, str]]:
        """List models with host information for display in UI."""
        logger.info(f"Listing Ollama models with host info from {self.host}")
        try:
            response = requests.get(f"{self.host}/api/tags")
            response.raise_for_status()
            models_data = response.json().get("models", [])

            # Extract host name for display
            if "://" in self.host:
                host_name = self.host.split("://")[1].split(":")[0]
            else:
                host_name = self.host.split(":")[0]

            models_with_info = []
            for model in models_data:
                if isinstance(model, dict) and "name" in model:
                    models_with_info.append(
                        {
                            "name": model["name"],
                            "host_url": self.host,
                            "host_name": host_name,
                            "display_name": f"{host_name} - {model['name']}",
                        }
                    )

            logger.info(f"Found {len(models_with_info)} Ollama models with host info")
            return models_with_info
        except requests.RequestException as e:
            logger.error(f"Error contacting Ollama API at {self.host}: {e}")
            return []
        except Exception as e:
            logger.error(f"Error parsing Ollama models list: {e}", exc_info=True)
            return []

    @staticmethod
    def list_models_from_all_hosts() -> List[Dict[str, str]]:
        """List models from all configured Ollama hosts."""
        from ..services.settings_service import (
            get_ollama_hosts_list,
            get_disable_remote_ollama_scan,
        )

        all_models: List[Dict[str, str]] = []
        hosts_list = get_ollama_hosts_list()
        remote_scan_disabled = get_disable_remote_ollama_scan()

        if remote_scan_disabled:
            logger.info(
                "Remote Ollama host scanning is disabled - only scanning local hosts"
            )

        for host_info in hosts_list:
            # Skip remote hosts if remote scanning is disabled
            if remote_scan_disabled and not host_info.get("is_local", False):
                logger.debug(
                    f"Skipping remote host {host_info['url']} - remote scanning disabled"
                )
                continue

            try:
                # Create a temporary client for this host
                temp_client = OllamaClient(host=host_info["url"])
                models_with_info = temp_client.list_models_with_host_info()
                all_models.extend(models_with_info)
            except Exception as e:
                logger.error(f"Error getting models from host {host_info['url']}: {e}")
                continue

        logger.info(f"Found total of {len(all_models)} models across all hosts")
        return all_models

    def _ensure_model_pulled(self, model_name: str):
        """Helper to pull an Ollama model if not present (can be slow)."""
        try:
            logger.info(f"Checking/Pulling Ollama model: {model_name}")
            self.client.pull(model_name)  # Use the client instance
            logger.info(f"Model {model_name} is available.")
        except Exception as e:
            logger.error(f"Failed to pull Ollama model {model_name}: {e}")
            # Decide if you want to raise the error or just log it

    def _is_model_available(self, model_name: str) -> bool:
        model_token = str(model_name or "").strip()
        if not model_token:
            return False
        models = self.list_models(force_refresh=True)
        if model_token in models:
            return True
        token_prefix = model_token.split(":", 1)[0]
        return any(str(item).split(":", 1)[0] == token_prefix for item in models)

    def _pull_model(self, model_name: str) -> None:
        command = ["ollama", "pull", model_name]
        try:
            process = subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                text=True,
                timeout=_OLLAMA_AUTO_PULL_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            # Fall back to Python client pull if CLI is unavailable.
            self.client.pull(model_name)
            return
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"ollama pull timed out for {model_name} after "
                f"{_OLLAMA_AUTO_PULL_TIMEOUT_SECONDS:.1f}s"
            ) from exc

        if process.returncode != 0:
            stderr_text = str(process.stderr or "").strip()
            stdout_text = str(process.stdout or "").strip()
            detail = stderr_text or stdout_text or f"return_code={process.returncode}"
            raise RuntimeError(f"ollama pull failed for {model_name}: {detail}")

    def _complete_model_auto_pull(
        self,
        *,
        model_token: str,
        event: threading.Event,
    ) -> None:
        """Complete one shared pull without retaining any caller's deadline."""

        pull_started = time.monotonic()
        pull_error: str | None = None
        pull_succeeded = False
        try:
            if not self._is_model_available(model_token):
                self._pull_model(model_token)
            pull_succeeded = self._is_model_available(model_token)
            if not pull_succeeded:
                pull_error = (
                    f"Model '{model_token}' still unavailable after auto-pull attempt."
                )
        except Exception as exc:
            pull_error = str(exc)
            pull_succeeded = False
        pull_elapsed = round(time.monotonic() - pull_started, 3)

        with _OLLAMA_AUTO_PULL_LOCK:
            state = _OLLAMA_AUTO_PULL_STATE.get(model_token, {})
            if state.get("event") is not event:
                return
            state["in_flight"] = False
            state["last_status"] = "succeeded" if pull_succeeded else "failed"
            state["last_error"] = pull_error
            state["last_elapsed_seconds"] = pull_elapsed
            event.set()

    def _attempt_model_auto_pull(
        self,
        model_name: str,
        *,
        wait_timeout_seconds: float | None = None,
    ) -> Dict[str, Any]:
        model_token = str(model_name or "").strip()
        now = time.monotonic()
        result: Dict[str, Any] = {
            "attempted": False,
            "succeeded": False,
            "failed": False,
            "model": model_token,
            "elapsed_seconds": 0.0,
            "retry_outcome": "not_attempted",
        }
        if not model_token:
            result.update(
                {
                    "failed": True,
                    "retry_outcome": "invalid_model",
                    "error": "missing model name",
                }
            )
            return result

        if not _OLLAMA_AUTO_PULL_ENABLED:
            result.update(
                {
                    "retry_outcome": "auto_pull_disabled",
                    "disabled_reason": str(_OLLAMA_AUTO_PULL_ENABLED_REASON),
                }
            )
            return result

        with _OLLAMA_AUTO_PULL_LOCK:
            state = _OLLAMA_AUTO_PULL_STATE.setdefault(
                model_token,
                {
                    "in_flight": False,
                    "event": threading.Event(),
                    "window_started_at_monotonic": now,
                    "attempts_in_window": 0,
                    "last_status": None,
                    "last_error": None,
                    "last_elapsed_seconds": 0.0,
                    "last_attempt_at_monotonic": None,
                    "last_waiter_count": 0,
                    "last_caller_wait_timeout_seconds": None,
                    "caller_wait_timeout_count": 0,
                },
            )

            in_flight = bool(state.get("in_flight", False))
            event = state.get("event")
            if not isinstance(event, threading.Event):
                event = threading.Event()
                state["event"] = event

            if in_flight:
                state["last_waiter_count"] = (
                    int(state.get("last_waiter_count", 0) or 0) + 1
                )
                waiter_event = event
                owns_pull = False
            else:
                owns_pull = True
                window_started = float(state.get("window_started_at_monotonic") or now)
                attempts_in_window = int(state.get("attempts_in_window", 0) or 0)
                if (now - window_started) >= _OLLAMA_AUTO_PULL_COOLDOWN_SECONDS:
                    window_started = now
                    attempts_in_window = 0

                if attempts_in_window >= _OLLAMA_AUTO_PULL_RETRY_BUDGET:
                    state["window_started_at_monotonic"] = window_started
                    state["attempts_in_window"] = attempts_in_window
                    state["last_status"] = "skipped_retry_budget"
                    state["last_error"] = (
                        "Auto-pull retry budget exhausted; wait for cooldown or "
                        "increase VON_OLLAMA_AUTO_PULL_RETRY_BUDGET."
                    )
                    result.update(
                        {
                            "attempted": True,
                            "failed": True,
                            "retry_outcome": "retry_budget_exhausted",
                            "error": state["last_error"],
                        }
                    )
                    return result

                state["window_started_at_monotonic"] = window_started
                state["attempts_in_window"] = attempts_in_window + 1
                state["last_attempt_at_monotonic"] = now
                state["in_flight"] = True
                event = state.get("event")
                if not isinstance(event, threading.Event):
                    event = threading.Event()
                    state["event"] = event
                event.clear()
                waiter_event = event

        if owns_pull:
            threading.Thread(
                target=self._complete_model_auto_pull,
                kwargs={"model_token": model_token, "event": waiter_event},
                name=f"ollama-auto-pull-{model_token}",
                daemon=True,
            ).start()

        wait_budget = _OLLAMA_AUTO_PULL_TIMEOUT_SECONDS
        if wait_timeout_seconds is not None:
            try:
                wait_budget = min(wait_budget, max(0.0, float(wait_timeout_seconds)))
            except (TypeError, ValueError):
                pass
        waiter_started = time.monotonic()
        completed = bool(waiter_event and waiter_event.wait(timeout=wait_budget))
        wait_elapsed = round(time.monotonic() - waiter_started, 3)
        with _OLLAMA_AUTO_PULL_LOCK:
            state_after_wait = _OLLAMA_AUTO_PULL_STATE.get(model_token, {})
            state_after_wait["last_caller_wait_timeout_seconds"] = round(wait_budget, 3)
            if not completed:
                state_after_wait["caller_wait_timeout_count"] = (
                    int(state_after_wait.get("caller_wait_timeout_count", 0) or 0) + 1
                )
            status_after_wait = str(state_after_wait.get("last_status") or "")
            last_error = state_after_wait.get("last_error")
            last_elapsed = float(state_after_wait.get("last_elapsed_seconds") or 0.0)

        if not completed:
            result.update(
                {
                    "attempted": True,
                    "succeeded": False,
                    "failed": True,
                    "in_progress": True,
                    "caller_wait_timed_out": True,
                    "elapsed_seconds": wait_elapsed,
                    "wait_timeout_seconds": round(wait_budget, 3),
                    "retry_outcome": "pull_in_progress_caller_budget_exhausted",
                    "failure_code": "model_readiness_wait_budget_exhausted",
                    "joined_single_flight": not owns_pull,
                    "error": (
                        "Shared Ollama model pull is still in progress after the "
                        "caller's wait budget expired."
                    ),
                }
            )
            return result

        pull_succeeded = status_after_wait == "succeeded"
        result.update(
            {
                "attempted": True,
                "succeeded": pull_succeeded,
                "failed": not pull_succeeded,
                "elapsed_seconds": wait_elapsed,
                "retry_outcome": (
                    "joined_single_flight"
                    if not owns_pull
                    else "retried_after_pull" if pull_succeeded else "pull_failed"
                ),
                "joined_single_flight": not owns_pull,
                "pull_elapsed_seconds": round(last_elapsed, 3),
                "auto_pull_attempted": True,
                "auto_pull_succeeded": pull_succeeded,
                "auto_pull_failed": not pull_succeeded,
            }
        )
        if last_error:
            result["error"] = str(last_error)
        return result


class OpenAIClient(LLMInterface):
    """Client for interacting with the OpenAI API."""

    DEFAULT_MODEL = DEFAULT_OPENAI_MODEL

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_key_env_var: Optional[str] = "OPENAI_API_KEY",
    ):
        """
        Initialize the OpenAI client.

        Args:
            api_key: Optional direct API key
            api_key_env_var: Name of environment variable containing the API key
        """
        env_val = _resolve_secret_env_value(api_key_env_var)
        if not env_val and api_key_env_var:
            try:
                from dotenv import dotenv_values  # type: ignore
                from pathlib import Path

                repo_root = Path(__file__).resolve().parents[3]
                env_path = repo_root / ".env"
                if env_path.exists():
                    values = dotenv_values(env_path)
                    raw = values.get(api_key_env_var)
                    if raw is not None:
                        env_val = str(raw)
            except Exception:
                env_val = env_val or None
        self.api_key = api_key or env_val or ""
        if not self.api_key:
            raise ValueError(
                f"OpenAI API key not provided or found in environment variable {api_key_env_var}."
            )
        self.client = openai.OpenAI(api_key=self.api_key)
        logger.info("OpenAIClient initialized.")

    @staticmethod
    def _split_request_timeout_from_llm_params(
        llm_params: Optional[Dict[str, Any]],
    ) -> tuple[Dict[str, Any], float | None]:
        return _split_request_timeout_from_llm_params(llm_params)

    def _get_structured_client_config(self, model: Optional[str]) -> LLMClientConfig:
        """Get configuration for structured tool calling client (JVNAUTOSCI-799)."""
        resolved_model = resolve_openai_model_name(model)
        return LLMClientConfig(
            model=resolved_model or self.DEFAULT_MODEL,
            provider="openai",
            api_key=self.api_key,
            connection_id="#V#openai_provider",
            deployment_id=resolved_model or self.DEFAULT_MODEL,
            # The effective API surface is selected by the structured adapter.
            # Defer optional-parameter policy until that surface is known.
            temperature=0.7,
        )

    def validate_model_response(self, response, model: str) -> str:
        """
        Validate that the response matches expectations for the model.
        Raises RuntimeError if validation fails.

        Args:
            response: The OpenAI API response object
            model: The model name used for the request

        Returns:
            str: The validated response content

        Raises:
            RuntimeError: If the response is invalid
        """
        # Basic response validation
        if not response:
            raise RuntimeError(f"Empty response from OpenAI {model}")

        if not hasattr(response, "choices") or not response.choices:
            raise RuntimeError(f"OpenAI {model} response contained no choices")

        choice = response.choices[0]
        if not hasattr(choice, "message") or not hasattr(choice.message, "content"):
            raise RuntimeError(
                f"OpenAI {model} response missing required message fields"
            )

        content = choice.message.content
        if not content or not isinstance(content, str):
            raise RuntimeError(
                f"OpenAI {model} response content invalid: {type(content)}"
            )

        # Model-specific validations
        if isinstance(model, str):
            # Check for unusually short responses from more capable models
            if "gpt-4" in model and len(content) < 10:
                msg = f"Unusually short response from {model}: {len(content)} chars"
                logger.warning(msg)
                warnings.warn(msg)

            # Check for potentially truncated responses
            if len(content) >= 99990:  # Close to token limit
                msg = f"Response from {model} may be truncated (length: {len(content)})"
                logger.warning(msg)
                warnings.warn(msg)

            # Check finish reason if available (only warn for valid string values)
            finish_reason = getattr(choice, "finish_reason", None)
            if isinstance(finish_reason, str):
                if finish_reason == "length":
                    msg = f"Response from {model} was truncated due to length limits"
                    logger.warning(msg)
                    warnings.warn(msg)
                elif finish_reason not in ("stop", ""):
                    msg = f"Unusual finish_reason '{finish_reason}' from {model}"
                    logger.warning(msg)
                    warnings.warn(msg)

        return content.strip()

    def generate(
        self,
        prompt: str,
        context: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        llm_params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate a response using the OpenAI API. Raises RuntimeError on failure."""
        # REFACTORING_NOTE: Model is now determined by the get_llm_client factory.
        target_model = (
            resolve_openai_model_name(model or self.DEFAULT_MODEL) or self.DEFAULT_MODEL
        )

        logger.info(f"Generating response using OpenAI model: {target_model}")
        if _should_log_llm_io():
            logger.debug(
                "[LLM PROMPT][OpenAI][%s]: %s", target_model, _truncate_for_log(prompt)
            )
        try:
            llm_params_for_model, request_timeout_seconds = (
                self._split_request_timeout_from_llm_params(llm_params)
            )
            request_client = (
                self.client.with_options(
                    timeout=request_timeout_seconds,
                    max_retries=0,
                )
                if request_timeout_seconds is not None
                else self.client
            )
            conv = build_conversation(prompt, context)
            messages = to_openai_messages(conv)
            responses_params = openai_responses_kwargs_from_model_parameters(
                llm_params_for_model,
                model=target_model,
            )
            if responses_params or _openai_model_defaults_to_responses(target_model):
                responses_params.setdefault(
                    "max_output_tokens",
                    resolve_openai_responses_max_output_tokens(
                        llm_params_for_model
                    ),
                )
                response = request_client.responses.create(  # type: ignore[attr-defined]
                    model=target_model,
                    input=messages,  # type: ignore[arg-type]
                    **responses_params,
                )
                if _should_log_llm_io():
                    logger.debug("OpenAI Responses raw response: %s", response)
                actual_model = getattr(response, "model", None) or target_model
                content = _extract_openai_responses_text(response)
                if not content:
                    raise RuntimeError(
                        f"OpenAI {target_model} Responses payload contained no text"
                    )
                if (
                    model is not None
                    and isinstance(actual_model, str)
                    and actual_model != target_model
                ):
                    logger.warning(
                        f"Model mismatch - Requested: {target_model}, Used: {actual_model}"
                    )
                    warnings.warn(
                        f"Model mismatch - Requested: {target_model}, Used: {actual_model}"
                    )
                if _should_log_llm_io():
                    logger.debug(
                        "[LLM RESPONSE][OpenAI][%s]: %s",
                        actual_model,
                        _truncate_for_log(content),
                    )
                return content

            openai_params = {}
            if llm_params_for_model:
                if "temperature" in llm_params_for_model:
                    safe_temperature = resolve_safe_temperature_for_model(
                        target_model,
                        llm_params_for_model["temperature"],
                    )
                    if safe_temperature is not None:
                        openai_params["temperature"] = safe_temperature
                # Add other OpenAI specific params like top_p, max_tokens, etc.

            response = request_client.chat.completions.create(  # type: ignore[arg-type]
                model=target_model,
                messages=messages,  # type: ignore[arg-type]
                **openai_params,
            )
            if _should_log_llm_io():
                logger.debug("OpenAI raw response: %s", response)

            # Track which model actually processed the request
            actual_model = getattr(response, "model", None) or target_model
            # Only warn about model mismatch if the caller explicitly requested a model
            if (
                model is not None
                and isinstance(actual_model, str)
                and actual_model != target_model
            ):
                # Warn only when the actual model appears less capable than requested
                def _tier(name: str) -> int:
                    if not isinstance(name, str):
                        return -1
                    name_l = name.lower()
                    if "gpt-4" in name_l or name_l.startswith("o1-"):
                        return 4
                    if "gpt-3.5" in name_l or "gpt-3" in name_l:
                        return 3
                    # Fallback baseline
                    return 1

                if _tier(actual_model) < _tier(target_model):
                    logger.warning(
                        f"Model mismatch - Requested: {target_model}, Used: {actual_model}"
                    )
                    warnings.warn(
                        f"Model mismatch - Requested: {target_model}, Used: {actual_model}"
                    )

            content = self.validate_model_response(response, actual_model)
            if _should_log_llm_io():
                logger.debug(
                    "[LLM RESPONSE][OpenAI][%s]: %s",
                    actual_model,
                    _truncate_for_log(content),
                )

            # Log token usage if available
            if hasattr(response, "usage"):
                logger.info(
                    f"Token usage - Input: {response.usage.prompt_tokens}, Output: {response.usage.completion_tokens}"
                )

            return content
        except openai.APIConnectionError:
            msg = "Failed to connect to OpenAI API"
            logger.error(msg, exc_info=True)
            raise RuntimeError(msg)
        except openai.RateLimitError as e:
            _code = (
                getattr(getattr(e, "body", None), "get", lambda *a: None)("code")
                if isinstance(getattr(e, "body", None), dict)
                else None
            )
            if _code == "insufficient_quota":
                msg = f"OpenAI quota exhausted (insufficient_quota): {str(e)}"
            else:
                msg = f"OpenAI rate limit exceeded: {str(e)}"
            logger.error(msg, exc_info=True)
            raise RuntimeError(msg)
        except openai.BadRequestError as e:
            msg = f"Invalid request to OpenAI API: {str(e)}"
            logger.error(msg, exc_info=True)
            raise RuntimeError(msg)
        except openai.APIError as e:
            msg = f"OpenAI API Error: {str(e)}"
            logger.error(msg, exc_info=True)
            raise RuntimeError(msg)
        except Exception as e:
            # Handle errors without stack traces for non-API errors
            msg = str(e)
            if isinstance(e, (ValueError, RuntimeError)):
                logger.error(msg)
            else:
                msg = f"Unexpected error generating response with OpenAI model {target_model}: {msg}"
                logger.error(msg, exc_info=True)
            raise RuntimeError(msg)

    def get_embedding(self, text: str, model: Optional[str] = None) -> List[float]:
        """Generate an embedding using OpenAI."""
        target_model = (
            model or "text-embedding-3-small"
        )  # Default to a common embedding model
        try:
            response = self.client.with_options(
                max_retries=0,
                timeout=8.0,
            ).embeddings.create(input=[text], model=target_model)
            return response.data[0].embedding
        except Exception as e:
            logger.error(
                f"Failed to generate embedding with OpenAI model {target_model}: {e}"
            )
            raise RuntimeError(f"OpenAI embedding error: {str(e)}") from e

    def list_models(self) -> List[str]:
        """List available models from OpenAI (focus on GPT models) with caching/backoff."""
        cache_key = "openai:models"
        now = time.time()

        # Check cache
        entry = _MODEL_CACHE.get(cache_key)
        if entry:
            age = now - entry.get("fetched_at", 0)
            if age < _MODEL_CACHE_TTL and not entry.get("error"):
                entry["hit_count"] = entry.get("hit_count", 0) + 1
                logger.debug(f"OpenAI models cache hit (age={age:.1f}s)")
                return entry.get("models", [])

        # Backoff on recent failures
        last_fail = _MODEL_FAIL_BACKOFF.get(cache_key, 0.0)
        if last_fail and now - last_fail < _MODEL_FAIL_BACKOFF_SECONDS:
            logger.warning(
                f"Skipping OpenAI model list call due to backoff; {now - last_fail:.1f}s since last failure < {_MODEL_FAIL_BACKOFF_SECONDS}s"
            )
            if entry and entry.get("models"):
                return entry.get("models", [])
            return []

        logger.info("Listing available OpenAI models (cache miss or expired).")
        start = time.time()
        models: List[str] = []
        error_msg: Optional[str] = None
        try:
            models_response = self.client.models.list()
            for model in getattr(models_response, "data", []):  # type: ignore[attr-defined]
                mid = getattr(model, "id", None)
                if isinstance(mid, str) and "gpt" in mid:
                    models.append(mid)
            models = sorted(models)
            logger.debug(f"Found OpenAI models (filtered): {models}")
        except openai.APIConnectionError as e:
            error_msg = f"Failed to connect to OpenAI API: {str(e)}"
            logger.error(error_msg)
        except openai.AuthenticationError as e:
            error_msg = f"Invalid OpenAI API key: {str(e)}"
            logger.error(error_msg)
        except openai.RateLimitError as e:
            body = getattr(e, "body", None)
            _code = body.get("code") if isinstance(body, dict) else None
            if _code == "insufficient_quota":
                error_msg = f"OpenAI quota exhausted (insufficient_quota): {str(e)}"
            else:
                error_msg = f"OpenAI rate limit exceeded: {str(e)}"
            logger.error(error_msg)
        except openai.APIError as e:
            error_msg = f"OpenAI API error: {str(e)}"
            logger.error(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error listing OpenAI models: {str(e)}"
            logger.error(error_msg, exc_info=True)

        duration = time.time() - start
        _MODEL_CACHE[cache_key] = {
            "models": models,
            "fetched_at": now,
            "build_time": duration,
            "error": error_msg,
            "source": "refresh" if not entry else "refresh-expired",
            "hit_count": 0 if entry is None else entry.get("hit_count", 0),
        }

        if error_msg:
            _MODEL_FAIL_BACKOFF[cache_key] = now
            # If we had a previous successful list, fall back to it
            if entry and entry.get("models"):
                logger.warning("Returning stale OpenAI model list due to error")
                return entry.get("models", [])
            raise RuntimeError(error_msg)

        return models


class GeminiClient(LLMInterface):
    """Client for interacting with the Google Gemini API."""

    def __init__(
        self, api_key: Optional[str] = None, default_model: str = DEFAULT_GEMINI_MODEL
    ):
        if genai is None:
            raise ImportError(
                "google-genai package is required for GeminiClient. Install with: pdm add google-genai"
            )
        self.api_key = api_key or get_gemini_api_key()
        if not self.api_key:
            raise ValueError(
                "Gemini API key not provided or found in environment variables "
                "(GEMINI_API_KEY or legacy GOOGLE_API_KEY)."
            )
        try:
            genai.configure(api_key=self.api_key)  # type: ignore[attr-defined]
        except Exception:
            logger.debug(
                "genai.configure not available; continuing without explicit config"
            )
        self.default_model = default_model
        # Check if the default model exists upon initialization
        try:
            try:
                self.client = genai.GenerativeModel(self.default_model)  # type: ignore[attr-defined]
            except Exception as e:
                logger.error(f"Failed dynamic GenerativeModel init: {e}")
                raise
            logger.info(f"GeminiClient initialized with model: {self.default_model}")
        except Exception as e:
            logger.error(
                f"Failed to initialize Gemini model '{self.default_model}': {e}"
            )
            raise ValueError(
                f"Failed to initialize Gemini model '{self.default_model}'. Check model name and API key."
            ) from e

    def _get_structured_client_config(self, model: Optional[str]) -> LLMClientConfig:
        """Get configuration for structured tool calling client (JVNAUTOSCI-799)."""
        return LLMClientConfig(
            model=model or self.default_model,
            provider="gemini",
            api_key=self.api_key,
            temperature=0.7,
        )

    def generate(
        self,
        prompt: str,
        context: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        llm_params: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Generate a response using the Gemini API. Raises RuntimeError on failure."""
        assert genai is not None
        target_model_name = model or self.default_model
        logger.info(f"Generating response using Gemini model: {target_model_name}")
        if _should_log_llm_io():
            logger.debug(
                "[LLM PROMPT][Gemini][%s]: %s",
                target_model_name,
                _truncate_for_log(prompt),
            )

        try:
            # Select the model - re-initialize if different from default
            if target_model_name == self.default_model:
                model_instance = self.client
            else:
                try:
                    model_instance = genai.GenerativeModel(target_model_name)  # type: ignore[attr-defined]
                    logger.info(f"Using specified Gemini model: {target_model_name}")
                except Exception as e:
                    logger.error(
                        f"Failed to initialize specified Gemini model '{target_model_name}': {e}"
                    )
                    raise RuntimeError(
                        f"Could not initialize Gemini model '{target_model_name}': {str(e)}"
                    ) from e

            gemini_config_params = {}
            if llm_params:
                if "temperature" in llm_params:
                    gemini_config_params["temperature"] = llm_params["temperature"]
                # Add other Gemini specific params like top_p, top_k, max_output_tokens

            generation_config = None
            if gemini_config_params:
                try:
                    generation_config = genai.types.GenerationConfig(
                        **gemini_config_params
                    )  # type: ignore[attr-defined]
                except Exception:
                    generation_config = None

            send_kwargs: Dict[str, Any] = {}
            if generation_config is not None:
                send_kwargs["generation_config"] = generation_config

            # Gemini handles history differently (pass directly to start_chat)
            if context and isinstance(context, list):
                history = to_gemini_history(_coerce_context(context))
                chat = model_instance.start_chat(history=cast(Any, history))
                response = chat.send_message(prompt, **send_kwargs)
            else:
                response = model_instance.generate_content(prompt, **send_kwargs)

            if _should_log_llm_io():
                logger.debug("Gemini raw response: %s", response)
            # Handle potential safety blocks or empty responses
            if response.parts:
                content = response.text
                if _should_log_llm_io():
                    logger.debug(
                        "[LLM RESPONSE][Gemini][%s]: %s",
                        target_model_name,
                        _truncate_for_log(content),
                    )
                return content
            elif response.prompt_feedback and response.prompt_feedback.block_reason:
                block_reason = response.prompt_feedback.block_reason
                logger.warning(f"Gemini response blocked due to: {block_reason}")
                raise RuntimeError(f"Gemini response blocked due to {block_reason}.")
            else:
                logger.warning(
                    "Gemini response was empty or blocked without specific reason."
                )
                raise RuntimeError("Gemini returned an empty or blocked response.")

        except Exception as e:
            logger.error(
                f"Error generating response with Gemini model {target_model_name}: {e}",
                exc_info=True,
            )
            raise RuntimeError(f"Gemini error: {str(e)}") from e

    def get_embedding(self, text: str, model: Optional[str] = None) -> List[float]:
        """Generate an embedding using Gemini."""
        # Ensure genai is available
        if genai is None:
            raise ImportError(
                "google-genai package is required for Gemini embeddings. Install with: pdm add google-genai"
            )

        # Cast to Any to avoid static analysis errors about exported members
        genai_any = cast(Any, genai)

        target_model = model or "models/embedding-001"
        try:
            result = genai_any.embed_content(
                model=target_model,
                content=text,
                task_type="retrieval_document",
                title="Embedding of text",
            )
            return result["embedding"]
        except Exception as e:
            logger.error(
                f"Failed to generate embedding with Gemini model {target_model}: {e}"
            )
            raise RuntimeError(f"Gemini embedding error: {str(e)}") from e

    def list_models(self) -> List[str]:
        """List available models from Gemini."""
        logger.info("Listing available Gemini models.")
        assert genai is not None

        try:
            model_names = []
            try:
                gm_iter = genai.list_models()  # type: ignore[attr-defined]
            except Exception:
                return []
            for m in gm_iter:
                name = getattr(m, "name", None)
                methods = getattr(m, "supported_generation_methods", [])
                if (
                    isinstance(name, str)
                    and isinstance(methods, list)
                    and "generateContent" in methods
                ):
                    model_names.append(name)
            logger.debug(f"Found Gemini models: {model_names}")
            return sorted(model_names)
        except Exception as e:
            logger.error(f"Error listing Gemini models: {e}", exc_info=True)
            return []


def get_llm_client(
    client_type: Optional[str] = None,
    force_init: bool = False,
    user_concept_id: Optional[str] = None,
    org_concept_id: Optional[str] = None,
    **kwargs,
) -> LLMInterface:
    """
    Factory function to get an LLM client instance based on the active setting.

    Args:
        client_type: If provided, overrides the database setting.
        force_init: Force reinitialization of clients.
        user_concept_id: User concept ID for per-user LLM settings (takes precedence over org and global).
        org_concept_id: Organization concept ID for per-org LLM settings (takes precedence over global).
        **kwargs: Additional arguments for client initialization.

    Returns:
        LLMInterface: The appropriate LLM client.
    """
    # Always ensure clients are up-to-date
    initialize_clients(force=force_init)

    provider = None
    model = None
    host = None

    if client_type:
        logger.info(
            f"Overriding database setting with explicit client type: {client_type}"
        )
        provider = client_type.lower()
        requested_host = kwargs.get("host")
        if isinstance(requested_host, str) and requested_host.strip():
            host = requested_host.strip()
    else:
        # Use resolve_llm_setting to get user > org > global precedence
        from ..services.settings_service import resolve_llm_setting

        active_llm = resolve_llm_setting(
            user_concept_id=user_concept_id, org_concept_id=org_concept_id
        )
        if active_llm:
            provider = active_llm.get("provider")
            model = active_llm.get("model")
            host = active_llm.get("host")  # Get the specific host for this model
            scope = active_llm.get("scope", "unknown")
            logger.info(f"LLM setting resolved from scope: {scope}")
        else:
            logger.warning("No active LLM setting found. Defaulting to Ollama.")
            provider = "ollama"

    if isinstance(model, str) and model.strip().startswith("#V#"):
        resolved_provider = resolve_provider_from_model_concept(model)
        if resolved_provider and resolved_provider != provider:
            logger.info(
                "Resolved provider '%s' from model concept %s (was '%s').",
                resolved_provider,
                model,
                provider,
            )
            provider = resolved_provider

    logger.info("--- LLM Client Selection ---")
    logger.info(f"Provider: {provider}, Model: {model}, Host: {host}")

    if (
        provider == "openai"
        and isinstance(model, str)
        and _looks_like_ollama_model(model)
    ):
        logger.warning(
            "OpenAI provider configured with Ollama model '%s'; routing to Ollama instead.",
            model,
        )
        provider = "ollama"
        model = _extract_ollama_model_id(model) or model

    if provider == "openai":
        if _openai_client:
            # Pass the model to the generate method, don't set it on the client instance
            return _openai_client
        else:
            logger.error(
                "OpenAI provider was selected, but the client is not available. Check API key."
            )
            # Fallback to Ollama if available
            ollama_fallback = _ensure_ollama_client()
            if ollama_fallback:
                logger.warning("Falling back to Ollama client.")
                return ollama_fallback
            else:
                raise RuntimeError("No LLM clients available")

    elif provider == "ollama":
        if isinstance(model, str):
            model = resolve_ollama_model_name(model) or model
        # Always create a new client with the specific host for this model
        # This ensures the correct remote host is used when a model from that host is selected

        # If no host is specified in settings but we have a model, check if it's a global model
        # that includes host information (e.g., "130.216.118.198 - gemma3:4b")
        effective_host = host
        if not effective_host and model:
            # Check if the model name contains host information
            if " - " in model:
                # Extract host from model name format: "host - modelname"
                parts = model.split(" - ", 1)
                if len(parts) == 2:
                    potential_host = parts[0].strip()
                    # Validate that it looks like a host (IP or hostname)
                    if (
                        "." in potential_host and not potential_host.startswith("http")
                    ) or ":" in potential_host:
                        effective_host = potential_host
                        # Extract just the model name without the host prefix
                        model = parts[1].strip()
                        logger.info(
                            f"Extracted host '{effective_host}' and model '{model}' from global model selection"
                        )

                        # Update the active host in settings so the UI reflects the change
                        try:
                            from ..services.settings_service import (
                                set_active_ollama_host,
                            )

                            set_active_ollama_host(effective_host)
                            logger.info(
                                f"Set '{effective_host}' as active Ollama host in settings"
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to update active Ollama host in settings: {e}"
                            )
                            # Continue anyway, as the client will still work with the extracted host

        try:
            # Prefer already-initialized global client to avoid duplicate inits unless a specific host is requested
            if effective_host is None:
                existing = _ensure_ollama_client()
                if existing is not None:
                    logger.info("Using existing Ollama client instance.")
                    return existing
            logger.info(
                f"Creating Ollama client for host: {effective_host or 'default'}"
            )
            return OllamaClient(host=effective_host)
        except Exception as e:
            logger.error(
                f"Failed to initialize Ollama client for host '{effective_host}': {e}"
            )
            # Fallback to the globally initialized client if it exists
            ollama_fb = _ensure_ollama_client()
            if ollama_fb:
                logger.warning("Falling back to default Ollama client.")
                return ollama_fb
            else:
                raise RuntimeError("No LLM clients available") from e

    elif provider == "gemini":
        try:
            return GeminiClient(**kwargs)
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}")
            gemini_fb = _ensure_ollama_client()
            if gemini_fb:
                logger.warning("Falling back to Ollama client.")
                return gemini_fb
            else:
                raise RuntimeError("No LLM clients available")

    else:
        # Invalid provider should raise ValueError instead of defaulting
        if client_type:  # Only raise if explicitly provided invalid type
            raise ValueError(f"Unknown provider '{provider}'")
        else:
            logger.warning(f"Unknown provider '{provider}'. Defaulting to Ollama.")
            default_fb = _ensure_ollama_client()
            if default_fb:
                return default_fb
            else:
                raise RuntimeError("No LLM clients available")


def validate_openai_config(
    env_var: str, model: str, key: str
) -> tuple[bool, list[str]]:
    """
    Validate OpenAI configuration parameters.

    Args:
        env_var: Environment variable name for the API key
        model: Model name to validate
        key: API key value

    Returns:
        Tuple of (is_valid, error_messages)
    """
    messages = []

    # Check environment variable
    if not env_var or not env_var.strip():
        messages.append("OpenAI environment variable name is empty or not configured")

    # Check model configuration
    if not model or not model.strip():
        messages.append("OpenAI model not configured")
    elif not model.startswith(_OPENAI_MODEL_PREFIXES):
        messages.append("Unusual OpenAI model name format")

    # Check API key
    if not key or not key.strip():
        messages.append("No API key found in environment variable")
    elif len(key) < 20:
        messages.append("API key seems too short for a valid OpenAI key")
    elif not key.startswith("sk-"):
        messages.append(
            "API key doesn't follow expected OpenAI format (should start with 'sk-')"
        )

    is_valid = len(messages) == 0
    return is_valid, messages


def get_active_model_name(
    *,
    user_concept_id: Optional[str] = None,
    org_concept_id: Optional[str] = None,
) -> Optional[str]:
    """Helper function to get the model name from the active LLM setting."""
    resolved_user_concept_id = user_concept_id
    resolved_org_concept_id = org_concept_id
    resolved_user_concept_id, resolved_org_concept_id = (
        _resolve_effective_llm_actor_scope(
            resolved_user_concept_id,
            resolved_org_concept_id,
        )
    )

    active_llm = resolve_llm_setting(
        user_concept_id=resolved_user_concept_id,
        org_concept_id=resolved_org_concept_id,
    )
    if active_llm:
        provider = active_llm.get("provider")
        model = active_llm.get("model")
        if isinstance(model, str) and model.strip().startswith("#V#"):
            resolved_provider = resolve_provider_from_model_concept(model)
            if resolved_provider:
                provider = resolved_provider
        if provider == "openai":
            return resolve_openai_model_name(model)
        if provider == "ollama":
            return resolve_ollama_model_name(model)
        return model
    return None


def get_active_model_parameters(
    *,
    user_concept_id: Optional[str] = None,
    org_concept_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return parameters from the same scoped setting as the active model.

    Durable workflows must inherit the complete active-model configuration,
    not merely its model name.  In particular, dropping a reasoning effort can
    make an otherwise usable OpenAI request reserve materially more output
    budget and fail differently from the settings readiness probe.
    """

    resolved_user_concept_id = user_concept_id
    resolved_org_concept_id = org_concept_id
    resolved_user_concept_id, resolved_org_concept_id = (
        _resolve_effective_llm_actor_scope(
            resolved_user_concept_id,
            resolved_org_concept_id,
        )
    )

    active_llm = resolve_llm_setting(
        user_concept_id=resolved_user_concept_id,
        org_concept_id=resolved_org_concept_id,
    )
    if not isinstance(active_llm, Mapping):
        return {}
    raw_parameters = active_llm.get("model_parameters")
    if not isinstance(raw_parameters, Mapping):
        return {}
    return dict(raw_parameters)
