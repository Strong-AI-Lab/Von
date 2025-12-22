#!/usr/bin/env python
"""Fix the missing tool call detector prompt to include {response} placeholder."""

from src.backend.services.text_value_service import (
    get_texts_for_concept, 
    TextRelationsRepository
)
from src.backend.services.text_value_service import upsert_text_for_concept
from bson import ObjectId

prompt_id = '#V#missing_tool_call_detection_prompt'

# New prompt with {response} placeholder
new_prompt_text = """You are a binary classifier for missing tool calls in an assistant reply. Input: one assistant message (not user text). Output: exactly one token: yes or no. Answer yes if the message promises or implies calling/using a tool or action (e.g., "I'll fetch", "Let me check", "I will use the Jira tool", "I'll run a query", "I'll search") but the message does not include any valid structured tool invocation payload (no JSON/function-call/tool-call block). Answer no if a valid tool call payload is present, or if the message makes no promise to call a tool (including "I cannot do that", explanations, or plain answers). Never add explanations or punctuation—output only yes or no.

Response:
{response}"""

print(f'Fixing detector prompt: {prompt_id}')
print(f'New prompt length: {len(new_prompt_text)} chars')

# Get existing hasContent relations
texts = get_texts_for_concept(prompt_id)
has_content_relations = [t for t in texts if t.get('predicate') == 'hasContent']

print(f'Found {len(has_content_relations)} existing hasContent relations')

# Delete old hasContent relations
for text_rel in has_content_relations:
    rel_id = text_rel.get('relation_id')
    if rel_id:
        try:
            TextRelationsRepository.delete_one({'_id': ObjectId(rel_id)})
            print(f'  ✓ Deleted relation {rel_id}')
        except Exception as e:
            print(f'  ⚠ Error deleting {rel_id}: {e}')

# Add the new text with {response} placeholder
print('Adding new prompt text...')
result = upsert_text_for_concept(
    prompt_id, 
    'hasContent', 
    new_prompt_text,
    lang='en'
)

print(f'✅ Updated detector prompt')
print(f'  Text value ID: {result["text_value_id"]}')
print(f'  Relation ID: {result["relation_id"]}')

# Verify
texts = get_texts_for_concept(prompt_id)
for t in texts:
    if t.get('predicate') == 'hasContent':
        text = t.get('text', '')
        print(f'\n✅ Verification:')
        print(f'  Length: {len(text)} chars')
        if '{response}' in text:
            print(f'  ✅ Contains {{response}} placeholder')
        else:
            print(f'  ❌ Missing {{response}} placeholder!')
