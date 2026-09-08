# Bounded knowledge federation pilot

Tracking: [JVNAUTOSCI-2730](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2730).
Programme: JVNAUTOSCI-2615. Worker admission: JVNAUTOSCI-2593.

## Capability and decision

Two independently backed Vons exchange explicitly selected person, organisation
and public knowledge through an operator-admitted SSH connection. The receiving
Von exposes a read projection through `search_federated_knowledge`. Empty query
returns visible records and coverage. Normal tool discovery advertises this tool.
It does not change canonical local assertion, concept or task collections.

This is complete-slice reconciliation, not general all-history event sourcing,
database replication or an Atlas replacement. The incumbent stores remain in
place. The pilot transfers at most 1,000 records / 4 MiB per direction; transfer
cost is proportional to the selected slice. It deliberately polls and transfers
the bounded slice even when unchanged, so a fresh authenticated observation can
renew private readability. Measure bytes and lag before adding a delta protocol.

A node owns its original claims. The same source assertion ID from two nodes
produces two distinct origin-qualified references. Local IDs are never silently
merged, and visibility never implies truth, endorsement, publication or execution
authority. The person an assertion concerns is independent of its audience.

## Commit, recovery and lifecycle

A source adapter reads only explicitly selected native scoped assertion IDs and
exact public graph triples. Two matching bounded scans detect changes during
capture; this is not a multi-document point-in-time database snapshot. Publication
uses a compare-and-set on the entire export head, recapturing after contention.
Any later source change is found by reconciliation; there is no timestamp cursor
that can permanently skip it. Failure to read the source does not advance a head.

The exporter persists one whole immutable-by-generation payload as its current
outbox head. Changed content increments generation; unchanged content retains the
generation and refreshes the source observation time. Payloads bind protocol,
origin, recipient, generation, checked time and content digest under a pairwise
HMAC key. SSH supplies channel authentication and confidentiality. HMAC provides
pairwise integrity, not third-party non-repudiation: both admitted peers know it.

The receiver validates the whole payload before one atomic Mongo document
replacement. The previous complete view remains readable until that replacement.
Duplicate delivery is idempotent, stale generations/observations and same-generation
equivocation are rejected. A later generation may skip intermediate generations:
its complete selected slice supersedes the previous one. Retractions are retained
as source lifecycle records but not returned as active claims. Deleted assertions,
removed selectors and narrowed source visibility disappear from the next complete
view. Intermediate assertions created and removed between polls are not an audit
history and are not promised by this protocol.

Receiver receipts distinguish applied/already-applied and include current
read-back generation/digest. A lost receipt permits retry; no imported record
emits Vontology mutation events or launches a workflow. Existing task claim fencing
coordinates only one database; knowledge exchange does not extend that lease.

## Authority and source content

Configuration and key files must be absolute regular files, owned by the service
user and inaccessible to group/other users. Admission is an operator operation,
not an MCP tool or a model-selected identity field. Both import and export are
opt-in per peer and audience. Private audience mappings are explicit and cannot
change kind or widen to public. Removing an import admission immediately prevents
its records appearing in reads, even while its stored snapshot remains on disk.

Source adapters allow only ordinary knowledge fields. Runtime RAG state, credentials,
roles, memberships, login bindings, scheduling and executable carriers are not copied.
Governance predicates are rejected. Required source concepts must be visible to the
whole exported audience; optional links are not copied. Vocabulary contains source
identifiers and a bounded set of unqualified canonical names/descriptions, not whole
concept documents, inherited theory closure or local authority. Public triples
retain available source/licence provenance; this is not licence inference.

Receiving private reads use the existing trusted actor resolver. Payload identity
claims cannot authenticate a user. An admitted source audience is mapped to a
locally recognised user or organisation; this config records the operator's explicit
identity decision, never an automatic slug merge or membership grant. Unknown IDs
remain source-local references. Public reads require no actor.

