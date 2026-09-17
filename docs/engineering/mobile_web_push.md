# Mobile Web Push — JVNAUTOSCI-2757

- **Kind:** Bounded implementation and operator handoff
- **Lifecycle:** Local browser acceptance complete; public activation is controller-owned
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

## Accepted local path and activation handoff

On 14 September 2026, the clean candidate
`70fc2819b67975cdc426eda47519106470ffd5ae` passed actual local browser delivery:

- Synthetic Alice and Bob logged in through the normal localhost Browser test
  login. Both task-owned profiles reported the exact candidate in `/health`.
- Settings → Conversations → Enable performed a real PushManager subscription,
  encrypted provider send and device-bound confirmation. The Settings iframe is
  outside `/von/`; the client now awaits the registered worker's activation
  rather than waiting indefinitely for that iframe to be controlled.
- A canonical message from Bob to Alice produced one generic notification while
  Alice's Von tab was closed and her browser remained running. The private
  message body did not appear in the alert. Bob received HTTP 404 attempting to
  open Alice's opaque notification receipt.
- A screenshot of the task-owned desktop showed the actual alert. A real X11
  mouse click opened its canonical URL and the accessible message exchange.
  Device Disable returned `disabled`, removed the PushManager subscription and
  left no notifications. Owned subscriptions were also cleaned up in the
  runner's finalisation path.

Environment: headed persistent Chromium 153.0.8010.12 on Linux ARM64, ordinary
browser tab, automation-granted notification permission, localhost secure
context. An isolated Dunst 1.9.2 notification daemon and private D-Bus supplied
native display/click on task display `:98`; both child processes stopped when
acceptance ended. The default session bus could not query host AppArmor policy
from this sandbox. The private test bus used its own same-user Unix socket and
configuration; no host policy, system service or unrelated desktop was changed.
This is **not** an installed-app, Android-phone, iPhone or public-HTTPS result.

The browser's `PushMessagingGcmEndpointWebpushPath` feature selected its actual
FCM `https://fcm.googleapis.com/preprod/wp/` transport. The unbranded browser's
older `jmt17.google.com/fcm/send/` endpoint rejected its subscription as expired.
The application provider allowlist remains unchanged: no Google-wide or staging
host exception was shipped. A permanent rejection of a confirmation now retires
its server record and clears the rejected browser subscription, offering fresh
opt-in instead of a misleading indefinite pending state.

The earlier diagnostic HTTP 403 came from importing unversioned `apiService.js`
into a document already using the release-scoped module graph. That second
coordinator created a new, unbound window selector. The actual Settings request
succeeded with its actor-bound selector; strict window validation was preserved.
Browser harnesses should use the page's actual module graph or its current
window header, not load a parallel copy of the coordinator.

Evidence is retained in the task worktree's `.run/push-resume/acceptance.json`,
`desktop-alert.png`, `click.png` and `accept.cjs`. The screenshot and canonical
message IDs are bound in the receipt. The source repair handoff was independently
checksum-verified as `#V#computer_file_copy_47a3732ac0294834ac602067ab67e644`, SHA256
`6a114a6798762d391cef8af2ff29508f62fa73f4b9b09f7f543970256a0e05da`.
Its earlier login/subscription preparation did not itself establish display.

Validation: 80 targeted backend notification/message/window-isolation tests
passed after incorporating current main. The final expired-confirmation change
passed all 29 notification backend cases; all 12 notification frontend cases,
affected static lint and diff checks passed. These cover permission dismissal
and denial, canonical coding reports with workflow launches suppressed, stale
subscriptions, expiry, duplicate/retry/crash bounds, actor/device/scope denial,
queued delivery after logout, and access-checked navigation. Synthetic unit
Push events are not counted as real display evidence.

The supported local delivery path now satisfies the bounded source merge gate.
Physical Android/iOS verification remains unavailable. The existing offline and
expired-session limitations above remain; no broader delivery guarantee is made.
Public activation still requires the controller to:

1. Deploy the exact merged `origin/main` revision through its authorised release
   route, keeping the prior release as code rollback.
2. Verify the protected VAPID/encryption configuration described above, the
   locked dependency environment and actual sender activation. Test keys exist
   only in the isolated fixture; their availability says nothing about public
   runtime configuration. Code rollback does not provision or rotate keys.
3. Read back the public served revision and HTTPS manifest/worker paths, then
   verify authenticated notification configuration and an installed-app delivery
   on the target device. Keep that evidence separate from localhost acceptance.
4. Reconcile the engineering issue through the coordinating operator: the Jira
   connector still returned `oauth_token_invalid_grant` during this continuation.

The coding run requests deployment through its structured result only. It does
not mutate the live service or send canonical task/completion messages; the
controller owns those effects. Shared service-worker/manifest changes in
JVNAUTOSCI-2758 still need to preserve this notification path.

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

- [Chromium push endpoint selection](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/components/push_messaging/push_messaging_utils.cc)
  and [endpoint constants](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/components/push_messaging/push_messaging_constants.cc)
  identify the newer Web Push and legacy staging routes checked during recovery.
