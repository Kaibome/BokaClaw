---
name: build-bokaclaw-images
description: >
  Builds the two split BokaClaw images — `bokaclaw-server:test` from the
  repository root (includes `server/` and `agent-loop/`) and
  `bokaclaw-dashboard:test` from `dashboard/` — using fast package mirrors
  suitable for the local Lima/Colima VM, then smoke-tests both images.
  TRIGGER when: asked to build or rebuild a BokaClaw image; a Docker build is
  stuck or failing on pip/npm downloads; files under `server/`,
  `agent-loop/`, `server/requirements.txt`, `dashboard/`, or either Dockerfile
  changed. DO NOT TRIGGER when: starting or stopping already-built containers
  (use `run-bokaclaw-stack`) or when working with the legacy all-in-one image.
license: Apache-2.0
metadata:
  author: BokaClaw
  version: "0.1.0"
  category: deployment
  tags: "docker, build, mirror, lima, colima, pypi, pnpm"
---

# Build the split BokaClaw images

Local builds run inside Docker Desktop/Colima, which is a Lima VM on this
machine. Direct PyPI and npm downloads from that VM measured **10–30 kB/s**;
the Tsinghua and npmmirror mirrors measured **~800 kB/s**. Always pass the
mirror build args locally. The Dockerfiles default to the official registries
so CI and other machines are unaffected.

## Commands

Run from the repository root:

```bash
# server: context is the repo root (it copies server/ AND agent-loop/)
docker build \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -f server/Dockerfile \
  -t bokaclaw-server:test .

# dashboard: context is dashboard/
docker build \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  -t bokaclaw-dashboard:test dashboard
```

Or run `scripts/build-images.sh`, which does both and then smoke-tests.

## Build contexts

| Image | Dockerfile | Context | Reason |
|---|---|---|---|
| `bokaclaw-server` | `server/Dockerfile` | repo root `.` | Dockerfile runs `COPY server/requirements.txt`, `COPY agent-loop /opt/agent-loop`, `COPY server/ .` |
| `bokaclaw-dashboard` | `dashboard/Dockerfile` | `dashboard/` | Standalone Next.js build; `entrypoint.sh` is copied from the context |

Never run the server build from inside `server/` — the `COPY server/...`
lines only resolve when the context is the repository root.

## Preconditions

- Docker daemon reachable (`docker info`); on macOS this is Colima, so the
  daemon itself runs in the Lima VM.
- Repository root is the working directory.
- Root `.dockerignore` excludes `.git`, `**/node_modules`, `**/.next`,
  `**/.env`, `**/.venv` — never remove those entries.
- No secret is passed as a build arg. Build args are recorded in image
  history; only registry URLs belong there. Runtime secrets go in
  `server/.env` (gitignored).

## What each build does

**Server** (`python:3.12-slim`): installs `server/requirements.txt`, installs
`agent-loop/` as the `bokaclaw-agent` package used by the in-process Agent
runtime, copies `server/`, exposes 8000.

**Dashboard** (`node:20-alpine`, multi-stage): `pnpm install` (falling back to
yarn/npm only if their lockfiles exist) → `npm run build` → Next.js
`standalone` output. `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_INSTANCE_NAME`
are baked in as literal placeholders and substituted at container start by
`entrypoint.sh`. Any new `NEXT_PUBLIC_*` variable must be declared with
`ENV NAME=NAME` in the Dockerfile or substitution silently skips it.

## Verify before declaring success

```bash
docker run --rm --entrypoint python bokaclaw-server:test \
  -c 'import fastapi, httpx, sqlalchemy, alembic, bokaclaw_agent; print("server-ok")'
docker run --rm --entrypoint sh bokaclaw-dashboard:test \
  -c 'test -f /app/server.js && test -d /app/.next/static && echo dashboard-ok'
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep bokaclaw
```

Expected approximate sizes: server ~900 MB, dashboard ~310 MB.

## Pitfalls (all seen in this repo)

Read `references/troubleshooting.md` for the full symptom → fix table. The
short version:

1. **Do not add `--frozen-lockfile` to `pnpm i`.** The current
   `package.json` overrides do not match `pnpm-lock.yaml`; frozen mode fails
   with `ERR_PNPM_LOCKFILE_CONFIG_MISMATCH`. The Dockerfile deliberately uses
   plain `pnpm i --registry=...`.
2. **pip needs long timeouts.** A single PyPI read timeout aborted a 40-minute
   build. Keep `--retries 10 --timeout 120` in the Dockerfile.
3. **Server build context must be the repo root.** A context of `server/`
   fails on `COPY server/requirements.txt`.
4. **Image architecture follows the host.** Colima is aarch64, so images are
   arm64. Only add `--platform linux/amd64` when explicitly deploying to
   amd64 (much slower under emulation); keep the tag distinct.
5. **Rebuild after Dockerfile changes.** Editing `server/` code invalidates
   the final `COPY`, not the dependency layer, so rebuilds stay fast. Editing
   `requirements.txt` or `agent-loop/` invalidates the pip layer.
6. **`docker build` is sequential in scripts.** Chaining with `;` (not `&&`)
   lets the second image build even if the first fails, and the final
   `RESULT` line tells you which one failed.

## Done criteria

- Both `docker build` commands exit 0.
- Both smoke checks print `server-ok` / `dashboard-ok`.
- `docker images` shows the expected tags and sizes.
- No plaintext secrets appear in `docker history` — only the mirror URLs.
