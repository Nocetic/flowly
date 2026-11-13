---
name: docker
description: "Author and improve Docker images and Compose stacks — write Dockerfiles (multi-stage builds, layer caching, small/secure images), debug builds and containers, and structure docker-compose. Covers image size, build cache ordering, non-root users, .dockerignore, healthchecks, and common pitfalls. Includes a stdlib Dockerfile linter. Use when the user wants a Dockerfile written or reviewed, a smaller/faster/secure image, a compose file, or help debugging a Docker build/container."
metadata: {"flowly":{"emoji":"🐳","tags":["devops","docker","containers","dockerfile","compose","ci","deployment"],"requires":{"bins":["python3"]},"optional_bins":["docker"],"category":"devops","related_skills":["kubernetes","sql-query","bash-scripting","api-security-audit"]}}
---

# Docker — Small, Cached, Non-Root Images

A good Docker image is **small** (fast to pull, less attack surface), **cache-friendly** (fast rebuilds), and **secure** (non-root, no secrets baked in). Most bad Dockerfiles fail all three by copying everything first, installing as root, and using a giant base. The discipline: order layers from least- to most-frequently-changing, use multi-stage to leave build tools behind, and pin what you depend on.

## What this skill produces

**Chat-first.** Default: the Dockerfile / compose YAML in a fenced block with the reasoning for the structure, plus the `dockerlint.py` findings if reviewing. The actual `docker build/run` is the user's to execute (the skill works fine without the daemon installed). Note image-size and cache implications.

## When to use

- "Write a Dockerfile for \<app/language\>." / "Containerize this."
- "Make my image smaller / build faster." / "Review my Dockerfile."
- "Write a docker-compose for \<services\>."
- "Why is my build slow / image huge / container crashing?"
- "How do I \<multi-stage / cache deps / run as non-root / pass env / mount volume\>?"

## Layer caching — the #1 thing to get right

Each instruction is a cached layer; a change invalidates that layer **and every layer after it**. So order from **least- to most-frequently-changing**:

1. Base image
2. System packages (rarely change)
3. **Dependency manifests only** (package.json / requirements.txt / go.mod) → install deps
4. **Then** copy the application source (changes constantly)

The classic mistake: `COPY . .` *before* installing deps — now every source edit re-runs the full dependency install. Copy the manifest, install, *then* copy the rest:

```dockerfile
COPY package*.json ./
RUN npm ci
COPY . .          # source changes don't bust the npm layer
```

## Multi-stage builds — small final images

Build in a fat stage (compilers, dev deps), copy only the artifacts into a slim runtime stage. The build tools never ship.

```dockerfile
# build stage
FROM golang:1.22 AS build
WORKDIR /src
COPY go.* ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /app

# runtime stage — tiny, no toolchain
FROM gcr.io/distroless/static
COPY --from=build /app /app
USER nonroot:nonroot
ENTRYPOINT ["/app"]
```

## Image size & base choice

