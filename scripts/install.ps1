# Install Flowly CLI/TUI on Windows from a git checkout.
#
# Mirrors the Unix installer: clone the repository, build an isolated uv venv
# (uv downloads and manages Python itself — no system Python required), and
# install Flowly into it as an editable checkout. Because the install lives in a
# real git checkout, `flowly update` fast-forwards it with `git pull` instead of
# waiting for a PyPI release. Git is fetched as portable MinGit when missing — no
# admin rights, isolated from any system Git. Flowly Desktop ships its own
# embedded runtime separately; this script never touches it.

[CmdletBinding()]
param(
    [string]$RepoUrl = $env:FLOWLY_REPO_URL,
    [string]$Branch  = $env:FLOWLY_BRANCH,
    [string]$Src     = $env:FLOWLY_SRC,
    [string]$Venv    = $env:FLOWLY_VENV,
    [string]$Python  = $env:FLOWLY_PYTHON,
    [switch]$SkipBootstrap,
    [switch]$NoPathUpdate,
    [switch]$SkipSystemDeps
)

$ErrorActionPreference = 'Stop'

# Native commands (git, uv) signal failure via exit codes we check explicitly
# with $LASTEXITCODE below — some git calls are *expected* to exit non-zero. On
# PowerShell 7.4+ this preference defaults to $true and would turn those into
# terminating errors; force it off. On Windows PowerShell 5.1 the variable
# doesn't exist and this assignment is a harmless no-op.
$PSNativeCommandUseErrorActionPreference = $false

# Pinned git-for-windows release for the portable-Git fallback. We use a static
# github.com/.../releases/download/<tag>/<asset> URL (NOT the api.github.com
# /releases/latest endpoint, which is rate-limited to 60 req/hour/IP and breaks
# installs behind CGNAT / corporate NAT).
$GitForWindowsVersion = '2.54.0'

# Defaults for anything not supplied via -params or FLOWLY_* env vars.
if (-not $RepoUrl) { $RepoUrl = 'https://github.com/Nocetic/flowly.git' }
if (-not $Branch)  { $Branch  = 'main' }
if (-not $Python)  { $Python  = '3.12' }

$FlowlyBase = Join-Path $env:LOCALAPPDATA 'Flowly'
if (-not $Src)  { $Src  = Join-Path $FlowlyBase 'repo' }
if (-not $Venv) { $Venv = Join-Path $FlowlyBase 'venv' }
$GitDir = Join-Path $FlowlyBase 'git'
$BinDir = Join-Path $FlowlyBase 'bin'

function Write-Step($Message) { Write-Host "[flowly] $Message" -ForegroundColor Cyan }
function Write-Ok($Message)   { Write-Host "[flowly] $Message" -ForegroundColor Green }

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

function Get-WindowsArch {
    # PROCESSOR_ARCHITECTURE reports x86 under WOW64; PROCESSOR_ARCHITEW6432
    # carries the real arch in that case.
    $a = $env:PROCESSOR_ARCHITEW6432
    if (-not $a) { $a = $env:PROCESSOR_ARCHITECTURE }
    switch ($a) {
        'AMD64' { 'x64' }
        'ARM64' { 'arm64' }
        'x86'   { 'x86' }
        default { 'x64' }
    }
}

# ── uv ──────────────────────────────────────────────────────────────────────
# uv manages Python and builds the venv. The official installer is user-scoped
# (no admin) and drops uv.exe in %USERPROFILE%\.local\bin.
function Install-Uv {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Write-Step "Using uv: $(uv --version)"
        return
    }
    Write-Step 'Installing uv package manager...'
    $prevProgress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        & powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Null
    }
    finally {
        $ProgressPreference = $prevProgress
    }
    $uvBin = Join-Path $env:USERPROFILE '.local\bin'
    if (Test-Path (Join-Path $uvBin 'uv.exe')) {
        $env:Path = "$uvBin;$env:Path"
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv installed but was not found on PATH. Open a new terminal and re-run.'
    }
    Write-Step "Using uv: $(uv --version)"
}

