import logging
import os
import sys
import warnings
import requests
try:  # Optional dependency (local model runtime)
    import ollama  # type: ignore
except ImportError:  # pragma: no cover - environment without ollama
    ollama = None  # type: ignore
import openai
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, TypedDict, Literal, Sequence, TYPE_CHECKING, cast

try:
    import google.generativeai as genai  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    genai = None  # type: ignore

if genai is not None:
    # Cast to Any so Pyright doesn't complain about dynamic attrs (configure, GenerativeModel, types, list_models)
    genai = cast(Any, genai)
from ..services.settings_service import get_active_llm_setting, get_openai_env_var
import time
from collections import defaultdict

# Configure logging
logger = logging.getLogger(__name__)

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
_MODEL_CACHE_TTL = int(os.getenv('LLM_MODEL_CACHE_TTL', '300'))  # seconds
_MODEL_FAIL_BACKOFF_SECONDS = int(os.getenv('LLM_MODEL_FAIL_BACKOFF', '60'))

#############################################
# Internal message schema & adapters
#############################################

AllowedRole = Literal['system', 'user', 'assistant', 'tool', 'model']

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
        role = msg.get('role') or 'user'
        content = msg.get('content')
        if not isinstance(content, str) or content == '':
            continue
        if role not in ('system','user','assistant','tool','model'):
            role = 'user'
        messages.append({'role': role, 'content': content})
    return messages

def build_conversation(prompt: str, context: Optional[Sequence[Dict[str, Any]]]) -> List[LLMMessage]:
    base = _coerce_context(context)
    base.append({'role': 'user', 'content': prompt})
    return base

