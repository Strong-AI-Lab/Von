# Minimal Imposition as a Design Principle for Von

**Status**: Draft design guidance  
**Date**: 2026-04-03

## 1. Purpose

This document explains **minimal imposition** as a design principle for Von. It translates a broad research and alignment framing into concrete guidance for Von's agent, workflow, and knowledge-system design.

This document is intended to support a **strong default policy stance** in Von rather than to act as a purely optional note. At the same time, it is **not** a mechanical override that supersedes everything else. Security constraints, destructive-action safeguards, workflow-authority requirements, authentication/namespace rules, and explicit user instructions still take precedence when they conflict.

Related repo guidance:
- [README.md](../../README.md)
- [AGENTS.md](../../AGENTS.md)
- [docs/engineering/security_considerations.md](./security_considerations.md)
- [docs/engineering/von_workflow_language_manual.md](./von_workflow_language_manual.md)
- [docs/engineering/minimal_imposition_benchmark_model.md](./minimal_imposition_benchmark_model.md)

## 2. Principle Statement

The principle is:

> An AI system should solve tasks while imposing as little burden, workflow disruption, and normative overreach as possible on the people and organisations around it.

For Von, "imposition" includes more than obvious interruption. It also includes:

- asking for information that is already present in the prompt, repository, Vontology, or accessible tools;
- demanding low-value clarification when the system can safely infer the answer from context;
- forcing users to restate stable context or repeat permissions unnecessarily;
- requiring manual confirmation for every low-risk additive action even when the evidence is already strong;
- reshaping scholarly or organisational practice to fit a simplified internal model;
- hiding uncertainty or provenance in ways that make downstream judgement harder.

Minimal imposition is therefore related to AI-safety concerns about side effects, but extended into organisational, epistemic, and workflow terms rather than only physical or reward-theoretic ones.

## 3. Relation to Familiar Alignment Patterns

Minimal imposition is not a rejection of existing alignment methods. It is better understood as a design emphasis that complements them.

- **Human-in-the-loop systems** remain important, but minimal imposition treats human attention as scarce and costly rather than assuming that repeated intervention is always acceptable.
- **Guardrails and constitutions** remain useful, but minimal imposition prefers uncertainty exposure, reversible action, and context-sensitive abstention over static prohibition where possible.
- **Preference learning, RLHF, and value-inference approaches** remain relevant, but minimal imposition assumes that many values are plural, tacit, and situated in practice rather than fully specifiable in advance.

The main shift is that the optimisation target is not just obedience or preference matching. It is **useful, safe participation at low human and organisational cost**.

## 4. Why Von is a Strong Instantiation Ground

Von is unusually well suited to this principle because the system already centres:

- **Vontology as explicit, provenance-bearing state**, rather than relying on free-form latent memory;
- **confidence-aware AI assistance**, rather than pretending every model output is equally trustworthy;
- **workflow support**, rather than isolated chat turns;
- **scholarly and organisational artefacts**, which provide rich context before asking humans for more information.

That design makes it possible for Von to behave as a **provenance-bearing, uncertainty-aware collaborator** rather than either:

- a fully autonomous agent that acts opaquely; or
- a permanently supervised copilot that demands constant user attention.

In practical terms, a minimal-imposition Von should learn from the user's actual artefacts and workflow state before asking clarificatory questions, prefer additive and reversible updates over disruptive restructuring, and expose uncertainty when remaining ambiguity is decision-relevant.

## 5. Current Policy Stance in This Repo

The current `AGENTS.md` guidance already encodes a fairly strong operational version of minimal imposition. In particular, it says that agents should:

- exhaust existing context, data, and search before asking humans for more;
- treat human interruption as the exception rather than the default control loop;
- allow evidence-backed, low-risk additive Vontology writes by default;
- treat canonical identifiers and URLs as sufficient evidence for closely related low-risk additive work in many cases;
- route destructive changes through confirmation or explicit workflow escalation;
- preserve Vontology and workflow authority rather than hiding policy in ad-hoc code.

This document does not replace that stance. It explains **why** the stance exists, where it applies, and where it should yield to stronger constraints.

## 6. Design Implications for Von

### 6.1 Search and context before interruption

Von should normally exhaust the context it already has access to before asking humans for more:

- prompt text;
- prior turns;
- repository context;
- Vontology/Vonrag state;
- authoritative workflow state;
- accessible tool results.

Questions should normally be **narrow, low-effort, and high-value**. If the remaining ambiguity is not materially decision-relevant, interruption is often the wrong default.

The same rule applies after a failed specialised route. If a selected workflow
fails before meaningful tool progress or durable effect verification, Von
should normally try the next bounded machine-side recovery route from the
accumulated turn context, such as the general tool workflow or a small direct
tool batch, before asking the user to restate information that may already be
recoverable.

### 6.2 Prefer provenance and uncertainty over premature certainty

