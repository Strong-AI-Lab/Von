# Login organisation defaults

An authenticated email identifies a person through `#V#hasVonLoginEmail`.
It does not identify their active organisation. The same person can still
deliberately switch between their current `#V#memberOfVonOrg` memberships.

`#V#hasVonLoginOrganisationPreferences` is a non-authority text predicate on
the person: a singleton JSON object mapping exact normalised login emails to
organisation concept IDs (or null for Personal). The canonical
`set_login_organisation_preference` service validates the email binding and
membership before writing, serialises per-person updates, and reads back.
Only an authorised preference owner/operator should invoke that primitive.
No email-domain inference, membership grant, or role change is performed.

Authenticated status reads validate the selected default against live narrow
membership. An absent/revoked default selects Personal, not another membership.
Malformed/ambiguous preference data or a storage outage does not silently pick
an organisation. The browser displays its retry gate rather than starting chat
with an unconfirmed context.

A changed authenticated email or changed effective default establishes a new
bootstrap generation. Before chat/history/tools initialise, the browser rotates
its opaque window ID, clears legacy organisation mirrors, and binds the exact
default through the membership-checking session endpoint. Reloads in that
generation preserve deliberate per-tab switches, including explicit Personal.
New tabs start with the login default rather than a browser-wide last-used org.
Cloudflare token renewal for the same email does not reset deliberate choices.
Browser-test fixtures retain their separately configured fixture bootstrap.

The old `member_of_organisation` list is **not a preference**. Reading its first
value previously caused SAIL to be selected for Michael's work email. Its
descriptive assertions are preserved; preference readers no longer consume it.

DGX-local Mongo and Mac/Atlas are separate authority stores. Configure and
verify each independently; federation does not replicate these authority or
preference records. A stored default is not proof of a successful Google login:
validate normal issuance, resulting tab context and actual effect namespace.
