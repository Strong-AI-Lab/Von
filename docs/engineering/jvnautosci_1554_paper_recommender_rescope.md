# JVNAUTOSCI-1554 Paper Recommender Re-scope

Status: closure note and backlog redirect
Last updated: 2026-04-04 (Pacific/Auckland)
Task: `JVNAUTOSCI-1554`

## 1. Purpose

`JVNAUTOSCI-1554` exists to reinterpret the old paper recommender work as a
modern workflow-first Von capability rather than a restoration of the earlier
Slack prototype.

This note records:

- what currently exists in code and Vontology,
- which legacy `JVNAUTOSCI-1` subtasks are obsolete, historical, or still
  conceptually relevant,
- and how the active implementation path under `JVNAUTOSCI-1553` was
  re-scoped.

It now serves primarily as closure evidence for `JVNAUTOSCI-1554`. The live
follow-on planning for the next open `JVNAUTOSCI-1553` step should be read from:

- `docs/engineering/jvnautosci_1654_open_epic_review.md`
- `docs/engineering/jvnautosci_1676_recommendation_tool_vs_workflow.md`

## 2. Audited current state

### 2.1 What exists now

The current Von codebase already has a real scholarly-paper representation
substrate:

- canonical workflow concepts for:
  - `#V#scholarly_paper_representation_workflow`
  - `#V#arxiv_paper_representation_workflow`
- paper acquisition and representation tools:
  - `download_paper`
  - `get_paper_metadata`
  - `materialise_scholarly_representation_for_file_copy`
- paper/arXiv workflow publication/bootstrap support in
  `paper_representation_workflow_vontology_service.py`
- targeted paper/arXiv workflow tests and gateway tests across the internal MCP
  path

`JVNAUTOSCI-1415` is already `Done`, and its closure evidence states that the
explicit paper and arXiv workflows were materialised and published to
Vontology.

### 2.2 What does not exist now

No current repo evidence supports the idea that Von still has an active Slack
paper recommender implementation as a first-class product surface. There are
small historical or unrelated Slack references, but no active paper
recommendation path built around Slack app delivery.

So the right interpretation is:

- the old Slack recommender is historical,
- the current substrate is scholarly-paper ingestion and representation,
- and the missing capability is workflow-first recommendation on top of that
  substrate.

### 2.3 Important phase-gating constraint

The broader `JVNAUTOSCI-1657` Phase 2 review concluded that the vertical slice
should not expand as if the context-bundle, dossier, provenance, and event-model
substrate were already complete.

That means the paper recommender should be advanced as a bounded modernisation
plan, but not treated as ready for broad end-to-end rollout until the relevant
substrate tasks land, especially:

- `JVNAUTOSCI-254`
- `JVNAUTOSCI-1563`
- `JVNAUTOSCI-1564`
- `JVNAUTOSCI-531`
- `JVNAUTOSCI-969`
- `JVNAUTOSCI-970`
- `JVNAUTOSCI-971`
- `JVNAUTOSCI-972`

## 3. Legacy task interpretation

### 3.1 Parent task `JVNAUTOSCI-1`

`JVNAUTOSCI-1` is still useful as history, but its description is explicitly
about a Slack-integrated matcher. It should therefore be treated as a legacy
prototype umbrella, not as the literal implementation brief for the modern Von
capability.

### 3.2 Legacy subtasks: mapping

The following mapping is the recommended interpretation of the historical
`JVNAUTOSCI-1` subtasks.

#### Historical input only

These are useful as research or prototype history, but should not drive current
implementation literally:

- `JVNAUTOSCI-2` survey models/tools for paper matching
- `JVNAUTOSCI-5` survey models/tools for paper matching - Alex
- `JVNAUTOSCI-8` curate an initial dataset of papers posted on Slack and project descriptions
- `JVNAUTOSCI-9` collect information on matching accuracy and preferences
- `JVNAUTOSCI-10` first prototype using OpenAI API and Slack app

#### Conceptually relevant, but superseded by modern workflow-first tasks

These describe needs that still matter, but their old wording should not be
implemented literally:

- `JVNAUTOSCI-3` explanation/highlighting methods
- `JVNAUTOSCI-4` survey models/tools for paper matching - Yang
- `JVNAUTOSCI-7` survey explanation methods - Yang
- `JVNAUTOSCI-20` recommend papers after project-description update
- `JVNAUTOSCI-44` extract paper information from PDF files shared in a channel
- `JVNAUTOSCI-45` modify prompts for performance
- `JVNAUTOSCI-46` allow multiple projects per user and modify UI
- `JVNAUTOSCI-47` separate feedback for decision and explanation
- `JVNAUTOSCI-51` make it visually easier to tell which paper is recommended

These should now be covered by the active `JVNAUTOSCI-1553` follow-on tasks,
not by reviving the original prototype structure.

#### Obsolete or prototype-specific

These are tied to the old operational surface and should be treated as obsolete
for the modern implementation path:

- `JVNAUTOSCI-52` Slack-native URL extraction
- `JVNAUTOSCI-56` `/R2D2-help` private-channel command
- `JVNAUTOSCI-62` duplicate deployment/development copy of the whole system

#### Generic prototype engineering work already absorbed historically

Several other old subtasks were generic prototype engineering or packaging work
and are best treated as absorbed historical scaffolding rather than live
backlog:

- database/persistence tidy-up tasks
- logging/style/docstring tasks
- package/readme/deployment-structure tasks
- prompt wording and dev-only notification tweaks

## 4. Modern target capability

The modern paper recommender should be treated as a workflow-first knowledge and
recommendation capability with these layers.

