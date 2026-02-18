# OpenStack Operations Runbooks (Von)

This document defines operational response runbooks for OpenStack-hosted Von.
It complements automated monitoring/alerting and backup/restore-drill timers
configured by the managed bootstrap templates in `infra/openstack/templates/`.

## Scope and Inputs

- Host-level service: `von.service` (application), `nginx.service` (reverse proxy)
- Monitoring script: `/usr/local/bin/von_monitor_health.sh`
- Backup script: `/usr/local/bin/von_backup_snapshot.sh`
- Restore-drill script: `/usr/local/bin/von_restore_drill.sh`
- Deploy script: `/usr/local/bin/deploy_von_release.sh`

Primary logs and audits:

- `/var/log/von/monitoring_events.jsonl`
- `/var/log/von/backup_audit.jsonl`
- `/var/log/von/restore_drill_audit.jsonl`
- `/var/log/von/deploy_audit.jsonl`
- `/var/log/von/central/` (consolidated log snapshots)
- `journalctl -u von.service -u nginx.service`

## Service Outage

### Detection

- Monitoring alert indicates service downtime or `/health` failure.
- `systemctl is-active von.service` reports inactive/failed.

### Immediate Triage

1. Check runtime status:
   - `sudo systemctl status von.service --no-pager`
   - `sudo journalctl -u von.service --since "-30 min" --no-pager`
2. Check proxy path:
   - `sudo systemctl status nginx.service --no-pager`
   - `curl -fsS http://127.0.0.1:5000/health`
3. Confirm current release:
   - `readlink -f /opt/von/current`

### Recovery

1. Retry restart:
   - `sudo systemctl restart von.service`
2. If still failing, roll forward/rollback with deploy script:
   - `sudo /usr/local/bin/deploy_von_release.sh --repo-ref main`
3. Verify:
   - `curl -fsS http://127.0.0.1:5000/health`
4. Confirm audit event appended in `/var/log/von/deploy_audit.jsonl`.

## Auth Failure

### Detection

- Monitoring alerts on repeated auth-failure pattern in recent `von.service` logs.
- Symptoms may include increased `401/403` responses or token validation errors.

### Triage

1. Inspect auth errors:
   - `sudo journalctl -u von.service --since "-30 min" --no-pager | grep -Ei '401|403|auth|invalid token|unauthori[sz]ed'`
2. Check deployment/credential changes since last healthy window:
   - `tail -n 20 /var/log/von/deploy_audit.jsonl`
3. Confirm expected environment values exist:
   - `sudo test -f /etc/von/von.env`
   - `sudo grep -E 'GOOGLE_OAUTH_(REDIRECT_URI|STRICT_STARTUP)' /etc/von/von.env`
4. If file-based OAuth secrets are used, confirm files exist and are non-empty:
   - `sudo test -s /etc/von/secrets/google_oauth_client_id`
   - `sudo test -s /etc/von/secrets/google_oauth_client_secret`

### Recovery

1. Rotate or restore valid credentials in `/etc/von/von.env` and/or `/etc/von/secrets/*` through secure process.
2. Restart service:
   - `sudo systemctl restart von.service`
3. Validate:
   - `/health` response success
   - auth failures drop below threshold in monitoring window.

## DB Credential Rotation

### Preparation

1. Obtain new DB credential and validate connectivity out-of-band.
2. Schedule low-traffic window.

### Execution

1. Update DB credential in `/etc/von/von.env` (or equivalent secret injection path).
2. Restart service:
   - `sudo systemctl restart von.service`
3. Validate:
   - `curl -fsS http://127.0.0.1:5000/health`
   - `sudo journalctl -u von.service --since "-15 min" --no-pager | grep -Ei 'mongo|database|authentication failed|timed out'`

### Rollback

1. Restore previous known-good credential.
2. Restart and verify health.
3. Document the incident and credential version transition.

## Rebuild from IaC

Use this when host integrity is uncertain or recovery-on-host has failed.

1. Preserve latest operational artefacts if possible:
   - backup archives (`/var/backups/von`)
   - audit logs (`/var/log/von/*.jsonl`)
2. Provision replacement host via Terraform:
   - `.\scripts\powershell\invoke_openstack_terraform.ps1 -Action apply -Environment <env> -AutoApprove`
3. Verify bootstrap completion:
   - `von.service` and `nginx.service` active
   - `/health` succeeds
4. Run deployment to target release:
   - `sudo /usr/local/bin/deploy_von_release.sh --repo-ref main`
5. Re-enable/confirm timers:
   - `systemctl list-timers | grep -E 'von-(monitor|log-collector|backup|restore-drill)'`

## Backup and Restore-Drill Operations

### Backup

- Automated timer: `von-backup.timer`
- Manual trigger:
  - `sudo systemctl start von-backup.service`
- Expected outputs:
  - archive in `/var/backups/von/`
  - audit event in `/var/log/von/backup_audit.jsonl`

### Restore Drill

- Automated timer: `von-restore-drill.timer`
- Manual trigger:
  - `sudo systemctl start von-restore-drill.service`
- Expected outputs:
  - success/failure audit in `/var/log/von/restore_drill_audit.jsonl`
  - alert on failure if webhook configured.

## Escalation Guidance

- If two consecutive monitoring failures occur for the same category, escalate to on-call immediately.
- If rollback cannot restore service health, escalate as sev-1 and initiate rebuild-from-IaC.
- Record all incident actions and timestamps in Jira for traceability.
