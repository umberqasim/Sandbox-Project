# Architecture and Sandbox Execution Workflow

## Component architecture

```mermaid
flowchart LR
    U[Mentor / Intern<br/>React dashboard] -->|X-API-Key, REST| API[FastAPI backend]
    P[Ezitech Internship Portal<br/>external caller] -.->|REST submissions| API
    API -.->|optional signed HTTPS callback| P
    API -->|enqueue| R[(Redis<br/>Celery broker + results)]
    R --> W[Celery worker<br/>concurrency 1 by default; configurable]
    API <--> DB[(PostgreSQL<br/>submissions)]
    W --> DB

    subgraph Worker pipeline
      direction TB
      SRC[Source acquisition<br/>git clone / ZIP extract / docker pull] --> STAT[Static analysis pipeline<br/>flake8, bandit, radon, pattern scans]
      STAT --> BUILD[Sandbox execution engine<br/>Docker SDK build]
      BUILD --> DYN[Dynamic analysis pipeline<br/>test runner, API health, smoke, load]
      DYN --> EVAL[AI evaluation engine<br/>scores + feedback engine]
      EVAL --> REP[Report generator<br/>Markdown / JSON]
    end

    W --> SRC
    BUILD --> DOCKER[(Host Docker daemon<br/>via docker.sock)]
    DYN --> DOCKER
    DOCKER --> SB[Sandbox containers<br/>limits, no caps, isolated network]
    W --> GH[GitHub API<br/>repo metadata]
    W --> ML[MLflow<br/>metrics per run]
    API --> MON[Monitoring<br/>JSON logs, /metrics]
```

## Sandbox execution workflow

```mermaid
flowchart TD
    A[Submission received] --> V{Validate input<br/>https + allowed host, image ref, ZIP size}
    V -- invalid --> X[422 / 400 to client]
    V -- ok --> Q[Queue Celery task, return task_id]
    Q --> S[Acquire source<br/>shallow clone with timeout / safe ZIP extract]
    S -- fails --> F[Record status = failed with error]
    S --> T[Detect project type<br/>correct the declared type if the repo looks different]
    T --> ST[Static analysis<br/>12 checks, no submitted code executed]
    ST --> B[Build Docker image<br/>own Dockerfile or generated template]
    B -- build error --> BE[Record build_error<br/>static scores still kept]
    B --> UT{Test folder or<br/>test files found?}
    UT -- yes --> RT[Run tests in network-less container]
    UT -- no --> WEB
    RT --> WEB{Web framework<br/>or routes detected?}
    WEB -- yes --> H[Start on internal network<br/>discover listening port, probe health,<br/>smoke check, 15-request load check]
    WEB -- no --> RUN[Run script: no network, read-only FS,<br/>512 MB, 0.5 CPU, timeout]
    H --> SC[Compute scores<br/>feature completion, deployment readiness, maturity]
    RUN --> SC
    SC --> FB[Generate feedback and roadmap]
    FB --> PER[Persist to PostgreSQL, log to MLflow]
    PER --> CL[Cleanup: stop and remove container,<br/>remove image, delete workspace]
```

Cleanup runs in a `finally` block, so the environment is destroyed even when a step fails. If the
evaluation engine itself crashes, the task still records a `failed` submission with the error.

## Sandbox isolation

| Control | Value |
|---|---|
| Memory | 512 MB, swap disabled |
| CPU | 0.5 CPU |
| Processes | 256 PIDs (blocks fork bombs) |
| Capabilities | all dropped, `no-new-privileges` |
| Network (web apps) | internal Docker network without internet, reachable only from the worker |
| Network (scripts, tests) | disabled (tests get network once only to install `pytest`, flagged in the result) |
| Filesystem | read-only for script runs, `/tmp` as tmpfs |
| Time | 120 s execution, 60 s tests |

## Scoring

Each category is a 0-100 score, or N/A when it does not apply (for example authentication for a
non-web script). **Engineering maturity** is the average of the applicable component scores. When fewer
than 5 components are available (Docker-image submissions, which have no source), the value is flagged
`partial` and excluded from ranking.
