# Von Environment Minimums and Cloud Development

This guide is the canonical reference for the smallest environment-variable sets
needed to run Von locally, test safely, and stand up a minimal cloud development
environment.

## 1. Local workstation minimum

If MongoDB is already running locally and you are happy with the default local
Ollama path, Von can start without any explicit environment variables because:

- `MONGO_URI` defaults to `mongodb://localhost:27017/`
- `VON_DB_NAME` defaults to `von_db`
- fallback to a different local Mongo instance is disabled by default
- the active LLM provider falls back to Ollama when no explicit remote provider
  is configured

For an explicit local shell, use:

```powershell
$env:MONGO_URI = 'mongodb://localhost:27017/'
$env:VON_DB_NAME = 'von_db'
```

If you do not want to rely on local Ollama defaults, add one provider option:

```powershell
$env:OPENAI_API_KEY = '<YOUR-OPENAI-KEY>'
# or
$env:GEMINI_API_KEY = '<YOUR-GEMINI-KEY>'
# or
$env:OLLAMA_HOST = 'http://<host>:11434'
```

`GEMINI_API_KEY` is the primary Gemini variable. Von also accepts the legacy
`GOOGLE_API_KEY` name for compatibility, but new setups should use
`GEMINI_API_KEY`. Managed deployments can instead set
`GEMINI_API_KEY_FILE=/etc/von/secrets/gemini_api_key`; the file should be
readable only by the Von service account. The default Gemini model is
`gemini-3.7-flash` (overridable with `VON_DEFAULT_GEMINI_MODEL`). The tracked
Gemini 3.7 premium-profile release input selects the stateless Interactions API
with response storage disabled; it takes effect only after governed registry
publication and canonical read-back.

## 2. How Von decides whether MongoDB is local or remote

Von does not have a separate `local/cloud` switch for MongoDB. It inspects the
effective Mongo URI host and logs one of:

- `Connecting to LOCAL MongoDB at ...`
- `Connecting to REMOTE MongoDB at ...`

That means cloud behaviour is controlled by the Mongo URI and startup guardrails,
not by a separate deployment-mode flag.

## 3. Test environment minimum

For backend tests, do not use `von_db`.

Use:

```powershell
$env:VON_DB_NAME = 'test_von_db'
```

For mock-only test flows that should not require MongoDB at all:

```powershell
$env:VON_USE_MOCK_DB = '1'
```

`run.ps1` intentionally blocks starting the real server with
`VON_DB_NAME=test_von_db`.

## 4. Minimum sane cloud runtime

For a remote host or coding-agent session running against Atlas:

```powershell
$env:MONGO_URI = '<YOUR-ATLAS-URI>'
$env:VON_DB_NAME = 'von_db'
$env:MONGO_ALLOW_LOCAL_FALLBACK = '0'
$env:VON_SKIP_BROWSER_LAUNCH = '1'
$env:FLASK_SECRET_KEY = '<32+ chars>'
```

Why these matter:

- `MONGO_ALLOW_LOCAL_FALLBACK=0` prevents Atlas failures from silently falling
  back to localhost
- `MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS=<loopback host:port list>` enables a
  secret-free, supervised SSH transport fallback. Von derives credentials from
  the primary URI in memory, validates the expected replica set when configured,
  and selects only the writable forwarded member. The SSH process itself must
  be supervised separately; on macOS use
  `scripts/install_macos_mongo_ssh_tunnel.py`.
- `MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET=<set name>` pins the forwarded members
  when the set name cannot be derived from an existing URI
- `MONGO_DNS_FALLBACK_URI=<direct-host Atlas URI>` can still be used as a
  non-local recovery path when SRV/DNS resolution is flaky
- `VON_MONGO_FALLBACK_STICKY_SECONDS=300` controls how long a working fallback
  is reused before Von probes the direct primary route again
- `VON_SKIP_BROWSER_LAUNCH=1` avoids remote hosts trying to open a local browser
- `FLASK_SECRET_KEY` is required for sane hosted session handling and is
  mandatory when strict hosted OAuth startup is enabled

### macOS supervised Atlas SSH fallback

Use a per-user launchd agent to own a persistent tunnel independently of the
Von process. It starts within that user's GUI login domain, restarts after SSH
or network failures, and is not a pre-login system daemon. Supply one forward
for every Atlas replica-set member so Von can select the current writable
primary after an election. The installer writes no Mongo credentials to the
LaunchAgent; it accepts SSH transport details only.

```sh
.venv/bin/python scripts/install_macos_mongo_ssh_tunnel.py install \
  --ssh-destination operator@relay.example \
  --identity-file /absolute/path/to/private-key \
  --known-hosts-file /absolute/path/to/known_hosts \
  --forward 27018:atlas-member-00.example:27017 \
  --forward 27019:atlas-member-01.example:27017 \
  --forward 27020:atlas-member-02.example:27017
```

The SSH identity must be owner-only, the relay must already have a pinned host
key in the selected known-hosts file, and the local ports must not be owned by
another process. The generated service uses strict host-key checking and
isolates itself from ambient SSH configuration. Then configure Von and restart
it, because these values are loaded when the backend module starts:

```dotenv
MONGO_ALLOW_LOCAL_FALLBACK=0
MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS=127.0.0.1:27018,127.0.0.1:27019,127.0.0.1:27020
MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET=atlas-example-shard-0
```

Explicitly pinning the actual replica-set name is strongly recommended,
especially for an SRV primary URI that does not carry a `replicaSet` option.
Check launchd and listener ownership without repeating the forward list:

