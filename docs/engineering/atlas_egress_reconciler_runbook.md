# Atlas Egress Reconciler Runbook

- **Kind:** Operational runbook
- **Lifecycle:** Active
- **Authority:** Canonical for the standalone reconciler and its bootstrap;
  Atlas policy and live configuration remain authoritative
- **Owner:** Von maintainers
- **Last reviewed:** 2 August 2026
- **Review trigger:** Atlas Admin API authentication or project IP access-list
  contract changes, or a change to the mobile-client/DGX network topology
- **Scope:** `scripts/atlas_egress_reconciler.py` and
  `scripts/install_macos_atlas_egress_reconciler.py`; no Von runtime code

## 1. Outcome and topology

This tool keeps two exact Atlas **database** IP access-list entries aligned
with changing public egress addresses:

- `von-mobile-client-egress` for the travelling Von/Codex client; and
- `von-dgx-starlink-egress` for the Auckland DGX behind Starlink.

The DGX is not geographically or network-local to the client. In this runbook,
it is a named authenticated peer reachable through the existing SSH path.
Nothing relies on LAN locality.

The simplest normal path is a local Atlas Admin API call. The existing SSH
path provides reciprocal recovery:

1. The client observes its own public IPv4 using two-source HTTPS consensus.
2. It reconciles its own named `/32` through the Atlas Admin API.
3. It asks the DGX to observe its Starlink egress, reconciles the DGX entry,
   and asks the DGX to prove the database TLS route before deleting the old
   DGX entry.
4. If the client's Atlas Admin API request is blocked, the client asks the DGX
   over authenticated SSH to add the client's new entry. The client proves its
   own database TLS route, then asks the DGX to remove the old entry.
5. If neither Atlas Admin API path works, the client keeps old entries, records
   a degraded state, writes exact manual recovery instructions, exits non-zero,
   and optionally raises a macOS notification.

The SSH session and its loopback listeners do not prove Mongo readiness. The
reconciler separately waits for Atlas to report the new entry `ACTIVE`, reads
the exact entry back, and completes a hostname-verified TLS handshake to an
Atlas member from the affected node.

## 2. Catch-22 boundary

Atlas database IP access lists and Atlas Administration API source-IP access
lists are separate controls. The automation breaks the catch-22 only if at
least one node can call the Admin API while its database `/32` is stale.

For this two-node design, use project service accounts with only **Project
Network Access Manager** and make their Admin API use independent of the two
changing egress addresses. The direct configuration is:

- turn off the organisation setting **Require IP Access List for the Atlas
  Administration API**; and
- leave each reconciler service account's Admin API access list empty.

Atlas documents that, when the organisation requirement is disabled, an empty
Admin API access list permits authenticated API calls from any address; adding
an entry makes the credential source-restricted again. A source restriction
containing only the current mobile and Starlink addresses recreates the
catch-22. If organisational policy requires Admin API source-IP restrictions,
use a separately managed stable third control-plane anchor instead; do not
enable this two-node scheduler while claiming it is self-recovering.

Atlas service-account secrets expire, so secret rotation remains a scheduled
operator responsibility.

Current Atlas references:

- [Admin API access and optional source-IP restriction](https://www.mongodb.com/docs/atlas/configure-api-access/)
- [Service-account OAuth client-credentials authentication](https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/2025-02-19/authentication)
- [Project IP access-list create operation and required role](https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/2025-03-12/operation/operation-creategroupaccesslistentry)
- [Project IP access-list status](https://www.mongodb.com/docs/api/doc/atlas-admin-api-v2/operation/operation-getgroupaccessliststatus)

## 3. One-time manual bootstrap

These are the only steps the tool cannot safely perform for itself.

### 3.1 Atlas

1. In the Atlas project, record its 24-character project ID.
2. Create a project service account for the mobile client and another for the
   DGX. Give each only **Project Network Access Manager**.
3. Save each client ID and its one-time client secret. Do not put a secret in
   this repository, Jira, chat, a LaunchAgent, or an SSH command.
4. Apply the Admin API policy in section 2: organisation requirement off and
   the two service-account Admin API access lists empty. If this is prohibited,
   stop and establish a stable management anchor.

No initial database access-list edit is normally required: the first explicit
`reconcile` command below creates the current `/32`. If a service account
cannot call the Admin API, manually add that node's current public IPv4 `/32`
under its exact comment, repair the Admin API path, and rerun the preflight.

### 3.2 DGX files and secret

The DGX needs Python 3.11 or later, one copy of the standalone script, an
owner-only configuration, and its own service-account secret. It does not need
Von to be running and does not need a resident daemon for reciprocal rescue.

Create owner-only directories on the DGX, copy the script over the existing
authenticated SSH route, and verify the copied checksum:

```sh
ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/absolute/path/to/known_hosts \
  -i /absolute/path/to/existing-ssh-key user@dgx \
  'install -d -m 700 "$HOME/.local/lib/von-ops" "$HOME/.config/von"'

scp -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/absolute/path/to/known_hosts \
  -i /absolute/path/to/existing-ssh-key \
  scripts/atlas_egress_reconciler.py \
  user@dgx:/home/user/.local/lib/von-ops/atlas_egress_reconciler.py
```

On the DGX, create `/home/user/.config/von/atlas-client-secret` without echoing
the secret and set mode `0600`. Then create this owner-only configuration:

```json
{
  "schema_version": 1,
  "atlas": {
    "project_id": "REPLACE_WITH_PROJECT_ID",
    "client_id": "mdb_sa_id_REPLACE_WITH_DGX_CLIENT_ID",
    "client_secret": {
      "source": "file",
      "path": "/home/user/.config/von/atlas-client-secret"
    }
  },
  "local_node": "dgx",
  "managed_entries": {
    "mobile": "von-mobile-client-egress",
    "dgx": "von-dgx-starlink-egress"
  },
  "allowed_peer_nodes": ["mobile"],
  "database_probe": {
    "host": "REPLACE_WITH_ONE_ATLAS_MEMBER.mongodb.net",
    "port": 27017
  },
  "state_path": "/home/user/.local/state/von/atlas-egress-reconciler.json",
  "lock_path": "/home/user/.local/state/von/atlas-egress-reconciler.lock"
}
```

Set the file to mode `0600`. `allowed_peer_nodes` is the server-side authority
boundary: an SSH caller may reconcile the `mobile` entry but cannot nominate an
unknown node, change a comment, project, credential, probe host, or arbitrary
command.

### 3.3 Mobile-client configuration and Keychain

Store the mobile service-account secret in macOS Keychain. Put `-w` last so
`security` prompts rather than exposing the secret in shell history or the
process list:

```sh
/usr/bin/security add-generic-password \
  -s org.strongailab.von.atlas-egress-reconciler \
  -a mdb_sa_id_REPLACE_WITH_MOBILE_CLIENT_ID \
  -U -w
```

Create `~/.config/von/atlas-egress-reconciler.json` with mode `0600`:

```json
{
  "schema_version": 1,
  "atlas": {
    "project_id": "REPLACE_WITH_PROJECT_ID",
    "client_id": "mdb_sa_id_REPLACE_WITH_MOBILE_CLIENT_ID",
    "client_secret": {
      "source": "macos-keychain",
      "service": "org.strongailab.von.atlas-egress-reconciler",
      "account": "mdb_sa_id_REPLACE_WITH_MOBILE_CLIENT_ID"
    }
  },
  "local_node": "mobile",
  "managed_entries": {
    "mobile": "von-mobile-client-egress",
    "dgx": "von-dgx-starlink-egress"
  },
  "allowed_peer_nodes": [],
  "database_probe": {
    "host": "REPLACE_WITH_ONE_ATLAS_MEMBER.mongodb.net",
    "port": 27017
  },
  "peer": {
    "node": "dgx",
    "ssh_destination": "user@dgx",
    "identity_file": "/absolute/path/to/existing-ssh-key",
    "known_hosts_file": "/absolute/path/to/known_hosts",
    "remote_python": "/usr/bin/python3",
    "remote_script": "/home/user/.local/lib/von-ops/atlas_egress_reconciler.py",
    "remote_config": "/home/user/.config/von/atlas-egress-reconciler.json"
  },
  "state_path": "/Users/REPLACE/.local/state/von/atlas-egress-reconciler.json",
  "lock_path": "/Users/REPLACE/.local/state/von/atlas-egress-reconciler.lock",
  "notification_interval_seconds": 86400
}
```

The SSH identity and pinned known-hosts file may be the same ones already used
by the supervised Mongo tunnel. The peer invocation deliberately ignores
ambient SSH configuration, disables interactive authentication and connection
sharing, and enables strict host-key checking.

## 4. First reconciliation and preflight

Run the DGX's own first reconciliation from the client:

```sh
ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/absolute/path/to/known_hosts \
  -i /absolute/path/to/existing-ssh-key user@dgx \
  /usr/bin/python3 \
  /home/user/.local/lib/von-ops/atlas_egress_reconciler.py \
  --config /home/user/.config/von/atlas-egress-reconciler.json reconcile
```

Then reconcile the mobile client and exercise both API paths without mutation:

```sh
.venv/bin/python scripts/atlas_egress_reconciler.py reconcile
.venv/bin/python scripts/atlas_egress_reconciler.py preflight
```

The first command may add a current `/32`; it removes a prior named entry only
after Atlas activation, canonical read-back and the target-node TLS probe. The
preflight must report both nodes successful before unattended installation.

For a non-mutating plan against the current access list:

```sh
.venv/bin/python scripts/atlas_egress_reconciler.py cycle --dry-run
```

## 5. Install the mobile scheduler

Install a per-user LaunchAgent after preflight succeeds:

```sh
.venv/bin/python scripts/install_macos_atlas_egress_reconciler.py install \
  --python-executable "$PWD/.venv/bin/python" \
  --script-path "$PWD/scripts/atlas_egress_reconciler.py" \
  --config-path "$HOME/.config/von/atlas-egress-reconciler.json" \
  --interval-seconds 3600
```

It runs at login and hourly. Hourly polling is the bounded substitute for
modifying Von core to emit a network-change or Mongo-failure event; an operator
can also invoke `cycle` immediately after a network move or a classified tunnel
failure. The LaunchAgent contains paths only, never Atlas or Mongo credentials.

Check it with:

```sh
.venv/bin/python scripts/install_macos_atlas_egress_reconciler.py status
.venv/bin/python scripts/atlas_egress_reconciler.py preflight
```

The latest cycle receipt is owner-only at the configured `state_path`; logs are
under `~/Library/Logs/Von/atlas-egress-reconciler.*.log`. Exit `0` means the
bounded operation succeeded, exit `2` means a cycle completed in degraded
state, and exit `1` means configuration or execution itself failed.

## 6. Manual recovery message

When both Admin API paths fail, the receipt and notification tell the operator
which observed `/32` and exact comment to add. The manual sequence is:

1. Atlas Project → Security → Network Access.
2. Add the reported IPv4 as `/32` with the reported exact managed comment.
3. Wait until Atlas reports it active.
4. Rerun `cycle` or `reconcile`.
5. Let the tool remove the prior named entry after its TLS proof; do not delete
   the old entry first.

Also inspect the classification. An SSH listener with downstream connection
timeouts can mean a stale DGX database access entry, but it can also mean DGX
DNS, routing, firewall or Atlas reachability failure. The Admin API result,
entry status and target-node TLS probe distinguish these cases without changing
Von runtime behaviour.

## 7. Removal and secret rotation

Uninstall is explicit and removes only the matching LaunchAgent plist:

```sh
.venv/bin/python scripts/install_macos_atlas_egress_reconciler.py uninstall \
  --confirm-uninstall
```

It retains the config, Keychain item, DGX helper, state receipt and logs. Remove
or rotate those separately only after confirming another access-management
path is working. During Atlas secret rotation, install the replacement secret
on the applicable node, run `preflight`, and only then revoke the old secret.