def to_openai_messages(conv: Sequence[LLMMessage]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in conv:
        role = m['role']
        # Map 'model' to 'assistant'; 'tool' currently downgraded to 'assistant'
        if role == 'model':
            role = 'assistant'
        if role == 'tool':
            role = 'assistant'
        if role not in ('system','user','assistant'):
            role = 'user'
        out.append({'role': role, 'content': m['content']})
    return out

def to_ollama_messages(conv: Sequence[LLMMessage]) -> List[Dict[str, str]]:
    # Ollama accepts the same shape; just normalize roles ('model'→'assistant')
    out: List[Dict[str, str]] = []
    for m in conv:
        role = m['role']
        if role == 'model':
            role = 'assistant'
        out.append({'role': role, 'content': m['content']})
    return out

def to_gemini_history(conv: Sequence[LLMMessage]) -> List[Dict[str, Any]]:
    history: List[Dict[str, Any]] = []
    for m in conv:
        role = m['role']
        if role in ('assistant','model','tool'):
            gem_role = 'model'
        elif role in ('system','user'):
            gem_role = 'user' if role != 'system' else 'user'
        else:
            gem_role = 'user'
        history.append({'role': gem_role, 'parts': [{'text': m['content']}]})
    return history

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
                # Get current model from settings for validation
                active_llm = get_active_llm_setting()
                current_model = active_llm.get('model', '') if active_llm else ''

                # Validate configuration and emit warnings
                is_valid, error_messages = validate_openai_config(current_env_var, current_model, current_key)
                for message in error_messages:
                    # Avoid false-positive model name warnings when the model clearly matches allowed prefixes
                    if message.startswith("Unusual OpenAI model name format"):
                        try:
                            safe_model = current_model or ""
                            if isinstance(safe_model, str) and safe_model.startswith((
                                'gpt-', 'o1-', 'text-', 'davinci', 'curie', 'babbage', 'ada'
                            )):
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

    # Always ensure Ollama client is available as a potential option
    if _ollama_client is None or force:
        try:
            _ollama_client = OllamaClient()
            logger.info("Ollama client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Ollama client: {e}")
            _ollama_client = None

class LLMInterface(ABC):
    """Abstract Base Class for Language Model interactions."""

    @abstractmethod
    def generate(self, prompt: str, context: Optional[List[Dict[str, Any]]] = None, model: Optional[str] = None, llm_params: Optional[Dict[str, Any]] = None) -> str:
        """Generate a response based on the prompt and optional context and parameters."""
        pass

    @abstractmethod
    def list_models(self) -> List[str]:
        """List available models for this interface."""
        pass

class OllamaClient(LLMInterface):
    """Client for interacting with local Ollama models."""

    def __init__(self, host: Optional[str] = None, default_model: str = "granite3.3:2b"):
        if ollama is None:
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
        self.host = self._normalize_host_url(resolved_host)  # Store the normalized host for direct API calls or logging
        self.default_model = default_model

        # Instantiate the ollama.Client.
        # The official ollama-python library client will automatically use
        # the OLLAMA_HOST environment variable if set, or its own internal default
        # if no host is provided. We now explicitly pass the resolved host.
        try:
            self.client = ollama.Client(host=self.host)
            logger.info(f"OllamaClient initialized for host: {self.host}")
        except TypeError as e:
            logger.error(f"Error initializing ollama.Client for host {self.host}: {e}. This might indicate an issue with the ollama library version or environment setup.")
            # Potentially re-raise or handle as a critical failure
            raise
        except Exception as e: # Catch other potential exceptions during client init
            logger.error(f"An unexpected error occurred during OllamaClient initialization for host {self.host}: {e}")
            raise

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
        if host.startswith(('http://', 'https://')):
            return host

        # If it's just an IP or hostname, add protocol and default port
        if ':' not in host:
            # Just IP/hostname, add default port
            return f"http://{host}:11434"
        elif '://' not in host:
            # Has port but no protocol
            return f"http://{host}"

        return host

    def generate(self, prompt: str, context: Optional[List[Dict[str, str]]] = None, model: Optional[str] = None, llm_params: Optional[Dict[str, Any]] = None) -> str:
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
                if ("." in potential_host and not potential_host.startswith("http")) or ":" in potential_host:
                    target_model = parts[1].strip()
                    logger.info(f"Extracted model name '{target_model}' from global model format")

        if not target_model:
            error_msg = "No Ollama model specified."
            logger.error(error_msg)
            raise ValueError(error_msg)

        logger.info(f"Generating response using Ollama model: {target_model}")
        print(f"[LLM PROMPT][Ollama][{target_model}]: {prompt}")

        conv = build_conversation(prompt, context)
        messages = to_ollama_messages(conv)

        ollama_options = {}
        if llm_params: # REFACTORING_NOTE: Ensure llm_params are processed correctly
            # Only pass parameters that are valid for ollama.Client.chat options
            valid_ollama_options = [
                "mirostat", "mirostat_eta", "mirostat_tau", "num_ctx", "num_gpu",
                "num_thread", "repeat_last_n", "repeat_penalty", "temperature",
                "seed", "stop", "tfs_z", "num_predict", "top_k", "top_p"
            ]
            for key, value in llm_params.items():
                if key in valid_ollama_options:
                    ollama_options[key] = value
                else:
                    logger.warning(f"Ignoring unknown llm_param: {key} for Ollama client")

        max_retries = 3
        base_delay = 1  # seconds

        for attempt in range(max_retries):
            try:
                # Ensure model is available locally (optional, can be slow)
                # self._ensure_model_pulled(target_model)

                response = self.client.chat(
                    model=target_model,
                    messages=messages,
                    options=ollama_options if ollama_options else None # Pass options if any
                )
                logger.debug(f"Ollama raw response: {response}")
                content = response['message']['content']
                print(f"[LLM RESPONSE][Ollama][{target_model}]: {content}")
                return content
            except Exception as e:
                # Handle known Ollama ResponseError distinctly if library present
                ollama_resp_err_cls = getattr(ollama, 'ResponseError', None) if ollama is not None else None
                if ollama_resp_err_cls and isinstance(e, ollama_resp_err_cls):
                    logger.warning(f"Ollama ResponseError on attempt {attempt + 1}/{max_retries}: {e} (Status: {getattr(e,'status_code','n/a')})")
                    if "CUDA error" in str(e) and getattr(e, 'status_code', None) == 500 and attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.info(f"Retrying after {delay} seconds due to CUDA error (500)...")
                        time.sleep(delay)
                        continue
                    logger.error(f"Failed to generate response with Ollama model {target_model} after {attempt + 1} attempts due to ResponseError: {e}", exc_info=True)
                    raise RuntimeError(f"Ollama ResponseError: {str(e)}") from e
                # Generic unexpected error path
                logger.error(f"Unexpected error on attempt {attempt + 1}/{max_retries} with Ollama model {target_model}: {e}", exc_info=True)
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.info(f"Retrying after {delay} seconds due to unexpected error...")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"Ollama unexpected error: {str(e)}") from e

        # This line should only be reached if all retries fail and an error wasn't re-raised properly inside the loop.
        # It acts as a final fallback.
        logger.error(f"Failed to generate response with Ollama model {target_model} after all {max_retries} retries.")
        raise RuntimeError(f"Ollama error: Failed after {max_retries} retries for model {target_model}.")

    def list_models(self) -> List[str]:
        """List available Ollama models (cached with backoff)."""
        now = time.time()
        cache_key = f"ollama|{self.host}"
        if now < _MODEL_FAIL_BACKOFF.get(cache_key, 0):
            cached = _MODEL_CACHE.get(cache_key)
            if cached:
                logger.debug("Serving stale Ollama model list due to backoff host=%s", self.host)
                return cached['models']
            return []
        cached = _MODEL_CACHE.get(cache_key)
        if cached and (now - cached['ts']) < _MODEL_CACHE_TTL:
            logger.debug("Ollama model cache hit host=%s age=%.1fs", self.host, now - cached['ts'])
            return cached['models']
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
                raw_models = data.get('models') or data.get('data') or []
                if isinstance(raw_models, list):
                    for m in raw_models:
                        if isinstance(m, dict):
                            name = m.get('name') or m.get('model') or m.get('id')
                            if isinstance(name, str):
                                models.append(name)
            build_sec = time.time() - t0
            _MODEL_CACHE[cache_key] = {'models': models, 'ts': now, 'build_sec': build_sec}
            logger.info("Ollama models fetched host=%s count=%d build=%.2fs", self.host, len(models), build_sec)
            return models
        except Exception as e:
            logger.error("Error contacting Ollama API host=%s err=%s", self.host, e)
            _MODEL_FAIL_BACKOFF[cache_key] = time.time() + _MODEL_FAIL_BACKOFF_SECONDS
            return cached['models'] if cached else []

    def list_models_with_host_info(self) -> List[Dict[str, str]]:
        """List models with host information for display in UI."""
        logger.info(f"Listing Ollama models with host info from {self.host}")
        try:
            response = requests.get(f"{self.host}/api/tags")
            response.raise_for_status()
            models_data = response.json().get("models", [])

            # Extract host name for display
            if '://' in self.host:
                host_name = self.host.split('://')[1].split(':')[0]
            else:
                host_name = self.host.split(':')[0]

            models_with_info = []
            for model in models_data:
                if isinstance(model, dict) and 'name' in model:
                    models_with_info.append({
                        'name': model['name'],
                        'host_url': self.host,
                        'host_name': host_name,
                        'display_name': f"{host_name} - {model['name']}"
                    })

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
        from ..services.settings_service import get_ollama_hosts_list, get_disable_remote_ollama_scan

        all_models: List[Dict[str, str]] = []
        hosts_list = get_ollama_hosts_list()
        remote_scan_disabled = get_disable_remote_ollama_scan()

        if remote_scan_disabled:
            logger.info("Remote Ollama host scanning is disabled - only scanning local hosts")

        for host_info in hosts_list:
            # Skip remote hosts if remote scanning is disabled
            if remote_scan_disabled and not host_info.get('is_local', False):
                logger.debug(f"Skipping remote host {host_info['url']} - remote scanning disabled")
                continue

            try:
                # Create a temporary client for this host
                temp_client = OllamaClient(host=host_info['url'])
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
            self.client.pull(model_name) # Use the client instance
            logger.info(f"Model {model_name} is available.")
        except Exception as e:
            logger.error(f"Failed to pull Ollama model {model_name}: {e}")
            # Decide if you want to raise the error or just log it

