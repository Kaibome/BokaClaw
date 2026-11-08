---
name: bokaclaw-docker-stack
description: 在 Docker 中本地构建并运行 BokaClaw：从仓库根目录构建 `bokaclaw-server:test`、从 `dashboard/` 构建 `bokaclaw-dashboard:test` 两个镜像（为 Lima/Colima 虚拟机配置了快速软件源），然后在 `bokaclaw-net` 和内部 `bokaclaw-exec` 网桥上启动四个容器——`bokaclaw-postgres`（pgvector/pg17）、`bokaclaw-server`（FastAPI，端口 8000）、`bokaclaw-dashboard`（Next.js，端口 3000）和 `bokaclaw-executor`（沙箱化的 Agent shell，仅内网可达）——并负责 Alembic 迁移、`server/.env` 生成和健康检查。触发场景：要求构建/重建 BokaClaw 镜像，启动或停止本地前端 + 后端 + Postgres，执行数据库迁移，重置管理员密码，或排查构建卡在软件包下载、dashboard 连不上 API/数据库的问题。不触发场景：要求修改应用代码，或仍在处理旧版 all-in-one 镜像。
license: Apache-2.0
metadata:
  author: BokaClaw
  version: "0.2.0"
  category: deployment
  tags: "docker, build, mirror, lima, colima, pypi, pnpm, postgres, fastapi, nextjs"
---

# Build and run BokaClaw in Docker

One skill for the whole local loop: build the two images, then run the four
containers. Repository root is the working directory for every command.

```bash
.claude/skills/bokaclaw-docker-stack/scripts/build-images.sh   # images
.claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh    # containers
```

After rebuilding an image, `RECREATE=1 start-stack.sh` replaces the existing
server/dashboard/executor containers with the new image. `stop-stack.sh
[--remove]` stops or removes containers; it never touches volumes.

## Run one half only

The two halves are independent. When the request names only one, run only that
half; do not build as a side effect of "start", and do not start containers
for "build".

| Request | Command |
|---|---|
| 只打镜像（两个） | `scripts/build-images.sh` |
| 只打 server 镜像 | `scripts/build-images.sh server` |
| 只打 dashboard 镜像 | `scripts/build-images.sh dashboard` |
| 只启动 / 只运行 | `scripts/start-stack.sh` |
| 重建镜像并让容器用上新镜像 | `scripts/build-images.sh && RECREATE=1 scripts/start-stack.sh` |
| 只停止 | `scripts/stop-stack.sh` |

`start-stack.sh` never builds. If an image is missing it exits with
`image ... not found; run .../build-images.sh first` — report that instead of
silently building.

## Part 1 — Build

Local builds run inside Docker Desktop/Colima, a Lima VM on this machine.
Direct PyPI and npm downloads from that VM measured **10–30 kB/s**; the
Tsinghua and npmmirror mirrors measured **~800 kB/s**. Pass the mirrors as
build args locally; the Dockerfiles default to the official registries, so CI
is unaffected.

```bash
# server: context is the repo root (it copies server/ AND agent-loop/)
docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -f server/Dockerfile -t bokaclaw-server:test .

# dashboard: context is dashboard/
docker build --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  -t bokaclaw-dashboard:test dashboard
```

| Image | Dockerfile | Context | Why |
|---|---|---|---|
| `bokaclaw-server` | `server/Dockerfile` | repo root `.` | Installs `server/requirements.txt`, installs `agent-loop/` as `bokaclaw-agent` for the in-process Agent runtime, copies `server/` |
| `bokaclaw-dashboard` | `dashboard/Dockerfile` | `dashboard/` | Next.js standalone build; `entrypoint.sh` comes from the context |

Never build the server from inside `server/` — its `COPY server/...` lines
only resolve with the repo-root context.

Verify before starting:

