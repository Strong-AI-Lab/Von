# VS Code Testing / Jest on Windows: `spawn ... ENOENT`

## Symptoms

- VS Code Testing panel (Jest provider) fails with errors like:
  - `Process failed: spawn C:\Windows\System32\cmd.exe ENOENT`
  - `Process failed: spawn pwsh.exe ENOENT`
  - `Process failed: spawn powershell.exe ENOENT`
- Developer Tools console may also show:
  - `potential listener LEAK detected, having N listeners already`
  - Extension host errors about `realpath` failures under the VS Code Insiders install folder.

## What this usually means

`ENOENT` from `spawn` means the extension host could not locate the executable it was asked to run.

In this specific failure mode we have seen **multiple different shells** fail (`cmd.exe`, `pwsh.exe`, `powershell.exe`). That strongly suggests an **extension-host environment/installation issue**, not a single missing shell.

One strong signal is an extension-host error like:

- `ENOENT: no such file or directory, realpath 'c:\\Program Files\\Microsoft VS Code Insiders\\<build>\\bin'`

If that directory does not exist on disk, it points to a **broken or partial VS Code Insiders install/update**.

In our case we’ve seen VS Code compute the binaries path as `...\\<build>\\bin` even though the actual install-level binaries live in `...\\Microsoft VS Code Insiders\\bin`.

## Workspace mitigations (low risk)

These won’t fix a broken VS Code install, but they can remove avoidable PATH lookups.

- Configure the Jest extension to use an **absolute shell path** (avoids PATH resolution):
  - Workspace setting `jest.shell`:
    - `C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe`

## Recommended remediation steps

1. **Repair VS Code Insiders install**
   - Confirm whether the logged `...\\Microsoft VS Code Insiders\\<build>\\bin` folder exists.
   - If it does not: reinstall/repair VS Code Insiders (or switch to stable) so the referenced folder exists.

2. **Pragmatic workaround: create a junction for the missing `bin`**

  If you want a quick local workaround (and you’re comfortable making a small change under `C:\\Program Files`), you can create a Windows junction so the missing path exists.

  Run **PowerShell as Administrator** and execute:

  - `New-Item -ItemType Junction -Path "C:\\Program Files\\Microsoft VS Code Insiders\\<build>\\bin" -Target "C:\\Program Files\\Microsoft VS Code Insiders\\bin"`

  Replace `<build>` with the hash you see in the logs (e.g. `1cd32455f8`).

  Notes:
  - This may need repeating after an Insiders update (the `<build>` directory changes).
  - Prefer reinstall/repair if you want the supported fix.

3. **Remove extension conflicts**
   - If you have both `ms-vscode.js-debug` and `ms-vscode.js-debug-nightly`, disable/uninstall one.
     - The logs will show many "already registered" warnings when both are active.

4. **Reload VS Code window**
   - Run `Developer: Reload Window` after any of the above changes.

## Notes

- This repository’s frontend tests can still be run reliably via the task `test:frontend` even if the VS Code Testing UI is misbehaving.
