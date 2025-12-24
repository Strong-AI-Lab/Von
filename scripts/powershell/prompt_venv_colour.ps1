# Safe virtual environment prompt colouring (no Invoke-Expression)
# Prints only the venv name in green: (von-3.13)

# Prevent venv activation scripts from overriding the prompt function.
# They still set $env:VIRTUAL_ENV_PROMPT, which we render here.
$env:VIRTUAL_ENV_DISABLE_PROMPT = 1

if (-not (Get-Variable -Name VON__PromptWrapped -Scope Global -ErrorAction SilentlyContinue)) {
    $global:VON__PromptWrapped = $true

    if (-not (Get-Variable -Name VON__OriginalPrompt -Scope Global -ErrorAction SilentlyContinue)) {
        $global:VON__OriginalPrompt = $function:prompt
    }

    function global:prompt {
        $originalPrompt = $global:VON__OriginalPrompt

        $previousPromptValue = if ($null -ne $originalPrompt) {
            & $originalPrompt
        }
        else {
            "PS $($executionContext.SessionState.Path.CurrentLocation)> "
        }

        $venvName = if ($env:VIRTUAL_ENV_PROMPT) {
            $env:VIRTUAL_ENV_PROMPT
        }
        elseif ($env:VIRTUAL_ENV) {
            Split-Path -Leaf $env:VIRTUAL_ENV
        }
        else {
            $null
        }

        if ($null -ne $venvName -and $venvName -ne '') {
            Write-Host -NoNewline "("
            Write-Host -NoNewline $venvName -ForegroundColor Green
            Write-Host -NoNewline ") "
        }

        return $previousPromptValue
    }
}
