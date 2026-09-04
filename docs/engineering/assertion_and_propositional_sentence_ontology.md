# Assertion and Propositional-Sentence Ontology

- **Kind:** Design and implementation boundary
- **Lifecycle:** Active; minimal public vocabulary published, assertion-record
  integration pending
- **Authority:** Selected target model and phased delivery plan under
  `AGENTS.md`; live Vontology, current code, and live Jira remain authoritative
  for present behaviour
- **Authority scope:** Text and logical assertions, propositions, logical
  sentences, contextual adoption, theory membership, theoremhood, and source
  document anchoring
- **Owner:** Von maintainers
- **Last reviewed:** 3 September 2026
- **Evidence as of:** Repository `fca0d2241ebb39346a1386d660744bb2e92eb02e`,
  Mac-hosted Von build `fa3cad70e990d533600c1276f575c65e2b9e83d5`,
  and canonical Atlas Vontology reads on 3 September 2026
- **Review trigger:** Publication of the first assertion vocabulary, adoption of
  a common assertion record, general logical-sentence storage, or a first-class
  context/theory query model

## 1. Purpose and selected direction

Von currently has useful assertion behaviour without a common assertion
ontology. Exact natural-language occurrences live in the scoped assertion
store, actor-scoped binary relations use a different record shape, canonical
ground relations live as values in concept relationship arrays, uncertain and
testing-theory assertions have further local representations, and paper
recommendations are reified as domain-specific Vontology concepts.

The selected direction is:

> Every assertion is a first-class, typed Vontology knowledge item. Text
> assertions and logical sentences are the parallel specialisations of an
> `Assertion` type beneath a new propositional-sentence bridge. `Logical
> Sentence` is also a `Logical Form`; a current ground binary predication is
> only one narrow specialisation. Assertion context, provenance,
> audience/authority, publication, lifecycle, and physical storage remain
> independent.

“First-class Vontology knowledge item” does not initially mean “ordinary
concept document”. The first implementation should give assertion records
stable identity, direct Vontology type IDs, type-extent participation, canonical
read-back, and the ability to be referenced by other knowledge items. It should
not duplicate every assertion into `concepts`, make two stores independently
authoritative, or recursively reify the metadata which types the assertion.

This design deliberately leaves text admission lightweight and logical
expressivity open. Neither a natural-language assertion nor a logical assertion
is assumed to be atomic, binary, first-order, non-modal, or otherwise limited
to the current knowledge-graph representation.

## 2. User and research outcome

The target user-visible outcome is that Von will be able to answer, inspect,
and reason over “what is asserted?” across exact text and formal
representations without presenting storage accidents as ontology:

- an exact user or source statement is stored immediately and is visibly an
  instance of `Text Assertion`;
- a canonical or scoped ground predicate is visibly an instance of `Ground
  Binary Predication` and therefore of `Ground Logical Sentence`, `Logical
  Sentence`, and `Assertion`;
- richer formal sentences can later be stored without being squeezed into
  subject-predicate-object fields;
- assertions retain the context in which they are put forward and do not
  become context-free truth merely because they are formal;
- text and logical forms are aligned only through an explicit, attributed
  interpretation or proposition identity; and
- papers, paragraphs, narratives, theories, and conversations can carry or
  organise assertions without being conflated with assertion context,
  endorsement, or truth.

The research outcome is a fair seam for comparing exact-text retrieval, graph
retrieval, richer logical representation, and context-sensitive reasoning. The
ontology must not predetermine that richer representation is always useful.

## 3. Starting point and live state

### 3.1 Live ontology

Before this design was accepted, the live public Vontology contained:

- `#V#propositional_information_thing`, directly below `#V#intangible`;
- `#V#logical_form`, `#V#theorem`, `#V#proof`, `#V#document`, and
  `#V#propositional_information_artefact` beneath propositional information;
- `#V#sentence` and `#V#paragraph` as document-structural types below
  `#V#sub_document`; and
- `#V#theory`, `#V#ephemeral_theory`, and `#V#context` on existing but not yet
  assertion-integrated branches.

The minimal public hierarchy in section 5.1 was published through the governed
Vontology mutation surface on 3 September 2026 and canonically read back from
Atlas. `#V#paper_recommendation_assertion` remains a useful reification
precedent, but still sits outside this propositional-information hierarchy and
represents a mutable recommendation record rather than the generic assertion
model selected here.

An existing `#V#claim` identifier is also visible through legacy search
surfaces. Search hierarchy places it under `#V#product`, while its fetch/type
projections are inconsistent, and its scholarly-workflow use does not
establish the generic assertion meaning selected here. It was neither renamed,
reparented, nor reused in this slice. Any later mapping from `claim_id` to a
Proposition item must first audit that live identifier and its consumers.

The existing names were treated as evidence to reuse rather than sufficient
definitions. Their live descriptions, instances, and parents were read back
before publication of the new parent links.

### 3.2 Current code and storage

The scoped store already supplies most of the hard operational properties for
text occurrences: exact body preservation, stable `ska_*` identity, actor-bound
scope, provenance, lifecycle, canonical read-back, optional concept links, and
durable revision-aware RAG maintenance. Its `standalone_text` record has no
Vontology type identity, however, while the older relation record has
subject-predicate-object fields but no assertion context and no logical-form
contract.

