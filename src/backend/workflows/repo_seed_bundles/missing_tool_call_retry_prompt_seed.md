# missing_tool_call_retry_prompt
Your previous message described an action that requires MCP tools, but you did not emit a tool call.

NOW respond with ONLY a tool-call JSON object or a JSON array of tool-call objects.
Choose exact tool names from the available tool list or current tool context.
Do NOT include prose.
Do NOT use Markdown.
Do NOT wrap the JSON in ``` fences, including ```json.
The first character MUST be an opening curly brace or an opening square bracket, and the response must contain only valid JSON.

Do NOT call write tools unless the user explicitly asked to create, save, update, delete, or otherwise mutate represented data.
When the user explicitly asked for a low-risk additive Vontology write and no specialised workflow/tool is available, prefer generic Vontology write tools that are available in the current tool surface: `create_concepts` for the represented instance, `upsert_singleton_text_relation` for supplied content such as `hasContent`, and `add_relationship` only when a minimal owner/date/provenance link is needed.
Do NOT invent domain-specific tool names such as `diary_create`.
