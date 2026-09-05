# Convergence towards learning organisational roles

- **Kind:** Programme design and engineering guide
- **Lifecycle:** Active
- **Authority:** Canonical programme design under `AGENTS.md`; applies to work
  claiming progress towards enduring role competence, not every repository task
- **Owner:** Von maintainers; live delivery ownership is recorded in
  [JVNAUTOSCI-2011](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2011)
- **Last reviewed:** 4 September 2026
- **Evidence basis:** Public Von `edf6f2b7be67610a760fed7e0ecb4d9704d81e77`
  and selected live Jira reads on 4 September 2026; no new runtime experiment
- **Review trigger:** A pilot cycle finishes, a material premise changes,
  transfer is attempted, or programme work stops producing role evidence
- **Supersedes:** The planning recommendations of the
  [April role-learning review](jvnautosci_2011_role_learning_review_2026-04-24.md).
  Its historical evidence remains intact.

## 1. Direction and meaning of convergence

Von should increasingly carry useful responsibilities for people, teams and
organisations while reducing the work needed to supervise it. Research-lab,
research-programme, research-process and funding-process management provide
initial environments; the aim includes similarly complex roles elsewhere.

Convergence means evidence that successive uses improve useful anticipation,
knowledge quality, belief revision, decisions, work products, recovery and
transfer at tolerable total cost. It is not a guarantee, monotonic improvement
on every episode, a scalar score, or convergence to one fixed account of a
changing organisation. New evidence can correctly lower confidence, overturn
a theory, retire a practice, or narrow a supported role.

Two failures must remain visible: adding individually useful components without
closing an organisational loop; and making safeguards, representation or
evaluation so costly that learning and useful participation cannot occur.
Existing proportional acceptance and authority rules in `AGENTS.md` govern.

## 2. The capability to close

The smallest meaningful organisational loop is:

> notice a role-relevant change or unmet need → recover the shared situation →
> identify a consequential unknown → investigate or make a bounded assumption →
> reason and act within standing delegation → inspect the outcome → revise
> knowledge, commitments or practice → use the revision in later work

These are semantic responsibilities, not mandatory model stages. One model
call and a few tools may perform several of them. Schedules and events can
provide occasions to notice needs, but do not decide their importance.

Three timescales need an explicit connection in the chosen pilot:

| Timescale | What should persist or change? | What demonstrates progress? |
| --- | --- | --- |
| Task or encounter | Exact observations, decisions, effects and unfinished work | A useful result or honest recoverable non-success |
| Continuing role | Commitments, dependencies, people, resource constraints, unknowns and next attention conditions | Work resumes across sessions and changes without repeated briefing or lost obligations |
| Learning and transfer | Attributed practice, applicability, outcome evidence, revision and withdrawal | Later independent work benefits, and unsuitable practice is rejected in a changed setting |

Each substantial programme tranche should close a selected connection between
these timescales. A supporting component may ship independently; the role
claim stays open until the connection is exercised. Do not turn every
component PR into a longitudinal research campaign.

## 3. Start from existing surfaces

| Need | Starting surface | Boundary still to establish in the pilot |
| --- | --- | --- |
| Continuing work | `task_management_service.py`, `task_execution_service.py`, durable schedules/events | A responsibility generates and revisits work even without a fresh user instruction |
| Shared working account | Conversation situations and `context_bundle_service.py` dossiers | Cross-session reconstruction uses current canonical sources; a summary does not replace them |
| Contextual knowledge | `scoped_assertion_service.py`, uncertain relationships and testing theories | Material assumptions, alternatives and dependencies survive retrieval and revision |
| Knowledge acquisition | Existing search/ingestion tools and acquisition profiles | Investigation is selected for decision usefulness, not merely empty ontology fields |
| Learning | `learning_candidate.v1`, episode evidence, experiment services and existing direct-adaptive model calls | A source-derived revision reaches a later decision, its outcome changes the lesson, and the changed state is subsequently used |
| Team use | Trusted actor scope, organisation-visible assertions and canonical reads | Shared knowledge is actually discoverable by the intended participants through the selected retrieval path |

These names identify code to inspect, not proof of deployment or adequacy.
General context inheritance and conflict semantics remain open in the
[contextual knowledge guide](contextual_knowledge_evolution.md). The
[assertion design](assertion_and_propositional_sentence_ontology.md) owns
assertion identity and typing; this guide does not create a competing model.

At the evidence date, main contains non-active candidate retention. An active
implementation under [JVNAUTOSCI-2720](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2720)
targets an actual later-use learning cycle. Re-read that issue and its branch
before adding another consumer. Its current plan selects direct-adaptive
capability choice and records the legacy automatic selector as disabled.
The process-global compatibility routing learner is therefore not evidence
that ordinary deployed turns learn through it. Its unspecialised training
population must be addressed if that path is deliberately reused.

