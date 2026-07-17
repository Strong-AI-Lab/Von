# Von Design and Engineering Document Index

- **Kind:** Documentation-governance index
- **Lifecycle:** Active
- **Authority:** Canonical navigation and classification only; it does not
  override current user direction, `AGENTS.md`, live represented authority, or
  live evidence
- **Owner:** Von maintainers
- **Last reviewed:** 18 July 2026
- **Review trigger:** Any change to `AGENTS.md` reading routes, canonical
  document selection, or document supersession
- **Scope:** Tracked design, engineering, operational, review, and generated
  documentation in this repository

## 1. Why this index exists

Von's documentation records several years of design, implementation, incidents,
Jira milestones, and research thinking. Those records are useful, but they do
not all describe the same system or carry the same authority. A confident old
status report is not a current implementation guide, and a live defect is not a
new design principle.

Coding agents should use this index before treating an engineering note as
current authority when the task is substantial, architecture-sensitive,
documentation-sensitive, or depends on an older design claim. In particular:

- read [`AGENTS.md`](../AGENTS.md), then progressively disclose only the
  documents and sections relevant to the task;
- determine what kind of claim the document is making;
- check its lifecycle, authority scope, and evidence date separately;
- verify present behaviour on the live authority and evidence surfaces; and
- treat an unlisted engineering note as supporting material, not as current
  project-wide authority.

The directory remains mostly flat for now so existing links and external
bookmarks continue to work. Status banners and this virtual organisation are
the first migration step; files can move into archival folders later, with
compatibility stubs, when that produces more value than link churn.

## 2. Do not compress document status into one label

Interpret these four dimensions independently:

| Dimension | Question | Typical values |
|---|---|---|
| **Kind** | What is this document? | constitution, principle, specification, manual, protocol, runbook, programme design, proposal, snapshot, audit, implementation record |
| **Lifecycle** | Is the document itself active? | draft, active, frozen, superseded, retired |
| **Authority** | What, if anything, may it prescribe? | governing, normative in scope, canonical reference, advisory, evidence only, historical only |
| **Evidence basis** | How well does it establish a factual claim? | live validated, observed, reported, inferred, proposed, aspirational, contradicted, unknown |

Words such as “complete”, “secure”, “implemented”, and “production ready” are
claims about a system or a bounded milestone, not document lifecycle values.
They require a scope, evidence locator, and date.

## 3. Authority and evidence order

The right precedence depends on the question.

| Question | Use this order |
|---|---|
| What is Von intended to become? | Current explicit user direction → `AGENTS.md` → applicable active guidance selected by `AGENTS.md` → accepted decision records → advisory programme designs and proposals |
| What policy, prompt, workflow, or knowledge is operationally represented? | Live canonical Vontology artefact → named published release/export → repository seed or fixture only when explicitly designated |
| What does Von actually do now? | Exact user-visible path and world-state read-back → persisted telemetry and receipts → live artefact inspection → current code and targeted tests → dated reports → prose assertions |
| What work is planned or complete? | Current user decision and live Jira → current git/CI evidence → dated programme notes and milestone reports |
| Why was a design decision made? | Accepted decision record → linked Jira decision → active design proposal → historical discussion |

A security or correctness failure is evidence that the implementation violates a
requirement; it does not supersede the requirement. Conversely, a document's
desired architecture does not prove the implementation currently behaves that
way.

## 4. Start here

`AGENTS.md` contains the governing invariants. This index routes deeper reading;
it does not make every line of a canonical document compulsory for every task.
Use the smallest relevant reading set and verify changing operational facts
live.

| Work | Required or primary documents |
|---|---|
| Every task | [`AGENTS.md`](../AGENTS.md) |
| Substantial, architecture-sensitive, or older-doc-dependent work | This index, then the applicable sections below |
| Authentication, private/cross-namespace data, writes, untrusted content, integrations, deployment, or administrator surfaces | [Security considerations](engineering/security_considerations.md) |
| Substantial agent-behaviour implementation | Relevant sections of the [modern agentic AI primer](engineering/intro_to_modern_agentic_ai_for_coding_agents.md) |
| Workflow or orchestration | Relevant vocabulary/semantics in the [VWL manual](engineering/von_workflow_language_manual.md); domain examples and appendices are reference material |
| Prompts, models, routing, optimisation, or fine-tuning | [prompt programmes and model routing](engineering/prompt_programs_and_model_routing_playbook.md) |
| Retrieval, memory, RAG, KB growth, or long-horizon state | [agent memory and enduring knowledge](engineering/agent_memory_and_enduring_knowledge.md) |
| Evaluation, benchmarks, or research-sensitive architecture | [agent evaluation and research uptake](engineering/agent_evaluation_and_research_uptake.md) |
| Minimal imposition, elicitation, or write policy | [minimal-imposition principle](engineering/minimal_imposition_design_principle.md) |
| Frontend or browser acceptance | [Frontend browser validation](engineering/frontend_browser_user_view_validation.md), at the validation tier justified by the claim |
| Live user-visible behaviour or telemetry diagnosis | Applicable sections of the [real-path replay and telemetry loop](engineering/real_path_server_replay_and_telemetry_loop.md), using the risk tier in `AGENTS.md` |
| Practical repository operation | [operational engineering guide](engineering/operational_engineering_guide.md), [authority-alignment scan](engineering/maintaining_global_design_constraints_and_authority_alignment_with_coding_agents.md) |

