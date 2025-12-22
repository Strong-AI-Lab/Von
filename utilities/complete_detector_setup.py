"""Complete the missing tool call detector setup - add text and relationship."""

from src.backend.services.text_value_service import upsert_text_for_concept
from src.backend.db.repositories.meta_relations_repository import (
    MetaRelationsRepository,
)

action_id = "#V#detect_missing_tool_call_action"
prompt_id = "#V#detect_missing_tool_call_prompt"

prompt_text = """You are a binary classifier for missing tool calls in an assistant reply. Input: one assistant message (not user text). Output: exactly one token: yes or no. Answer yes if the message promises or implies calling/using a tool or action (e.g., "I'll fetch", "Let me check", "I will use the Jira tool", "I'll run a query", "I'll search") but the message does not include any valid structured tool invocation payload (no JSON/function-call/tool-call block). Answer no if a valid tool call payload is present, or if the message makes no promise to call a tool (including "I cannot do that", explanations, or plain answers). Never add explanations or punctuation—output only yes or no.

Response:
{response}"""

print("Adding prompt text...")
upsert_text_for_concept(prompt_id, "hasContent", prompt_text)
print("✓ Added prompt text")

print("Creating uses_prompt relationship...")
MetaRelationsRepository.create_meta_relation(
    source_concept_id=action_id, predicate="uses_prompt", target_concept_id=prompt_id
)
print("✓ Linked action to prompt")

print("\n✅ Detector fully configured!")
print(f"Action: {action_id}")
print(f"Prompt: {prompt_id}")
