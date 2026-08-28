$script:AcmPythonVersion = '3.13.15'

function Test-AcmStoreAlias {
    param([Parameter(Mandatory = $true)][string] $Path)

    $normalized = $Path.Replace('/', '\')
    return $normalized -match '(?i)\\Microsoft\\WindowsApps\\'
}

function Get-AcmPythonCandidates {
    $candidates = [System.Collections.Generic.List[object]]::new()
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)

    foreach ($commandName in @('py', 'python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3', 'python')) {
        $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) {
            continue
        }
        $path = if ($command.Source) { $command.Source } else { $command.Path }
        if (-not $path -or (Test-AcmStoreAlias $path)) {
            continue
        }
        if ($commandName -eq 'py') {
            foreach ($selector in @('-3', '-3.14', '-3.13', '-3.12', '-3.11', '-3.10')) {
                if ($seen.Add("$path|$selector")) {
                    $candidates.Add([pscustomobject]@{ FilePath = $path; PrefixArgs = @($selector) })
                }
            }
        }
        elseif ($seen.Add("$path|python")) {
            $candidates.Add([pscustomobject]@{ FilePath = $path; PrefixArgs = @() })
        }
    }

    $standardPaths = [System.Collections.Generic.List[string]]::new()
    foreach ($minor in 14..10) {
        if ($env:LOCALAPPDATA) { $standardPaths.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python3$minor\python.exe")) }
        if ($env:SystemDrive) { $standardPaths.Add((Join-Path $env:SystemDrive "Python3$minor\python.exe")) }
        if ($env:ProgramW6432) { $standardPaths.Add((Join-Path $env:ProgramW6432 "Python3$minor\python.exe")) }
        if ($env:ProgramFiles) { $standardPaths.Add((Join-Path $env:ProgramFiles "Python3$minor\python.exe")) }
        if (${env:ProgramFiles(x86)}) {
            $standardPaths.Add((Join-Path ${env:ProgramFiles(x86)} "Python3$minor-32\python.exe"))
            $standardPaths.Add((Join-Path ${env:ProgramFiles(x86)} "Python3$minor\python.exe"))
        }
    }

    foreach ($path in $standardPaths) {
        if ((Test-Path -LiteralPath $path -PathType Leaf) -and -not (Test-AcmStoreAlias $path) -and $seen.Add("$path|python")) {
            $candidates.Add([pscustomobject]@{ FilePath = $path; PrefixArgs = @() })
        }
    }
    return $candidates.ToArray()
}

function Test-AcmPythonCandidate {
    param([Parameter(Mandatory = $true)] $Candidate)

    $probe = @'
import json
import sqlite3
import ssl
import sys

try:
    import tkinter  # noqa: F401
    tkinter_ok = True
except Exception:
    tkinter_ok = False

if sys.version_info[:2] < (3, 10) or sys.version_info.releaselevel != 'final':
    raise SystemExit(13)

print(json.dumps({'executable': sys.executable, 'tkinter': tkinter_ok}))
'@

    try {
        $probeArguments = @($Candidate.PrefixArgs) + @('-c', $probe)
        $output = & $Candidate.FilePath @probeArguments 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output) {
            Write-Verbose "Python candidate probe exited with code $LASTEXITCODE for $($Candidate.FilePath)."
            return $null
        }
        $result = ($output | Select-Object -Last 1) | ConvertFrom-Json -ErrorAction Stop
        if (-not $result.executable -or (Test-AcmStoreAlias ([string]$result.executable))) {
            return $null
        }
        $resolved = [System.IO.Path]::GetFullPath([string]$result.executable)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            return $null
        }
        return [pscustomobject]@{ Path = $resolved; Tkinter = [bool]$result.tkinter }
    }
    catch {
        Write-Verbose "Python candidate probe failed for $($Candidate.FilePath): $($_.Exception.Message)"
        return $null
    }
}

function Find-AcmPython {
    param([object[]] $Candidates)

    if (-not $PSBoundParameters.ContainsKey('Candidates')) {
        $Candidates = Get-AcmPythonCandidates
    }
    foreach ($candidate in $Candidates) {
        $result = Test-AcmPythonCandidate $candidate
        if ($result) {
            if (-not $result.Tkinter) {
                Write-Warning 'Python 3.10+ is usable, but tkinter is unavailable. The native file picker will be unavailable.'
            }
            return $result.Path
        }
    }
    return $null
}

function Get-AcmPython313InstallerSpec {
    param([string] $Architecture)

    if (-not $Architecture) {
        try {
            $Architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        }
        catch {
            $Architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        }
    }

    switch -Regex ($Architecture) {
        '^(?i:X64|AMD64)$' {
            $fileName = 'python-3.13.15-amd64.exe'
            $sha256 = 'edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403'
            break
        }
        '^(?i:Arm64|ARM64)$' {
            $fileName = 'python-3.13.15-arm64.exe'
            $sha256 = 'c252c676087c49e6b94e95a273536b78921c28a5fc9f86d15d25392328247249'
            break
        }
        '^(?i:X86|x86)$' {
            $fileName = 'python-3.13.15.exe'
            $sha256 = '741c07276eb2d57e7ee012d643f021c58cb38d11c5389be46c15d41d1a10b447'
            break
        }
        default {
            throw "Unsupported Windows architecture: $Architecture"
        }
    }

    return [pscustomobject]@{
        FileName = $fileName
        Sha256 = $sha256
        Urls = @(
            "https://mirrors.huaweicloud.com/python/$script:AcmPythonVersion/$fileName",
            "https://www.python.org/ftp/python/$script:AcmPythonVersion/$fileName"
        )
    }
}