## 5. Current governing and routed guidance

`AGENTS.md` is governing. The documents below are current routed references in
their stated scope. Their current status depends on maintainer review,
implementation evidence, and freshness metadata—not circularly on being linked
from `AGENTS.md`.

| Document | Kind and lifecycle | Authority and freshness boundary |
|---|---|---|
| [`AGENTS.md`](../AGENTS.md) | Active constitution | Governing repository instructions, subordinate to current explicit user direction and higher-level safety rules |
| [Security considerations](engineering/security_considerations.md) | Active security guide with dated posture observations | Required for security-sensitive scopes; verify deployment-profile and implementation claims live |
| [Modern agentic AI primer](engineering/intro_to_modern_agentic_ai_for_coding_agents.md) | Active principle/background guide | Routed background for substantial agent-behaviour planning; not a current-status report |
| [VWL manual](engineering/von_workflow_language_manual.md) | Active manual | Canonical reference for VWL vocabulary and runtime interfaces; consult relevant sections, and verify live artefacts/behaviour |
| [Prompt programmes and model routing](engineering/prompt_programs_and_model_routing_playbook.md) | Active playbook | Normative only within its stated prompt/model scope |
| [Agent memory and enduring knowledge](engineering/agent_memory_and_enduring_knowledge.md) | Active design guide | Normative only within its stated memory/retrieval scope |
| [Agent evaluation and research uptake](engineering/agent_evaluation_and_research_uptake.md) | Active protocol/design guide | Normative only for the evaluation or research claim being made |
| [Minimal-imposition principle](engineering/minimal_imposition_design_principle.md) | Active principle | Normative within its stated scope, subject to user direction, security, and authority boundaries |
| [Frontend browser validation](engineering/frontend_browser_user_view_validation.md) | Active practical guide | Risk-tiered reference; browser evidence needs a dated locator when used |
| [Real-path replay and telemetry loop](engineering/real_path_server_replay_and_telemetry_loop.md) | Active practical guide | Risk-tiered protocol; exact-path evidence outranks prose, but full protocol is not universal |
| [Operational engineering guide](engineering/operational_engineering_guide.md) | Active companion runbook | Consult by operational need and revalidate host/tool-specific details |
| [Authority-alignment scan](engineering/maintaining_global_design_constraints_and_authority_alignment_with_coding_agents.md) | Active companion guide | Advisory structural review procedure under `AGENTS.md` |

## 6. Programme direction and current synthesis

No formal Architecture Decision Record corpus existed at this review. Do not
mistake a design proposal or Jira implementation summary for an accepted ADR.
Until an ADR process is established, acceptance must be grounded in current
human direction, `AGENTS.md`, and the relevant live Jira decision record. A
design proposal or private synthesis becomes governing only after explicit
human acceptance and promotion into a current public authority surface.

| Document | Classification | Correct use |
|---|---|---|
| [Von for Agentic AI](engineering/Von_for_AgenticAI.md) | Advisory long-horizon programme design with an April 2026 state snapshot | Use the durable target direction; revalidate every present-state, file-size, gap, and priority claim |
| Private research syntheses | Advisory material retained outside the public repository | Public coding agents must not depend on private notes; promote approved decisions into the applicable public canonical guide |
| [Automated policy learning](engineering/automated_policy_learning_design.md) | Design with partial substrate | Use as a proposed learning architecture, not proof of a closed operational loop |
| [Testing workflows and ephemeral theories](engineering/testing_workflows_ephemeral_theories_design.md) | Research/design proposal with partial substrate | Use for design intent and explicit hypotheses; verify implemented surfaces |
| [Multi-agent coordination](engineering/multi_agent_coordination_design.md) | Early design proposal | Use as a direction to evaluate, not implemented architecture |
| [Vontology tooling from KA/KR literature](engineering/vontology_tooling_from_ka_kcap_kr_literature.md) | Research-backed advisory roadmap | Use for alternatives and research uptake, not present capability claims |

