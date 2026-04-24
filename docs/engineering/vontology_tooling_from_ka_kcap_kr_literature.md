# Vontology Tooling Directions from KA, KCAP, Common-Sense KR, and Scientific KR

**Status:** design note  
**Date:** 24 April 2026  
**Scope:** proposed Vontology read/write/support tools informed by knowledge acquisition, knowledge capture, commonsense knowledge representation, and scientific knowledge representation literature.

## 1. Purpose

This note identifies Vontology tools that would make Von better at grounded retrieval, mixed-initiative knowledge acquisition, scientific reasoning, and long-horizon agent memory.

The design assumption is the core Von one: Vontology, workflows, prompt programmes, represented metadata, and durable KB assertions should hold the authored policy. Python should provide reusable support surfaces only: query execution, validation, telemetry, provenance capture, gateway exposure, and safe mutation pathways.

The tool names below are proposals. They should be treated as capability sketches to be refined against existing Vontology predicates and workflow concepts before implementation.

## 2. Literature Signals

### 2.1 Assisted Knowledge Acquisition

The Cyc KA papers point to a mixed-initiative acquisition loop rather than passive form filling. "Knowledge Begets Knowledge" describes using an existing KB and inference system to elicit, verify, and accelerate new knowledge entry. "An Interactive Dialogue System for Knowledge Acquisition in Cyc" emphasises topic/user modelling, prioritised interactions, a transparent agenda, and system-initiated questions aimed at knowledge gaps and inferential utility. TextLearner adds another important point: acquisition from text benefits from storing intermediate document models and competing hypotheses, not only final extracted facts.

For Von this implies tools that can:

- discover what knowledge is missing for a concept, type, task, workflow, or paper;
- explain why a question is worth asking;
- rank acquisition opportunities by expected downstream utility;
- keep hypotheses, ambiguous readings, and rejected interpretations visible;
- validate new assertions against type, context, provenance, and consistency constraints before durable promotion.

### 2.2 CommonKADS and Ontology Engineering

CommonKADS separates task, inference, domain, and communication models. Protege's long-lived success comes partly from making ontology editing, frames/classes/properties, constraints, and plugins accessible to working knowledge engineers. SHACL shows the value of explicit validation shapes over relying on schema comments or downstream code failures.

For Von this implies tools that can:

- inspect the expected relation profile for a type or task;
- validate a concept or assertion bundle against represented constraints;
- separate user-facing task competence from domain ontology facts;
- expose ontology modelling gaps without making the LLM infer graph integrity from raw rows.

### 2.3 Contexts and Microtheories

Cyc's use of microtheories is a central lesson for Von. Context boundaries allow different assumptions, competing hypotheses, fictional claims, domain-specific defaults, and localised inference control. Context work by Guha/McCarthy/Buvac reinforces that the context of an assertion is not metadata decoration; it changes what follows from the assertion.

For Von this implies tools that can:

- query a concept or relation under an explicit context;
- compare what is asserted, inherited, or contradicted across contexts;
- return the context lineage that made an answer available;
- let workflows choose the correct context without hard-coded Python policy.

### 2.4 Commonsense Knowledge Graphs

ConceptNet shows the value of broad, labelled, natural-language-facing relations gathered from heterogeneous sources. ATOMIC and COMET show a complementary event-centred style: commonsense is often about likely preconditions, intents, effects, reactions, and next events. These are closer to Davidsonian or frame-like representations than to simple binary facts.

For Von this implies tools that can:

- retrieve by event frame, participant role, likely cause/effect, and mental-state role;
- distinguish direct entity predicates from event-mediated role predicates;
- surface low-confidence/generated commonsense separately from curated assertions;
- turn commonsense completion into reviewable candidate knowledge, not silent authority.

### 2.5 Scientific KR

Scientific KR standards point in a stricter direction than open commonsense graphs. OBO Foundry stresses coordinated ontology evolution and orthogonality. PROV-O and nanopublications make provenance and attribution first-class. BioPAX and SBML show that scientific mechanisms and models need formal exchange representations, not only text search hits.

