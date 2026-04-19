## JVNAUTOSCI-1781 Workflow Discovery Architecture Review

Status: Final review for Jira closure  
Date: 19 April 2026  
Author: Codex

### 1. Purpose

`JVNAUTOSCI-1781` asked whether workflow discovery should remain on the current
capability index, move to a scalable RAG-backed architecture, or adopt a hybrid
design.

As of 19 April 2026, that framing is materially stale. Von already has a
hybrid discovery pipeline, but its primary dedicated workflow tier is still a
Python-authored lexical BM25 index. The live architectural question is now:

- should Von keep a dedicated workflow retrieval plane;
- should that plane remain lexical/BM25 based;
- or should it evolve into a scalable authority-aligned hybrid retrieval
  substrate while preserving the current discovery API boundary.

This document closes `1781` as the architectural review/design task and defines
the target future runtime state that subsequent implementation tasks should
deliver.

### 2. Current Live Architecture

#### 2.1 Discovery pipeline

The current discovery path is layered rather than singular:

- `src/backend/services/workflow_discovery_service.py:61` sets a hard
  `SEARCH_TIMEOUT_SECONDS = 0.5`.
- `src/backend/services/workflow_discovery_service.py:1137` begins the primary
  dedicated workflow search via `_search_workflow_capabilities(...)`.
- `src/backend/services/workflow_discovery_service.py:335` falls back to
  `_search_workflows_semantic(...)`, which delegates to general concept semantic
  search.
- `src/backend/services/workflow_discovery_service.py:383` then falls back to
  `_search_workflows_vontology(...)`.
- `src/backend/services/workflow_discovery_service.py:1182` composes these into
  `discover_workflows(...)`.
- `src/backend/services/workflow_discovery_service.py:1416` wraps this for turn
  use in `discover_workflows_for_turn(...)`.

#### 2.2 Current dedicated workflow tier

The dedicated workflow tier is still transitional:

- `src/backend/services/workflow_capability_service.py:57` defines
  `_TOKENISE_RE`.
- `src/backend/services/workflow_capability_service.py:58` defines
  `_STOP_WORDS`.
- `src/backend/services/workflow_capability_service.py:150` defines
  `WorkflowCapabilityIndex`.
- `src/backend/services/workflow_capability_service.py:191` builds the index
  from authoritative workflow registry material via `index_from_registry(...)`.
- `src/backend/services/workflow_capability_service.py:321` performs ranked
  search over that in-memory BM25 index.
- `src/backend/services/workflow_capability_service.py:629` exposes runtime
  state for readiness/build/error diagnostics.
- `src/backend/services/workflow_capability_service.py:812` exposes the public
  `search_workflow_capabilities(...)` API.

This is already better than hard-coded workflow-family heuristics because the
indexed text is sourced from authoritative Vontology workflow descriptions and
routing metadata. But the retrieval core remains Python-authored lexical policy.

#### 2.3 General semantic fallback

The semantic fallback is not a dedicated workflow retrieval surface:

- `src/backend/services/concept_search_service.py:544` defines
  `_semantic_search(...)`.
- `src/backend/services/concept_search_service.py:643` defines
  `search_concepts(...)`.
- `src/backend/services/concept_search_service.py:721` includes `"semantic"` in
  the supported `match_type` values.
- `src/backend/services/concept_search_service.py:913` routes semantic requests
  through `_semantic_search(...)`.
- `src/backend/services/concept_embedding_service.py:42` fixes the concept
  embedding namespace as `"concepts"`.
- `src/backend/services/rag_backends/llamaindex_backend.py:120` defines
  `LlamaIndexRAGService`.
- `src/backend/services/rag_backends/llamaindex_backend.py:220`,
  `:239`, and `:393` show that this backend already provides a namespace-aware
  persisted retrieval surface with incremental query support.

This means Von already has a scalable general retrieval substrate, but workflow
discovery is not yet using a dedicated hybrid workflow namespace on top of it.

#### 2.4 Selector/routing context

Workflow discovery is already part of a richer turn pipeline:

- `src/backend/workflows/conversation_turn_stage_model.py:282` declares the
  `expected_outcome_inference` stage.
- `src/backend/workflows/conversation_turn_stage_model.py:292` declares the
  `selector_preparation` stage.
- `src/backend/workflows/conversation_turn_stage_model.py:302` declares the
  `selector_decision` stage.
- `src/backend/integrations/internal_mcp/orchestrator.py:24948` defines
  `_prepare_turn_selector_context_outputs(...)`.
- `src/backend/integrations/internal_mcp/orchestrator.py:24962` derives the
  `expected_outcome_contract`.
- `src/backend/integrations/internal_mcp/orchestrator.py:24999` calls
  `discover_workflows_for_turn(...)`.
- `src/backend/integrations/internal_mcp/orchestrator.py:25378` begins the
  route/action phase that consumes the prepared discovery output.