```bash
docker run --rm --entrypoint python bokaclaw-server:test \
  -c 'import fastapi, httpx, sqlalchemy, alembic, bokaclaw_agent; print("server-ok")'
docker run --rm --entrypoint sh bokaclaw-dashboard:test \
  -c 'test -f /app/server.js && test -d /app/.next/static && echo dashboard-ok'
```

Expected sizes: server ~900 MB, dashboard ~310 MB.

Build pitfalls (full log in `references/troubleshooting.md`):

1. **No `--frozen-lockfile`.** `package.json` overrides do not match
   `pnpm-lock.yaml`; frozen mode fails with
   `ERR_PNPM_LOCKFILE_CONFIG_MISMATCH`. The Dockerfile uses plain
   `pnpm i --registry=...`.
2. **Keep pip retries/timeouts.** `--retries 10 --timeout 120` prevents one
   stalled PyPI connection from aborting a long build.
3. **Architecture follows the host.** Colima is aarch64, so images are arm64;
   use `--platform linux/amd64` with a distinct tag only for amd64 targets.
4. **No secrets as build args.** They are recorded in image history. Runtime
   secrets belong in `server/.env` (gitignored, excluded by `.dockerignore`).
5. **`NEXT_PUBLIC_*` placeholders** are baked at build time and substituted at
   container start. A new variable needs `ENV NAME=NAME` in the Dockerfile.

## Part 2 — Run

| Container | Image | Host port | Purpose |
|---|---|---|---|
| `bokaclaw-postgres` | `pgvector/pgvector:pg17` | 8432 | `postgres` DB (legacy Mem0 tables) + `mem0_app` DB (users, conversations, `memory_index_*`) |
| `bokaclaw-server` | `bokaclaw-server:test` | 8000 | FastAPI; `/agent`, `/conversations`, `/memories`, `/auth` |
| `bokaclaw-dashboard` | `bokaclaw-dashboard:test` | 3000 | Next.js UI; `/` redirects to `/login` |
| `bokaclaw-executor` | `bokaclaw-server:test` | none | Sandboxed shell executor for agent `Bash` / `PowerShell` / `Monitor` |

Browser → `http://localhost:3000`; browser API calls →
`http://localhost:8000`; dashboard server-side calls →
`http://bokaclaw-server:8000`; server shell tools →
`http://bokaclaw-executor:8099` on the internal `bokaclaw-exec` network.

`start-stack.sh` generates `server/.env` with random `POSTGRES_PASSWORD`,
`JWT_SECRET`, `ADMIN_API_KEY` when missing, creates `bokaclaw-net`, starts
Postgres, **synchronises the Postgres password with `.env`**, idempotently
adds the bridge rule to `pg_hba.conf`, runs `alembic upgrade head`, starts the
sandboxed executor (read-only rootfs, non-root, workspace mounted rw), starts
the server and dashboard, then waits for `/openapi.json` and `/api/health`.

The agent workspace (`AGENT_WORKSPACE_ROOT`, default `/app/workspace`) is
bind-mounted from `WORKSPACE_HOST`, default `/Users/pankaibo/BokaClaw/workspace`.
Override it for another machine: `WORKSPACE_HOST=/path/to/workspace
scripts/start-stack.sh`. Files written by the agent survive `RECREATE=1`
because they live on the host, not in the container layer.

Manual container commands are in `references/env-and-volumes.md` if the script
fails and you need to isolate a step.

### `server/.env`

Gitignored. Never commit, print, or copy it into an image. Full template and
field meanings: `references/env-and-volumes.md`. Minimum for this stack:

- `POSTGRES_*` (password required), `POSTGRES_DB=postgres`,
  `APP_DB_NAME=mem0_app`
- `JWT_SECRET`, `ADMIN_API_KEY`, `DASHBOARD_URL=http://localhost:3000`
- fastembed settings (`MEMORY_DEFAULT_EMBEDDER_PROVIDER=fastembed`,
  `MEMORY_DEFAULT_EMBEDDER_MODEL=BAAI/bge-small-zh-v1.5`,
  `HF_ENDPOINT=https://hf-mirror.com`). The ONNX model itself is cached at
  `/app/history/fastembed` on the `mem0-local-history` volume
  (`FASTEMBED_CACHE_PATH`, passed by `start-stack.sh`), so `RECREATE=1` does not
  download it again; if a first download crawls, see
  `references/troubleshooting.md` §9.
