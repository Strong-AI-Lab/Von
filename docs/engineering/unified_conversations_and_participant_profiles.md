# Unified conversations and participant profiles

- **Kind:** Capability and implementation boundary
- **Lifecycle:** Active
- **Owner:** Von conversation interface
- **Review trigger:** Changes to participant identity, conversation transport or avatar audience semantics
- **Native task:** `#V#task_agent_3f9f792044179b8f9c90ebeb121f7784`

## User outcome and authority

Conversations provides one activity-ordered list of ordinary Von conversations and
person/agent message exchanges. Selecting a row opens its appropriate reader and
composer. Persisting a visible contribution promotes the same row; viewing,
renaming, retrying a duplicate append, tool activity and progress heartbeats do not.
Pins remain a separate user preference. Incoming activity does not change selection
or discard the draft in another conversation.

This is a shared discovery and presentation layer over the existing canonical
stores. Ordinary transcripts remain in `chat_history`; direct messages remain
Vontology message concepts. A message exchange is identified by the exact set of
participants and its organisation, not by an agent's name. Group exchanges are
readable but the existing pair-only sending capability is not presented as a group
reply. Imported actors and transcript provenance remain intact. The old Messages
tab reference redirects to Conversations; message references retain their source
and exact retrieval locator.

The simplest adequate baseline is the existing readers and canonical APIs with a
common metadata projection. No transcript migration, new workflow, universal
conversation schema or model call is required for list maintenance. Generation is
an explicit image request, where model judgement is useful.

## Catalogue and read behaviour

`GET /api/messages/conversation-catalogue` merges bounded server-side keyset pages
from both stores. Its opaque cursor is bound to actor, namespace, organisation and
mailbox facet. Each source applies the common time/key boundary before limiting;
an unconsumed candidate is fetched on the next page. A source failure is reported
and does not advance the common cursor past the unavailable source.

Ordinary conversations use persisted contribution metadata. Legacy dates are
materialised from the latest visible transcript entry by conditional, batched
derived updates without changing the transcript or its modification time. Message
exchange dates come from canonical message creation times. The foreground browser
polls on a four-second cadence without accumulating overlapping requests; exact
latency also depends on the service and network.

The default catalogue uses the window's current context. **All organisations**
adds the viewer's message exchanges across their verified memberships; this does
not broaden ordinary conversation access. The reply destination explicitly shows
the original organisation even when it differs from the current window.

Unread acknowledgements require visible contributions in an active document.
Ordinary conversations use per-viewer monotonic read cursors with a quiet initial
baseline; precise timestamps and stable turn IDs prevent a rounded HTTP date from
losing the read acknowledgement. Existing shared-conversation invitation receipts
remain authoritative. Direct messages retain canonical `read_by` state. Loading a
background source alone does not mark it read.

The title/participant and Unread filters apply to loaded catalogue rows. **Load
older conversations** extends the bounded list. When further pages exist the badge
is an explicit lower bound (`N+`), not a claimed total over unexamined history.
Existing loaded pins remain visible across refreshes; discovering a pin beyond the
initial history window still requires loading older conversations. These bounded
list limitations are not a replacement for global conversation/content search.

## Reusable identity and avatar capability

The same profile editor is available from Settings, a participant's concept view,
a conversation header and contribution portraits. It resolves display names from
the existing concept identity, offers concept details, and supports upload,
private generation preview, explicit publication and removal. Other participants'
profiles are readable where visible; editing requires the authenticated subject's
own identity. Legacy identity headers cannot authorise profile changes.

`participant_profile` provides agent parity through the internal MCP catalogue:
`get`, `generate`, `set_avatar` and `remove_avatar`. The HTTP entry points are
`/von/api/participants/profile`, `/profiles`, `/avatar` and `/avatar/generate`.
The image generation provider is requested once with retries disabled; a private
candidate is not automatically published. Provider image charges are described in
the editor, and are not claimed as part of an ordinary conversation's model cost.

Avatar variants use Von's existing scope modes:

| Scope | Mode |
|---|---|
| User | `user_only_default` |
| Organisation | `organisation_general` |
| User and organisation | `user_org_default` |
| Global | `global_general` |

Canonical file-copy visibility enforces the audience. Matching combined, user,
organisation and global variants take precedence in that order; a variant from a
different organisation is not selected as the current organisation's portrait.
Publication creates a 256-pixel PNG derivative with source metadata removed.
The editor accepts a file picker or one dropped PNG, JPEG or WebP (up to 8 MiB),
preserves the source through the private image store, and offers zoom and position
controls. `/avatar/prepare` applies EXIF orientation before suggesting framing.
The bundled OpenCV frontal-face detector runs locally: one detected face gets a
suggested crop, while zero or multiple faces retain a centred crop for manual
correction. Detector failure also preserves manual upload; locations do not
identify people or enrol biometric identities. Detection can miss faces and is
only an adjustable default.

`/avatar` accepts an optional square `crop` (`x`, `y`, `size`) in oriented source
pixels. `/avatar/generate` accepts a private `source_image_concept_id`; after
checking its owner, it sends oriented, metadata-free photo pixels to the existing
configured OpenAI client using image edits with `gpt-image-1.5`. The style is the
user's prompt. Without a source it retains text-only generation. These are
distinct provider operations; image-edit failure does not fall back to text-only
generation. Generated candidates record their private source provenance and
require explicit **Use avatar**. **Return to source photo** restores manual
framing, including after provider failure. Existing MCP calls retain their
previous text-generation and centred-publication interface.

The original upload/generated candidate stays private. Replacing or
removing a variant retires that publication, allowing another applicable variant
to become visible. Image reads validate the viewer and window context, use private
no-store caching, and never expose the private source blob URL in the public
profile projection.

The generally reusable support added here is participant presentation, explicit
scope-aware avatar publication, common contribution rendering and copy actions,
exact exchange locators, activity metadata, and durable per-viewer read state.
These capabilities preserve source-specific affordances, including Thinking,
model controls, recommendations, tasks and original reply recipients where they
actually apply.

## Acceptance and delivery boundary

The release claim is bounded behaviour (Tier 1), with canonical read-back and
negative authority/scope cases for avatar publication and exchange retrieval
(Tier 2). Required evidence includes interleaved/equal-time pagination, unread
precision, draft and selection preservation, exact group/organisation identity,
profile permission denial, publication read-back, and authenticated desktop and
narrow-viewport checks. The native task holds dated delivery evidence and the
current merge decision. Repository publication does not activate a running server.
