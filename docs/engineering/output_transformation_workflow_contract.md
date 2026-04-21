# Output Transformation Workflow Contract

Date: 2026-02-20

## Purpose

Define one reusable workflow-output envelope for response transformations so route code can stay minimal and diagnostics stay consistent.

Current first-class adopter: `#V#chat_buttonify_workflow` (`transform_name=buttonify`, `transform_version=v1`).

## Contract (`output_transformation_workflow_contract_v1`)

Workflow actions should emit:

1. `schema_version`
2. `transform_name`
3. `transform_version`
4. `stage_metadata`
5. `input_payload`
6. `output_payload`
7. `diagnostics`

### Field shape

```json
{
  "schema_version": "output_transformation_workflow_contract_v1",
  "transform_name": "buttonify",
  "transform_version": "v1",
  "stage_metadata": {
    "workflow_id": "#V#chat_buttonify_workflow",
    "stage_id": "extract_options"
  },
  "input_payload": {
    "screen_text_chars": 128,
    "user_prompt_chars": 42
  },
  "output_payload": {
    "options": ["Proceed", "Hold"],
    "source": "llm",
    "prompt_id": "#V#buttonify_prompt_v1",
    "prompt_truncated": false
  },
  "diagnostics": {
    "status": "success",
    "suppression_reason": null,
    "error_class": null,
    "model": "openai:gpt-5-mini"
  }
}
```

## Route integration pattern

Route code should:

1. Invoke the transform workflow with final screen text and transform settings.
2. Validate emitted options shape.
3. Fail closed / no-op when workflow output is empty or invalid; do not reconstruct options from prose.
4. Attach output + diagnostics to existing response-transformation telemetry.

## Additional transform candidate

Design-ready candidate for the same contract pattern: screen cleanup/finalisation transformation over `screen_text` prior to renderer emission (for example fence normalisation or compact layout rewrites).
