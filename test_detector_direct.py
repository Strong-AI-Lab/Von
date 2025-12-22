#!/usr/bin/env python
"""Test that the detector prompt text exists and can be retrieved."""

from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.concept_service import get_concept_by_concept_id

# Check the detector action concept
action = get_concept_by_concept_id('#V#detect_missing_tool_call_action')
print("✅ Detector action concept found")

# Check it has the right relationships
rels = action.get('relationships', {}) if isinstance(action, dict) else {}
print(f"   Relationships: {list(rels.keys())}")

# Check the prompt ID relationship
uses_prompt_rels = rels.get('#V#uses_prompt', [])
prompt_id = uses_prompt_rels[0] if uses_prompt_rels else None
print(f"   Uses prompt: {prompt_id}")

# Check the prompt concept exists and has content
if prompt_id:
    prompt_texts = get_texts_for_concept(prompt_id)
    content_texts = [t for t in prompt_texts if t.get('predicate') == 'hasContent']
    if content_texts:
        print(f"✅ Detector prompt text found!")
        print(f"   Length: {len(content_texts[0]['text'])} chars")
    else:
        print(f"❌ Detector prompt has no hasContent text")
        print(f"   Available predicates: {set(t.get('predicate') for t in prompt_texts)}")
else:
    print("❌ Detector action has no uses_prompt relationship")