- `MEMORY_BACKEND=markdown`, `AGENT_MEMORY_ROOT=/app/memory` (the
  `mem0-local-memory` volume, mounted into the server only — never into the
  executor)
- `AGENT_LOOP_MOCK=true` for UI-only tests; real replies need
  `AGENT_LOOP_MOCK=false` plus `AGENT_MODEL` / `AGENT_API_KEY` / `AGENT_BASE_URL`
  (DeepSeek: `deepseek-flash`, `https://api.deepseek.com`)
- `AGENT_WORKSPACE_ROOT=/app/workspace`, bind-mounted from `WORKSPACE_HOST`
- `AGENT_EXECUTOR_URL=http://bokaclaw-executor:8099` and
  `AGENT_EXECUTOR_TOKEN` (both appended by `start-stack.sh`) so shell tools
  run in the executor instead of the server process

### Reusing the old all-in-one volume

`mem0-local-pg` was created by the deleted all-in-one container, so:

1. `pg_hba.conf` only allows `127.0.0.1`; other containers fail with
   `FATAL: no pg_hba.conf entry for host ... no encryption`. The start script
   appends `host all all 0.0.0.0/0 scram-sha-256` and reloads.
2. The Postgres password is unknown. The script runs
   `ALTER USER postgres PASSWORD '<value from .env>'` through the local trust
   socket, making `.env` the source of truth.

The volume's legacy pgvector collection was created at **512 dimensions**. The
workspace index uses `halfvec(3072)` and zero-pads smaller vectors, so a model
change no longer breaks inserts; rows from a different model stay keyword-only
until `python -m app.memory.cli reindex` re-embeds them.

### Health checks

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/openapi.json   # 200
curl -s http://localhost:8000/auth/setup-status                                # {"needsSetup":false}
curl -s http://localhost:8000/api-keys -H "X-API-Key: $ADMIN_API_KEY"          # auth works
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/login           # 200
curl -sN -X POST http://localhost:8000/agent \
  -H "X-API-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"message":"ping","stream":true}'                                        # meta → delta → done (mock)
docker exec bokaclaw-postgres psql -d mem0_app -tAc 'SELECT version_num FROM alembic_version'  # 014
docker exec bokaclaw-server python -m app.memory.cli reconcile --all
```

### Login and accounts

If an admin account exists but its password is unknown, reset it (overwrites
the old password, minimum 8 characters):

```bash
docker exec -e EMAIL=you@example.com -e PASSWORD='new-password' \
  -e PYTHONPATH=/app bokaclaw-server \
  python scripts/reset_admin_password.py
```

Registration (`POST /auth/register`) only works while `users` is empty.

### Operations

```bash
docker logs -f bokaclaw-server
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
lsof -nP -iTCP:3000 -iTCP:8000 -iTCP:8432 -sTCP:LISTEN
```

Do not mix this stack with `server/docker-compose.yaml`: that compose file
builds `server/dev.Dockerfile`, mounts the repo, publishes the API on **8888**
and conflicts on ports 8432/3000.

## Done criteria

Build:
- Both builds exit 0 and the smoke checks print `server-ok` / `dashboard-ok`.

Run:
- All four containers are `Up`; postgres/server/dashboard publish the ports above, the executor publishes none.
- `/openapi.json` and `/login` return 200.
- `alembic_version` is the latest revision (`007`).
- The Agent page streams a mock reply when `AGENT_LOOP_MOCK=true`.
- `docker exec bokaclaw-server python -c "import urllib.request;print(urllib.request.urlopen('http://bokaclaw-executor:8099/healthz').read().decode())"` returns `status: ok`.
