# Catalyst Cloud Terraform IaC Deployment

This guide documents a repeatable infrastructure-as-code workflow for provisioning Von infrastructure on Catalyst Cloud/OpenStack.

The Terraform stack lives in `infra/openstack/` and is designed for environment-scoped deployments (`dev`, `staging`, `prod`).

## What the stack provisions

- Compute instance (`openstack_compute_instance_v2`) and managed NIC/port.
- Security group and ingress/egress network controls.
- Persistent block storage volume (optional, enabled by default).
- Floating IP allocation/association (configurable for new or existing addresses).
- Managed VM bootstrap (cloud-init) for Von service user, systemd service, NGINX reverse proxy, and first deploy.

## 1) Prepare authentication (secrets at runtime)

Do not put credentials in repository files.

Use one of:

- `OS_CLOUD` + `%USERPROFILE%\.config\openstack\clouds.yaml`, or
- direct `OS_*` environment variables from your OpenStack account.

Example PowerShell setup:

```powershell
$env:OS_CLOUD = 'catalystcloud'
```

## 2) Prepare environment tfvars

Templates are committed and safe:

- `infra/openstack/environments/dev/dev.tfvars.example`
- `infra/openstack/environments/staging/staging.tfvars.example`
- `infra/openstack/environments/prod/prod.tfvars.example`

Create local untracked files:

```powershell
Copy-Item .\infra\openstack\environments\dev\dev.tfvars.example .\infra\openstack\environments\dev\dev.tfvars
```

Then fill in required values such as `network_id`, `subnet_id`, `image_id`, and `key_pair_name`.

The examples now include managed bootstrap defaults (`enable_managed_bootstrap=true`)
with HTTPS reverse proxy ingress on ports `80` and `443`.

## 3) Run the PowerShell entrypoint

Use:

`scripts/powershell/invoke_openstack_terraform.ps1`

Examples:

```powershell
# Validate schema + variable guardrails
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action validate -Environment dev

# Generate and save plan
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action plan -Environment dev

# Apply
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action apply -Environment dev -AutoApprove
```

Destroy (when needed):

```powershell
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action destroy -Environment dev -AutoApprove
```

Production safety:

- `apply` and `destroy` for `prod` require `-AllowProdChanges`.

## 4) Guardrails included

- Required variable checks for core infra IDs and names.
- Naming convention validation for `name_prefix`.
- CIDR validation for SSH ingress, app ingress, and egress configuration.
- Production-change safety gate in the PowerShell runner.
- Managed deploy flow on host (`preflight -> restart -> health check -> rollback`) via `/usr/local/bin/deploy_von_release.sh`.
- HTTPS-by-default reverse proxy config with optional self-signed bootstrap certificate generation.

## 5) Host-side deployment and rollback

After bootstrap, deploy updates on the VM without manual service wiring:

```bash
sudo /usr/local/bin/deploy_von_release.sh --repo-url https://github.com/Strong-AI-Lab/Von.git --repo-ref main
```

The deploy script will:

1. validate prerequisites
2. activate the new release and restart `von.service`
3. poll `http://127.0.0.1:5000/health`
4. roll back to the previous release automatically if health checks fail
5. append deployment audit metadata to `/var/log/von/deploy_audit.jsonl`

## 6) CI/CD deployment workflow

Workflow file: `.github/workflows/openstack-deploy.yml`

Capabilities:

- CI gates for OpenStack deployment changes:
  - lint (`terraform fmt -check`, shell syntax check of deploy template)
  - type (`terraform validate`)
  - tests (`tests/infra/test_openstack_deploy_templates.py`)
- Build and upload versioned release artefact (`von-<commit>.tar.gz` + SHA256 file)
- Manual non-interactive OpenStack host deployment via SSH with health-gated rollout and rollback
- Deployment summary with release metadata and captured audit payload

Required GitHub environment secrets for deploy runs:

- `OPENSTACK_DEPLOY_HOST`
- `OPENSTACK_DEPLOY_USER`
- `OPENSTACK_DEPLOY_SSH_PRIVATE_KEY`
- `OPENSTACK_DEPLOY_PORT` (optional)
- `OPENSTACK_DEPLOY_KNOWN_HOSTS` (recommended)

## 7) Monitoring, alerting, backup drills, and runbooks

Managed bootstrap installs systemd timers for operational reliability:

- `von-monitor.timer` for uptime/auth/db/TLS checks
- `von-log-collector.timer` for central log snapshots
- `von-backup.timer` for scheduled backup snapshots
- `von-restore-drill.timer` for scheduled backup restore validation

Primary audit trails:

- `/var/log/von/monitoring_events.jsonl`
- `/var/log/von/central_log_audit.jsonl`
- `/var/log/von/backup_audit.jsonl`
- `/var/log/von/restore_drill_audit.jsonl`
- `/var/log/von/deploy_audit.jsonl`

Optional webhook alerting:

- configure `bootstrap_alert_webhook_url` in non-committed environment tfvars or runtime injection path.

Operational incident runbooks:

- `docs/engineering/openstack_operations_runbooks.md`

## 8) Production OAuth hardening and startup validation

Managed bootstrap can enforce secure Google OAuth startup checks:

- set `bootstrap_google_oauth_strict_startup = true`
- set `bootstrap_google_oauth_redirect_uri` to your hosted HTTPS callback URL
- keep `bootstrap_google_oauth_enable_dynamic_redirects = false` for production

Avoid committed plaintext secrets:

- inject `bootstrap_flask_secret_key`, `bootstrap_google_oauth_client_id`, and
  `bootstrap_google_oauth_client_secret` via `TF_VAR_*` runtime environment variables, or
- pre-provision OAuth secret files at:
  - `/etc/von/secrets/google_oauth_client_id`
  - `/etc/von/secrets/google_oauth_client_secret`

With strict startup enabled, Von fails fast with actionable diagnostics when
OAuth configuration is missing, localhost/insecure, or otherwise unsafe.

## 9) Mongo Atlas hardening and credential rotation posture

Managed bootstrap exposes Mongo startup guardrails for hosted deployments:

- `bootstrap_mongo_strict_startup = true` (recommended for staging/prod)
- `bootstrap_mongo_startup_probe = true`
- `bootstrap_mongo_require_tls = true`
- `bootstrap_mongo_allow_local_fallback = false`
- `bootstrap_mongo_allowed_host_suffixes = [".mongodb.net"]`
- `bootstrap_mongo_uri_file = "/etc/von/secrets/mongo_uri"`

Secret handling guidance:

- keep `bootstrap_mongo_uri = null` in committed tfvars.
- inject `TF_VAR_bootstrap_mongo_uri` at apply time, or pre-provision
  `/etc/von/secrets/mongo_uri` out-of-band.

Operational checks:

- restrict Atlas network allowlist to approved host/NAT egress IPs only.
- validate post-deploy DB path with `/admin/db/health?probe=rw`.

With strict startup enabled, Von fails fast with clear diagnostics when Mongo
URI policy or startup connectivity/auth/read/write probe checks fail.

## Related docs

- `docs/engineering/openstack_deployment.md`
- `infra/openstack/README.md`
- `docs/engineering/catalyst_cloud_swift_setup.md`
- `docs/engineering/openstack_operations_runbooks.md`
