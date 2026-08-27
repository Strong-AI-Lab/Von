# OpenRouter Provider Deployment Note

- **Kind:** Bounded deployment and operator note
- **Lifecycle:** Active
- **Authority:** Operational guidance for the opt-in OpenRouter transport only;
  model eligibility and routing remain in scoped settings and the represented
  model registry/workflows
- **Last reviewed:** 27 August 2026
- **Review trigger:** OpenRouter API, privacy, pricing, or Von provider-policy changes

OpenRouter is an optional external inference source. It broadens the catalogue
available for evaluation, but configuring a key does not enable a model, alter
the scoped primary, or make OpenRouter a default. An authenticated actor must
still have the exact `openrouter` provider and model slug in their effective
enabled-model setting.

Configure `OPENROUTER_API_KEY` or, preferably on managed hosts,
`OPENROUTER_API_KEY_FILE`. The OpenStack bootstrap variables are
`bootstrap_openrouter_api_key` and `bootstrap_openrouter_api_key_file`; keep the
inline value outside tracked tfvars. Von uses
`https://openrouter.ai/api/v1` unless `OPENROUTER_BASE_URL` explicitly selects
another compatible endpoint.

Every Von OpenRouter generation in this release uses Chat Completions and
requires the following provider preferences:

- `zdr: true`;
- `data_collection: "deny"`; and
- `require_parameters: true`.

Router metadata is requested so telemetry can retain the requested provider and
model, concrete response model, generation identifier, upstream routing
evidence, token usage, and provider-reported cost when OpenRouter supplies it.
Missing cost or routing data remains unavailable rather than being recorded as
zero. The catalogue is discovery data only and does not grant model authority.

OpenRouter charges and upstream rate limits vary by model and provider. Check
the current OpenRouter model catalogue and account limits before allowing a
model. Zero-data-retention eligibility and denial of data-collection endpoints
reduce retention exposure but do not make an external broker equivalent to a
local model; prompts still cross Von's trust boundary and request metadata may
be retained. This is why OpenRouter remains explicit and opt-in for private or
organisation-sensitive work.

This release does not enable OpenRouter Auto Router, cross-model fallback,
Responses API, embeddings, response caching, server tools/plugins/guardrails,
or deployment activation. Same-model upstream selection remains observable
OpenRouter transport behaviour; Von's represented policy still chooses the
model.
