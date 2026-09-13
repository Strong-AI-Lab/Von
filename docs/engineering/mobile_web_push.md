# Mobile Web Push — JVNAUTOSCI-2757

- **Kind:** Bounded implementation and operator handoff
- **Lifecycle:** Candidate; not accepted or deployed
- **Owner:** Von maintainers, for Michael Witbrock
- **Scope:** Direct-message notification opt-in for one actor/organisation context
  per app installation; multiple devices enrol independently
- **Review trigger:** Delivery acceptance, supported browser/provider changes,
  authentication changes, or service-worker work in JVNAUTOSCI-2758
- **Decision surface:** [JVNAUTOSCI-2757](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2757)

## Capability and boundary

Settings → Conversations offers Enable, a once-per-minute test, Refresh status,
and device-wide Disable. Browser permission is requested only by Enable.
Unsupported and uninstalled iOS cases explain what is needed. Denied/dismissed
permission does not cause another automatic prompt. Lost worker state requires
an explicit reconnect. An existing confirmed opt-in renews on a Settings visit;
unvisited subscriptions expire after 30 days.

The manifest uses the existing Von artwork. The service worker handles push and
clicks only; it neither caches authenticated responses nor intercepts fetches.
Coordinate `/von/service-worker.js`, `/von/manifest.webmanifest`, and manifest
links with the independent PWA task before merging either candidate.

Canonical direct messages remain the source of truth. A separate leased sender
reads recipient-scoped message concepts, including coding-agent messages, without
depending on workflow launch or invoking a model. Notifications concern messages
created since opt-in and within the past day. Read messages are skipped. A
recipient/organisation/date index supports the bounded scan, and an indexed
anti-join against terminal receipts handles late canonical inserts without
repeating completed deliveries. There is no second task-event alert source.

Subscriptions and navigation receipts are derived transactional records in
`web_push_subscriptions` and `web_push_receipts`. Endpoints and browser keys are
encrypted with the existing token-encryption helper. Receipt URLs and notification
tags are opaque, contain no sender/recipient identifiers or message text, and
expire after seven days. Lock-screen text is always generic; richer previews are
outside this delivery.

Browser actor identity comes from the authenticated, assurance-checked server
session. A signed HttpOnly SameSite device cookie scopes enrolment/revocation.
The existing actor-bound window context supplies the organisation, with a live
membership check. A challenge sent exclusively through the proposed endpoint
must return from the same signed-in device before activation. JSON mutation
requests require the same Origin. Provider URL restrictions and disabled HTTP
redirects prevent a submitted endpoint from granting arbitrary server requests.
Supported providers are FCM, Mozilla's production push endpoint, and Apple's
`*.push.apple.com` endpoints.

Each enrolment has a fresh generation, preventing queued pushes from an earlier
account or scope from matching a reused endpoint. The service worker rechecks
the live actor/device/generation before display. Logout revokes the server record
before clearing authentication; a storage failure leaves logout visibly
unfinished for retry. Browser cleanup also unsubscribes and closes notifications.
Current organisation membership and canonical message access are rechecked on
click. Only the same-origin `/von/?notification=OPAQUE_RECEIPT` route can open.

**Delivery limits:** validating the current session requires a working connection
to Von at receipt time. Offline, expired-login, or unavailable-origin deliveries
may be missed; WebKit may revoke subscriptions that repeatedly cannot display.
The app exposes re-enrolment and retains canonical messages. In-flight OS alerts
and system notification history cannot be recalled reliably. This limitation
needs evaluation on the actual supported device before acceptance.

Provider requests have a ten-second transport timeout and a 60-second provider
TTL. Three attempts maximum are counted durably before I/O, with one/two-minute
retry delays. A 120-second lease permits recovery after process death. A stable
provider Topic and notification tag, plus worker receipt memory, suppress repeat
alerts; this does not claim exactly-once OS delivery. Provider acceptance is
recorded separately from display. Permanent 404/410 responses retire endpoints.

## Protected configuration and rollback

The operator must provide these through the existing protected runtime
configuration; do not copy them into the checkout, browser evidence or logs:

- `VON_WEB_PUSH_PRIVATE_KEY`: base64url VAPID EC private key in the format accepted
  by `pywebpush`, or an operator-protected PEM path.
- `VON_WEB_PUSH_PUBLIC_KEY`: matching base64url uncompressed P-256 public key.
- `VON_WEB_PUSH_SUBJECT`: VAPID contact URI (`mailto:` or HTTPS).
- `VON_WEB_PUSH_ENCRYPTION_KEY`: dedicated Fernet key for subscription storage.

