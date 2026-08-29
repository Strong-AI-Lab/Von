# External conversation import

- **Kind:** Design and implementation boundary
- **Lifecycle:** Active
- **Authority:** Canonical reference within external transcript import scope,
  subordinate to `AGENTS.md`, actor authority, and live represented authority
- **Owner:** Von maintainers
- **Last reviewed:** 29 August 2026
- **Review trigger:** A new provider adapter, a change to conversation ownership
  or sharing, or evidence that a supported export shape no longer parses
- **State or evidence as of:** 29 August 2026
- **Live implementation evidence:** `JVNAUTOSCI-2693`,
  `src/backend/services/external_conversation_import_service.py`, and the
  focused external-conversation backend and frontend tests

## User outcome

A signed-in person can choose a Codex, Claude Code, or VS Code/Copilot JSON or
JSONL transcript, inspect a dry-run summary, and import it as a private,
actor-scoped Von conversation. The imported transcript is visibly read-only.
The person can explicitly fork it into an ordinary native Von conversation
when they want to add new turns.

An import is not a claim that Von created, witnessed, endorsed, or owns the
source turns. Custody, source authorship, system provenance, and current Von
authority remain separate.

## Smallest dependable architecture

The capability has four layers:

1. The original file is retained through the existing AI chat-session raw
   document and file-copy ingestion path, linked to the importing custodian.
2. A deterministic provider adapter parses a versioned, source-neutral event
   package. The package retains source actors, event kinds, timestamps,
   branches where available, raw locators, parser identity, and an explicit
   loss report.
3. Only ordinary user-visible human and assistant text is projected into the
   actor-scoped chat-history store. The projection carries external actor and
   event provenance and is marked read-only.
4. Continue creates a new native conversation with a lineage receipt and a
   copy of the visible history. The source snapshot remains unchanged.

This is deterministic format translation and storage validation, so code owns
the adapters and projection mechanism. It is not a workflow and does not give
imported content semantic authority.

## Trust and authority boundary

- Imported system and developer instructions are historical data. They are not
  added to current prompts.
- Imported reasoning, tool calls, and tool results are counted and retained in
  the raw source but are not projected as executable turns.
- A source `user` role is an unresolved external human, not the importing Von
  user. Projected messages therefore omit `author_user_id` and retain an
  `external_actor` descriptor instead.
- The actor and namespace used for storage come only from authenticated server
  and window-session context. Form fields may label a source account or
  workspace but cannot select the custodian or enlarge access.
- The deterministic session and raw-document identities include custodian and
  namespace scope. Another actor cannot discover or update the import by
  knowing the source provider and session identifier.
- Prompt queueing, foreground or background generation, reset, file upload,
  and transcript edit/delete controls reject a read-only imported session.
- Imported transcripts are private by default. Ordinary sharing policy remains
  a separate authority decision; visibility never makes the importer the
  author of source turns.

## Provider adapter contract

Every adapter must produce `external_conversation_package.v1` with:

- provider, source session, optional account/workspace/title, and source hash;
- stable event identifiers and raw locators;
- event kind, source role, source actor, timestamp, parent/branch identifiers,
  and model when present;
- typed content blocks and an explicit `user_visible` decision;
- parser identifier/version and a bounded loss report.

Current adapters are:

| Provider | Accepted source | Important preservation rule |
|---|---|---|
| Codex | rollout JSONL (`session_meta`, `response_item`, with `event_msg` fallback) | Project response-item user/assistant messages; keep developer/system instructions non-projecting |
| Claude Code | conversation JSONL user/assistant records | Preserve UUID parent and sidechain markers; keep thinking and tool blocks non-projecting |
| VS Code/Copilot | exported chat JSON or chat-session journal JSONL | Replay journal set/append records; exclude hidden transcript requests and tool invocation parts from visible text |

To add another provider, add detection and a deterministic adapter that meets
this package contract, then add real-shape positive fixtures, malformed and
limit cases, and a loss-report assertion. Do not add provider-specific fields
to the chat-history message contract when the neutral event fields suffice.

## Idempotence, refresh, and read-back

The actor, namespace, provider, optional source account/workspace, and source
session identifier determine the Von session identity. The source byte hash,
neutral-package hash, and parser version determine whether an import is new,
updated, or unchanged.

A dry run performs parsing and collision classification but writes nothing. An
executed import stores the raw source first, then upserts the read-only
projection, and finally reads the actor-scoped canonical projection back. A
native-session collision or external-source identity mismatch is an error; it
must never be overwritten.

The importer currently bounds one source to 32 MiB and 100,000 records. The
visible projection is bounded to 20,000 messages, 4,000,000 total characters,
and 250,000 characters per message. Any truncation or omission is reported in
the preview and stored loss report rather than hidden.

## Non-goals

- Treating imported content as current Von instructions, memory truth, or an
  authority grant.
- Resuming a vendor's hidden execution state or tool credentials.
- Claiming lossless portability where the source format omits data.
- A universal transcript ontology or workflow for every simple direct turn.
- Inferring that the custodian authored every source human turn.