## 7. Dated snapshots, diagnostics, and proposals

These files retain evidence and rationale, but must not drive current task
ordering or implementation without live revalidation:

- [Agentic architecture status — 23 April 2026](engineering/agentic_architecture_status_2026-04-23.md)
- [Recent architecture progress — 21 April 2026](engineering/recent_architecture_progress_2026-04-20.md)
- [Enduring-memory architecture — 23 April 2026](engineering/jvnautosci_1962_enduring_memory_architecture_2026-04-23.md)
- [Self-improvement worlds architecture — 23 April 2026](engineering/jvnautosci_1963_self_improvement_worlds_architecture_2026-04-23.md)
- [Evaluator architecture — 23 April 2026](engineering/jvnautosci_1964_evaluator_architecture_2026-04-23.md)
- [Role-learning review — 24 April 2026](engineering/jvnautosci_2011_role_learning_review_2026-04-24.md)
- [Workflow execution analysis — 7 April 2026](engineering/workflow_execution_architecture_analysis.md)
- [Chat-turn control-plane analysis — 7 April 2026](engineering/chat_turn_workflow_control_plane_analysis.md)
- [Comparative arXiv workflow analysis — 7 April 2026](engineering/arxiv_paper_workflow_analysis.md)
- [Workflow self-authoring gap analysis — 5 April 2026](engineering/workflow_self_authoring_gap_analysis.md)
- [Workflow system audit — 7 February 2026](engineering/workflow_audit_2026-02-07.md)
- [VWL implementation audit — 24 March 2026](engineering/vwl_implementation_audit_2026-03-24.md)
- [MCP reliability incident reflection](engineering/mcp_reliability_analysis.md)
- dated `jvnautosci_*` reviews, audits, designs, and implementation summaries,
  unless a current guide above explicitly adopts their conclusions.

## 8. Historical and generated material

- [`intro_to_modern_agentic_ai_for_coding_agents.suggested.md`](engineering/intro_to_modern_agentic_ai_for_coding_agents.suggested.md)
  is a superseded, unselected proposal. It is not the guide required by
  `AGENTS.md`.
- [`AINotes.md`](AINotes.md) is a frozen tactical snapshot, not current task or
  repository status. Use live git and Jira instead.
- [Historical agent guidance](engineering/historical_agent_guidance_notes.md)
  is an archive and already labels itself accordingly.
- The `JVNAUTOSCI-799` document family records a bounded 2025 milestone. Its
  completion, coverage, and readiness claims are not current system-level
  evidence.
- Files under [`docs/generated/`](generated/) are generated evidence snapshots.
  Read their timestamps and inputs; never treat a generated report as standing
  design authority.
- A date in a filename is an evidence boundary, not a freshness guarantee.

## 9. Metadata for new and substantially revised documents

Use this compact header, adapting fields to the document:

```markdown
**Kind:** manual | runbook | design | proposal | snapshot | audit | record
**Lifecycle:** draft | active | frozen | superseded | retired
**Authority:** governing | normative in scope | canonical reference | advisory |
evidence only | historical only
**Authority scope:** ...
**Owner:** named person or team | unassigned
**Last reviewed:** YYYY-MM-DD
**Review due or trigger:** YYYY-MM-DD or named change trigger
**State or evidence as of:** YYYY-MM-DD or not applicable
**Supersedes / superseded by:** ...
**Live authority or implementation evidence:** ...
**Open questions:** ...
```

Maintenance rules:

1. Only current human direction, `AGENTS.md`, or an explicitly authorised
   decision may elevate a document to governing or normative status.
2. Keep one active canonical document per subject. Update it instead of adding
   a near-duplicate “suggested”, “final”, or “new” guide.
3. Separate target-state design from current-state evidence. Date the latter.
4. Freeze audits and snapshots. Publish a new observation rather than silently
   rewriting the old evidence base.
5. Supersession is two-way: banner and replacement link on the old document,
   backlink from the replacement where useful, and an index update.
6. Do not use unqualified “current”, “complete”, “secure”, “canonical”, or
   “production ready”. State the scope, date, and evidence.
7. Repository descriptions of Vontology-governed workflows, prompts, policies,
   or knowledge must say whether they are manuals, exports, seeds, fixtures, or
   live authority.
8. Re-check links, code anchors, live Jira, and represented artefacts whenever a
   substantial task relies on an older document.

This first pass classifies the authority-bearing and highest-risk material. It
does not pretend that all 104 pre-existing engineering files have already
received complete metadata. Until they do, unlisted documents remain supporting
material whose claims require independent verification.
