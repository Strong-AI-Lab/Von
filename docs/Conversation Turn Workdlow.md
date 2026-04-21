# Conversation Turn Workflow

## Purpose
Provide a clear, end-to-end model of what happens from a user submitting a prompt to Von returning a response. This document describes the current behaviour and sequencing, with explicit decision points and outputs so it can serve as a baseline for future workflow design.

## Actors and Components
- **Frontend chat UI**: collects the prompt, renders the assistant message, manages LLM debug display, and post-processes rendered HTML. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js).
- **Backend generate handler**: `/von/generate` orchestrates prompt loading, tool use, presenter handling, and response assembly. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py).
- **Internal MCP orchestrator**: LLM + tool loop and workflow selector. See [src/backend/integrations/internal_mcp/orchestrator.py](src/backend/integrations/internal_mcp/orchestrator.py).
- **Workflow tracing utilities**: execution traces and workflow definitions. See [src/backend/workflows/definitions.py](src/backend/workflows/definitions.py) and [src/backend/workflows/__init__.py](src/backend/workflows/__init__.py).

## Inputs and Outputs
### Inputs
- User prompt text and client context (user id, org id, language, gmail profile).
- Presenter mode flag (always true from the chat UI).
- Authenticated session/namespace and prompt fragments from ontology.

### Outputs
- `response` (screen text shown to the user).
- `presenter_channels` (screen + spoken + format metadata).
- `llm_debug` (context stats, tool stats, aux traces).
- `rag_trace` and tool progress metadata (when enabled).

## Workflow Model (Current State)
### Stage 1: Frontend submit
1. The user enters text and submits the prompt.
2. The UI sends `/von/generate` with `presenter_mode: true`. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L5640-L5750).
3. A tool-use progress polling loop starts while the request is in flight. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L120-L250).

### Stage 2: Backend intake and context assembly
1. `/von/generate` derives the effective user/namespace and loads prompt fragments from the ontology (behaviour, screen, narration). See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L2050-L2245).
2. The handler builds the enhanced context from history + system prompts, and trims it for size. Tool results can also be truncated for storage. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L603-L760) and [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L2800-L2920).

### Stage 3: Orchestrator run (LLM + tool loop)
1. If the internal MCP orchestrator is available, it is invoked with prompt, context, model, namespace, and auxiliary system prompt. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L2910-L3050).
2. The orchestrator may call a workflow selector for authenticated presenter turns, used mainly to decide narration routing. See [src/backend/integrations/internal_mcp/orchestrator.py](src/backend/integrations/internal_mcp/orchestrator.py#L2905-L2965).
3. The orchestrator executes the tool loop: LLM response → strict tool-call JSON parsing → tool invocation → tool result message → repeat until no further tool calls or caps reached. See [src/backend/integrations/internal_mcp/orchestrator.py](src/backend/integrations/internal_mcp/orchestrator.py#L2774-L2925).
4. During post-tool backfill, the orchestrator now runs a **minimal-imposition auto-proceed gate**. If the model says it is continuing (for example “Proceeding now”) and does not request a user decision, the missing-tool-call subworkflow is invoked to continue execution in the same turn. This is controlled by `auto_proceed_minimal_imposition_enabled` (default: enabled).

### Stage 3a: Ontology discovery context (current reality)
There is **no dedicated ontology discovery pre-flight** today. The orchestrator does not inject a curated list of existing predicate/type concepts before tool planning. This means the LLM must already “know” or guess correct `#V#...` predicate IDs, which increases invalid predicate attempts.

Existing building blocks that are **not yet wired into tool planning**:
- **Annotations pipeline** (span extraction + candidate concept lookup). The backend can extract spans and candidate concepts via `extract_annotations` + `concept_search_service`, but chat only triggers this when the UI toggle is enabled, and results are not fed into tool selection. See [src/backend/services/annotation_extraction_service.py](src/backend/services/annotation_extraction_service.py) and [src/backend/server/routes/annotations_routes.py](src/backend/server/routes/annotations_routes.py).
- **Salient predicates** (per-instance/type). A cached endpoint exists for `#V#salient_binary_predicate_for_type`, but it is currently used for UI tooling, not tool planning. See [src/backend/server/routes/vontology_routes.py](src/backend/server/routes/vontology_routes.py#L3233-L3760).
- **RAG concept description search**. `search_concept_descriptions` is available via internal MCP, but the default chat flow does not invoke it (except the RAG counts fastpath). See [src/backend/integrations/internal_mcp/catalogue.py](src/backend/integrations/internal_mcp/catalogue.py#L3317-L3580).

### Stage 4: Presenter protocol handling
1. The model response is parsed for `<screen>` and `<spoken>` tags. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L3035-L3120).
   - Tags inside fenced code blocks are ignored, so “format examples” do not count as real presenter output.
2. **Screen backfill** runs when tools were used and the screen channel is missing or unsuitable. Heuristics include missing tags, empty screen, tool-dump-like output, or screen identical to spoken. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L3050-L3185).
   - Backfill order: response text (if safe) → LLM synthesis using tool summaries → deterministic tool summary. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L3185-L3335).
   - If **no tool messages exist**, screen backfill still runs, but uses a non-tool synthesis prompt (user + model response only).
3. **Spoken backfill / narration** runs if spoken is missing. It uses either a narration workflow (when tracing is enabled) or a local LLM narration prompt. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L3360-L3535).