For Von this implies tools that can:

- materialise scientific claims with assertion, provenance, publication, context, and confidence separated;
- represent mechanisms as entities, activities, conditions, causal/temporal structure, evidence, and competing hypotheses;
- align Vontology concepts to external identifiers and ontology terms;
- compare claims from different papers, datasets, and contexts;
- validate units, quantities, experimental methods, and model dependencies where a domain schema exists.

## 3. Current Vontology Surface

Von already has a useful base surface:

- concept and KB search: `search_concepts`, `vontology_concept_search`, `search_knowledge_base`;
- concept/text access: `get_text_relations`, `get_text_relations_summary`;
- relation access: `find_relations_with_argument`, `get_predicate_incidence`;
- scholarly acquisition: `search_arxiv`, `get_paper_metadata`, `materialise_scholarly_representation_for_file_copy`;
- workflow tools and Jira/task tools.

The important gap is not "more broad search". It is structured, context-aware, provenance-aware, role-aware Vontology interrogation and safe knowledge promotion.

## 4. Proposed Tool Families

### 4.1 Typed Predicate Incidence and Relation Profiles

**Already identified near-term gap:** extend `get_predicate_incidence` or add a companion tool that reports argument-type distributions for predicates connected to a concept or type.

Useful tool shapes:

- `get_typed_predicate_incidence`
- `summarise_type_relation_profile`
- `compare_relation_profile_to_type_expectations`

Core output:

- predicate id and name;
- direction or matched argument position;
- direct relation count;
- distinct target count;
- target type counts per argument role;
- sample assertions with relation ids;
- Davidsonian expansion summary when the concept participates through event roles;
- source/context/provenance counts.

Why it matters:

- For non-Davidsonian binary facts, it answers "what kinds of things is this entity related to, and how?"
- For Davidsonian/event representations, it answers "what event frames connect this entity to other typed participants?"
- For workflow selection, it helps the planner choose a narrow relation lookup instead of a broad semantic search.
- For ontology engineering, it exposes modelling drift such as predicates used with unexpected argument types.

### 4.2 Context and Microtheory Query Tools

Proposed tools:

- `query_relations_in_context`
- `compare_context_assertions`
- `explain_context_lineage`
- `list_applicable_contexts_for_turn`

Core output:

- assertions available in the requested context;
- assertion source: direct, inherited, inferred, or hypothetical;
- context inclusion path;
- conflicts or shadowed assertions from neighbouring contexts;
- whether the assertion is durable, provisional, fictional, workflow-local, or user-private.

Useful situations:

- A user asks "what do we believe about this paper's claim?" where lab belief, paper claim, and general background differ.
- A workflow needs one answer under the user's private namespace and another under project-wide knowledge.
- Scientific hypotheses need to be stored without becoming settled fact.
- A LLM-facing answer needs to justify that it used the right context rather than a globally convenient one.

Implementation notes:

- The authored context hierarchy and policies must live in Vontology/VWL.
- Python should only execute context-bounded retrieval, compute lineage, and shape telemetry.
- Tool output must be namespace-scoped and must not leak private context existence through counts.

### 4.3 Provenance, Evidence, and Explanation Tools

Proposed tools:

- `trace_assertion_provenance`
- `explain_relation_path`
- `get_supporting_evidence_bundle`
- `compare_evidence_for_claim`

Core output:

- assertion ids and relation ids;
- source artefacts, authors, timestamps, and acquisition route;
- provenance graph in a PROV-like shape: entity, activity, agent, source;
- confidence and review status;
- short LLM-facing evidence summary plus machine-readable evidence rows;
- explicit gaps where no source or verifier is recorded.

Useful situations:

- A user asks for a factual answer and Von must show whether it is grounded in Vontology, a paper, a user note, or model recall.
- A paper-derived claim is contradicted by a later source.
- An agent needs to decide whether a claim can be promoted from ephemeral theory to durable knowledge.
- Evaluation needs to distinguish "answer correct by luck" from "answer grounded by inspected evidence".

