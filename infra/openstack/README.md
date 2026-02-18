# OpenStack Terraform IaC for Von

This directory provides reusable Terraform infrastructure for deploying Von on OpenStack/Catalyst Cloud.

## Scope

The stack composes four modules:

- `modules/security_group`: security group and rule management.
- `modules/compute_instance`: primary compute instance and network port.
- `modules/persistent_volume`: attachable block storage volume.
- `modules/floating_ip`: floating IP allocation and association.

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

## Guardrails built in

- Required input checks (`network_id`, `subnet_id`, `image_id`, `key_pair_name`, etc.).
- Naming convention check for `name_prefix`.
- CIDR validation on SSH ingress, app ingress, and egress lists.
- Safety toggle for production change operations in the PowerShell runner.
