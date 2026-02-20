# Concept Relation Extent Display Redesign (Design Only)

## Goal
Redesign the concept-tab `Relationships` section so it behaves more like predicate extent displays:

1. Show all relation assertions where the current concept appears in either argument position (arg1 or arg2).
2. Make relation assertion symmetric, so the user can assert with the current concept fixed as arg1 or as arg2.

This document is design-only and does not include implementation.

## Current State and Gap
- Current UI (`dynamicTabs.js`, `concept_tab.html`) renders grouped chips by predicate and mostly follows outgoing relations.
- Current read route (`GET /vontology/api/vontology/relationships`) returns a convenience map keyed by predicate, but does not provide a canonical row-wise extent view with explicit argument position for the focal concept.
- Current add form always treats current concept as source (`source_id`) and only accepts a target field.

## Proposed UX

### Section Rename and Layout
- Rename section heading from `Relationships` to `Relation Extent`.
- Replace chip-only grouped display with a table-first extent display aligned to predicate extent visual language.
- Keep lightweight predicate grouping as an optional secondary view (collapsed by default).

### Extent Table
Use a table with one row per assertion:

| Column | Purpose |
| --- | --- |
| Role | Whether current concept is `arg1` or `arg2` in this assertion |
| Predicate | Predicate cartouche, clickable |
| Arg1 | Concept/text shown for arg1 |
| Arg2 | Concept/text shown for arg2 (or first object value for n-ary fallback) |
| Source | `structured` or `text_relations` |
| Updated | Timestamp, when available |
| Actions | Remove assertion (existing semantics) |

Notes:
- Reuse cartouche affordances from predicate extent display (type/individual/predicate colouring, click to open, right-click to copy concept_id).
- Keep sorting and filtering parity with predicate extent (`source`, predicate filter, role filter, sort by updated/predicate/role).

### Assert Relation Composer
Below the table, add a single inline composer:

- `Role` control (required): `Current concept is arg1` | `Current concept is arg2`.
- `Predicate` selector/search:
  - include structural + salient + searchable “Other…” predicates.
  - preserve text-predicate detection behaviour from existing form.
- `Counterparty` input:
  - concept search input for concept predicates.
  - text/lang/type inline form for binary text predicates.
- Primary action: `Add assertion`.

Composer behaviour:
- If role is arg1, add relation as `(current_concept, predicate, counterparty)`.
- If role is arg2, add relation as `(counterparty, predicate, current_concept)` for concept predicates.
- For text predicates, role is forced to arg1 (or arg2 option disabled with explanation), because current text APIs are subject/text-value oriented.

## API/Data Contract Design

### Read Path
Add a row-wise extent endpoint for concepts:

- `GET /vontology/api/vontology/relationships/extent?concept_id=<id>&limit=<n>&offset=<n>&source=<optional>&predicate=<optional>&role=<optional>`

Response shape (conceptual):
- `success: bool`
- `concept_id: str`
- `total: int`
- `rows: [ ... ]`

Each row:
- `relation_id`
- `relation_kind` (`structured` | `text`)
- `predicate_id`
- `arg1` (concept id or text payload)
- `arg2` (concept id or text payload)
- `matched_argument_indexes` (include `1` and/or `2`)
- `source`
- `timestamp`

Implementation note:
- Build via existing shared service `build_concept_relations_payload(...)` with:
  - `include_relations_arg1=true`
  - `include_relations_any_arg=true`
  - `include_text_relations_arg1=true`
- Then transform to UI rows (normalised arg1/arg2 fields + role derivation).

### Write Path
Reuse existing routes for this phase:
- Concept predicates: `POST /vontology/api/vontology/relationships/add`
- Text predicates: `POST /api/concepts/<concept_id>/texts`

Directional logic in UI:
- Role `arg1`: existing behaviour.
- Role `arg2`: submit with swapped subject/target for concept predicates.

## Styling and Reuse Plan
- Reuse predicate extent classes where feasible:
  - container/table/filter/status patterns from `.predicate-extent-*`
- Extract shared table helper in follow-up implementation (to avoid duplicating predicate extent/table behaviour).
- Preserve current cartouche classes (`.concept-cartouche.*`) and search dropdown style (`.vontology-search-results`).

## Accessibility and Interaction
- Keyboard support:
  - tab through filters/composer.
  - Enter submits composer.
  - Arrow key navigation in search dropdowns.
- Clear status messages for add/remove success and validation errors.
- Explicit labels for role selection and predicate/counterparty inputs.

## Non-Goals (This Task)
- No ontology semantics changes for structural predicate meaning.
- No new inference rules.
- No migration of all existing relationship chips to cards/charts.

