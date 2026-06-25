# Install Flowly CLI/TUI on Windows from PyPI.
#
# Windows keeps the public CLI install Python/pip based. Flowly Desktop ships
# its own embedded runtime separately, so this script does not download a
# standalone Flowly binary.

[CmdletBinding()]
param(
    [switch]$SkipBootstrap,
    [switch]$NoPathUpdate,
    [switch]$SkipSystemDeps
)

$ErrorActionPreference = 'Stop'

function Write-Step($Message) {
    Write-Host "[flowly] $Message" -ForegroundColor Cyan
}

function Write-Ok($Message) {
    Write-Host "[flowly] $Message" -ForegroundColor Green
}

function Find-Python {
    $candidates = @(
        @{ File = 'py'; Args = @('-3') },
        @{ File = 'python'; Args = @() },
        @{ File = 'python3'; Args = @() }
    )

    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate.File -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }

        try {
            $args = @($candidate.Args) + @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)')
            & $candidate.File @args
            if ($LASTEXITCODE -eq 0) {
                return @{
                    File = $candidate.File
                    Args = $candidate.Args
                }
            }
        }
        catch {
            # Try the next candidate.
        }
    }

    throw @"
Python 3.11 or newer is required for the Flowly CLI.

Install Python from https://www.python.org/downloads/windows/ and enable "Add python.exe to PATH",
then rerun:
  irm https://useflowlyapp.com/install.ps1 | iex

The Flowly Desktop app does not require this step because it includes its own runtime.
"@
}

function Invoke-Python {
    param(
        [hashtable]$Python,
        [string[]]$Arguments
    )
    & $Python.File @($Python.Args + $Arguments)
}

function Add-UserPath {
    param([string]$PathToAdd)

    if ($NoPathUpdate) { return }
    if (-not $PathToAdd) { return }

    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($current) {
        $parts = $current.Split(';') | Where-Object { $_ -ne '' }
    }
    if ($parts -notcontains $PathToAdd) {
        $newPath = (@($PathToAdd) + $parts) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    }
    if (($env:Path.Split(';') | Where-Object { $_ -eq $PathToAdd }).Count -eq 0) {
        $env:Path = "$PathToAdd;$env:Path"
    }
}

function Install-SystemDeps {
    # Optional native tools Flowly shells out to for certain features:
    #   ffmpeg  - voice messages and the video skills (pixel-art, etc.)
    #   ripgrep - fast file search for the agent (it falls back to a slower
    #             search without it)
    # These are nice-to-have: Flowly degrades gracefully when they're missing.
    # A Python wheel can't ship system binaries, so offer them via winget (the
    # built-in Windows package manager). Best-effort - never fails the install.
    if ($SkipSystemDeps) { return }

    $deps = @(
        @{ Name = 'ffmpeg';  Command = 'ffmpeg'; WingetId = 'Gyan.FFmpeg' },
        @{ Name = 'ripgrep'; Command = 'rg';     WingetId = 'BurntSushi.ripgrep.MSVC' }
    )

    $missing = $deps | Where-Object { -not (Get-Command $_.Command -ErrorAction SilentlyContinue) }
    if (-not $missing) {
        Write-Step 'Optional tools already present: ffmpeg, ripgrep'
        return
    }

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        $names = ($missing | ForEach-Object { $_.Name }) -join ', '
        Write-Step "Optional tools missing ($names). Install 'App Installer' (winget) from the Microsoft Store, then re-run - or add them manually."
        return
    }

    foreach ($dep in $missing) {
        Write-Step "Installing optional tool: $($dep.Name) (for voice/audio and fast search)..."
        try {
            $wingetArgs = @(
                'install', '--id', $dep.WingetId, '-e', '--silent',
                '--accept-package-agreements', '--accept-source-agreements',
                '--disable-interactivity'
            )
            & winget @wingetArgs | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "Installed: $($dep.Name)"
            }
            else {
                Write-Step "Skipped $($dep.Name) (winget exit $LASTEXITCODE) - Flowly works without it. Add later: winget install --id $($dep.WingetId)"
            }
        }
        catch {
            Write-Step "Skipped $($dep.Name) - Flowly works without it. Add later: winget install --id $($dep.WingetId)"
        }
    }
}

$python = Find-Python

Write-Step 'Installing Flowly CLI from PyPI...'
try {
    Invoke-Python -Python $python -Arguments @('-m', 'ensurepip', '--upgrade')
}
catch {
    Write-Step 'ensurepip was unavailable; trying pip directly.'
}
Invoke-Python -Python $python -Arguments @('-m', 'pip', 'install', '--user', '--upgrade', 'flowly-ai')

# Ask Python for the exact per-user scripts directory. A `pip install --user`
# on Windows places console scripts in USER_BASE\PythonXY\Scripts (note the
# version subdir), NOT USER_BASE\Scripts — the nt_user install scheme. Deriving
# it from site.USER_BASE + 'Scripts' drops the PythonXY segment, so the flowly
# launcher lands somewhere this script never adds to PATH. sysconfig reports the
# real path for whichever interpreter actually ran the install.
$scriptsDir = (Invoke-Python -Python $python -Arguments @('-c', 'import sysconfig; print(sysconfig.get_path("scripts", "nt_user"))') | Select-Object -Last 1).Trim()
Add-UserPath -PathToAdd $scriptsDir

$flowlyExe = Join-Path $scriptsDir 'flowly.exe'
$flowlyCmd = Join-Path $scriptsDir 'flowly.cmd'

if (Test-Path $flowlyExe) {
    $flowly = $flowlyExe
}
elseif (Test-Path $flowlyCmd) {
    $flowly = $flowlyCmd
}
else {
    $resolved = Get-Command flowly -ErrorAction SilentlyContinue
    if ($resolved) {
        $flowly = $resolved.Source
    }
    else {
        throw "Flowly was installed, but the flowly launcher was not found. Add this directory to PATH and open a new terminal: $scriptsDir"
    }
}

& $flowly --version | Out-Null

Install-SystemDeps

Write-Ok 'Flowly CLI installed.'
Write-Host ''

# First-run onboarding: when the console is interactive, open the
# account-or-API-key picker right away (it also seeds the workspace). With no
# interactive input (CI / -SkipBootstrap), fall back to the non-interactive
# workspace seed and print the manual next steps.
if (-not $SkipBootstrap -and -not [System.Console]::IsInputRedirected) {
    try { & $flowly setup }
    catch { try { & $flowly bootstrap } catch {} }
}
else {
    if (-not $SkipBootstrap) {
        try { & $flowly bootstrap }
        catch { Write-Step "bootstrap failed; run 'flowly doctor --fix' after install." }
    }
    Write-Host 'Get started (open a new terminal first so PATH is picked up):'
    Write-Host '  1. flowly setup                    # choose an account or API key'
    Write-Host '  2. flowly service install --start  # run the gateway in the background'
    Write-Host '  3. flowly                          # start chatting'
}
