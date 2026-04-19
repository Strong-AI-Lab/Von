You are inferring durable workflow identity metadata for a Von workflow-authoring request.

Request text:
{request_text}

Task:
- Infer a concise reusable workflow name that captures the requested workflow behaviour.
- Optionally return an explicit `target_workflow_id` only when the request already implies or explicitly names a clean canonical workflow concept id.
- Infer a brief `workflow_description` that describes the intended user-visible behaviour.

Return strict JSON only with this shape:
{
  "schema_version": "workflow_authoring_identity_inference.v1",
  "target_workflow_name": "Concise workflow name",
  "target_workflow_id": null,
  "workflow_description": "Brief workflow description"
}

Rules:
- Prefer short reusable names over prompt-shaped names.
- Keep `target_workflow_name` language-level and readable; do not add hashes, timestamps, or long prompt excerpts.
- Return `target_workflow_id` only when it is explicitly supported by the request or clearly implied as an existing canonical concept id. Otherwise return `null` and let runtime derive a deterministic id from the chosen name.
- Preserve the requested workflow behaviour, but do not restate the full request verbatim.
- Do not add markdown fences, commentary, or extra keys.
