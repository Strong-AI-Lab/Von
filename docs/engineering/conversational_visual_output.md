# Conversational visual output

Implementation guide for retained visual replies. Owner: Von engineering.
Review when provider output contracts, client renderers, image limits or the
existing prompt/profile mechanisms change. Delivery evidence and unresolved
acceptance belong to the native task
`#V#task_agent_0a2baa3bd7a33ac6838d39f6179e05c7` and its pull request.

## Source, transport and display

The existing assistant message remains the canonical source for fenced Mermaid
and mathematics. The client protects `\(inline\)` and `\[display\]` before
Markdown parsing, then renders with KaTeX. Dollar signs, escaped delimiters and
code examples remain text. Mermaid fences use strict rendering and sanitised
SVG. Both forms retain exact source and copy controls. A syntax/renderer failure
leaves a local source fallback. A newly completed message replaces provisional
markup, so an incomplete earlier expression does not leave a permanent error.

Foreground completion, queue hydration and restored messages use the existing
chat rendering entry point. Rendering is idempotent for retained element IDs;
asynchronous work cannot replace a newer message generation. Existing source
links, tables, charts and screen/spoken channels continue through their usual
paths. There is no narration or semantic renderer-selection call for images.

`LLMResponse.content_parts` contains ordered, provider-neutral `LLMContentPart`
values: `text`, decoded `image`, or `media_error`. Image bytes exist transiently
at the transport/storage boundary and are excluded from the part's repr. Only
recognised visible provider forms enter this list; reasoning, arbitrary provider
HTML and function-call arguments do not. A completed image-only result is usable
even with an empty text projection.

Retention replaces bytes with version 1 parts containing `part_id`, `kind`, and
either `text` or an actor-scoped `asset`. These are canonical assistant history
fields, with `image_attachments` supplying existing provider-input hydration.
History reconstruction builds the display contract without needing private LLM
debug data. `turn_display_elements_v1` is the sole display contract: retained
parts project to ordered `text_block` and `image` elements with stable IDs.
Renderer profiles gain the `image` family and retained-image object kind.
Unsupported kinds/versions become identifiable text fallbacks.

The plain Responses adapter keeps its string interface through
`VisibleTextProjection`: ordinary text where available, otherwise an image/error
caption. Rich consumers may read its `content_parts`, also present in response
metadata. This compatibility path does not independently enable image generation.

## Enablement and adaptable guidance

Generation, image understanding and rendering are separate capabilities. The
native image tool is added only for an OpenAI Responses decision with an exact
selected represented API profile. On that profile, the existing
`#V#has_model_capabilities_json` text predicate may contain:

```json
{
  "image_generation": {
    "enabled": true,
    "model": "<authorised image model supported by this provider route>",
    "quality": "auto",
    "size": "auto",
    "output_format": "png"
  }
}
```

Merge this field with the existing JSON object; preserve its other capabilities.
The API profile must already select a compatible conversational model and
Responses surface. Configure names from current provider documentation and
account entitlement; no model-name heuristic enables this feature. Read back the
resolved profile and observed provider request. If disabled or absent, no native
image tool is added. Ordinary function definitions stay separate. Unsupported
tool options are not forwarded as arbitrary SDK arguments.

The first configuration accepts `auto`, `1024x1024`, `1024x1536` and
`1536x1024` sizes, `auto/low/medium/high` quality and PNG/JPEG/WebP output. These
are the implemented option subset, not a claim about every current provider
size. Output is decoded against actual bytes and existing storage bounds: still
PNG/JPEG/WebP, at most 8 MiB and 25 million pixels. This covers the configured
common sizes; unusually large lossless output receives an explicit media error
instead of bypassing storage validation. Review limits using observed output
before enabling larger formats.

The adaptive call includes compact implemented client affordances (format IDs,
versions, source conventions and fallback support). Tool availability remains
the authority for generation. Executable renderers stay in reviewed code;
Vontology records cannot download client implementations.

The [visual guidance seed](../../src/backend/workflows/repo_seed_bundles/conversation_visual_guidance_seed.md)
is a **release input**, not a second production authority. Use canonical concept
and text-relation tools to create/update the named behaviour prompt and bind it
to the intended actor via `#V#specific_to_von_user`. The existing
`get_user_specific_prompt_fragments` path consumes it along with established
preferences. No additional fetch or model stage is introduced. Read back its
body, type and binding, then confirm its concept ID and hash in the ordinary
turn's applied prompt snapshot. Revise the live prompt independently. Do not
activate the seed, change credentials or enable paid generation in production
as a side effect of importing this code.

