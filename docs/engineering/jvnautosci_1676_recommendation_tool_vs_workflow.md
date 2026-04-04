# JVNAUTOSCI-1676 Recommendation Tool vs Workflow

Status: working architecture note
Last updated: 2026-04-04 (Pacific/Auckland)
Task: `JVNAUTOSCI-1676`

## 1. Purpose

`JVNAUTOSCI-1676` exists to clarify when paper recommendation in Von should
remain a bounded reusable primitive and when it should be expressed as a
workflow family, especially for research-group use cases.

This note is intentionally grounded in the current implementation rather than a
fresh greenfield design.

## 2. Grounded current state

The current paper recommendation vertical slice already has four distinct
surfaces.

### 2.1 Subject profile and context surface

`JVNAUTOSCI-117` established a Vontology-backed recommendation profile for a
subject concept, currently exposed through the settings routes and stored via
the generic paper-matching profile predicate.

Relevant current anchors:

- `src/backend/server/routes/settings_routes.py`
- `src/backend/services/paper_recommendation_profile_vontology_service.py`
- `#V#has_paper_matching_profile_json`

### 2.2 Recommendation evaluation/materialisation primitive

The current recommendation core is a bounded semantic evaluation path that:

- assembles a subject bundle,
- recalls candidate papers,
- applies embedding scoring plus LLM reranking,
- and materialises recommendation assertions plus evaluation JSON into
  Vontology.

Relevant current anchors:

- `src/backend/services/paper_recommendation_materialisation_service.py`
- `src/backend/services/paper_recommendation_vontology_service.py`
- `#V#paper_recommendation_assertion`
- `#V#has_paper_recommendation_assertion`
- `#V#has_paper_recommendation_evaluation_json`

### 2.3 Workflow authority and event-triggered refresh

The current recommendation logic is already tied to a canonical workflow and
prompt authority surface:

- workflow: `#V#paper_recommendation_evaluation_workflow`
- prompt concept: `#V#paper_recommendation_reranker_prompt`
- event type: `paper_recommendation.requested`

The workflow bootstrap and event binding support live in:

- `src/backend/services/paper_recommendation_workflow_vontology_service.py`

The repo seed bundle remains only a transitional publication asset:

- `src/backend/workflows/repo_seed_bundles/paper_recommendation_workflow_seed_bundle.json`

### 2.4 Von-native review surface

`JVNAUTOSCI-1675` established the first authoritative Von-native review path in
the settings tab. It is a bounded inspection surface layered on top of the
recommendation primitive, not the authority for the recommendation logic
itself.

Relevant current anchors:

- `src/backend/services/paper_recommendation_review_service.py`
- `src/backend/server/routes/settings_routes.py`
- `src/frontend/web/von_interface/templates/settings_tab.html`

## 3. Decision

Recommendation in Von should remain both:

- a bounded reusable primitive for explicit ranking/evaluation requests; and
- a workflow family for triggered, stateful, or multi-stage recommendation
  behaviour.

This is not a contradiction. The primitive is the shared capability; workflows
are the authoritative orchestration and policy surfaces that call it when the
task is larger than one bounded evaluation.

## 4. When recommendation is a bounded primitive

Recommendation should stay a tool-like primitive when all or most of the
following are true:

- the caller has an explicit subject concept to evaluate for
- the candidate pool is explicit or can be bounded tightly
- the goal is "rank or inspect these candidate papers now"
- the result can be consumed immediately by a user, route, MCP tool, or another
  workflow step
- there is no multi-actor review/approval chain
- there is no need for scheduling, digests, or downstream follow-on actions
- failure can be reported locally without needing recovery orchestration

Examples:

- "Rank these ten represented papers for this researcher profile."
- "Re-run the current bounded review after a profile save."
- "Show rationale and provenance for the current shortlist."
- "Evaluate whether newly ingested paper X should be considered for subject Y."

Current primitive surfaces:

- `build_paper_recommendations`
- `materialise_paper_recommendations`
- `materialise_paper_recommendations_for_subject(...)`

These should remain reusable support surfaces rather than being replaced by one
bespoke workflow per small ranking request.

## 5. When recommendation should be a workflow family

Recommendation should be expressed as a workflow when one bounded ranking call
is not enough to represent the intended behaviour.

That is the case when any of the following apply:

- recommendation is triggered by events or schedules rather than a direct user
  request
- candidate discovery, evaluation, review, feedback, and downstream action need
  to be chained together
- recommendation must be refreshed or invalidated after KB mutations
- multiple subjects, teams, or projects must be evaluated in one coordinated run
- the result should drive later actions such as notifications, meeting
  preparation, assignment, or follow-up collection
- provenance, staleness, or approval checkpoints matter across steps

Examples:

- new paper ingestion -> identify affected subjects -> refresh recommendations
  -> surface review opportunities
- weekly research-group digest of new papers matched to projects or people
- project-level literature triage for a team rather than one person
- "who in this group should review this paper?" as a multi-subject matching and
  comparison workflow
- meeting-preparation workflows that nominate papers worth discussing and attach
  rationale/provenance

These behaviours belong in workflow authority, not in Python-side orchestration
or in an oversized single MCP tool.

## 6. Recommended authoritative split

### 6.1 Shared primitive authority

The reusable recommendation evaluation capability should stay grounded in:

- subject profile overlay JSON
- materialised recommendation assertion concepts
- evaluation JSON with score, rationale, decision mode, and provenance
- the canonical reranker prompt concept

### 6.2 Workflow authority

Workflow-level behaviour should carry:

- when refreshes happen
- which subject sets are in scope
- how candidate discovery is widened or narrowed
- how recommendations are surfaced for review
- when feedback is requested
- what downstream actions happen after review
- how staleness, invalidation, and retry are handled

In short:

- the primitive decides "how well do these represented papers fit this subject?"
- workflows decide "when, why, for whom, and what happens next?"

## 7. Research-group interpretation

For research-group use cases, the right default is not "replace the primitive
with a single big workflow" and not "keep everything as one tool call".

The better stack is:

- primitive for bounded subject-paper semantic evaluation
- workflow family for group-level orchestration
- review surfaces for human inspection and correction
- later feedback capture for learning/evaluation loops

Recommended early research-group workflow families:

- shared literature triage for one group or project
- role-targeted paper digests
- matching papers to projects or teams rather than only to one user profile
- reviewer suggestion workflows for a new paper
- meeting agenda suggestion workflows that nominate candidate papers for
  discussion

## 8. Immediate next work implied by this note

The next practical step under `JVNAUTOSCI-1553` should be:

1. Keep the current bounded recommendation primitive as the shared evaluation
   surface.
2. Treat the canonical recommendation workflow as the authority for refresh and
   materialisation.
3. Implement `JVNAUTOSCI-116` so feedback is captured against this split rather
   than against a Slack-like one-shot interface.
4. Add later workflow tasks for group-level recommendation families only where a
   bounded primitive call is clearly insufficient.

## 9. Bottom line

Recommendation in Von should not collapse entirely into "just a tool" or "just
a workflow".

The durable architecture is:

- primitive for bounded semantic recommendation evaluation
- workflow family for triggered and multi-stage recommendation behaviour
- Vontology assertions and prompt concepts as the authoritative substrate
- Von-native review and later feedback surfaces on top
