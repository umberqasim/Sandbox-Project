# Enterprise AI Engineering Sandbox (Ezitech AI-022)

An evaluation platform that takes an internship submission (GitHub/GitLab repository, ZIP upload or
pre-built Docker image), runs it inside an isolated container, tests it, analyses its engineering
quality and produces a scored, structured report. A React dashboard provides submission, results,
leaderboard and platform metrics.

## Features

| Area | What it does |
|---|---|
| Submission | GitHub / GitLab URL (https, allow-listed hosts), ZIP upload, Docker image |
| Sandbox | Clone/extract, image build, dependency install, launch, test and cleanup for supported types. Runtime/test containers are limited to 512 MB; each Dockerfile build step defaults to a 1024 MB memory cap (MAX_BUILD_MEMORY_MB) |
| Project types | Python, Node.js, PHP/Laravel (Flutter available on machines with enough RAM) |
| Static analysis | Structure, code quality (flake8 / `node --check` / `php -l`), security (bandit + secret/dangerous-call scan), API quality, architecture (folders, file size, radon complexity), database config, authentication, error handling, security configuration, environment config, documentation |
| Dynamic analysis | Test suite execution (pytest / npm test / phpunit), API health probe with listening-port discovery, root-URL smoke check, 15-request load check |
| Scores | Feature completion, code quality, architecture, security, API quality, deployment readiness, engineering maturity (+ per-check scores) |
| Feedback | Strengths, weaknesses, missing requirements, security risks, performance and refactoring suggestions, improvement roadmap (rule-based) |
| AI review (optional) | A short mentor-style review written by a language model on Groq from the evaluation results. Advisory only: it never changes a score. Off unless `GROQ_API_KEY` is set |
| Reporting | JSON result, Markdown report download, MLflow run per evaluation |
| Leaderboard | Highest engineering score, fastest build, best architecture, best API design, best documentation, best performance |
| Operations | Celery + Redis queue (one worker slot by default; configurable with `WORKER_CONCURRENCY`), `/metrics`, JSON logs, re-evaluation, scale-test script, Kubernetes reference manifests |

Documentation: [architecture & workflow](docs/architecture.md) · [database schema](docs/database-schema.md) ·
[API reference](docs/api.md) · [deployment guide](docs/deployment-guide.md) ·
[technology justifications](docs/technical-justifications.md) · [technical presentation](docs/technical-presentation.pptx) · [live demo runbook](docs/live-demo-guide.md). Interactive API docs are served at
`http://localhost:8000/docs` (Swagger UI).

## Quick start

```bash
# WSL2 without systemd: start the Docker daemon in every new terminal session
sudo dockerd > /tmp/docker.log 2>&1 &

cp .env.example .env            # then set API_KEY to a fresh random value (openssl rand -hex 16)
docker compose up --build
```

- Dashboard: http://localhost:5173 (enter the `API_KEY` from `.env`)
- API: http://localhost:8000 (Swagger UI at `/docs`)

```bash
curl -X POST http://localhost:8000/evaluate \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"repo_url": "https://github.com/jatins/express-hello-world", "project_type": "node"}'
# -> {"task_id": "...", "status": "queued"}
```

### AI review (optional)

Set `GROQ_API_KEY` in `.env` and run `docker compose up -d worker`. Each evaluation of a source project then gets a
short written review (summary, strengths, concerns, next steps) shown in a clearly labelled "AI review" block and in the
Markdown report. Without a key nothing changes.

- **Advisory only.** It is stored separately (`scores.ai_review`) and never changes a score, a ranking or the rule-based feedback.
- **Privacy.** By default only evaluation results are sent (scores, finding descriptions, file paths) - no source code, no
  repository URL, no logs. `AI_REVIEW_SEND_CODE=true` also sends up to 5 short, redacted excerpts around dangerous-call
  findings; secrets are never sent. The data goes to Groq, a third party, so do not enable it for confidential submissions.
