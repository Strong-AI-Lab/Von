#!/usr/bin/env python
"""Add detector prompt text to the concept."""

from src.backend.services.text_value_service import upsert_text_for_concept

DETECTOR_PROMPT = """You are a binary classifier for missing tool calls in an assistant reply. Input: one assistant message (not user text). Output: exactly one token: yes or no. Answer yes if the message promises or implies calling/using a tool or action (e.g., "I'll fetch", "Let me check", "I will use the Jira tool", "I'll run a query", "I'll search") but the message does not include any valid structured tool invocation payload (no JSON/function-call/tool-call block). Answer no if a valid tool call payload is present, or if the message makes no promise to call a tool (including "I cannot do that", explanations, or plain answers). Never add explanations or punctuation—output only yes or no.

Response:
{response}"""

concept_id = "#V#missing_tool_call_detection_prompt"
predicate = "hasContent"

result = upsert_text_for_concept(concept_id, predicate, DETECTOR_PROMPT, lang="en")
print(f"Upserted detector prompt text to {concept_id}:")
print(f"  Result: {result}")
print(f"  Prompt length: {len(DETECTOR_PROMPT)} chars")
