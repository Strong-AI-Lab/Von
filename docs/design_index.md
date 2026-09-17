# Von Design and Engineering Document Index

- **Kind:** Documentation-governance index
- **Lifecycle:** Active
- **Authority:** Canonical navigation and classification only; it does not
  override current user direction, `AGENTS.md`, live represented authority, or
  live evidence
- **Owner:** Von maintainers
- **Last reviewed:** 17 September 2026
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

Maintained public manuals, contracts, runbooks and governing design guidance
belong in this repository. Exploratory proposals, architecture reviews, dated
audits, working notes and implementation/experiment records belong in the
private Von research repository. Public instructions must remain self-contained;
private notes do not acquire implementation authority through relocation.

Historical citations may use commit-pinned public links. These preserve the
previously published evidence without making private access a prerequisite.

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
| Jira-to-Von task authority and migration recovery | [Jira-to-Von migration guide](engineering/workflow_first_jira_to_von_migration_jvnautosci_1223.md); the current task-project writer controls task updates |
| Build/deployment receipts and task work products | [Build and deployment evidence](engineering/deployment_evidence.md) |
| Task product selection and continuing a responsibility from Tasks | [Task responsibility continuation](engineering/task_responsibility_continuation.md) |
| Subscription-backed coding task pickup on the DGX | [Codex DGX worker pilot](engineering/codex_dgx_worker.md) |
| Personal/team authority to assign coding agents | Draft [coding-agent assignment authority](engineering/coding_agent_assignment_authority_design.md); proposed only, no live grant or enforcement authority |
| Coding-agent progress and completion messages in Von | [Interactive coding-agent messages](engineering/codex_dgx_worker.md#interactive-coding-agent-messages); operator-specific delivery configuration in personal `AGENTS.md` |
| Enduring role competence, organisational continuity, active acquisition, learned reuse or transfer | [Role-learning convergence](engineering/role_learning_convergence.md), with the [staged development rehearsal](engineering/role_convergence_rehearsal.md); live Jira owns delivery status and the next integrating increment |
| Material security exposure: authentication/authorisation, private or cross-namespace data, secrets, untrusted content with tool authority, effects outside ordinary bounded and recoverable delegation, deployment, or administrator surfaces | [Security considerations](engineering/security_considerations.md) |
| Substantial agent-behaviour implementation | Relevant sections of the [modern agentic AI primer](engineering/intro_to_modern_agentic_ai_for_coding_agents.md) |
| Workflow or orchestration | Relevant vocabulary/semantics in the [VWL manual](engineering/von_workflow_language_manual.md); domain examples and appendices are reference material |
| Prompts, models, routing, optimisation, or fine-tuning | [prompt programmes and model routing](engineering/prompt_programs_and_model_routing_playbook.md) |
| Retrieval, memory, RAG, KB growth, or long-horizon state | [agent memory and enduring knowledge](engineering/agent_memory_and_enduring_knowledge.md) |
| Semantic task search, task embeddings and task RAG context | [Task semantic retrieval](engineering/task_semantic_retrieval.md) |
| Private Otter archive access, conversation images or research-slide reading | [Private Otter archive and conversation images](engineering/otter_archive_and_conversation_images.md) |
| Authenticated Figma access through coding agents; native-backend prerequisites | [Figma authenticated access](engineering/figma_authenticated_access.md) |
| Conversational diagrams, equations, generated/tool images or visual format extension | [Conversational visual output](engineering/conversational_visual_output.md) |
| Reusing external ontology concepts with source identity and attribution | [KnowKat ontology sources and governed adoption](engineering/knowkat_ontology_sources.md) |
| Experience- or discussion-derived learning | [Role-learning convergence](engineering/role_learning_convergence.md), [memory and enduring knowledge](engineering/agent_memory_and_enduring_knowledge.md), and [evaluation and research uptake](engineering/agent_evaluation_and_research_uptake.md). The current authoritative task surface owns experimental selection and delivery; private proposals are optional background, not public implementation prerequisites. |
| Importing external human/agent transcripts into conversation carriers | [External conversation import](engineering/external_conversation_import.md) |
| Unified conversation discovery, message exchanges, read state or participant avatars | [Unified conversations and participant profiles](engineering/unified_conversations_and_participant_profiles.md) |
| Execution-summary cost display and user/organisation thresholds | [Execution cost display](engineering/execution_cost_display.md) |
| Durable assertions, text/logical assertion typing, propositions, context-sensitive retrieval, hypotheses, publication or promotion, or user-, organisation-, project-, source-, theory-, or time-relative knowledge | [Assertion and propositional-sentence ontology](engineering/assertion_and_propositional_sentence_ontology.md), then [contextual knowledge evolution](engineering/contextual_knowledge_evolution.md); for canonical ontology publication or scope change, the active [ontology publication authority boundary](engineering/ontology_publication_authority.md) |
| Evaluation, benchmarks, or research-sensitive architecture | [agent evaluation and research uptake](engineering/agent_evaluation_and_research_uptake.md) |
| Minimal imposition, elicitation, or write policy | [minimal-imposition principle](engineering/minimal_imposition_design_principle.md) |
| Frontend or browser acceptance | [Frontend browser validation](engineering/frontend_browser_user_view_validation.md), at the validation tier justified by the claim |
| Steering an active chat turn versus queueing a follow-up | [Chat steering and queueing](engineering/chat_steering_and_queueing.md) |
| Dictation, audio transcription or mobile speech playback | [Speech I/O](engineering/speech_io.md); current conversational delivery plan remains in its linked Jira issue |
| Live user-visible behaviour or telemetry diagnosis | Applicable sections of the [real-path replay and telemetry loop](engineering/real_path_server_replay_and_telemetry_loop.md), using the risk tier in `AGENTS.md` |
| Substantial Jira task definition or pull-request merge boundary | The decision discipline in [`AGENTS.md`](../AGENTS.md#5-capability-slice-planning), the practical [Jira guidance](engineering/operational_engineering_guide.md#53-jira) and [merge-decision procedure](engineering/operational_engineering_guide.md#62-make-new-evidence-change-a-decision), and the [pull-request template](../.github/PULL_REQUEST_TEMPLATE.md) |
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
| [OpenRouter provider deployment](engineering/openrouter_provider_deployment.md) | Active bounded operator note | Operational reference for the opt-in OpenRouter transport; scoped settings and represented model policy remain authoritative |
| [Agent memory and enduring knowledge](engineering/agent_memory_and_enduring_knowledge.md) | Active design guide | Normative only within its stated memory/retrieval scope |
| [Role-learning convergence](engineering/role_learning_convergence.md) | Active programme design and guide | Canonical programme direction under `AGENTS.md` for claims about enduring role competence; no runtime activation, universal per-PR gate or demonstrated role capability is implied |
| [Knowledge federation pilot](engineering/knowledge_federation_pilot.md) | Bounded implementation and authority contract | JVNAUTOSCI-2730/2731: automatic scoped discovery, conversation recall and on-demand private artefact copies; opt-in complete-slice exchange between independent stores; source audiences, atomic receipt/read projection, expiry, recovery and later SC448086 admission. Not general database replication or execution federation. |
| [Task-project home transfer](engineering/task_project_home_transfer.md) | Bounded task-project operations and recovery | Signed project snapshots, preserved audiences and archives, explicit Atlas-to-DGX write-home handoff, current-actor dispatch and interruption recovery. |
| [Contextual knowledge evolution](engineering/contextual_knowledge_evolution.md) | Active design guide | Advisory under `AGENTS.md` when assertion/context distinctions are material; it does not select a microtheory formalism or prove present implementation |
| [Assertion and propositional-sentence ontology](engineering/assertion_and_propositional_sentence_ontology.md) | Active design and partial implementation boundary | Canonical model and phased integration plan; the minimal public vocabulary is live, while assertion-record typing, context semantics, general logical storage, and reasoning remain future work |
| [External conversation import](engineering/external_conversation_import.md) | Active design and implementation boundary | Canonical reference for source-neutral transcript packages, actor-scoped read-only projections, provider adapters, append-only resynchronisation, durable machine-wide batches, and native continuation; verify supported provider export shapes against current fixtures and live evidence |
| [Ontology publication authority](engineering/ontology_publication_authority.md) | Active design/implementation boundary | Operational reference for `JVNAUTOSCI-2632` and represented publication-profile semantics from `JVNAUTOSCI-2671`; distinguish semantic advice and publication from visibility and operational administration, and revalidate the current runtime, profiles, and represented roles before relying on deployment-specific claims |
| [Agent evaluation and research uptake](engineering/agent_evaluation_and_research_uptake.md) | Active protocol/design guide | Normative only for the evaluation or research claim being made |
| [Minimal-imposition principle](engineering/minimal_imposition_design_principle.md) | Active principle | Explanatory guidance under `AGENTS.md`; it adds no independent per-task gates |
| [Frontend browser validation](engineering/frontend_browser_user_view_validation.md) | Active practical guide | Risk-tiered reference; browser evidence needs a dated locator when used |
| [Real-path replay and telemetry loop](engineering/real_path_server_replay_and_telemetry_loop.md) | Active practical guide | Risk-tiered protocol; exact-path evidence outranks prose, but full protocol is not universal |
| [Operational engineering guide](engineering/operational_engineering_guide.md) | Active companion runbook | Consult by operational need and revalidate host/tool-specific details |
| [Durable task ownership](engineering/durable_task_ownership.md) | Active implementation and rollout contract | Task exclusivity, lease fencing, uncertain-effect recovery and legacy-worker database admission; verify runtime activation separately from merge |
| [Atlas egress reconciler](engineering/atlas_egress_reconciler_runbook.md) | Active bounded operational runbook | Use for the standalone mobile-client/DGX database access-list reconciler; revalidate Atlas API policy and credentials live |
| [Authority-alignment scan](engineering/maintaining_global_design_constraints_and_authority_alignment_with_coding_agents.md) | Active companion guide | Advisory structural review procedure under `AGENTS.md` |

## 6. Working research and historical evidence

The 5 September 2026 working-note archive was reconciled with public main on
17 September 2026. Sixty-eight tracked archived documents moved out of the
public working tree; the active Jira-to-Von migration guide remains public.
Von-Private's `research/design_notes/` collection groups proposals, reviews and dated
records, with a source-path and content-hash migration manifest. The private
repository is optional research context, not a dependency of public development.

For previously published working records, the
[pre-move public document index](https://github.com/Strong-AI-Lab/Von/blob/5b2f8b976069935da73b82bca433fb3cefffcd3d/docs/design_index.md)
provides historical navigation. Its statements retain their original evidence
boundaries and do not govern current work. Moving a file does not remove its
previously public Git history.

Remaining public references (retain each document's stated authority):

| Document | Classification | Correct use |
|---|---|---|
| [Proactive clarification and conversational role learning](engineering/proactive_clarification_and_role_learning.md) | Draft, source-grounded design proposal | Use for material referent questions, alias/role learning, short-reply continuation, recovery and staged evaluation. It builds on existing ordinary-turn support; no runtime behaviour, represented fact or policy activation is established. |
| [Conversational rumination](engineering/conversational_rumination.md) | Capability and bounded acceptance record | Optional model-selected background inquiries, independent task execution and later actor-scoped product consumption; no deployment or broad role-competence claim. |
| [Ontology repair plans](engineering/ontology_repair_plan_design.md) | Design with implemented substrate | Use for the plan/approve/execute shape and its affordance argument; it adds no authority route, and agent-reachable ontology mutation remains an open decision |
| [Coding-agent assignment authority](engineering/coding_agent_assignment_authority_design.md) | Draft / proposed; source audit refreshed 17 September 2026 | Extends existing dispatch provenance with scoped personal/team grants; complete inventory and refreshed private handoff remain separate. No implementation or activation authority |

Current guidance remains listed above, including maintained implementation
references whose filenames contain “design” or “notes”. Classification follows
the document's function, not its filename. Active rehearsal and test protocols
remain public when they support reproducible implementation work.

Generated tooling documentation and reusable fixtures may remain under
[`docs/generated/`](generated/). Dated research inventories and local evidence
belong with their working notes, subject to the destination's data-handling
rules. Raw Jira, conversation and telemetry payloads must not enter Git merely
because the destination repository is private.

- [Mobile Web Push candidate and operator handoff](engineering/mobile_web_push.md)
  records the bounded JVNAUTOSCI-2757 implementation and unfulfilled browser
  transport acceptance; it does not establish a deployed notification service.

## 7. Metadata for new and substantially revised documents

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

Unlisted documents remain supporting material whose authority and factual
claims require independent verification.
