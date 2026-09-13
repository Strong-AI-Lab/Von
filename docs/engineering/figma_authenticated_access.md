# Authenticated Figma access for Von coding agents

**Kind:** Runbook and bounded verification record

**Lifecycle:** Active

**Authority:** Advisory; does not grant access or effects

**Owner:** Von engineering

**Last reviewed / evidence as of:** 2026-09-13

**Review trigger:** Figma client registration, OpenAI plugin routing, account
binding, or worker capability changes

## Delivery and evidence

The existing Figma connection worked in the subscription-backed Codex DGX
assignment. Reuse it for authorised Figma work; no new Python client, plugin,
credential copy, backend activation, or public deployment was needed. This is
an authenticated account-read result, not an implementation of Figma access
inside Von's web backend.

Evidence belongs to native task
`#V#task_agent_734cdccf5add884c0b2af9b65db90bc6`, coding run
`486c2aeaf49a4d53b714292e5a6d375d`. The controller-projected canonical task read
succeeded. It supplied no originating conversation locator, attachments or
file-copy bytes, and no Figma file URL. An unavailable conversation is not
evidence that a file does not exist.

| Check | Observed result | Claim boundary |
| --- | --- | --- |
| Session tool discovery | `mcp__codex_apps__figma_*` tools exposed, including `figma_whoami` | Availability alone does not prove authentication |
| Live `mcp__codex_apps__figma_whoami({})` | `isError: false`; returned the assigning user's expected account and one plan | Successful authenticated remote account read through this session's managed connection |
| Repository search | No Figma implementation found in `src`, `scripts`, `tests` or `docs` at baseline `b4106c690d910ac028c231577ac9c26c8d11475c` | Does not inventory private host configuration |
| Local CLI inspection | `codex-cli 0.154.0`; `mcp add --help` and `mcp login --help` succeeded | Syntax support only; no second agent or login launched |

The account email and plan identifier remain in the private tool receipt rather
than this public document. Credentials and host authentication files were not
read. No file content, write, new consent flow, expiry/refresh, revocation,
different-user isolation, future worker session, or direct Von backend path was
tested. The authentication check used an existing grant; it does not prove
normal issuance of a new grant. No authority mechanism changed.

## Architecture: distinguish the surfaces

| Surface | Connection and identity | Consequence for Von |
| --- | --- | --- |
| ChatGPT Figma app/connector or plugin | Hosted integration; user connects the external service through the product | Tools and connection are supplied by that product, not by Von |
| Codex Figma plugin | Packaged integration available through a supported plugin surface | This assignment already had callable managed Figma tools |
| Direct Codex MCP configuration | Streamable HTTP to `https://mcp.figma.com/mcp`, with Figma OAuth | Alternative for a host without the managed connection; configure and authenticate that host |
| Direct Von backend client | Von would be a separately configured client with its own user binding | Not established by a successful Codex tool call |

