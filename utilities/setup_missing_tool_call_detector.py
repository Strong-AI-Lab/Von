"""Setup the missing tool call detector configuration in the Vontology."""

from src.backend.vontology.utils_vontology import create_vontology_concept
from src.backend.services.relationship_write_service import add_relationship
from src.backend.services.text_value_service import upsert_text_for_concept

# Create the detector action concept
action_result = create_vontology_concept("#V#thing", "detect_missing_tool_call_action")
action_id = action_result["concept"]["concept_id"]
print(f"Created action: {action_id}")

# Create the prompt concept
prompt_result = create_vontology_concept("#V#thing", "detect_missing_tool_call_prompt")
prompt_id = prompt_result["concept"]["concept_id"]
print(f"Created prompt: {prompt_id}")

# Add the prompt text
prompt_text = """Analyze this LLM response and determine if the model said it would perform an action using tools but did not actually emit a tool call.

Response to analyze:
{response}

Answer with ONLY "yes" or "no":
- yes: The model explicitly stated it would perform an action ("I will", "I'm going to", "Let me", "Executing", etc.) but did not include a tool call JSON object
- no: The model either (a) included a valid tool call, (b) did not promise to take action, or (c) only described what it would do without claiming to do it now

Answer:"""

upsert_text_for_concept(prompt_id, "hasContent", prompt_text)
print("Added prompt text")

# Create uses_prompt relationship
add_relationship(action_id, "uses_prompt", prompt_id)
print(f"Linked action to prompt: {action_id} -> {prompt_id}")

print("\nDetector configured successfully!")
print(f"Action ID: {action_id}")
print(f"Prompt ID: {prompt_id}")
