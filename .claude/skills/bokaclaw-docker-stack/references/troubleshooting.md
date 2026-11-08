# Build troubleshooting log

Symptoms observed on this repo, in the order they appeared. Keep this file
updated when a new failure mode is fixed.

## 1. Build stuck for tens of minutes in `pip install`

**Symptom**

```
#8 [4/5] RUN pip install --no-cache-dir --retries 10 --timeout 120 ...
#8 578.2 Downloading anthropic-0.125.0-py3-none-any.whl (1.2 MB)
#8 688.6   1.2/1.2 MB 12.4 kB/s eta 0:00:00
```

After 14 minutes the build was still downloading wheels; the dashboard build
had not started. PyPI throughput from the Lima VM was 10–30 kB/s.

**Cause**

Docker runs in Colima/Lima. The VM's route to `files.pythonhosted.org` is slow;
this is network, not Docker.

**Fix**

Pass the mirror as a build arg (default stays official):

```bash
docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -f server/Dockerfile -t bokaclaw-server:test .
```

Measured improvement inside the same VM: a 3.1 MB SQLAlchemy wheel went from
~2 minutes to ~4 seconds. The Dockerfile also keeps `--retries 10 --timeout 120`
so one stalled connection does not abort a long install.

## 2. `ReadTimeoutError` from `files.pythonhosted.org`

**Symptom**

```
pip._vendor.urllib3.exceptions.ReadTimeoutError:
HTTPSConnectionPool(host='files.pythonhosted.org', port=443): Read timed out.
```

The build died after ~2360 s because the command was chained with `&&`, so the
dashboard build never ran.

**Fix**

- Use the mirror (item 1).
- Keep retries/timeouts in the Dockerfile.
- Do not chain independent image builds with `&&` in automation; use one
  `docker build` per command and report both exit codes.

## 3. `ERR_PNPM_LOCKFILE_CONFIG_MISMATCH`

**Symptom**

```
ERR_PNPM_LOCKFILE_CONFIG_MISMATCH Cannot proceed with the frozen installation.
The current "overrides" configuration doesn't match the value found in the lockfile
```

**Cause**

`dashboard/package.json` overrides and the checked-in `pnpm-lock.yaml` disagree.
The host `pnpm install` tolerates it; `--frozen-lockfile` does not.

**Fix**

Keep the Dockerfile's original semantics:

```dockerfile
elif [ -f pnpm-lock.yaml ]; then corepack enable pnpm && pnpm i --registry="${NPM_REGISTRY}";
```

Do **not** "fix" this by adding `--frozen-lockfile` unless the lockfile is
regenerated and the overrides agreed on in the same change.

## 4. `COPY failed: file not found ... server/requirements.txt`

**Cause**

The server Dockerfile is context-sensitive: it copies `server/` and
`agent-loop/`, so the context must be the repository root.

**Fix**

```bash
# correct
docker build -f server/Dockerfile -t bokaclaw-server:test .

# wrong — context is server/
cd server && docker build .
```

`server/Makefile` targets also use the repo-root context (`-f Dockerfile ..`).

## 5. `ModuleNotFoundError: bokaclaw_agent` at container start

**Cause**

The server image installs `agent-loop/` in-process. Building from a context
that excludes `agent-loop/` (or an old cached image) produces a server that
cannot serve `POST /agent`.

**Fix**

- Build from the repo root.
- Check the Dockerfile order: `COPY agent-loop /opt/agent-loop` and
  `pip install /opt/agent-loop` must come before `COPY server/ .`.
- Verify with the smoke check in `SKILL.md`.

## 6. Dashboard starts but every API call goes to the wrong host

**Cause**

`NEXT_PUBLIC_*` values are placeholders baked at build time and replaced at
container start by `dashboard/entrypoint.sh`. If a new variable is not
declared as `ENV NAME=NAME` in the Dockerfile, it is never substituted.

**Fix**

