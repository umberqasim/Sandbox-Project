# Enterprise AI Engineering Sandbox

**Ezitech Industry AI Case Study - AI-022**

An AI-assisted evaluation platform for internship projects submitted as GitHub or GitLab repositories, ZIP archives, or Docker images. It builds and runs supported projects in Docker, performs engineering checks, and generates scored reports.

The React dashboard supports submissions, evaluation reports, a public scorecard, leaderboards, and platform metrics. Evaluation scores and feedback are rule-based. An optional Groq review adds written advice but never changes scores.

## System Architecture

~~~mermaid
flowchart LR
    U[Submitter / Mentor] --> FE[React Dashboard]
    FE --> API[FastAPI REST API]
    API --> DB[(PostgreSQL)]
    API --> Q[Redis Queue]
    Q --> W[Celery Worker]
    W --> SB[Docker Sandbox]
    SB --> AN[Static Analysis]
    SB --> TE[Automated Tests]
    AN --> EV[Scoring and Feedback]
    TE --> EV
    EV --> DB
    EV --> ML[MLflow]
    EV --> R[JSON and Markdown Reports]
    AI[Optional Groq Review] -. advisory only .-> R
~~~

## The Problem This Solves

| Manual evaluation | Engineering Sandbox |
|---|---|
| Mentors configure projects one by one | Repeatable automated evaluation workflow |
| Setup failures delay reviews | Supported projects build and run in containers |
| Scores and feedback vary by reviewer | Consistent rule-based checks and scoring |
| Writing findings takes time | Reports summarize scores, risks, and next steps |
| Results are difficult to compare | Leaderboards and public scorecard |

## Features

- **Submissions:** GitHub/GitLab HTTPS repositories, ZIP uploads, and Docker images.
- **Sandbox lifecycle:** Clone or extract, build, install dependencies, configure, launch, test, and clean up.
- **Project types:** Python, Node.js, PHP/Laravel; Flutter where host resources allow.
- **Static analysis:** Structure, code quality, security patterns, API quality, architecture, database configuration, authentication indicators, error handling, environment configuration, and documentation.
- **Dynamic checks:** Existing test suites, API health probe with port discovery, root URL HTTP smoke check, and 15-request load check.
- **Scores:** Feature completion, code quality, architecture, security, API quality, deployment readiness, and engineering maturity, with per-check results.
- **Feedback:** Strengths, weaknesses, missing requirements, risks, performance and refactoring suggestions, and an improvement roadmap.
- **Optional AI review:** Groq-generated mentor-style review of findings; advisory only.
- **Reports and tracking:** JSON results, Markdown downloads, MLflow run per evaluation.
- **Leaderboard:** Highest engineering score, fastest build, best architecture, API design, documentation, and performance.
- **Operations:** Celery/Redis queue, re-evaluation, JSON logs, Prometheus metrics, scale-test script, Kubernetes reference manifests.
- **Resource-conscious defaults:** One worker slot; runtime/test containers capped at 512 MB; build steps default to configurable 1024 MB memory cap.

## Tech Stack

**Backend:** Python 3.11, FastAPI, Pydantic, Celery, Redis, PostgreSQL, Docker SDK  
**Analysis:** pytest, flake8, Bandit, Radon, node --check, php -l, supported project test commands  
**Frontend:** React, Vite  
**Infrastructure:** Docker Compose, Kubernetes reference manifests, Prometheus metrics  
**Evaluation:** Rule-based scoring and feedback, optional Groq review, MLflow tracking

## Getting Started

### Prerequisites
- Docker Engine with Docker Compose v2
- 4 GB RAM for the default single-worker setup; individual submitted builds may need more.
- WSL2 without systemd may require starting Docker in each new terminal.

### Start the platform

~~~bash
# WSL2 without systemd
sudo dockerd > /tmp/docker.log 2>&1 &

# From the project root
cp .env.example .env
# Set API_KEY to a fresh random value, e.g. openssl rand -hex 16
docker compose up --build
~~~

- Dashboard: http://localhost:5173
- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs

Enter the API_KEY from .env in the dashboard.

### Submit an evaluation

~~~bash
curl -X POST http://localhost:8000/evaluate \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"https://github.com/jatins/express-hello-world","project_type":"node"}'
~~~

The response includes a queued task_id. Follow it in the dashboard or task API.

### Run backend tests

Python 3.11 tests do not require Docker, PostgreSQL, or Redis:

~~~bash
cd backend
python3.11 -m pip install -r requirements-dev.txt
python3.11 -m pytest -q tests
~~~

## Optional AI Review

Set GROQ_API_KEY in .env and start or recreate the worker. Default model: openai/gpt-oss-120b; AI_REVIEW_MODEL can select another supported model.