- **Not deterministic.** The same project can get differently worded reviews; each evaluation keeps the one it received, with the model name.
- **Never blocks an evaluation.** No network, a rate limit, a timeout or an unusable answer simply mean no review.
- Model: `openai/gpt-oss-120b` by default (`AI_REVIEW_MODEL` to change it). Groq shut down `llama-3.3-70b-versatile` for
  free and developer keys on 2026-08-16 and recommends `openai/gpt-oss-120b` instead ([deprecation details](https://console.groq.com/docs/deprecations)). If the review never appears, run
  `docker compose logs worker | grep "AI review"`: the log line says whether the key or the model is the problem.

Run the unit tests with Python 3.11 (same runtime as the backend image; no Docker, Postgres or Redis service is required):

```bash
cd backend
python3.11 -m pip install -r requirements-dev.txt
python3.11 -m pytest -q tests
```

## Project structure

```
Sandbox-Project/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── Dockerfile, requirements.txt, requirements-dev.txt
│   ├── app/
│   │   ├── main.py              # FastAPI routes, leaderboard, metrics
│   │   ├── tasks.py             # Celery tasks (crash-safe persistence)
│   │   ├── sandbox_engine.py    # clone/extract -> build -> run -> score -> cleanup
│   │   ├── analysis.py          # static analysis
│   │   ├── testing_engine.py    # test execution, API health, smoke and load checks
│   │   ├── feedback_engine.py   # engineering maturity + feedback synthesis
│   │   ├── report_generator.py  # Markdown report
│   │   ├── validation.py        # URL / image reference validation
│   │   ├── github_api.py, mlflow_tracking.py, database.py, schemas.py, auth.py, ...
│   └── tests/                   # unit tests
├── frontend/                    # React + Vite dashboard
├── k8s/                         # Kubernetes reference manifests (not run on a live cluster)
├── scripts/scale_test.py        # concurrent-submission load test
└── docs/
```

## Security model

- Submission build steps and runtime execute in Docker, not as host processes. Runtime/test containers receive memory,
  CPU and PID limits, dropped capabilities and no-new-privileges; build steps receive a 1024 MB memory cap
  (MAX_BUILD_MEMORY_MB). API-style projects run on an internal network; other runtime containers have networking disabled.
- Repository URLs are restricted to `https://` and an allow-list of hosts (`ALLOWED_GIT_HOSTS`),
  which blocks `file://`, internal hosts and git command-execution transports. Clones have a timeout.
- ZIP uploads are size-limited, checked for path traversal, and limited in unpacked size and file count.
- Only the **worker** mounts the Docker socket; the public API container does not.
- Portal callbacks (`callback_url`) are https-only, refuse private/internal addresses (checked when queueing and again by
  name resolution before every attempt), never follow redirects and can be signed (`CALLBACK_SECRET`) and restricted
  to known hosts (`CALLBACK_ALLOWED_HOSTS`).
- API access requires `X-API-Key` (constant-time comparison). CORS is header-based, without credentials;
  set `CORS_ORIGINS` to lock it to the dashboard origin in production.

**Known limitation:** the worker controls the host Docker daemon (needed to launch sibling containers), and Docker
build steps execute submitted RUN instructions with network access. Build memory is capped, but builds do not have
the same CPU/PID/network restrictions as runtime containers. For production, use a dedicated VM or rootless Docker,
gVisor or Sysbox.

## Reliability

- Tasks are acknowledged **after** they finish. If a worker is killed mid-evaluation, the submission is recorded as
  *failed - interrupted* (use **Re-evaluate**) as soon as the worker starts again, instead of vanishing.
- Every evaluation has a time limit (`EVAL_SOFT_TIME_LIMIT` 1500 s, `EVAL_HARD_TIME_LIMIT` 1800 s).
- Containers and images started by an evaluation are labelled; on start-up each worker removes what a killed run left
  behind. `docker compose` services are never touched.

## Verification

Verified 2026-09-28: backend suite **277 passed, 8 skipped** in a Python 3.11 container; the Vite production build succeeded in a temporary Node 20 container; local API health returned ok.

## Preparing a submission

Keep .env.example with placeholders and exclude the local .env file, which may contain the API key. When creating a source archive, also leave out node_modules, __pycache__, old ZIP snapshots (current3.zip, project_full.zip), patch helpers (apply_* and update-*.patch) and editor backup files (*.bak, *.v2bak). These are not application or EEF deliverables.

## Coverage of the case study

| Requirement | Status |
|---|---|
| Submission: GitHub, ZIP, Docker image, GitLab | Done |
| Sandbox: clone, build, install, launch, test, destroy, configure environment | Implemented for supported types. Builds have a configurable 1024 MB memory cap; env.example is used for Python/Node and Laravel gets a throw-away key. Build steps still need network access |
| Validation: structure, API availability, security configuration, error handling | Done (static heuristics + live health probe) |
| Validation: database connectivity, authentication flow | Static detection only; the platform does not connect to a database or log in |
| Testing: unit tests, UI smoke, performance | Unit tests run when a test folder exists; UI smoke is an HTTP check (no browser); performance is a 15-request burst |
| Testing: API tests (Newman), integration and database tests, Playwright | Not implemented (integration/database/UI test files are only detected). Node projects without scripts.test are reported as not run, not passed |
| Analysis scores and feedback engine | Done, rule-based. An optional advisory LLM review (Groq) is available when `GROQ_API_KEY` is set; scores never depend on it |
| Leaderboard (6 categories) | Done |
| Architecture components (orchestration, sandbox, evaluation, static/dynamic pipelines, test runner, report generator, REST API, monitoring) | Done |
| Bonus: parallel evaluation, one-click re-evaluation | One worker slot by default for 4 GB hosts; set `WORKER_CONCURRENCY=2` for parallel evaluations on a larger host. Re-evaluation is available from the dashboard |
| Bonus: auto-scaling workers | Kubernetes reference manifests only (StatefulSet + HPA); statically validated but not run on a live cluster. Docker Compose does not autoscale |
| Bonus: public scorecard | Done (`GET /public/scorecard`, unauthenticated, top 5 per category; `/public` dashboard page needs no API key) |
| Bonus: resource usage monitoring | Partial: CPU/memory snapshot after the load check is shown in the report/dashboard; continuous monitoring is not implemented |
| Bonus: plagiarism detection | Done (exact + structural fingerprints of tokenised source, so verbatim AND renamed copies are found; file-level matches for partly copied projects; common starter code ignored automatically once enough submissions exist; compares only against this platform's earlier submissions of the same project type; mentor-facing note/flag, never changes a score - see `docs/technical-justifications.md`) |
| Integration with the Ezitech Internship Portal | REST submission endpoints and an optional signed callback are implemented; real portal connection remains unverified (see API documentation) |
| Deliverables: architecture diagram, workflow, API docs, schema, deployment guide | In `docs/` |
| Deliverables: technical presentation and demo guide | [8-slide technical presentation](docs/technical-presentation.pptx) and [live demo runbook](docs/live-demo-guide.md) prepared; the actual demonstration still needs to be presented |