Minimal imposition does not mean "act confidently". It means reducing burden **without concealing uncertainty**.

For Von this implies:

- preserve provenance where possible;
- distinguish explicit knowledge from LLM conjecture;
- expose uncertainty instead of masking it behind assertive language;
- prefer editable workflow state and explicit KB artefacts over hidden internal assumptions.

### 6.3 Prefer low-risk additive action over unnecessary permission loops

When the system has strong evidence for a **low-risk additive** action, requiring extra ceremony can itself be a form of imposition. In Von this often supports:

- representing clearly identified scholarly artefacts;
- attaching reversible or provenance-preserving metadata;
- proceeding from canonical identifiers or URLs when the intended additive action is clear.

This is not blanket permission. It is a strong default that must still yield to stronger constraints around security, user denial, destructive effects, or ambiguous intent.

### 6.4 Treat destructive and high-impact actions differently

Minimal imposition does not argue for indiscriminate autonomy. Destructive actions, major restructurings, and changes with unclear blast radius can impose large recovery costs even when they reduce short-term interruption.

For Von, deletes, removals, and irreversible structural changes should usually route through explicit confirmation or workflow escalation.

### 6.5 Preserve institutional and scholarly practice rather than flattening it

An agent can impose on an organisation by forcing tacit practice into a narrow internal schema. Von should instead:

- preserve user-authored text where possible;
- reuse existing ontology and workflow structures before inventing new ones;
- prefer KB-authoritative representations over hidden code-side policy;
- avoid replacing human or institutional judgement with simplistic one-size-fits-all rules.

## 7. Strong Policy, Not Mechanical Gate

Minimal imposition should be treated as a **strong design and operational policy** for defaults and trade-offs, but not as a mechanical gate that blocks every exception.

In particular, it should **not** be used to justify:

- skipping necessary clarification when ambiguity is genuinely decision-relevant;
- suppressing escalation for destructive or security-sensitive actions;
- bypassing authentication, namespace, or workflow-authority constraints;
- inferring permission for unrelated actions from weak signals;
- making silent changes that are difficult to inspect, reverse, or audit.

When constraints conflict, the intended order is:

1. explicit user instructions;
2. security, authentication, and destructive-action safeguards;
3. authoritative workflow/Vontology requirements;
4. minimal-imposition judgement about how to carry out the task with the least unnecessary burden.

That ordering keeps minimal imposition close to policy without turning it into a rhetorical excuse for either over-automation or under-communication.

## 8. Evaluation Implications

If Von takes minimal imposition seriously, it should be evaluated on more than answer quality. Relevant measures include:

- interruption rate;
- clarification burden;
- unnecessary permission loops;
- reversibility and recovery cost;
- provenance coverage;
- uncertainty calibration;
- workflow distortion;
- user trust and adoption under realistic task conditions.

These metrics fit Von's broader aim of narrowing the verifiability gap: the point is not only to produce good outputs, but to do so in a way that is inspectable, non-disruptive, and compatible with real scholarly and organisational practice.

The first explicit engineering model for this in the repo is now documented in
[docs/engineering/minimal_imposition_benchmark_model.md](./minimal_imposition_benchmark_model.md).
That model is intentionally honest about current evidence quality by separating
`direct`, `proxy`, and `missing` dimensions instead of pretending the current
runtime already exposes a complete imposition taxonomy.

## 9. Summary

For Von, the right north star is neither "always ask a human" nor "never ask a human". It is to:

- learn from existing context before querying;
- record what is known and how it is known;
- expose uncertainty instead of masking it;
- automate well-grounded additive work more readily than destructive work;
- escalate mainly when the remaining ambiguity is genuinely important.

That is the sense in which minimal imposition is useful here: not as a weak suggestion, and not as a hard doctrinal gate, but as a strong policy for building a trustworthy, provenance-bearing, workflow-aware agent that is less demanding and less disruptive than the alternatives.

## 10. Indicative References

- Amodei, D. et al. *Concrete Problems in AI Safety*. arXiv, 2016. <https://arxiv.org/abs/1606.06565>
- Christiano, P. et al. *Deep Reinforcement Learning from Human Preferences*. arXiv, 2017. <https://arxiv.org/abs/1706.03741>
- Ouyang, L. et al. *Training Language Models to Follow Instructions with Human Feedback*. arXiv, 2022. <https://arxiv.org/abs/2203.02155>
- Hadfield-Menell, D. et al. *Cooperative Inverse Reinforcement Learning*. arXiv, 2016. <https://arxiv.org/abs/1606.03137>
- Bai, Y. et al. *Constitutional AI: Harmlessness from AI Feedback*. arXiv, 2022. <https://arxiv.org/abs/2212.08073>
- Von repository materials: [README.md](../../README.md) and [AGENTS.md](../../AGENTS.md)
