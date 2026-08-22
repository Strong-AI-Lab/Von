# Ontology Publication Authority

- **Kind:** Design and implementation-boundary reference
- **Lifecycle:** Active
- **Authority:** Operational reference for the deployed `JVNAUTOSCI-2632`
  authority boundary
- **Authority scope:** Canonical Vontology publication, retraction, identity
  consolidation, and publication-scope change
- **Owner:** Von maintainers
- **Last reviewed:** 22 August 2026
- **Review trigger:** Merge, deployment, a role-lifecycle change, or a new
  canonical ontology mutation entry point
- **Live authority or implementation evidence:** Initial activation completed
  on 13 August 2026 at commit
  `1248322dad4080e91d89e23b36bc4ca5aa57c9e1`; revalidate the current runtime
  commit, governed mutation service, represented role relations, and receipts
  before relying on this record in another environment
- **Open questions:** Long-term role-assignment lifecycle and any broader
  theory/context model remain separate work

## 1. Purpose and boundary

This release addresses a specific failure mode: historical visibility of a
concept must not decide who may alter its canonical meaning or move it between
publication contexts. Visibility, provenance, scoped assertions, canonical
publication, and operational administration remain separate facts.

The authority decision is made against the source and destination publication
contexts of the intended effect. A visible concept is not thereby mutable. An
ordinary actor can still use the existing bounded, provenance-bearing
actor/organisation assertion path where that is the appropriate outcome;
canonical publication is a different capability.

This guide concerns represented knowledge. It does not turn a conversation,
session ID, namespace, workflow visibility, or a model/tool argument into
semantic authority.

## 2. Authority meanings

Two represented semantic roles are recognised:

- An **organisation ontology administrator** may make covered canonical
  changes in that exact organisation publication context.
- A **global ontology administrator** may make covered canonical changes in
  the global publication context. It is also required for the controlled
  adoption of historical or mixed scope.

An authenticated actor retains the existing bounded ability to create and edit
canonical material in that actor's exact user-private publication context. The
actor may also exercise a represented organisation or global semantic role
through a trusted actor-bound turn when the final intent is checked live. A
scope choice does not itself confer authority over an organisation, global
publication, mixed history, or an inverse target in another context.

`#V#von_administrator` remains an operational role. Its represented assignment
uses a dedicated operational-administrator lifecycle and can authorise bounded
deployment, diagnostic, or bootstrap control-plane operations. The existing
operator token remains a separate bootstrap and recovery credential. Neither
form implies a semantic role, reveals private organisation knowledge, or can by
itself publish or change ontology scope.

The role assertions are represented knowledge with explicit organisation or
global context. The authority decision resolves them live for the effect; a
cached label, client payload, session reference, or legacy role resolver is not
an adequate substitute. Grant-specific actor, request, and reason provenance is
stored on the exact subject-role relation rather than on its deduplicated role
text value, and a grant succeeds only when that exact relation is read back.

## 3. Agent delegation

An authenticated actor may authorise an agent to carry out one ontology effect
already covered by that actor's live semantic authority. For an ordinary
actor-bound turn, the trusted server may derive that exact authorisation from
the resolved effect without an additional per-effect confirmation. The
resulting capability is deliberately narrow:

- server-issued from the grantor's trusted identity and current role evidence;
- bound to one agent, audience, tool, operation, target/delta fingerprint, and
  effect identifier;
- short-lived, revocable, non-recursive, and non-amplifying; and
- rechecked against the grantor's live semantic authority when used.

There is no standing delegation to a separately acting agent. An agent,
workflow, client, model, or tool payload cannot create, enlarge, or relay a
semantic delegation. Sessionless gateway and stdio mutations are currently
denied before target-sensitive reads; they must not recover a grantor's private
visibility merely from an opaque grant.

An actor-bound workflow effect executed within the same trusted server does not
become a separate authority handoff merely because an agent selected or
composed it. After the resolved write passes the workflow mutation ceiling, an
exact actor-private effect may retain the authenticated actor's direct
authority. When an executing agent needs a separate capability, including for
an ordinary-turn organisation or global creation, the server may issue one
exact same-turn delegation only after resolving the final intent and checking
the actor's corresponding live semantic role. This issuance is part of
executing the authorised turn; it does not introduce an extra human
confirmation or let the model enlarge its scope. The governed MCP path retains
agent provenance, a durable effect receipt, and canonical read-back. Existing
custom scholarly handlers do not yet share that receipt path; their bounded
exception preflights every existing mutation target as the exact actor's
private concept and requires global schema support to be preprovisioned.
Historical/mixed, other-user, reserved-governance, sessionless, or separately
delegated effects do not inherit this actor-bound path merely because the model
requested them.

## 4. Governed effects and scope transition