### 4.1 Paper intake and representation

This layer already has meaningful coverage:

- ingest or fetch candidate papers,
- represent them as scholarly-paper concepts,
- persist provenance and file-copy/source linkage,
- expose inspectable workflow and tool telemetry.

This is the substrate created by the current paper/arXiv workflow family.

### 4.2 Researcher recommendation profiles

Von needs a modern way to represent what makes a paper relevant to a person,
team, or project. That should not be a single opaque text box glued to a Slack
profile. It should become a Vontology-native recommendation profile that can be:

- user-visible and editable,
- maintained over time by Von,
- grounded in project descriptions, uploaded artefacts, and already-represented
  papers where appropriate,
- and explained back to the user.

### 4.3 Candidate discovery

Candidate papers can come from several sources:

- explicit paper/arXiv ingestion
- future feed or source integrations
- optional external signals such as Papers with Code or SciSciNet

External sources are enrichments, not the core recommender definition.

### 4.4 Ranking and explanation

This is the core missing capability:

- compare represented papers against represented researcher/project context,
- score or rank candidates,
- emit concise explanation grounded in KB evidence and provenance,
- and fail visibly when the substrate is too incomplete to justify a strong
  recommendation.

### 4.5 Delivery and review surfaces

Recommendation delivery should be separated from the ranking core. Slack may be
one future surface, but it should not again become the authoritative shape of
the capability.

Preferred first surfaces are:

- a Von/web review surface for recommendations,
- triggered recommendations after profile updates or candidate-paper ingestion,
- inspectable recommendation provenance and rationale.

### 4.6 Feedback and evaluation

The system should capture graded user feedback on:

- whether the recommendation itself was useful,
- whether the explanation was useful,
- and any free-text correction that helps future ranking/evaluation.

## 5. Post-rescope backlog snapshot under `JVNAUTOSCI-1553`

### 5.1 What landed from the re-scope

#### `JVNAUTOSCI-117`

This is now `Done` and established the modern researcher recommendation profile
surface:

- editable project/research-interest representation,
- stronger specificity than the old free-text description,
- eventual Von-assisted maintenance with conservative confirmation rules.

#### `JVNAUTOSCI-120`

This is now `Done` and should be read as the first workflow-first ranking and
explanation step on the modern substrate rather than as a request to restore the
old Slack prototype.

The more explicit semantic-evaluation replacement task is now
`JVNAUTOSCI-1679`, which replaced the lexical ranker with workflow-materialised
semantic evaluation.

If Jira still shows open child work beneath `JVNAUTOSCI-1679`, treat that as
the remaining detailed semantic-evaluation tranche rather than reopening
`JVNAUTOSCI-120` as the live brief.

#### `JVNAUTOSCI-1675`

This is now `Done` and established the first authoritative Von-native review
surface:

- a settings-tab recommendation review path,
- inspectable rationale and provenance,
- and bounded trigger points for recommendation inspection.

### 5.2 Current open and later follow-on work

#### `JVNAUTOSCI-1676`

This is the next open architecture/planning step under `JVNAUTOSCI-1553`. It
should clarify:

- when recommendation remains a bounded reusable primitive,
- when it should be expressed as a workflow family,
- and which research-group recommendation use cases should call the primitive
  versus becoming explicit workflows.

#### `JVNAUTOSCI-116`

This remains open (`Backlog`) and should now be read as post-review feedback
capture work:

- implement graded recommendation feedback,
- capture explanation feedback separately from relevance feedback,
- and preserve enough signal for later evaluation, learning, or workflow
  invalidation.

#### `JVNAUTOSCI-270`

Keep as an optional source-enrichment evaluation task for Papers with Code /
Hugging Face Papers.

#### `JVNAUTOSCI-776`

Keep as an optional source-enrichment evaluation task for SciSciNet.

### 5.3 What should no longer be treated as the live backlog brief

Do not continue to read this note as if `JVNAUTOSCI-120` were still the single
active implementation brief for the recommendation core. The April 2026 follow-on
split is now:

- `JVNAUTOSCI-1679` for the semantic evaluation/materialisation replacement,
- `JVNAUTOSCI-1675` for delivery and review surfaces,
- `JVNAUTOSCI-1676` for the next tool-versus-workflow clarification.

## 6. Recommended next interpretation order

1. Keep paper/arXiv representation as the authoritative substrate and repair any
   residual workflow-family issues there if they block downstream work.
2. Treat the landed profile, semantic evaluation, and review surfaces
   (`JVNAUTOSCI-117`, `JVNAUTOSCI-1679`, `JVNAUTOSCI-1675`) as the current base
   capability.
3. Clarify the bounded recommendation primitive versus workflow-family split in
   `JVNAUTOSCI-1676` before adding broader research-group recommendation flows.
4. Implement feedback capture and evaluation (`JVNAUTOSCI-116`) against that
   clarified workflow family.
5. Use `JVNAUTOSCI-270` and `JVNAUTOSCI-776` as optional source-enrichment
   decisions rather than hard blockers.

## 7. Closure interpretation for `JVNAUTOSCI-1554`

`JVNAUTOSCI-1554` should count as complete when:

- the legacy Slack-centred work is explicitly reinterpreted,
- the current paper/arXiv capability is recognised as the real substrate,
- the old subtasks are mapped to obsolete, historical, or superseded status,
- and the active `JVNAUTOSCI-1553` backlog is rewritten to express the modern
  workflow-first implementation path.

That closure condition is now satisfied. This note should therefore remain as a
historical re-scope record, while live `JVNAUTOSCI-1553` follow-on planning
shifts to the later April 2026 task split and linked notes.