Canonical concept-to-concept assertions are still array entries at
`concepts.relationships.<predicate>`. The write path uses set insertion and
returns no durable edge ID. Read paths synthesise locators containing array
positions, so those values cannot become logical-sentence or assertion
identity. Structural inverse rows may or may not both be stored, and their
physical multiplicity must not create two semantic assertions.

The live Jira boundary also matters:

- `JVNAUTOSCI-343` is `In Progress` and already distinguishes exact assertion
  occurrence (`assertion_id`), proposition-like claim (`claim_id`), formal
  assertion, text value, text relation, and note;
- `JVNAUTOSCI-2038` is `To Do` and owns general context/microtheory querying,
  inclusion, lineage, and conflict behaviour.

This design extends those decisions. It changes the earlier assumption that
assertion typing could remain merely implicit, but it does not require every
claim/proposition to become an ordinary concept.

## 4. Ontological distinctions

### 4.1 Sentence, assertion, proposition, and act

Use four distinct notions:

| Notion | Meaning in Von | Initial representation |
|---|---|---|
| Propositional sentence | A complete propositional information item which may be put forward, denied, queried, proved, or merely considered | Vontology type; text and logical branches |
| Assertion | A propositional sentence put forward as holding in one explicit or documented implicit context | Typed assertion record with stance, context, lifecycle, and provenance |
| Proposition | A formulation-independent, truth-evaluable content identity shared only when equivalence has been established | Optional later `claim_id`/proposition item |
| Assertion act | The event in which an agent states, adopts, publishes, extracts, or infers an assertion | Existing source-event/provenance fields initially; a first-class event only when a consumer needs it |

An assertion is not automatically true. `asserted` records a contextual stance,
not verification, theoremhood, publication, or universal endorsement.

The first implementation intentionally permits a shortcut from an assertion
record to its exact text or logical body without requiring a separate
proposition node or assertion-act node. The identities remain separable so that
those nodes can be introduced without changing what an existing `assertion_id`
meant.

### 4.2 Text assertion and logical sentence

A text assertion is an assertion whose asserted form is exact natural-language
text. It is parallel to a logical sentence; it is not a logical form. The body
may be a fragment, one grammatical sentence, several sentences, or a longer
passage, and may express quantification, modality, functions, temporality,
rules, higher-order claims, or other complexity which Von has not formally
analysed. `Propositional Sentence` names the semantic information unit here,
not an orthographic sentence. Absence of a formalisation means only “not
formally represented”.

If one admitted passage expresses several independently useful claims, preserve
that exact source assertion and derive separately identified, explicitly
aligned assertions when a consumer needs segmentation. Admission must not
silently pretend that analysis has already found a unique atomic claim.

A logical form is any expression in an identified formal or quasi-formal
language. In this first Von hierarchy, `Logical Sentence` denotes the asserted,
closed, proposition-bearing case: it has no free variables, but may contain
bound variables and quantifiers. Other logical forms may include terms, open
formulae, lambda expressions, queries, templates, or partial semantic
representations. The customary neutral syntactic category of a closed but
unasserted formula is deliberately deferred. If Von later needs it, add a
neutral `Closed Logical Form` and a `Logical Assertion` intersection rather
than weakening what an existing assertion ID means.

A ground logical sentence contains no variables at all—free or bound—and no
unresolved metavariables or placeholders, relative to its logic. “Ground” does
not mean atomic, binary, function-free, non-modal, or simple: `P(a) ∧ Q(f(b))`
and `□P(a)` can be ground, while `∀x P(x)` is closed but not ground. A ground
binary predication is the currently needed atomic arity-two special case.

Quantified, modal, temporal, higher-order, rule-like, and functional features
are composable. They must not be modelled as one disjoint enumeration in which
choosing one prevents the others. Introduce a specific subtype or syntax
feature only when a parser, reasoner, query, renderer, or evaluation consumes
the distinction.

### 4.3 Assertion occurrence and reusable content

The existing standalone `assertion_id` remains a contextual source occurrence.
Identical wording from two source events remains two assertions. A scoped
ground relation currently has set-like identity and must not silently overwrite
or impersonate source-occurrence evidence.

The following identities therefore remain distinct:

- `assertion_id`: one situated assertion occurrence or standing contextual
  adoption, with lifecycle and provenance;
- exact text or logical-form value, which is content carried by an assertion
  but not its occurrence, context, or provenance;
- logical expression key: a stable normalised key for one formal sentence in
  one logical language, independent of assertion context and never by itself
  evidence of formulation-independent semantic equivalence;
- standing assertion key: a set-like adoption identity derived from an
  expression, direct context, and asserting authority where the underlying
  surface represents standing knowledge rather than a source occurrence;
- `claim_id` or future proposition ID: an optional shared semantic identity
  created only after cross-form or cross-occurrence equivalence is established;
- source event or assertion-act ID: the event which produced, adopted, or
  revised the assertion; and
- physical record, revision, and derived-index identities, which remain
  storage lineage rather than semantic identity.

Text similarity, matching triples, model extraction, translation, or a shared
document does not silently establish proposition identity.

### 4.4 Object-language content and assertion qualifiers

