---
name: run-bokaclaw-stack
description: >
  Starts, migrates and health-checks the local BokaClaw stack as three
  containers — `bokaclaw-postgres` (pgvector/pg17), `bokaclaw-server` (FastAPI,
  port 8000) and `bokaclaw-dashboard` (Next.js, port 3000) — on a shared
  `bokaclaw-net` bridge, reusing the `mem0-local-pg` and `mem0-local-history`
  volumes. TRIGGER when: asked to start/stop/restart the local frontend +
  backend + Postgres, run Alembic migrations, reset the admin password, or
  diagnose why the dashboard cannot reach the API/database. DO NOT TRIGGER
  when: building or rebuilding images (use `build-bokaclaw-images`) or when
  asked to change application code.
license: Apache-2.0
metadata:
  author: BokaClaw
  version: "0.1.0"
  category: deployment
  tags: "docker, local, postgres, fastapi, nextjs, alembic"
---

# Run the local BokaClaw stack

Three containers on one Docker network. Images must already be built
(`build-bokaclaw-images`); this skill never builds.

| Container | Image | Host port | Purpose |
|---|---|---|---|
| `bokaclaw-postgres` | `pgvector/pgvector:pg17` | 8432 | `postgres` DB (mem0 memories) + `mem0_app` DB (users, conversations) |
| `bokaclaw-server` | `bokaclaw-server:test` | 8000 | FastAPI; `/agent`, `/conversations`, `/memories`, `/auth` |
| `bokaclaw-dashboard` | `bokaclaw-dashboard:test` | 3000 | Next.js UI; redirects `/` → `/login` |

Browser → `http://localhost:3000`; browser API calls → `http://localhost:8000`;
dashboard server-side calls → `http://bokaclaw-server:8000`.

## One command

```bash
.claude/skills/run-bokaclaw-stack/scripts/start-stack.sh
```

The script generates `server/.env` with random `POSTGRES_PASSWORD`,
`JWT_SECRET` and `ADMIN_API_KEY` if the file is missing, creates the network,
starts Postgres, waits, **synchronises the Postgres password with `.env`**,
adds the bridge rule to `pg_hba.conf`, runs `alembic upgrade head`, starts the
server and dashboard, and waits for `/openapi.json` and `/api/health`.

Stop without deleting data:

```bash
.claude/skills/run-bokaclaw-stack/scripts/stop-stack.sh          # docker stop
.claude/skills/run-bokaclaw-stack/scripts/stop-stack.sh --remove # docker rm as well
```

`--remove` never removes volumes.

## Manual equivalent

Use these when debugging the script; all commands run from the repository root.

```bash
docker network create bokaclaw-net          # ignore "already exists"

docker run -d --name bokaclaw-postgres --network bokaclaw-net --restart unless-stopped \
  --env-file server/.env -p 8432:5432 \
  -v mem0-local-pg:/var/lib/postgresql/data \
  pgvector/pgvector:pg17 postgres -c listen_addresses='*'

# one-off migration (the image does NOT migrate on start)
docker run --rm --network bokaclaw-net --env-file server/.env \
  -e POSTGRES_HOST=bokaclaw-postgres -e POSTGRES_PORT=5432 \
  -v mem0-local-history:/app/history \
  bokaclaw-server:test alembic upgrade head

docker run -d --name bokaclaw-server --network bokaclaw-net --restart unless-stopped \
  --env-file server/.env -e POSTGRES_HOST=bokaclaw-postgres -e POSTGRES_PORT=5432 \
  --add-host host.docker.internal:host-gateway \
  -p 8000:8000 -v mem0-local-history:/app/history \
  bokaclaw-server:test uvicorn main:app --host 0.0.0.0 --port 8000

docker run -d --name bokaclaw-dashboard --network bokaclaw-net --restart unless-stopped \
  -e NEXT_PUBLIC_API_URL=http://localhost:8000 \
  -e API_INTERNAL_URL=http://bokaclaw-server:8000 \
  -e NEXT_PUBLIC_INSTANCE_NAME=BokaClaw \
  -p 3000:3000 bokaclaw-dashboard:test
```

