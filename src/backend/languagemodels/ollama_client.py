"""
Provides the Ollama client implementation for the LLM interface.
"""

from .llm_interface import OllamaClient

# You can add other Ollama-specific utility functions here if needed,
# that are not part of the LLMInterface contract.

__all__ = ["OllamaClient"]  # Control what 'from .ollama_client import *' imports
