# Recent Architecture Progress — Updated April 21, 2026

## Purpose

This note records the state of the `JVNAUTOSCI-1913` / `JVNAUTOSCI-1116`
anti-hack and de-bloating programme after the April 21, 2026 landings through
`JVNAUTOSCI-1972`.

For the broader current-code review of where Von still falls short of the
agentic architecture described in `Von_for_AgenticAI.md`, and for the latest
priority ordering after the 23 April 2026 repeat review, see
`docs/engineering/agentic_architecture_status_2026-04-23.md`.

This note remains useful as a historical anti-drift snapshot through the April
21 tranche. Its code-size measurements and "recommended next steps" should be
read as historical April 21 guidance, not as the live post-`1989`
programme-wide ordering.

It replaces an earlier draft that had become materially stale. In particular:

- `JVNAUTOSCI-1929` is now `Done`
- `JVNAUTOSCI-1930` is now `Done`
- `JVNAUTOSCI-1953` is now `Done`
- `JVNAUTOSCI-1954` is now `Done`
- `JVNAUTOSCI-1955` is now `Done`
- `JVNAUTOSCI-1956` has now removed the remaining turn-record response-surface fallback from the primary critic path
- the structured tool-family keyword/prefix heuristic slab is no longer an open violation

The aim of this note is not to provide a full historical changelog. It is to state, as clearly as possible, what has actually been removed, what architectural risks remain, and what the next worthwhile structural steps are.

## Design Intent

Von's architectural doctrine is unchanged:

- durable decision policy belongs in authority surfaces such as Vontology, workflows, prompts, and KB assertions
- Python should provide reusable support surfaces: execution, validation, telemetry, persistence, integration, and safety
- when a behaviour can be represented in workflow/prompt/KB/Vontology rather than encoded as a code-side semantic rule, that represented form should be preferred

The work over April 10–21 materially moved the codebase in that direction. It did not finish the programme.

## What Actually Landed

### 1. Orchestrator lexical and semantic-routing cleanup

The orchestrator no longer owns the worst of the old semantic routing policy.

Completed work in this area:

- `JVNAUTOSCI-1914` removed orchestrator-side lexical routing and guided-retrieval steering
- `JVNAUTOSCI-1943`, `1944`, `1948`, `1949`, `1945`, and `1951` repaired the live grounded tool path, ontology follow-up, evidence shaping, and answer-path degradation without reintroducing prompt-keyword steering
- `JVNAUTOSCI-1929` made the turn expected-outcome contract a first-class boundary object
- `JVNAUTOSCI-1930` made dispatch preflight explicitly contract-aware for multi-surface evidence requirements
- `JVNAUTOSCI-1953` removed the remaining structured tool-family prompt-keyword / prefix heuristic slab and replaced it with authority-backed family resolution derived from represented tool metadata, explicit required tools, and recent actual tool use
- `JVNAUTOSCI-1954` added a first-class predicate-incidence support surface so explicit predicate-relative turns no longer have to rely on broad relation-hit paging before narrowing to the relevant predicate
- `JVNAUTOSCI-1955` stopped grounded false-negative tool-path turns from being silently recorded as `completed_verified`; those turns now surface as `false_success` / `false_completion_claim` when retrieved evidence contradicts a low-information answer
- `JVNAUTOSCI-1956` removed the remaining turn-record response-surface fallback so `required_evidence_answer_consistency_blocker` is now produced only by the authority-backed postcondition critic path

This is a real architectural shift. Structured tool availability is no longer widened or narrowed by Python heuristics such as matching `jira`, `task`, `epic`, or tool-name prefixes against the prompt.

### 2. Workflow discovery and routing substrate cleanup

The workflow-discovery path is no longer based on the old BM25 / stopword / lexical capability-index doctrine.

Completed work in this area:

- `JVNAUTOSCI-1781` recorded the architectural decision that workflow discovery should remain a dedicated retrieval plane rather than collapse into generic concept search
- `JVNAUTOSCI-1915` replaced the BM25 capability index with a RAG-backed workflow retrieval surface over represented workflow artefacts

The old code-side lexical workflow-capability doctrine is gone. Python now provides the retrieval surface rather than being the durable source of recommendation policy.

### 3. Support-surface decomposition and executor cleanup

Several large mixed-responsibility runtime slabs were decomposed into clearer support surfaces.

Completed work in this area:

- `JVNAUTOSCI-1821` extracted part of the custom-workflow dispatch cluster from the worst region of the orchestrator
- `JVNAUTOSCI-1824` decomposed the base workflow executor into clearer support phases
- `JVNAUTOSCI-1950` converged the durable executor on those shared runtime support surfaces so base/durable behaviour does not drift
- `JVNAUTOSCI-1827` turned catalogue assembly into explicit capability-family builder support surfaces rather than one large inline registration slab

This did not solve the orchestrator monolith, but it did remove multiple points where runtime behaviour was being patched inline inside oversized functions.

### 4. Semantic-heuristic removal from domain and authoring services

Several support services that previously contained English-language or regex-heavy interpretation policy were moved toward authority-backed interpretation.

Completed work in this area:

- `JVNAUTOSCI-1916` moved file-copy diagram interpretation away from regex NER / stopword / confidence heuristics into an authority-backed interpretation surface
- `JVNAUTOSCI-1952` replaced person/company/meeting file-copy representation heuristics with one shared authority-backed entity-representation interpretation surface
- `JVNAUTOSCI-1917` removed authoring semantics from English lexical fallbacks in workflow creation and annotation extraction
- `JVNAUTOSCI-1918` moved write-tool request-evidence inference out of prompt-text semantic policy and into a Vontology-backed evidence surface
- `JVNAUTOSCI-1931` hardened workflow publication so transient per-turn specifics are structurally kept out of reusable authority artefacts

### 5. Guardrails, anti-drift, and test quality

The work was not only code movement. Mechanical guardrails were added to stop these regressions from quietly returning.

Completed work in this area:

- `JVNAUTOSCI-1919` extended the workflow-purity report with anti-drift contracts for key support surfaces
- `JVNAUTOSCI-1947` repaired stale tests that were still pinning removed heuristic behaviour or bypassing the authority-backed paths they were supposed to exercise
- `JVNAUTOSCI-1953` added purity-report anti-drift coverage for the retired structured tool-family heuristic symbols

### 6. Operational and tooling cleanup

Completed work in this area:

- `JVNAUTOSCI-1946` migrated the Atlassian MCP path from deprecated HTTP+SSE transport to Streamable HTTP

That is not central to `1913`, but it removed an operational liability and aligned the checked-in control surface with the real supported transport.

## What Changed in Practical Terms

The most important difference from ten days earlier is not a line-count win. It is that several major code-side semantic policy surfaces are now gone.

Before this round:

- workflow discovery still depended on lexical / BM25 logic
- orchestrator tool-family exposure still depended on prompt-keyword and prefix heuristics
- file-copy and workflow-authoring services still carried English-language interpretation hacks
- turn-contract behaviour still leaked across boundaries as case-specific plumbing

Now:

- workflow discovery is retrieval-backed and authority-aligned
- structured tool-family resolution is authority-backed rather than prompt-keyword-driven
- explicit predicate-relative ontology turns now have a first-class incidence lookup rather than broad unfiltered relation paging as the primary narrowing step
- several domain interpretation surfaces have been moved behind prompt/Vontology-backed services
- turn expected-outcome contract handling is explicit and stage-boundary-visible
- turn-contract tool requirements now travel as explicit `required_tools` contract state rather than being re-inferred from prose inside the orchestrator
- grounded false-negative answer paths no longer silently count as verified success in turn-execution correctness reporting
- anti-drift reporting now guards multiple high-risk support surfaces

That is substantial progress even though the remaining monoliths are still too large.

## Current Structural Reality

Some honest current code-size facts:

- `src/backend/integrations/internal_mcp/orchestrator.py` is still `34,608` lines
- `src/backend/integrations/internal_mcp/catalogue.py` is still `27,466` lines
- `src/backend/server/routes/von_routes.py` is still `13,348` lines

So the codebase is materially cleaner in responsibility placement, but not yet dramatically smaller in its largest files.

## Current Jira State Relevant to This Programme

As of April 21, 2026:

| Task | Status | Note |
|------|--------|------|
| `JVNAUTOSCI-1116` | `To Do` | Epic remains open even though many child cleanups are complete |
| `JVNAUTOSCI-1913` | `In Progress` | Still the right umbrella; April 21 follow-through through `JVNAUTOSCI-1972` removed contract-text tool inference and the final turn-contract prose backfill from the live path |
| `JVNAUTOSCI-1929` | `Done` | Turn contract is now a first-class boundary object |
| `JVNAUTOSCI-1930` | `Done` | Dispatch preflight is now contract-aware |
| `JVNAUTOSCI-1953` | `Done` | Structured tool-family keyword/prefix heuristics removed |
| `JVNAUTOSCI-1954` | `Done` | Predicate-incidence lookup landed for entity/type incidence narrowing |
| `JVNAUTOSCI-1955` | `Done` | Grounded false-negative turns no longer count as verified success |
| `JVNAUTOSCI-1970` | `Done` | Remaining write-denial heuristic and hard-coded write-risk classes removed from the live write path |
| `JVNAUTOSCI-1121` | `Done` | `/von/generate` lifecycle and response/debug support surfaces extracted from `von_routes.py` |
| `JVNAUTOSCI-1120` | `To Do` | Frontend monolith cleanup remains open |
| `JVNAUTOSCI-1825` | `Done` | `create_flask_app(...)` is now a materially smaller composition layer over extracted startup/admin support surfaces |
| `JVNAUTOSCI-1971` | `Done` | Residual buttonify heuristic/prose-recovery helpers and stale compatibility surfaces removed; live path is structured-output-only |
| `JVNAUTOSCI-1972` | `Done` | Residual turn-contract prose parsing removed; structured contract state is now the only live Python authority for `required_tools` |
| `JVNAUTOSCI-768` | `To Do` | Vontology concept structure for tool heuristics still not landed |
| `JVNAUTOSCI-770` | `To Do` | Rule-loader integration task still not landed |
| `JVNAUTOSCI-1813` | `To Do` | Deferred structural exception-barrier task, not in the core `1116` line |

## What Still Remains

The remaining work is now more concentrated. The biggest live problems are no longer the same ones this note identified earlier.

### 1. Buttonify’s residual heuristic helper slab has now been removed

**Location:** [buttonify_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/buttonify_service.py:152)

The live route/workflow path was already structured-output-first through `#V#chat_buttonify_workflow` and `buttonify_options_json`, but `buttonify_service.py` still carried residual heuristic/prose-recovery helpers and stale tests/docs that obscured the intended authority boundary.

That residual slab is now gone. Buttonify options are validated from structured output only, and malformed/non-JSON output fails closed instead of being reverse-engineered from prose.

### 2. The orchestrator remains the biggest structural risk

**Location:** [orchestrator.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/integrations/internal_mcp/orchestrator.py:28634)

Even after the recent cleanups, `InternalMCPChatOrchestrator` is still an oversized integration monolith. The most egregious surviving semantic slab from the previous draft, the structured tool-family prompt-keyword heuristic block, is gone, and `1954`/`1955` repaired two important grounded-answer failure families. But the class still combines too many concerns:

- workflow dispatch
- tool planning and tool execution orchestration
- turn reporting and tracing
- renderer and response shaping paths
- multiple support-policy seams that are still easier to patch inline than to extract

This is the main structural reason new hacks tend to accumulate here first.

### 3. Backend route layers improved materially, but the monolith is still too large

**Location:** [von_routes.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/server/routes/von_routes.py)

`JVNAUTOSCI-1121` is now done, and it made a real structural dent: the `/von/generate` durable-turn lifecycle, chat-history persistence, and success/error payload shaping helpers were extracted into `generate_route_support.py`, taking several hundred lines out of `von_routes.py` and giving later route work clearer seams. But the backend route layer is still large and mixed in responsibility, so this remains an ongoing structural concern rather than a solved problem.

### 4. Flask app-factory sprawl is no longer the main route-layer blocker

**Location:** [utils_flask.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/server/utils_flask.py)

`JVNAUTOSCI-1825` is now done. The old `create_flask_app(...)` slab has been cut down materially by extracting request timing/configuration, startup bootstrap, chat-history admin context resolution, diagnostics helpers, and prewarm wiring into explicit support surfaces. The file is still large, but the structural risk has shifted away from one huge all-purpose app-factory function and toward the remaining large route modules themselves.

### 5. Catalogue size is still large, but the risk has shifted

**Location:** [catalogue.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/integrations/internal_mcp/catalogue.py:29952)

`JVNAUTOSCI-1827` already removed the worst inline assembly slab by turning `build_default_catalogue()` into a composition layer over builder helpers. The file is still very large, but the remaining risk is now more about size, merge pressure, and further per-family decomposition than about one egregious remaining semantic-hack block.

That means it is still a structural concern, but it is not the highest-priority anti-policy-drift problem anymore.

## Revised Summary of Remaining Violations