### Stage 5: Response assembly and persistence
1. The final response is the screen channel (except for debug-only tool-result fallback formats). See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L3535-L3615).
2. Debug metadata is assembled (context stats, tool stats, presenter health). See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L3615-L3845).
3. On authenticated turns, user/tool/assistant messages are persisted to session history; unauthenticated flows use in-memory context. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L2760-L2890).

### Stage 6: Frontend render and post-processing
1. The UI renders the assistant message using `presenter_channels.screen`, with optional spoken text stored for TTS. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L5675-L5745).
2. If markdown rendering is enabled, the rendered HTML is post-processed for quick-reply “buttonify” transforms. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L1730-L1825).
3. If annotation is enabled, the response is sent for annotation and suggestions are rendered. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L5695-L5745).

## Decision Points and Branches
- **Direct tool-call debug mode**: optional path allowing user-provided tool JSON (read-only by default). See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L2737-L2835).
- **Orchestrator availability**: no orchestrator → single LLM call, no tool loop. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L2910-L2995).
- **Tool-call parsing failures**: parsing errors are captured as tool-invocation errors and surfaced in the response. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L2990-L3050).
- **Screen/spoken backfill**: only triggers when presenter channels are missing or unsuitable. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L3050-L3535).

## Buttonify (Quick-Reply) Model
When markdown rendering is active, the UI transforms certain patterns into prompt-insert buttons.

### Trigger points
- Render pipeline: `renderChatMarkdownIntoContainer()` applies transforms after HTML injection. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L1730-L1785).
- Render toggle: `setVonMessageRenderMode()` re-applies transforms when switching to rendered view. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L1785-L1825).

### Transform types
1. Blockquote quoted instruction → button. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L1472-L1565).
2. Inline strong quoted instruction → button (gated by allowed patterns). See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L1572-L1635).
3. Reply options list → buttons. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L1323-L1470).
4. List item quoted instruction → button. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L1619-L1720).