The worker starts only with complete configuration. Missing configuration leaves
the UI explicitly unavailable. Startup failure is logged without subscription
material. PDM installs the locked `pywebpush` dependency. New indexes and TTL
collections are created at sender startup; there is no rewrite of existing
concept meaning, migration database, or user membership.

Keep the encryption/VAPID keys across code rollback. Stop the candidate sender
by reverting to the previous coherent release or removing its configuration and
restarting through the controller. Retain the encrypted derived stores for
recovery; do not delete canonical messages. The worker has no fetch interception,
and after rollback its missing validation endpoint prevents further notification
display. Before rotating either key, disable enrolments through the service and
require fresh opt-in; replacing the encryption key alone makes old subscriptions
unreadable. The controller's code rollback is not key/configuration rollback.

## Evidence and remaining operator work

The candidate has targeted backend tests for challenge binding, foreign
actor/device/scope denial, canonical coding reports with suppressed workflow
launch, delayed insertion, duplicate/retry/crash handling, opt-out, expiry,
membership removal, logout and access-checked navigation. Frontend tests exercise
the actual worker handler and Settings module. Synthetic worker events in unit
tests do not establish a real push or OS notification display.

The disposable transport fixture is reproducible with:

```sh
pdm run python tests/browser/webPushFixture.py
PLAYWRIGHT_BROWSERS_PATH=/tmp/von-playwright node tests/browser/webPushSmoke.cjs
```

It uses generated in-memory keys, an in-memory database, synthetic authenticated
actors and the candidate's actual API, Settings section and service-worker file.
It does not read live configuration or call a model. Stop the fixture after use.
For a prepared isolated display the runner accepts `VON_PUSH_HEADED=1`.

On 12 September 2026, Chromium 153.0.8010.12, Linux ARM64, an ordinary headless
390×844 tab registered the real worker and rendered the denied state correctly.
`Notification.permission` was `denied` while the injected Permissions API state
was `granted`; the real `PushManager.subscribe` call returned
`AbortError: Registration failed - permission denied`. No subscription, provider
send, installed app, background/closed-app alert, or device display was verified.
Evidence is retained in `.run/web-push-smoke/receipt.json` and `controls.png` in the
task worktree. An isolated headed attempt failed because the retained Xvfb binary
crashed starting display `:97`; no other task's display/profiles were repurposed.

**Merge decision: not ready.** The task requires an actual supported push path;
the available browser transport has not provided one. Unit tests and registration
do not satisfy that criterion. Source publication must preserve this boundary.

Bounded operator preparation needed under the existing task authority:

1. Prepare a dedicated notification browser bridge bound to this task worktree
   and its current candidate SHA, with separate synthetic Alice/Bob sessions and
   an isolated disposable database. Use
   `/home/mjw/.codex-von-worker/studio-browser/README.md`; the listed retained
   profiles belong to other tasks. Place bridge requests/receipts under this
   worktree's `.run/notification-browser/`; allocate unused localhost ports and
   verify roots, SHA and actor scopes through health/auth read-back.
2. Supply an isolated working headed display and a browser/profile supporting
   actual Web Push. Keep test VAPID/encryption keys outside the coding checkout;
   permit its normal provider transport. Browser fixture login must preserve
   real memberships and must not enrol real users for synthetic tests.
3. Demonstrate the normal authenticated opt-in/challenge, a canonically created
   recipient message and actual background notification, click, test and disable.
   Recheck unrelated-account denial and queued delivery after logout. Record OS,
   browser, installed/tab state and real provider method. Android and iOS physical
   evidence remains unavailable here; distinguish available-platform acceptance
   from those phone results.
4. Repair Jira connector authentication or have the coordinating operator update
   the engineering decision surface; the worker's read returned an invalid-grant
   reauthentication error. Do not mark the task delivered during this handoff.
5. Once acceptance supports merge, complete CI/merge, verify `origin/main`, then
   request the exact merged SHA through the controller. Verify protected runtime
   configuration, actual sender activation, HTTPS manifest/service-worker paths,
   public served revision and the installed-app path. No deployment was performed
   or requested by this unfinished candidate.

## Platform sources checked

- [WebKit: Web Push for Home Screen apps on iOS/iPadOS](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)
  documents the installation and user-gesture permission requirements.
- [WebKit: Declarative Web Push](https://webkit.org/blog/16535/meet-declarative-web-push/)
  describes a newer backward-compatible route. This candidate retains the
  standard service-worker path because it needs live revocation checks; it does
  not claim declarative offline delivery.
- [MDN Push API](https://developer.mozilla.org/en-US/docs/Web/API/Push_API)
  and [pywebpush's implementation](https://github.com/web-push-libs/pywebpush)
  describe browser subscriptions and encrypted provider transport.