- **Advisory:** Stored separately in scores.ai_review; does not affect scores, ranking, or rule-based feedback.
- **Privacy:** By default only scores, finding descriptions, and file paths are sent. No source code, repository URL, or logs.
- **Source excerpts:** AI_REVIEW_SEND_CODE=true sends up to five short, redacted excerpts around dangerous-call findings. Secrets are not sent. Avoid this option for confidential submissions.
- **Best effort:** Network errors, rate limits, timeouts, or unusable responses do not block evaluation.
- **Non-deterministic wording:** Each review records its model; wording can vary.
- Groq retired llama-3.3-70b-versatile for free and developer keys on 2026-08-16 and recommends openai/gpt-oss-120b ([details](https://console.groq.com/docs/deprecations)). Diagnose missing reviews with docker compose logs worker.

## API Overview

| Endpoint | Purpose |
|---|---|
| POST /evaluate | Queue repository evaluation |
| POST /evaluate/zip | Submit ZIP |
| POST /evaluate/docker | Submit Docker image |
| Task endpoints | Check status and retrieve results |
| Report endpoints | Retrieve JSON or Markdown report |
| GET /public/scorecard | Public top five per category |
| GET /metrics | Prometheus-compatible metrics |
| GET /docs | Interactive Swagger documentation |

See [API reference](docs/api.md) for schemas and details.

## Project Structure

~~~text
Sandbox-Project/
|-- docker-compose.yml, .env.example
|-- backend/
|   |-- Dockerfile, requirements*.txt
|   |-- app/
|   |   |-- main.py              # API, leaderboard, metrics
|   |   |-- tasks.py             # Celery tasks and crash-safe persistence
|   |   |-- sandbox_engine.py    # clone, build, run, score, cleanup
|   |   |-- analysis.py          # static analysis
|   |   |-- testing_engine.py    # test, health, smoke, load checks
|   |   |-- feedback_engine.py   # maturity and feedback
|   |   |-- report_generator.py  # Markdown reports
|   |   \-- validation.py, github_api.py, mlflow_tracking.py, database.py, ...
|   \-- tests/
|-- frontend/                    # React + Vite
|-- k8s/                         # reference manifests
|-- scripts/scale_test.py
\-- docs/
~~~

## Security and Reliability

- Submission build and runtime execute in Docker. Runtime/test containers have memory, CPU, PID limits, dropped capabilities, and no-new-privileges. Build steps default to a configurable 1024 MB memory cap.
- API-style projects use an internal network; other runtime containers have networking disabled. Builds require network access.
- Repository URLs require HTTPS and an allowed host (ALLOWED_GIT_HOSTS); clones time out; file://, internal hosts, and Git command-execution transports are blocked.
- ZIP upload size, unpacked size, file count, and path traversal are checked.
- Only the worker mounts the Docker socket; the public API does not.
- Portal callback URLs require HTTPS and reject private/internal addresses; addresses are rechecked, redirects are not followed, callbacks may be signed (CALLBACK_SECRET) and host-restricted (CALLBACK_ALLOWED_HOSTS).
- API calls require X-API-Key with constant-time comparison. CORS has no credentials; set CORS_ORIGINS for production.
- Tasks are acknowledged after completion. Interrupted evaluations are recorded and can be re-evaluated; the worker cleans up leftovers on startup.
- Time limits default to 1500 seconds soft and 1800 seconds hard (EVAL_SOFT_TIME_LIMIT, EVAL_HARD_TIME_LIMIT).

**Deployment limitation:** The worker controls the host Docker daemon to start sibling containers. Docker build steps execute submitted RUN instructions with network access and do not have runtime CPU/PID/network restrictions. Production should use a dedicated VM or consider rootless Docker, gVisor, or Sysbox.

## Verification

Verified 2026-09-28:
- Backend tests: **277 passed, 8 skipped** in Python 3.11 container.
- Vite production build succeeded in temporary Node 20 container.
- API health endpoint returned ok.

## EEF Requirement Coverage

| Requirement | Status and limitation |
|---|---|
| GitHub, GitLab, ZIP, Docker submissions | Implemented |
| Clone, build, install, configure, launch, test, destroy | Implemented for supported types; build memory defaults to 1024 MB, needs network, and may need more RAM |
| Structure, API availability, security configuration, error handling | Static heuristics and live health probe |
| Database connectivity and authentication flow | Static detection only; no database connection or login |
| Unit tests, UI smoke, performance | Existing unit tests run when present; UI smoke is HTTP only, no browser; performance is a 15-request burst |
| Newman API, integration/database tests, Playwright | Not implemented; presence may be detected but suites are not run. Node projects without scripts.test are reported as not run |
| Scores and feedback | Rule-based; optional Groq review does not affect scores |
| Six leaderboard categories | Implemented |
| Orchestration, sandbox, analysis pipelines, test runner, reports, REST API, monitoring | Implemented |
| Parallel evaluation and re-evaluation | Re-evaluation available; one worker by default for 4 GB hosts; WORKER_CONCURRENCY=2 is for larger hosts |
| Autoscaling workers | Kubernetes StatefulSet/HPA are references, not run on a live cluster; Compose does not autoscale |
| Public scorecard | GET /public/scorecard, top five per category; public dashboard page needs no API key |
| Resource monitoring | Partial snapshots after load check; no continuous monitoring |
| Plagiarism detection | Exact and structural token fingerprints compare earlier submissions of the same project type; partial file matches supported; common starter code ignored after enough submissions. Mentor-facing and does not affect scores. See [technical justifications](docs/technical-justifications.md) |
| Internship Portal integration | REST endpoints and optional signed callback implemented; real portal connection unverified |
| Architecture, workflow, API docs, schema, deployment guide | In docs/ |
| Presentation and live demo | [Technical presentation](docs/technical-presentation.pptx) and [demo runbook](docs/live-demo-guide.md) prepared; live presentation remains to be delivered |

## Documentation

- [Architecture and workflow](docs/architecture.md)
- [Database schema](docs/database-schema.md)
- [API reference](docs/api.md)
- [Deployment guide](docs/deployment-guide.md)
- [Technical justifications](docs/technical-justifications.md)
- [Live demo runbook](docs/live-demo-guide.md)
- [Swagger UI](http://localhost:8000/docs)

## Preparing a Source Submission

Keep .env.example with placeholders; exclude local .env containing the API key. Exclude node_modules, __pycache__, old ZIP snapshots (current3.zip, project_full.zip), patch helpers (apply_*, update-*.patch), and editor backups (*.bak, *.v2bak) from source archives.