Keep features of the asserted sentence distinct from facts about its assertion
record. In particular:

- object-language negation is not retraction or denial of the assertion record;
- a modal or epistemic operator in a sentence is not assertion confidence or
  adoption status;
- a time referred to by a sentence is not its source-event, ingestion,
  publication, or lifecycle time; and
- a quantifier or higher-order operator is not a Vontology type-extent query.

An adapter may expose explicit correspondences when a named logic gives them
precise semantics. It must not manufacture those correspondences from similar
field names.

## 5. Type architecture

### 5.1 Minimal first hierarchy

The published public hierarchy is:

```text
#V#propositional_information_thing                         existing
└── #V#propositional_sentence                             new
    └── #V#assertion                                      new
        ├── #V#text_assertion                             new
        └── #V#logical_sentence                           new
            └── #V#ground_logical_sentence                new
                └── #V#ground_binary_predication          new

#V#logical_form                                           existing
└── #V#logical_sentence                                   second parent

#V#document                                               existing carrier branch
└── #V#sub_document
    ├── #V#sentence                                       existing text/document occurrence
    └── #V#paragraph                                      existing text/document occurrence
```

The published definitions are:

| Type | Definition |
|---|---|
| `#V#propositional_sentence` | A propositional information thing presenting a complete propositional content that can be asserted, denied, queried, proved, or considered. It is a semantic information unit, not necessarily one grammatical sentence or a logical form. |
| `#V#assertion` | A propositional sentence put forward as holding in an explicit or documented implicit context. Assertion records a contextual stance; it does not by itself imply truth, verification, canonical publication, or theoremhood. |
| `#V#text_assertion` | An assertion whose asserted form is preserved as exact natural-language text. Its form may be a fragment, one grammatical sentence, several sentences, or a longer passage, and may express arbitrarily complex content without thereby being a Logical Form. |
| `#V#logical_sentence` | An assertion expressed as a closed formula in an identified formal or quasi-formal language. It has no free variables, although it may contain bound variables and quantifiers, and is also a Logical Form. |
| `#V#ground_logical_sentence` | A logical sentence containing no free variables, bound variables, unresolved metavariables, or placeholders relative to its logic. Groundness does not imply atomicity, binarity, function-freedom, or absence of modal or temporal operators. |
| `#V#ground_binary_predication` | An atomic ground logical sentence in which one binary predicate is applied to two ordered ground arguments. It is the narrow form projected by current binary Vontology relations, not the general assertion representation. |

`#V#logical_sentence` has both `#V#assertion` and `#V#logical_form` as direct
parents. The first says that this particular closed form is put forward in a
context; the second says that its body is expressed in an identified logic.

`Ground Binary Predication` is preferred to “Ground Binary Predicate
Assertion”: the predicate is the operator, while a predication is its
application to arguments. The latter can remain a display alias if existing
language or clients require it. Its meaning is an atomic arity-two logical
sentence with ground, ordered arguments. The planned adapter over the current
canonical graph should project only positive instances. Negation of such a
predication remains a wider Ground Logical Sentence unless a later consumer
justifies a signed-literal subtype; polarity belongs in the logical
representation rather than in ad hoc class names.

Do not add `Ground Atomic Logical Sentence` merely to complete a taxonomy. It
can be inserted later between Ground Logical Sentence and Ground Binary
Predication if an atomic-versus-compound query, renderer, or reasoner consumes
the distinction. Future negative, n-ary, rule, modal, temporal, quantified,
functional, or higher-order cases belong under the widest adequate
logical-sentence type, not underneath the binary class.

### 5.2 Published state and receipts

Canonical read-back confirms that all six concepts are global public types,
have the exact descriptions in this design, and have no user or organisation
publication restriction. Exact-ID searches return one concept for each stable
ID. The governed dependent writes completed with these receipts:

| Effect | Effect ID | Authority receipt ID |
|---|---|---|
| Create `#V#assertion` | `effect_2896e03f9a16801192671228` | `omr_08c2ec3da2c56e234f95126715095e0b5b675589eefa0d14db085a80d3991a13` |
| Create `#V#text_assertion` | `effect_e9bb9c5fa2ff438d3bc3bde1` | `omr_d612c3844f419dbe108431f3b2ccfc7e6cc5aac12b45db54a2640e74d9af5361` |
| Create `#V#logical_sentence` | `effect_d3acfedc2df18f5fda916df4` | `omr_10107237be341a2d56ce1eaa10f1ab30919310fe464a36db99d60f931c56f69c` |
| Create `#V#ground_logical_sentence` | `effect_c8d257d0c7705d919ede7513` | `omr_6162c7b7cc63fa70a73532b72948563cf7e073c218ba4ea5bbd72ded169c9279` |
| Create `#V#ground_binary_predication` | `effect_34d40ec2931cac9c38b5702c` | `omr_94b21192aac97af3634d993110b08672aa2c5509ca395a483c9350d12cddbdd5` |
| Add `#V#logical_sentence is_a_type_of #V#logical_form` | `effect_bd46a9f04c9f486f023bb78c` | `omr_1b76d44a22a2ff231f868a46bb7b424ec24c7a27cb0a065795098eebd7e6dbda` |

