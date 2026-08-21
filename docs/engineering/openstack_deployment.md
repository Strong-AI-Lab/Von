# Von OpenStack Deployment Manual (Researcher-Friendly)

This manual is a practical, step-by-step guide for deploying and testing Von on Catalyst Cloud/OpenStack if you are an AI researcher rather than a cloud engineer.

Use this as your primary checklist. For deeper operations detail, see:
- `docs/engineering/catalyst_cloud_terraform_iac.md`
- `docs/engineering/openstack_operations_runbooks.md`
- `docs/engineering/environment_minimums.md`

## 1) What you need before starting

### 1.1 Access and tools checklist

- [ ] You can open a PowerShell terminal on your local machine.
- [ ] `terraform` is installed and available on PATH.
- [ ] You have OpenStack credentials (`OS_CLOUD` profile or `OS_*` variables).
- [ ] You have this repo cloned locally.
- [ ] You have one environment selected: `dev`, `staging`, or `prod`.

Quick check:

```powershell
terraform -version
```

### 1.2 Values you must collect first

Ask your cloud admin (or copy from your OpenStack project) before proceeding:

- [ ] `region_name`
- [ ] `network_id`
- [ ] `subnet_id`
- [ ] `external_network_pool`
- [ ] `image_id`
- [ ] `key_pair_name`
- [ ] allowed SSH source CIDR (your IP, usually `/32`)
- [ ] allowed HTTPS source CIDR(s)
- [ ] domain name for Von (for example `dev-von.example.org`)

If you do not have these, stop here and gather them first.

## 2) Create your local environment config

From repo root:

```powershell
Copy-Item .\infra\openstack\environments\dev\dev.tfvars.example .\infra\openstack\environments\dev\dev.tfvars
```

Then edit the new `dev.tfvars` file and replace all `REPLACE_WITH_*` placeholders.

Minimum settings to check in your tfvars:

- `environment = "dev"`
- `enable_managed_bootstrap = true`
- `bootstrap_domain_name` set to your DNS name
- `bootstrap_enable_https = true`
- `ssh_ingress_cidrs` restricted to your IP
- `application_ingress_rules` include ports `80` and `443` only for approved sources

## 3) Set secrets safely (do not commit)

Do not put real secrets in tracked tfvars files.

Set runtime secrets in your current PowerShell session:

```powershell
$env:TF_VAR_bootstrap_flask_secret_key = '<YOUR-SECRET-HERE>'
$env:TF_VAR_bootstrap_google_oauth_client_id = '<YOUR-CLIENT-ID-HERE>'
$env:TF_VAR_bootstrap_google_oauth_client_secret = '<YOUR-CLIENT-SECRET-HERE>'
$env:TF_VAR_bootstrap_mongo_uri = '<YOUR-MONGODB-URI-HERE>'
$env:TF_VAR_bootstrap_openai_api_key = '<YOUR-OPENAI-KEY-HERE>'
# or, for Gemini
$env:TF_VAR_bootstrap_gemini_api_key = '<YOUR-GEMINI-KEY-HERE>'
```

Set OpenStack auth profile (example):

```powershell
$env:OS_CLOUD = 'catalystcloud'
```

## 4) Validate and plan before applying

Run these from repo root:

```powershell
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action validate -Environment dev
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action plan -Environment dev
```

Expected result:

- `validate` completes with no errors.
- `plan` completes and writes a `.tfplan` file.

If validation fails, fix tfvars values first. Do not apply until validation passes.

## 5) Deploy (apply)

```powershell
.\scripts\powershell\invoke_openstack_terraform.ps1 -Action apply -Environment dev -AutoApprove
```

Expected result:

- Terraform finishes successfully.
- VM is created.
- Managed bootstrap configures Von + NGINX.

## 6) Post-deploy smoke test checklist

### 6.1 Basic service checks

- [ ] Open `https://<your-domain>/health` in a browser (or use `curl`).
- [ ] Response contains `"status":"healthy"`.

### 6.2 Database/auth path checks

From the VM (SSH in), run:

```bash
curl -fsS http://127.0.0.1:5000/health
curl -fsS http://127.0.0.1:5000/admin/db/health?probe=rw
```

- [ ] `/health` succeeds.
- [ ] `/admin/db/health?probe=rw` succeeds and reports probe success.
- [ ] The DB health output is safe to paste in tickets or chat: it contains only
      redacted Mongo connection location metadata, not credentials, auth DB,
      database path, or URI query parameters.

### 6.3 System services and timers

On VM:

```bash
sudo systemctl status von.service --no-pager
sudo systemctl status nginx.service --no-pager
systemctl list-timers | grep -E 'von-(monitor|log-collector|backup|restore-drill)'
```

- [ ] `von.service` is active.
- [ ] `nginx.service` is active.
- [ ] reliability timers are present.

### 6.4 Workflow/model bootstrap checks

For hosted LLM-backed workflows, set the scoped model seed values before apply:

```powershell
$env:TF_VAR_bootstrap_default_llm_provider = 'openai'
$env:TF_VAR_bootstrap_default_llm_model = 'gpt-5.5'
$env:TF_VAR_bootstrap_default_llm_organisation_concept_id = '#V#university_of_auckland_strong_ai_lab'
```

For Gemini 3.7 Flash, use the corresponding provider/model seed values:

```powershell
$env:TF_VAR_bootstrap_default_llm_provider = 'gemini'
$env:TF_VAR_bootstrap_default_llm_model = 'gemini-3.7-flash'
```

After deployment, check:

```bash
curl -fsS https://<your-domain>/admin/workflow_materialisation_diagnostics
```

- [ ] Durable workflow startup is no longer stuck in `initialising`.
- [ ] A basic chat request succeeds rather than failing with `workflow_definition_not_found`.

## 7) Functional testing checklist (after deployment)

Use this quick pass:

- [ ] Von web UI loads over HTTPS.
- [ ] Login works (if OAuth enabled for the environment).
- [ ] You can send a basic chat request and receive a response.
- [ ] A second chat request still works (sanity for session persistence).
- [ ] If using RAG features, run one RAG query and verify it returns data.

Optional backend verification (from repo root):

```powershell
$env:VON_DB_NAME='test_von_db'
pytest tests/infra/test_openstack_deploy_templates.py
```

## 8) Rolling out code updates after initial deployment

On VM:

```bash
sudo /usr/local/bin/deploy_von_release.sh --repo-url https://github.com/Strong-AI-Lab/Von.git --repo-ref main
```

This script performs health-gated rollout and automatic rollback if health fails.

## 9) Quick rollback/recovery checklist

If service is unhealthy:

1. Check service logs:
   - `sudo journalctl -u von.service --since "-30 min" --no-pager`
2. Re-run deploy script (it includes rollback logic):
   - `sudo /usr/local/bin/deploy_von_release.sh --repo-ref main`
3. Re-test:
   - `curl -fsS http://127.0.0.1:5000/health`
   - `curl -fsS http://127.0.0.1:5000/admin/db/health?probe=rw`
   - The DB health response is safe to paste because Mongo credential material
     and raw URI query/path details are redacted.

If still broken, use:
- `docs/engineering/openstack_operations_runbooks.md`

## 10) Common mistakes to avoid

- Do not commit real secrets into tfvars or git.
- Do not open SSH/HTTPS ingress to `0.0.0.0/0` unless explicitly approved.
- Do not run backend tests against `VON_DB_NAME=von_db`.
- Do not skip `validate` and `plan` before `apply`.
- Do not confuse a green `/health` with a complete chat-capable deployment;
  workflow/materialisation diagnostics and a real chat smoke test are required.

## 11) “Done” checklist for a healthy deployment

- [ ] Terraform validate/plan/apply all succeeded.
- [ ] HTTPS endpoint is reachable.
- [ ] `/health` returns healthy.
- [ ] `/admin/db/health?probe=rw` passes.
- [ ] Any copied DB health output contains only redacted Mongo location metadata.
- [ ] `von.service` and `nginx.service` are active.
- [ ] Monitoring/backup timers are active.
- [ ] Basic UI + chat test succeeded.