Implementation notes:

- Use a nanopublication-like split between assertion, provenance, and publication/context metadata.
- Do not hide provenance behind human prose. Return structured evidence rows for validators and telemetry.

### 4.4 Shape, Constraint, and Modelling Validation Tools

Proposed tools:

- `validate_concept_against_shape`
- `validate_assertion_bundle`
- `list_shape_requirements_for_type`
- `find_shape_violations`

Core output:

- applicable type or workflow shapes;
- required, recommended, and disallowed predicates;
- domain/range/type mismatches;
- cardinality or uniqueness problems;
- missing provenance or missing context;
- severity, repair suggestions, and affected assertion ids.

Useful situations:

- Before creating a workflow, prompt, paper, person, project, or mechanism concept.
- Before promoting extracted paper claims into durable represented knowledge.
- During ontology cleanup when predicate usage has drifted.
- As an acceptance gate for knowledge-writing workflows.

Implementation notes:

- Shapes should be represented in Vontology, not hard-coded Python dictionaries.
- Python may provide a SHACL-inspired validator over Vontology's own data model.
- Validation should be non-destructive by default and return a repair plan rather than mutating automatically.

### 4.5 Mixed-Initiative KA Agenda Tools

Proposed tools:

- `suggest_knowledge_acquisition_questions`
- `rank_knowledge_gaps`
- `explain_acquisition_question`
- `record_ka_answer_with_validation`

Core output:

- candidate questions;
- target concept, missing predicate, expected argument type, and context;
- why the question matters: validation failure, workflow blocker, high-value relation, user-requested goal, or inference utility;
- answer capture schema;
- validation result and proposed assertion bundle.

Useful situations:

- A user adds a new concept and Von should ask the one or two highest-value follow-up questions, not a long form.
- A workflow cannot proceed because a required relation is missing.
- A project role, paper, dataset, or model deployment lacks enough represented state for future automation.
- Von wants to reduce repeated user interruptions by filling high-utility slots opportunistically.

Implementation notes:

- The agenda policy belongs in Vontology/workflows.
- The tool should expose candidate gaps and utility evidence; the workflow decides whether to ask.
- User-authored text must be preserved exactly.

### 4.6 Concept Placement and Ontology Alignment Tools

Proposed tools:

- `suggest_concept_placement`
- `compare_candidate_parent_types`
- `align_external_identifier`
- `find_external_ontology_mappings`

Core output:

- candidate parent types and sibling concepts;
- supporting names, descriptions, predicates, and sources;
- conflicts with existing type hierarchy;
- external ids and ontology terms: DOI, ORCID, arXiv, PubMed, GO/OBO, DBLP, Wikidata, OpenAlex, where applicable;
- confidence and review state.

Useful situations:

- A new paper, method, dataset, person, institution, or biological entity is introduced.
- A scientific claim uses a term that should align to an external controlled vocabulary.
- Retrieval fails because the local concept and external identifier have not been reconciled.
- Duplicate concepts are suspected.

Implementation notes:

- External alignment should remain evidence-bearing and reversible.
- The canonical Vontology concept id should remain the local authority even when external ids are attached.

### 4.7 Scientific Claim and Mechanism Materialisation Tools

Proposed tools:

- `materialise_scientific_claim`
- `build_mechanism_skeleton`
- `compare_mechanism_models`
- `validate_scientific_model_bundle`

Core output:

- claim concept and assertion bundle;
- paper/source provenance;
- involved entities, processes, measurements, methods, datasets, and conditions;
- causal, temporal, and part-whole structure;
- competing or alternative mechanism links;
- model exchange references where applicable: BioPAX-like pathway, SBML-like quantitative model, or domain-specific representation.

Useful situations:

- Reading a molecular biology paper and extracting mechanism hypotheses.
- Representing a scientific result separately from the general belief that the result is true.
- Comparing two papers' causal models.
- Asking "what evidence supports this pathway?" or "which assumptions does this model require?"

Implementation notes:

- The tool should create a reviewable bundle, not silently assert all extracted claims as settled truth.
- Mechanism schemas should be Vontology concepts and predicates.
- Units and quantities should be validated where a domain shape exists.

### 4.8 Davidsonian Role-Frame Retrieval Tools

Proposed tools:

- `find_event_frames_by_participant`
- `query_role_frame`
- `summarise_event_role_incidence`
- `expand_binary_relation_to_event_frames`

Core output:

- event/frame concept ids;
- roles played by the anchor concept;
- other participants grouped by role and type;
- time, place, modality, negation, source, and context qualifiers;
- related direct binary assertions when they exist.

Useful situations:

- Questions such as "which papers did I write with X?" where authorship may be direct or mediated through paper/event roles.
- Commonsense questions about actions, intentions, preconditions, and effects.
- Scientific mechanism queries where entities participate in processes under conditions.
- Translation between direct binary relations and more expressive event representations.

Implementation notes:

- The tool should not assume one event schema. It should use Vontology role metadata and type constraints.
- Outputs need to make the direct-vs-event-mediated distinction explicit to the LLM.

### 4.9 Contradiction, Compatibility, and Belief-State Tools

Proposed tools:

- `find_claim_conflicts`
- `compare_belief_states`
- `summarise_assertion_truth_state`
- `propose_conflict_resolution_workflow`

Core output:

- mutually incompatible assertions;
- context in which each assertion holds;
- source, confidence, review status, and recency;
- whether the conflict is logical, temporal, terminological, or only apparent;
- candidate next actions: ask user, inspect source, align concepts, create hypothesis context, or mark obsolete.

Useful situations:

- Two papers make incompatible claims.
- A user corrects a previously stored fact.
- A workflow sees both "task is closed" and "task remains open" under different sources.
- A generated commonsense candidate conflicts with curated knowledge.

Implementation notes:

- The tool should report conflicts; workflows decide resolution.
- Do not let answer-generation collapse context-specific contradictions into one unqualified statement.

### 4.10 Analogy, Pattern, and Case Retrieval Tools

Proposed tools:

- `find_structurally_similar_cases`
- `find_analogous_mechanisms`
- `summarise_recurrent_relation_pattern`
- `recommend_reusable_workflow_pattern`

Core output:

- matched structures and bindings;
- predicate/type/frame overlap;
- differences that matter;
- provenance and confidence;
- whether the analogy is asserted, inferred, or generated.

Useful situations:

- A new workflow resembles a prior research-operations workflow.
- A biological mechanism resembles a known pathway pattern.
- A knowledge acquisition task should reuse the question agenda from a similar concept type.
- A role-learning loop needs prior cases before asking the user.

Implementation notes:

- Similarity policy should be represented and inspectable where possible.
- Python can provide graph matching and ranking primitives, but not domain-specific hidden scoring doctrine.

### 4.11 Authority-Surface and Drift Audit Tools

Proposed tools:

- `audit_authority_surface_for_concept`
- `trace_prompt_workflow_authority`
- `find_repo_policy_drift_candidates`
- `summarise_workflow_tool_contracts`

Core output:

- authoritative concept ids for prompts, workflows, predicates, and policy artefacts;
- repo-side seeds/snapshots and whether they are only bootstrap material;
- code paths that appear to contain semantic policy;
- missing Vontology artefacts required by a workflow;
- recommended Jira/refactor tasks when authority drift is discovered.

Useful situations:

- Before changing prompt, routing, workflow, or grounded retrieval behaviour.
- During staleness reviews of old Jira tasks.
- When tests appear to pin removed heuristic behaviour.
- When a workflow failure is actually a missing represented artefact.

Implementation notes:

- This is a meta-tool family, but it fits Von's architecture: represented authority is a runtime dependency, not a documentation preference.
- Outputs should be suitable for Jira comments and implementation planning.

### 4.12 Ephemeral Theory Promotion Tools

Proposed tools:

- `list_ephemeral_theories_for_turn`
- `promote_ephemeral_theory_candidate`
- `discard_ephemeral_theory`
- `trace_theory_promotion_history`

Core output:

- temporary hypotheses generated during a turn or workflow;
- evidence and reasoning steps;
- conflicts and missing validation;
- proposed durable assertion bundle;
- promotion/discard decision record.

Useful situations:

- TextLearner-like reading creates competing interpretations of a passage.
- A workflow uses a temporary model of a user's intent and later needs to keep or discard it.
- A scientific paper reading path proposes claims that require human or tool validation.
- Long-horizon learning needs to avoid losing useful intermediate theories while also avoiding unreviewed KB pollution.

Implementation notes:

- Ephemeral hypotheses need explicit lifetime, context, owner, and promotion gates.
- Promotion must be a write workflow with validation and provenance.

## 5. Prioritised Roadmap

### Near Term

1. `get_typed_predicate_incidence` or equivalent extension to `get_predicate_incidence`.
2. `trace_assertion_provenance` and `get_supporting_evidence_bundle`.
3. `query_relations_in_context` with explicit context lineage.
4. `validate_concept_against_shape` with Vontology-authored shapes.
5. `find_event_frames_by_participant` for Davidsonian role-frame retrieval.

These directly improve the current failure class around entity-relative information requests and make 1894-style tests less dependent on broad semantic search.

### Medium Term

1. `suggest_knowledge_acquisition_questions`.
2. `suggest_concept_placement` and `align_external_identifier`.
3. `materialise_scientific_claim`.
4. `find_claim_conflicts`.
5. `summarise_type_relation_profile`.

These shift Von from retrieval to active knowledge growth and scientific memory.

### Later

1. `find_structurally_similar_cases`.
2. `build_mechanism_skeleton`.
3. `compare_mechanism_models`.
4. `promote_ephemeral_theory_candidate`.
5. `audit_authority_surface_for_concept`.

These support higher-order self-improvement, mechanistic scientific reasoning, and role-learning loops.

## 6. Evaluation and Acceptance Strategy

Each new tool family should be validated through the real gateway path, not only service-level unit tests.

Minimum acceptance pattern:

- Invoke through `InternalMCPGateway.invoke()` and the stdio MCP surface when externally exposed.
- Add focused service tests for edge cases and schema validation.
- Add orchestration-path tests where selector/planner behaviour depends on the tool.
- Use replay-style turns modelled on the 1894 class of failures:
  - "What papers are mine?"
  - "Which projects is this person affiliated with?"
  - "What evidence supports this claim?"
  - "What does this paper say under its own claims context versus what our lab believes?"
  - "Which event frames connect this entity to this process?"
- Inspect turn telemetry for:
  - tool chosen;
  - context lineage;
  - predicate/type filters;
  - evidence rows;
  - excluded candidates and exclusion reasons;
  - answer-first final response rather than execution bookkeeping.

Success criteria:

- The LLM can choose a narrow Vontology tool when the entity is known.
- The answer can cite structured evidence and provenance.
- Context-sensitive claims are not flattened into global facts.
- Davidsonian/event-mediated relations are retrievable without hand-authored per-domain Python.
- Missing knowledge leads to useful KA questions or explicit uncertainty, not hallucinated completion.

## 7. Security and Access Constraints

All tools must preserve Von's namespace and authority model:

- Count summaries must not leak the existence of private concepts or contexts.
- Provenance output must respect source-level access controls.
- Write or promotion tools must fail closed and use explicit workflow escalation for destructive or authority-changing mutations.
- Tool payloads must avoid printing secrets or private prompt bodies unless the caller is authorised and the workflow requires it.
- Generated commonsense and extracted scientific hypotheses must be marked as candidates until validated and promoted.