`#V#propositional_sentence` was created and repaired through the visible
authenticated concept editor before the serial governed run. The initial
create committed the concept with composite user-and-organisation visibility
but returned the indeterminate postcondition-failure receipt
`omr_JDCan1WT8Goe2oD1-KHduf1Q`. The two explicit scope repairs then succeeded:
`omr_1d85526460d5c15d87b79352b37c0884bcc8a225159554c2f2ea8a0890ec91b9`
for composite-to-user and
`omr_6f7e4309695b5020d18d68af8b2987a186234a0104455ad67bd7e6c852bd8440`
for user-to-global. Its description upsert also committed but returned the
indeterminate postcondition-failure receipt
`omr_oN7aaAlhmraXdWY8ulwDyddk`. No ambiguous write was blindly retried:
completion is based on subsequent independent canonical read-back. The final
record is global, has the intended parent and exact description, and has no
publication restriction.

The verified direct edges are:

```text
#V#propositional_sentence is_a_type_of #V#propositional_information_thing
#V#assertion is_a_type_of #V#propositional_sentence
#V#text_assertion is_a_type_of #V#assertion
#V#logical_sentence is_a_type_of #V#assertion
#V#logical_sentence is_a_type_of #V#logical_form
#V#ground_logical_sentence is_a_type_of #V#logical_sentence
#V#ground_binary_predication is_a_type_of #V#ground_logical_sentence
```

The existing `#V#theorem is_a_type_of
#V#propositional_information_thing` and `#V#sentence is_a_type_of
#V#sub_document` edges remain unchanged. This publication is vocabulary only:
no existing scoped assertion, relationship row, text relation, claim, theorem,
or assertion occurrence was retyped or copied. A secondary incoming-edge/type
extent read briefly omitted the new Text Assertion child while its canonical
forward edge was already present; exact fetch and later independent hierarchy
searches establish the intended edge, but consumers should not treat every
secondary index as transactionally synchronous with canonical state.
At final recheck the hierarchy helper enumerated all expected children, but its
compact rows labelled them `kind=individual`; canonical concept fetches and
exact type-filtered searches label the same IDs `kind=type`. Treat that helper
field as a read-projection defect, not as evidence that the live concepts have
individual rather than type identity.

### 5.3 Why not use the existing `#V#sentence` as the common parent?

The live `#V#sentence` is presently a sub-document: a linguistic/document
occurrence which can be located inside a paragraph or paper. A logical sentence
is not a sub-document, and an exact text assertion may be a fragment or several
grammatical sentences before optional analysis.

The new `#V#propositional_sentence` therefore carries the semantic “complete
propositional information unit” meaning. Existing document sentences may
`express` or anchor an assertion or proposition. If a real consumer later needs
the identity intersection, add `Sentential Text Assertion` as a subtype of both
`Text Assertion` and the existing document `Sentence`; do not reparent every
text assertion or every existing sentence in the first slice.

### 5.4 Theorem and proof

The existing `#V#theorem` is relevant but should not be reparented in the first
slice. Theoremhood is a role or status relative to a theory, logic, assumptions,
and proof standard; a theorem may have textual and logical formulations and
need not be ground or first-order. Its current description and extent must be
audited before deciding whether it denotes a proposition, a sentence, a
theory-qualified role, or a mixture of these.

Theoremhood is stronger and differently shaped than assertion status:

- an axiom may be asserted in a theory without being a theorem;
- a conjecture or hypothesis may be asserted for consideration without being
  proved;
- the same sentence may be a theorem in one theory and not in another; and
- `epistemic_status=asserted` must never cause automatic theorem typing.

The later theory slice should represent `is theorem in theory` and its proof or
derivation explicitly. A global `is_an_instance_of Theorem` must not substitute
for that qualified relation. Only add a theorem-sentence subtype if a concrete
consumer and the live extent establish that it is semantically sound. Until
then, no new code should infer theoremhood.

## 6. First-class assertion record

### 6.1 Logical model

The common assertion envelope should expose at least:

```text
assertion_id
schema_version
direct_type_concept_ids[]
assertion_context
scope / audience
canonical_publication
lifecycle_status and revision
epistemic or adoption status
asserted_form
provenance
source_anchor?
claim_id? / proposition_id?
alignment_links[]?
derived_index_state?
```

`direct_type_concept_ids` names the most specific directly asserted Vontology
types. Type closure is computed through Vontology ancestry. Multiple direct
types remain possible for independently useful facets.

The asserted form is a tagged union, not a universal triple:

```json
{
  "kind": "text",
  "text": "exact source text",
  "language": "en-NZ"
}
```

or:

```json
{
  "kind": "logical",
  "logical_language_concept_id": "#V#...",
  "language_version": "...",
  "serialisation": "...",
  "ground_atom_projection": {
    "predicate_concept_id": "#V#...",
    "arguments": ["#V#arg_1", "#V#arg_2"]
  }
}
```

The ground-atom projection is optional and indexed when available. It is not
the general logical representation. A richer logical sentence may have a
validated abstract syntax tree, an external-language serialisation, both, or a
reference to an immutable logic artefact. The language and version are
required whenever a formal string is stored; syntax without its language is
not an interpretable logical form.

### 6.2 A typed knowledge item, not necessarily a concept document

