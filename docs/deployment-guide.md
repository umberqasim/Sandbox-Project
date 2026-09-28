# Deployment Guide

## Requirements

- Docker Engine 24+ with Docker Compose v2 (WSL2 with Docker works; without systemd start the daemon
  with `sudo dockerd > /tmp/docker.log 2>&1 &` in every new terminal)
- 4 GB RAM minimum (Flutter builds need more and are disabled by default)
- Outbound internet access for the worker (git clone, image pulls, package installs)

## Local / demo deployment (Docker Compose)

```bash
cp .env.example .env        # set API_KEY: openssl rand -hex 16
docker compose up --build
```

| Service | Port | Notes |
|---|---|---|
| frontend | 5173 | Vite dev server |
| backend | 8000 | FastAPI |
| db | 127.0.0.1:5432 | bound to localhost only |
| redis | internal | broker and result backend |
| worker | - | Celery, concurrency controlled by `WORKER_CONCURRENCY` (default 1), mounts the Docker socket |

Compose runs one worker with one evaluation slot by default to fit a 4 GB host. Set `WORKER_CONCURRENCY=2` in `.env` for a larger machine. `docker compose up --scale worker=N` is **not** supported (the worker has a fixed `container_name` and a fixed `SANDBOX_WORKER_ID`); horizontal scaling exists only in the Kubernetes reference manifests. Each Dockerfile build step is also capped at 1024 MB by default (MAX_BUILD_MEMORY_MB).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `API_KEY` | required | shared secret for `X-API-Key` |
| `CORS_ORIGINS` | `*` | comma-separated dashboard origins |
| `ALLOWED_GIT_HOSTS` | `github.com,gitlab.com,bitbucket.org` | hosts submissions may be cloned from |
| `ENABLED_PROJECT_TYPES` | `python,node,php` | add `flutter` on a machine with enough RAM |
| `MAX_EXECUTION_SECONDS` / `MAX_TEST_SECONDS` | 120 / 60 | run and test timeouts |
| MAX_MEMORY_MB / MAX_PIDS | 512 / 256 | runtime and test-container limits |
| MAX_BUILD_MEMORY_MB | 1024 | memory cap for each Dockerfile build step; keep at 1024 MB on a 4 GB host |

Docker applies the memory cap to each build step. Some WSL/Linux kernels cannot enforce swap limits; Docker reports that limitation, while the memory cap still applies.

| `MAX_UPLOAD_MB` / `MAX_UNZIPPED_MB` / `MAX_ZIP_FILES` | 50 / 300 / 20000 | ZIP limits |
| `CLONE_TIMEOUT_SECONDS` | 120 | git clone timeout |
| `BUILD_NO_CACHE` | 1 | build images without Docker layer cache so build times are comparable (`0` = allow cache) |
| `API_STARTUP_ATTEMPTS` | 10 | 2-second attempts to wait for a web app to start |
| `WORKER_CONCURRENCY` | 1 | evaluations handled at a time; set to 2 on a larger host |

## Production hardening checklist

1. Build the frontend (`npm run build` in `frontend/`) and serve `dist/` from a static server behind HTTPS;
   the compose file runs the Vite development server.
2. Remove `--reload` and the source-code bind mounts from the backend and worker.
3. Set `CORS_ORIGINS` to the dashboard origin and use a long random `API_KEY`; keep `.env` out of git and out of ZIP files you share.
4. Change the default Postgres credentials and do not publish port 5432.
5. Run workers on a dedicated host. The worker has access to the Docker daemon, and `docker build`
   executes submitted `RUN` steps with network access; use rootless Docker, gVisor or Sysbox for stronger isolation.
6. Kubernetes: `k8s/` contains reference manifests (`kubectl apply -k k8s/`), validated statically but never run on a
   cluster. They need Docker-based nodes labelled `sandbox-docker=true`, a ReadWriteMany volume for ZIP uploads and
   locally built images; see `k8s/README.md`.

## Verification

```bash
curl http://localhost:8000/health
python scripts/scale_test.py --count 10 --api-key "$API_KEY"
cd backend && python -m pytest -q tests
```