| Category | Current state | Best current handling |
|----------|---------------|-----------------------|
| Structured tool-family prompt heuristics | Removed by `1953` | Keep anti-drift guard in place |
| Low-information answer-surface heuristic | Removed by `1956` | Keep the prompt-backed critic authoritative |
| Turn contract stage-boundary plumbing | Fixed by `1929` | Done |
| Multi-surface dispatch verification | Fixed by `1930` | Done |
| Write confirmation / destructive regex | Removed by `1970` | Keep represented authority surfaces canonical |
| Hardcoded write-risk tool classes | Removed by `1970` | Keep represented runtime profile authoritative |
| Residual buttonify heuristic helper slab | Removed by `1971` | Keep structured-output validation fail-closed |
| Orchestrator monolith | Still present | Ongoing structural extraction |
| Backend route monolith | Improved by `1121`, still present | Continue route/app-factory decomposition |
| Flask app-factory sprawl | Reduced materially by `1825` | No longer the sharpest open route-layer problem |

## Recommended Next Steps

This section is now historical for overall programme sequencing.

After the later same-day landings through `JVNAUTOSCI-1994`, `1993`, and the
repeat review in `1989`, the live next-task ordering moved to the broader
memory/evaluator/self-improvement umbrellas recorded in
`docs/engineering/agentic_architecture_status_2026-04-23.md`.

The still-relevant `JVNAUTOSCI-1913`-local guidance from this note is narrower:

1. continue orchestrator extraction where it removes real mixed-responsibility
   pressure or hidden policy drift rather than opening noise tickets based only
   on file size;
2. continue route-layer de-bloating where it produces clearer reusable support
   seams; and
3. keep anti-drift tests and purity checks aligned with the newer
   authority-backed architecture.

Do not use this note's April 21 sequencing as the current programme-wide next
step list.

## Implementation Note — April 21, 2026

A first support-surface landing has now been made toward item 2 above.

`turn_execution_record_service.py` now accepts an authority-supplied `required_evidence_answer_consistency_blocker` via `critic_verdict` and records that source explicitly when the completion gate is driven by the authored critic.

`run_turn_execution_critic(...)` now also emits a structured `critic_verdict` on the live action path so that this authority seam is carried through workflow outputs, diagnostics, and turn-record finalisation rather than existing only as a record-builder input contract.

At that stage, the gate was no longer structurally forced to depend on a Python response-surface check. That mattered because it let a workflow- or LLM-authored critic take ownership of the decision without another turn-record schema change.

### Follow-on Landing — April 21, 2026

That authoritative producer has now been introduced on the canonical conversation-turn path under `JVNAUTOSCI-1956`.

- `#V#conversation_turn_execution_workflow` and `#V#tool_calling_workflow` no longer call `turn_execution.critic` directly for the postcondition-critic stage. They now invoke `#V#kb_mutation_postcondition_critic_workflow` as an explicit subworkflow.
- `#V#kb_mutation_postcondition_critic_workflow` is now a real multi-step authority surface: it first builds a bounded turn-execution evidence bundle, then runs `llm.action` against the new authoritative prompt concept `#V#prompt_turn_execution_postcondition_critic`, and only then finalises the canonical turn-execution record.
- `turn_execution_runtime_support.py` now supports an evidence-building mode that suppresses the synthetic default `critic_verdict`, so the prompt-authored critic can become the primary producer instead of merely decorating a Python-owned judgement.
- `turn_execution_record_service.py` no longer derives `required_evidence_answer_consistency_blocker` from response-surface heuristics. If the authoritative critic path cannot produce a blocker, the turn record now leaves that field empty rather than reviving the retired Python detector.

### Follow-on Landing — April 21, 2026

A bounded `JVNAUTOSCI-1913` follow-on landing in `JVNAUTOSCI-1972` has now
removed the remaining orchestrator contract-text tool-inference seam.

- `TurnExpectedOutcomeContract` now carries explicit `required_tools` state and preserves it across stage-boundary rendering, dispatch preflight, and selector/tool/recovery context projection without reparsing support prose back into Python decision state.
- the expected-outcome inference prompt now emits `required_tools` as part of the authoritative contract payload instead of forcing later Python recovery from prose wording
- the old orchestrator helpers that inferred KB / relation / web retrieval surfaces by scanning contract text have been deleted

That means the next meaningful anti-egregious-path work is no longer more
contract-prose parsing cleanup. It is continued orchestrator extraction where it
reduces mixed-responsibility pressure, followed by the represented tool-metadata
and rule-loader follow-through tasks.