The release centralises authority decisions for the supported canonical
concept, text, and relationship mutations, including promotion/retraction. The
chat/adaptive turn, workflow, authenticated HTTP, and internal MCP entry points
enter that governed path rather than each treating visibility as mutation
permission. Identity consolidation is a dedicated governed effect: it uses a
server-built complete affected-graph plan, requires authority for every
affected publication context, preserves the target's publication scope, and
claims success only after exact canonical read-back. Sessionless stdio writes
and graph-wide rename and delete execution remain fail-closed until they can
carry an equivalent complete effect plan; their read or preview paths remain
available where safe.

Legacy inline `names[]` cleanup is a dedicated governed effect rather than a
generic concept update. The authenticated client receives an opaque selector
bound to the concept, exact array position, exact entry value, and complete
array snapshot. Execution rechecks that selector under the same concept-level
lock used by canonical `hasName` mutations, requires a canonical `hasName` to
remain, compare-and-sets only the selected legacy entry, and succeeds only when
read-back proves both the removal and the unchanged canonical-name snapshot.
Natural-language values are not trimmed, case-folded, title-cased, or Unicode
normalised by the deletion boundary.

For ordinary-turn `create_concepts`, the model may select
`user_only_default`, `organisation_general`, or `global_general` according to
the intended concept semantics. Omitting the choice remains the conservative
`user_only_default`. The server binds actor and current-organisation identity;
the model cannot name a different user or organisation, and organisation or
global creation executes only when the actor's corresponding live semantic
authority permits it. Denial is reported as denial: the system must not
silently downgrade a rejected organisation or global request to private
creation. Scope selection is model judgement within that authority ceiling,
not an automatic ladder, confirmation ritual, or hard-coded semantic
classifier.

Canonical relationships inherit the source concept's exact publication
context; `add_relationship` does not take an independent scope choice. A
caller-supplied `scope_mode` on that command is rejected rather than ignored.
When a relationship-like fact needs an audience independent of a broader
visible subject, the model may instead choose the provenance-bearing
`upsert_scoped_assertion` path with user or organisation scope. Organisation
identity remains server-bound and organisation scope requires a current trusted
organisation, but the optional organisation binding must not make user-scoped
assertion capabilities disappear for an authenticated actor who has no current
organisation.

An exact canonical publication context may contain no restriction, exactly one
user restriction, exactly one organisation restriction, or exactly one of each.
The last form is a supported composite context, and a mutation affecting it
requires authority over both its user and organisation components. A scope
shape containing legacy aliases, more than one user or organisation, malformed
targets, or another unresolved combination remains historical or mixed; it is
not reinterpreted as a canonical composite.

Authority and visibility predicates are reserved to dedicated governance
operations. Generic mutation is denied for unresolved historical or mixed
scope. The dedicated scope-change path first exposes a scope read-back and
preview, then requires an optimistic scope fingerprint, explicit edit,
source-and-destination authority, and a request identifier. For a canonical
scope, user and organisation controls are independent: enabling a component
adds only that restriction and disabling it removes only that restriction,
while preserving the complementary component. Consequently removing the sole
restriction produces global scope; removing either component from a composite
preserves the other. Additions are bound to the authenticated actor or current
organisation, and execution verifies the exact edge delta and final read-back.
The controls do not implement an automatic user-to-organisation-to-global
ladder. Ambiguous legacy aliases, multiplicity, and malformed scope are rejected
by this quick-edit contract; any controlled adoption of historical or mixed
scope must use the broader explicit governed path and must never silently
reinterpret malformed legacy data as global publication.

Undo is deliberately fail-closed until it can carry an equally exact target,
authority decision, receipt, and canonical read-back. This is preferable to a
plausible-looking inverse effect whose publication boundary cannot be proved.

## 5. Evidence, recovery, and failure semantics

Every governed mutation is associated with an authority decision and a durable
receipt. The receipt records the bounded intent, authority evidence, effect
state, and canonical read-back. Idempotency is keyed to the authorised effect:
retries may return the already-recorded result, while a conflicting reuse or an
effect still in progress must not execute a second mutation.

Success requires canonical read-back, not merely a handler response. If a
write outcome or read-back is uncertain, the receipt must report an honest
non-success or indeterminate state with enough information for bounded
reconciliation. Scope transitions use compensation where possible, but
compensation is not a substitute for reporting the final canonical state.

Denials are typed and non-leaking: callers receive the missing authority or
delegation condition without disclosure of inaccessible concepts, roles, or
contexts.

## 6. Bootstrap and operational limits

Initial role creation is a one-time, explicitly enabled local operator
bootstrap for the first global semantic administrator and the first represented
Von operational administrator. The principal must be an existing canonical
concept, and each bootstrap is successful only after the exact represented role
is read back. These are distinct one-time roots: the operator token does not
become a permanent route around semantic governance, and semantic authority
does not open operational controls. A global semantic administrator may then
establish only the first administrator root for an actor-visible organisation;
after that, administrators for that exact organisation govern its role
lifecycle. Neither the last global semantic root nor the last represented
operational root can be removed.

