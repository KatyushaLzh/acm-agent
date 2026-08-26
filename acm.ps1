[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $AcmArgs
)

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $repoRoot
try {
    $pythonExecutable = 'python'
    $commandName = $null
    for ($index = 0; $index -lt $AcmArgs.Count; $index++) {
        if ($AcmArgs[$index] -ieq '--root') {
            $index++
            continue
        }
        if ($AcmArgs[$index] -ilike '--root=*') {
            continue
        }
        if (-not $AcmArgs[$index].StartsWith('-')) {
            $commandName = $AcmArgs[$index]
            break
        }
    }
    if ($commandName -ieq 'web') {
        . (Join-Path $repoRoot 'tools\acm-python313-windows.ps1')
        $pythonExecutable = Ensure-AcmPython313
        if (-not $pythonExecutable) {
            exit 2
        }
    }

    & $pythonExecutable -m tools.acm_agent @AcmArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
