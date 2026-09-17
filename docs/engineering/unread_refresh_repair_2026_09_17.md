# Unread-window refresh repair — 17 September 2026

Evidence for native task `#V#task_agent_5497a7092c696239e79ced69414ac9a1`.
The native task remains the decision surface. This repairs the navigation
regression following PR #740 without requesting production deployment.

## Reproduction and scope

The supplied manager regression fails on `29327080be6ac7235cf2223148d98216176179f4`:
after seeking to an older unread window, manual Refresh renders both that window
and a disconnected latest page (`Expected: false; Received: true`). The test was
run before changing production code and is retained with stronger assertions.

Source evidence, staged and checksum-verified by the controller:

- Patch: `#V#computer_file_copy_7e3e5575afea4192a547e7f076ea9ea5`, SHA-256
  `30a206a193417e211c5ad86b14cdd30ac5a74d93bd0914b5a58f70021692178c`.
- Manager proof: `#V#computer_file_copy_e233925b936642d59fd57885b03e8b69`, SHA-256
  `5f1475d34a8bc0518b5ff7fd34904872b9f02c47e96cc6676787ad8ddf8e5aa8`.

Manual Refresh now replaces an earlier bounded window with the latest page and
resets both cursors. Automatic refresh, including partial read-ack fallback,
cannot join or navigate away from an earlier window. An unconfirmed read retains
its unread label and existing Retry action; explicit Refresh/Latest remains
available. Ordinary latest-page reconciliation also replaces a disjoint result
instead of displaying a gap as continuous history.

Direct callers were inspected: initial open and post-send loading already replace
the window; unread seek and Latest explicitly replace it; earlier/later controls
extend it through chronological cursors; polling has its existing guard. The
repair leaves exchange/actor/organisation selection and composer logic intact.
PR #692 was open during validation; its composer changes were inspected and are
outside the changed production hunks.

## Validation

Tier 1 evidence on the candidate based on the above merge:

- 29 exchange-view tests pass, including all previous 26 and three regression
  cases: manual Refresh, partial read receipt/retry, and disconnected new arrivals.
- 58 tests pass across six exchange/message-panel suites, covering draft, reply,
  send-failure, read and navigation behaviour.
- All 24 existing backend exchange-catalogue/message-route tests pass through
  PDM; no backend code or schema changed.
- Static frontend lint and `git diff --check` pass.
- `tests/browser/messageUnreadNavigation.cjs` passes at 390×850 and 1280×850 in
  Chromium. The actual Conversation actions → Refresh messages control replaces
  the older window, clears the later cursor, preserves the unsent draft, resumes
  polling, and retrieves earlier history with the latest page's cursor. Existing
  seek, visible-read receipt, catalogue and send checks remain passing.

Browser results and inspected screenshots are retained in
`.run/refresh-navigation/` in the task worktree. These use production frontend
modules/CSS with synthetic HTTP responses, not authenticated production data or
public deployment. A partial read receipt while viewing older history remains
uncertain until retry or explicit navigation; labels are never optimistically
cleared. No broader production-health or latency claim is made.
