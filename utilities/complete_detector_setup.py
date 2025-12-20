"""Complete the missing tool call detector setup - add text and relationship."""

from src.backend.services.text_value_service import upsert_text_for_concept
from src.backend.db.repositories.meta_relations_repository import MetaRelationsRepository

action_id = '#V#detect_missing_tool_call_action'
prompt_id = '#V#detect_missing_tool_call_prompt'

prompt_text = """Analyze this LLM response and determine if the model said it would perform an action using tools but did not actually emit a tool call.

Response to analyze:
{response}

Answer with ONLY "yes" or "no":
- yes: The model explicitly stated it would perform an action ("I will", "I'm going to", "Let me", "Executing", etc.) but did not include a tool call JSON object
- no: The model either (a) included a valid tool call, (b) did not promise to take action, or (c) only described what it would do without claiming to do it now

Answer:"""

print('Adding prompt text...')
upsert_text_for_concept(prompt_id, 'hasContent', prompt_text)
print('✓ Added prompt text')

print('Creating uses_prompt relationship...')
MetaRelationsRepository.create_meta_relation(
    source_concept_id=action_id,
    predicate='uses_prompt',
    target_concept_id=prompt_id
)
print('✓ Linked action to prompt')

print('\n✅ Detector fully configured!')
print(f'Action: {action_id}')
print(f'Prompt: {prompt_id}')
