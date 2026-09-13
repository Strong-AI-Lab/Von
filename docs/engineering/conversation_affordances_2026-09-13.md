# Conversation affordance reconciliation — 13 September 2026

**Kind:** implementation and acceptance record
**Lifecycle:** dated evidence
**Authority:** evidence only; task remains the delivery authority
**Task:** #V#task_agent_63185986b5f7204bf1c718922c4a54f4

## Diagnosis and decisions

The Messages rectangle was created only after a refresh while scrolled away,
inside the transcript. The chat arrow was independent of arrivals. They both
navigated to the end, despite the rectangle claiming “New messages”. Chat also
had a separate new-arrival boundary, which moved forward on successive arrivals.

| Affordance | Disposition and rationale |
| --- | --- |
| Latest navigation | Reconciled: shared native circular down-arrow button, “Scroll to latest message”, 44 × 44 pixels at every width, keyboard activation, visible focus and pressed states. Visible when away from the end, regardless of new arrivals. Each adapter scrolls its own surface and transfers focus to the destination. |
| Placement | Reconciled: latest navigation occupies a composer row rather than overlaying transcript text or being clipped outside the mobile composer. Messages places its unread action beside the arrow. |
| New/unread navigation | Semantically distinct from latest navigation. Messages offers “Jump to first unread message” for loaded canonical unread contributions; opening starts at the first loaded unread. Chat offers “Jump to first new message” for arrivals since scrolling away; subsequent arrivals preserve the first boundary. The different names deliberately distinguish canonical unread state from an ephemeral new-arrival boundary. |
| Read acknowledgement | Retained canonical mechanisms: Messages acknowledges visible recipient-addressed contribution IDs with receipt checking; chat uses a server-validated observed last-incoming turn/timestamp cursor. Navigation itself never calls either read endpoint. Messages detaches observers during render/position restoration and excludes the portion hidden behind the fixed composer. Earlier pages remain unread until displayed. Chat's cursor is a conversation-level read model, not a per-contribution receipt; unifying storage semantics would be a separate backend change. |
| Scrolling, refresh and paging | Retained document scrolling for chat and an internal pane for Messages, matching their existing layouts. Messages explicit and background refresh now both preserve position when away from the bottom; earlier-page loading preserves the viewport. Reconnect refresh retains loaded earlier pages and exposes the existing earlier-page cursor for gaps. |
| Unread badges/filter | Retained common conversation catalogue's canonical counts and Unread filter. Messages retains visible Unread labels and outlines, including older pages; failed receipts retain labels and Retry. Chat retains new-arrival separator and its server cursor badge. Loading a conversation is not a bulk message-read operation. |
| Composer | Retained shared compact draft sizing and Enter/Shift+Enter/IME handling. Desktop Enter sends; narrow Enter inserts a newline. Both retain explicit send and draft recovery. Chat's speech, attachments, model/steering and More actions are supported chat capabilities; Messages keeps recipient/context display, per-exchange drafts and group-reply limitation. Adding unsupported speech or dropping group recipients would be incorrect. |
| Alignment and sender identity | Already equivalent: own contributions right, others/Von left; sender names and avatars remain visible. Messages uses participant profiles; chat preserves author identity and assistant role. No alignment rewrite needed. |
| Widths/content | Retained readable proportional widths rather than identical fixed widths: Messages caps bubbles at 760px within a 900px list, with 94% on narrow screens; chat accommodates structured assistant output and its existing controls. Both wrap ordinary text and contain long/code content. |
| Marking and per-message actions | Retained chat's turn-specific editing/annotation/runtime controls; Messages preserves Copy Markdown, Discuss with Von, concept references, and recommendation review where applicable. These act on different canonical objects; showing unsupported chat actions on message concepts would misrepresent capability. |
| Header/tools | Retained recipient profiles and Refresh in Messages; chat's session, model and runtime tools remain chat-specific. Both expose Tasks and context/reference access through existing paths. |

No new prompt, workflow, authority or backend schema is introduced. Shared code
owns presentation and native button semantics only. Existing APIs own reads and
writes. This is Tier 1 client behaviour acceptance with canonical in-memory
fixture read-back, not a new authority boundary or a claim of backend redesign.

## Validation and limits

- Targeted Jest coverage includes both render paths, shared button semantics,
  first-new-boundary retention, scroll/pagination restoration, destination focus,
  no navigation-triggered acknowledgement, failed and partial read receipts,
  stale observers, hidden documents, exchange switching and draft isolation.
- `tests/browser/conversationReadability.cjs` serves the actual templates,
  styles and modules with isolated in-memory APIs. At 1440px and 360px it checks
  opening without clearing unseen/unloaded contributions, failed-read retry,
  incoming content while scrolled away, latest activation by Enter and Space,
  44px hit targets, keyboard focus, pagination, refresh/reopening, badge read-back,
  participant alignment, sender labels and no horizontal overflow. It records
  screenshots and measurements in `/tmp/conversation-affordance-evidence`.
- The browser fixture is deliberately unauthenticated and never forwards APIs
  to a running Von service. Operator-retained authenticated profiles were bound
  to other tasks; they were not repurposed or claimed as candidate evidence.
- The frontend is served as source ES modules, with no npm build script.
  Browser module loading, targeted static lint and a versioned `git archive`
  release package provide the applicable build evidence.
- No model calls, live message mutations, deployment, migrations or infrastructure
  changes are requested or performed. Public deployment is not instructed in
  the task title/description, so the worker's deployment request remains empty.

## Supplied screenshot provenance

- #V#computer_file_copy_d278592f3ce44fb9a7ce07505fccd1a4 — image.png,
  556×198, SHA-256
  `1929efb9f4cacf1975dbc4f37f5dfb3f92fc874777421fcf788bb086bed14336`.
- #V#computer_file_copy_8644a620dd2c4e83a7de1bb6d70c4453 — image.png,
  203×177, SHA-256
  `f410329d24b5122b447945a253774a882f43f38f10a39ac88a78c33ec47d525e`.

The task's descriptions of these crops informed diagnosis; their image bytes
were not available in the assigned context and are not claimed as inspected.