class OpenAIClient(LLMInterface):
    """Client for interacting with the OpenAI API."""

    DEFAULT_MODEL = "gpt-3.5-turbo"

    def __init__(self, api_key: Optional[str] = None, api_key_env_var: Optional[str] = "OPENAI_API_KEY"):
        """
        Initialize the OpenAI client.

        Args:
            api_key: Optional direct API key
            api_key_env_var: Name of environment variable containing the API key
        """
        env_val = os.environ.get(api_key_env_var) if api_key_env_var else None
        self.api_key = api_key or env_val or ''
        if not self.api_key:
            raise ValueError(f"OpenAI API key not provided or found in environment variable {api_key_env_var}.")
        self.client = openai.OpenAI(api_key=self.api_key)
        logger.info("OpenAIClient initialized.")

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

        if not hasattr(response, 'choices') or not response.choices:
            raise RuntimeError(f"OpenAI {model} response contained no choices")

        choice = response.choices[0]
        if not hasattr(choice, 'message') or not hasattr(choice.message, 'content'):
            raise RuntimeError(f"OpenAI {model} response missing required message fields")

        content = choice.message.content
        if not content or not isinstance(content, str):
            raise RuntimeError(f"OpenAI {model} response content invalid: {type(content)}")

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
            finish_reason = getattr(choice, 'finish_reason', None)
            if isinstance(finish_reason, str):
                if finish_reason == 'length':
                    msg = f"Response from {model} was truncated due to length limits"
                    logger.warning(msg)
                    warnings.warn(msg)
                elif finish_reason not in ('stop', ''):
                    msg = f"Unusual finish_reason '{finish_reason}' from {model}"
                    logger.warning(msg)
                    warnings.warn(msg)

        return content.strip()

    def generate(self, prompt: str, context: Optional[List[Dict[str, Any]]] = None, model: Optional[str] = None, llm_params: Optional[Dict[str, Any]] = None) -> str:
        """Generate a response using the OpenAI API. Raises RuntimeError on failure."""
        # REFACTORING_NOTE: Model is now determined by the get_llm_client factory.
        target_model = model or self.DEFAULT_MODEL

        logger.info(f"Generating response using OpenAI model: {target_model}")
        print(f"[LLM PROMPT][OpenAI][{target_model}]: {prompt}")
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
                **openai_params
            )
            logger.debug(f"OpenAI raw response: {response}")

            # Track which model actually processed the request
            actual_model = getattr(response, 'model', target_model)
            # Only warn about model mismatch if the caller explicitly requested a model
            if model is not None and isinstance(actual_model, str) and actual_model != target_model:
                # Warn only when the actual model appears less capable than requested
                def _tier(name: str) -> int:
                    if not isinstance(name, str):
                        return -1
                    name_l = name.lower()
                    if 'gpt-4' in name_l or name_l.startswith('o1-'):
                        return 4
                    if 'gpt-3.5' in name_l or 'gpt-3' in name_l:
                        return 3
                    # Fallback baseline
                    return 1
                if _tier(actual_model) < _tier(target_model):
                    logger.warning(f"Model mismatch - Requested: {target_model}, Used: {actual_model}")
                    warnings.warn(f"Model mismatch - Requested: {target_model}, Used: {actual_model}")

            content = self.validate_model_response(response, actual_model)
            print(f"[LLM RESPONSE][OpenAI][{actual_model}]: {content}")

            # Log token usage if available
            if hasattr(response, 'usage'):
                logger.info(f"Token usage - Input: {response.usage.prompt_tokens}, Output: {response.usage.completion_tokens}")

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

    def list_models(self) -> List[str]:
        """List available models from OpenAI (focus on GPT models) with caching/backoff."""
        cache_key = 'openai:models'
        now = time.time()

        # Check cache
        entry = _MODEL_CACHE.get(cache_key)
        if entry:
            age = now - entry.get('fetched_at', 0)
            if age < _MODEL_CACHE_TTL and not entry.get('error'):
                entry['hit_count'] = entry.get('hit_count', 0) + 1
                logger.debug(f"OpenAI models cache hit (age={age:.1f}s)")
                return entry.get('models', [])

        # Backoff on recent failures
        last_fail = _MODEL_FAIL_BACKOFF.get(cache_key, 0.0)
        if last_fail and now - last_fail < _MODEL_FAIL_BACKOFF_SECONDS:
            logger.warning(
                f"Skipping OpenAI model list call due to backoff; {now - last_fail:.1f}s since last failure < {_MODEL_FAIL_BACKOFF_SECONDS}s"
            )
            if entry and entry.get('models'):
                return entry.get('models', [])
            return []

        logger.info("Listing available OpenAI models (cache miss or expired).")
        start = time.time()
        models: List[str] = []
        error_msg: Optional[str] = None
        try:
            models_response = self.client.models.list()
            for model in getattr(models_response, 'data', []):  # type: ignore[attr-defined]
                mid = getattr(model, 'id', None)
                if isinstance(mid, str) and 'gpt' in mid:
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
            'models': models,
            'fetched_at': now,
            'build_time': duration,
            'error': error_msg,
            'source': 'refresh' if not entry else 'refresh-expired',
            'hit_count': 0 if entry is None else entry.get('hit_count', 0)
        }

        if error_msg:
            _MODEL_FAIL_BACKOFF[cache_key] = now
            # If we had a previous successful list, fall back to it
            if entry and entry.get('models'):
                logger.warning("Returning stale OpenAI model list due to error")
                return entry.get('models', [])
            raise RuntimeError(error_msg)

        return models