function Save-AcmPython313Installer {
    param(
        [Parameter(Mandatory = $true)] $Spec,
        [Parameter(Mandatory = $true)][string] $Destination,
        [scriptblock] $Downloader
    )

    if (-not $Downloader) {
        $Downloader = {
            param($Uri, $OutFile)
            Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing -ErrorAction Stop
        }
    }

    foreach ($url in $Spec.Urls) {
        try {
            Write-Host "Downloading Python $script:AcmPythonVersion from $url"
            $null = & $Downloader $url $Destination
            if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
                throw 'The downloader did not create the installer file.'
            }
            $stream = [System.IO.File]::OpenRead($Destination)
            try {
                $hasher = [System.Security.Cryptography.SHA256]::Create()
                try {
                    $actualHash = ([System.BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
                }
                finally {
                    $hasher.Dispose()
                }
            }
            finally {
                $stream.Dispose()
            }
            if ($actualHash -ne ([string]$Spec.Sha256).ToLowerInvariant()) {
                throw "SHA-256 mismatch (expected $($Spec.Sha256), got $actualHash)."
            }
            return $Destination
        }
        catch {
            Write-Warning "Download failed for $url`: $($_.Exception.Message)"
            Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
        }
    }
    return $null
}

function Install-AcmPython313 {
    param(
        [string] $Architecture,
        $Spec,
        [scriptblock] $Downloader,
        [scriptblock] $InstallerRunner
    )

    if (-not $Spec) {
        try {
            $Spec = Get-AcmPython313InstallerSpec -Architecture $Architecture
        }
        catch {
            Write-Error $_.Exception.Message
            return $false
        }
    }

    $tempDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("acm-python313-" + [guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Path $tempDirectory -ErrorAction Stop | Out-Null
        $installerPath = Join-Path $tempDirectory $Spec.FileName
        if (-not (Save-AcmPython313Installer -Spec $Spec -Destination $installerPath -Downloader $Downloader)) {
            Write-Error 'Unable to download a verified Python installer from either source.'
            return $false
        }

        $arguments = @(
            '/quiet',
            'InstallAllUsers=0',
            'PrependPath=1',
            'Include_launcher=1',
            'InstallLauncherAllUsers=0',
            'Include_test=0'
        )
        Write-Host 'Installing Python 3.13 for the current Windows user...'
        if ($InstallerRunner) {
            $exitCode = & $InstallerRunner $installerPath $arguments
        }
        else {
            $process = Start-Process -FilePath $installerPath -ArgumentList $arguments -Wait -PassThru -ErrorAction Stop
            $exitCode = $process.ExitCode
        }
        if ([int]$exitCode -notin @(0, 3010)) {
            Write-Error "Python installer exited with code $exitCode."
            return $false
        }
        return $true
    }
    catch {
        Write-Error "Python installation failed: $($_.Exception.Message)"
        return $false
    }
    finally {
        if (Test-Path -LiteralPath $tempDirectory) {
            Remove-Item -LiteralPath $tempDirectory -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Ensure-AcmPython {
    param(
        [object[]] $Candidates,
        [scriptblock] $ReadAnswer,
        [scriptblock] $InstallAction
    )

    $candidateArguments = @{}
    if ($PSBoundParameters.ContainsKey('Candidates')) {
        $candidateArguments.Candidates = $Candidates
    }
    $pythonPath = Find-AcmPython @candidateArguments
    if ($pythonPath) {
        return $pythonPath
    }

    if (-not $ReadAnswer) {
        $ReadAnswer = { param($Prompt) Read-Host $Prompt }
    }
    :prompt while ($true) {
        try {
            $answer = & $ReadAnswer 'Python 3.10 or newer is required to start the ACM dashboard. Install Python 3.13.15 now? [y/n]'
        }
        catch {
            Write-Host 'No interactive input is available. Install Python 3.10 or newer manually, then retry.'
            return $null
        }
        if ($null -eq $answer) {
            Write-Host 'No interactive input is available. Install Python 3.10 or newer manually, then retry.'
            return $null
        }
        switch (([string]$answer).Trim().ToLowerInvariant()) {
            'y' { break prompt }
            'n' {
                Write-Host 'Python installation declined; the ACM dashboard was not started.'
                return $null
            }
            default {
                Write-Host 'Please enter y to install or n to exit.'
                continue prompt
            }
        }
    }

    $installed = if ($InstallAction) { & $InstallAction } else { Install-AcmPython313 }
    if (-not $installed) {
        return $null
    }

    $pythonPath = Find-AcmPython @candidateArguments
    if (-not $pythonPath) {
        Write-Error 'Python 3.13.15 installation completed, but the Python 3.10+ or core-library verification failed.'
        return $null
    }
    return $pythonPath
}
