# Flowly CLI Distribution

This document defines the public install contract for Flowly CLI/TUI.

Flowly Desktop and Flowly CLI are distributed differently:

- Desktop ships an embedded Nuitka runtime inside the Electron app.
- CLI installs from a **git checkout**: the native script clones the repository,
  builds an isolated uv-managed virtualenv, and installs Flowly into it as an
  editable checkout. That checkout is what lets `flowly update` fast-forward with
  `git pull`. The installer must not download a second standalone Flowly binary
  for normal users.

## Public Install Commands

macOS and Linux:

```bash
curl -fsSL https://useflowlyapp.com/install.sh | bash
```

The installer bootstraps uv and git when needed, then:

1. clones the repo to `~/.local/share/flowly/repo` (a full single-branch clone —
   **not** `--depth 1` — so the updater can count how far behind you are),
2. creates a uv venv at `~/.local/share/flowly/venv` (a *sibling* of the
   checkout, never inside it, so an update's autostash can't sweep it up),
3. installs Flowly editable into that venv (`uv pip install -e`),
4. symlinks the `flowly` launcher onto your PATH.

uv manages the Python runtime, so users do not need Python installed before
running the installer. Because the venv lives outside `uv/tools` and
`pipx/venvs`, Flowly reports install mode `source`, and `flowly update` updates
it with `git pull --ff-only` + reinstall.

Windows PowerShell:

```powershell
irm https://useflowlyapp.com/install.ps1 | iex
```

The Windows installer mirrors the Unix one. It ensures uv and git — downloading
**portable MinGit** (a plain zip, no admin, isolated from any system Git) when
git is missing — then clones to `%LOCALAPPDATA%\Flowly\repo`, builds a uv venv at
`%LOCALAPPDATA%\Flowly\venv`, installs Flowly editable, and writes a `flowly.cmd`
launcher that runs `python -m flowly` from the venv (so an editable reinstall on
update never has to overwrite a running `flowly.exe`). No system Python is
required. Flowly Desktop remains the separate, self-contained runtime.

## Install-script knobs

Both scripts honor the same overrides, as an environment variable or a flag:

| Override | Default | Purpose |
|---|---|---|
| `FLOWLY_REPO_URL` | the GitHub repo | Clone source |
| `FLOWLY_BRANCH` | `main` | Branch to track |
| `FLOWLY_SRC` | `~/.local/share/flowly/repo` (Unix) / `%LOCALAPPDATA%\Flowly\repo` | Checkout dir |
| `FLOWLY_VENV` | `~/.local/share/flowly/venv` / `%LOCALAPPDATA%\Flowly\venv` | Virtualenv dir |
| `FLOWLY_PYTHON` | `3.12` | uv-managed Python version |

Plus `--skip-bootstrap`, `--no-path-update`, and `--skip-system-deps`.

## Web App Contract

The website should serve these scripts from the web app and keep them in sync
with this repository:

- `GET /install.sh` -> raw contents of `scripts/install.sh`
- `GET /install.ps1` -> raw contents of `scripts/install.ps1`

The combined `/download` page should make the split explicit:

- Desktop App: native GUI app with embedded local runtime.
- CLI/TUI: terminal-first **git-checkout** install; uv-managed Python on every
  platform, portable git fetched on Windows when missing. `flowly update` pulls
  the latest straight from git.

## Legacy packaged installs (PyPI — retired)

Flowly no longer publishes releases to PyPI. The git checkout produced by the
native scripts is the only install path that receives updates.

Installs made back when the package was published (`uv tool install flowly-ai`,
`pipx install flowly-ai`, `pip install --user flowly-ai`) still run, but can
never move forward. `flowly update` recognises them and says so, pointing at the
install script, which migrates in place: `~/.flowly` is untouched, the old
package is retired only after the new install proves it works, and an installed
background service is rewritten onto the new install and restarted.

Because that path is gone, so is its machinery: `update` no longer queries the
PyPI JSON API, and the Windows detached-updater helper (which existed only
because pip had to replace a running `flowly.exe`) was removed — the git
install's launcher runs `python -m flowly`, so nothing overwrites a running
executable.

## Desktop Runtime Note

The Nuitka build workflow remains useful for Flowly Desktop. It produces the
runtime that Desktop embeds in the app bundle. It should not be presented as the
public CLI installation path, and `flowly update` no-ops inside it (the managed
binary is owned by the desktop app's own updater).
