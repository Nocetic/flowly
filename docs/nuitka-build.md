# macOS standalone build checks

The build workflow lives in `Nocetic/flowlyai-build`. It checks out this repository
at one resolved commit for both ARM64 and Intel. Manual builds can supply a full
40-character `source_sha`; when omitted, the workflow resolves `main` once.

## Dependency contract

Install with `uv sync --locked --group nuitka`. The `nuitka` dependency group pins
the compiler and its dependencies in `uv.lock`, without adding compiler tooling
to the installed agent's runtime dependencies. Subsequent build commands use
`uv run --no-sync` to keep that verified environment intact.

The current MCP 2 SDK uses `httpx2` for SSE and Streamable HTTP and `mcp_types` for
protocol models. It does not require `httpx_sse`. Adding that obsolete package to
the application would hide a stale build recipe rather than fix its includes.

## Before compilation

`scripts/nuitka_preflight.py` takes the same arguments as `python -m nuitka` and
checks the compiler version, installed agent version, explicit module includes,
distribution metadata, bundled data paths, and lazy MCP transport imports.
Missing requirements are reported together and stop the build before compilation.
Use `--check-only` to run those checks without compiling.

The existing conditional includes in `flowly/cli/entry.py` remain authoritative
for semantic routing. Intel macOS sets `FLOWLY_NUITKA_NO_SEMANTIC=1`; ARM64 keeps
the semantic runtime and the onnxruntime dylib repair step.

## Artifact acceptance

Both macOS jobs verify the binary architecture, require `--version` to report the
source project's version, and run `mcp --help` with an isolated `FLOWLY_HOME`.
Compiler XML reports are retained as diagnostic artifacts. A successful Python
preflight is not proof that the compiled artifact works; the native binary smoke
checks must also pass before Desktop packaging.

Run a macOS-only validation build with:

```sh
gh workflow run nuitka-binary.yml -R Nocetic/flowlyai-build \
  --ref main -f source_sha="$(git rev-parse HEAD)" \
  -f skip_windows=true -f skip_linux=true
```

The source commit must already be pushed. Manual runs create build artifacts,
not a GitHub Release or a Desktop auto-update. Windows and Linux build validation
is a separate step; a passing macOS run does not certify those platforms.
