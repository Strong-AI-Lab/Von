"""Factory for instantiating LLM clients with structured tool calling."""

import logging
from typing import Optional

from .client import LLMClient, LLMClientConfig
from .providers.openai_client import OpenAIClient
from .providers.gemini_client import GeminiClient
from .providers.ollama_client import OllamaClient

logger = logging.getLogger(__name__)


def get_llm_client(config: LLMClientConfig) -> LLMClient:
    """Factory function to instantiate the appropriate LLM client.

    Args:
        config: LLMClientConfig with model identifier and options

    Returns:
        Appropriate LLMClient subclass instance

    Raises:
        ValueError: If model is not recognised or dependencies are missing
    """

    model_lower = config.model.lower()

    # OpenAI models
    if any(x in model_lower for x in ["gpt-", "text-davinci", "text-curie"]):
        return OpenAIClient(config)

    # Gemini models
    elif any(x in model_lower for x in ["gemini", "bard"]):
        return GeminiClient(config)

    # Ollama models (default for local/self-hosted)
    elif any(x in model_lower for x in ["ollama", "llama", "mistral", "neural", "local"]):
        return OllamaClient(config)

    # Fallback: try Ollama (most common for local models)
    else:
        logger.info(f"Model '{config.model}' not explicitly matched; attempting Ollama client")
        return OllamaClient(config)
