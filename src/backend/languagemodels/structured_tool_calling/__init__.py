"""Structured tool calling for LLM clients.

This module provides a unified interface for structured tool invocation across
different LLM providers (OpenAI, Gemini, Ollama). Structured tool calling
replaces JSON-in-text parsing with provider-native function calling or
constrained decoding, enabling:

- Deterministic tool invocation (eliminating parsing ambiguity)
- Reliable workflow step execution (JVNAUTOSCI-803)
- Accurate execution trace recording
- Provider-agnostic abstraction

Design principles:
- Canonical internal representation (ToolCall, ToolDefinition)
- Provider adapters handle native APIs (OpenAI functions, Gemini, etc.)
- Graceful fallback to constrained decoding or JSON-in-text (behind feature flags)
- Async/sync support for concurrent workflow execution
- Call IDs for execution tracing
"""

from .types import (
    ToolCall,
    ToolDefinition,
    ToolResult,
    LLMContinuation,
    LLMResponse,
    ToolCallError,
    StructuredToolTransportError,
    UnsupportedStructuredToolTransportError,
    StructuredToolCapabilityRejectedError,
    StructuredToolProtocolError,
)
from .client import LLMClient, LLMClientConfig
from .factory import get_llm_client

__all__ = [
    "ToolCall",
    "ToolDefinition",
    "ToolResult",
    "LLMContinuation",
    "LLMResponse",
    "ToolCallError",
    "StructuredToolTransportError",
    "UnsupportedStructuredToolTransportError",
    "StructuredToolCapabilityRejectedError",
    "StructuredToolProtocolError",
    "LLMClient",
    "LLMClientConfig",
    "get_llm_client",
]