The release also contains one inventory-bound initial migration fixed to
`#V#michael_witbrock`. Its dry run inventories legacy elevated roles,
represented memberships, semantic roles, and operational roles across all
subjects. Apply requires both the configured bootstrap credential and
Michael's authenticated human session, and refuses to start if anyone else is
an elevated holder. It represents Michael as the Von operational
administrator, global ontology administrator, exact-organisation ontology
administrator, and organisation owner for every represented membership. Each
effect is read back exactly; interrupted application is recorded as partial
and can resume only against the same safe inventory. The migration was applied
in the primary deployment on 13 August 2026 and then disabled again by a normal
restart; its completion does not authorise reuse in another environment.

This release does not select a universal microtheory, organisation-membership
model, or context formalism. It also does not grant a global administrator
visibility into private material merely because it grants global publication
authority. Those boundaries remain governed by the existing access-control and
contextual-knowledge guidance.

## 7. Validation boundary

This is an authority-release change and requires Tier 3 evidence before any
release claim. The material cases are: positive and negative organisation and
global authority; operational-admin non-equivalence; expiry, revocation,
tampering, and cross-audience delegation denial; alternate entry-point
enforcement; private-context non-leakage; retry/concurrency behaviour; and
receipt-backed canonical read-back of successful and partial effects.
Where a supported workflow route uses direct actor-private authority or
same-turn exact delegation, the positive evidence must exercise its normal
production actor and current-organisation binding, live role resolution,
delegation issuance where applicable, and write ceiling rather than a manually
injected delegation value. Creation evidence must cover private defaulting,
explicit organisation and global selection, spoofed-identity containment,
authority denial without downgrade or mutation, and exact canonical read-back.
Scope-edit evidence must cover every add/remove state transition across global,
user, organisation, and composite contexts, plus legacy/multiple-scope
rejection and stale-preview refusal.

For legacy inline-name cleanup, the bounded evidence additionally covers exact
Unicode preservation, stale and repeated selector refusal, canonical-name
retention, shared locking with canonical `hasName` writes, authenticated HTTP
actor binding, embedding invalidation, and user-visible deletion through the
same browser control used by the Concept tab.

The motivating historical changes are evidence targets, not general permission
to alter live Vontology. Applying any such change still requires a deployed
authority configuration and a separately authorised actor/effect.

## 8. Primary-deployment evidence

The 13 August 2026 activation used an encrypted pre-activation backup, a
trusted Michael Witbrock browser session, the inventory-bound migration, and a
normal restart that closed the one-time bootstrap aperture. Canonical read-back
showed Michael as the sole represented Von operational administrator, the
global ontology administrator, and an ontology administrator and owner for
each of his four represented organisations. Header-supplied identity remained
denied on both authority-management and governed HTTP mutation paths.

The first live replay repaired the previously blocked global relationship
`#V#graduate_student --is_a_type_of--> #V#university_student`. Receipt
`omr_fe7d18665d6801f6705a88413223a2c3d352e8ab58c46938f368bc3e49222f24`
finished `succeeded` and its canonical read-back proved both the forward edge
and the `#V#university_student --has_subtype--> #V#graduate_student` inverse.

The candidature replay then resolved the canonical identities and publication
scope of the 23 historically blocked people. Fresh canonical read-back showed
all 23 global `#V#is_an_instance_of -> #V#student` edges and all 23 inverse
`#V#student -> #V#has_instance` edges. Each relationship has its own terminal
`succeeded` receipt with both directions present; together with the five
pre-existing classifications, the candidature set is 28 of 28. The replay also
exposed a false user-visible success claim for one stopped stale identifier.
The finaliser now rejects a cited ontology relationship-success claim unless
its durable effect succeeded and the exact relationship read-back agrees.

A read-only, write-tripped live-store preflight for the plausible identity pair
`#V#ph_d_student -> #V#phd_student` produced deterministic plan digest
`367e997ab34186fc29bbbde16f90fb45dce68e30e86d110a9b690b544af6a267`.
It covered seven concepts, five incoming-reference rewrites, and four text
relations, with no residual source reference or structural target self-edge.
No live identity merge was executed: a merge still requires an exact identity
decision and its separately authorised plan.

This is evidence for those exact deployment effects and authority boundaries,
not a general multi-tenant certification claim.

## Related guidance

- [Access control and namespaces](access_control_and_namespaces.md) explains
  trusted identity and visibility; it does not grant semantic publication.
- [Contextual knowledge evolution](contextual_knowledge_evolution.md) explains
  why assertion context, authority, provenance, and publication must remain
  distinguishable.
- [Security considerations](security_considerations.md) governs the
  authentication, delegation, private-data, and operator-security posture.