# ── Git ─────────────────────────────────────────────────────────────────────
# Git is required (the install is a checkout, and `flowly update` git-pulls it).
# Use a system Git when present, else download portable MinGit — a plain zip
# containing cmd\git.exe, enough to clone/fetch/pull. Returns the git command to
# invoke ('git' on PATH, or the full path to the portable git.exe).
function Install-Git {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Step "Using git: $(git --version)"
        return 'git'
    }

    Write-Step "git not found - downloading portable MinGit to $GitDir (no admin required)..."
    $arch = Get-WindowsArch
    if ($arch -eq 'x86') {
        $assetName = "MinGit-$GitForWindowsVersion-32-bit.zip"
    }
    else {
        # 64-bit MinGit runs on x64 and on arm64 via x64 emulation.
        $assetName = "MinGit-$GitForWindowsVersion-64-bit.zip"
    }
    $tag = "v$GitForWindowsVersion.windows.1"
    $url = "https://github.com/git-for-windows/git/releases/download/$tag/$assetName"
    $tmp = Join-Path $env:TEMP $assetName

    Write-Step "Downloading $assetName (Git for Windows $GitForWindowsVersion)..."
    $prevProgress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'   # ~20s instead of ~5min with the progress bar
    try {
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    }
    finally {
        $ProgressPreference = $prevProgress
    }

    if (Test-Path $GitDir) { Remove-Item -Recurse -Force $GitDir }
    New-Item -ItemType Directory -Path $GitDir -Force | Out-Null
    Expand-Archive -Path $tmp -DestinationPath $GitDir -Force
    Remove-Item -Force $tmp -ErrorAction SilentlyContinue

    $gitExe = Join-Path $GitDir 'cmd\git.exe'
    if (-not (Test-Path $gitExe)) {
        throw "MinGit extraction did not produce git.exe at $gitExe. Install Git manually from https://git-scm.com/download/win and re-run."
    }

    # Session PATH for the rest of this run; persist cmd\ so `flowly update`
    # finds git in fresh shells.
    $gitCmdDir = Join-Path $GitDir 'cmd'
    $env:Path = "$gitCmdDir;$env:Path"
    Add-UserPath -PathToAdd $gitCmdDir

    Write-Ok "Installed portable git: $(& $gitExe --version)"
    return $gitExe
}

