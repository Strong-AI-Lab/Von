# Cloudflare Access single sign-in

Opt-in deployment support (JVNAUTOSCI-2738). This is not a general replacement
for Von authentication or an invitation to expose its origin directly.

Protect the **entire public hostname** with a Cloudflare Access application and
an explicit Google-account allow policy. Require Access JWT verification in the
Cloudflare Tunnel connector, retain a catch-all 404, and keep Von's origin on
loopback. Do not enable an Access Bypass policy, including for API paths.

Set the four `VON_CLOUDFLARE_ACCESS_*` values documented in `.env.template`, using
the application's AUD tag and team domain, plus a strong private
`FLASK_SECRET_KEY` (at least 32 characters). Restart Von. Never copy tokens,
private keys or client secrets into logs or this documentation.

On that exact hostname, Von independently verifies the RS256 signature with
keys fetched only from the configured team, issuer, audience, expiry, issue
time, optional not-before time and human identity claims. Certificate retrieval
failure denies access. A raw email header is never sufficient. The verified
email must resolve uniquely through `#V#hasVonLoginEmail`; ordinary contact
emails do not grant login. No user or organisation membership is auto-created.

The resulting actor binding is cached in Von's signed session for the same
assertion, but the assertion is cryptographically checked on every public
request. As with existing OAuth sessions, changing a login-email binding is not
an immediate revocation mechanism for an already bound session: revoke the
Access session as well when immediate removal is needed. Actor changes clear
previous organisation/chat state. A Cloudflare-derived cookie cannot authenticate
on a different hostname or after this feature is disabled.

The user signs into Google through Access once; Von then recognises the same
governed identity. Logout clears Von state and takes the browser through Access
logout. Direct localhost/Tailscale routes keep their existing authentication
policy and must not be advertised as public bypasses.

Rollback: set `VON_CLOUDFLARE_ACCESS_ENABLED=false` and restart. Keep Access and
connector validation enabled. The public route returns to its original separate
Von sign-in, provided its Google callback remains configured. Do not overwrite
other `.env` settings. Validate both successful normal Google issuance and
denials in the actual deployment before claiming single-sign-in delivery;
synthetic JWT tests alone are not that proof.