### Behaviour
- Click inserts text into the prompt.
- Shift-click inserts without auto-send.
- Auto-send uses `submitChatPromptImmediately()`. See [src/frontend/web/von_interface/static/js/chatTab.js](src/frontend/web/von_interface/static/js/chatTab.js#L740-L820).

### Optional model-driven buttonify
When `VON_BUTTONIFY_MODEL_ENABLE=1`, the backend runs the `buttonify` stage / `#V#chat_buttonify_workflow` to emit structured quick replies in `llm_debug.buttonify.options`.  
The live backend path is structured-output-only: there is no heuristic preflight or prose-scraping fallback. If the workflow/LLM path cannot emit valid JSON options, buttonify no-ops with source `none`; successful structured extraction records source `llm`.

### Canonical response-transformation telemetry contract
All response transformations now emit a canonical, per-turn telemetry payload in `llm_debug.response_transformations` (schema `response_transformations_v1`).

Each transformation event includes mandatory fields:
- `transform_name`
- `transform_version`
- `status`
- `input_summary`
- `output_summary`
- `options_emitted_count`
- `source_path`
- `latency_ms`
- `model_id`
- `suppression_reason`
- `error_class`
- `timestamp_utc`

Current required adopters:
- `buttonify`
- `screen_backfill`
- `spoken_backfill`

The contract is emitted for success, fallback, no-op, skipped, and failure paths.

## Observability and Trace Artefacts
- **LLM debug payload**: response, tool invocations, tool stats, context stats, presenter metadata. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L3615-L3845).
- **Response transformation telemetry**: canonical transformation events at `llm_debug.response_transformations`.
- **Turn execution timing breakdown**: `llm_debug.turn_execution_diagnostics.timing_breakdown` includes per-stage elapsed time, per-stage LLM time, and stage/model LLM duration rows for before/after latency comparisons.
- **Workflow traces**: stored as execution traces and surfaced via `aux_llm_calls` when enabled. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L3400-L3465).
- **Tool-use progress**: emits progress updates for the UI “Thinking…” indicator. See [src/backend/server/routes/von_routes.py](src/backend/server/routes/von_routes.py#L603-L705).
- **Model registry snapshot**: summary metadata (source + sample models) attached to `aux_llm_calls` and workflow traces. See [src/backend/integrations/internal_mcp/orchestrator.py](src/backend/integrations/internal_mcp/orchestrator.py#L3668-L3730).

### Lightweight telemetry introspection by turn
`GET /von/history/debug?session_id=<id>&history_index=<n>&view=transformations` now returns only the response-transformation telemetry payload for that turn (`response_transformations` + `transformations_count`), which is useful for sampled quality audits.

## Observed Failure Modes (from recent traces)
1. **Screen backfill over-asserts tool outcomes**
   - Response-based screen backfill can restate unverified actions when only partial tool evidence exists.
2. **Excess caution blocks trivial writes**
   - The model may request manual confirmation for prerequisite checks instead of executing them immediately.
3. **Auto-proceed gate disabled**
   - If `auto_proceed_minimal_imposition_enabled` is disabled, “Proceeding now” style responses can intentionally stop after narration and wait for another user turn.
4. **Workflow selector verdicts are non-binding**
   - Selector metadata does not enforce subsequent stage execution.
5. **Predicate discovery gap**
   - The model may emit invalid predicates (e.g., `has_author`) when no existing predicate ID is surfaced in context.
6. **Annotation results are not used for tool planning**
   - Annotation suggestions exist but are not part of the orchestrator’s decision context.
7. **Presenter tags in fenced code blocks**
   - If the model prints `<spoken>/<screen>` in a code block, it should be treated as explanatory text, not as valid presenter output.
8. **No-tool presenter response degenerates to spoken-only**
   - Without backfill, a presenter response that only provides `<spoken>` would collapse into a screen-only line, losing structured output.

## Known Gaps (Foundations for a Formal Workflow)
- **Screen grounding check**: validate screen claims against tool outputs before final response emission.
- **Complexity analysis**: explicit pre-tool assessment with a stored rationale.
- **Tool policy selection**: explicit budget and policy step that gates tool execution.
- **Ontology discovery pre-flight**: deterministic, read-only candidate discovery for types/predicates before tool selection.
- **Multi-turn context persistence**: short-lived cache of discovered concept IDs across follow-up turns.

## Proposed Workflow: Ordered predicate creation + self-correcting execution
Purpose: ensure **all predicates exist before use**, then execute ontology writes in dependency order, with automatic repair for missing concepts.

### Workflow stages (high level)
1. **Planner**
   - Extract intended assertions and required predicates/types.
   - Build a dependency plan (predicates/types before relationships, salient updates before instance links).

2. **Ontology preflight**
   - Resolve candidate concept IDs (predicate/type) by name.
   - Run `concept_exists` for each required ID.
   - Emit a preflight report with missing/ambiguous IDs.

3. **Create missing predicates (gated)**
   - For each missing predicate, create concept as `instance_of #V#predicate`.
   - Add `hasName` (NL + CODE) and `hasDescription` where available.
   - Re-check existence for all newly created predicates.

4. **Execute ordered writes**
   - Apply salient predicate updates.
   - Create instances.
   - Add relationships using verified predicate IDs.

5. **Verify + summarise**
   - Re-fetch key nodes or re-run `concept_exists` checks.
   - Summarise tool ledger and final state.

### Self-correction loop (per step)
- If a write fails due to **missing predicate/type**, re-enter preflight for the missing ID(s), create them if permitted, then **retry only the failed step**.
- If creation is not permitted or ambiguous, **stop and ask the user** for the exact predicate/type to use.

### Idempotency + safety rules
- All steps must be safe to re-run: creation is gated by `concept_exists`, relationships are added only when predicates are verified.
- Never guess predicate IDs; if a predicate cannot be resolved, halt and request clarification.
- Tool ledger is the authority for the final screen summary.

## Stage-to-Model Policy Mapping (Proposed)
This mapping introduces **per-stage model policies** without changing current behaviour. It documents *where* a workflow model policy should be queried once implemented. For now, the default policy remains **single-model** (the active LLM) for all stages.

### Stage: `planner`
- **When**: Before the tool loop begins, as the assistant plans the action/tool sequence.
- **Where**: `InternalMCPChatOrchestrator.run()` before the first LLM call.

### Stage: `tool_call`
- **When**: Each LLM call that is expected to emit tool calls.
- **Where**: `InternalMCPChatOrchestrator.run()` inside the tool loop.

### Stage: `tool_recovery`
- **When**: Missing-tool-call detection and JSON repair.
- **Where**: `_missing_tool_call_retry_prompt()` / `_infer_missing_tool_call_retry_tool_calls()` in `orchestrator.py`.

### Stage: `classifier`
- **When**: Lightweight classification (missing tool call, screen-vs-spoken quality checks, buttonify candidate extraction).
- **Where**: `orchestrator.py` classifier paths and presenter backfill checks.

### Stage: `critic`
- **When**: Actor–critic checks over tool plans or final summaries (policy compliance, provenance).
- **Where**: Future workflow state after the actor output but before execution/response emission.

### Stage: `screen_backfill`
- **When**: Presenter screen channel is missing, tool-dump-like, or too similar to spoken.
- **Where**: `_screen_looks_like_tool_dump()` / backfill block in `von_routes.py`.

### Stage: `narration`
- **When**: Spoken channel generation (narration workflow).
- **Where**: `CHAT_NARRATION_WORKFLOW_ID` execution in `von_routes.py`.

### Stage: `summariser`
- **When**: Tool-result summarisation and compact response synthesis.
- **Where**: Tool-summary synthesis paths in `von_routes.py`.

### Stage: `buttonify`
- **When**: Structured quick-reply extraction for UI buttonification.
- **Where**: Before frontend transforms in `chatTab.js` (server-side candidate generation).

## Compatibility Note
The initial workflow model policy should preserve **current single-model behaviour** (active LLM for all stages), with optional local-only fallback for low-risk classifier stages. No runtime behaviour changes are required until policy resolution is implemented.

## Glossary
- **Presenter mode**: protocol that outputs `screen` and `spoken` channels for display and TTS.
- **Screen backfill**: second-pass screen synthesis when the model does not provide a valid screen.
- **Orchestrator**: internal MCP loop that runs the LLM and tools.
- **Workflow execution trace**: stored record of workflow execution and status.