## 4. A minimal working role account

Begin with inspectable text in an existing dossier linked to canonical tasks,
sources and work products. Add typed structure only where an actual consumer
needs stable identity, query, revision or exact execution. The useful content is:

- purpose, beneficiary and the responsibility undertaken;
- role-holder and occupancy period, kept distinct from the role itself;
- actual standing delegation and material limits, obtained from trusted
  authority rather than inferred from a role name or a document;
- current commitments, owners, handoffs, resources and dependencies;
- source-qualified beliefs, competing interpretations, assumptions and unknowns;
- evidence likely to change a decision, and the next condition for attention;
- the current work product and links to learned practice where relevant.

This is an authoring aid, not a required schema or an onboarding questionnaire.
Von should acquire available context itself. Ask only for material missing
information or authority, retaining the unanswered question while continuing
independent work. Do not ask people to curate an ontology before helping them.

Separate role, person, occupancy, responsibility and permission when they matter.
An existing role class or a task's role text does not establish an executable
mandate. A new holder can inherit appropriate organisational context without
inheriting a predecessor's private information or personal permissions.

## 5. Knowledge, theories and reasoning must affect decisions

For a material conclusion, retain the weakest adequate account of its source,
context, date, assumptions and downstream use. For example, distinguish a
funder's rule, a local interpretation, provisional eligibility and a scenario
assuming an exception. Retain both plausible interpretations when unresolved.

When a premise changes, identify affected conclusions and work products,
reconsider them, and record what changed or why no change was needed. Start
with explicit dependency links or a bounded dependency section in the dossier;
do not wait for a universal truth-maintenance engine. Do not propagate a
retraction into unrelated contexts or confuse source revision with falsity.

Use LLM judgement for semantic interpretation and investigation choice; use
exact calculation or constraint tools when dates, quantities or formal
conditions require them. A confidence number, successful write, source count,
or claim absent from the base is not a proof. Preserve source dependence and
calibrate claims only to the evidence actually available.

Record an important unknown together with the decision it affects, available
investigations and the consequence of leaving it unresolved. Choose among
search, source inspection, observation, a small experiment, a focused question
or a recoverable assumption according to expected usefulness and burden.
Formal value-of-information arithmetic is optional. Assess afterwards whether
the investigation changed a decision or avoided consequential rework.

Coverage matters when asserting absence: record what was examined and what
remains unavailable. Failure to observe an approval or opportunity is not proof
that none exists. Missing expected events can themselves warrant attention.

## 6. Learning must close through later use

The [represented-advice design](represented_advice_design.md) owns candidate
semantics, applicability and lifecycle. Use it rather than building a second
learning store. Learn from successful practice, demonstrations, discussion,
corrections, failures and delayed outcomes. Preserve who contributed what and
when; later clarification cannot become evidence of earlier autonomous skill.

For the first bounded cycle, identify one lesson, a later independent decision,
the exact revision considered, the resulting action and canonical outcome,
and an evidence-based disposition. Then show a subsequent use of the revised
state or the absence of withdrawn advice. Explicit deliberation can be an
adequate first consumer; permanent prompt injection is not required.

Retaining a conjecture is cheaper and weaker than activating general advice.
Useful low-consequence learning should not wait for broad role certification.
An inconclusive or negative comparison may justify narrowing or withdrawing a
lesson; report that honestly, without calling it improved task performance.
Recovery, no-advice operation and alternative authorised capabilities remain
available. This programme does not grant autonomous activation authority.

Transfer conditional practice, not source-specific facts, private examples or
permissions. Test a case where the lesson helps and one where circumstances
make it inappropriate. Introduce a different organisational setting early
enough to expose hard-coded assumptions, then expand the claim only with
evidence. Generalisation is not established by parameterised code alone.

## 7. First pilot and evidence sequence

Select one research/funding-process continuity responsibility: maintain the
readiness of a small work package or opportunity, including outstanding
commitments, decision-relevant uncertainty and a useful next-action brief.
The selected integration task is
[JVNAUTOSCI-2721](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2721);
its live record owns the actual operator, source selection and delivery state.
Use [UC-06 work, JVNAUTOSCI-2590](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2590)
where it fits, and consume the bounded learning work in 2720 where it fits.
Neither complete general microtheories nor every historical acceptance item
on neighbouring tasks is a prerequisite for this pilot.

