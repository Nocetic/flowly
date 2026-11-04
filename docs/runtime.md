# The Flowly runtime

The runtime is how Core ships inside Flowly Desktop. It is a plain folder:

- a portable CPython (the build uv installs);
- the dependencies `uv.lock` pins;
- Core, built from one git tag;
- a `flowly` launcher.

Nothing is compiled. Running Core from the runtime is the same as running it from a checkout, frozen at one version.

The CLI does not use it: the terminal install is a git checkout (`docs/cli-distribution.md`).

## Layout

```
flowly-runtime/
├── flowly-runtime.json   {"name": "flowly-runtime", "version", "commit", "target", "python"}
├── bin/flowly            the CLI launcher (bin\flowly.cmd on Windows)
└── python/               the interpreter, with Core in its site-packages
```

**Targets:** `darwin-arm64`, `darwin-x64`, `linux-x64`, `linux-arm64`, `win-x64`. Each target is built on its own machine because the dependencies include native wheels.

## Building one

```bash
uv run --no-project --python 3.12 python scripts/build_runtime.py --ref v3.4.0 --out dist/runtime
```

What the builder does, in order:

1. **Snapshot.** It takes the source with `git archive <ref>`, never from the working tree, so uncommitted edits can't ship.
2. **Python.** It copies uv's portable CPython into `python/` and removes its `EXTERNALLY-MANAGED` marker: this interpreter is ours to fill.
3. **Standard library.** It byte-compiles the standard library.
4. **Dependencies.** It installs exactly what `uv.lock` pins (`uv export --frozen --no-dev`), then installs Core's wheel with `--no-deps`. Everything is byte-compiled.
   - Every optional extra in `pyproject.toml` goes in too, so nothing is downloaded after install. A new extra ships without touching the builder.
   - Two stay out (`LEFT_OUT_EXTRAS`): `dev` (test tools) and `screen` (Linux only; its own installer puts playwright and Chrome into a virtualenv on the cloud machine).
5. **Launchers and manifest.** It writes the launchers and `flowly-runtime.json`.
6. **Smoke test.** It runs the launcher from a neutral folder, and the build fails unless:
   - it reports the tag's version;
   - Core and its native packages import from inside the folder;
   - running it wrote no file into the folder.
7. **Archive.** It writes `flowly-runtime-<version>-<target>.tar.gz` (`.zip` on Windows) with a `.sha256` beside it.

## Releasing

`.github/workflows/runtime.yml` builds every target on its own runner.

- **On a pull request** that touches the builder, it keeps the archives as workflow artifacts.
- **On a tag `vX.Y.Z`:**
  - it checks that the tag matches the version in `pyproject.toml`;
  - it attaches every archive and `.sha256` to that tag's GitHub release.

Flowly Desktop's build downloads them from that release, so a Core release comes before the Desktop release that carries it.

## How Core behaves inside a runtime

`flowly/runtime_bundle.py` finds the runtime from the interpreter: `sys.prefix` is `<runtime>/python`, and the manifest sits beside it.

**Every start uses `-s -P -B`.** The launchers, the background service and Core's own children all pass these flags (`python_command()`):

- `-s`: no user site-packages.
- `-P`: the working folder is not on `sys.path`, so a Core checkout there can't shadow the runtime.
- `-B`: Python never writes `.pyc` files. The folder may sit inside a signed app, and a written file breaks the signature's seal.

**`flowly update` does nothing.** The program carrying the runtime updates it.

**`flowly service install` runs the runtime.** It writes either the launcher the manifest names, or the runtime's own interpreter. It never uses whatever `flowly` comes first on `PATH`.

**The carrying program may add two keys to the manifest:**

- **`"launcher"`:** a path relative to the runtime folder. The service then runs that path. Flowly Desktop on macOS names `FlowlyAgent.app/Contents/MacOS/flowly-agent`, the path its background service has always run.
- **`"host"`:** the program carrying the runtime. With a host named, Core does not wrap itself in the OS sandbox (`flowly/sandbox/cli_wrap.py`), as Desktop's earlier compiled build never did.
