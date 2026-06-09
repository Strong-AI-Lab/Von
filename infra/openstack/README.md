# OpenStack Terraform IaC for Von

This directory provides reusable Terraform infrastructure for deploying Von on OpenStack/Catalyst Cloud.

If you want a plain-language, researcher-focused walkthrough, start with:

- `docs/engineering/openstack_deployment.md`
- `docs/engineering/environment_minimums.md`

## Scope

The stack composes four modules:

- `modules/security_group`: security group and rule management.
- `modules/compute_instance`: primary compute instance and network port.
- `modules/persistent_volume`: attachable block storage volume.
- `modules/floating_ip`: floating IP allocation and association.

When `enable_managed_bootstrap=true` (default), the stack also renders cloud-init
bootstrap assets from `templates/` to configure a runnable Von host:

- non-root Linux service account (`bootstrap_service_user` / `bootstrap_service_group`)
- `systemd` service unit (`von.service`) with restart policy and boot-time enablement
- NGINX reverse proxy with HTTP->HTTPS redirect and TLS termination
- host-side deployment script with health checks and rollback-on-failure

## Prerequisites

- Terraform `>= 1.5.0`.
- OpenStack credentials available at runtime via:
  - `OS_CLOUD` + `clouds.yaml`, or
  - direct `OS_*` environment variables.
- Existing OpenStack network/subnet/image/key pair values for your environment.

## Environment templates

Tracked templates live under:

- `environments/dev/dev.tfvars.example`
- `environments/staging/staging.tfvars.example`
- `environments/prod/prod.tfvars.example`

Create real local var files (untracked):

1. Copy the template for your environment to `*.tfvars` in the same folder.
2. Fill in project-specific IDs and CIDRs.
3. Keep secrets out of these files. Use runtime OpenStack auth environment variables instead.

Example:

```powershell
Copy-Item .\infra\openstack\environments\dev\dev.tfvars.example .\infra\openstack\environments\dev\dev.tfvars
```

## PowerShell workflow

Use the repo script for repeatable operations:

`scripts/powershell/invoke_openstack_terraform.ps1`

Examples:

```powershell
# Validate dev configuration and guardrails
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action validate -Environment dev

# Generate a plan
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action plan -Environment dev

# Apply (explicit approval flag)
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action apply -Environment dev -AutoApprove

# Destroy
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action destroy -Environment dev -AutoApprove
```

Production applies and destroys are blocked unless `-AllowProdChanges` is supplied.

## Managed bootstrap and deploy flow

Managed bootstrap uses `user_data` generated from
`templates/cloud-init/von_bootstrap.yaml.tftpl` unless you provide explicit
`user_data`.

Bootstrap behaviour:

- installs runtime dependencies (`python3`, `python3-venv`, `git`, `nginx`)
- creates and configures service/runtime directories under `/opt/von`
- writes `/etc/systemd/system/von.service` and `/etc/nginx/sites-available/von.conf`
- writes `/etc/von/von.env` with secure cookie defaults, OAuth startup controls,
  and durable workflow startup enabled for real chat/workflow turns
- enables and starts `nginx` and `von`
- performs first deployment by invoking `/usr/local/bin/deploy_von_release.sh`

The generated deploy script executes:

1. preflight checks (required tools, user, filesystem)
2. release activation + `systemctl restart von`
3. post-restart `GET /health` validation
4. automatic rollback to the previous release if health checks fail
5. append a JSON audit event to `/var/log/von/deploy_audit.jsonl`

You can trigger later deployments directly on the host:

```bash
sudo /usr/local/bin/deploy_von_release.sh --repo-url https://github.com/Strong-AI-Lab/Von.git --repo-ref main
```

Or deploy a pre-built versioned artefact (for CI/CD pipelines):

```bash
sudo /usr/local/bin/deploy_von_release.sh --artifact-path /tmp/von-<commit>.tar.gz --repo-ref main
```

## GitHub Actions deployment pipeline

Workflow: `.github/workflows/openstack-deploy.yml`

- Pull requests touching OpenStack deployment files run CI gates:
  - lint gate (`terraform fmt -check`, deploy script syntax check)
  - type gate (`terraform validate`)
  - tests (`tests/infra/test_openstack_deploy_templates.py`)
  - versioned release artefact build/upload (`von-<sha>.tar.gz`)
- Manual `workflow_dispatch` can deploy non-interactively to the target host over SSH.
- Deployment summary includes release metadata and captured deploy audit payload.

Required environment secrets for workflow-dispatch deploy:

- `OPENSTACK_DEPLOY_HOST`
- `OPENSTACK_DEPLOY_USER`
- `OPENSTACK_DEPLOY_SSH_PRIVATE_KEY`
- `OPENSTACK_DEPLOY_PORT` (optional, defaults to `22`)
- `OPENSTACK_DEPLOY_KNOWN_HOSTS` (recommended; if omitted, host key checking is disabled for that run)

## Monitoring, Alerting, and Recovery Automation

Managed bootstrap now installs systemd-driven reliability automation:

- `von-monitor.timer` -> `/usr/local/bin/von_monitor_health.sh`
  - checks service uptime (`von.service`)
  - checks `/health` response
  - checks auth-failure and DB-failure rates from journald window
  - checks TLS expiry threshold
