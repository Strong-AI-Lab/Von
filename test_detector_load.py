#!/usr/bin/env python
"""Test that the detector can now be loaded successfully."""

from src.backend.integrations.internal_mcp.orchestrator import InternalMCPChatOrchestrator
import logging

# Enable detailed logging
logging.basicConfig(level=logging.INFO)

# Create orchestrator (this will trigger detector loading)
orchestrator = InternalMCPChatOrchestrator(
    chat_session_id='test',
    user_concept_id='#V#michael_witbrock',
    logger=logging.getLogger('test'),
    auxiliary_prompt_text=None
)

# Try to load the detector
detector = orchestrator._get_missing_tool_call_detector()

if detector:
    print("✅ Detector loaded successfully!")
    print(f"  Prompt ID: {detector.prompt_id}")
    print(f"  Prompt text length: {len(detector.prompt_text) if detector.prompt_text else 'None'}")
    print(f"  Model: {detector.model}")
else:
    print("❌ Detector failed to load")