## 8. Design Guidance for Implementation Tickets

When turning a proposed tool into Jira work, include:

- the canonical Vontology concepts, predicates, shapes, contexts, and workflows that should become authoritative;
- the exact existing service and MCP surfaces to extend;
- the read/write authority boundary;
- the telemetry contract;
- the LLM-facing shaped payload;
- real-path validation scenarios;
- examples covering both direct binary relations and event/frame-mediated Davidsonian relations;
- source/provenance expectations;
- the user-visible improvement in plain language.

The implementation should not smuggle domain-specific scoring, relation lists, or prompt bodies into Python. If a reusable primitive is missing, add the primitive and keep authored policy in Vontology/VWL.

## 9. Sources Consulted

- Witbrock et al., "Knowledge Begets Knowledge: Steps towards Assisted Knowledge Acquisition in Cyc", AAAI Spring Symposium KCVC 2005. <https://mdsoar.org/items/baffdcf7-62b7-4315-909d-7242f5cd60b1>
- Witbrock et al., "An Interactive Dialogue System for Knowledge Acquisition in Cyc", IJCAI 2003 Workshop on Mixed-Initiative Intelligent Systems. <https://paperzz.com/doc/8421381/an-interactive-dialogue-system-for-knowledge-acquisition-...>
- Curtis et al., "Methods of Rule Acquisition in the TextLearner System", AAAI Spring Symposium 2009. <https://cdn.aaai.org/Symposia/Spring/2009/SS-09-07/SS09-07-005.pdf>
- Witbrock et al., "Cyc and the Big C: Reading that Produces and Uses Hypotheses about Complex Molecular Biology Mechanisms", AAAI Workshop 2015. <https://cdn.aaai.org/ocs/ws/ws0113/10177-46009-1-PB.pdf>
- Wielinga et al., "The CommonKADS Framework for Knowledge Modelling", 1992. <https://research.utwente.nl/en/publications/the-commonkads-framework-for-knowledge-modelling/>
- Musen and the Protege Team, "The Protege Project: A Look Back and a Look Forward", AI Matters 2015. <https://pubmed.ncbi.nlm.nih.gov/27239556/>
- W3C, "Shapes Constraint Language (SHACL)", W3C Recommendation 2017. <https://www.w3.org/TR/shacl/>
- W3C, "PROV-O: The PROV Ontology", W3C Recommendation 2013. <https://www.w3.org/TR/prov-o/>
- Smith et al., "The OBO Foundry: coordinated evolution of ontologies to support biomedical data integration", Nature Biotechnology 2007. <https://www.nature.com/articles/nbt1346>
- Groth, Gibson, and Velterop, "The anatomy of a nanopublication", Information Services and Use 2010. <https://journals.sagepub.com/doi/10.3233/ISU-2010-0613>
- Demir et al., "The BioPAX community standard for pathway data sharing", Nature Biotechnology 2010. <https://www.nature.com/articles/nbt.1666>
- Finney and Hucka, "Systems biology markup language: Level 2 and beyond", Biochemical Society Transactions 2003. <https://pubmed.ncbi.nlm.nih.gov/14641091/>
- Speer, Chin, and Havasi, "ConceptNet 5.5: An Open Multilingual Graph of General Knowledge", AAAI 2017. <https://arxiv.org/abs/1612.03975>
- Sap et al., "ATOMIC: An Atlas of Machine Commonsense for If-Then Reasoning", AAAI 2019. <https://arxiv.org/abs/1811.00146>
- Bosselut et al., "COMET: Commonsense Transformers for Automatic Knowledge Graph Construction", ACL 2019. <https://aclanthology.org/P19-1470/>
- Lenat, "CYC: A large-scale investment in knowledge infrastructure", Communications of the ACM 1995. <https://cacm.acm.org/research/cyc/>
- Buvac, "Formalizing Context" resource page, including links to McCarthy and Guha context work. <https://www-formal.stanford.edu/buvac/>
