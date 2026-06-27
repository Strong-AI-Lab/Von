You are inferring structured user-request evidence for proposed write tools.

Current user prompt:
{current_prompt}

Recent user prompts kept for continuation context:
{recent_user_prompts_json}

Requested write tools and payload summaries:
{requested_tools_json}

Task:
- For each requested tool, decide whether the user has clearly requested that tool's side effect.
- Only treat a recent prompt as evidence when the current prompt is a continuation, confirmation, or follow-up that clearly refers back to the recent request.
- Do not treat "this tool could help" as evidence that the user requested the side effect.
- Separately decide whether the user clearly denies that tool's side effect.
- For destructive tools, separately decide whether explicit destructive confirmation is present.
- If the request is unclear, return low confidence rather than guessing.
- For additive ingestion tools, a directly supplied artefact or URL can count as an explicit request for that ingestion side effect when the current prompt clearly presents it for action.
- For external-system side effects, including outbound Gmail/email sending through `gmail_send_message`, the current prompt must clearly ask Von to perform the external action; a draft, list, summary, or hypothetical plan is not enough.
- For `workflow_execute`, use `explicit_request` when the current prompt clearly asks Von to run, execute, represent, materialise, save, add, or otherwise perform the concrete side effect represented by the workflow payload. Do not require the user to name `workflow_execute` literally when the payload names the matching workflow or side effect. Use `low_confidence` when the workflow payload is only a speculative option, a plan, or a tool that could help.

Return strict JSON only with this shape:
{
  "schema_version": "write_tool_request_evidence.v1",
  "tool_evidence": [
    {
      "tool_name": "exact requested tool name",
      "request_state": "explicit_request | recent_request_context | low_confidence",
      "confirmation_state": "explicit_confirmation | recent_confirmation_context | low_confidence",
      "denial_state": "explicit_denial | recent_denial_context | low_confidence",
      "rationale": "brief explanation"
    }
  ]
}

Rules:
- Include exactly one entry for each requested tool.
- Preserve the requested tool name exactly.
- Use `explicit_request` only when the current prompt itself clearly asks for that tool's side effect.
- Use `recent_request_context` only when the recent preserved user context clearly carries the request and the current prompt is a continuation of it.
- Use `explicit_confirmation` only when the current prompt itself authorises a destructive change.
- Use `recent_confirmation_context` only when the current prompt is a clear continuation / approval of a recent destructive request.
- Use `explicit_denial` only when the current prompt itself clearly forbids that tool's side effect.
- Use `recent_denial_context` only when preserved recent context clearly carries the denial and the current prompt is a continuation of it rather than an override.
- Otherwise use `low_confidence`.
