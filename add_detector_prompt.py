#!/usr/bin/env python
"""Add detector prompt text to the concept."""

from src.backend.services.text_value_service import upsert_text_for_concept

DETECTOR_PROMPT = """You are a classifier that detects when an LLM response claims a tool will be called but does not actually emit a valid tool call invocation.

Analyse the response and answer only:
- "yes" if the response explicitly or implicitly promises a tool call (e.g. "I will search for", "Let me call", "I'll use the") but the response text contains NO valid JSON tool call block
- "no" if either the response contains valid JSON tool call(s) OR the response makes no tool call promise

Be strict: only say "yes" if there is a clear disconnect between what the response says it will do and what it actually does."""

concept_id = '#V#missing_tool_call_detection_prompt'
predicate = 'hasContent'

result = upsert_text_for_concept(concept_id, predicate, DETECTOR_PROMPT, lang='en')
print(f'Upserted detector prompt text to {concept_id}:')
print(f'  Result: {result}')
print(f'  Prompt length: {len(DETECTOR_PROMPT)} chars')
