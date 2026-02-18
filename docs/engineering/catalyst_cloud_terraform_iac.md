# Catalyst Cloud Terraform IaC Deployment

This guide documents a repeatable infrastructure-as-code workflow for provisioning Von infrastructure on Catalyst Cloud/OpenStack.

The Terraform stack lives in `infra/openstack/` and is designed for environment-scoped deployments (`dev`, `staging`, `prod`).

## What the stack provisions

- Compute instance (`openstack_compute_instance_v2`) and managed NIC/port.
- Security group and ingress/egress network controls.
- Persistent block storage volume (optional, enabled by default).
- Floating IP allocation/association (configurable for new or existing addresses).

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

## Related docs

- `infra/openstack/README.md`
- `docs/engineering/catalyst_cloud_swift_setup.md`