Every result reports origin, scope, provenance and freshness. Coverage is always
limited to configured received slices; an empty result never claims global absence.
Private reads expire after the configured observation age (default one hour).
Public records remain readable with a stale label. Revocation cannot make a trusted
recipient forget bytes already received, and offline revocation is bounded by that
read lease. Scope removal is propagated by a successful fresh exchange. Config or
key errors fail closed. Do not expose private diagnostics, whole-generation counts
or digests to actors who can see only part of the snapshot.

## Configuration and operation

Use a private file outside the repository, referenced by `VON_FEDERATION_CONFIG`
in each Von's environment. The example placeholders must be replaced deliberately;
there is no automatic all-person/all-organisation subscription.

```json
{
  "schema_version": "von_federation_config.v1",
  "node_id": "atlas-mac",
  "exports": {
    "dgx": {
      "enabled": true,
      "key_file": "/private/path/dgx.key",
      "audiences": ["user:#V#alice", "org:#V#lab", "public"],
      "assertion_ids": ["ska_explicit_selection"],
      "public_relations": [
        {"subject": "#V#paper", "predicate": "#V#is_an_instance_of", "object": "#V#scientific_paper"}
      ]
    }
  },
  "imports": {
    "dgx": {
      "enabled": true,
      "key_file": "/private/path/dgx.key",
      "audiences": ["user:#V#alice", "org:#V#lab", "public"],
      "audience_map": {
        "user:#V#alice": "user:#V#alice",
        "org:#V#lab": "org:#V#lab",
        "public": "public"
      },
      "max_age_seconds": 3600
    }
  },
  "transports": {
    "dgx": {
      "ssh_host": "primarydgxspark",
      "python": "/home/mjw/Von/.venv/bin/python",
      "script": "/home/mjw/Von/scripts/knowledge_federation.py",
      "config": "/private/remote/path/federation.json",
      "env_file": "/home/mjw/Von/.env"
    }
  }
}
```

Generate a fresh random 32-byte key, store as hexadecimal with mode 0600, and
provision it to that admitted peer through the existing secure channel. Each node
has its own config and Mongo database. One operator process can exchange in both
directions, so the DGX need not SSH into a laptop. Use the repository environment:

```sh
pdm run python scripts/knowledge_federation.py --config /private/path/federation.json exchange --peer dgx
pdm run python scripts/knowledge_federation.py --config /private/path/federation.json watch --peer dgx --interval 60
```

`export` emits a signed payload to stdout; `import` consumes it on stdin. Neither
is exposed as an ordinary MCP mutation. `watch` reloads config every cycle and
records receipts or sanitised error types. Use a host service manager for restart
and log rotation; source/private records must not be placed in service logs.

Back up each node's own canonical data, configuration, keys and federation heads.
If an exporter loses its generation state, restore it or deliberately re-enrol it
under a new node ID. Do not reset a receiver's replay fence to force old data in.
Rollback: stop the polling process, disable imports/exports, unset the environment
config, and restart the application if needed. Native knowledge remains untouched.

## Validation and third-node study

Required tests cover two independent stores; person/org/public positive and negative
reads through the production MCP gateway; forged identities and scope changes;
tampering, replay, incompatible protocol and equivocation; failed source reads and
partial transfers; retractions, selector removal and source visibility withdrawal;
private expiry and admission revocation; and independent contradictory origins.
Run a real two-node canonical-write/export/import/MCP-read path after unit checks.

SC448086 is not admitted by this release. Upgrade it, give it its own database and
node identity, and admit only selected scopes before using it as the third case.
It should test long-offline recovery, old-protocol rejection and selective audience
access. Reconnecting a worker to the same Atlas database is not a third replica.

No general vector indexing, online-only publication authority reconciliation,
automatic semantic conflict resolution, peer mesh, all-history retention, ontology
release migration or cross-database execution ownership is claimed. Add these only
when a measured next capability requires them.
