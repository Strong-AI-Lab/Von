# Blob Store Artefact Source Strategy

## Purpose

Define a single approach for onboarding artefacts into Von's durable blob layer and
for indexing/retrieval workflows that consume those artefacts.

This document covers `JVNAUTOSCI-883` follow-on scope from `JVNAUTOSCI-879`.

## Decisions

1. Blob store is authoritative for durable artefact bytes.
2. A canonical artefact record is carried as `artifact_record` metadata (concept ID,
   blob reference, content type, size, and provenance timestamps/source fields).
3. Retrieval/indexing paths should operate on blob references, not stable local paths.
4. Filesystem import is supported as a first-class path via `import_local_file_copy`.
5. Google Drive/Docs ingestion remains optional and is deferred until auth/session
   lifecycle and export-format policy are fully stabilised.

## Filesystem Import Path

`import_local_file_copy` performs:

1. Workspace-root bounded local file read.
2. Durable upload to configured blob backend (local or Swift).
3. Registration of a `#V#computer_file_copy` concept with blob metadata.
4. Emission of canonical `artifact_record` metadata for downstream indexing/retrieval.

This provides a stable way to ingest pre-existing local files without relying on
ephemeral cache paths.

## Google Drive/Docs Strategy

Google Drive/Docs ingestion is intentionally staged:

1. Reuse existing Google auth/integration primitives.
2. Export bytes to normalised durable artefacts in blob store.
3. Emit the same `artifact_record` shape used by filesystem/arXiv paths.
4. Route retrieval and indexing through the same blob-backed pathways.

Until this is implemented, filesystem import plus existing upload/arXiv flows remain
the supported production path for durable artefact onboarding.

