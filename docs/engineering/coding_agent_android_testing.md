# Optional Android testing for coding-agent instances

This is a **candidate follow-on** to the prepared instance package. It is not
production enabled or accepted yet. It must not delay the core package.

The intended job is native Chrome/Gboard investigation without a connected
phone. The simplest baseline is the existing operator-owned Mac SDK/image,
with new disposable AVD data for each task/run. No emulator or account is
created just because a coding instance is provisioned.

## Authority and routes

`coding_agent_android` uses the existing `VON_CODING_AGENT_REGISTRY`. An
instance can have an optional `android` entry with a fixed `provisioner_command`,
`host` and `unix_account`. The test host can differ from the coding host. The
service accepts only the authenticated enrolled agent or delegator, in the
exact organisation, with current membership and a current task created by the
enrolled delegator and assigned to that agent. No client actor/org/path/command
is accepted. Reserved coding identities can use this capability without
reprovisioning their worker.

The fixed command invokes `scripts/codex_von_android.py --binding PRIVATE_JSON`.
The binding uses the core provisioner's host/account/identity validation and
adds `android`: `platform` (OS/architecture pair), `sdk_root`, `system_image`
(relative to SDK), `abi`, `arch`, `state_root`, `ram_mib`, `cores`, `adb_port`,
`emulator_port`, `host_capacity`, `capacity_enrolment_verified`, and a map of
named `fixtures` to operator-approved disposable URLs. Defaults bound failed
boot occupancy to 600 seconds and idle occupancy to 1800 seconds; legitimate
operations renew idle activity. These protect shared test-host resources, not
whole coding-task duration.

The SDK and image are immutable shared inputs. Mutable AVD data and control
sockets are private to the prepared account/binding and task/run. All test-host
consumers must use the same operator-owned capacity inode; the core package's
single-slot admission serialises consumers. Guest RAM/CPU are explicit launch
arguments; headroom includes host overhead. A reserve check runs during boot
and use. This is not a hard total-RSS guarantee or a defence against arbitrary
same-account processes. Separate untrusted tenants require prepared accounts
and suitable OS confinement.

For a Mac without incoming SSH, `codex_von_android_transport.py` supports a
private Unix listener and client. An operator can use SSH StreamLocal reverse
forwarding from the Mac to a private DGX socket. Both parent directories must be
owner-only. The DGX registry binds the client command to that exact socket; the
Mac listener binds a fixed host configuration. No TCP listener, remote-login
setting, credentials in model context or extra coding worker is needed. The
trusted service/operator owns the socket. Do not expose it directly to an
untrusted model process, since the host trusts the canonical service's prior
authorisation. Transport lifecycle remains operator-managed; this candidate
does not install another scheduler or silently enable a persistent tunnel.

## Operations and evidence

Call `discover` with instance/task IDs. It returns platform, tool versions,
acceleration and configured budgets; it is not proof of available capacity or
a running emulator. `start` also requires a stable `run_id`. Repeating an active
or terminal run returns retained state, rather than launching again. Read
`status` until ready, waiting for capacity, or stopped. A fresh attempt after a
terminal run uses a different run ID, after inspecting retained evidence.

Ready sessions support native `tap`, bounded plain ASCII `text`,
`open_fixture` by enrolled name, `screenshot`, `report`, and `stop`. Screenshot
and run-report bytes travel to the controller, which checks the digest,
rechecks canonical assignment, and uses canonical task attachment services.
Reports identify actual Android/build/Chrome/Gboard versions and native-emulator
coverage. They do not assert physical-device behaviour. The older Android17 /
Chrome145 / Gboard17.2 evidence does not establish Chrome152 acceptance.

`android_instance_id` in a worker configuration exposes the optional capability
in task context. The existing controller polls a task/run-specific request file
inside the coding workspace and writes responses there. The coding child gets
no Mongo credential or general host command. Requests cannot select another
task, actor or host. Private controller receipts deduplicate requests; an
interrupted effect remains uncertain and is not automatically repeated. No
additional worker or poller is installed. Task authority is rechecked by the
canonical service for each operation. Mailbox reads/writes reject symlink
redirection from the coding workspace.

The current single-slot capacity baseline means a coding worker cannot acquire
a second slot for an emulator on its own host while retaining the first. The
intended DGX-to-Mac route uses separate host budgets. Same-host concurrent
coding/emulation needs explicit shared reservation support before activation.

## Current delivery decision and remaining acceptance

Merge decision: **not ready**. This branch is based on unmerged PR #682.
124 targeted tests pass, covering task/org/assignment denial, duplicate launch, command
injection rejection, and the existing shared lock's real contention/inheritance.
The normal canonical service path on DGX successfully discovered the real Mac
SDK through a temporary private SSH socket forward. A subsequent launch was
refused by capacity admission: 10.17 GiB estimated available at the final check, below
8 GiB reserve plus 6 GiB launch headroom. No emulator was launched and the guard
was not reduced to obtain a passing run.

Still required before delivery:

- real launch, environment read-back, native interaction and canonical attachment
  read-back through the supported controller route;
- real interruption/stop and verified cleanup, including stale supervisor/socket
  recovery (targeted coverage does not establish live emulator cleanup);
- demonstrable callable access from the existing coding worker/controller,
  rather than only an operator's canonical service invocation;
- core package integration after its own acceptance, without inheriting this
  follow-on as a gate for the core package.

Broader architectures, Chrome152 and physical devices remain disclosed coverage
extensions. No production deployment or additional coding worker is authorised
by this candidate's test setup.