Add the `ENV NAME=NAME` line to `dashboard/Dockerfile`, rebuild, and pass the
real value at `docker run`. Verify no placeholder survives:

```bash
docker exec bokaclaw-dashboard sh -c "grep -rl NEXT_PUBLIC_API_URL .next | head"
# expected: no output
```

## 7. Wrong CPU architecture

**Symptom**

Images built on an Apple Silicon host are `arm64`. They fail to start on an
amd64 cluster with `exec format error`.

**Fix**

Pin the platform explicitly when the target differs:

```bash
docker build --platform linux/amd64 ... -t bokaclaw-server:amd64 .
```

Use a distinct tag so a locally built arm64 image is not overwritten;
emulated builds are substantially slower.

## 8. Secrets in image history

`--build-arg` values are visible in `docker history`. Never pass
`POSTGRES_PASSWORD`, `JWT_SECRET`, `ADMIN_API_KEY`, or model keys as build
args. Inject them at runtime with `--env-file server/.env`, which is
gitignored and excluded from the build context by `.dockerignore`.

## 9. Server never answers because fastembed is downloading its model

**Symptom**

`/openapi.json` returns `000` for many minutes after a recreate, and
`docker logs bokaclaw-server` sits on a model download:

```
Downloading model.onnx: 40%|████      | 38.0M/94.8M [21:30<32:04, 30.1kB/s]
```

or on `cas-bridge.xethub.hf.co` at a similar rate; a hung connection shows no
progress at all.

**Why**

The memory backend builds the embedder during startup, so the API does not serve
until the ~95 MB ONNX model is in the cache. fastembed's default cache is
`/tmp/fastembed_cache`, which is container-local: every `RECREATE=1` starts
empty and downloads again. `hf-mirror.com` answers quickly, but it redirects
large files to Hugging Face's xet storage, whose speed from this machine
swings between a crawl and normal.

**Fix: keep the cache on the persistent volume**

`start-stack.sh` passes `-e FASTEMBED_CACHE_PATH=/app/history/fastembed`, and
`/app/history` is the `mem0-local-history` volume, so the model is downloaded
once and reused by every later container. Nothing else is needed: fastembed
honours the variable and the cached files are ~91 MB.

**Fallback: seed the cache from the host**

When the first download is still crawling, the host network is often much
faster than the Docker VM (~4 MB/s vs ~30 KB/s here). Download on the host,
then copy the hub cache into the volume:

```bash
# 1. Host download into a throwaway cache (XET is what makes it slow).
HF_HUB_DISABLE_XET=1 HF_ENDPOINT=https://hf-mirror.com python - <<'PY'
from fastembed import TextEmbedding
TextEmbedding(model_name="BAAI/bge-small-zh-v1.5", cache_dir="/tmp/fastembed-seed")
PY

# 2. The ONNX blob is named by its sha256; check that the bytes match the name.
find /tmp/fastembed-seed -path '*bge-small-zh-v1.5*' -name blobs -type d \
  -exec sh -c 'for b in "$1"/*; do test "$(shasum -a 256 "$b" | cut -d" " -f1)" = "$(basename "$b")" && echo "ok $b"; done' _ {} \;

# 3. Copy into the running container. Fastembed wants the hub layout as the
#    cache root, so copy the CONTENTS of the seed cache.
docker cp /tmp/fastembed-seed/. bokaclaw-server:/app/history/fastembed/

# 4. Drop partial downloads and restart so fastembed loads the cached model.
docker exec bokaclaw-server sh -c 'find /app/history/fastembed -name "*.incomplete" -delete'
docker restart bokaclaw-server
docker exec bokaclaw-server du -sh /app/history/fastembed     # ~91M
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/openapi.json   # 200, no "Downloading" in logs
```

Do not delete the `mem0-local-history` volume to "fix" this: it also holds the
legacy `history.db` used by the one-time Mem0 migration. A retry is often enough on its own, since the slow xet route is
intermittent; seeding is only worth the manual steps when a recreate keeps
stalling.