class GeminiClient(LLMInterface):
    """Client for interacting with the Google Gemini API."""

    def __init__(self, api_key: Optional[str] = None, default_model: str = "gemini-pro"):
        if genai is None:
            raise ImportError("google.generativeai package is required for GeminiClient. Install with: pip install google-generativeai")
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("Gemini API key not provided or found in environment variables (GEMINI_API_KEY).")
        try:
            genai.configure(api_key=self.api_key)  # type: ignore[attr-defined]
        except Exception:
            logger.debug("genai.configure not available; continuing without explicit config")
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
             logger.error(f"Failed to initialize Gemini model '{self.default_model}': {e}")
             raise ValueError(f"Failed to initialize Gemini model '{self.default_model}'. Check model name and API key.") from e


    def generate(self, prompt: str, context: Optional[List[Dict[str, Any]]] = None, model: Optional[str] = None, llm_params: Optional[Dict[str, Any]] = None) -> str:
        """Generate a response using the Gemini API. Raises RuntimeError on failure."""
        target_model_name = model or self.default_model
        logger.info(f"Generating response using Gemini model: {target_model_name}")
        print(f"[LLM PROMPT][Gemini][{target_model_name}]: {prompt}")

        try:
            # Select the model - re-initialize if different from default
            if target_model_name == self.default_model:
                model_instance = self.client
            else:
                try:
                    model_instance = genai.GenerativeModel(target_model_name)  # type: ignore[attr-defined]
                    logger.info(f"Using specified Gemini model: {target_model_name}")
                except Exception as e:
                    logger.error(f"Failed to initialize specified Gemini model '{target_model_name}': {e}")
                    raise RuntimeError(f"Could not initialize Gemini model '{target_model_name}': {str(e)}") from e

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
                print(f"[LLM RESPONSE][Gemini][{target_model_name}]: {content}")
                return content
            elif response.prompt_feedback and response.prompt_feedback.block_reason:
                block_reason = response.prompt_feedback.block_reason
                logger.warning(f"Gemini response blocked due to: {block_reason}")
                raise RuntimeError(f"Gemini response blocked due to {block_reason}.")
            else:
                logger.warning("Gemini response was empty or blocked without specific reason.")
                raise RuntimeError("Gemini returned an empty or blocked response.")

        except Exception as e:
            logger.error(f"Error generating response with Gemini model {target_model_name}: {e}", exc_info=True)
            raise RuntimeError(f"Gemini error: {str(e)}") from e

    def list_models(self) -> List[str]:
        """List available models from Gemini."""
        logger.info("Listing available Gemini models.")
        try:
            model_names = []
            try:
                gm_iter = genai.list_models()  # type: ignore[attr-defined]
            except Exception:
                return []
            for m in gm_iter:
                name = getattr(m, 'name', None)
                methods = getattr(m, 'supported_generation_methods', [])
                if isinstance(name, str) and isinstance(methods, list) and 'generateContent' in methods:
                    model_names.append(name)
            logger.debug(f"Found Gemini models: {model_names}")
            return sorted(model_names)
        except Exception as e:
            logger.error(f"Error listing Gemini models: {e}", exc_info=True)
            return []

