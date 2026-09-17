# Unread-message navigation latency evidence — 17 September 2026

Evidence record for native task
`#V#task_agent_43536ba9d1dd211b359a0cf4709f8707`.
The task remains the delivery/decision surface.

Source screenshot: `#V#computer_file_copy_ce8757decd8044169fdb4ae3fd5f74d5`,
SHA-256 `3034e3c2c05f2ef86940c8277c5b351af4a45a537960675ee89c9d02a2f7b199`.
The staged original was inspected; it shows the disabled “Loading unread
messages…” action. It provides no measured duration or server trace.

## Diagnosis and matched measurement

At baseline `9fdc8b623`, `jumpToFirstUnread` exhausts every earlier page,
reconciling/rendering the growing message list on each response. It does this
even when an unread message is already visible, because older unread gaps may
exist. This makes client requests, transport and rendering scale with complete
history, rather than the target window.

`tests/browser/messageUnreadLatency.cjs` exercises the production pane/CSS in
Chromium with 1,000 synthetic messages, 50 messages per page and a fixed 50 ms
HTTP response delay. Unread messages are at positions 700 and 980; the catalogue
count deliberately understates them. IntersectionObserver is disabled for this
latency comparison so read acknowledgements cannot change the target.

| Viewport width | Baseline | Candidate | Requests before/after | Rendered messages before/after |
| --- | --- | --- | --- | --- |
| 390 px | 2,083.5 ms | 66.3 ms | 19 / 1 | 1,000 / 50 |
| 1,280 px | 2,616.1 ms | 77.4 ms | 19 / 1 | 1,000 / 50 |

These are matched local fixture observations (final candidate rerun), not production percentiles
or measured Atlas query latency. The candidate asks the existing scoped exchange
service to locate the earliest unread, then returns a bounded chronological
window. Both reads retain actor visibility, exact participants, organisation and
deleted-message filtering. Earlier/later cursors retain history access. A window
with later pages pauses latest-page polling until the reader loads through it or
uses Latest message, avoiding disconnected ranges in the displayed transcript.
No database migration, index change, represented write or model request is used.

## Validation and limitations

- Backend exchange and route tests: scope/group/deletion/recipient filtering,
  authenticated actor selection, unread gaps, equal-time ordering, bidirectional
  pagination and empty results.
- Frontend exchange tests: one-request seek, stale counts, retry, pending
  position preservation, conversation switching, later pagination and return to
  latest; existing read acknowledgement and draft coverage.
- `tests/browser/messageUnreadNavigation.cjs`: desktop/phone navigation, draft
  preservation, visible-read receipts and catalogue reconciliation, using real
  IntersectionObserver in its read-state cases.
- `tests/browser/messageUnreadLatency.cjs`: timed click-to-focused-target with
  the production frontend; the candidate asserts the fixture's one-second target.

Local evidence is retained under `.run/unread-latency-baseline`,
`.run/unread-latency-candidate` and `.run/unread-navigation` in the task worktree.
The baseline module copy is `.run/unread-baseline/messagePanel.js`.

There is no prepared authenticated browser profile bound to this task checkout.
The host browser README was inspected; its retained profiles belong to other
checkouts/tasks and were not reused. Private database access and live mutations
are outside this worker request. Thus the production bottleneck's exact share,
actual database query plan, and public under-one-second target remain unverified.
The observed history traversal is independently sufficient to explain delay and
its removal is a bounded improvement. A future authorised live measurement can
use a task-bound, read-only operator-prepared profile without asking Michael to
log in. No deployment is requested by this task.