Terminology has moved: OpenAI's current documentation describes a shared
ChatGPT/Codex plugin catalogue. Plugins can bundle skills and MCP servers;
installation and external-service connection are separate steps. Older
app/connector names do not imply a different protocol, nor do shared catalogue
entries prove identical runtime capabilities. See [OpenAI plugins](https://learn.chatgpt.com/docs/plugins).

Hosted web tools and locally configured MCP servers remain distinct. Web
ChatGPT does not read a local Codex configuration; local clients on the same
Codex host share MCP configuration. The current session's `codex_apps` tool
namespace is observed evidence of its exposed route, not proof of an underlying
local TOML entry. See [OpenAI MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

Figma documents per-user OAuth for its remote server and recommends the Codex
plugin. Its manual Codex alternative uses the hosted MCP endpoint. No local
Figma desktop process is required for that route. See [Figma remote setup](https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/).

## Setup and token ownership

For a session already exposing Figma tools, call `whoami` first. Match the
returned identity to the intended connection owner before accessing private
designs. The Von coding-agent identity records who performed the work; it is
not the Figma account identity and does not confer a Figma seat.

For a new managed connection, find Figma in the product's plugin catalogue,
install/connect it, complete the Figma consent flow under the intended account,
then start a fresh session and repeat the identity check. Installation is not
proof of consent or file permissions. This run did not reinstall or reconnect
the already functioning integration.

For an operator-owned direct Codex connection, the documented commands are:

```sh
codex mcp add figma --url https://mcp.figma.com/mcp
# If authentication is still required after adding the server:
codex mcp login figma
```

Equivalent non-secret server configuration in the host's Codex `config.toml`:

```toml
[mcp_servers.figma]
url = "https://mcp.figma.com/mcp"
```

Use the host's supported credential store. Codex exposes
`mcp_oauth_credentials_store` values `auto`, `file` and `keyring`, plus callback
port/URL settings. A headless host needs an operator-reachable callback route;
a browser's localhost is not the DGX's localhost. Arrange that route without
exposing the callback publicly by default. See [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Token handling for this delivery stays with the managed integration. Its
internal token storage, refresh implementation and exact issued scopes were
not inspected. Do not extract its tokens into Von or put bearer tokens in
prompts, repo configuration, logs or task evidence. For a direct installation,
keep the credential store outside the checkout, private to its owning runtime;
prefer an available OS keyring. A shared service must not lend one user's
connection to another caller merely because both invoke the same coding agent.

Figma MCP manages its own OAuth permissions; REST API scope configuration is
not the way to configure MCP. A personal REST token is not a demonstrated
substitute for the managed connection. See [Figma scopes](https://developers.figma.com/docs/rest-api/scopes/).

## Operations and access limits

The documented MCP read tools include design context, screenshots, metadata,
variables, Code Connect information and account identity. Write capabilities
include creating files, generating diagrams, capturing UI and editing designs;
availability varies by client and tool. The active tool contract and any
required Figma skill govern the actual invocation. Do not claim arbitrary
REST/MCP operation parity. See [Figma tools](https://developers.figma.com/docs/figma-mcp-server/tools-and-prompts/).

Figma enforces the connected user's existing content permissions and plan/seat
limits. `whoami` is exempt from the documented read-call rate limit, so its
success does not establish remaining design-read quota. The fetched rate-limit
page has an ambiguous rendered table and different prose for Starter limits;
no precise account quota was inferred from it. Recheck the provider's current
limits and actual response before a batch operation. Figma also states that
only catalogue-listed clients can connect; new clients must join its waitlist.
See [Figma access and limits](https://developers.figma.com/docs/figma-mcp-server/rate-limits-access/).

## Direct Von backend: remaining prerequisites and alternatives

No evidence supplied here establishes a registered Von Figma MCP client or a
delegated credential interface from the hosted connection to Von. This is an
unverified prerequisite, not a provider rejection observed against Von.
The existing [MCP proxy](../../src/backend/integrations/internal_mcp/mcp_proxy_base.py)
is a stdio transport. The [Otter client](../../src/backend/integrations/otter_live_client.py)
demonstrates HTTP OAuth but contains Otter-specific issuer and collector
assumptions; copying it and changing the URL would not establish Figma client
registration or per-user isolation.

The working alternative is to delegate a bounded Figma job to a Codex session
with its own verified connection, supplying the intended file/node and output.
Return the requested artefact and provenance through the ordinary task result;
do not transfer credentials. This run establishes the account-read portion of
that route only.

If direct backend access becomes necessary, first establish Figma registration
for Von and the intended user/organisation binding. A separate REST OAuth app
is another option for REST-supported jobs: register its redirect URI and minimal
scopes, use authorisation code plus state validation and S256 PKCE, and exchange
the code server-side. Store the returned user ID, expiry and tokens under the
trusted Von actor binding. Figma's REST refresh flow replaces the previous
access token, requiring coordinated refresh and atomic storage. These REST
rules must not be assumed to describe MCP token lifetimes. See [Figma OAuth apps](https://developers.figma.com/docs/rest-api/oauth-apps/).

A native implementation would need a real consent/read test, actor mismatch
denial, refresh/revocation handling and secret-redaction evidence before an
authentication-boundary claim. Those are prerequisites for that separate path,
not acceptance gates for this documentation-only delivery.

Figma's desktop MCP at `http://127.0.0.1:3845/mcp` is another documented route
where the Figma desktop app and an eligible setup are available. It is not a
verified headless DGX fallback and should not be exposed as a shared unauthenticated
service. See [desktop setup](https://developers.figma.com/docs/figma-mcp-server/local-server-installation/).

## Repeating acceptance for a concrete Figma job

1. Discover the actual Figma tools in the executing session and call `whoami`.
   Record success/error and whether the intended identity matched, without
   copying personal details into public evidence.
2. Use the task's supplied Figma file and node with `get_design_context`; inspect
   the resulting design context and screenshot. Record the exact source and
   result. An access error is not proof that the file is globally absent.
3. If the job requires a write, apply the authorised bounded change using the
   relevant tool/skill, then read back the intended file/node. Capture its link
   and recovery route. Do not create a test design merely to prove login.
4. On revoked/expired authentication, use the owning product's reconnect flow;
   on permissions or quota failure, report that distinct condition. Do not
   substitute another user's credential or repeatedly retry an unchanged denial.

Validation for this delivery is Tier 0 for the document and a live account-read
smoke check for the existing route. Merge is justified by the bounded evidence
above; it does not assert a new native Figma integration or full OAuth lifecycle
acceptance. Revert this document and its index entry to roll back the repository
change; no runtime or credential rollback is required.