```sh
.venv/bin/python scripts/install_macos_mongo_ssh_tunnel.py status
./run.sh restart -NoBrowser -HealthTimeoutSec 180
./run.sh status
```

Installer status proves only that the launchd-managed SSH PID owns each
configured loopback listener; it does not probe Mongo. While the primary route
is healthy, `./run.sh status` should continue to report `Mongo: Atlas`. A
functional fallback test must report `Mongo: Atlas via SSH tunnel (...)` and
complete a Mongo ping or canonical read through that route.

The status command exits successfully when the query itself completed, even
when the service is absent or degraded. For a healthy listener layer, require
`configuration_status="configured"`, `plist_valid=true`,
`label_matches=true`, `installed=true`, `loaded=true`,
`listeners_checked=true`, and `listener_coverage_complete=true`.
`mongo_route_probed=false` is expected until a separate Mongo operation is
performed.

Before removal, delete or disable
`MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS`, restart Von, and verify that its live
Mongo route no longer depends on the tunnel. Removal is then explicit:

```sh
.venv/bin/python scripts/install_macos_mongo_ssh_tunnel.py uninstall \
  --confirm-uninstall
.venv/bin/python scripts/install_macos_mongo_ssh_tunnel.py status
```

Uninstall removes the LaunchAgent plist, but retains the SSH key,
known-hosts file, and `~/Library/Logs/Von` tunnel logs.

Mongo still validates the remote certificate chain. Because a forwarded Atlas
member is contacted at a loopback hostname, only hostname matching is relaxed
for URIs derived from syntactically validated loopback endpoints. The
authenticated SSH tunnel, strict relay host-key check, writable-primary check,
and expected replica-set check when configured bound that exception. Do not
expose these ports on a non-loopback interface.

Fallback selection is failure-sensitive: a pure DNS/SRV failure tries the
direct-host Atlas URI first; TLS, TCP, periodic health-check transport failures,
and operation failures handled by the shared retry path try the SSH route
first. An explicitly enabled local-development Mongo fallback is always last.
A working remote fallback is reused for a bounded window and Von then probes
the direct primary again.

For stricter hosted Mongo startup, also set:

```powershell
$env:VON_MONGO_STRICT_STARTUP = '1'
$env:VON_MONGO_STARTUP_PROBE = '1'
$env:VON_MONGO_REQUIRE_TLS = '1'
$env:VON_MONGO_ALLOWED_HOST_SUFFIXES = '.mongodb.net'
```

## 5. Browser testing on a cloud host

Von does not currently include Playwright or Cypress browser e2e tests in this
repo. Frontend automated coverage is Jest + jsdom. Browser testing against a
cloud deployment is therefore manual smoke testing over HTTPS.

For browser testing without login:

- public HTTPS in front of Von is enough
- the supported OpenStack path keeps Flask bound to `127.0.0.1` and exposes it
  via NGINX

For browser testing with Google login:

```powershell
$env:GOOGLE_OAUTH_CLIENT_ID = '<YOUR-CLIENT-ID>'
$env:GOOGLE_OAUTH_CLIENT_SECRET = '<YOUR-CLIENT-SECRET>'
$env:GOOGLE_OAUTH_REDIRECT_URI = 'https://<your-domain>/von/api/auth/google/callback'
$env:GOOGLE_OAUTH_STRICT_STARTUP = '1'
```

Important constraints:

- the browser must access the same host as `GOOGLE_OAUTH_REDIRECT_URI`
- strict hosted OAuth forbids `GOOGLE_OAUTH_ENABLE_DYNAMIC_REDIRECTS=true`
- dynamic redirects are intended only for local/ngrok-style development

Prefer file-based secret injection on hosted systems when possible:

- `MONGO_URI_FILE`
- `GOOGLE_OAUTH_CLIENT_ID_FILE`
- `GOOGLE_OAUTH_CLIENT_SECRET_FILE`

## 6. Minimum viable cloud development path

Use the tracked OpenStack/Catalyst path in:

- `docs/engineering/openstack_deployment.md`
- `infra/openstack/environments/dev/dev.tfvars.example`

Recommended progression:

1. Start with anonymous browser access only.
   - inject `TF_VAR_bootstrap_mongo_uri`
   - keep `bootstrap_mongo_strict_startup=false`
   - keep OAuth unset
   - deploy and verify `/health`, `/admin/db/health?probe=rw`, UI load, and a
     basic chat request
   - `/admin/db/health?probe=rw` output is redacted and safe to paste; it should
     not contain raw Mongo credentials, database path, or URI query parameters

2. Harden Mongo once Atlas connectivity is stable.
   - set `bootstrap_mongo_allow_local_fallback=false`
   - enable `bootstrap_mongo_strict_startup=true`
   - enable `bootstrap_mongo_startup_probe=true`

3. Add Google login only after DNS and HTTPS are stable.
   - inject `TF_VAR_bootstrap_flask_secret_key`
   - inject `TF_VAR_bootstrap_google_oauth_client_id`
   - inject `TF_VAR_bootstrap_google_oauth_client_secret`
   - set `bootstrap_google_oauth_redirect_uri` to the public callback URL
   - enable strict OAuth startup

4. Treat cloud browser validation as manual smoke plus existing repo tests.
   - Jest/jsdom for frontend automation
   - `VON_DB_NAME=test_von_db` for pytest
   - manual browser checks for the live hosted instance
