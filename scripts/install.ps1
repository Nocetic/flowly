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

$FlowlyVerbose = $env:FLOWLY_VERBOSE -eq '1'
$FlowlyProgressTailLines = 12
if ($env:FLOWLY_PROGRESS_TAIL_LINES -match '^[0-9]+$') {
    $FlowlyProgressTailLines = [int]$env:FLOWLY_PROGRESS_TAIL_LINES
}
$script:FlowlyProgressRenderedLines = 0
$script:FlowlyProgressLastFrame = ''
$script:FlowlyCursorHidden = $false
$FlowlyEsc = [char]27
$FlowlyUseAnsi = (-not [Console]::IsOutputRedirected) -and (-not $env:NO_COLOR) -and ($env:TERM -ne 'dumb')
$FlowlyBlue = ''
$FlowlyBlueSoft = ''
$FlowlyBlueMuted = ''
$FlowlyGreen = ''
$FlowlyRed = ''
$FlowlyReset = ''
if ($FlowlyUseAnsi) {
    $FlowlyBlue = "${FlowlyEsc}[38;5;45m"
    $FlowlyBlueSoft = "${FlowlyEsc}[38;5;81m"
    $FlowlyBlueMuted = "${FlowlyEsc}[38;5;75m"
    $FlowlyGreen = "${FlowlyEsc}[38;5;49m"
    $FlowlyRed = "${FlowlyEsc}[38;5;203m"
    $FlowlyReset = "${FlowlyEsc}[0m"
}

function Format-FlowlyBrand { "${FlowlyBlue}[flowly]${FlowlyReset}" }
function Write-Step($Message) { Write-Host "$(Format-FlowlyBrand) $Message" }
function Write-Ok($Message)   { Write-Host "${FlowlyGreen}[flowly]${FlowlyReset} $Message" }
function Write-Err($Message)  { Write-Host "${FlowlyRed}[flowly]${FlowlyReset} $Message" }

function Write-Banner {
    if ((Get-FlowlyTerminalWidth) -lt 56) {
        Write-Host "${FlowlyBlueSoft}FLOWLY${FlowlyReset}"
        Write-Host ''
        return
    }

    Write-Host "${FlowlyBlueSoft} ███████╗██╗      ██████╗ ██╗    ██╗██╗  ██╗   ██╗${FlowlyReset}"
    Write-Host "${FlowlyBlueSoft} ██╔════╝██║     ██╔═══██╗██║    ██║██║  ╚██╗ ██╔╝${FlowlyReset}"
    Write-Host "${FlowlyBlue} █████╗  ██║     ██║   ██║██║ █╗ ██║██║   ╚████╔╝ ${FlowlyReset}"
    Write-Host "${FlowlyBlue} ██╔══╝  ██║     ██║   ██║██║███╗██║██║    ╚██╔╝  ${FlowlyReset}"
    Write-Host "${FlowlyBlueMuted} ██║     ███████╗╚██████╔╝╚███╔███╔╝███████╗██║   ${FlowlyReset}"
    Write-Host "${FlowlyBlueMuted} ╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝ ╚══════╝╚═╝   ${FlowlyReset}"
    Write-Host ''
}

function Test-FlowlyAnimation {
    if ($FlowlyVerbose) { return $false }
    if ([Console]::IsOutputRedirected) { return $false }
    if ($env:TERM -eq 'dumb') { return $false }
    return $true
}

function Format-FlowlyElapsed {
    param([int]$Seconds)
    $minutes = [Math]::Floor($Seconds / 60)
    $rest = $Seconds % 60
    return ('{0:00}:{1:00}' -f $minutes, $rest)
}

function Get-FlowlyBar {
    param([int]$Fill)
    $width = 10
    if ($Fill -lt 0) { $Fill = 0 }
    if ($Fill -gt $width) { $Fill = $width }
    return (('#' * $Fill) + ('-' * ($width - $Fill)))
}

function Get-FlowlyBarRenderable {
    param([int]$Fill)
    $raw = Get-FlowlyBar -Fill $Fill
    $filled = $raw.TrimEnd('-')
    $empty = $raw.Substring($filled.Length)
    return "${FlowlyBlue}${filled}${FlowlyBlueMuted}${empty}${FlowlyReset}"
}

function Get-FlowlyTerminalWidth {
    try {
        $width = [Console]::WindowWidth
        if ($width -ge 40) { return $width }
    }
    catch { }
    return 80
}

function Clear-FlowlyProgress {
    if ($script:FlowlyProgressRenderedLines -gt 0) {
        Write-Host -NoNewline ("{0}[{1}A{0}[J" -f $FlowlyEsc, $script:FlowlyProgressRenderedLines)
        $script:FlowlyProgressRenderedLines = 0
        $script:FlowlyProgressLastFrame = ''
    }
}

