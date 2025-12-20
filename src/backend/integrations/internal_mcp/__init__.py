"""Internal MCP gateway package scaffolding.

Provides a minimal but functional interface for calling backend services through
an internal Model Context Protocol bridge. The gateway is deliberately kept
lightweight so the surface can evolve without forcing early runtime coupling.
"""

from .gateway import (
    InternalMCPGateway,
    MethodDefinition,
    MethodCatalogue,
    GatewayDisabledError,
)
from .catalogue import build_default_catalogue
from .transport import InternalMCPTransport
from .schemas import Schema, SchemaValidationError, validate_payload
from .orchestrator import (
    InternalMCPChatOrchestrator,
    OrchestratorResult,
    ToolCallParsingError,
)

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
    "InternalMCPChatOrchestrator",
    "OrchestratorResult",
    "ToolCallParsingError",
]
