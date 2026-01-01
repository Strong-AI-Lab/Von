#!/usr/bin/env python3
"""Add hasContent text relation to the missing tool call detection prompt."""

import sys
from pathlib import Path

# Add src to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.backend.services.text_value_service import upsert_text_for_concept

# Read the prompt
prompt_file = project_root / "utilities" / "detector_prompt.txt"
prompt_text = prompt_file.read_text(encoding="utf-8")

print(f"Adding hasContent relation to #V#missing_tool_call_detection_prompt")
print(f"Prompt length: {len(prompt_text)} characters")

# Add the content
result = upsert_text_for_concept(
    subject_concept_id="#V#missing_tool_call_detection_prompt",
    predicate="hasContent",
    text=prompt_text,
    lang="en-NZ",
    context={"text_type": "NL"},
)

print(f"Result: {result}")
print("✅ Prompt content added successfully!")
