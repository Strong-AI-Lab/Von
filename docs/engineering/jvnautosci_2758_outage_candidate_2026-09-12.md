# Mobile outage candidate — 12 September 2026

- **Kind:** dated implementation and evidence handoff
- **Lifecycle:** release evidence
- **Authority:** evidence only; Jira JVNAUTOSCI-2758 owns the live decision
- **Owner:** Codex DGX, for Michael Witbrock
- **Review trigger:** candidate validation, status-route preparation or release

## Delivery boundary

User job: recover from an unreachable Von on mobile, retain useful work, and
show an attributable return-time estimate when one exists. The fair baseline
is the existing `evaluateServerHealthState` and `main.js` polling/backoff.
The candidate adds presentation, a small public-only service-worker fallback,
and an optional operator-bound deployment status publisher. No model calls,
new workflow, HTTP write endpoint, credentials or database migration are involved.

The initial publication was blocked on production status routing. The 14 September
operator repair and final integration evidence below supersede that observation.
Live delivery status belongs to JVNAUTOSCI-2758 and PR #665; deployment is performed
and verified by the receiving controller, not this coding process.

## Candidate behaviour

- The existing sustained-failure decision opens a dismissible full-screen
  dialog, retaining the DOM and focus. Active thinking suppresses takeover.
  Check status uses the existing poll; recovery closes the dialog. Restart
  auto-reload is suppressed during outage recovery, a non-empty composer or
  active thinking. Dependency-only failures retain their existing handling.
- The service worker controls `/von/` and intercepts only home navigations and
  four allowlisted public fallback files. Installation fetches release-scoped
  bytes using same-origin access, retaining only asset bytes and content type.
  It never caches app HTML, APIs, health responses, histories, files or
  credentials. The fallback handles network errors and HTTP 5xx, including
  proxy error pages. Fresh backend health plus app HTML is needed to reopen.
- An unsent composer-text copy survives a reload in the same browser tab using
  namespace-bound `sessionStorage`. It is exposed only after authentication and
  organisation bootstrap, as a reviewable recovery copy, never auto-filled or
  resent. Scope switching hides that copy. Attachments, other composers and a
  discarded browser session are not persisted by this helper; open-page state
  is retained without reload. Storage eviction/quota can limit recovery.
- `/von-status/maintenance.json` is a proposed operator-bound independent
  read-only static route. The browser retains only allowlisted public status
  fields. Missing, stale, failed or exceeded estimates remain unknown/delayed.
  Source, update time, the user's local time zone and cached provenance are shown.
- `scripts/codex_von_maintenance.py` publishes an atomic public record from
  deployment configuration, before stop where configured. ETA comes only from
  an explicit operator estimate, never a health timeout. Successful verified
  deployment/rollback publishes `ready`; failed recovery publishes `failed`.
  A crash leaves the prior update to expire. Publication failures are recorded
  without rolling back an otherwise healthy release.

## Evidence and its limits

Baseline checkout: `ab10a303e` (`origin/main` on inspection).
Read-only local production `/health` reported clean revision
`8401c3ac375bd5288da541686892e70fcd0db75d`, AgentTest false. That is runtime context,
not acceptance of this candidate. No live service was stopped or deployed.

Targeted checks: 22 backend tests and 24 frontend tests passed; the component
browser fixture passed. Evidence and screenshots are retained in
`.run/outage-2758/` in the task worktree.

- `pdm run pytest tests/test_codex_von_deploy.py tests/test_codex_von_maintenance.py tests/backend/test_versioned_static.py -q`
- `node node_modules/jest/bin/jest.js --runInBand src/frontend/web/von_interface/static/js/test/outageStatus.test.js src/frontend/web/von_interface/static/js/test/serverHealthState.test.js`
- `PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/outageComponents.cjs EVIDENCE_DIR`
- Footer-build and login-template regression suites (8 of the 24 frontend tests).
- Changed main/test JS static lint and `git diff --check`.