## Images, continuity and recovery

Completed OpenAI `image_generation_call` output and MCP `ImageContent`/embedded
image blob resources use `conversation_output_service` and the existing private
conversation image store. A tool resource link remains a reference; there is no
new arbitrary URL fetcher. MCP bytes are removed before adaptive effect
journalling and evidence retention. Provider raw traces omit image bytes.

Original bytes go to the existing durable blob store and file-copy registration.
Checksums, dimensions and provenance accompany the asset; private revised
prompts and provider response/item identifiers are not automatic captions.
Display descriptors omit private prompts and storage locations. The existing
authenticated original route rechecks owner access and checksum. A shared
conversation does not grant another participant access to its owner's private
image. That participant sees an unavailable image notice; this change creates
no new sharing policy.

Follow-ups hydrate original pixels from retained descriptors at the provider
boundary, including after reopening history or changing compatible providers.
They do not depend on indefinite provider response retention. Each derived image
has its own asset identity and immutable original reference. Provenance records
all supplied `input_concept_ids`; a single source becomes `parent_concept_ids`
when the provider explicitly reports an edit. Multiple inputs alone do not prove
which was the edited parent. The model resolves the requested referent from
conversation context and the source labels supplied with the actual pixels.

Storage uses the existing owner/checksum/provenance deduplication. Repeated
completion events do not render a second element. Generation completion and
storage completion remain distinct: incomplete/invalid output or storage failure
is a `media_error` beside any useful text, and storage errors retain a bounded
provider/tool recovery locator. A successful image remains visible beside a
later canonical effect failure report; stale model claims cannot override that
report. The existing progress lifecycle reports model work and media retention.
Model-call thresholds remain advisory under the existing adaptive lifecycle.

Native image calls disable SDK retries. A connection interruption or server
error on that route reports an unknown outcome and does not trigger the adaptive
transport recovery call. This avoids duplicate paid work; it is not proof that
an interrupted request generated nothing. Reconcile the provider receipt before
any explicit retry. If the durable store itself is unavailable, the error
locator does not promise that unavailable bytes can be recovered. A UI reload
uses retained assets and never generates an image.

## Validation and extending the seam

Run the targeted Python provider/content/history/display tests and the frontend
Markdown tests. `tests/browser/conversation_visuals_fixture.py` serves the real
chat renderer and Markdown service with disposable generated fixture pixels;
`tests/browser/conversationVisuals.cjs` checks desktop/mobile/dark presentation,
copy/source, order, reload and duplicate events. It is explicitly a
credential-free fixture, not authenticated provider acceptance. Follow the
[browser guide](frontend_browser_user_view_validation.md) and DGX host README
for candidate-bound authenticated acceptance. The full capability additionally
requires one ordinary real generation and one bounded edit using an authorised
non-Sol route, canonical asset read-back and an owner/wrong-actor check. Record
actual model, asset IDs, usage/latency and candidate revision.

Libraries are self-hosted. `npm ci` and `npm run build:conversation-visuals`
rebuild pinned Mermaid, KaTeX and DOMPurify assets from `package-lock.json`.
Commit the generated assets because the existing Python server serves them
directly. Updating the dependencies requires rebuilding and rechecking source
safety and browser presentation.

For example, a future static spatial view can add a versioned `map` part with
typed coordinates and source references, validate it in the existing display
service, register its renderer family/profile, advertise the implemented client
capability and add its reviewed client renderer. Unsupported clients retain an
identifiable source/reference fallback. A future provider maps outputs into the
same neutral parts and retention path without changing turn orchestration.
Interactive implementations need isolated code and explicit action interfaces;
a MIME label is never execution authority.

Stable artefact/element references can later connect interactive plots and
equations, spoken explanations with referent highlighting, animation of change,
maps and spatial views, or persistent shared canvases across screens. Capability
discovery lets the model choose what the current client can actually present.
Sonification or haptics are possible where a concrete user and device job makes
them useful. These are extension directions, not implemented features or release
gates for this change.

Provider references: [OpenAI image generation](https://developers.openai.com/api/docs/guides/image-generation),
[Mermaid usage](https://mermaid.js.org/config/usage.html),
[KaTeX options](https://katex.org/docs/options.html).
