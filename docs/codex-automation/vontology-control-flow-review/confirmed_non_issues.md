# Confirmed Non-Issues And Watch Items

This avoids duplicate filing. Re-check if code or live evidence changes.

## Already Covered

- Broad tool metadata/write/evidence authority: `JVNAUTOSCI-1985`.
- Workflow-purity prompt-source/repo-seed drift: `JVNAUTOSCI-2080`.
- Broad buttonify prose-extraction heuristics: completed `JVNAUTOSCI-1971`.
- Regex-heavy person/company/meeting file-copy inference: completed
  `JVNAUTOSCI-1952`; current issue is downstream materialisation contracts,
  now tracked by `JVNAUTOSCI-2216`.

## Inspected, Not Filed On 2026-04-30

- `paper_recommendation_ranking_service.py`: appeared to be a compatibility
  wrapper; filed real policy drift against materialisation/Vontology/delivery.
- `workflow_selection_policy_service.py` and
  `workflow_selection_experience.py`: learned-selection support; file only if
  token features directly override represented routing authority.
- `renderer_applicability_service.py`: renderer aliases; file only with stronger
  evidence of durable semantic/display-profile authority.
- `workflow_override_policy_service.py` and `workflow_continuation_service.py`:
  watch areas; file only if they override represented workflow/routing policy.
- `workflow_launch_input_contracts.py` arXiv ID extraction: covered by
  `JVNAUTOSCI-2212` as deterministic launch-input support. File only if the
  extractor registry grows into phrase-specific or domain launch policy.

## Inspected, Not Filed On 2026-05-01

- `tool_result_hint_actions.py`: generic action support for Vontology-authored
  signal/follow-up hints. File only if integration-specific hint text or
  selection policy appears in Python.
- `workflow_mcp_tool_actions.py`: generic static MCP invocation support with
  namespace and write guardrails. File only if tool choice or domain process
  policy is added there rather than represented workflow metadata.

## Inspected, Not Filed On 2026-05-02

- `conversation_turn_stage_model.py`: current evidence points to stage
  labelling/telemetry support. File only if stage specs start pruning context,
  changing user-facing response semantics, or overriding workflow authority.
- `renderer_applicability_vontology_service.py`: renderer-profile seed
  blueprints are watch-listed, but the stronger live issue is
  `JVNAUTOSCI-2234` for concept-summary panel construction after applicability.
- `scripts/publish_email_resource_link_extraction_workflow.py`: one-shot
  publication script for a Vontology workflow. File only if it becomes a normal
  runtime workflow source or recurring Python workflow authoring pathway.

Do not file solely because a file is large or contains domain nouns. File when
Python owns durable decision policy, evidence choice, prompt/workflow content,
user-visible copy, or KB/profile semantics.