The Chromium component/network fixture uses production outage modules, the
health evaluator, and the service worker. It verifies portrait (390×844),
landscape (844×390), keyboard-sized (390×350) layouts, transient/active-thinking
non-takeover, retained open draft, restored focus, outage reload/reopen, device
offline, same-tab recovered draft after reconnect, exactly four cached public
files, and zero HTTP effects. It does **not** exercise full authenticated
`main.js` polling, an installed physical phone, public OAuth, or actual DGX
planned-stop publication. Backend lifecycle tests cover publication order,
verified completion, rollback and failed recovery with a disposable file.

## 14 September reviewed continuation

Recovered PR #665 and merged `origin/main` at `22acdc223982ae34a72f5e32003a4cf74562342b`
without conflicts. Real acceptance found and repaired three candidate defects:

- Healthy runtimes explicitly skipping startup materialisation could not leave
  the offline shell. Explicit `skipped` now permits recovery, still requiring
  healthy status and fresh marked app HTML.
- Selecting an organisation after initial page load left draft recovery bound
  to the initial scope. Recovery now rebinds after the confirmed switch,
  removes the previous listener/view and keeps namespace isolation.
- The recovery panel occupied a narrow mobile composer control column. It now
  spans the composer width; an authenticated reload on final product revision
  `2c7eb97e7e05d40d8cf3b1e4c7a65b7fb732f177` verified a readable 360px recovery
  panel in a 390px viewport, with expanded draft and discard control.

Tested product revision: `efa7f0dda6c650e87488abfe093915dcd6c8f021`.
`pdm run` passed 22 targeted backend tests; Jest passed 17 outage/health tests.
The production component browser passed three viewports, focus restoration,
transient and active-thinking non-takeover, offline reload/reopen, recovery,
public-only caching and zero effects. `git diff --check` passed.

Authenticated Chromium at 390×844 through the operator's independent localhost
front (5091), backed by exact-candidate AgentTest (5087), verified synthetic
Alice login and conversation creation, transient health failure without
takeover, actual controlled backend stop, sustained outage dialog, retained
open draft, independent status HTTP 200 during outage, reload/reopen fallback,
device-offline wording and recovery after restart. The fixture creates a new
signing key on every restart, so recovery correctly reached login; after normal
synthetic login and organisation selection the original draft was available as
an explicit recovery copy. Zero generate/send-message requests occurred. Cache
inspection found exactly the four public fallback assets. Screenshots inspected.
This is desktop Chromium mobile viewport acceptance, not a physical installed
phone or public OAuth test.

Evidence is retained under `.run/outage-resume/`: `acceptance-final.json`,
`acceptance.cjs`, `authenticated-outage.png`, `recovered.png`, final `recovery-layout.json`/`.png`, and component
receipts/screenshots. Earlier failed probes are retained to explain the fixes.
The supplied canonical archive
`#V#computer_file_copy_45ea7ecc36eb4f76a78ad59e6c44e5ec` was checksum-verified:
`d0400a6ae476eca9b3f01f90759a35ff4e154685d8ac562a7db4f9ea02225b51`.
Its controlled planned-stop proof showed attributed ETA HTTP 200 while backend
returned 503 and cleared ETA only after exact-candidate health 200. That proof
belongs to revision `2ded69af7`; the publisher is unchanged in this continuation.
Fresh browser acceptance observed that retained record as stale and displayed
return time unknown, rather than inventing an estimate.

## Historical operator handoff (superseded by repair below)

The old authentication/setup blocker is resolved. Do not prepare another login
fixture or ask Michael to sign in. The fixture proxy launcher encountered a
read-only host log path; its existing foreground `serve` mode worked without
changing its bindings. Candidate ports 5087 and 5088 are confirmed closed after acceptance. The
foreground proxy session ended on interrupt, but port 5091 still listens. The
operator must identify and stop only this fixture proxy; its retained status
command reports PID 3158922, which is not proof of current process ownership.

