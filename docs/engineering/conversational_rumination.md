# Conversational rumination

- **Kind:** Capability and acceptance record
- **Reviewed:** 14 September 2026
- **Task:** `#V#task_agent_42b6d9f8b35a707c5a43bf57428c4c8e`
- **Scope:** Optional background inquiries in actor-owned, organisation-scoped
  conversations. Publication does not activate or deploy a running server.

An ordinary turn can choose `task_start_background_inquiry` when investigating
an unknown is likely to help later conversation. The model can also do nothing.
Names, novelty and missing profile fields do not trigger a separate classifier
or compulsory stage. The foreground continues after submission.

The inspected represented rumination definitions perform ontology enrichment;
they did not supply this conversational inquiry capability. This implementation
reuses canonical tasks, task execution submission, the existing server dispatcher,
current task work products and the foreground conversation situation. It adds
no scheduler, worker process, workflow, product store or represented-policy
activation. The direct capability instructions are versioned with its interface;
they do not override a Vontology-governed prompt.

## Execution and authority

The ordinary-turn gateway binds actor, organisation, namespace, source session,
request ID and user prompt. The selected exact mention must occur in that turn.
The task notes preserve the mention, question and selected relevant context with
the source session and turn. Only this bounded context is copied; the full source
conversation is not sent to background research or external search.

Creation and launch use existing actor-scoped idempotency. One source turn has
one stable inquiry identity even if retry wording changes. A completed retry
returns the existing product instead of launching research again. Subsequent
turns see pending and completed inquiries, allowing the model to avoid repeating
the same opportunity. This semantic cross-turn choice is distinct from exact
transport-retry deduplication.

Independent submission verifies the source task's creator, organisation, Von
assignee and actor-owned conversation. A server-derived task carrier has its own
history, situation and execution fence. Queue reconciliation rechecks the source
and destination; public task execution payloads cannot opt into this internal
mode. Background inquiry recursion is rejected: this capability authorises one
inquiry, not an expanding task tree.

The task uses retrieval within the existing actor scope. Source material is
untrusted evidence; it does not authorise messages, broader publication or
changes to the source conversation. Findings must distinguish actual retrieval,
candidate identities, uncertainty and proposals. An unavailable search is a
valid retained outcome, not proof of nonexistence. The task links a readable
canonical current work product. An actor-effective content assertion on its
existing execution concept is a supported product carrier; a new concept is not
required.

## Later conversation

The foreground reads at most three recent source-linked inquiry products,
checking the task creator and organisation as well as normal concept visibility.
Each content projection is capped at 5,000 characters and carries the full-body
hash, product/task IDs, original source context and explicit truncation. Older
omission and unavailable reads are reported distinctly. Canonical products remain
available through existing task and content tools.

The conversational model decides whether a result merits mention or a question.
It reconciles the latest topic, corrections and declines with the old inquiry,
and retains surfaced/declined/corrected product references in the existing
conversation situation. The background task never replaces that situation.
This is model judgement, not a guarantee that every useful connection will be
raised or every stochastic repetition prevented.

## Bounded acceptance, 14 September 2026

Authenticated tests used this task's isolated DGX fixture, synthetic Alice and
organisation, the existing dispatcher and provider-observed `gpt-6-astra`.
Source conversations, queue carriers, task products, full results and telemetry
are retained in the controller run evidence under
`.run/conversational-rumination/`. The source preparation archives are
`#V#computer_file_copy_b2125cea8d3c462582a1d3ffc156a381` (SHA-256
`dcb304e485a07c84296fe7c26c3c4db797f628384d1745560d6e8c4703f5f9d2`)
and `#V#computer_file_copy_75f55ab47b6041eba8fa1f01862493e9` (SHA-256
`7f40a2e1c2ab5c25c73a0582a442b278f551723879b2d52638bf6e3df77eead7`).
These are scoped source records, not claims of public deployment.

| Case | Observation |
| --- | --- |
| Seminar planning mentioning a known researcher | Answered without starting an inquiry; curiosity was optional. |
| Research idea and requested working title | Quietly submitted one Scone inquiry and returned only the title in 26.1 seconds. |
| Explicit independent-launch control | Submitted a task and answered in 32.0 seconds; research ran for 179.3 seconds on a distinct carrier. |
| Foreground during research | Rewrote a calendar label in 10.1 seconds without raising the research topic. |
| Correction while inquiry ran | Retained “Cyc, not Scone”; after the Scone product arrived, a later answer still used Cyc and the corrected title wording, without another inquiry or question. |
| Unavailable retrieval | Saved actor-visible products with attempted URLs and explicit missing-key/index observations; later synthesis did not invent primary-source findings. |
| Paper connection | Quietly investigated arXiv 2304.03442 while returning a title in 29.4 seconds. `get_paper_metadata` retrieved its actual abstract via the arXiv abstract-page fallback. The saved product includes the URL, retrieval evidence, candidates, uncertainty and a proposed study connection. |
| Later sourced follow-up | Used the retained paper product, linked the actual source, separated abstract-supported findings from extrapolation, and offered a concrete study question. A preceding general study turn chose not to mention the paper. |
| Declined topic | Retained the report and answered two subsequent calendar-label requests without another research prompt (5.5 and 3.0 seconds). |

The positive product is
`#V#task_execution_d58705e70904ba0481f11c52967c20cc`, linked from
`#V#task_agent_5c685f68cd679081b12b441097a6ff40`; its observed content SHA-256 is
`cbf869affc97d2f11a16d8ce7d46d001dcb3da8fcd0bfbd4abdf63092d0af314`.
It records metadata retrieval evidence `ev_DHH9K6S7OKbK9d2oieY_M4Hm` and metadata
payload hash `99286977ce7432d1a82dee0fbe446cebf000c1abc8cef54bfc620d5f579dcf55`.
The PDF was not read; only abstract-level claims were supported.

The three completed background runs took 179.3, 246.1 and 248.8 seconds, with
24, 30 and 32 model calls. Reported total tokens were 770,761, 1,140,175 and
1,051,995; cached input tokens were 666,194, 962,446 and 900,095 respectively.
Exact model pricing was unavailable, so no monetary-cost estimate is claimed.
The sparse fixture lacked task vocabulary and general product types: canonical
fixture preparation repaired the minimum task vocabulary, and models recovered
from unavailable product creation by using scoped content on existing execution
records. These runs establish operation, not cost efficiency. Web search and
extraction lacked the fixture's Tavily key. No host credentials were copied or
changed, and no production data or memberships were changed.

144 targeted tests passed across execution, queue, task products, conversation
projection and the new inquiry boundary; the eight inquiry tests also passed
after compacting the submission receipt. The ordinary-turn suite had 280 passes
and two database-authentication failures reproduced against unchanged source.
Minimum Python 3.11 syntax and the PDM lock check passed. The final receipt
compaction changes returned metadata only; it omits the redundant queued prompt.

Merge evidence supports this narrow capability. General initiative quality,
long-term role improvement, public OAuth, public deployment and production cost
remain outside this acceptance claim.
