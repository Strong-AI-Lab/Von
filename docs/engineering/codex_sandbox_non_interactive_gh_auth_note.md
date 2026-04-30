# Codex Sandbox Non-Interactive GitHub Auth Note

This note preserves the exact guidance given during the `JVNAUTOSCI-2210`
setup discussion, because Jira comment rendering can rewrite nested Markdown
code fences.

Verbatim preserved text:

Yes. For automation, use a token-based non-interactive path.

Best options:

1. `GH_TOKEN` environment variable
Good for CI/sandbox runs where you can inject a secret into the process.

```powershell
$env:GH_TOKEN = "<REDACTED_FINE_GRAINED_PAT_OR_APP_TOKEN>"

gh auth status
gh repo view Strong-AI-Lab/Von
```

For Git pushes over HTTPS, also configure Git to use `gh` as credential helper:

```powershell
gh auth setup-git
git ls-remote origin HEAD
git push --dry-run origin main
```

2. `gh auth login --with-token`
Good when you want to seed the sandbox user’s `gh` keyring/config non-interactively.

```powershell
$token = "<REDACTED_FINE_GRAINED_PAT>"
$token | gh auth login --with-token
gh auth setup-git
gh auth status
git push --dry-run origin main
```

Do not echo real tokens into logs. In practice, read from a secret file or injected secret variable:

```powershell
Get-Content -Raw C:\secure\github-token.txt | gh auth login --with-token
```

3. GitHub App installation token
Best for recurring automation. A GitHub App can mint short-lived installation tokens scoped to one repo. Inject that as `GH_TOKEN` for the run. This avoids long-lived personal tokens.

4. SSH deploy key
Also non-interactive, but the sandbox must have a writable/configured `.ssh` directory and the private key must be provisioned securely:

```powershell
$env:GIT_SSH_COMMAND = 'ssh -i C:\secure\von_deploy_key -o IdentitiesOnly=yes'
git ls-remote git@github.com:Strong-AI-Lab/Von.git HEAD
```

For this Codex sandbox case, I’d prefer either:

- short-lived GitHub App token injected as `GH_TOKEN`, or
- fine-grained PAT injected as `GH_TOKEN` plus `gh auth setup-git`.

Then make the sandbox preflight fail closed on:

```powershell
gh auth status
git ls-remote origin HEAD
git push --dry-run origin HEAD:refs/heads/codex/preflight
```

If those do not pass, the sandbox should not start work that expects to publish.