# Clone the repo, or fast-forward an existing checkout to the branch tip. A full
# single-branch clone (NOT --depth 1) so `flowly update`'s behind-count
# (git rev-list --count HEAD..origin/<branch>) works.
function Update-Checkout {
    param([string]$Git)

    if (Test-Path (Join-Path $Src '.git')) {
        Write-Step "Updating existing checkout at $Src ..."
        & $Git -C $Src remote set-url origin $RepoUrl 2>$null
        & $Git -C $Src fetch --prune origin $Branch
        if ($LASTEXITCODE -ne 0) { throw 'git fetch failed - check your network / remote.' }
        # Force the tracked branch to the fetched tip in one step: -B creates or
        # resets the local branch, -f overwrites any local working-tree changes
        # in this managed checkout. Equivalent to a checkout + reset --hard.
        & $Git -C $Src checkout -f -B $Branch "origin/$Branch"
        if ($LASTEXITCODE -ne 0) { throw 'git checkout failed.' }
        return
    }

    if ((Test-Path $Src) -and (Get-ChildItem -Force $Src -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        throw "Install directory $Src exists but is not a git checkout. Move it aside (or set FLOWLY_SRC), then re-run."
    }

    Write-Step "Cloning $RepoUrl (branch $Branch) into $Src ..."
    $parent = Split-Path $Src -Parent
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    & $Git clone --branch $Branch --single-branch $RepoUrl $Src
    if ($LASTEXITCODE -ne 0) { throw 'git clone failed.' }
}

# Build the isolated venv and install Flowly into it as an editable checkout.
# Editable + a venv OUTSIDE the checkout keeps detect_install_mode() reporting
# "source" (sys.prefix is this venv, not uv/tools or pipx/venvs), which routes
# `flowly update` to the in-place git pull.
function Install-FromSource {
    $venvPy = Join-Path $Venv 'Scripts\python.exe'
    if (Test-Path $venvPy) {
        # The installer is meant to be re-run (it's the documented way to
        # force-refresh an install). Reuse a healthy venv instead of recreating
        # it: if the gateway is running as a service, it has this python.exe
        # open, and Windows refuses to delete an in-use executable (--clear
        # would fail with a sharing violation). `uv pip install -e` below is
        # idempotent against an existing venv, same as `flowly update`.
        Write-Step "Reusing existing virtualenv at $Venv ..."
    }
    else {
        Write-Step "Creating Flowly virtualenv at $Venv (Python $Python)..."
        # --clear: only reached when (re)creating, to wipe any broken/partial
        # leftovers from an interrupted previous run.
        & uv venv --clear --python $Python $Venv
        if ($LASTEXITCODE -ne 0) { throw 'uv venv failed.' }
    }

    Write-Step "Installing Flowly (editable) from $Src ..."
    & uv pip install --python $venvPy -e $Src
    if ($LASTEXITCODE -ne 0) { throw 'uv pip install failed.' }
}

# Write a flowly.cmd launcher that runs `python -m flowly` from the venv. Going
# through python -m (not a generated flowly.exe) means an editable reinstall on
# update never has to overwrite a launcher this very process is running — no
# Windows file-lock failures.
function Install-Launcher {
    $venvPy = Join-Path $Venv 'Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        throw "The editable install did not produce $venvPy."
    }
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
    $cmdPath = Join-Path $BinDir 'flowly.cmd'
    $body = "@echo off`r`n`"$venvPy`" -m flowly %*`r`n"
    # ANSI (Default) so a localized install path in the launcher survives for
    # cmd.exe — ASCII would mangle any non-7-bit character in the path.
    Set-Content -Path $cmdPath -Value $body -Encoding Default -NoNewline
    return $cmdPath
}

# Best-effort: retire an older `pip install --user flowly-ai` so its launcher
# doesn't shadow ours. Never fails the install.
function Remove-LegacyPyPiInstall {
    foreach ($pyName in @('py', 'python', 'python3')) {
        $cmd = Get-Command $pyName -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $shown = & $cmd.Source -m pip show flowly-ai 2>$null
            if ($shown) {
                Write-Step 'Removing the previous PyPI install (pip --user flowly-ai)...'
                & $cmd.Source -m pip uninstall -y flowly-ai 2>$null | Out-Null
            }
        }
        catch { }
        break
    }
}

function Install-SystemDeps {
    # Optional native tools Flowly shells out to for certain features:
    #   ffmpeg  - voice messages and the video skills (pixel-art, etc.)
    #   ripgrep - fast file search for the agent (it falls back to a slower
    #             search without it)
    # These are nice-to-have: Flowly degrades gracefully when they're missing.
    # Offer them via winget (the built-in Windows package manager). Best-effort -
    # never fails the install.
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

# ── Main ────────────────────────────────────────────────────────────────────
Install-Uv
$git = Install-Git
Update-Checkout -Git $git
Install-FromSource

# Prove the new venv works before touching any previous install, so a failed
# clone/build can never leave the machine with no working flowly.
$venvPy = Join-Path $Venv 'Scripts\python.exe'
& $venvPy -m flowly --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'The new Flowly venv did not run (python -m flowly --version failed).' }

# Only now retire the old install, and BEFORE writing our launcher.
Remove-LegacyPyPiInstall

$flowly = Install-Launcher
Add-UserPath -PathToAdd $BinDir

& $flowly --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Flowly was installed, but the launcher at $flowly did not run." }

Install-SystemDeps

Write-Ok "Flowly CLI installed (git checkout: $Src)."
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
