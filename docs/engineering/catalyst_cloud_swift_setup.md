# Catalyst Cloud (NZ) Swift setup for Von blob store

This guide explains how to configure Von’s **Swift-backed blob store** against **Catalyst Cloud (New Zealand)**.

Von supports a pluggable blob store backend:
- **Cache**: arXiv’s external MCP server writes PDFs to a local cache directory (default `data/arxiv_cache/`).
- **Durable store**: Von uploads artefacts (e.g. arXiv PDFs) to the configured blob store backend.

This document focuses on configuring the **durable** Swift backend.

## Prerequisites

- You have a Catalyst Cloud account with access to a project/tenant that can use **Object Storage (Swift)**.
- You have either:
  - an **Application Credential** (recommended), or
  - an **OpenStack RC file** (password-based auth).
- You are on Windows using **PowerShell** (this repo’s default shell).

## Safety and security

- Do **not** commit credentials to git.
- Prefer **Application Credentials** over password auth.
- If you create `clouds.yaml`, keep it in your user profile (not in the repo).

## 1) Install dependencies

The Swift backend uses `openstacksdk`.

If you have not installed dependencies for this repo yet:

```powershell
pdm install
```

## 2) Choose an authentication method

Von’s Swift implementation supports standard OpenStack authentication via:
- `OS_CLOUD` pointing at a `clouds.yaml` entry (recommended), or
- environment variables (`OS_AUTH_URL`, `OS_USERNAME`, etc.).

### Option A (recommended): `clouds.yaml` + `OS_CLOUD`

Create this file:

- `C:\Users\<you>\.config\openstack\clouds.yaml`

Example (fill in values from Catalyst Cloud):

```yaml
clouds:
  catalyst:
    region_name: "<your-region>"
    interface: "public"
    identity_api_version: 3
    auth:
      auth_url: "<OS_AUTH_URL>"
      application_credential_id: "<APP_CRED_ID>"
      application_credential_secret: "<APP_CRED_SECRET>"
```

Then set:

```powershell
$env:OS_CLOUD = 'catalystcloud'
```

Important:
- `OS_CLOUD` must match the **cloud name** defined in your `clouds.yaml`.
  - Some Catalyst/OpenStack tooling creates an entry named `catalystcloud` by default. If your `clouds.yaml` uses that name, set `$env:OS_CLOUD = 'catalystcloud'` (or rename the entry to `catalyst`).
- On Windows, if `openstacksdk` is not finding your `clouds.yaml`, you can force the config path:

```powershell
$env:OS_CLIENT_CONFIG_FILE = "$HOME\.config\openstack\clouds.yaml"
```

Notes:
- `region_name` and `auth_url` must match the values Catalyst provides for your project.
- If you maintain multiple OpenStack environments, add multiple entries under `clouds:`.

### Option B: Environment variables (RC file values)

If Catalyst provides a Bash-style RC file (`export OS_...`), copy the values into PowerShell instead:

```powershell
$env:OS_AUTH_URL = '<from RC file>'
$env:OS_USERNAME = '<from RC file>'
$env:OS_PASSWORD = '<from RC file>'
$env:OS_PROJECT_NAME = '<from RC file>'
$env:OS_USER_DOMAIN_NAME = '<from RC file or Default>'
$env:OS_PROJECT_DOMAIN_NAME = '<from RC file or Default>'
$env:OS_REGION_NAME = '<from RC file>'
```

If your RC file provides different variables (e.g. project/tenant IDs), prefer following Catalyst’s docs for your account configuration.

## 3) Configure Von to use Swift

Von selects the blob store backend via environment variables.

Required:

```powershell
$env:VON_BLOB_STORE_BACKEND = 'swift'
$env:VON_SWIFT_CONTAINER = 'von-artifacts'
```

Optional:

```powershell
# Store all Von objects under this prefix within the container
$env:VON_SWIFT_PREFIX = 'von'

# If you have a stable public base URL for object access, you can use it
# to construct an HTTP(S) URI in BlobRef.uri. Otherwise Von will use
# a 'swift://container/key' URI.
$env:VON_SWIFT_PUBLIC_BASE_URL = '<https://...>'
```

### Create the container

The container must exist. Create it in Catalyst Cloud’s dashboard, or via OpenStack CLI (if you have it installed and configured):

```powershell
openstack container create von-artifacts
```

## 4) Smoke test (no server start)

This runs a minimal put/get/list round-trip using Von’s blob store factory.

```powershell
$code = @'
from src.backend.services.blob_store import get_blob_store_from_env

store = get_blob_store_from_env()
ref = store.put_bytes(
    "smoke_test/hello.txt",
    b"kia ora",
    content_type="text/plain",
    metadata={"source": "catalyst_cloud_swift_setup"},
)
print("PUT:", ref)

data = store.get_bytes("smoke_test/hello.txt")
print("GET:", data)

print("EXISTS:", store.exists("smoke_test/hello.txt"))
print("LIST:", store.list("smoke_test"))
'@

pdm run python -c $code
```

Expected behaviour:
- `PUT` prints a `BlobRef` with `backend='swift'`.
- `GET` prints `b'kia ora'`.
- `LIST` includes `smoke_test/hello.txt`.

## 5) arXiv behaviour with Swift enabled

With the Swift backend configured:
- arXiv downloads will still be written to the local cache (default `data/arxiv_cache/`).
- Von will upload the PDF to Swift under a key like `arxiv/papers/<arxiv-id>.pdf`.
- Tool output will include `storage { backend, key, uri }` so downstream components can persist and retrieve the durable location.

## Troubleshooting

### Authentication errors

- Confirm `OS_CLOUD` matches an entry in `clouds.yaml`, or that your `OS_...` environment variables are set correctly.
- Confirm the credential/project has access to **Object Storage**.

### Wrong region or endpoint

- Catalyst Cloud values can vary by project and region. Use the `auth_url` and `region_name` provided for your account.

### Container not found

- Ensure `VON_SWIFT_CONTAINER` exists.
- If you use a prefix, verify `VON_SWIFT_PREFIX` does not contain leading/trailing slashes.

## Related documents

- If you’re configuring arXiv MCP usage as well, see the arXiv MCP integration notes in `AGENTS.md`.
- For general guidance on MCP tool design and reliability, see [docs/engineering/mcp_tools_best_practices.md](docs/engineering/mcp_tools_best_practices.md).
