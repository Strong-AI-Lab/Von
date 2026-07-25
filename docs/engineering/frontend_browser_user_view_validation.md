# Frontend Browser User-View Validation Guide

- **Kind:** Browser-acceptance protocol and practical guidance
- **Lifecycle:** Active
- **Authority:** Canonical browser-validation guidance routed by `AGENTS.md`;
  apply it at the validation tier justified by the claim
- **Created:** 2026-04-06
- **Last substantive content update before this metadata review:** 2026-06-08
- **Last reviewed:** 2026-07-25
- **Evidence boundary:** Each acceptance claim still requires its own dated
  user-view evidence

## 1. Purpose

This document explains how to do reliable browser-level validation of Von's
user-facing UI, especially when the important behaviour depends on authenticated
state, realistic saved data, or rich user-scoped surfaces such as:

- chat and conversation history
- Messages
- invites and shared-conversation affordances
- user-scoped settings
- other panes whose meaningful state only appears after login

It exists because anonymous UI state is often too shallow for real acceptance
work. For frontend tasks, browser checks against a realistic authenticated
user-view can reveal layout, overflow, visibility, and state-propagation issues
 that code inspection and unit tests will not catch.

## 2. When to Read It

Read this guide before planning or implementing work that involves:

- frontend layout or responsiveness
- user-visible acceptance that should be checked in a real browser
- chat, Messages, conversation history, invites, or other authenticated UI
  surfaces
- browser automation or Chrome DevTools validation
- test-fixture or pseudouser strategy for UI validation

## 3. Core Principle

For many Von UI tasks, the best acceptance evidence is not:

- a nearby unit test alone;
- a mocked DOM tree alone; or
- an anonymous browser session alone.

For a user-visible layout or interaction claim, the best evidence is usually:

1. targeted automated tests for the changed code path;
2. a live browser check on the actual UI surface; and
3. where relevant, an authenticated user-view with realistic data.

If the bug report came from a specific pane, treat that pane as the primary
acceptance path even if another nearby surface appears to share the same CSS or
rendering logic.

## 4. Why Authenticated User-View State Matters

Anonymous mode is often not representative enough because it may hide or remove:

- saved conversations;
- Messages threads and real message bubbles;
- per-user badges, counts, and stateful controls;
- invite flows;
- user-specific history and metadata;
- richer content that exposes wrapping or clipping bugs.

Ad-hoc mocking inside DevTools is still useful for diagnosis, but it is not a
complete substitute for a dependable authenticated browser-testing path.

## 5. Validation menu

Choose the cheapest evidence sufficient for the concrete browser claim. These
options are not an ordered ladder and Tier 1 does not require all of them:

- targeted tests for the changed module;
- the relevant static check;
- a live check on the affected browser surface;
- authenticated or realistic fixture state when anonymous state cannot expose
  the claim; and
- targeted in-browser instrumentation for diagnosis.

A good final acceptance note usually states:

- what automated checks ran;
- which exact browser surface was tested;
- whether the check was anonymous, authenticated, or fixture-backed; and
- what concrete evidence was observed.

## 6. Authenticated Pseudouser Strategy

Von should support a dependable pseudouser-based browser-testing path for rich
frontend validation.

Current practical direction:

- use a dedicated Strong AI Lab pseudouser rather than a real human user's
  account for routine acceptance checks;
- keep credentials and recovery details out of the repository;
- ensure the authenticated state has representative saved data;
- make the setup repeatable enough that coding agents and developers can use it
  routinely rather than only as a one-off manual exercise.

Important:

- do not commit credentials;
- do not print secrets in logs, docs, or task notes;
- document the workflow, not the secret material;
- keep clear boundaries between test-fixture behaviour and production
  behaviour.

### 6.1 Implemented local browser-test mode

`JVNAUTOSCI-1747` now provides a concrete local-development path for this:

- enable `VON_BROWSER_TEST_AUTH_ENABLED=1` in your local environment;
- optionally set:
  - `VON_BROWSER_TEST_PSEUDOUSER_NAME`
  - `VON_BROWSER_TEST_PSEUDOUSER_EMAIL`
  - `VON_BROWSER_TEST_PSEUDOUSER_CONCEPT_ID`
  - `VON_BROWSER_TEST_ORGANISATION_CONCEPT_ID`
- restart Von so the launcher picks the values up from `.env`;
- open the Settings tab in a browser served directly from `localhost` or
  `127.0.0.1`;
- confirm the Settings authentication area reports the browser-test mode
  status, target pseudouser identity, and any unavailable reason before trying
  to log in;
- use the `Browser Test Login` control in the authentication area.

This route is intentionally:

- disabled by default;
- localhost-only;
- intended for local development and browser acceptance work, not for
  production access.

When invoked successfully it establishes a real Flask session for the configured
pseudouser and seeds representative user-view fixture data, currently including:

- saved organisation-scoped chat sessions;
- representative Messages threads;
- unread incoming messages;
- long content intended to exercise wrapping, clipping, and dense header/meta
  layouts.

