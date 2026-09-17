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

## Current delivery decision and evidence

The bounded emulator/controller capability passed live acceptance on 13 September
2026. Core PR #682 merged on 17 September 2026; this separate follow-on
now integrates the accepted core and current task timing/retry changes. No
production Android registry or emulator is enabled by publication.

131 targeted integration tests pass, covering worker/inbox/provisioner regressions, task and
organisation denial, duplicate requests, retained uncertain effects, shared
capacity contention and symlink-safe controller mailboxes.

The first admission was correctly refused at 10.17 GiB estimated available
versus the unchanged 14 GiB requirement (8 GiB reserve plus 6 GiB launch headroom).
After Michael requested continuation and headroom reached 19.18 GiB, a fresh
bridge attempt ran live acceptance. The ADB build rejected a hostname-qualified
`-L` endpoint; the candidate now uses its supported `-P` private port with default
loopback binding. Per-run host logs survive disposable AVD cleanup.

Verified route: this coding agent wrote task-bound requests to the existing
controller handler on DGX, under Codex VS Code's canonical task authority; that
handler invoked the enrolled service through a private SSH StreamLocal relay to
the Mac. No second coding worker or model was started. The normal worker polling
hook is covered by tests; it was not deployed to the production DGX worker.

Live run `native-acceptance-03` verified:

- Android17 / SDK37 / build CE2A.260420.019, Chrome145.0.7632.218,
  Gboard17.2.2.895242737, 3 GiB guest RAM and two cores;
- native Chrome navigation, text entry and a Gboard key tap; repeating the same
  key-tap request returned the same receipt and inserted only one character;
- repeating start retained the same emulator PID; competing admission was denied;
- screenshot and report attachments through canonical task services; all three
  downloaded byte hashes matched their canonical source hashes;
- another organisation context was denied before access to the host;
- supervisor interruption terminated emulator and ADB, removed mutable AVD data
  and the control socket, and released capacity; repeating terminal start did
  not relaunch; post-cleanup available memory was 17.43 GiB.

Canonical task evidence:

- `#V#computer_file_copy_8daf6dd0ef6c4a59a64db9eddb30819a` (initial screenshot)
- `#V#computer_file_copy_98e796f288344301b52d685109593166` (native keyboard screenshot)
- `#V#computer_file_copy_08b649de8635494fb152e7103190c67d` (environment/action report)

The report records host operations, not browser DOM input-event telemetry or a
claim that image pasting is fixed. Chrome152 and physical-device coverage remain
outside this acceptance. For release, reconcile this follow-on against the
accepted core package and run applicable CI. Do not repeat the completed native
acceptance merely because a controller output/comment changed.