function Hide-FlowlyCursor {
    if (-not $script:FlowlyCursorHidden) {
        Write-Host -NoNewline "${FlowlyEsc}[?25l"
        $script:FlowlyCursorHidden = $true
    }
}

function Show-FlowlyCursor {
    if ($script:FlowlyCursorHidden) {
        Write-Host -NoNewline "${FlowlyEsc}[?25h"
        $script:FlowlyCursorHidden = $false
    }
}

function Get-FlowlyLogTail {
    param(
        [string]$StdoutPath,
        [string]$StderrPath
    )

    $lines = @()
    foreach ($path in @($StdoutPath, $StderrPath)) {
        if (Test-Path $path) {
            try {
                $lines += Get-Content -Path $path -Tail $FlowlyProgressTailLines -ErrorAction SilentlyContinue
            }
            catch { }
        }
    }
    if ($lines.Count -gt $FlowlyProgressTailLines) {
        $lines = $lines | Select-Object -Last $FlowlyProgressTailLines
    }
    return @($lines)
}

function Join-FlowlyCommandLogs {
    param(
        [string]$LogPath,
        [string]$StdoutPath,
        [string]$StderrPath
    )

    $lines = @()
    foreach ($path in @($StdoutPath, $StderrPath)) {
        if (Test-Path $path) {
            try { $lines += Get-Content -Path $path -ErrorAction SilentlyContinue }
            catch { }
        }
    }
    Set-Content -Path $LogPath -Value $lines -Encoding UTF8
}

function Show-FlowlyProgress {
    param(
        [int]$Fill,
        [string]$Action,
        [int]$ElapsedSeconds,
        [bool]$LogsOpen,
        [string]$LogPath,
        [string]$StdoutPath,
        [string]$StderrPath,
        [bool]$CanReadKeys
    )

    $width = Get-FlowlyTerminalWidth
    $maxAction = $width - 24
    if ($maxAction -lt 20) { $maxAction = 20 }
    $actionText = $Action
    if ($actionText.Length -gt $maxAction) {
        $actionText = $actionText.Substring(0, $maxAction)
    }

    $lines = @()
    $lines += "$(Format-FlowlyBrand) Installing Flowly"
    $lines += ("         [{0}] {1}" -f (Get-FlowlyBarRenderable -Fill $Fill), $actionText)
    $elapsed = Format-FlowlyElapsed -Seconds $ElapsedSeconds
    if ($LogsOpen) {
        $lines += ("         {0}{1} elapsed | press o/Ctrl+O to hide logs{2}" -f $FlowlyBlueMuted, $elapsed, $FlowlyReset)
    }
    elseif ($CanReadKeys) {
        $lines += ("         {0}{1} elapsed | press o/Ctrl+O for logs{2}" -f $FlowlyBlueMuted, $elapsed, $FlowlyReset)
    }
    else {
        $lines += ("         {0}{1} elapsed | log: {2}{3}" -f $FlowlyBlueMuted, $elapsed, $LogPath, $FlowlyReset)
    }

    if ($LogsOpen) {
        $lines += ''
        $lines += "${FlowlyBlueMuted}--- live log ---${FlowlyReset}"
        $tail = Get-FlowlyLogTail -StdoutPath $StdoutPath -StderrPath $StderrPath
        if ($tail.Count -eq 0) {
            $lines += '         waiting for log output'
        }
        else {
            $maxLine = $width - 9
            if ($maxLine -lt 20) { $maxLine = 20 }
            foreach ($line in $tail) {
                $text = [string]$line
                if ($text.Length -gt $maxLine) {
                    $text = $text.Substring(0, $maxLine)
                }
                $lines += ("         {0}" -f $text)
            }
        }
    }

    $block = ($lines -join "`n") + "`n"
    if ($block -eq $script:FlowlyProgressLastFrame) { return }

    Clear-FlowlyProgress
    Write-Host -NoNewline $block
    $script:FlowlyProgressRenderedLines = $lines.Count
    $script:FlowlyProgressLastFrame = $block
}

