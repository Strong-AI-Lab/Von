#!/usr/bin/env python
"""Utility script: ensure the internal MCP orchestrator can load the detector.

This is NOT a pytest test module.

Usage (PowerShell):
    pdm run python utilities/check_detector_orchestrator_load.py
"""

import logging

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("utilities.check_detector_orchestrator_load")

    catalogue = build_default_catalogue()
    transport = InternalMCPTransport()
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=transport,
        enabled=True,
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        logger=logger,
        max_tool_invocations=1,
    )

    detector = orchestrator._get_missing_tool_call_detector()
    if not detector:
        print("❌ Detector failed to load")
        return 1

    model = detector.model if detector.model else "(default)"
    print("✅ Detector loaded successfully")
    print(f"   Action ID: {detector.action_id}")
    print(f"   Prompt ID: {detector.prompt_id}")
    print(f"   Model: {model}")
    print(f"   Prompt text length: {len(detector.prompt_text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
