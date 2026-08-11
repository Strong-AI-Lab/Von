"""Internal MCP gateway package scaffolding.

Provides a minimal but functional interface for calling backend services through
an internal Model Context Protocol bridge. The gateway is deliberately kept
lightweight so the surface can evolve without forcing early runtime coupling.
"""

from typing import TYPE_CHECKING

from .gateway import (
    INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
    INTERNAL_MCP_UNTRUSTED_PAYLOAD_ACTOR_SOURCE,
    GatewayDisabledError,
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
    internal_mcp_actor_context_is_trusted_local_operator,
    internal_mcp_actor_context_is_untrusted_payload_fallback,
)
from .catalogue import build_default_catalogue
from .transport import (
    InternalMCPHandlerCancelled,
    InternalMCPTransport,
    get_internal_mcp_execution_scope,
    internal_mcp_cancellation_requested,
    raise_if_internal_mcp_cancelled,
)
from .schemas import Schema, SchemaValidationError, validate_payload
from ...services.request_progress_service import (
    CancellationRequested,
    ProgressTracker,
)

if TYPE_CHECKING:
    from .orchestrator import (
        InternalMCPChatOrchestrator,
        OrchestratorResult,
        ToolCallParsingError,
    )

_ORCHESTRATOR_EXPORTS = {
    "InternalMCPChatOrchestrator",
    "OrchestratorResult",
    "ToolCallParsingError",
}


def __getattr__(name: str):
    if name in _ORCHESTRATOR_EXPORTS:
        from .orchestrator import (
            InternalMCPChatOrchestrator,
            OrchestratorResult,
            ToolCallParsingError,
        )

        exports = {
            "InternalMCPChatOrchestrator": InternalMCPChatOrchestrator,
            "OrchestratorResult": OrchestratorResult,
            "ToolCallParsingError": ToolCallParsingError,
        }
        value = exports[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    "INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE",
    "INTERNAL_MCP_UNTRUSTED_PAYLOAD_ACTOR_SOURCE",
    "CancellationRequested",
    "GatewayDisabledError",
    "InternalMCPChatOrchestrator",
    "InternalMCPGateway",
    "InternalMCPHandlerCancelled",
    "InternalMCPTransport",
    "MethodCatalogue",
    "MethodDefinition",
    "OrchestratorResult",
    "ProgressTracker",
    "Schema",
    "SchemaValidationError",
    "ToolCallParsingError",
    "build_default_catalogue",
    "get_internal_mcp_execution_scope",
    "internal_mcp_actor_context_is_trusted_local_operator",
    "internal_mcp_actor_context_is_untrusted_payload_fallback",
    "internal_mcp_cancellation_requested",
    "raise_if_internal_mcp_cancelled",
    "validate_payload",
]