Override the image default `CMD` (`--reload`) as shown; `--reload` is for
development and wastes CPU in a long-running local stack.

## `server/.env`

Gitignored. Never commit it, never print it, never copy it into an image.
The template and the meaning of every value are in
`references/env-and-volumes.md`. Minimum for a working local stack:

- `POSTGRES_*` (password required), `APP_DB_NAME=mem0_app`, `POSTGRES_DB=postgres`
- `JWT_SECRET`, `ADMIN_API_KEY`, `DASHBOARD_URL=http://localhost:3000`
- `MEM0_DEFAULT_EMBEDDER_PROVIDER=fastembed`,
  `MEM0_DEFAULT_EMBEDDER_MODEL=BAAI/bge-small-zh-v1.5`,
  `MEM0_DEFAULT_EMBEDDING_DIMS=512`, `HF_ENDPOINT=https://hf-mirror.com`
  when reusing an existing pgvector volume
- `AGENT_LOOP_MOCK=true` for UI-only smoke tests; real Agent replies need a
  model key (`OPENAI_API_KEY`) and `AGENT_LOOP_MOCK=false`

## Reusing the old all-in-one volume

`mem0-local-pg` was created by the deleted all-in-one container. Two things
differ from a fresh volume:

1. **`pg_hba.conf` only allows `127.0.0.1`**, so containers get
   `FATAL: no pg_hba.conf entry for host ... no encryption`. Append
   `host all all 0.0.0.0/0 scram-sha-256` and reload. The start script does
   this idempotently.
2. **The Postgres password is unknown** (the container holding it was
   deleted). The script always runs
   `ALTER USER postgres PASSWORD '<value from .env>'` through the container's
   local trust socket, so `.env` becomes the source of truth.

If the volume holds memories created with the fastembed model, pgvector fixed
the vector width at 512. Starting the server with the default OpenAI embedder
(1536) produces dimension-mismatch errors. Keep the fastembed settings above,
or start a fresh volume if you intend to switch models.

## Health checks

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/openapi.json   # 200
curl -s http://localhost:8000/auth/setup-status                                # {"needsSetup":false}
curl -s http://localhost:8000/api-keys -H "X-API-Key: $ADMIN_API_KEY"          # API auth works
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/login           # 200
curl -sN -X POST http://localhost:8000/agent \
  -H "X-API-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"message":"ping","stream":true}'                                        # meta → message.delta → done (mock)
docker exec bokaclaw-postgres psql -d mem0_app -tAc 'SELECT version_num FROM alembic_version'  # 007
```

## Login and accounts

The database may already contain an admin account whose password is unknown.
Reset it (minimum 8 characters) — this overwrites the old password:

```bash
docker exec -e EMAIL=you@example.com -e PASSWORD='new-password' \
  -e PYTHONPATH=/app bokaclaw-server \
  python scripts/reset_admin_password.py
```

Registration (`POST /auth/register`) only works while `users` is empty.

## Operations

```bash
docker logs -f bokaclaw-server
docker logs -f bokaclaw-dashboard
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
lsof -nP -iTCP:3000 -iTCP:8000 -iTCP:8432 -sTCP:LISTEN
```

After rebuilding an image, the existing container keeps running the old one.
Recreate the server/dashboard containers:

```bash
RECREATE=1 .claude/skills/run-bokaclaw-stack/scripts/start-stack.sh
```

## Do not mix with `server/docker-compose.yaml`

That compose file builds `server/dev.Dockerfile` with the repo mounted and
publishes the API on **8888**, not 8000. It conflicts with these containers on
ports 8432/3000. Stop this stack first if you need the compose workflow.

## Done criteria

- All three containers are `Up` and the ports above are published.
- `/openapi.json` and `/login` return 200.
- `alembic_version` equals the latest revision (`007`).
- The Agent page shows a streamed mock reply when `AGENT_LOOP_MOCK=true`.
