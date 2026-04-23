You are designing a safe workflow revision candidate in response to an episode critique.

Use the provided context fields:

- `target_workflow_id`
- `existing_workflow_spec`
- `episode_self_improvement_suggestion`
- `episode_self_improvement_benchmark_summary`
- `workflow_authoring_prompt_contract`
- `workflow_authoring_prompt_health`

Return JSON with exactly these keys:

- `target_workflow_id`
- `repair_summary`
- `repaired_workflow_spec`

Rules:

- Propose the smallest workflow-level revision that directly addresses the critique.
- Keep the workflow ID unchanged.
- Preserve unrelated behaviour unless the critique evidence requires a broader fix.
- Respect the workflow-authoring contract and keep executable steps structurally valid.
- Do not invent missing benchmark evidence. If the evidence is weak, make the repair conservative.
- The output must be a valid authoring spec for the same workflow, not prose about a future change.
