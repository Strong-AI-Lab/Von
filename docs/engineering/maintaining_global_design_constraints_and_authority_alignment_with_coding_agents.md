# Maintaining Global Design Constraints and Authority Alignment

- **Kind:** Routed architecture-review guidance
- **Lifecycle:** Active
- **Authority:** Advisory method under the constitutional rules in `AGENTS.md`
- **Last substantive content update:** 2026-07-25
- **Last reviewed:** 2026-07-25

## 1. Purpose

Use this guide for a bounded structural review after substantial
workflow/orchestration work, or when repeated patching suggests that code shape
is impairing delivery.

The goal is not architectural purity and not a backlog full of vague refactor
tasks. The goal is to improve the next user-facing capability slice by removing
a demonstrated source of change risk, authority confusion, poor observability,
or excessive validation burden.

## 2. Start with Capability Evidence

Before scanning adjacent code, write down:

- the capability or user outcome that exposed the problem;
- the concrete failure, maintenance cost, or evidence gap;
- the smallest architectural boundary involved;
- the present validation tier and nearest faithful test path.

Do not begin with file size, a preferred pattern, or the assumption that more
representation is inherently better. A deterministic implementation can be the
right design for deterministic semantics. A represented workflow or prompt is
the right authority when behaviour must remain inspectable, adaptable,
governable, or learnable.

## 3. When a Scan Is Worthwhile

Perform a bounded scan when one or more of these is true:

- several recent fixes landed in the same tangled function or boundary;
- a support layer can silently distort user-visible meaning;
- coordination or decision policy is accumulating in Python;
- responsibility mixing makes the real path hard to test;
- integration-boundary sprawl makes a small capability change expensive;
- stage context is changed invisibly or cannot be reconstructed from telemetry;
- the change exposed duplicate or incompatible sources of authority.

Usually skip the scan for a small isolated Tier 0/1 change with a clear seam and
no evidence of adjacent structural risk.

## 4. What Makes a Strong Candidate

A strong refactor candidate normally combines at least two of:

- **Repeated patch pressure:** local conditionals or helpers keep accumulating.
- **Mixed responsibilities:** boundary parsing, policy, execution, persistence,
  telemetry, and response shaping are entangled.
- **Authority drift:** code decides adaptable routing, ranking, recovery, or
  user-facing semantics that a represented artefact should govern.
- **User-visible interpretation risk:** support code can alter meaning without
  an authored policy or visible lineage.
- **Weak test seams:** only a large end-to-end setup can exercise a small
  invariant.
- **Integration sprawl:** registries, factories, routes, or engines collect
  unrelated capability families.
- **Operational burden:** the shape materially increases latency, cost,
  brittleness, or acceptance effort.

Size alone is not evidence. Old code, unfamiliar style, or a large line count
does not justify a refactor.

## 5. Diagnose the authority boundary

Identify the user outcome, the actual selected authority or execution surface,
the maximum capability available, the decisions that benefit from model
judgement, and any reusable mechanism the capability genuinely needs. Use the
constitutional default in `AGENTS.md`; do not create a local risk taxonomy or
repeat a generic control checklist here.

If a proposed refactor adds a compulsory restriction, stage, or wrapper, apply
the evidential burden in `AGENTS.md` and compare it with the simplest permissive
baseline on the complete user job. Otherwise, no safety dossier is required.
Refactoring should clarify authority without moving hidden policy into smaller
functions, freezing a probabilistic label behind code, or forcing an exact
mechanism through an LLM/workflow layer.

## 6. Common Drift Patterns

### 6.1 One more local case

A nearby legacy table or handler makes a small new branch look cheap. Treat the
existing table as possible debt, not precedent. Ask which artefact should own
the new behaviour and whether the support layer only needs a generic input,
primitive, or telemetry field.

### 6.2 Domain-name pull

A prompt or Jira title names a paper, person, source, or business object, and
the implementation becomes domain-specific even though the failure is generic.
Record:

- the domain symptom;
- the generic failure class;
- the intended authority surface;
- the support-only code scope.

A proper noun in generic orchestration is a warning sign, not proof of a defect.

### 6.3 Handler-first repair

If the first substantive edit for workflow-authoritative behaviour is in a
domain Python handler, pause. Determine whether:

- the workflow/prompt lacks an input, transition, or guidance;
- a canonical tool is not exposed;
- the runtime lacks a small reusable primitive;
- the direct handler test is bypassing the actual failing path.

Materialise the represented behaviour first when possible. Add only the reusable
mechanism proved missing.

### 6.4 Invisible context shaping

Different stages often need different views of canonical turn state. That is
not drift by itself. Drift occurs when inclusion, omission, retrieval, or
compaction is implicit, unevaluated, or absent from telemetry. Fix the lineage
and projection contract rather than requiring every stage to receive identical
context.

### 6.5 Tests pinning the wrong architecture

A green test can preserve a bypass or hidden policy just as effectively as a
correct invariant. Check that the test enters through the authority boundary
whose behaviour is being claimed.

## 7. Bounded Scan Method

1. Limit the scan to touched and directly adjacent surfaces.
2. Inspect recent patch pressure and responsibility boundaries, not just line
   counts.
3. Identify the capability cost and authority risk.
4. Choose the smallest useful response:
   - no action;
   - clarify a comment or contract;
   - extract a reusable support seam in the current task;
   - remove obsolete policy or duplication;
   - create a linked follow-up task when the change is substantial or outside
     current scope.
5. State the validation that would prove behaviour and authority were
   preserved.

Do not create follow-up Jira work automatically. Create it when the evidence is
strong, the problem is materially consequential, and it cannot be handled
safely within the authorised scope.

## 8. Writing a Useful Refactor Task

A useful task names:

- the affected capability and current failure/cost;
- exact files, functions, or boundaries;
- responsibilities that should separate;
- the delegated-capability, assurance, mechanism, represented, and ephemeral
  judgement split;
- compatibility constraints;
- a targeted and nearest-faithful validation plan;
- what will become simpler, faster, safer, or easier to change;
- what safe actions, strategies, or recovery options a proposed compulsory
  control would block.

Avoid titles such as “clean up large file”. Prefer an outcome such as “separate
HTTP parsing from turn execution so route variants share one tested execution
boundary”.

## 9. Mechanical Guardrails

Use a static or contract check when a high-risk drift pattern is precise enough
to detect with low false positives. Current workflow-purity evidence may include
checks for Python-authored prompt sources or support-surface policy contracts.

Keep guardrails:

- structural or symbol-aware where possible;
- limited to hard repository constraints;
- free of domain success policy;
- paired with a clear remediation path.

Do not turn an adaptive design preference into a build-breaking keyword scan.
Mechanical checks supplement evidence and review; they do not prove authority
alignment.

## 10. Success Standard

A successful review produces either:

- no action, with a clear reason the current shape is adequate; or
- a small number of evidence-backed changes/tasks that reduce a demonstrated
  capability or maintenance burden.

The result should make the next end-to-end capability easier to deliver and
validate. If it adds ceremony, representation layers, or policy machinery
without improving that outcome, it has missed the point.