- `src/backend/workflows/workflow_selector.py:515` and `:790` show that the
  selector already consumes candidate workflows as a distinct decision surface.

This matters because the future retrieval substrate should improve recall and
candidate quality without moving selector or answer policy back into Python.

#### 2.5 Present test limitations

The current unit surface does not strongly exercise real dedicated recall:

- `tests/backend/test_workflow_discovery_service.py:53` autostubs
  `_search_workflow_capabilities` to return `[]`.
- `tests/backend/test_workflow_discovery_service.py:60` also stubs capability
  runtime state to a ready/no-error shape.

That is acceptable for narrow unit tests, but it means `1781` should not infer
architectural adequacy from those tests alone.

### 3. Recent Failure Evidence

`1781` should be interpreted in light of the current replay evidence, not only
the older task wording.

#### 3.1 Broad fallback instead of clean workflow retrieval

`logs/replay_1925_result.json` captured an earlier replay for:

`What are key predicates or represented relationships for SAIL students?`

Observed evidence:

- `conversation.request_id = live-kb-prompt-fc50bb79-d4bb-4908-b513-440645fb0af9`
- `evaluation.selected_workflow_id = #V#tool_calling_workflow`
- `evaluation.observed_tools = [search_knowledge_base, search_concepts, jira_search, search_web, search_arxiv]`

That is not a clean workflow retrieval success. It is broad tool fan-out from
the generic tool-calling path.

#### 3.2 Null workflow and zero tool path

`logs/replay_1925_salient_variant_result.json` captured the variant:

`What predicates are salient to SAIL students?`

Observed evidence:

- `request_id = live-kb-prompt-62731277-00c4-41bc-b759-ab23e926a015`
- `selected_workflow = null`
- `observed_tools = []`
- response began: `I could not find any direct information...`

This is earlier-stage failure: no grounded workflow or tool path was selected
at all.

#### 3.3 Post-1914 reduction in broad heuristic fan-out

`logs/replay_1914_1925_result.json` shows the same original prompt after the
`1914` lexical-heuristic removal:

- `evaluation.selected_workflow_id = #V#tool_calling_workflow`
- `evaluation.observed_tools = [find_relations_with_argument, search_knowledge_base]`

This is better than the older five-tool fan-out, but it is still not a robust
authority-aligned workflow retrieval outcome.

### 4. Architectural Options

#### Option A: Keep the current BM25 capability index as the long-term substrate

Advantages:

- very simple cold start and rebuild behaviour;
- fast in-memory lookup;
- good observability because the index is small and explicit.

Problems:

- lexical tokenisation, stopwording, and normalisation remain Python-authored
  semantic policy;
- multilingual and paraphrase robustness remains poor;
- the current replay failures are consistent with missed semantic recall and
  selector candidate starvation;
- this does not align with the broader `1913` anti-hack direction.

Decision: reject as the long-term architecture.

#### Option B: Collapse workflow discovery into the general concept-search plane

Advantages:

- reuse the existing concept vector infrastructure;
- remove one dedicated workflow retrieval subsystem.

Problems:

- workflows are not ordinary concepts for retrieval purposes; they need a
  different index unit and metadata contract;
- workflow discovery needs executability, routing-profile, publication, and
  discovery-exemplar awareness, not just concept similarity;
- mixing workflows into the general concept retrieval plane would weaken
  observability and make workflow-specific ranking harder to diagnose;
- the current concept semantic path is a fallback convenience, not an explicit
  workflow retrieval authority surface.

Decision: reject as the primary architecture.

#### Option C: Keep a dedicated workflow retrieval plane, but replace the
transitional BM25 core with a scalable hybrid retrieval substrate

Advantages:

- preserves the correct architectural boundary: workflow retrieval remains its
  own support surface;
- keeps authority in Vontology workflow text, routing metadata, and discovery
  exemplars rather than in Python heuristics;
- supports a future runtime state with dense, sparse, or multi-vector recall
  without changing the discovery API;
- fits both today’s implementation constraints and the expected future runtime
  state.

Decision: adopt.

### 5. Recommended Future Runtime State

The future runtime state should look like this:

1. A dedicated workflow retrieval namespace remains the primary recall surface.
2. Indexed workflow documents are materialised only from authoritative
   Vontology workflow text and metadata:
   - workflow description/purpose text
   - routing profile metadata
   - discovery exemplars
   - publication/executability metadata needed for later filtering
3. The retrieval engine behind `search_workflow_capabilities(...)` becomes a
   scalable hybrid substrate rather than a Python BM25 implementation.
4. Hybrid means:
   - dense semantic recall for paraphrase and multilingual robustness;
   - sparse or lexical-compatible recall for exact terminology and rare tokens;
   - optional later reranking or late interaction where justified by runtime
     budgets.
5. Executability, routing eligibility, and safety filtering remain explicit
   support-surface checks after retrieval, not hidden lexical proxies before it.
6. Discovery keeps a hard bounded latency budget, but the budget is spent on a
   dedicated workflow retrieval plane rather than on a Python-authored lexical
   first pass plus fallback retries.