The initial `is_an_instance_of` semantics should be implemented by the
assertion record's direct Vontology type IDs and the common Vontology type
extent/query layer. A read of `#V#assertion` or any supertype should include
authorised assertion records alongside ordinary concept instances, labelled by
item kind and storage lineage.

This avoids three immediate defects:

1. duplicating every assertion into the ordinary concept collection;
2. making a concept copy and its existing assertion store compete as truth;
3. infinite automatic reification, because the `is_an_instance_of` metadata
   which types an assertion would itself otherwise demand another assertion
   node, whose typing would demand another, and so on.

Typing, context, provenance, and form fields in the assertion envelope are
meta-representation plumbing and are not recursively reified by default. If a
particular meta-assertion later needs its own provenance, disagreement, or
context, it may be explicitly promoted as a new assertion. Reflection is
finite and opt-in, not universal.

The common item resolver should permit an assertion ID to be the source or
target of supported assertion-level relations even when it is not a
`#V#...` concept ID. Do not mint a shadow concept merely to satisfy an old
concept-only function signature; extend the bounded resolver when an actual
consumer requires cross-item reference.

## 7. Mapping current assertion surfaces

| Current surface | Assertion interpretation | Initial direct type | Authority after integration |
|---|---|---|---|
| `standalone_text` scoped record | Exact situated natural-language assertion | `#V#text_assertion` | Existing scoped assertion row |
| Scoped concept-valued subject-predicate-object record | Contextual ground atom | `#V#ground_binary_predication` | Existing scoped assertion row |
| Scoped subject-predicate-text record | Ground binary predication with a literal argument, not standalone text | `#V#ground_binary_predication` | Existing scoped assertion row |
| Canonical `concepts.relationships` entry | Positive ground binary predication in the base publication context | `#V#ground_binary_predication` | Existing canonical relationship until an explicit cutover |
| Base `TextRelation` | Ground predication with a language-tagged literal; names, notes, and descriptions are not standalone claims merely because the object is text | `#V#ground_binary_predication` when exposed as an assertion | Existing TextRelation authority |
| Uncertain relationship row | Candidate/tentative ground logical sentence | `#V#ground_binary_predication` plus qualified epistemic status | Existing uncertain-assertion owner until adapted |
| Testing-theory embedded assertion | Theory-local text assertion or logical sentence, depending on its body | Appropriate assertion subtype | Existing testing-theory state until adapted |
| Paper recommendation assertion concept | Domain-specific recommendation record which may later specialise Assertion after semantic audit | No automatic reparent in the first slice | Existing recommendation service |

Adding another independent assertion store for each row is not the goal. The
common service and read contract adapt these current authorities, then permit
later cutovers one surface at a time.

## 8. Stable identity for canonical ground assertions

Current `struct::<source>::<predicate>::<array-position>` values are view
locators and change when array order changes. They must not become assertion or
logical-sentence IDs.

For a base ground atom, derive a stable logical expression key from:

- logical language and version;
- polarity;
- canonical predicate concept ID; and
- ordered, typed arguments.

Then derive the set-like `standing_assertion_key` from that expression key, the
direct base-publication context, and the applicable asserting/publication
authority. The expression key deliberately excludes context; neither key is a
formulation-independent proposition ID.

For structural predicate/inverse pairs, use ontology-governed preferred
orientation so `is_an_instance_of(a, T)` and its stored inverse
`has_instance(T, a)` project as one ground logical sentence. Whether an inverse
row is physically present is a storage detail.

The initial base-graph assertion catalogue should be a rebuildable,
revision-aware materialised projection of `concepts.relationships` and
TextRelations. It gives stable assertion IDs and type-extent participation, but
the existing graph remains the sole semantic authority. Relationship writes
update or invalidate the projection through the current mutation/event seam;
a reconciler repairs missed derived work. If a later release makes typed
assertion records authoritative and derives legacy edge arrays from them, that
must be an explicit one-way authority cutover, not indefinite dual-write
ambiguity.

Removing and later re-adding the same set-like base assertion may reactivate
the same standing assertion identity. Individual mutation or assertion acts
remain separate provenance events. Scoped source occurrences keep their
existing occurrence identities.

## 9. Contexts, theories, and theoremhood

### 9.1 Context

Every assertion has exactly one direct assertion context in the initial
contract. Existing safely selected implicit intake contexts may remain, but
their records should gain a context-kind reference and must not pretend that
audience scope and context are identical.

The base canonical graph is also a context: the base publication context. It
is not “the context-free truth”.

Resolve visibility first, independently of context inclusion. Then compute the
semantic view over authorised contexts. Context inclusion must never reveal
the existence, counts, identifiers, or contents of an inaccessible context.

Do not implement inheritance, precedence, contradiction resolution, or lifting
merely to add the first type IDs. Those semantics remain with
`JVNAUTOSCI-2038` and should be introduced when a real query must distinguish
direct, inherited, hypothetical, paper-local, project, lab, or user-private
knowledge.

### 9.2 Theory and microtheory

A theory is an organised propositional information object containing or
governing sentences, assumptions, axioms, hypotheses, rules, theorems, and
proofs. A context is the environment in which assertions are adopted,
considered, inherited, or queried. They are related but not automatically the
same thing.

Recommended relations for the later theory slice are:

- theory has member sentence/assertion, qualified by role such as axiom,
  hypothesis, definition, observation, or theorem;
- theorem is theorem in theory and is supported by a proof or derivation;
- context uses or adopts theory;
- context includes context through an explicit, directed, provenance-bearing
  relation; and
- lifting between contexts is explicit and does not follow from collection or
  namespace membership.

A microtheory may deliberately be both a theory and a context if Von selects
that convention. Do not make every theory a context solely because Cyc-style
microtheories combine those roles.

### 9.3 Conversation situations

A conversation situation is a lightweight provisional theory/carrier. It may
provide referent bindings, assumptions, questions, and effect observations
needed to interpret a text assertion. Importing an assertion from it does not
turn the whole situation into canonical knowledge or authority. The source
conversation and the durable assertion retain separate identities and
provenance.

## 10. Narratives, paragraphs, papers, and source passages

Documents and discourse structures carry assertions; containment does not
assert their contents.

| Structure | Semantics |
|---|---|
| Paper/document | A versioned carrier which may present claims, theories, evidence, questions, quotations, and narratives |
| Paragraph/sentence occurrence | A located structural part of a document; useful as a source anchor |
| Narrative | An ordered discourse object which may present, attribute, contrast, deny, or quote assertions; membership is not logical conjunction |
| Theory | A structured body of sentences and inferential roles; not identical to the document which describes it |
| Assertion context | The setting in which an assertion is adopted or considered; not identical to document containment |

An assertion extracted from a paper should be anchored to a stable document
version and passage selector where available. Its attribution role must
distinguish at least author assertion, quotation, reported assertion,
discussion, denial, question, and extractor interpretation when those
differences affect use. A paper-local claim context can collect what the paper
presents without converting those claims into lab belief, user belief, or base
publication.

Paragraph and sentence identity should remain stable relative to a document
version. Page/character offsets alone may be compatibility selectors; they are
not cross-version semantic identity.

Narrative ordering will eventually require an ordered membership record or
sequence position. A set-valued `contains` edge is insufficient when order and
rhetorical role matter. This is not required for the first assertion slice.

## 11. Text-to-logic alignment and propositions

Formalisation creates a distinct, attributed candidate logical sentence. It
does not mutate or replace the source text assertion, and it does not publish a
ground relation merely because a model produced valid syntax.

Use explicit links such as:

- logical sentence formalises text assertion;
- assertion expresses proposition;
- assertion supports, challenges, translates, paraphrases, or quotes another
  assertion; and
- proposition has textual or logical formulation.

The exact predicate vocabulary should be published only with its first
consumer. The semantic rules are immediate:

- similar wording is not equivalence;
- a logically stronger or weaker sentence is not an equivalent formulation;
- extraction is provenance, not adoption;
- valid syntax is not truth or publication;
- a confirmed alignment preserves both assertion IDs and both provenance
  records; and
- retraction of one occurrence, form, or alignment does not silently retract
  the others.

`JVNAUTOSCI-343`'s `claim_id` is the natural compatibility route to an optional
proposition identity. Do not require one for exact text admission or every
ground atom. When a stable cross-form identity is needed, either make
`claim_id` the API alias of a Vontology Proposition item or record an explicit
mapping; do not maintain two unexplained semantic IDs.

## 12. Delivery plan

### Phase 0 — accept vocabulary and reconcile planning surfaces

1. Review this type graph with live definitions and type extents for
   `Propositional Information Thing`, `Logical Form`, `Theorem`, `Proof`,
   `Sentence`, `Paragraph`, `Theory`, and `Context`.
2. Confirm the exact public names and descriptions, especially the local
   meaning of `Propositional Sentence` and the contextual meaning of
   `Assertion`.
3. Update `JVNAUTOSCI-343` so every assertion record must have Vontology type
   identity; retain its existing occurrence/claim/formalisation distinctions.
4. Update `JVNAUTOSCI-2038` to consume the common assertion identity rather
   than inventing context-local assertion rows.
5. Create a separate bounded implementation task only if ontology publication
   and common-envelope work cannot be delivered coherently under those current
   owners.

The names and semantics were accepted, the live hierarchy was audited, and the
minimal vocabulary was published. Jira reconciliation remains part of normal
handoff rather than a second ontology decision.

### Phase 1 — publish the minimal ontology and type scoped assertions

1. **Complete:** publish the minimal types in section 5 through the governed
   Vontology path with exact canonical read-back.
2. Add direct type IDs and an explicit context to the common scoped assertion
   envelope while preserving all current `ska_*` IDs, exact bodies, scopes,
   provenance, revisions, tombstones, and RAG state.
3. Map `standalone_text` to `Text Assertion` and existing scoped relation rows
   to `Ground Binary Predication`.
4. Extend exact/list reads and type extents to expose type closure and item
   kind without exposing inaccessible assertion IDs or counts.
5. Keep the old `assertion_form` and subject/predicate/object projections as
   compatibility fields; new semantics come from the typed envelope.
6. Backfill idempotently and resumably. Malformed legacy rows remain visible to
   authorised diagnostics with a typed migration state; they are not silently
   discarded or guessed.

This is the smallest independently useful release: exact text and current
scoped ground relations become real Vontology assertion instances.

