# Uncertain Relationship Retrieval Contract (JVNAUTOSCI-1314)

## Scope
- Relationship retrieval surfaces now support asserted-only, uncertain-only, and combined retrieval without breaking existing callers.
- Uncertainty metadata is returned in stable fields for APIs and MCP tools.

## Retrieval Controls
- `uncertainty_mode`: `asserted_only` | `uncertain_only` | `include_uncertain`
- `include_uncertain`: boolean convenience flag (equivalent to `uncertainty_mode=include_uncertain` when mode is omitted)
- `uncertainty_statuses`: optional list of assertion statuses to keep (for uncertain rows)

Defaults:
- Existing callers are unchanged (`asserted_only`).

## API: `/api/vontology/relationships/extent`
New query parameters:
- `uncertainty_mode`
- `include_uncertain`
- `uncertainty_status` (repeatable query param)

New source filter value:
- `source=uncertain_assertions`

Returned row additions:
- `is_asserted`: boolean
- `relation_state`: `asserted` | `uncertain`
- `uncertainty`: object or `null` with:
  - `assertion_id`
  - `status`
  - `confidence_score`
  - `provenance`
  - `created_at_utc`
  - `updated_at_utc`
  - `target_kind`

## MCP: `fetch_concept` / `find_relations_with_argument`
New input fields:
- `include_uncertain`
- `uncertainty_mode`
- `uncertainty_statuses`

New output section:
- `uncertainty_diagnostics` with:
  - `mode`
  - `include_uncertain`
  - `statuses`

Relation/hit additions:
- `is_asserted`
- `relation_state`
- `uncertainty` (for uncertain rows/hits)