def get_llm_client(client_type: Optional[str] = None, force_init: bool = False, user_concept_id: Optional[str] = None, org_concept_id: Optional[str] = None, **kwargs) -> LLMInterface:
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
        logger.info(f"Overriding database setting with explicit client type: {client_type}")
        provider = client_type.lower()
    else:
        # Use resolve_llm_setting to get user > org > global precedence
        from ..services.settings_service import resolve_llm_setting
        active_llm = resolve_llm_setting(user_concept_id=user_concept_id, org_concept_id=org_concept_id)
        if active_llm:
            provider = active_llm.get("provider")
            model = active_llm.get("model")
            host = active_llm.get("host")  # Get the specific host for this model
            scope = active_llm.get("scope", "unknown")
            logger.info(f"LLM setting resolved from scope: {scope}")
        else:
            logger.warning("No active LLM setting found. Defaulting to Ollama.")
            provider = "ollama"

    logger.info(f"--- LLM Client Selection ---")
    logger.info(f"Provider: {provider}, Model: {model}, Host: {host}")

    if provider == "openai":
        if _openai_client:
            # Pass the model to the generate method, don't set it on the client instance
            return _openai_client
        else:
            logger.error("OpenAI provider was selected, but the client is not available. Check API key.")
            # Fallback to Ollama if available
            if _ollama_client:
                logger.warning("Falling back to Ollama client.")
                return _ollama_client
            else:
                raise RuntimeError("No LLM clients available")

    elif provider == "ollama":
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
                    if ("." in potential_host and not potential_host.startswith("http")) or ":" in potential_host:
                        effective_host = potential_host
                        # Extract just the model name without the host prefix
                        model = parts[1].strip()
                        logger.info(f"Extracted host '{effective_host}' and model '{model}' from global model selection")

                        # Update the active host in settings so the UI reflects the change
                        try:
                            from ..services.settings_service import set_active_ollama_host
                            set_active_ollama_host(effective_host)
                            logger.info(f"Set '{effective_host}' as active Ollama host in settings")
                        except Exception as e:
                            logger.warning(f"Failed to update active Ollama host in settings: {e}")
                            # Continue anyway, as the client will still work with the extracted host

        try:
            # Prefer already-initialized global client to avoid duplicate inits unless a specific host is requested
            if effective_host is None and _ollama_client is not None:
                logger.info("Using existing Ollama client instance.")
                return _ollama_client
            logger.info(f"Creating Ollama client for host: {effective_host or 'default'}")
            return OllamaClient(host=effective_host)
        except Exception as e:
            logger.error(f"Failed to initialize Ollama client for host '{effective_host}': {e}")
            # Fallback to the globally initialized client if it exists
            if _ollama_client:
                logger.warning("Falling back to default Ollama client.")
                return _ollama_client
            else:
                raise RuntimeError("No LLM clients available") from e

    elif provider == "gemini":
        try:
            return GeminiClient(**kwargs)
        except Exception as e:
            logger.error(f"Failed to initialize Gemini client: {e}")
            if _ollama_client:
                logger.warning("Falling back to Ollama client.")
                return _ollama_client
            else:
                raise RuntimeError("No LLM clients available")

    else:
        # Invalid provider should raise ValueError instead of defaulting
        if client_type:  # Only raise if explicitly provided invalid type
            raise ValueError(f"Unknown provider '{provider}'")
        else:
            logger.warning(f"Unknown provider '{provider}'. Defaulting to Ollama.")
            if _ollama_client:
                return _ollama_client
            else:
                raise RuntimeError("No LLM clients available")

