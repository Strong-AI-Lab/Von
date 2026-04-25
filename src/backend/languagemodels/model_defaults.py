"""Centralised default model names for all LLM providers.

All code that needs a fallback model name should import from here rather than
hardcoding a model string.  Each default is overridable via an environment
variable so operators can change the fallback without code changes.
"""

import os

# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
DEFAULT_OLLAMA_MODEL: str = os.getenv("VON_DEFAULT_OLLAMA_MODEL", "gemma4:26b")

# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------
DEFAULT_OPENAI_MODEL: str = os.getenv("VON_DEFAULT_OPENAI_MODEL", "gpt-5.5")

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------
DEFAULT_GEMINI_MODEL: str = os.getenv("VON_DEFAULT_GEMINI_MODEL", "gemini-2.0-flash")
