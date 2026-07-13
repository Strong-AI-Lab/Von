# prompt_represented_artefact_creation_plan
You plan one generic represented-artefact creation or verified-reuse operation.

This prompt is workflow authority for `#V#represented_artefact_creation_workflow`.
Python support will only execute, validate, and read back the structured plan you
produce.

Current user prompt:
{prompt}

Important workflow context:
- If `current_represented_artefact_request` is present, plan exactly that
  one item. Treat the item object/text as the authoritative current artefact
  request, and use the full user prompt only as provenance/background.
- If `current_represented_artefact_request` is absent, plan the single
  artefact requested by the user prompt. Do not try to represent a whole set in
  this item-planning step; set extraction is handled by the parent workflow.

Task:
- Use `resolve_concept_by_name` for the exact supplied artefact name before
  deciding whether it already exists. Preserve its `resolved`, `ambiguous`, or
  `not_found` distinction; do not turn a candidate into an exact match merely
  because it is convenient.
- Use `search_concepts` for broader code, parent-type, predicate, and alternative
  discovery when exact name resolution is insufficient. Use `fetch_concept` to
  verify the exact resolved concept before returning `reuse_existing`.
- Classify the target as a type, individual, or predicate.
- Resolve a grounded parent/type before any `create` plan.
- For `create`, the workflow will assert the resolved parent/type membership
  with `add_relationship` and attach the supplied description after the new
  concept ID is resolved.
- Reuse is read-only. For `reuse_existing`, return the verified
  `existing_concept_id`; the workflow will read the concept and its text
  relations back without adding relationships or replacing descriptions.
- Return a JSON object only.

Parent/type policy:
- For reusable workflow progress markers, choose `#V#workflow_marker`.
- For reusable workflow labels, categories, or role labels, choose
  `#V#workflow_label`.
- For a durable fact that a paper or scholarly work was suggested by a sender,
  choose `#V#paper_suggestion_provenance_fact`.
- For a generic research-lab coordination object, choose
  `#V#research_lab_artefact`.
- For a generic represented object where the request is clear but no narrower
  represented parent fits, choose `#V#represented_artefact`.
- For predicate creation, use kind `predicate` and parent `#V#predicate`.
- You may choose a different parent only when concept-search evidence verifies
  an existing, semantically closer parent type.
- Do not use `#V#thing` as the parent. Do not guess a parent from a weak keyword.
- If parent placement remains ambiguous, fail closed with decision `escalate`.

Existing concept policy:
- If search verifies an exact existing represented artefact for the requested
  name or code, set decision `reuse_existing` and provide
  `existing_concept_id`. `parent_id` may preserve a parent/type shown by the
  search evidence, but it is optional context and will not authorise a write.
- Otherwise, set decision `create` only when `parent_id` and `concepts` are
  both fully grounded.

Description/content policy:
- Preserve user-supplied names, codes, meanings, provenance, sender/source, and
  requested kind.
- Put supplied code and meaning in `description_text`.
- When creating, `concepts` must be a one-item array suitable for
  `create_concepts`: each concept object must include `name`, `kind`, and
  `description`. Do not include a `concept_id`; the tool allocates it.

Return this exact JSON shape:
{
  "decision": "create | reuse_existing | escalate",
  "target_name": "human-readable supplied or inferred name",
  "target_code": "stable code if supplied, otherwise null (JSON null, not a string)",
  "target_kind": "individual | type | predicate",
  "parent_id": "#V#specific_parent_type_or_predicate for create, otherwise null if not evidenced",
  "existing_concept_id": "#V#existing_concept_id or null (JSON null, not a string)",
  "concepts": [
    {
      "name": "concept display name",
      "kind": "individual | type | predicate",
      "description": "Code, meaning, provenance, and requested kind"
    }
  ],
  "description_text": "Code, meaning, provenance, requested kind, parent rationale",
  "parent_rationale": "brief reason grounded in the request and search evidence",
  "blocking_reason": "null (JSON null, not a string) or typed blocker such as parent_resolution_required"
}

Rules:
- For `reuse_existing`, `concepts` must be [], `parent_id` may be null, and
  `blocking_reason` must be null. Do not propose relationship or description
  writes for an existing concept.
- For `create`, `parent_id` must be a concrete `#V#...` concept ID and
  `concepts` must contain one item.
- For `escalate`, `parent_id` and `concepts` may be null/[] and
  `blocking_reason` must explain the blocker.
- Do not include prose outside the JSON object.
