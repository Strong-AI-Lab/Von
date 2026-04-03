# JVNAUTOSCI-1657 Parallel Triage

This note records the outcome of `JVNAUTOSCI-1663`: the parallel portfolio-clarity work needed so `JVNAUTOSCI-1657` can proceed without ambiguity about hardening ownership or stale-epic consolidation.

## Outcome

Two conclusions are now explicit:

1. `JVNAUTOSCI-857` and `JVNAUTOSCI-539` should not be blurred together.
2. The main consolidation candidates (`766`, `1117`, `710`, `739`, `977`, `1116`, `1119`) are mostly review-and-mapping cases, not safe closure cases, because they still retain open child tasks.

## Hardening Split

### `JVNAUTOSCI-539` remains the governance epic

`JVNAUTOSCI-539` is the right home for durable access/governance semantics:

- `JVNAUTOSCI-631` Add role and aspect to user and org
- `JVNAUTOSCI-704` Implement role-based UI visibility with Vontology-driven inheritance
- `JVNAUTOSCI-901` Replace namespace-based access with explicit organisation user role binding concepts
- `JVNAUTOSCI-1036` Implement Von Super-User Administration System with MCP Tool Gating
- `JVNAUTOSCI-1168` Define authoritative effective-namespace contract (already `Done`)

This epic should stay distinct. It is not just generic production hardening. It is the long-lived access-control and governance line.

### `JVNAUTOSCI-857` should be operationally split

`JVNAUTOSCI-857` is too broad to act as a single execution bucket. For planning purposes it should be treated as two tracks.

Fail-closed / privileged-safety work to pull forward:

- `JVNAUTOSCI-1438` Add authentication to admin endpoints
- `JVNAUTOSCI-1437` Implement rate limiting on Flask API endpoints
- `JVNAUTOSCI-871` Visibility / Permission Semantics Are Broken
- `JVNAUTOSCI-638` Secure user-specific and org-specific concept access control modifications
- `JVNAUTOSCI-627` Harden concept scoping relation toggles
- `JVNAUTOSCI-835` Prevent tests or tools from wiping prod `von_db`
- `JVNAUTOSCI-858` admin endpoints and authorisation review
- `JVNAUTOSCI-859` secrets and configuration hygiene
- `JVNAUTOSCI-860` backup policy safety
- `JVNAUTOSCI-861` audit logging for privileged operations

Robustness / performance / operational resilience work that can remain a separate queue:

- `JVNAUTOSCI-811` Handle OpenAI 429 TPM errors gracefully
- `JVNAUTOSCI-659` Handle large numbers of concepts
- `JVNAUTOSCI-357` low-latency caching for `von_db`
- similar non-governance operational tasks still sitting under `857`

The planning rule is:

- use `539` for authoritative governance model work
- use the fail-closed subset of `857` when privileged safety must advance in parallel with execution improvements
- treat the rest of `857` as robustness/performance backlog rather than as a competing governance epic

## Consolidation Mapping

### `JVNAUTOSCI-766` versus `JVNAUTOSCI-833`

`766` should be reinterpreted as an early formulation of KB-authored routing/selector behaviour now better expressed under `JVNAUTOSCI-833`.

However, `766` is not ready for closure. It still has open child tasks including:

- `767` rule schema
- `768` Vontology concept structure for heuristics
- `769` rule loader service
- `770` orchestrator integration
- `771` admin UI
- `772` MCP tools
- `773` testing and validation
- `774` documentation and migration
- `845` persisted artefacts / virtual Vontology view

So the correct action is:

- keep `833` as the active home for selector/routing capability
- review each `766` child task individually for re-parenting, supersession, or closure as obsolete
- do not close `766` on the basis of a broad “already absorbed” statement

### `JVNAUTOSCI-1117` into `JVNAUTOSCI-537` and `JVNAUTOSCI-866`

`1117` is directionally aligned with the active UI epics, especially:

- `JVNAUTOSCI-537` Von Frontend UI / UX & Interaction Improvements
- `JVNAUTOSCI-866` Beautiful, flexible UX for a knowledge-using and -creating AI system

But `1117` still has open child tasks:

- `1124` Empty State & Onboarding
- `1125` Chat Progressive Disclosure
- `1126` Visual Polish & Design Tokens
- `1127` Settings Page Simplification

So the correct action is:

- treat `537` and `866` as the active execution homes
- explicitly map `1124` to `1127` into those epics or close them individually as obsolete
- do not close `1117` until that child-task mapping is recorded

### `JVNAUTOSCI-710`

`710` is not ready for closure. It still has at least one open child task:

- `725` Repository Cleanup - Phase 5: Re-documentation

This means `710` should be treated as review-for-closure only. It can be closed later only after its remaining child work is either completed or explicitly superseded.

### `JVNAUTOSCI-739`

`739` is also not ready for closure. It still has open child tasks including:

- `740` Login with Google feature does not work
- `749` cascading deletion cleanup

These are substantive residual tasks, not historical bookkeeping. `739` should remain a review/mapping case until those items are explicitly moved, superseded, or completed.

### `JVNAUTOSCI-977` versus `JVNAUTOSCI-1460`

`1460` is the broader and more current epic for external interaction surfaces and shared gateway/runtime work.

Future new channel/device/browser surface work should default there, not under `977`.

But `977` is not yet safely superseded, because it still retains open child tasks including:

- `121` congratulatory messages/posts
- `248` Google Tasks MCP integration
- `793` harden Gmail/GSuite/Slack MCP access
- `802` Gmail sending capability
- `805` calendar access
- `979` role-based access to Slack teams

So the correct action is:

- treat `1460` as the strategic successor epic
- review `977` child tasks one by one and either move them under `1460` / `539`, or close them as obsolete
- avoid blanket closure language until that work is done

### `JVNAUTOSCI-1116` and `JVNAUTOSCI-1119`

These were previously easy to overstate as “probably absorbed”, but they are not close-ready.

`1116` still has open child tasks:

- `235` Generalize LLM Integration Architecture with ModelManager Pattern
- `1120` Deconstruct Frontend Monoliths
- `1121` Refactor Backend Route Layers
- `1122` Consolidate Project Utilities
- `1123` Repository Housekeeping

`1119` still has open child tasks:

- `1132` Strict Error Handling Reform
- `1133` Type Safety Initiative
- `1134` CSS Monolith Splitting
- `1135` Global State Refactoring

So these should be treated as review-for-consolidation umbrellas, not as epics that can already be declared finished or absorbed.

## Portfolio Rule Going Forward

When `JVNAUTOSCI-1657` or later planning work makes a consolidation claim, that claim should name:

- the source epic/task
- the active destination epic/task
- whether the source item is being re-parented, superseded, narrowed, or closed as obsolete
- whether unresolved child tasks still remain

If unresolved child tasks remain, the source epic should usually stay open until those children are explicitly dealt with.

## Immediate Effect on `JVNAUTOSCI-1657`

`JVNAUTOSCI-1657` can now proceed with a clearer portfolio model:

- Phase 2 / later execution should treat `539` as governance-authority work.
- Parallel hardening should pull forward the fail-closed subset of `857`.
- Broader robustness/performance tasks under `857` can remain queued separately.
- Consolidation candidates should be handled as explicit mapping exercises, not as optimistic closures.
