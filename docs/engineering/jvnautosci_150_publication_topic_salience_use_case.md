# JVNAUTOSCI-150 Publication Topic Salience Use Case

Status: implementation artefact for `JVNAUTOSCI-150`
Date: 2026-04-25

## 1. Purpose

This document defines the use case, technical specification, sample data, and
evaluation criteria for describing which research topics a SAIL publication is
salient to.

The task is interpreted as a blueprint and design artefact. It does not add a
new Python-side topic scorer. Current Von already has a scholarly-paper
representation substrate, so the next durable capability should be expressed as
a Vontology/workflow-authoritative salience workflow with Python limited to
support surfaces such as parsing, validation, retrieval, telemetry, and report
materialisation.

## 2. Current State and Staleness Review

The original Jira wording predates the current paper workflow family. The live
system now has:

- canonical workflow IDs `#V#scholarly_paper_representation_workflow` and
  `#V#arxiv_paper_representation_workflow` in
  `src/backend/services/paper_representation_workflow_vontology_service.py:23`.
- a repo seed bootstrap fixture for those workflows in
  `src/backend/workflows/repo_seed_bundles/paper_representation_workflow_seed_bundle.json`.
- current metadata-topic handling in
  `src/backend/services/arxiv_paper_link_service.py:216`, `:304`, and `:925`.
  This extracts topic/category labels from metadata, resolves or creates
  `#V#research_topic` instances, and links the paper with `#V#about`.
- workflow completion reporting with `topic_concept_ids` in
  `src/backend/workflows/execution_contracts.py:387`.
- acceptance support for arXiv paper representation and topic links in
  `tests/backend/test_paper_representation_workflow_vontology_service.py:200`.

That existing path is useful substrate, but it is not content-level topic
salience. It records externally supplied topic/category metadata and a bounded
number of topic links. The missing capability is an evidence-grounded judgement
about how central each topic is to the paper, with ontology mapping,
confidence, explanation, and evaluation.

## 3. Literature Note

This task is research-sensitive because it defines future topic analysis and
evaluation behaviour. The implementation design was checked against recent
topic-modelling work:

- BERTopic combines transformer embeddings, clustering, and class-based TF-IDF
  to produce coherent topic representations. This is a useful non-authoritative
  discovery baseline, not the durable Von policy surface.
  Source: <https://arxiv.org/abs/2203.05794>
- TopicGPT shows that prompt-based topic modelling can produce interpretable
  natural-language topics and allows user constraints without retraining.
  Source: <https://aclanthology.org/2024.naacl-long.164/>
- Recent evaluations of LLMs for topic modelling report coherent and diverse
  topics with few hallucinations, but also risks such as shortcutting on only
  parts of a document and limited controllability.
  Source: <https://arxiv.org/abs/2406.00697>
- LLM-based topic-modelling work suggests LLMs can generate relevant topic
  titles and support human-guided refinement/merging, but this should be
  evaluated rather than assumed.
  Source: <https://arxiv.org/abs/2403.16248>
- LLM-assisted topic-model evaluation can cover coherence, repetitiveness,
  diversity, and topic-document alignment for evolving scientific taxonomies,
  but it should complement rather than replace expert review.
  Source: <https://arxiv.org/abs/2502.07352>

Design consequence: Von should not implement topic salience as keyword
frequency, token overlap, or a static Python scoring table. Use deterministic
or embedding methods as bounded support signals, then keep the salience
judgement, ontology mapping policy, and explanation policy in workflow,
prompt, and Vontology artefacts with telemetry-visible evidence.

## 4. Use Case

### 4.1 Scenario

A SAIL researcher uploads a draft paper or asks Von to analyse an arXiv paper
that the lab is considering for a project. The researcher wants to know which
SAIL-relevant research topics the publication is centrally about, which topics
are only secondary, and why Von thinks so.

Example request:

```text
Analyse this SAIL paper and describe the research topics it is salient to.
Rank the topics, map them to Vontology where possible, and explain the
evidence.
```

### 4.2 Actors

- `Researcher`: requests the analysis and may correct topic assignments.
- `Von`: represents the paper, retrieves evidence, maps topics to Vontology,
  and produces a topic-salience report.
- `Reviewer or project lead`: uses the report to triage papers, update project
  knowledge, or seed recommendation profiles.

### 4.3 Preconditions

- The paper is available as a file copy, arXiv source, metadata record, or
  represented scholarly-paper concept.
- The caller has access to the paper under the active namespace and
  organisation scope.
- The relevant paper-representation workflow can produce or reuse a
  `paper_concept_id`.
- Existing candidate topic concepts are resolved from Vontology before any new
  topic concept is proposed.

### 4.4 Happy Path