The current default pseudouser for this local mode is the Strong AI Lab
pseudouser:

- `Zhan von Witbrock <zhanvonwitbrock@gmail.com>`

Repeated use is designed to be stable rather than destructive:

- fixture users and memberships are reused rather than recreated blindly;
- saved chat sessions are deterministic and only missing fixture turns are
  added;
- seeded Messages are reused by fixture key rather than duplicated on every
  refresh;
- unread fixture messages are reset to unread so the Messages pane remains
  useful for acceptance checks.

If the `Browser Test Login` button is missing, do not assume the feature is
absent. Check the status line in the Settings authentication area first. The
running app should now report whether browser-test auth is:

- available;
- disabled because `VON_BROWSER_TEST_AUTH_ENABLED` is off; or
- configured but unavailable because the current browser/request is not a local
  `localhost` / `127.0.0.1` session.

## 7. What a Good User-View Fixture Should Cover

A strong authenticated browser-validation fixture should make it easy to inspect
at least the following:

- one or more saved chat conversations with realistic long-form assistant and
  user content;
- Messages threads containing long text and inline concept cartouches;
- counts, badges, and stateful controls that only appear after login;
- at least one narrow/mobile check and one desktop check;
- enough persistent state that regressions are visible without manual data entry
  every time.

For layout tasks, include deliberately awkward content:

- long titles;
- long paragraphs;
- long inline concept IDs or cartouches;
- mixed controls and metadata in headers;
- message lists with both sent and received content.

## 8. Browser Testing Tactics

When using Chrome DevTools or equivalent browser tooling:

- measure actual rendered widths when width/overflow is in question;
- check the exact pane that the bug report refers to;
- inspect both desktop and narrow/mobile-sized viewports when wrapping matters;
- capture at least one screenshot or concrete measurement when the visual change
  is the acceptance target.

Helpful evidence includes:

- container widths;
- message bubble widths;
- explicit overflow measurements;
- screenshots of the relevant pane;
- confirmation that the issue is absent on both desktop and narrow layouts when
  required by the task.

### 8.1 Playwright from Codex on macOS

When Codex needs scripted browser evidence against a local Von server on macOS,
prefer the repo's isolated AgentTest backend:

- start it with `./run.sh restart -AgentTest -HealthTimeoutSec 180`;
- validate against `http://127.0.0.1:5010`;
- stop it afterwards with `./run.sh stop -AgentTest -NoBrowser`.

In VS Code / VS Code Insiders, do not assume the Codex Browser or Chrome plugin
surfaces are usable just because the plugin bundles are installed. If
`agent.browsers.list()` is empty, or `agent.browsers.get("iab")` /
`agent.browsers.get("extension")` reports that the browser is unavailable after
one lightweight retry, stop trying to recover the Codex plugin bridge inside the
coding session. Treat that as a host-surface limitation and use the
AgentTest-plus-Playwright path for local browser evidence. The Codex App may
expose an in-app browser pane and plugin UI that VS Code Insiders does not.

If the workspace has `playwright` / `@playwright/test` installed but Chromium
still fails to launch from Codex, check whether the failure is sandbox-related
before treating it as a missing dependency. A typical sandbox failure includes:

`MachPortRendezvousServer... Permission denied`

If the current tool host permits an approved unsandboxed or browser-enabled
execution path, use it. If it does not, record the host limitation and use the
nearest available faithful surface; do not repeatedly retry an unavailable
permission mode. A quick smoke check, where permitted, is:

`node -e "import('playwright').then(async ({ chromium }) => { const b = await chromium.launch({ headless: true }); const p = await b.newPage(); await p.goto('http://127.0.0.1:5010/health'); console.log(await p.textContent('body')); await b.close(); })"`

The acceptance note should state whether browser evidence came from Playwright,
the target URL, and any host limitation that materially reduced fidelity.

## 9. When Mocking Is Still Appropriate

In-browser mocking or synthetic DOM setup is appropriate when:

- the live system lacks the necessary fixture data;
- you need to isolate a rendering path quickly;
- you are diagnosing whether the bug is CSS/layout versus data-loading/state.

But if you stop there, say so explicitly. Mock-backed browser diagnosis is not
the same as authenticated user-view acceptance.

## 10. Durable validation lessons

Record or create follow-up work only when a lesson has demonstrated recurring
material cost and cannot be fixed or noted simply in the current task. Then:

- update the smallest relevant guide rather than appending a new universal
  rule;
- create Jira follow-up only for a concrete unmet capability; and
- state the actual evidence limitation when it materially affects the claim.

## 11. Current Follow-Up

The first practical authenticated pseudouser path now exists via
`JVNAUTOSCI-1747`, but the broader testing strategy should still evolve toward:

- richer fixture coverage for invites and shared-conversation surfaces;
- stronger browser-level regression scripts for common acceptance paths;
- convenient fixture refresh/reseed pathways that remain explicitly local-only
  and safe by default.
