# prompt_represented_artefact_set_extraction
You decide whether a represented-artefact request should be handled as one
artefact or as a bounded set of independently representable artefacts.

This prompt is workflow authority for the fan-out step of
`#V#represented_artefact_creation_workflow`. Python support only executes the
structured workflow plan that you return.

Current user prompt:
{prompt}

Task:
- Preserve user-supplied names, codes, meanings, provenance, hierarchy hints,
  source-system identifiers, and workflow/account associations.
- If the request names multiple reusable labels, markers, predicates, types, or
  other durable KB coordination artefacts, return mode `set` with one
  `artefact_specs` item per artefact.
- If the request clearly names a single artefact, return mode `single` and an
  empty `artefact_specs` array.
- If `current_represented_artefact_request` is present in workflow context,
  return mode `single`; this is already an item-level child run.
- Do not call tools in this step. Do not create or mutate concepts here. This
  step only decomposes the request so child item workflows can perform
  grounded resolution, ontology-native mutation, and read-back.

Each `artefact_specs` item must be a self-contained child request. Include:
- exact display name;
- exact stable code or source-system ID when supplied;
- meaning/description;
- kind hint such as workflow label, workflow marker, predicate, type, individual,
  or paper suggestion provenance fact;
- parent/type hint if grounded by the request or prior search evidence;
- hierarchy or association hints needed by later read-back;
- provenance/source text sufficient for the child workflow to preserve context.
- when parent/type placement is grounded enough, a complete child plan so the
  child workflow can skip another LLM planning/search loop and proceed directly
  to mutation/read-back.

For workflow-label set items, use this grounded parent/type:
- `parent_id`: `#V#workflow_label`
- `target_kind`: `individual`
- `decision`: `create`

For each such planned item, include a one-item `concepts` array suitable for
`create_concepts`, preserving the display name exactly, and a `description_text`
that preserves code, meaning, hierarchy hints, association hints, and provenance.

Return this exact JSON shape:
{
  "mode": "single | set | escalate",
  "artefact_specs": [
    {
      "name": "exact or human-readable artefact name",
      "code": "stable code or null",
      "kind_hint": "workflow label | workflow marker | predicate | type | individual | paper suggestion provenance fact | represented artefact",
      "meaning": "meaning/description to preserve",
      "parent_hint": "grounded parent/type hint or null",
      "hierarchy_hint": "hierarchy information or null",
      "association_hint": "workflow/account/profile/source association or null",
      "provenance": "source facts and user request provenance",
      "prompt": "self-contained child prompt for this one artefact",
      "decision": "create | reuse_existing | null",
      "target_name": "exact display name or null",
      "target_code": "stable code or null",
      "target_kind": "individual | type | predicate | null",
      "parent_id": "#V#grounded_parent_or_type_concept_id or null",
      "existing_concept_id": "#V#existing_exact_concept_id or null",
      "concepts": [
        {
          "name": "exact display name",
          "kind": "individual",
          "description": "durable description preserving code, meaning, hierarchy, association, and provenance"
        }
      ],
      "description_text": "durable text preserving code, meaning, hierarchy, association, and provenance",
      "parent_rationale": "why the parent/type is grounded"
    }
  ],
  "set_summary": "brief summary of the represented set or null",
  "blocking_reason": "null or typed blocker"
}

Rules:
- For `single`, `artefact_specs` must be [] and `blocking_reason` must be null.
- For `set`, `artefact_specs` must contain 2 to 16 items.
- For `set`, each item prompt must ask for exactly one represented artefact.
- For `escalate`, `artefact_specs` may be [] and `blocking_reason` must explain
  the blocker.
- Do not include prose outside the JSON object.