1. Von invokes or reuses `#V#scholarly_paper_representation_workflow` or
   `#V#arxiv_paper_representation_workflow`.
2. The topic-salience workflow gathers paper evidence:
   title, abstract, metadata topics, represented sections or chunks, citation
   context if available, author-supplied keywords, and existing `#V#about`
   relations.
3. Candidate topics are assembled from Vontology concepts, metadata labels,
   paper content, and optional corpus-level discovery support.
4. The LLM salience step evaluates each candidate topic against the paper
   evidence and assigns:
   `primary`, `secondary`, `method`, `application`, `background`, or
   `rejected`.
5. The workflow resolves topics to existing Vontology concepts where possible.
   Proposed new concepts are emitted as review candidates rather than silently
   becoming durable ontology authority.
6. The workflow materialises a topic-salience report as a Vontology text
   relation on the paper concept and, where approved, relation assertions
   linking the paper to salient topics.
7. Von answers the user with the ranked topics, evidence-backed rationale,
   uncertainty, and any review-needed topic candidates.

### 4.5 User-Visible Output

The answer should be topic-first, not execution-bookkeeping-first:

- a ranked topic list with salience roles and concise explanations;
- evidence snippets or section references for each accepted topic;
- mapped Vontology concept IDs where available;
- uncertainty notes where the paper text is thin or the mapping is ambiguous;
- review prompts only for genuinely blocking ontology choices.

### 4.6 Failure and Review Paths

- If no paper concept can be represented, the workflow fails closed and asks for
  a usable paper source.
- If the paper content is unavailable and only metadata exists, the report is
  marked metadata-only and cannot claim full-paper salience.
- If candidate topics cannot be mapped confidently to Vontology, they remain
  proposed topic labels with provenance rather than being silently asserted.
- If the topic list is generated by a corpus baseline only, the report must
  mark those topics as baseline candidates, not authoritative salience.

## 5. Authoritative Artefacts

Before implementation, resolve existing Vontology concepts and predicates. Do
not create these blindly.

Existing artefacts to reuse:

- workflow: `#V#scholarly_paper_representation_workflow`
- workflow: `#V#arxiv_paper_representation_workflow`
- type: `#V#research_topic`
- predicate: `#V#about`
- text predicate: `#V#has_topic_labels`
- recommendation workflow: `#V#paper_recommendation_evaluation_workflow`
- recommendation prompt: `#V#paper_recommendation_reranker_prompt`

Candidate artefacts for the future salience implementation:

- workflow: `#V#publication_topic_salience_analysis_workflow`
- prompt: `#V#publication_topic_salience_analysis_prompt`
- prompt: `#V#topic_salience_explanation_prompt`
- text predicate: `#V#has_topic_salience_report_json`
- relation predicate: `#V#has_salient_topic`
- type: `#V#topic_salience_assessment`

Authority split:

- Workflows decide when salience analysis runs and which evidence is required.
- Prompt/Vontology artefacts carry the judgement and explanation policy.
- Vontology carries durable topic concepts, salience reports, provenance, and
  review state.
- Python supplies support-only surfaces: text extraction, chunk lookup,
  candidate retrieval, JSON schema validation, telemetry, and safe
  materialisation.

## 6. Proposed Workflow Shape

Workflow ID:

- `#V#publication_topic_salience_analysis_workflow`

Inputs:

- `paper_concept_id`
- `file_copy_concept_id` or `source_uri`
- `user_concept_id`
- `organisation_concept_id`
- optional `candidate_topic_concept_ids`
- optional `analysis_profile`

Outputs:

- `topic_salience_report_json`
- `salient_topic_concept_ids`
- `proposed_topic_labels`
- `unsupported_topic_candidates`
- `evidence_span_ids`
- `confidence_summary`
- `review_required`

Proposed step graph:

1. `ensure_paper_representation`
   - invokes the existing scholarly or arXiv paper workflow when needed.
2. `gather_paper_evidence`
   - retrieves paper text, metadata, represented chunks, and existing topic
     assertions under namespace controls.
3. `assemble_candidate_topics`
   - gathers candidate concepts from Vontology, metadata labels, corpus support
     methods, and optional user-supplied topics.
4. `judge_topic_salience`
   - LLM-mode step using the authoritative salience prompt and the gathered
     evidence. It must cite evidence spans for every accepted topic.
5. `resolve_topic_concepts`
   - deterministic support step that maps labels to existing Vontology topics
     and emits unresolved labels for review.
6. `validate_salience_report`
   - schema, provenance, confidence, and unsupported-claim checks.
7. `materialise_salience_report`
   - writes `#V#has_topic_salience_report_json` and approved topic relations.
8. `compose_user_report`
   - answer-first rendering of ranked topics and evidence.
