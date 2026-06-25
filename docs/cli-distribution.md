# Flowly CLI Distribution

This document defines the public install contract for Flowly CLI/TUI.

Flowly Desktop and Flowly CLI are distributed differently:

- Desktop ships an embedded Nuitka runtime inside the Electron app.
- CLI installs from PyPI. It must not download a second standalone Flowly
  binary for normal users.

## Public Install Commands

macOS and Linux:

```bash
curl -fsSL https://useflowlyapp.com/install.sh | bash
```

The macOS/Linux installer bootstraps uv when needed, then runs:

```bash
uv tool install --python 3.12 flowly-ai --force
```

uv manages the Python runtime, so users do not need Python installed before
running the installer.

Windows PowerShell:

```powershell
irm https://useflowlyapp.com/install.ps1 | iex
```

The Windows installer uses Python + pip:

```powershell
py -3 -m pip install --user --upgrade flowly-ai
```

If Python 3.11+ is missing, the script stops with a direct Python download
message. Flowly Desktop remains the no-Python path on Windows because it ships
its own embedded runtime.

## Web App Contract

The website should serve these scripts from the web app and keep them in sync
with this repository:

- `GET /install.sh` -> raw contents of `scripts/install.sh`
- `GET /install.ps1` -> raw contents of `scripts/install.ps1`

The combined `/download` page should make the split explicit:

- Desktop App: native GUI app with embedded local runtime.
- CLI/TUI: terminal-first PyPI install; uv-managed Python on macOS/Linux,
  Python + pip on Windows.

## Desktop Runtime Note

The Nuitka build workflow remains useful for Flowly Desktop. It produces the
runtime that Desktop embeds in the app bundle. It should not be presented as
the public CLI installation path.
