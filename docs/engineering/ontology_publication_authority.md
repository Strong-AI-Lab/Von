# Ontology Publication Authority

- **Kind:** Design and implementation-boundary reference
- **Lifecycle:** Draft candidate
- **Authority:** Candidate reference for the `JVNAUTOSCI-2632` branch; it does
  not prove deployment, represented role assignment, or live authorisation
- **Authority scope:** Canonical Vontology publication, retraction, identity
  consolidation, and publication-scope change
- **Owner:** Von maintainers
- **Last reviewed:** 13 August 2026
- **Review trigger:** Merge, deployment, a role-lifecycle change, or a new
  canonical ontology mutation entry point
- **Live authority or implementation evidence:** Revalidate the governed
  mutation service, invocation boundaries, represented role relations, and
  receipts in the target environment
- **Open questions:** Long-term role-assignment lifecycle and any broader
  theory/context model remain separate work

## 1. Purpose and boundary

This candidate addresses a specific failure mode: historical visibility of a
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
canonical material in that actor's exact user-private publication context. That
does not confer authority over an organisation, global publication, mixed
history, or an inverse target in another context.

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

An authorised human may authorise an agent to carry out one already-authorised
ontology effect. The resulting capability is deliberately narrow:

- server-issued from the grantor's trusted identity and current role evidence;
- bound to one agent, audience, tool, operation, target/delta fingerprint, and
  effect identifier;
- short-lived, revocable, non-recursive, and non-amplifying; and
- rechecked against the grantor's live semantic authority when used.

There is no standing agent delegation. An agent, workflow, client, model, or
tool payload cannot create, enlarge, or relay a semantic delegation. Sessionless
gateway and stdio mutations are currently denied before target-sensitive reads;
they must not recover a grantor's private visibility merely from an opaque grant.

## 4. Governed effects and scope transition

The candidate centralises authority decisions for the supported canonical
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

Governed creation defaults to the actor's exact user-private publication
context. Organisation publication must be requested explicitly as
`organisation_general` and requires that organisation's semantic authority.
The legacy dual user-and-organisation visibility mode is rejected at this
boundary because that visibility shape is mixed, not a canonical ownership or
publication context.

Authority and visibility predicates are reserved to dedicated governance
operations. Generic mutation is denied for unresolved historical or mixed
scope. The dedicated scope-change path first exposes a scope read-back and
preview, then requires an optimistic scope fingerprint, explicit destination,
source-and-destination authority, and a request identifier. It is the only
candidate path that may adopt legacy/mixed scope; it must never silently
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

The candidate also contains one inventory-bound initial migration fixed to
`#V#michael_witbrock`. Its dry run inventories legacy elevated roles,
represented memberships, semantic roles, and operational roles across all
subjects. Apply requires both the configured bootstrap credential and
Michael's authenticated human session, and refuses to start if anyone else is
an elevated holder. It represents Michael as the Von operational
administrator, global ontology administrator, exact-organisation ontology
administrator, and organisation owner for every represented membership. Each
effect is read back exactly; interrupted application is recorded as partial
and can resume only against the same safe inventory. This candidate mechanism
does not imply that the migration has been run in any live environment.

This candidate does not select a universal microtheory, organisation-membership
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

The motivating historical changes are evidence targets, not permission to
alter live Vontology during implementation or test work. Applying any such
change still requires a deployed authority configuration and a separately
authorised actor/effect.

## Related guidance

- [Access control and namespaces](access_control_and_namespaces.md) explains
  trusted identity and visibility; it does not grant semantic publication.
- [Contextual knowledge evolution](contextual_knowledge_evolution.md) explains
  why assertion context, authority, provenance, and publication must remain
  distinguishable.
- [Security considerations](security_considerations.md) governs the
  authentication, delegation, private-data, and operator-security posture.