## Acceptance Criteria
1. The concept tab shows a relation extent table with rows where current concept appears as arg1 or arg2.
2. Each row shows predicate and both argument columns, with role indication.
3. User can add a concept-predicate relation with current concept fixed as arg1 or arg2.
4. User can remove rows from the table with existing permission/error behaviour preserved.
5. Filter/sort behaviour matches predicate extent interaction quality.
6. Existing predicate extent display remains unchanged.

## Suggested Wireframe
```text
Relation Extent
[Role: Any v] [Predicate: any] [Source: any] [Apply] [Reset]

| Role | Predicate | Arg1                  | Arg2                  | Source      | Updated           | Actions |
| arg1 | related_to| #V#current_concept    | #V#target_concept     | structured  | 17 Feb 2026 13:04 |   x     |
| arg2 | depends_on| #V#other_concept      | #V#current_concept    | structured  | 17 Feb 2026 12:02 |   x     |
| arg1 | hasName   | #V#current_concept    | "Example label" (en)  | text_rel... | 16 Feb 2026 09:40 |   x     |

Add assertion:
[Current concept is: arg1 v] [Predicate v / search] [Counterparty concept/text input] [Add assertion]
```

## Chat Display Element Extension (Under `JVNAUTOSCI-865`)

### Goal
Add a dedicated display element for chat rendering of relation truth-state groups, with:
- clickable concept cartouches in minimal form, and
- explicit visual variants for asserted vs not-asserted relations.

### Proposed Display Element Type
- `element_type`: `relation_truth_state`
- `intent`: `truth_state_relation_view`
- `channel`: `screen`

This keeps relation-state rendering semantically distinct from generic tables and avoids broad behavioural changes to all existing table renderers.

### Payload Sketch
```json
{
  "title": "Current Truth State",
  "groups": [
    {
      "label": "Conference-level",
      "status": "asserted",
      "assertions": [
        {
          "assertion_id": "a1",
          "arg1": "#V#michael_witbrock",
          "predicate": "#V#attended_event",
          "arg2": "#V#international_ai_cooperation_and_governance_forum_2025_melbourne",
          "is_asserted": true
        }
      ]
    },
    {
      "label": "Missing (should exist)",
      "status": "missing_expected",
      "assertions": [
        {
          "assertion_id": "a2",
          "arg1": "#V#michael_witbrock",
          "predicate": "#V#panelist_in_event",
          "arg2": "#V#panel_4_ai_for_industry_ai_for_society_iaicgf_2025_melbourne",
          "is_asserted": false
        }
      ]
    }
  ]
}
```

### Rendering Behaviour
- Group header:
  - `asserted` -> green/check styling
  - `missing_expected` -> red/cross styling
  - optionally `uncertain` -> amber/warning styling
- Assertion row format (minimal):
  - `[arg1 cartouche] -- [predicate cartouche] --> [arg2 cartouche]`
- Cartouches:
  - clickable to open concept tabs
  - right-click copies concept id
  - compact presentation (name-first with id tooltip)

### Display-Elements Contract Changes
- Add `relation_truth_state` to `ALLOWED_DISPLAY_ELEMENT_TYPES` in `display_elements_service.py`.
- Add payload validator for:
  - `title: string`
  - `groups: list`
  - `group.status` in allow-list (`asserted`, `missing_expected`, `uncertain`)
  - each assertion requiring `arg1`, `predicate`, `arg2`, `is_asserted`
- Add chat renderer in `chatTab.js` alongside current `table/timeline/task_view/workflow_view` handlers.

### Variant Rules (Asserted vs Not Asserted)
- Asserted relation (`is_asserted=true`):
  - normal stroke arrow and neutral/positive text tone.
- Not asserted relation (`is_asserted=false`):
  - dashed arrow or muted line, plus red/amber accent depending on group status.
  - optional suffix label: `not currently asserted`.

### Acceptance Criteria (Chat Element)
1. Chat can render `relation_truth_state` elements without falling back to raw JSON.
2. Each assertion renders three clickable cartouches (arg1, predicate, arg2).
3. Asserted and not-asserted variants are visually distinct and accessible.
4. Existing display element types continue to validate and render unchanged.

## Execution Plan (Relates to `JVNAUTOSCI-865`)

### Plan Intent
Turn the above design into an implementation sequence that:
- keeps `relation_truth_state` as the primary structured rendering pathway under `JVNAUTOSCI-865`,
- closes current clickability/visibility gaps for concept and relation elements, and
- introduces a compact cartouche mode that is visually obvious and space-efficient.

### Workstream Map

#### Workstream A: Shared Cartouche Unification (Foundation for `JVNAUTOSCI-865`)
Objective:
- Remove divergence between legacy `.concept-cartouche` usage and shared `.vontology-cartouche` usage.