The current remaining dependency is **production independent status binding**:

- A fresh anonymous GET of the public status URL returns HTTP 302 to Cloudflare
  Access, so the browser's credential-free status fetch cannot consume it.
- The local production backend status path returns HTTP 404. Local health is
  healthy, AgentTest false, clean revision
  `632af9653609da91218e781278b21c8b5c69acc2`.
- These observations do not prove whether a private ingress/configuration
  binding exists. Controller credentials/configuration are outside coding scope.

The coordinating operator should inspect the existing ingress and supply or
prepare a bounded route mapping **only** `/von-status/maintenance.json` to an
operator-owned public file independently of backend port 5000, with anonymous
GET access (no Access redirect), no listing or HTTP write route. Bind
`public_status_root` and `maintenance_agent` in the deployment controller and
install the maintenance module with the coherent release bundle. ETA remains
optional and explicitly reasoned; never derive it from the health timeout.
Keep private application routes behind their existing access boundary.
If this requires infrastructure changes, use an explicitly scoped operator task
with a route/configuration rollback; code deployment alone is not that grant.

Return observed route/publisher bindings without credentials, public JSON
read-back, an independent-origin/readiness proof without an unnecessary public
outage, and the rollback reference. Then resume PR #665, required CI, merge and
controller deployment of the current merged main revision. Verify public/static
revision and readiness, and publication/clearing through the actual release.
No deployment SHA is requested before this dependency is resolved.

A cold first visit with no installed service worker, a cleared cache, and an
expired Cloudflare Access session remain explicit infrastructure/session
boundaries. No public ingress, controller configuration or database was changed
by this coding run. Canonical task updates belong to the receiving controller.

## 14 September production-route repair and final integration

The controller supplied canonical file copy
`#V#computer_file_copy_7f218d32c4104a0eac1cf8c6425657cd`, SHA-256
`94dfc533c89653a2a35902e51021dc2ec6a0716432c23887d114e4ec07529dd1`.
The archive checksum was independently verified. Its operator handoff and receipts
record a separate fixed-file read-only status service, exact public ingress,
controller publisher binding, coherent deployment-module installation and
configuration rollback under the operator's `outage665-public-status-2a003`
evidence root. Private app routes retained Access redirects; the status child
path was denied and POST returned 405. The abandoned fixture proxy was stopped.
The operator verified attributed planned publication and cleared it only after
matching local/public readiness. Production was not stopped for that probe.
A fresh coding-run anonymous GET at 03:15:53 UTC returned HTTP 200 JSON with
`Cache-Control: no-store`, ready state and no ETA, consistent with that receipt.

Current main `2b9379cfc` introduced push notifications after the previous outage
acceptance. Integration exposed competing worker registrations for `/von/`.
Both features now register `/von/service-worker.js`, which imports the existing
release-scoped outage worker script. The push handler, subscription, scope and
binding cache remain in the same registration; outage cache cleanup remains
confined to outage caches. Both registration callers disable import caching for
release updates. The template retains both the PWA manifest and outage styles.

Final targeted checks passed: 22 backend deployment/maintenance/static tests and
29 frontend outage/health/push tests. The Chromium component fixture now loads
the combined production worker, verifies its active URL, delivers a real worker
message to disable a synthetic local push binding, and then proves the outage
cache still supports offline reload/reopen, recovery, draft retention and zero
HTTP effects. Three mobile viewports passed and screenshots were inspected.
Evidence: `.run/outage-release/components/`. This supplements the retained full
authenticated acceptance above; it is not a new physical-device or push-provider
display claim. Push privacy, duplicate suppression and click confinement are
covered by the existing handler tests running with the imported outage script.

The publication decision is recorded on PR #665. Controller deployment must
read back the merged public/static revision and readiness and retain the actual
maintenance publication/clearing receipt. Physical phones, cold uncached first
visits and expired Cloudflare sessions remain the previously stated limits.