- **Pick a slim base:** `python:3.12-slim`, `node:20-alpine`, `distroless`, or `scratch` for static binaries. A `-slim`/`alpine` base can be 10× smaller than the default. (Alpine uses musl libc — occasionally breaks wheels/glibc binaries; `-slim` Debian is the safe default.)
- **Clean up in the same RUN layer** (a separate cleanup layer doesn't shrink the image): `RUN apt-get update && apt-get install -y --no-install-recommends X && rm -rf /var/lib/apt/lists/*`.
- **`.dockerignore`** is mandatory — exclude `.git`, `node_modules`, build output, secrets; it shrinks the build context (faster) and prevents leaking files.
- Fewer, well-ordered layers; combine related RUNs but don't sacrifice caching.

## Security

- **Run as non-root.** Create a user and `USER` to it; root in a container is root-ish on escape and bad practice.
- **No secrets in the image.** ENV/ARG and `COPY` bake secrets into layers (visible in history forever). Use build secrets (`--mount=type=secret`), runtime env, or a secrets manager.
- **Pin versions** — base image tags (ideally by digest) and package versions — for reproducible, non-surprising builds. `latest` drifts.
- **Minimal surface:** distroless/slim has fewer CVEs and no shell to exploit. Scan images (Trivy/Scout). (→ `api-security-audit` for app-level security.)

## Runtime correctness

- **ENTRYPOINT vs CMD:** ENTRYPOINT = the executable (fixed), CMD = default args (overridable). Use **exec form** (`["app","--flag"]`) not shell form so signals (SIGTERM) reach your process for clean shutdown.
- **One concern per container**; use compose/orchestration for multi-service.
- **HEALTHCHECK** so the platform knows when the container is actually ready/alive.
- **EXPOSE** documents ports; publish with `-p`. Persist data in **volumes**, not the container layer.

## docker-compose (local multi-service)

```yaml
services:
  api:
    build: .
    ports: ["8080:8080"]
    environment: [DATABASE_URL=postgres://db/app]
    depends_on:
      db: {condition: service_healthy}
  db:
    image: postgres:16-alpine
    environment: [POSTGRES_DB=app]
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "postgres"]
      interval: 5s
    volumes: ["pgdata:/var/lib/postgresql/data"]
volumes: {pgdata: {}}
```
`depends_on` only waits for *start* unless you add a healthcheck condition — otherwise the app races the DB.

## The linter

`scripts/dockerlint.py` scans a Dockerfile for the common issues above (stdlib, heuristic):
```bash
python3 scripts/dockerlint.py Dockerfile
```
Flags: unpinned base (`latest`/no tag), `COPY . ` before dependency install, running as root, `apt-get` without cleanup/`--no-install-recommends`, secrets in ENV/ARG, shell-form ENTRYPOINT/CMD, missing `.dockerignore` (if path given), and `ADD` where `COPY` suffices.

## Chat output format

````
**Dockerfile — Node app (multi-stage, non-root, cached)**

```dockerfile
FROM node:20-slim AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
COPY --from=build /app/dist ./dist
COPY --from=build /app/node_modules ./node_modules
USER node
HEALTHCHECK CMD node healthcheck.js
CMD ["node", "dist/server.js"]
```
Deps cached separately from source; runtime stage drops build artifacts; runs as `node`.
Add a .dockerignore (node_modules, .git, dist). Lint: `dockerlint.py Dockerfile`.
````

## Workflow

1. **Identify** the language/runtime, build steps, ports, and runtime deps.
2. **Structure for cache:** base → system pkgs → dep manifest + install → source. Use **multi-stage** if there's a build step.
3. **Slim & secure:** slim/distroless base, non-root USER, no secrets, pinned versions, `.dockerignore`.
4. **Runtime:** exec-form ENTRYPOINT/CMD, HEALTHCHECK, volumes for data.
5. **Lint** with `dockerlint.py`; for multi-service add compose with healthcheck-gated `depends_on`.
6. **Deliver** the Dockerfile/compose + reasoning; route orchestration to `kubernetes`, app security to `api-security-audit`, scripts to `bash-scripting`.

## Key pitfalls

- **`COPY . .` before installing deps.** Busts the dependency cache on every source change — copy the manifest and install first.
- **Cleanup in a separate layer.** `rm` in a new RUN doesn't shrink earlier layers; clean in the *same* RUN as the install.
- **Running as root.** Default user is root; create and switch to a non-root user.
- **Secrets in ENV/ARG/COPY.** Baked into image history permanently — use build secrets or runtime env.
- **`latest` / unpinned base.** Non-reproducible; pin tags (or digests) and package versions.
- **Shell-form CMD/ENTRYPOINT.** Wraps in `/bin/sh -c`, so SIGTERM doesn't reach your app → unclean shutdown/zombie. Use exec form `["..."]`.
- **No `.dockerignore`.** Bloats the build context and risks copying `.git`/secrets/`node_modules`.
- **Fat base image.** Default tags are huge; use `-slim`/`alpine`/distroless.
- **`ADD` for plain files.** Use `COPY`; reserve `ADD` for remote URLs/tar auto-extract (and prefer not to).
- **`depends_on` without healthcheck.** The app starts before the DB is ready and crashes.

## Quick reference

- Cache order: base → system pkgs → dep manifest+install → source (least→most changing).
- Multi-stage: build fat, `COPY --from=build` artifacts into a slim runtime stage.
- Slim/distroless base; clean apt in the same RUN with `--no-install-recommends` + `rm -rf /var/lib/apt/lists/*`.
- Non-root `USER`; no secrets in layers; pin base+package versions; ship a `.dockerignore`.
- Exec-form `ENTRYPOINT`/`CMD`; add `HEALTHCHECK`; volumes for data. `dockerlint.py` to check.
- Orchestration → kubernetes; app sec → api-security-audit.