Scope:
- Introduce a single renderer helper used by:
  - concept relation extent table (`dynamicTabs.js`),
  - predicate extent table (`predicateExtentDisplay.js`),
  - chat relation truth-state rows (`chatTab.js`).
- Keep existing behaviour (open concept, copy id, keyboard focus), but route through one cartouche constructor.

Acceptance gate:
- All concept-like cells in the two extent tables and relation truth-state rows use the same cartouche element family.
- Right-click copy and click-to-open are consistent across all three surfaces.

#### Workstream B: Compact Cartouche Variant (Core UX part of `JVNAUTOSCI-865`)
Objective:
- Add a dense cartouche presentation where kind colour is expressed as background and the right kind pill is omitted.

Scope:
- Add explicit compact variant support to cartouche rendering (`name-first`, optional id tooltip, no right-side kind pill).
- Ensure kind colour mapping remains:
  - `individual` -> green
  - `type` -> blue
  - `predicate` -> violet
- Reuse existing settings toggle semantics (`kind-as-background`) so behaviour is predictable.

Acceptance gate:
- Relation-heavy rows can render in compact mode without loss of discoverability.
- Visual distinction between type/individual/predicate remains clear in both standard and compact variants.

#### Workstream C: Concept/Predicate Detection Hardening
Objective:
- Make clickability robust when values are concept-like but not already clean `#V#...` strings.

Scope:
- Add a shared value normaliser for renderer inputs (trimming, punctuation stripping, `#v#` normalisation, canonical prefix handling).
- Use backend row metadata (`arg*_is_concept`, relation shape) plus normalisation before deciding plain text vs concept cartouche.
- Keep conservative fallback to plain text for non-concept values.

Acceptance gate:
- Previously missed concept identifiers in extent rows become clickable cartouches.
- No false-positive conversion of clear literal text values.

#### Workstream D: Relation Rendering Priority and Fallback (Explicitly tied to `JVNAUTOSCI-865`)
Objective:
- Ensure relation special handling is visible even when the model emits markdown/prose instead of structured elements.

Scope:
- Primary path stays `display_elements.relation_truth_state` (authoritative for `JVNAUTOSCI-865`).
- Add deterministic fallback parser for simple triple lines (`arg1 -- predicate --> arg2`) when no structured relation display element is present.
- Mark fallback-rendered blocks with provenance metadata for diagnostics.

Acceptance gate:
- If structured `relation_truth_state` exists, renderer uses it.
- If absent but parseable triples exist, relation block still renders with special relation styling.
- If neither exists, existing plain markdown rendering remains unchanged.

### Implementation Order
1. Workstream A (shared cartouche helper in one place).
2. Workstream B (compact variant on top of shared helper).
3. Workstream C (hardening detection in extent renderers).
4. Workstream D (chat relation fallback, preserving `JVNAUTOSCI-865` primary path).
5. Cross-cutting clean-up and documentation update.

Rationale:
- This order minimises churn by stabilising rendering primitives before adding parser/fallback logic.

### Test Plan

#### Frontend Unit/Component Tests
- Update/add tests in:
  - `src/frontend/web/von_interface/static/js/test/chatTab.test.js`
  - predicate extent and dynamic tab tests (new tests if missing)
- Cover:
  - compact cartouche class/structure,
  - click and context menu behaviours,
  - relation_truth_state render precedence,
  - fallback triple parsing only when structured element is absent.

#### Backend Contract Tests
- Keep `relation_truth_state` schema validation tests in `display_elements_service` green.
- Ensure `/relationships/extent` tests still pass and add cases for concept-id normalisation edge patterns where appropriate.

#### Manual Verification Checklist
- Chat response with structured `relation_truth_state` payload.
- Chat response with plain triple markdown only.
- Concept tab relation extent rows containing:
  - canonical `#V#...`,
  - lower-case `#v#...`,
  - trailing punctuation variants.

### Telemetry and Diagnostics
- Add lightweight counters/flags (debug-safe) for:
  - `relation_truth_state_source` (`structured` vs `derived_from_text`),
  - `cartouche_render_count`,
  - `concept_token_parse_miss_count`.
- Include counters in existing debug pathways, not as separate infrastructure.

### Risk Notes
- Main risk is accidental over-linkification of plain text that resembles ids.
- Mitigation:
  - conservative normalisation rules,
  - strict fallback conditions,
  - tests for false positives.

### Suggested Jira Decomposition (under `JVNAUTOSCI-865`)
1. Subtask A: Shared cartouche helper adoption across relation/table renderers.
2. Subtask B: Compact cartouche variant and settings harmonisation.
3. Subtask C: Extent renderer concept-id detection hardening.
4. Subtask D: Chat fallback parser for relation triple lines.
5. Subtask E: Test and diagnostics completion pass.