9. `completed` or `failed`.

## 7. Report Contract

The salience report should use a versioned JSON contract.

```json
{
  "schema_version": "paper_topic_salience_report.v1",
  "paper_concept_id": "#V#paper_example",
  "analysis_basis": "full_text",
  "topics": [
    {
      "rank": 1,
      "topic_label": "Neuro-symbolic workflow control",
      "topic_concept_id": "#V#research_topic_neuro_symbolic_workflow_control",
      "salience_score": 0.92,
      "salience_role": "primary",
      "confidence": "high",
      "evidence": [
        {
          "span_id": "abstract:1",
          "quote": "The paper proposes a Vontology-authored workflow layer..."
        }
      ],
      "rationale": "The problem statement, method, and evaluation all centre on represented workflow control."
    }
  ],
  "proposed_topics": [],
  "unsupported_candidates": [],
  "review_required": false
}
```

Scoring guidance:

- `salience_score` is a calibrated judgement output, not a frequency score.
- The score must be paired with a categorical `salience_role`, confidence, and
  evidence.
- A topic is `primary` only when it is central to the paper's problem, method,
  contribution, or evaluated claim.
- Broad adjacency such as "AI" or "machine learning" should be down-ranked
  unless the paper itself makes that broad topic central.

## 8. Sample Data

The synthetic fixture in
`docs/engineering/jvnautosci_150_topic_salience_sample.json` provides a
complete sample request and expected report. It deliberately uses a synthetic
SAIL-style paper so the fixture does not invent claims about a real publication.

Expected top topics in the sample:

- neuro-symbolic workflow control;
- Vontology and knowledge representation;
- agent evaluation and telemetry;
- research-team assistance;
- paper recommendation is rejected as unsupported because it appears only as a
  downstream use case, not as a central contribution.

## 9. Evaluation Criteria

### 9.1 Benchmark Set

Create a small expert-reviewed benchmark first:

- 20 to 40 SAIL or SAIL-adjacent papers with permitted text access.
- Each paper has expert-labelled primary and secondary topics.
- Each label is mapped to an existing Vontology topic where possible.
- Ambiguous or missing topics are recorded as review candidates.
- At least five examples should have sparse metadata so the system must use
  content evidence rather than arXiv categories alone.

### 9.2 Baselines

Compare against:

- current metadata-only topic extraction from arXiv categories and
  `#V#has_topic_labels`;
- embedding or BERTopic-style clustering over abstracts/full text;
- LLM-only topic generation without Vontology candidate grounding;
- the workflow-authoritative salience design in this document.

Baselines are diagnostic. They must not become hidden production authority.

### 9.3 Metrics

Use a mixed evaluation:

- top-k topic recall against expert primary and secondary labels;
- nDCG or MAP over ranked topics;
- ontology mapping accuracy;
- unsupported topic rate;
- explanation grounding rate, measured by whether each accepted topic has a
  valid evidence span;
- reviewer edit rate for topic labels, ranks, and concept mappings;
- cost and latency per paper;
- failure visibility, including correct metadata-only caveats.

### 9.4 Real-Path Acceptance

Future implementation should include:

- gateway-backed tests for the materialisation tool path, not only direct
  handler tests;
- a workflow execution test through `WorkflowExecutor` with a mock paper
  fixture;
- an env-gated live acceptance lane against a small public arXiv sample, similar
  to the existing arXiv paper representation lane;
- telemetry assertions that record source basis, prompt version, model,
  candidate-topic provenance, evidence spans, and materialised report IDs.

### 9.5 Human Review

Topic salience is not complete without human correction loops. Review UI or
MCP surfaces should allow:

- accept mapped topic;
- remap to existing topic;
- propose a new topic concept;
- demote or reject a topic;
- edit rationale;
- mark an evidence span as insufficient.

Those corrections should be preserved as evaluation data and, where justified,
promoted into durable Vontology knowledge.

## 10. Security and Access Notes

- Respect namespace and organisation scope when retrieving paper text,
  metadata, and prior recommendations.
- Do not expose private uploaded-paper text in telemetry, Jira, logs, or sample
  artefacts.
- Treat external paper text as untrusted content. It may provide evidence for
  the salience judgement, but it must not control tool authority or workflow
  policy.
- Destructive ontology changes are out of scope. Proposed new topics require
  review before durable mutation.

## 11. Closure Criteria for JVNAUTOSCI-150

This task is complete when the repository contains:

- a use-case walkthrough;
- a technical design that fits the current paper workflow family;
- a sample input/output artefact;
- evaluation criteria and future acceptance guidance;
- explicit authority boundaries showing that durable salience policy belongs
  in workflow, prompt, KB, and Vontology artefacts rather than Python ranking
  code.