def validate_openai_config(env_var: str, model: str, key: str) -> tuple[bool, list[str]]:
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
    elif not model.startswith(('gpt-', 'o1-', 'text-', 'davinci', 'curie', 'babbage', 'ada')):
        messages.append("Unusual OpenAI model name format")

    # Check API key
    if not key or not key.strip():
        messages.append("No API key found in environment variable")
    elif len(key) < 20:
        messages.append("API key seems too short for a valid OpenAI key")
    elif not key.startswith('sk-'):
        messages.append("API key doesn't follow expected OpenAI format (should start with 'sk-')")

    is_valid = len(messages) == 0
    return is_valid, messages

def get_active_model_name() -> Optional[str]:
    """Helper function to get the model name from the active LLM setting."""
    # Try to get user/org context from session if available
    try:
        from flask import session, has_request_context
        from ..security.access_control import get_effective_user_concept_id
        from ..services.settings_service import resolve_llm_setting

        if has_request_context():
            user_concept_id = get_effective_user_concept_id()
            org_concept_id = session.get('organisation_concept_id')
            active_llm = resolve_llm_setting(user_concept_id=user_concept_id, org_concept_id=org_concept_id)
            if active_llm:
                return active_llm.get("model")
    except Exception:
        pass

    # Fallback to global setting
    active_llm = get_active_llm_setting()
    if active_llm:
        return active_llm.get("model")
    return None
