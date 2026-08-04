[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $AcmArgs
)

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $repoRoot
try {
    & python -m tools.acm_agent @AcmArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
