# KnowKat ontology sources and attributed adoption

KnowKat supplies existing representational infrastructure over standard MCP.
The first supported source is the complete pinned OpenCyc 2012 CC-BY-3.0 OWL
export. Von reuses its existing MCPStdIOClient; no second transport, reasoner
or automatic corpus ingestion is added. The operator installs KnowKat and its
immutable package separately. This document describes the shipped integration,
not evidence of activation on any running deployment.

## Configure a deliberately public source

Set operator environment values (or deployment .env):

```text
VON_KNOWKAT_PUBLIC=true
VON_KNOWKAT_COMMAND_JSON=["/absolute/knowkat/.venv/bin/knowkat","serve-mcp","--package","/absolute/opencyc-package"]
VON_KNOWKAT_TIMEOUT_SEC=60
```

An optional `--policy /absolute/policy.json` is part of that operator argument
vector. Tool callers cannot supply paths, commands, policy or audience overrides.
A permissive licence does not make a package public: the explicit public setting
is required before any launch. Private KnowKat collections are not supported by
this first integration. Revoking the public setting denies the next call.
Public reads require no private-owner identity. Missing configuration, transport
failure and upstream licence denial return explicit errors.

The child receives a minimal environment, not Von's credentials. Each call uses
the existing bounded SDK session and verifies package integrity upstream. The
60-second default bounds an unavailable external process; 5–300 seconds is an
operator setting. Full-source retrieval in local acceptance took seconds per
call; no corpus-wide latency guarantee is made.

The catalogue exposes `knowkat_list_knowledge_bases`, `knowkat_search_concepts`,
`knowkat_get_concept`, `knowkat_get_statements`, and
`knowkat_get_ontology_neighbourhood`, preserving upstream provenance, errors,
limits and cursors. Source content is untrusted evidence, never instructions or
a grant of downstream write authority. Search and neighbourhoods may be bounded;
use statement pagination for omitted evidence.

## Adoption through existing governed services

Search existing Vontology before inventing a local structure, and inspect the
source's actual assertions. Existing represented guidance is discoverable as
`#V#kr_design_materialisation_workflow` and
`#V#prompt_kr_design_materialisation_plan`. The proposal tool references these
surfaces; their live prompt bodies remain authoritative and are not duplicated
or replaced by this integration. No new task-specific orchestration is needed.

`knowkat_prepare_adoption` accepts an exact source IRI and the caller's grounded
local name, kind, parent and interpretation. It returns:

- one `create_concepts` argument object, defaulting to actor-private scope;
- complete attribution and selected RDF evidence in canonical `hasNote` text;
- an explicit source-page completeness flag and continuation cursor;
- exact provenance-text arguments for an existing governed text write on reuse;
- read-back requirements, with `effect_status: not_executed`.

The proposal is a work product, not durable adoption. Execute the returned
creation through the normal governed capability, then read its concept and text
relations. When creation reports existing-identity reuse, it remains read-only:
upsert the returned provenance text with `upsert_text_relation` if absent. This
non-singleton note preserves older source versions instead of replacing them.
Every required effect must have canonical read-back before reporting completion.

Identity uses the existing opaque pair `ontology-iri` plus original IRI, with
actor-scoped canonical IDs. A single explicit external identity is now bound
into governed create intent, exact agent delegation and marker read-back. Other
complex multi-effect create fields retain their existing rejection. Ambiguous
legacy identity evidence or an unverified ID occupant requires inspection;
names alone never establish identity. Reuse does not rename, retype, change
scope or repair markers on an existing concept. Blank-node IDs are package-local
and cannot serve as independent adoption identities.

Original IRIs, source statement IDs, package/source hashes, version, parser,
licence links, full notice, attribution and transformations are retained in the
note. Local interpretation and parent choice are separately labelled. Source
parents and OWL restrictions remain quoted source assertions; they are not
silently installed as global taxonomy or flattened into a single-parent tree.
Represent an additional local relation through its existing governed relation
service only when that particular local assertion is justified.

## Export and evidence

Normal `concept_service.export_concepts` and its existing concept export route
now include canonical `text_relations`, including identity markers and adoption
notes. Reads use the canonical base-publication text view, retain per-row context
and provenance, and expand the text batch until its completeness sentinel clears.
Only actor-visible concepts contribute records or counts. The separate disabled
whole-ontology HTTP export remains disabled. Import round-trip support for these
text rows is not claimed by this delivery.

Tests exercise actual governed creation, same-turn delegation issuance and use,
changed-identity denial, identity collisions, cross-actor export denial, distinct
homonyms, repeated reuse, and attribution-preserving export in an isolated
Mongo-compatible fixture. The only database emulation adaptation replaces
unsupported MongoDB `$text` candidate acquisition with exact substring matching;
identity checks, canonical writes, authority decisions and read-back execute the
production services. A real-source acceptance additionally uses the actual
KnowKat executable/full package through Von's adapter to adopt bicycle and tyre
changing, then creates a separately labelled local bicycle-maintenance concept
and relation. These fixture writes do not mutate live Vontology.

Run targeted tests with PDM. To enable the reproducible full-package case:

```sh
KNOWKAT_ACCEPTANCE_COMMAND_JSON='["/absolute/knowkat/.venv/bin/knowkat","serve-mcp","--package","/absolute/opencyc-package"]' \
KNOWKAT_ACCEPTANCE_EVIDENCE=/absolute/evidence.json \
pdm run pytest tests/backend/test_knowkat_integration.py -q
```

Without that operator configuration only the optional full-corpus acceptance is
skipped; the ordinary regression suite needs no KnowKat installation. KnowKat's
own tests cover independent graph isomorphism, complete source count/dedup
reconciliation, source licence policy refresh and protocol lifecycle. Its strict
policy applies to package/MCP APIs, not legacy REST/JSON. Source admission remains
separate from the certifi-only software MPL exception and from Von write authority.

NextKB, DLPB, Wikidata, general reasoning, native-KB decoding, private-source
sharing, remote transport, live prompt activation and live Von deployment are
outside this delivery.
