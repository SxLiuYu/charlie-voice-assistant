[CmdletBinding()]
param(
    [switch]$InstallMissing
)

$ErrorActionPreference = "Stop"
$minimumVersion = [version]"3.10"
$standardLibraryModules = @(
    "argparse",
    "json",
    "mimetypes",
    "os",
    "pathlib",
    "sys",
    "time",
    "urllib",
    "uuid"
)
$probe = 'import argparse, json, mimetypes, os, pathlib, sys, time, urllib, uuid; print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1])+chr(46)+str(sys.version_info[2]))'

function Find-CompatiblePython {
    $candidates = @()
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $candidates += [pscustomobject]@{
            Executable = $pythonCommand.Source
            PrefixArgs = @()
            Display = "python"
        }
    }

    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCommand) {
        $candidates += [pscustomobject]@{
            Executable = $pyCommand.Source
            PrefixArgs = @("-3")
            Display = "py -3"
        }
    }

    foreach ($candidate in $candidates) {
        try {
            $invokeArgs = @($candidate.PrefixArgs) + @("-c", $probe)
            $output = & $candidate.Executable @invokeArgs 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $output) {
                continue
            }

            $version = [version]@($output)[-1]
            if ($version -ge $minimumVersion) {
                return [pscustomobject]@{
                    Executable = $candidate.Executable
                    PrefixArgs = $candidate.PrefixArgs
                    Display = $candidate.Display
                    Version = $version.ToString()
                }
            }
        }
        catch {
            Write-Verbose "Python probe failed for $($candidate.Display): $($_.Exception.Message)"
            continue
        }
    }

    return $null
}

$python = Find-CompatiblePython
$installed = $false

if (-not $python) {
    if (-not $InstallMissing) {
        throw "Python $minimumVersion or newer with the required standard library modules was not found. Re-run with -InstallMissing."
    }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Python is missing and WinGet is unavailable. Install Python 3.10 or newer, then run this check again."
    }

    & $winget.Source install `
        --id Python.Python.3.12 `
        --exact `
        --scope user `
        --accept-package-agreements `
        --accept-source-agreements
    $installExitCode = $LASTEXITCODE

    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
    $python = Find-CompatiblePython
    $installed = $installExitCode -eq 0

    if (-not $python) {
        if ($installExitCode -ne 0) {
            throw "WinGet could not install Python 3.12."
        }
        throw "Python was installed, but it is not available in the current shell. Open a new terminal and run the check again."
    }
}

[pscustomobject]@{
    status = "ready"
    python_command = $python.Display
    python_version = $python.Version
    installed_now = $installed
    dependency_type = "standard-library-only"
    checked_modules = $standardLibraryModules
} | ConvertTo-Json -Depth 3
