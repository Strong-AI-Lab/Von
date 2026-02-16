"""Internal MCP gateway package scaffolding.

Provides a minimal but functional interface for calling backend services through
an internal Model Context Protocol bridge. The gateway is deliberately kept
lightweight so the surface can evolve without forcing early runtime coupling.
"""

from typing import TYPE_CHECKING

from .gateway import (
    InternalMCPGateway,
    MethodDefinition,
    MethodCatalogue,
    GatewayDisabledError,
)
from .catalogue import build_default_catalogue
from .transport import InternalMCPTransport
from .schemas import Schema, SchemaValidationError, validate_payload

if TYPE_CHECKING:
    from .orchestrator import (
        CancellationRequested,
        InternalMCPChatOrchestrator,
        OrchestratorResult,
        ProgressTracker,
        ToolCallParsingError,
    )

_ORCHESTRATOR_EXPORTS = {
    "CancellationRequested",
    "InternalMCPChatOrchestrator",
    "OrchestratorResult",
    "ProgressTracker",
    "ToolCallParsingError",
}


def __getattr__(name: str):
    if name in _ORCHESTRATOR_EXPORTS:
        from .orchestrator import (
            CancellationRequested,
            InternalMCPChatOrchestrator,
            OrchestratorResult,
            ProgressTracker,
            ToolCallParsingError,
        )

        exports = {
            "CancellationRequested": CancellationRequested,
            "InternalMCPChatOrchestrator": InternalMCPChatOrchestrator,
            "OrchestratorResult": OrchestratorResult,
            "ProgressTracker": ProgressTracker,
            "ToolCallParsingError": ToolCallParsingError,
        }
        value = exports[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    "InternalMCPGateway",
    "MethodDefinition",
    "MethodCatalogue",
    "GatewayDisabledError",
    "Schema",
    "SchemaValidationError",
    "validate_payload",
    "build_default_catalogue",
    "InternalMCPTransport",
    "CancellationRequested",
    "InternalMCPChatOrchestrator",
    "OrchestratorResult",
    "ProgressTracker",
    "ToolCallParsingError",
]