- `von-log-collector.timer` -> `/usr/local/bin/von_collect_logs.sh`
  - consolidates app/service/proxy logs into `/var/log/von/central/`
- `von-backup.timer` -> `/usr/local/bin/von_backup_snapshot.sh`
  - creates backup archives in `/var/backups/von`
- `von-restore-drill.timer` -> `/usr/local/bin/von_restore_drill.sh`
  - validates latest backup extractability and required artefacts

Audit logs:

- monitoring: `/var/log/von/monitoring_events.jsonl`
- central-log collection: `/var/log/von/central_log_audit.jsonl`
- backup: `/var/log/von/backup_audit.jsonl`
- restore drill: `/var/log/von/restore_drill_audit.jsonl`
- deploy: `/var/log/von/deploy_audit.jsonl`

Optional alert webhook:

- set `bootstrap_alert_webhook_url` to deliver operational alerts.
- if unset, alerts are logged locally (no external delivery).

Runbooks:

- `docs/engineering/openstack_operations_runbooks.md`

## OAuth runtime secret injection

Managed bootstrap supports production-safe OAuth startup hardening without
committing plaintext credentials:

- `bootstrap_google_oauth_strict_startup=true` enables fail-fast startup checks.
- `bootstrap_google_oauth_redirect_uri` should be set to the deployed HTTPS callback URL.
- OAuth secret values can be injected via runtime variables
  (`TF_VAR_bootstrap_google_oauth_client_id`, `TF_VAR_bootstrap_google_oauth_client_secret`)
  or by pre-provisioning secret files on host:
  - `bootstrap_google_oauth_client_id_file` (default `/etc/von/secrets/google_oauth_client_id`)
  - `bootstrap_google_oauth_client_secret_file` (default `/etc/von/secrets/google_oauth_client_secret`)
- `bootstrap_flask_secret_key` should be set to a strong value (32+ characters) when strict startup is enabled.

Do not commit real OAuth secrets in tracked tfvars files.

## LLM provider bootstrap

Managed bootstrap can also seed the provider secret and scoped model setting
needed for real chat/workflow execution:

- `bootstrap_openai_api_key_file` defaults to `/etc/von/secrets/openai_api_key`.
- OpenAI keys can be injected with `TF_VAR_bootstrap_openai_api_key` or by
  pre-provisioning `bootstrap_openai_api_key_file` on host.
- `bootstrap_default_llm_provider`, `bootstrap_default_llm_model`, and either
  `bootstrap_default_llm_organisation_concept_id` or
  `bootstrap_default_llm_user_concept_id` can seed the scoped LLM setting after
  first deploy using Von's settings service.

Do not assign `bootstrap_openai_api_key = null` in local tfvars when using
`TF_VAR_bootstrap_openai_api_key`; tfvars values override runtime environment
variables.

Operational note:

- `/health` can be green before the workflow/model path is actually usable.
  After deploy, check `/admin/workflow_materialisation_diagnostics` and run a
  real `/von/generate` smoke test before closing the environment task.

## Mongo Atlas hardening and startup probes

Managed bootstrap also supports MongoDB Atlas startup hardening:

- `bootstrap_mongo_strict_startup=true` enforces fail-fast startup validation.
- `bootstrap_mongo_require_tls=true` enforces TLS URI policy checks.
- `bootstrap_mongo_allowed_host_suffixes` defaults to `[".mongodb.net"]`.
- `bootstrap_mongo_allow_local_fallback` should be `false` for hosted staging/prod.
- `bootstrap_mongo_startup_probe=true` runs auth/read/write probe checks at service startup.
- `bootstrap_mongo_uri_file` defaults to `/etc/von/secrets/mongo_uri`.

Secret handling:

- do not set `bootstrap_mongo_uri = null` in tfvars when using
  `TF_VAR_bootstrap_mongo_uri`; tfvars values override runtime environment
  variables.
- inject secrets at runtime using `TF_VAR_bootstrap_mongo_uri`, or pre-provision
  `bootstrap_mongo_uri_file` on host.

Operational note:

- Atlas project database network access should be restricted to approved host/NAT
  egress addresses. For Catalyst VMs with a floating IP, add the floating IP
  used for outbound Atlas connections.
- Atlas Admin API calls have a separate API-key or service-account access list:
  the operator machine that runs discovery or access-list scans must also be
  allowed there.
- after deploy, use `/admin/db/health?probe=rw` for basic DB auth/read/write verification.

## Guardrails built in

- Required input checks (`network_id`, `subnet_id`, `image_id`, `key_pair_name`, etc.).
- Naming convention check for `name_prefix`.
- CIDR validation on SSH ingress, app ingress, and egress lists.
- Safety toggle for production change operations in the PowerShell runner.
- HTTPS reverse proxy defaults (HTTP redirect + TLS files), with optional self-signed bootstrap cert generation.
- Deployment health gating with rollback in managed host deploy script.
- Deployment audit logging (`deploy_audit.jsonl`) for version/time/outcome traceability.
- Monitoring/backup/restore-drill timers with auditable JSONL event logs.
- Strict OAuth startup mode rejects unsafe/missing hosted OAuth configuration.
- Strict Mongo startup mode rejects unsafe/missing hosted Mongo configuration.