### Phase 2 — connect canonical ground relations

1. Define canonical ground-atom keys and inverse-orientation rules using
   represented predicate metadata.
2. Build the rebuildable typed assertion projection for canonical
   relationships and TextRelations.
3. Expose a common `get assertion` and bounded `query assertions` contract over
   scoped text, scoped ground atoms, canonical ground atoms, and TextRelations.
4. Add type, context, argument, predicate, lifecycle, provenance, and storage
   lineage filters. Preserve exact-match, concept-link, semantic-text, and
   inherited match types rather than flattening them.
5. Replace array-position relationship locators only as assertion identity;
   retain them temporarily as labelled compatibility locators where clients
   still need them.
6. Reconcile projection updates after canonical add/remove operations and prove
   that a stored inverse does not create a duplicate assertion.

### Phase 3 — admit general logical-sentence assertions

1. Add a logical-sentence admission/read contract based on logical language,
   version, serialisation, direct sentence types, context, provenance, and
   lifecycle.
2. Keep parser-specific abstract syntax behind registered language adapters.
   Do not invent one universal AST before two real logical languages or
   consumers need it.
3. Treat current ground binary assertions as one adapter into that contract.
4. Exercise at least one non-binary form which distinguishes the architecture:
   for example, a quantified sentence with a functional term or a modal
   sentence. It may remain reasoning-opaque so long as it is honestly stored,
   typed, retrieved, and rendered.
5. Add candidate formalisation from text as a separate assertion with explicit
   provenance, review status, and alignment; never as an automatic canonical
   edge write.

### Phase 4 — context, theory, theorem, and conflict querying

1. Add direct context membership to text assertions and logical sentences using
   the common identity.
2. Introduce only the inclusion, lineage, and conflict semantics needed by a
   concrete paper-versus-lab, hypothesis-versus-base, or testing-theory
   consumer.
3. Adapt Testing Theory assertions instead of maintaining a permanent sibling
   assertion ontology.
4. Represent theory-relative theoremhood and proof/derivation lineage without
   reparenting the existing Theorem type unless its audited meaning and a
   concrete consumer justify that change.
5. Return direct/inherited/inferred/hypothetical status and non-leaking context
   lineage through the `JVNAUTOSCI-2038` query surface.

### Phase 5 — document/narrative anchoring and proposition alignment

1. Add stable source-passage anchors and attribution roles for paper,
   paragraph, sentence, transcript, and conversation sources.
2. Add narrative membership only when ordered/rhetorical retrieval consumes
   it.
3. Introduce Proposition items through `claim_id` when cross-form,
   cross-language, cross-document, or cross-context identity is needed.
4. Complete explicit text-to-logical equivalence, revision, contradiction, and
   provenance queries without lexical deduplication.

## 13. Minimum acceptance evidence

### Phase 1 ship criteria

- Store an arbitrary exact source text with analysis disabled and read back the
  unchanged body, `assertion_id`, direct `#V#text_assertion` type, inferred
  Assertion/Propositional Sentence ancestry, context, scope, provenance,
  lifecycle, and RAG state.
- Read an existing scoped concept-valued and literal-valued ground relation as
  `#V#ground_binary_predication`, with Ground Logical Sentence, Logical
  Sentence, Assertion, and Propositional Sentence ancestry.
- Preserve two identical text occurrences from distinct source events as two
  assertion IDs.
- Retract and reassert one assertion without changing its body, type meaning,
  source identity, or unrelated alignments.
- Query the Assertion type extent as an authorised actor without leaking
  inaccessible rows or lower-bound counts derived from them.
- Read old REST/MCP response shapes without a material regression.
- Prove that assertion type metadata does not recursively generate assertion
  records.

### Phase 2 ship criteria

- Project one canonical dynamic relation and one structural relation into
  stable typed assertion IDs independent of array position.
- Show that a structural inverse row projects to the same assertion ID and
  that source-only storage still yields the same result.
- Remove and reconcile a base relation without leaving an active assertion
  projection.
- Retrieve text and ground assertions through one envelope while preserving
  storage lineage, match kind, context, and completeness.

### Phase 3 and later distinguishing cases

- Store and retrieve a closed logical sentence which is not a binary ground
  atom, including language/version and exact serialisation.
- Preserve a quantified, modal, functional, temporal, or higher-order feature
  combination without forcing it into an exclusive subtype.
- Formalise a text assertion as a candidate logical sentence and prove that
  the source body remains unchanged and no base-publication edge appears.
- Represent a theorem only with theory-relative proof/derivation evidence; an
  unproved asserted logical sentence remains non-theorem.
- Anchor a quoted sentence in a paper without presenting it as the paper's or
  Von's adopted assertion.
- Query the same proposition asserted in two contexts with distinct assertion
  IDs, provenance, status, and direct/inherited lineage.

Use Tier 2 validation for authority-bound persistent assertion changes and
derived projection reconciliation. Ontology publication authority changes or
new cross-scope context inclusion may require the applicable Tier 3 boundary;
ordinary typed scoped-assertion storage does not become Tier 3 merely because
it is an ontology-aware write.

## 14. Stop-ship conditions

- Text parsing, concept resolution, logical formalisation, or proposition
  creation becomes a precondition for exact text admission.
