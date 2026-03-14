import logging
import os
import sys
import warnings
import requests

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
import openai
from abc import ABC, abstractmethod

# Structured tool calling support (JVNAUTOSCI-799)
from .structured_tool_calling import (
    LLMResponse,
    ToolDefinition,
    get_llm_client as get_structured_client,
    LLMClientConfig,
)
from typing import (
    Optional,
    List,
    Dict,
    Any,
    TypedDict,
    Literal,
    Sequence,
    TYPE_CHECKING,
    cast,
)

try:
    from google import genai  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    genai = None  # type: ignore

if genai is not None:
    # Cast to Any so Pyright doesn't complain about dynamic attrs
    genai = cast(Any, genai)
from ..services.settings_service import resolve_llm_setting, get_openai_env_var
from ..services.llm_api_key_resolution import get_gemini_api_key
import time
from collections import defaultdict

# Configure logging
logger = logging.getLogger(__name__)


def _should_log_llm_io() -> bool:
    """Return True if LLM prompt/response debug logging is enabled."""

    value = os.environ.get("VON_DEBUG_LLM_IO", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _truncate_for_log(text: str, *, max_chars: int = 2000) -> str:
    if not isinstance(text, str):
        return str(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"


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


def _extract_openai_model_id(value: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower().startswith("openai:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    return cleaned or None


def _extract_ollama_model_id(value: str) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.lower().startswith("ollama:"):
        cleaned = cleaned.split(":", 1)[1].strip()
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


def initialize_clients(force: bool = False):
    """
    Initialize LLM clients based on settings.
    Args:
        force: If True, reinitialize clients even if they exist
    """
    global _ollama_client, _openai_client, _last_openai_env_var, _last_openai_key

    current_env_var = get_openai_env_var()
    current_key = os.getenv(current_env_var) if current_env_var else None

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
                # Get current model from settings for validation (try session context first)
                user_concept_id = None
                org_concept_id = None
                try:
                    from flask import session, has_request_context

                    if has_request_context():
                        user_concept_id = session.get("user_concept_id")
                        org_concept_id = session.get("organisation_concept_id")
                except Exception:
                    pass
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

                _openai_client = OpenAIClient(api_key_env_var=current_env_var)
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
        return client.generate_with_tools_sync(
            prompt=prompt,
            available_tools=available_tools,
            system_message=system_message,
            context=self._convert_context_for_structured_client(context),
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
        self, host: Optional[str] = None, default_model: str = "granite3.3:2b"
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

        logger.info(f"Generating response using Ollama model: {target_model}")
        if _should_log_llm_io():
            logger.debug(
                "[LLM PROMPT][Ollama][%s]: %s", target_model, _truncate_for_log(prompt)
            )

        conv = build_conversation(prompt, context)
        messages = to_ollama_messages(conv)

        ollama_options = {}
        if llm_params:  # REFACTORING_NOTE: Ensure llm_params are processed correctly
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
            for key, value in llm_params.items():
                if key in valid_ollama_options:
                    ollama_options[key] = value
                else:
                    logger.warning(
                        f"Ignoring unknown llm_param: {key} for Ollama client"
                    )

        max_retries = 3
        base_delay = 1  # seconds

        for attempt in range(max_retries):
            try:
                # Ensure model is available locally (optional, can be slow)
                # self._ensure_model_pulled(target_model)

                response = self.client.chat(
                    model=target_model,
                    messages=messages,
                    options=(
                        ollama_options if ollama_options else None
                    ),  # Pass options if any
                )
                logger.debug(f"Ollama raw response: {response}")
                content = response["message"]["content"]
                if _should_log_llm_io():
                    logger.debug(
                        "[LLM RESPONSE][Ollama][%s]: %s",
                        target_model,
                        _truncate_for_log(content),
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
                        f"Ollama ResponseError on attempt {attempt + 1}/{max_retries}: {e} (Status: {getattr(e,'status_code','n/a')})"
                    )
                    if (
                        "CUDA error" in str(e)
                        and getattr(e, "status_code", None) == 500
                        and attempt < max_retries - 1
                    ):
                        delay = base_delay * (2**attempt)
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

        try:
            response = self.client.embeddings(model=target_model, prompt=text)
            return response["embedding"]
        except Exception as e:
            logger.error(
                f"Failed to generate embedding with Ollama model {target_model}: {e}"
            )
            raise RuntimeError(f"Ollama embedding error: {str(e)}") from e

    def list_models(self) -> List[str]:
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
        if cached and (now - cached["ts"]) < _MODEL_CACHE_TTL:
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


class OpenAIClient(LLMInterface):
    """Client for interacting with the OpenAI API."""

    DEFAULT_MODEL = "gpt-3.5-turbo"

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
        env_val = os.environ.get(api_key_env_var) if api_key_env_var else None
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

    def _get_structured_client_config(self, model: Optional[str]) -> LLMClientConfig:
        """Get configuration for structured tool calling client (JVNAUTOSCI-799)."""
        resolved_model = resolve_openai_model_name(model)
        return LLMClientConfig(
            model=resolved_model or self.DEFAULT_MODEL,
            api_key=self.api_key,
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
            conv = build_conversation(prompt, context)
            messages = to_openai_messages(conv)

            openai_params = {}
            if llm_params:
                if "temperature" in llm_params:
                    openai_params["temperature"] = llm_params["temperature"]
                # Add other OpenAI specific params like top_p, max_tokens, etc.

            response = self.client.chat.completions.create(  # type: ignore[arg-type]
                model=target_model,
                messages=messages,  # type: ignore[arg-type]
                **openai_params,
            )
            logger.debug(f"OpenAI raw response: {response}")

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
            response = self.client.embeddings.create(input=[text], model=target_model)
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
        self, api_key: Optional[str] = None, default_model: str = "gemini-pro"
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
                    generation_config = genai.types.GenerationConfig(**gemini_config_params)  # type: ignore[attr-defined]
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

            logger.debug(f"Gemini raw response: {response}")
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

    logger.info(f"--- LLM Client Selection ---")
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


def get_active_model_name() -> Optional[str]:
    """Helper function to get the model name from the active LLM setting."""
    # Get user/org context from session if available
    user_concept_id = None
    org_concept_id = None
    try:
        from flask import session, has_request_context
        from ..security.access_control import get_effective_user_concept_id

        if has_request_context():
            user_concept_id = get_effective_user_concept_id()
            org_concept_id = session.get("organisation_concept_id")
    except Exception:
        pass

    active_llm = resolve_llm_setting(
        user_concept_id=user_concept_id, org_concept_id=org_concept_id
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
