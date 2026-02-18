# OpenStack Terraform IaC for Von

This directory provides reusable Terraform infrastructure for deploying Von on OpenStack/Catalyst Cloud.

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

## Guardrails built in

- Required input checks (`network_id`, `subnet_id`, `image_id`, `key_pair_name`, etc.).
- Naming convention check for `name_prefix`.
- CIDR validation on SSH ingress, app ingress, and egress lists.
- Safety toggle for production change operations in the PowerShell runner.
- HTTPS reverse proxy defaults (HTTP redirect + TLS files), with optional self-signed bootstrap cert generation.
- Deployment health gating with rollback in managed host deploy script.
- Deployment audit logging (`deploy_audit.jsonl`) for version/time/outcome traceability.
