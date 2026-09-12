# Execution summary cost preferences

**Kind:** implementation manual  
**Lifecycle:** active  
**Authority scope:** execution-summary presentation and its preference record  
**Owner:** Von maintainers  
**Last reviewed:** 2026-09-12  
**Review trigger:** changes to cost telemetry, viewer scope or preference storage

The Thinking/execution summary shows an estimated USD cost immediately after
duration. Comparisons are strictly greater than: the default display threshold
is US$0.01 and the default high-cost threshold is US$0.10. High costs use red
text and the visible and accessible words “High cost”. This is a display
preference, not a spending limit or an authority grant.

## Represented configuration

`#V#hasExecutionCostDisplayPreferences` is a singleton JSON text relation in
`en-NZ`, attached directly to the relevant user concept for a personal override,
or to an organisation concept for that organisation's default:

```json
{"currency":"USD","display_above":"0.01","alert_above":"0.10"}
```

Both amounts must be non-negative decimal strings (up to 12 integer and 18
fractional digits); the alert threshold must be at least the display threshold.
The pair is atomic: do not combine one user's threshold with one organisation
threshold. Resolution is valid viewer/user record, then valid current
organisation record, then the server defaults above. Empty `{}`, missing,
ambiguous, inaccessible or invalid records inherit the next scope. No record
is created by a read. The authenticated viewer's server-bound window context
selects the organisation, including when viewing someone else's shared history;
neither the conversation owner's preferences nor query-string scope applies.

`GET /api/settings/execution_cost_display` returns the effective pair with
`source` equal to `user`, `organisation` or `default`. Responses are not cached.
`PUT` to the same endpoint replaces the authenticated user's record through
the existing governed singleton-text mutation service. Supply the JSON above,
or `{}` to restore inheritance. It cannot target another user or organisation.
Existing authorised canonical ontology tools can maintain the organisation's
same singleton text relation; their normal publication/organisation authority
applies. No new organisation administration surface is introduced.

The predicate is registered in the existing read-only virtual code-concept
registry as a binary text predicate. Canonical discovery and write validation
can therefore resolve it without a database bootstrap or migration. Preference
values themselves are persisted Vontology text relations, not code constants.
This delivery does not change live user/organisation records. Such represented
changes require canonical tools, read-back and the existing authority context;
direct database writes are not part of this work.

## Telemetry and rendering

The existing per-execution `llm_usage_cost_summary.estimated_cost` is the source,
for both live and retained historical Thinking cards. Newly calculated costs
carry `amount_decimal` and `known_amount_decimal` strings from Decimal arithmetic;
existing numeric fields remain for compatibility. Summary aggregation prefers
the exact strings. Browser comparisons use scaled integers before formatting.
Older numeric-only history uses the stored number's decimal spelling; lost
historical precision cannot be recovered. The UI preserves fractional precision
so a just-over-threshold amount does not appear equal to its threshold.

Loading preferences, missing/unknown cost, partial pricing, non-USD currency,
zero and costs at or below the display threshold leave a reserved slot blank.
They never show a misleading zero or a complete total based on partial pricing.
Delayed final telemetry is rendered on the normal Thinking-card update path.
Preference arrival updates already-mounted historical slots. Context changes
clear cached preferences, and stale responses cannot update a newer context.
Active updates refresh preferences at most once per minute; reloading the page
also refreshes settings. No currency conversion or additional model call occurs.

The slot has a fixed preferred width, remains within the responsive header, and
retains its height during finalisation. Exceptionally long amounts may be
ellipsised; their complete estimate and alert are available in the tooltip and
accessible label. Duration, completion state, activity, diagnostic controls and
the existing detailed cost breakdown retain their existing data sources.

## Validation and delivery boundaries

Targeted Python tests cover preference precedence, invalid/absent configuration,
actor-bound read/write, canonical service integration and exact cost strings.
Frontend tests cover both strict boundaries, historical numeric compatibility,
missing/loading/partial values, accessible high-cost signalling and late scope
responses. `tests/browser/executionCost.cjs` uses the actual chat template,
stylesheet and renderer with local fixture telemetry at desktop/mobile widths;
it checks layout stability, overflow and six cost states. It makes no model
requests and does not use live user data. This fixture evidence does not claim
authenticated live Vontology activation or public deployment.
