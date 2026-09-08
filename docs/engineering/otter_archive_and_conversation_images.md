# Private Otter archive and conversation images

Implementation and operating reference for [JVNAUTOSCI-2726](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2726).
Delivery evidence and current release decision belong in Jira. This page describes the bounded interface;
it does not establish that a particular deployment has been configured or activated.

## Private archive configuration

Provision the separate private OtterArchiveMCP checkout, its SQLite index, and the original backup
on the machine running Von. Keep that server on stdio. Set these variables in the runtime environment:

| Variable | Meaning |
| --- | --- |
| `VON_OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID` | Verified owning Von user; required before access |
| `VON_OTTER_ARCHIVE_RESOURCE_ID` | Deployment selector; defaults to `personal_otter_archive` |
| `VON_OTTER_ARCHIVE_MCP_PROJECT_DIR` | Private server checkout; required |
| `OTTER_ARCHIVE_DB` | Existing archive index; required |
| `VON_OTTER_ARCHIVE_MCP_COMMAND` | Optional executable override; otherwise the checkout's virtual environment entry point |
| `VON_OTTER_ARCHIVE_MCP_TIMEOUT_SEC` | Stdio process liveness bound (default 30 seconds); adjust from actual operation evidence |

The owner is resolved before launching the process. Ordinary turns receive a hidden trusted resource
binding; model arguments cannot choose a path, executable or owner. The subprocess receives only a
small operating environment and the database path, without provider credentials. Changing configuration
requires restarting the affected runtime. Removing the owner binding also denies subsequent reads of
staged private screenshots and derived crops. Existing LinkedIn and hosted Otter integrations remain
independent.

All ten upstream tools are available with the `otter_archive_` prefix. Orient broad questions with
status/overview, then search or retrieve bounded sources. `find_entities` frequencies describe source
records and observed labels, not reconciled meetings or verified identities. Unresolved export-group
associations stay unresolved. Ordinary reads do not start OCR, vision inference or corpus maintenance.

`get_artifact`/search results supply an authenticated `source_url`. That view displays original images,
source text, labelled OCR and cached inference, with extraction provenance available for inspection.
`get_screenshot` stages a checksum-verified original in Von's existing blob/file-copy store. Original
chunk retrieval returns bounded metadata and an authenticated download URL, keeping base64 out of model
text. Consumers reassembling originals must check the complete upstream hash.

## Reusable image handling

The existing drop, paste and picker routes feed one image pipeline. Up to eight still PNG, JPEG or WebP
images may accompany a message (8 MiB and 25 million pixels per image). Media is decoded rather than
trusted by filename. The composer shows progress, previews, removal and failures; unresolved preparation
blocks submission. Removing an unsent image removes its message association, not its durable source.

Original bytes live in the existing durable blob store. File-copy records retain detected media type,
dimensions, checksum and source provenance. New image records explicitly use `attributes.v1` as their
file metadata authority to avoid duplicate per-field text-relation writes and reads; existing file-copy
records retain their earlier metadata contract. The normal concept name and type remain represented.

Messages and history carry small attachment descriptors. Only the provider boundary hydrates access-
checked original bytes into native image inputs, with source IDs, dimensions and citation URLs alongside
the images. Retained continuations and ordinary telemetry contain descriptors rather than base64.
OpenAI Chat Completions/Responses and Ollama have image transport; the Gemini structured transport
currently reports unsupported image input explicitly. A transport does not establish that every model
on that provider supports vision; use a vision-capable configured model and inspect failures.

`crop_conversation_image` creates a labelled crop/enlargement for small details. It preserves parent
checksum, pixel rectangle, scale and source audience. Resizing cannot reconstruct destroyed information.
Originals remain independently inspectable. Conversation image reads require the owning actor; an Otter
`shared` label or organisation membership does not grant archive access.

## Validation and limits

The CC0 fixtures in `tests/fixtures/research_images` and their generator/reference file cover columns,
symbols, an exact recurrence, a logarithmic chart, table/code structure, directed feedback and degradation.
Reference criteria precede tuning. Tests cover authority before proxy launch, hidden bindings, original
checksums, SDK image bytes and references, crop provenance, history projection and composer failures.
Use the browser protocol for live upload/reload/follow-up and source-view acceptance.

Private live receipts stay outside Git. Report the selected model, input/preprocessing, source hashes,
actual crop/cache use, latency and any failed attempts. Sparse OCR or model enrichment is not evidence
that a topic is absent. Cached model interpretations can be wrong; scientific claims must remain tied
to inspectable original evidence. This integration does not reconcile the whole archive, prove
corpus-wide OCR accuracy, cancel Otter, deploy a network archive service or grant wider access.

## Original presentation decks

`read_presentation_slides` accepts an actor-visible PPTX or PDF file-copy concept
and returns up to four numbered slide images per call, native text, speaker notes
(where present), source hashes and a next offset. Images use the existing native
vision transport and private image viewer; original source bytes remain unchanged.
The reader recognises Office package structure even when legacy import metadata
says `.bin`. `read_file_copy` also uses bounded Office extraction for this case,
with detected type reported separately from stored metadata.

Install LibreOffice (`soffice` on PATH) for PPTX rendering. The converter uses an
isolated temporary profile and a 90-second process bound. Derived PDFs are cached
by source hash and renderer version. Optional `include_ocr` caches Tesseract output
by image hash and engine version. Every call checks source access before reading
derived caches. The service rejects a PPTX/rendered-PDF slide-count mismatch.
Renderings can differ from PowerPoint in font/layout details; native text and
rendered evidence remain separately attributed. Audio, animation and video playback
are not reconstructed.

Use the returned native images for diagram interpretation, retaining slide numbers,
source hashes, model and prompt versions with any persisted interpretation. A cached
render or OCR result is not a verified semantic interpretation. Reuse represented
workflow authoring for analysis/persistence/recovery rather than treating a successful
file import, graph creation or schedule as proof of slide understanding.
