# Semantic task retrieval

- **Kind:** Bounded implementation and operational reference
- **Lifecycle:** Active
- **Authority:** This retrieval path only; canonical task and access services remain authoritative
- **Owner:** Von maintainers
- **Reviewed:** 13 September 2026
- **Review trigger:** Task visibility, embedding runtime or task search changes

`task_search(query="organise transport to the academic event", search_mode="semantic")`
and `GET /api/tasks/search?search_mode=semantic&query=...` retrieve task meaning
through the existing task search filters. The default remains lexical search.
The semantic mode returns ranked tasks, `semantic_retrieval` diagnostics and
`rag_context` excerpts with task concept IDs for citations. These excerpts are
retrieved data, not instructions or authority. The existing task-search tool
delivers this context to Von without another prompt or workflow stage.

## Representation and retrieval

`task_document.v1` serialises identity, title, description, status, priority,
assignee, creator, organisation, project, collection IDs, labels, components,
source, external references, reference code and relevant timestamps. Context
identifiers preserve canonical references; this slice does not expand them to
project descriptions or ingest comments, attachments, private source archives
or work products. Tasks remain canonical Vontology-backed operational records;
their embedding cache is derived storage, not enduring domain knowledge.

Search reads canonical actor-visible tasks and applies the existing filters
before embedding or ranking. It pins the existing RAG embedding runtime for
the query and document batches, then ranks by cosine similarity with concept
ID as a deterministic tie-breaker. Pagination follows ranking. There is no
unstated relevance threshold: even a weak nearest neighbour can appear, so
consumers must judge relevance from the returned task and score. RAG context
is limited to 12,000 text characters, with explicit excerpt truncation.

The index compares document SHA-256 and the RAG provider/model/host signature.
Unchanged documents reuse vectors; edits, status changes and model changes
regenerate them. The query vector is always generated in the pinned runtime.
Full task documents are retained in the response as ordinary task data; only
vectors, hashes, identifiers and timestamps are stored in SQLite.

## Access, freshness and lifecycle

Semantic search binds the trusted server-session or in-process actor and
forces canonical visibility enforcement. Assignee/project/organisation query
filters cannot supply actor identity. Anonymous queries see public tasks only.
Legacy header-derived identities and access-bypass contexts cannot enter this
semantic path. There is a second batched canonical visibility/existence read
after embedding, which excludes tasks deleted or revoked during embedding.
As with ordinary reads, this is a snapshot, not a serialisable transaction
against edits occurring after the read.

`VON_TASK_SEMANTIC_INDEX_PATH` defaults to the ignored
`data/task_semantic_index.sqlite3`. Newly created files use mode 0600. Treat
vectors as private data and keep the containing runtime directory private.
The cache is partitioned by the trusted actor and organisation. It contains no
independent task text, audience grant or candidate authority. Every request
gets its candidates and content from canonical storage; an old cache row can
never resurrect a deleted task or grant a revoked audience access.

After successful indexing, the actor partition retains only the current
filtered snapshot. A filter change can therefore evict useful cached vectors;
this favours simple recovery and bounded retained data over maximum reuse.
Deleted/revoked rows disappear from that partition on its next search. An
inactive actor partition can retain derived vectors until operator cleanup,
but cannot expose them through retrieval. Concurrent cache writers may cause
extra embedding work, but cannot replace the vectors retained by an in-flight
search for its own snapshot.

Trusted operator code may call `clear_task_semantic_index()` in an actor-bound
context to discard that partition, or `reindex_semantic_tasks()` to rebuild all
currently visible tasks, including bulk tasks. Neither operation changes task
meaning or shared RAG namespaces. Ordinary next searches also rebuild missing
rows, so recovery needs no database migration, scheduler or durable queue.
For a corrupt SQLite file, an operator may stop its callers, move the disposable
file aside, and resume: the next search creates a fresh index. Keep this action
separate from canonical databases. Rolling back the code leaves lexical search
and canonical task state intact; the derived file can be discarded.

## Failure and observability

Embedding, malformed-vector or local index failures retry through existing
lexical search and explicitly return `status=degraded`, `ranking=lexical` and
`error_code=task_semantic_index_unavailable`. They do not claim semantic empty
results or return old cached text. If canonical lexical reads also fail, the
existing task error path reports failure. Retry is on demand, and successful
embedding batches survive later failures. Corrupt individual vector rows are
treated as cache misses. Logs omit task/query text and provider exception
bodies; diagnostics report candidate, embedded, cache-hit and access-recheck
counts, embedding-version hash and indexing/ranking elapsed milliseconds.

## Evidence and operating limits

The tests in `tests/backend/test_task_semantic_index_service.py` exercise the
real filtered task service, repository visibility filters, local SQLite and
Flask route with an isolated mock database and deterministic fixture embedder.
They cover paraphrase geometry versus lexical matching, status/project filters,
pagination, current text after edits, embedding-version changes, corruption,
provider failure, cross-actor and cross-organisation reads, public reads,
revocation, deletion and deletion during embedding. This proves the selected
plumbing and access boundaries, not the deployed embedding model's quality.

`tests/fixtures/task_retrieval/cases.json` supplies ordinary research and
administrative tasks with explicit relevance judgements. The optional harness:

```sh
pdm run python scripts/evaluate_task_semantic_retrieval.py --use-configured-embedder
```

embeds only the synthetic fixture, does not read or mutate canonical tasks,
and reports recall@k, reciprocal rank, nDCG@k, index build time, per-query time
and the embedding signature against the current substring baseline. It requires
an available configured embedder; running it can incur that provider's charges.
No live embedding-provider quality or latency result is claimed by fixture tests.
Access-control acceptance is the route/service test suite, not this harness.

This implementation performs exact vector ranking over matching canonical
tasks. It reuses the existing batched hydration path and adds a batched access
read, without per-task database reads. Cold searches embed every matching task;
warm searches still read canonical task data and rank all candidates. Use
project/collection filters for large imports. Large-corpus latency and an
approximate nearest-neighbour index are not established by this delivery.
No new model-routing policy, task workflow, public deployment or background
index activation is included.