The [staged rehearsal](role_convergence_rehearsal.md) supplies a fictional,
chronological development case. It is not held-out efficacy evidence. A real
pilot needs an authorised source, a responsible operator and an independently
inspectable work product; those exact identities belong in protected evidence
and the live task, not this public design.

The [test programme](role_convergence_test_programme.md) connects that development
case to existing replay tasks, a real operational witness, prospective use and
later learned-benefit comparisons. Its current selection and release decisions
remain in 2721.

| Increment | Smallest useful outcome | Claim left open |
| --- | --- | --- |
| Continuity | Recover one responsibility, observe a later change and carry unresolved work without duplication | Learned improvement and broad autonomy |
| Revision | A changed source leads to reconsidered conclusions and an updated work product, preserving uncertainty | General logical/context reasoning |
| Learned reuse | A source-derived lesson is used in an independent encounter; outcome evidence changes its disposition and later exposure | All-role or all-organisation benefit |
| Transfer | Useful practice survives a materially different setting and a non-applicable case without carrying private facts or authority | Universal role competence |

Keep these increments integrated around the same continuing job. Do not build
four general platforms. Existing components can satisfy an increment without
another implementation project.

## 8. Observe convergence and act on divergence

Use an existing Jira task and linked experiment/work-product evidence. Record
the current delivery decision, observed change from baseline and next missing
connection. No new dashboard, score, persistent registry or universal CI gate
is required. Do not duplicate live status in this guide.

During an active pilot, the assigned implementation owner reviews evidence
after each completed cycle and in the existing weekly programme review. The
epic's accountable owner chooses continuation, narrowing, subtraction or
reallocation. If ownership is unassigned, expose that in Jira; writing this
guide does not assign people, launch a worker or schedule notifications.

| Observation | Programme response |
| --- | --- |
| Components ship but no responsibility is carried better across encounters | Make the missing integration the next slice; defer unrelated substrate expansion |
| Repeated explanation, curation or supervision erases useful time saved | Change acquisition/context or narrow the role; count operator and maintainer effort, not just user-facing questions |
| Sources change but conclusions remain stale | Prioritise the affected dependency/reconsideration path |
| Lesson does not improve decisions or is misapplied | Narrow, revise or withdraw it; compare the simpler no-advice path |
| A compulsory control blocks useful recoverable work | Apply the existing evidential burden and remove or narrow the control where unjustified |
| A material wrong effect, disclosure or failed withdrawal is observed | Contain the affected path, repair/recover and revisit that release decision |
| No role evidence appears across two planned weekly reviews | Record the actual blocker and choose a smaller end-to-end job, a needed integration, or an explicit pause; more component completions cannot stand in for evidence |

The two-review trigger concerns programme allocation, not automatic shutdown,
an SLA, or a veto on independently useful fixes. The first review is due at the
earlier of the pilot's first completed cycle or its next weekly review after
implementation begins. Cadence and capacity may be revised in Jira with a
reason; there is no claim that the review already runs automatically.

For a claim of learned benefit, compare independent later tasks with and
without the retained lesson under the same suitable model/tools and equivalent
starting conditions. Separate development material, held-out evaluation and
prospective use. Prevent later-event leakage, assisted-success inflation and
experimental learning contaminating the baseline. Sample size and controls
follow the claim; 2720's selected experiment owns its own research criteria.

Measure useful outcomes, missed obligations/opportunities, premise revision,
human correction and supervision, appropriate initiative, recovery, latency,
available cost and transfer effort. Missed work requires an independent account
of what should have been noticed; successful triggered turns alone cannot
measure it. Preserve delayed feedback and attribution uncertainty: a grant win,
polished report or critic score alone does not establish good management.

## 9. Ownership and completion boundaries

- `JVNAUTOSCI-2011` owns the role outcome, prioritisation, current evidence and
  next connecting increment. Keep its live description usable, not a duplicate
  of this guide or a list of every possible future subsystem.
- `JVNAUTOSCI-2721` owns the first longitudinal integration pilot and its
  independently deliverable continuity, learning and early-transfer increments.
- `JVNAUTOSCI-2590` owns its bounded digest/obligation capability;
  `JVNAUTOSCI-2720` owns its selected learning experiment. This guide does not
  expand either task's acceptance criteria or override an active candidate.
- `JVNAUTOSCI-2038` and the assertion/context guides own general contextual
  semantics. A pilot may use explicit scoped text and dependencies first.
- `JVNAUTOSCI-2579` owns broader scientific evaluation; publication-grade claims
  do not gate ordinary bounded usefulness.

Updating guidance and Jira establishes engineering direction only. Positive
role claims require work-product and later-use evidence; deployment and real
external commitments require their actual authority. Full generalisation is
an open research objective, not a completion label for this documentation.