- Existing exact text, `ska_*` identity, provenance, scope, lifecycle, or RAG
  recovery is rewritten or lost.
- The new assertion projection and existing graph can independently disagree
  about whether a base assertion is active.
- Array-position locators become durable assertion identity.
- Stored structural inverses become duplicate logical sentences.
- A text assertion is classified as a logical form solely because it has
  concept links or an extracted candidate relation.
- Ground binary fields become the universal logical-sentence schema or prevent
  quantified, modal, functional, temporal, n-ary, rule, or higher-order forms.
- A model-produced formalisation becomes user-adopted, true, theorem, or base
  publication without the applicable explicit decision.
- Document containment, narrative membership, source attribution, context
  inclusion, visibility, endorsement, and truth are collapsed.
- Type-extent queries leak private assertion existence or counts.
- Assertion typing recursively reifies its own representation metadata.
- The first slice creates a reasoner, universal context lattice, flag-day graph
  migration, or ordinary concept node for every assertion without a current
  consumer that needs it.

## 15. Explicit non-goals for the first release

- automatic truth verification;
- a universal logical syntax or parser;
- reasoning over every stored formal language;
- automatic decontextualisation or formalisation;
- a universal proposition/equivalence identity;
- complete narrative or rhetorical modelling;
- general context inheritance, conflict resolution, or belief revision;
- migration of every TextRelation or concept edge into a new authoritative
  assertion store;
- reification of every assertion act, proof step, or meta-assertion; and
- replacement of the current scoped authority, publication, or RAG boundaries.

## 16. Relevant precedent

The design follows established distinctions without adopting another system's
whole ontology:

- [CRMinf](https://cidoc-crm.org/extensions/crminf/html/CRMinf_v1.2.1.html)
  separates propositions and proposition sets from belief adoption,
  argumentation, observation, and inference.
- [RDF 1.1 Concepts](https://www.w3.org/TR/rdf11-concepts/) leaves named-graph
  contextual meaning application-defined. RDF 1.2's triple-term/reifier model
  is relevant future export precedent, but the current design does not depend
  on it.
- [OWL 2 structural semantics](https://www.w3.org/TR/owl-syntax/) distinguishes
  asserted axioms and annotations; annotations alone do not supply Von's
  contextual assertion semantics.
- [PROV-O](https://www.w3.org/TR/prov-o/) supplies useful Entity, Activity, and
  Agent lineage distinctions without treating provenance as truth.
- [Common Logic](https://www.iso.org/standard/66249.html) reinforces that a
  formal sentence requires an identified logical language and interpretation,
  not merely a triple-shaped string.
- [McCarthy's context work](https://www-formal.stanford.edu/jmc/context3/context3.html)
  and [Cyc microtheories](https://cyc.com/archives/glossary/microtheory/)
  motivate explicit context and lifting semantics rather than namespace-based
  implication.
- [DoCO](https://sparontologies.github.io/doco/current/doco.html) and the
  [Web Annotation Data Model](https://www.w3.org/TR/annotation-model/) provide
  useful later patterns for document structure and passage anchoring.

## 17. Related Von guidance and ownership

- [Contextual knowledge evolution](contextual_knowledge_evolution.md) governs
  the separation of assertion context, provenance, audience/authority,
  publication, lifecycle, and storage.
- [Ontology publication authority](ontology_publication_authority.md) governs
  publication of the new canonical types and predicates; adding type identity
  does not widen assertion visibility or publication authority.
- [Testing workflows and ephemeral theories](testing_workflows_ephemeral_theories_design.md)
  supplies a current consumer and adapter target, not a second generic
  assertion ontology.
- [Vontology tooling from KA/KCAP/KR literature](vontology_tooling_from_ka_kcap_kr_literature.md)
  supplies the context-query and provenance-tool direction.
- `JVNAUTOSCI-343` owns exact occurrence, claim/proposition, formalisation,
  alignment, lifecycle, and common retrieval.
- `JVNAUTOSCI-2038` owns general context/microtheory query, inclusion, lineage,
  and conflict semantics.
- `JVNAUTOSCI-2043` and `JVNAUTOSCI-2048` are downstream scientific-claim and
  ephemeral-theory consumers; they should consume this common identity rather
  than create private assertion taxonomies.
- `JVNAUTOSCI-1266` is the later context-relative truth, entailment-profile,
  and capability-negotiation surface; this first slice does not implement it.
- `JVNAUTOSCI-359` is an older flat edge assertion/hypothesis labelling
  proposal. Reconcile any still-useful certainty requirement through the common
  envelope rather than reviving a parallel foundation.

## 18. Current handoff decision

**Implementation decision: vocabulary published; keep the typed-record release
open.** The semantic hierarchy is now live and canonically verified.
This does not yet make existing scoped text assertions or ground relationship
rows instances in the common type extent, and it does not implement context
inheritance, a general logical-sentence store, or theorem-relative reasoning.

The minimum evidence for the next release is the remaining Phase 1 ship
criteria: direct Vontology type identity and explicit context on the existing
assertion envelope; compatibility-preserving, idempotent backfill; authorised
type-extent reads; and exact read-back proving that occurrence identity, text,
scope, provenance, lifecycle, and derived RAG state are unchanged.
