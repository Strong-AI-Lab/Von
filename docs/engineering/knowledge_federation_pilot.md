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
place. The original v1 pilot transferred at most 1,000 records / 4 MiB per direction; the
v2 extension below raises this bounded metadata capacity. Transfer
cost is proportional to the selected slice. It deliberately polls and transfers
the bounded slice even when unchanged, so a fresh authenticated observation can
renew private readability. The v2 extension below adds an authenticated compact refresh for unchanged records.

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

## Practical continuity extension (JVNAUTOSCI-2731)

The v2 snapshot adds automatic native scope selection and portable conversation
and file catalogues. Both endpoints must be upgraded together before enabling the
new configuration. Metadata capacity is 20,000 records / 12 MiB per direction;
an over-capacity capture fails visibly and preserves the last complete generation.
There is no silent first-N export. This remains bounded reconciliation, with two
matching scans, not an unbounded database replica.

An export may add `assertion_audiences` and `file_audiences`, each an explicit
subset of its admitted private audiences. Newly created native assertions in those
scopes are included on the next successful exchange without editing ID lists.
Assertion visibility and vocabulary checks still apply. Public graph export remains
an exact operator selection. Native file manifests require an unambiguous explicit
user or organisation visibility relation; user visibility takes precedence over
organisation visibility. Unsupported or unscoped legacy file visibility, conversation
image proxy records and copies imported by federation are not automatically relayed.

`conversation_users` admits owners across their current and future valid owner-prefixed
namespaces; `conversation_scopes` can instead select exact `{user_id, namespace}`
pairs. Missing or conflicting legacy namespaces are excluded. Discovery exports
only owner-private session identifiers, titles, update times and previews of the
last two user/assistant messages (at most 500 characters each). It never exposes
system messages, tool payloads, execution diagnostics or credentials as operational
fields. User-visible text can itself contain sensitive information and retains its
owner's audience. Shared conversation access does not authorise copying another
owner's carrier. Imported conversation catalogues are not editable chat sessions.

Use `search_federated_knowledge` with `kind`, `query`, `federated_id` or
`source_concept_id`. Results sort by source update time, newest first, and offer
`next_offset`. Pagination is a live view, so concurrent reconciliation can change
page order. Empty results mean no match within the received configured coverage,
not global absence. Previews are not full-text conversation indexing. The result
also names surfaces that remain local, including executable tasks and editable
conversation originals.

`read_federated_conversation` retrieves visible conversation text on demand in
60,000-character pages. Pass both `next_offset` and `content_digest` to continue;
a changed conversation requires restarting the read. A new local conversation can
use this evidence to continue discussing the work, but this does not transfer a
running workflow, task claim, editor state or browser chat history entry.

`import_federated_file` fetches a discovered file of at most 32 MiB, checks its
size and SHA256 and calls the canonical file-copy ingestion service. The local
copy is private to the requesting trusted user and retains origin, source identity,
source audience and content hash. Existing normal file/OCR/diagram tools can use the
returned local concept ID. Repeated imports use the canonical user/content/source
lookup. A materialised copy has its own lifecycle; source withdrawal removes remote
discovery/read access but does not erase a previously authorised private copy.

File manifests report `on_demand` or `source_only_metadata_or_size_limit`.
`on_demand` means eligible for an attempted fetch, not that source connectivity or
blob availability has been checked. Both conversation text and original file bytes
require a reachable source. Cached catalogue previews retain the existing freshness
lease. External Otter/LinkedIn/KnowKat archives remain separate integration sources;
this extension does not clone them or copy their credential/resource bindings.

### Content transport and recovery

The standard content route uses the admitted SSH command. If the source cannot
accept reverse SSH, run the optional operator content reader with `serve --port
5013`. It binds only `127.0.0.1` and accepts only `/content` POSTs. Expose it to the
other host through an SSH reverse tunnel bound there to `127.0.0.1`, for example
`ssh -N -R 127.0.0.1:15013:127.0.0.1:5013 admitted-peer`. Configure the receiving
node's `content_endpoints` mapping, e.g. `{"atlas-mac":
"http://127.0.0.1:15013/content"}`. Only loopback URLs without credentials or
redirect targets are valid; this is not a public HTTP server.

Requests are signed with the admitted pairwise key and bind origin, recipient,
request and observation time. Responses additionally bind the caller's random
nonce and exact catalogue record digest. Source reads recheck native eligibility
and receiver reads recheck admission after transfer. The listener cannot mutate
configuration, import knowledge, write files or invoke workflow execution. Protect
and supervise both reader and tunnel with the host's service manager. The same
limitations of pairwise HMAC and trusted peer retention apply as to snapshots.

A stopped tunnel produces a typed unavailable result, not a fabricated empty
conversation or a claim that a file has been saved. Metadata exchange is independent
of the reverse content route. Rollback stops the reader/tunnel, restores the earlier
private configuration, and disables the extended selection before downgrading both
nodes; retain generation heads and existing canonical data.


When both peers have the same catalogue digest, v2 omits record bytes from the
refresh. The receiver reconstructs only from its matching stored head and verifies
the original signature over the full reconstructed payload before updating freshness.
A missing/mismatched cache requests a full retry; a stale, tampered or equivocal
refresh cannot advance the view. Changed catalogues still transfer a complete slice.
This reduces unchanged wire traffic without introducing per-assertion delta cursors.
