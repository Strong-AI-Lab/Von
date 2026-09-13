# Mobile outage candidate — 12 September 2026

- **Kind:** dated implementation and evidence handoff
- **Lifecycle:** unfinished candidate
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

Merge decision: **not ready**. Component evidence supports the UI and fallback,
but does not replace the task's authenticated candidate/PWA acceptance or prove
an independently served production status record. These are blocking because
an installed client must have a usable fallback and the maintenance record
must remain available while the actual backend is stopped. The Jira connector
returned `UNAUTHORIZED / oauth_token_invalid_grant`; the supplied assignment
was used, without inventing inaccessible comments or conversation context.

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

## Bounded operator handoff

The README at `/home/mjw/.codex-von-worker/studio-browser/README.md` was read.
Its listed fixtures belong to other tasks or an immutable release; none is
bound to this candidate. Do not reuse or stop them.

1. Prepare a new task-specific localhost AgentTest profile rooted at
   `/home/mjw/.codex-von-worker/worker-state/worktrees/35c046837b9bd2c7`, using the
   established operator-prepared authentication route, generation disabled and
   existing memberships preserved. Bind a new client/supervisor request bridge
   under this checkout's ignored `.run/` directory. Supply its exact profile,
   port, verified SHA and restart command. No credentials belong in the checkout.
2. Confirm an existing independently served static route can map only
   `/von-status/maintenance.json` to an operator-owned public file while port
   5000 is stopped. No directory listing or HTTP write route. If there is no
   such route, this needs a separately scoped infrastructure setup and recovery
   plan; code deployment alone does not authorise changing Cloudflare ingress.
3. Bind `public_status_root` and `maintenance_agent` in the operator deployment
   configuration. Optional `maintenance_public_reason` must be suitable for
   public display; optional `maintenance_estimate_seconds` must be reasoned from
   the release plan. Default ETA is absent. Keep these fields outside task text
   and install the new maintenance module with the coherent deployment bundle.
4. Provide a disposable independent status route/file in the candidate fixture
   to test planned-stop publication, external read-back, clearing only after
   verified readiness and a stale/failed update. Use isolated network faults or
   the owned fixture stop, never an unnecessary public outage.
5. Resume authenticated browser acceptance, review and publication after that
   setup. Preserve the task worktree. The controller must perform any eventual
   authorised deployment and verify the exact merged `origin/main` SHA plus
   independently served record and public readiness. No deployment SHA is
   requested before the blocking evidence is obtained.

A cold first visit with no installed service worker remains subject to the
proxy/Cloudflare response. A cleared browser cache also removes the fallback.
No independently served cold-visit app route was found in this repository;
this residual infrastructure boundary is not hidden behind a success claim.