function Invoke-FlowlyCommand {
    param(
        [int]$StartFill,
        [int]$EndFill,
        [string]$Action,
        [string]$File,
        [string[]]$Arguments
    )

    $logPath = Join-Path ([IO.Path]::GetTempPath()) ("flowly-install-{0}.log" -f ([Guid]::NewGuid().ToString('N').Substring(0, 8)))

    if ($FlowlyVerbose) {
        Write-Step "$Action..."
        & $File @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$Action failed." }
        return
    }

    if (-not (Test-FlowlyAnimation)) {
        Write-Step "$Action..."
        & $File @Arguments *> $logPath
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "$Action complete."
            return
        }
        Write-Err "$Action failed. Full log: $logPath"
        Get-Content -Path $logPath -Tail 120 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
        throw "$Action failed."
    }

    $stdoutPath = "$logPath.out"
    $stderrPath = "$logPath.err"
    $job = Start-Job -ScriptBlock {
        param([string]$Command, [string[]]$CommandArgs, [string]$OutPath, [string]$ErrPath)
        & $Command @CommandArgs > $OutPath 2> $ErrPath
        if ($null -ne $LASTEXITCODE) {
            $LASTEXITCODE
        }
        elseif ($?) {
            0
        }
        else {
            1
        }
    } -ArgumentList $File, (,$Arguments), $stdoutPath, $stderrPath

    $started = Get-Date
    $logsOpen = $false
    $canReadKeys = -not [Console]::IsInputRedirected

    Hide-FlowlyCursor
    while ($job.State -eq 'Running') {
        if ($canReadKeys) {
            try {
                while ([Console]::KeyAvailable) {
                    $key = [Console]::ReadKey($true)
                    $isCtrlO = ($key.Key -eq [ConsoleKey]::O) -and (($key.Modifiers -band [ConsoleModifiers]::Control) -ne 0)
                    if ($key.KeyChar -eq 'o' -or $key.KeyChar -eq 'O' -or $isCtrlO) {
                        $logsOpen = -not $logsOpen
                    }
                }
            }
            catch {
                $canReadKeys = $false
            }
        }

        $elapsedSeconds = [int]((Get-Date) - $started).TotalSeconds
        $fill = $StartFill + [Math]::Floor($elapsedSeconds / 3)
        if ($fill -ge $EndFill) { $fill = $EndFill - 1 }
        if ($fill -lt $StartFill) { $fill = $StartFill }
        if ($fill -lt 1) { $fill = 1 }
        Show-FlowlyProgress -Fill $fill -Action $Action -ElapsedSeconds $elapsedSeconds -LogsOpen $logsOpen -LogPath $logPath -StdoutPath $stdoutPath -StderrPath $stderrPath -CanReadKeys $canReadKeys
        Start-Sleep -Milliseconds 200
        $job = Get-Job -Id $job.Id
    }

    Wait-Job -Job $job | Out-Null
    $statusOutput = Receive-Job -Job $job
    Remove-Job -Job $job -Force
    Join-FlowlyCommandLogs -LogPath $logPath -StdoutPath $stdoutPath -StderrPath $stderrPath

    $status = 1
    if ($statusOutput) {
        $status = [int]($statusOutput | Select-Object -Last 1)
    }
    $elapsedTotal = [int]((Get-Date) - $started).TotalSeconds

    if ($status -eq 0) {
        Show-FlowlyProgress -Fill $EndFill -Action $Action -ElapsedSeconds $elapsedTotal -LogsOpen $logsOpen -LogPath $logPath -StdoutPath $stdoutPath -StderrPath $stderrPath -CanReadKeys $canReadKeys
        Start-Sleep -Milliseconds 80
        Clear-FlowlyProgress
        Show-FlowlyCursor
        Write-Ok ("{0} complete in {1}." -f $Action, (Format-FlowlyElapsed -Seconds $elapsedTotal))
        return
    }

    Clear-FlowlyProgress
    Show-FlowlyCursor
    Write-Err ("{0} failed after {1}. Full log: {2}" -f $Action, (Format-FlowlyElapsed -Seconds $elapsedTotal), $logPath)
    Get-Content -Path $logPath -Tail 120 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
    throw "$Action failed."
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
        Invoke-FlowlyCommand -StartFill 2 -EndFill 4 -Action 'Creating Python environment' -File 'uv' -Arguments @('venv', '--clear', '--python', $Python, $Venv)
    }

    Write-Step "Installing Flowly (editable) from $Src ..."
    Invoke-FlowlyCommand -StartFill 4 -EndFill 7 -Action 'Installing packages' -File 'uv' -Arguments @('pip', 'install', '--python', $venvPy, '-e', $Src)
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

# A gateway service installed by a previous install has THAT install's flowly
# executable baked into its scheduled task. After the old install is retired,
# the task points at a binary that no longer exists and a restart can't bring
# the gateway back. Rewrite the task against this install and restart it.
function Update-ServiceIfInstalled {
    param([string]$Flowly)
    $taskXml = Join-Path $env:USERPROFILE 'AppData\Local\flowly\ai.flowly.gateway.xml'
    if (-not (Test-Path $taskXml)) { return }

    Write-Step 'Refreshing the background service to point at this install...'
    & $Flowly service install --start *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok 'Background service updated and restarted.'
    }
    else {
        Write-Step "Couldn't refresh the service automatically - run: flowly service install --start"
    }
}

# ── Main ────────────────────────────────────────────────────────────────────
Write-Banner
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

Update-ServiceIfInstalled -Flowly $flowly

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