7. Telemetry records:
   - retrieval source and namespace
   - candidate counts before and after filtering
   - build readiness / cold-start state
   - whether dense, sparse, or hybrid recall was used
   - the effective discovery query used after expected-outcome enrichment
8. Failure semantics remain fail-closed:
   - no fallback to guessed Python-authored workflow descriptions;
   - no return to English stopword lists or token overlap steering.

### 6. Authority Model

The correct authority split is:

- Authoritative:
  - workflow descriptions and purpose text in Vontology text relations
  - workflow routing profiles
  - workflow discovery exemplars
  - workflow publication/executability metadata
- Support-only code:
  - indexing/materialisation pipeline
  - retrieval backend adapter
  - bounded latency/timeouts
  - post-retrieval validation and filtering
  - telemetry and diagnostics

The retrieval model, index structure, and storage engine may evolve. The
authored workflow discovery policy must not drift back into Python token tables,
stopword sets, or hard-coded semantic cues.

### 7. Cold Start, Rebuild, and Incremental Update

#### 7.1 Current state

The current capability index is simple to rebuild in-process and easy to expose
through runtime state. By contrast, the concept retrieval plane already has a
more scalable lifecycle:

- `src/backend/utilities/concept_index_worker.py:27`, `:169`, and `:182` show a
  worker-driven indexing path for concept embeddings.
- `src/backend/services/concept_embedding_service.py:36`, `:38`, `:631`, and
  `:718` show explicit stale/pending/indexed status handling and aggregate
  stats.

#### 7.2 Required future behaviour

The dedicated workflow retrieval plane should adopt equivalent lifecycle
discipline:

- authoritative workflow text/metadata changes mark workflow retrieval
  documents stale;
- incremental reindex should be the default;
- full rebuild remains available for cold start or drift repair;
- discovery telemetry should expose whether misses occurred because the index
  was stale, empty, or unavailable.

### 8. Short Retrieval Literature Review

This review is intentionally short and targeted toward the architectural choice.

- Lewis et al., *Retrieval-Augmented Generation for Knowledge-Intensive NLP
  Tasks* (NeurIPS 2020), showed the value of combining parametric generation
  with explicit non-parametric retrieval for knowledge-intensive tasks, with
  better factuality and updateability than parametric-only generation.
  Source: <https://arxiv.org/abs/2005.11401>
- Santhanam et al., *ColBERTv2* (NAACL 2022), showed that late interaction can
  materially improve retrieval quality while using compression to keep the space
  cost manageable.
  Source: <https://arxiv.org/abs/2112.01488>
- Formal et al., *SPLADE v2* (2021), showed that learned sparse retrieval can
  preserve exact-term and inverted-index strengths while substantially
  outperforming plain lexical baselines on retrieval benchmarks.
  Source: <https://arxiv.org/abs/2109.10086>
- Chen et al., *M3-Embedding* (2024/2025 revisions), is directly relevant to
  Von’s expected future runtime state because it unifies dense, sparse, and
  multi-vector retrieval in one multilingual model family.
  Source: <https://arxiv.org/abs/2402.03216>

Implication for Von: the long-term choice should not be “BM25 or dense vectors”.
The right design is a hybrid workflow retrieval plane with a stable API and
authoritative Vontology inputs, leaving room for dense+sparse or dense+sparse
+ multi-vector implementations as runtime maturity increases.

### 9. Decision

`JVNAUTOSCI-1781` is resolved with this recommendation:

- keep a dedicated workflow retrieval plane;
- retire the current Python BM25/stopword capability index as the long-term
  retrieval core;
- replace it with an authority-aligned scalable hybrid workflow retrieval
  substrate;
- keep the existing discovery API boundary where practical so that the change
  is mostly internal to the retrieval support surface.

### 10. Follow-On Work

#### 10.1 Immediate implementation follow-on

`JVNAUTOSCI-1915` is the direct implementation task for this architectural
decision. It should:

- replace the current BM25/English stopword discovery surface;
- preserve authoritative Vontology workflow text/metadata as the indexed input;
- add proper lifecycle and telemetry for the new retrieval substrate.

#### 10.2 Replay-driven failure follow-on

`JVNAUTOSCI-1943` remains the targeted follow-on for the salience/paraphrase
failure family. It should not redefine the retrieval architecture itself. It
should implement and validate the discovery/selector path against this chosen
architecture.

### 11. User Impact Summary

This task does not directly change a production answer path by itself. Its
effect is to remove architectural ambiguity before more code is written in the
wrong direction.

If the follow-on work is implemented correctly, users should see:

- fewer missed workflow candidates for paraphrased or multilingual requests;
- less fallback to generic tool-calling behaviour;
- better diagnosability when workflow recall fails;
- better long-term scaling as the workflow catalogue grows.

Without this decision, Von is likely to keep accumulating small lexical fixes on
top of a transitional retrieval tier.
