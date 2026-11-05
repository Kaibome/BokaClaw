---
name: bokaclaw-db-query
description: 只读查询本地 BokaClaw Postgres 数据库，无需在宿主机安装 psql：通过 `docker exec` 在 `bokaclaw-postgres` 容器内运行 `psql`，提供列出表（含精确行数和磁盘占用）、查看表结构、统计行数、执行任意 SELECT 和导出 CSV 的辅助命令。覆盖两个数据库：`mem0_app`（users、conversations、request_logs、agent_runs 等）和 `postgres`（mem0 的 `memories` 向量表）。触发场景：询问有哪些表、查看/描述表结构、统计数据行数、查询会话/请求日志/记忆/Agent 运行记录，或对本地 BokaClaw 数据库执行 SQL。不触发场景：启动/停止/构建容器（请用 bokaclaw-docker-stack）、修改应用代码或迁移、查询远程/托管数据库。
license: Apache-2.0
metadata:
  author: BokaClaw
  version: "0.1.0"
  category: database
  tags: "postgres, psql, docker, sql, tables, mem0, bokaclaw"
---

# Query the local BokaClaw database

Everything runs through `psql` **inside** `bokaclaw-postgres` via
`docker exec`. Nothing is installed on macOS; do not add a host `psql`, a
`PGPASSWORD`, or connection plumbing.

```bash
.claude/skills/bokaclaw-db-query/scripts/db.sh [flags] <command> [args]
```

Flags go **before** the command.

| Flag | Meaning |
|---|---|
| `-d, --db <name>` | `mem0_app` (default) or `postgres` |
| `--csv` | CSV output, for `query` |
| `-w, --write` | Allow writes (default is a read-only transaction) |
| `-h, --help` | Usage |

| Command | Output |
|---|---|
| `tables` | Every table with exact row count and total size |
| `databases` | Database names and sizes |
| `schema <table>` | `\d+`: columns, indexes, constraints |
| `count <table>` | Exact row count |
| `query <sql>` | Run SQL; omit SQL to read it from stdin |

```bash
db.sh tables
db.sh -d postgres tables                        # mem0 vector database
db.sh schema conversations
db.sh count request_logs
db.sh query "SELECT id, title FROM conversations ORDER BY created_at DESC LIMIT 5"
db.sh query < report.sql
db.sh --csv query "SELECT * FROM request_logs LIMIT 20" > logs.csv
db.sh -w query "UPDATE conversations SET title = 'x' WHERE id = '...'"
```

When the user asks for data, run the query and show the rows. Do not paste the
SQL back as the answer, and do not summarize away values the user asked for.

## Two databases, one container

| Database | Tables | What lives there |
|---|---|---|
| `mem0_app` | `users`, `api_keys`, `settings`, `conversations`, `conversation_messages`, `request_logs`, `agent_runs`, `agent_run_events`, `refresh_token_jtis`, `alembic_version` | Application state, written by `bokaclaw-server` |
| `postgres` | `memories` | mem0's vector collection (pgvector, 512-dim fastembed vectors) |

`memories` has no foreign key to `users`; the app isolates by `user_id` /
`agent_id` columns inside the row. Its columns are `id`, `vector`, and
`payload` (jsonb) — the memory text is `payload->>'data'`:

```bash
db.sh -d postgres query \
  "SELECT id, payload->>'data' AS memory, payload->>'user_id' AS user_id FROM memories LIMIT 10"
```

## Sensitive rows

`settings` stores runtime configuration, including provider API keys in
`value` (for example the DeepSeek key under `config_overrides`). Never dump the
whole table or its `value` column into a reply. Select specific keys, and
redact secrets (`left(value::text, 20) || '…'`) when the user only needs to
confirm a setting exists.

## Read-only by default

Unless `--write` is passed, the session runs with
`PGOPTIONS=-c default_transaction_read_only=on`, so any write fails at the
database even if the keyword check is bypassed by a function call. The script
also rejects SQL that does not start with `SELECT` / `WITH` / `SHOW` / `TABLE`
/ `VALUES` / `EXPLAIN` / a `\d`-style meta command, which turns the usual
accident into a clear message.

Use `--write` only when the user explicitly asked to change data, and show the
`UPDATE`/`DELETE` before running it.

## Safety

- Never run `docker volume rm mem0-local-pg`, `docker compose down -v`, or
  `colima delete`: they destroy every table in both databases. The volume is
  the whole PG 17 cluster.
- Never mount `~/.colima/_lima/_disks/colima/datadisk` with `hdiutil`/`diskutil`
  while Colima is running. Use `docker exec` (this skill) or `colima ssh`.
- For a backup, dump to the host instead of copying `PGDATA`:
  ```bash
  docker exec bokaclaw-postgres pg_dump -U postgres -Fc mem0_app > mem0_app.dump
  docker exec bokaclaw-postgres pg_dump -U postgres -Fc postgres > memories.dump
  ```

## Overrides

| Variable | Default | Use |
|---|---|---|
| `PG_CONTAINER` | `bokaclaw-postgres` | Container name |
| `PG_USER` | `postgres` | Database role |
| `PG_DB` | `mem0_app` | Default database |

## Troubleshooting

- **`container 'bokaclaw-postgres' is not running`** — start it with
  `.claude/skills/bokaclaw-docker-stack/scripts/start-stack.sh`; do not start a
  second Postgres container.
- **`database "x" does not exist`** — only `mem0_app` and `postgres` exist
  unless the user created another; check with `db.sh databases`.
- **`relation "x" does not exist`** — run `db.sh tables`; psql resolves
  unqualified names through `search_path` (`public`).
- **`cannot execute INSERT in a read-only transaction`** — expected without
  `--write`.
- **`permission denied for table ...`** — the role is `postgres`, which is the
  owner; a different `PG_USER` may lack grants.
- **`no such container` right after a rebuild** — the container was recreated
  under the same name; wait for `docker ps` to show it as `Up`.
