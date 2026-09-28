## Unreleased

- Cap each Dockerfile build step at 1024 MB memory by default (MAX_BUILD_MEMORY_MB) to reduce OOM risk on 4 GB hosts.
- Report Node projects with no scripts.test as tests not run instead of a passing test result.
- Correct the Compose worker concurrency note in the Kubernetes reference guide.

# Changelog

## 0.10.10

### Changed
- **Plagiarism detection upgraded from one exact-similarity percentage to three real-world techniques.**
  - *Structural fingerprint.* Alongside the exact fingerprint, every file also gets one where every name
    (variable, function, class, module, framework call) becomes `ID` and numbers become `NUM`, keeping only
    keywords and punctuation. A copy whose identifiers were renamed scores ~5% exact but 70-100% structural
    and is now flagged. Reordered functions still match (windows are local to a function).
  - *File-level matching.* Each file is fingerprinted separately, so the report says which of the submission's
    files sit inside which of the other project's files. One copied file inside an otherwise original, bigger
    project (project-level similarity ~7%) is found at 100% and flagged. Files under 60 windows (a few lines)
    are ignored.
  - *Automatic boilerplate removal.* A window that appears in at least 3 and at least 30% of the earlier
    submissions being compared (starter template, framework scaffold) is dropped from both sides before
    comparing, once at least 5 earlier submissions exist. The result reports how much of the project that was.
    A real copy is still caught, because its own code is not common.
- Thresholds were calibrated on real code, not guessed: unrelated stdlib packages reach at most 12.6%
  structural similarity; two genuinely different CRUD apps 20% (Express) and 10% (Flask). Note at 35% /
  flag at 65% structural, 25% / 50% exact, 60% / 85% for a file.
- Fingerprints are now stored per file (`{"version": 2, "files": {...}}`); earlier submissions stored as
  `{"shingles": [...]}` are still compared, on exact windows only.
- `tests/test_round14_plagiarism_detection.py` rewritten: 25 tests including renamed copy, reordered
  functions, single copied file, starter-code ignore-and-still-catch-copies, legacy fingerprints.
- Still documented limits: no cross-language detection, no GitHub/web search, heavily rewritten logic is not
  found, and with fewer than 5 earlier submissions common code cannot be told apart from copied code.

## 0.10.9

### Added
- **Bonus challenge: AI Plagiarism Detection.** New `backend/app/plagiarism.py`. Each evaluated submission's
  application source (test files, vendored and build folders excluded, via the same `analysis._source_files`
  used elsewhere) is stripped of comments and string-literal contents, tokenised, and turned into a set of
  hashed 5-token shingles. That fingerprint is compared by Jaccard similarity against the stored fingerprints
  of earlier submissions of the same project type (newest 200) and kept on the submission's own scores as the
  internal `_plagiarism_fingerprint`. Matches at or above 25% are shown as a "Note", at or above 50% as
  "FLAGGED", in the dashboard and the Markdown report. It never changes any score, ranking or feedback; a
  Docker image, an empty repo or a first-of-its-kind submission is simply not checked.
- The file set covers `.py .js .jsx .ts .tsx .php .java .go .rb .dart .c .cpp .cs .vue`, wider than
  `analysis.CODE_EXTENSIONS`, so React and Flutter submissions are fingerprinted too.
- A submission's own earlier runs are excluded from its comparison (by repo URL, and `upload:<filename>` for
  ZIPs - `run_evaluation_from_zip` now receives the original filename), so re-evaluating or re-uploading the
  same project is not flagged as plagiarism of itself.
- Comparison uses the type the submission is stored under (the declared type), not the detected one. Before this,
  a project declared 'python' but detected as 'node' searched the 'node' rows and never found its peers.
- Limitation (documented): it is a code-structure heuristic, not rename-invariant and not evidence of
  academic dishonesty; shared starter code can trigger it.
- Covered by `backend/tests/test_round14_plagiarism_detection.py`.

## 0.10.8

### Added
- **Bonus challenge: Resource Usage Monitoring.** Right after the load test, the worker takes a one-shot
  CPU/memory snapshot of the already-running sandbox container via Docker's stats API
  (`testing_engine._capture_resource_usage`). Memory is reported against `MAX_MEMORY_MB` (the sandbox's
  own hardening limit) with page cache subtracted, matching what `docker stats` shows; CPU % follows
  Docker's own delta formula. Never raises and never changes any score - purely additional information,
  shown in the Markdown report ("Resource usage (snapshot after the load check)") and the dashboard's
  Dynamic Testing block. Covered by `backend/tests/test_round13_resource_monitoring.py` (cgroup v1/v2 cache
  handling, missing stats fields, a divide-by-zero guard on CPU%, a `.stats()` call that raises, and the
  full wiring into `check_api_health`).

## 0.10.7

### Added
- **Bonus challenge: Public Engineering Scorecard.** A new `GET /public/scorecard` endpoint - deliberately
  unauthenticated, unlike every other endpoint - returns the same ranking logic as `/leaderboard` ("latest"
  mode only), capped at the top 5 rows per category (`PUBLIC_SCORECARD_LIMIT`). The authenticated
  `/leaderboard` logic was extracted into a shared `_build_leaderboard()` helper so the two can never drift
  apart or show different data. On the dashboard, a new `/public` page (no API key prompt) renders it with
  the same layout as the existing Leaderboard tab, so the link can be shared with other interns, recruiters
  or mentors without handing out the platform's API key. Covered by
  `backend/tests/test_round12_public_scorecard.py` (no-auth access, ranking parity with `/leaderboard`, the
  5-row cap holding even when more data exists, and that no field is exposed beyond what `/leaderboard`
  already returns).

## 0.10.6

### Added
- **Runtime Auth/DB evidence surfaced on the dashboard.** The heavy auth/DB probe evidence added in 0.10.5
  was already computed and stored (`scores.authentication_flow.runtime_evidence`,
  `scores.database_connectivity.runtime_evidence`) and already appeared in the Markdown report, but the
  React dashboard never rendered it. A new "Runtime Auth/DB Evidence" block now shows these lines
  (labelled "heavy probe - additive only") whenever the probe found something, and the same text is added
  to the Authentication Flow / Database Connectivity score-box tooltips. No backend or scoring change -
  purely making already-computed, already-additive evidence visible where mentors actually look.

## 0.10.5

### Added
- **Heavy (runtime) authentication/database verification.** On the already-running health-check
  container, the worker now also tries a handful of conventional paths: `/register`, `/signup`, etc.
  (POST, dummy body), `/login`, `/signin`, etc. (POST, deliberately wrong password), and `/me`,
  `/profile`, etc. (GET, no auth). This is additive evidence only:
  - A login that rejects bad credentials, or a protected route that answers 401/403 unauthenticated,
    raises `authentication_flow` to 100.
  - A register endpoint that answers normally (200/201/400/409/422) raises `database_connectivity`
    by 40 (capped at 100).
  - A register endpoint that fails with what looks like a real DB-driver connection error
    (`ECONNREFUSED` and similar) is recorded as `runtime_evidence` but never raises OR lowers the
    score - the sandbox provides no real database service, so this is expected for many fine
    submissions.
  - None of this can ever lower a score: a wrong path guess (a 404/405 everywhere) leaves the static
    result exactly as it was.
  - Shown in the Markdown report under "Runtime Auth/DB Evidence"; not yet on the dashboard UI.
- `SCORING_VERSION` is 6 (authentication_flow/database_connectivity can now be raised at runtime).

## 0.10.4

### Added
- **`.env.example` values are now injected into the sandbox container for Python and Node** (PHP/Laravel
  already had its own throw-away `.env` since 0.8.2). Values are passed at `docker run` time via Docker's
  `environment` parameter - not baked into the image - so it also works for submissions with their own
  Dockerfile. Applies to the test run, the API health check and the plain-execution container. Only
  variable names (never values) are shown in the report/dashboard as `env_injected`.
- `SCORING_VERSION` is 5: a submission that previously crashed for lack of, say, `DATABASE_URL` may now
  build and run.

## 0.10.4

### Added
- **"Configure Environment" for Python and Node** (the case study's Sandbox Environment step): if a submission ships
  `.env.example` but no real `.env` - the normal way to commit config to a public repo without secrets - it is now
  copied in as `.env` during the image build, the same way Laravel projects already got a generated `.env`. Apps that
  read config through python-dotenv or Node's `dotenv` package now have values to load instead of crashing on missing
  configuration. Only ever copies what the submission itself published; no values are invented or supplied by the
  platform. `docs/api.md`'s `environment_configuration` scoring is unchanged - this only affects whether the app can run.

## 0.10.3

### Fixed
- **`check_has_runnable_entrypoint` (0.10.2) matched `.listen(...)` inside comments**, so
  `expressjs/express` was still marked "failed": its `lib/application.js` documents usage with
  `*    http.createServer(app).listen(80);` inside a `/** */` JSDoc comment, not real code. Verified
  against the actual file from GitHub. `.listen(...)`/`.run(...)`/`uvicorn.run(...)` are now matched
  after stripping `/* */` and full-line `//`/`#` comments.

## 0.10.2

### Fixed
- **Feature completion gave 100/100 to any reachable app, even with no evidence of a real feature**
  (no tests, no detected route - a bare health probe getting any response was enough). It now needs a
  detected route (`api_quality.route_count`) for 100; a reachable app with no detected route scores 50
  (basis `api_health_no_routes_detected`).
- **A library submitted on its own (e.g. `expressjs/express`) was marked "failed"** because nothing
  answered its health check, even though it was never meant to run as a server. A new check
  (`check_has_runnable_entrypoint`: a Node `package.json` "start" script or a `.listen(...)` call, or a
  Python `.run(...)`/`uvicorn.run(...)`/`manage.py`, outside tests/examples) now decides this: if none is
  found, the submission is scored like any other non-web project (status `success`) with a note in the
  report and dashboard explaining why, instead of being marked failed for not serving HTTP. An app that
  does have an entry point and still doesn't answer is unaffected - it still fails.
- `SCORING_VERSION` is 4 (feature completion's scoring changed).

## 0.10.1

### Fixed
- **README was not found unless it was named exactly `README.md`.** Linux file names are case-sensitive, so
  `Readme.md` (expressjs/express) and `readme.md` were reported as "Missing README.md" and scored 0 for
  documentation. Structure, Documentation and Required Features now accept any letter case.
  `SCORING_VERSION` is 3 because these scores change.

## 0.10.0

### Added
- **Portal callback** (`backend/app/callback.py`). `POST /evaluate`, `POST /evaluate/upload` (form field) and
  `POST /evaluate/docker-image` accept an optional `callback_url`. When the evaluation ends - with any verdict, including
  `failed` and "interrupted by a worker restart" - the worker POSTs a small JSON summary (`event`, `submission_id`,
  `status`, build/execution flags, duration, `error`, the 0-100 score of each category, `result_path`, `report_path`).
  No logs and no source code are sent; the full record stays behind `GET /results/{id}`.
  - Safety: https only, no credentials in the URL, optional host allow-list (`CALLBACK_ALLOWED_HOSTS`), the host is resolved
    before every attempt and refused if any address is private/loopback/link-local (so the callback cannot be used to reach
    internal services), redirects are never followed. `CALLBACK_ALLOW_INSECURE=1` and `CALLBACK_ALLOW_PRIVATE=1` relax this
    for local testing only.
  - Signing: with `CALLBACK_SECRET` set, each call carries `X-Sandbox-Timestamp` and
    `X-Sandbox-Signature: sha256=HMAC-SHA256("<timestamp>.<body>")`; `X-Sandbox-Delivery` is identical for every retry.
  - Delivery: 3 attempts (`CALLBACK_MAX_ATTEMPTS`) with back-off for network errors, 5xx, 408, 425 and 429; other answers are
    final. Delivery never fails or changes an evaluation.
- Requests without `callback_url` queue exactly as before (the extra task argument is only passed when a URL is given).

### Known limits
- The callback URL is not stored in the database (adding a column would need a migration; the tables are created with
  `create_all`). It lives in the task message and, while the evaluation runs, in Redis. Consequently **Re-evaluate does not
  send a callback**, and a callback that could not be delivered is only logged (no retry queue).
- The address check and the connection are separate steps (DNS rebinding); use `CALLBACK_ALLOWED_HOSTS` in production.
- The FastAPI `version` string said `0.8.0`; it now says `0.10.0`.

## 0.9.0

### Added
- **Optional AI review** (`backend/app/ai_review.py`): a short mentor-style review (summary, strengths, concerns, next steps)
  written by a Groq-hosted model. Advisory only - stored as `scores.ai_review`, never changes a score, ranking or the
  rule-based feedback. Off unless `GROQ_API_KEY` is set; any failure means no review and an unchanged evaluation.
  By default only evaluation results are sent (no source code, repository URL or logs); `AI_REVIEW_SEND_CODE=true` adds up to
  five redacted excerpts around dangerous-call findings. Output is validated and sanitised (fixed JSON structure, length
  limits, no links or HTML). Shown in the dashboard with an "AI review - advisory" label and in the Markdown report.
  Default model `openai/gpt-oss-120b` (`AI_REVIEW_MODEL`); Groq shut down `llama-3.3-70b-versatile` for free/developer keys on 2026-08-16.
  Environment: `GROQ_API_KEY`, `AI_REVIEW_MODEL`, `AI_REVIEW_SEND_CODE`, `AI_REVIEW_TIMEOUT_SECONDS`, `AI_REVIEW_MAX_TOKENS`.

## 0.8.5

### Kubernetes reference manifests (statically validated with kubeconform --strict; still not run on a cluster)
- **`$(DB_PASSWORD)` was used before it was defined** in the backend and worker env lists, so Kubernetes passed the literal
  text `$(DB_PASSWORD)` in `DATABASE_URL`. `DB_PASSWORD` now comes first.
- **The worker HPA scaled on CPU but the containers had no `resources.requests`**, so it could never compute a utilisation.
  Requests and limits are set on every container.
- **`hostPath: /var/run/docker.sock` does not exist on containerd-only clusters.** The volume is now `type: Socket`, the worker
  is pinned to nodes labelled `sandbox-docker=true`, and the requirement is documented.
- **Uploaded ZIPs could not reach the worker** (no shared volume). Added a ReadWriteMany `sandbox-uploads-pvc`, mounted by the API and worker.
- **Images `sandbox-worker:latest` / `sandbox-*:latest` were never built by Compose** and `:latest` forces a pull. The worker uses
  the backend image with a different command, `imagePullPolicy: IfNotPresent` is set, and the build commands are documented.
- The worker is a **StatefulSet** with `SANDBOX_WORKER_ID` = pod name, so the 0.8.4 crash recovery can find a re-created pod's
  evaluations (with a Deployment every pod got a new random id).
- `kubectl apply -f k8s/` would have applied `secrets.example.yaml` with placeholder values; use `kubectl apply -k k8s/`
  (kustomization added). Postgres: `Recreate` strategy and `PGDATA` sub-folder for a ReadWriteOnce volume; Services are ClusterIP.
- Docs now say honestly that `docker compose up --scale worker=N` does not work and that CPU is a weak scaling signal for this
  workload (queue length is the right one).

### Fixed
- **"Build+run took 88s - consider trimming dependencies" was unfair.** The number was the whole evaluation
  (build, tests, start-up waits, load check) and builds run without Docker's cache, so small healthy projects got the
  advice. The suggestion now looks at the image build time only and appears only beyond 300 s (`SLOW_BUILD_SECONDS`).
- **The Metrics "success rate" mixed two different things.** Deliberately broken test submissions (a missing repository, an
  app bound to localhost) counted as platform failures. `/metrics` now also returns `completion_rate_percent`
  (evaluations that ended with a verdict - only internal errors, worker interruptions and time-limit stops count against
  it) and `platform_error_count`; `success_rate_percent` keeps its meaning (submissions that built and ran). The dashboard
  shows both, with labels that say which is which.

## 0.8.4

Worker reliability. Verified with a real Redis and a real Celery worker that was SIGKILLed mid-evaluation.

### Fixed
- **A worker restart used to lose the running evaluation.** The task was acknowledged before it ran, so a killed worker
  left no database row and the dashboard showed "STARTED" forever. Tasks are now acknowledged late; the evaluations a
  worker was running are remembered in Redis, and when the worker (or its re-created container) starts again they are
  recorded as *failed - interrupted, use Re-evaluate* and the Celery task is marked failed so the dashboard stops
  polling. The broker's later re-delivery of the same message is ignored, so nothing is recorded twice or re-run
  (a task that crashes its worker can no longer loop forever).
- **No time limit on an evaluation.** Celery now stops a run after `EVAL_SOFT_TIME_LIMIT` (default 1500 s; the engine's
  own cleanup still runs) and kills it after `EVAL_HARD_TIME_LIMIT` (1800 s). The result says the limit was reached.
- **Killed evaluations leaked images and containers** (a 3.6 GB `sandbox-run` image was found on disk). Everything the
  engine starts or builds is now labelled, and each worker start removes its own leftovers plus any managed resource
  older than `JANITOR_STALE_AFTER_SECONDS` (2400 s). Compose services and other workers' recent runs are never touched.

### Added
- `SANDBOX_WORKER_ID=sandbox_worker` in `docker-compose.yml`: a stable worker id, so recovery also works after
  `docker compose up -d` re-creates the container. Leave it unset when running several workers (the hostname is used).
- `backend/tests/test_round5_reliability.py` (17 tests).

### Known limits
- If a worker's id changes (a Kubernetes pod is replaced), its interrupted task is only reported when the broker
  re-delivers it after the visibility timeout (`CELERY_VISIBILITY_TIMEOUT`, default 3600 s).
- Docker images pulled for Docker-image submissions carry no label, so the janitor cannot recognise them if a run
  is killed before its own cleanup.

## 0.8.3

Found by running 0.8.2 on laravel/laravel and express-hello-world.

### Fixed
- **Laravel answered HTTP 500 on `/` inside the sandbox** (root check failed, load check 0% ok). The skeleton defaults to
  database sessions/cache and SQLite, but the sandbox has no database file and no migrations. The generated PHP image now
  switches to file sessions/cache, a SQLite file and runs `migrate` (image only, never the repository).
- **Three false negatives for PHP/Laravel**: `env('KEY')` was not recognised as environment-based secrets (Security
  Configuration 0), `->withExceptions(...)` was not recognised as error handling (Error Handling 0), and about 90 optional
  `env('X', default)` reads were reported as "not declared in .env.example" (Environment Config 58). Only reads without a
  fallback value count as required, and Laravel's `config/` files are ignored for that rule.
- Feedback no longer says "0% of burst requests succeeded - check error handling" when `/` itself returned a 5xx; it says
  the load check could not measure anything and why the root check failed.
- "API health: reachable (/health ...)" now shows the HTTP status, because "reachable" only means the app answered
  (express-hello-world has no `/health` route and answers 404).

## 0.8.2

Found by running 0.8.1 in Docker (express-hello-world, failed clone, leaderboard, laravel/framework report).

### Fixed
- **Feature evidence came from test files.** Routes, authentication, database, error-handling and environment-variable
  detection now ignore tests (laravel/framework "read" `FOO` and `SOMETHING_FROM_ENV` from its own tests). PHP test
  detection no longer matches names such as `contest.php`.
- **A 404 could win "Best Performance".** A library answering `/` with 404 in 3 ms ranked first. Only runs whose root check
  passed are ranked for latency.
- **Build time was not comparable** (same repo: 27 s, later 55 s, because of Docker layer cache). Images are now built
  with `nocache` (`BUILD_NO_CACHE=0` restores caching).
- **Failed-clone message exposed git's command line and the internal temp path.** It now says the repository was not
  found or is private; the raw git output stays in the logs.
- Dashboard status line said "done" for evaluations whose result was `failed`; it now shows the result status.
- **The "app listens on localhost only" diagnosis never appeared** (found with `flask-localhost.zip`). Docker's embedded
  DNS resolver (127.0.0.11) listens inside every container on a user-defined network and was counted as "the app's
  reachable port", so a localhost-bound Flask app was probed on the wrong port and got the generic "no response" message.
  The resolver is now ignored and every 127.x address counts as loopback.
- **PHP projects could never run their tests.** The generated PHP Dockerfile installed Composer with `--no-dev`, but PHPUnit
  is a dev dependency, so every Laravel project showed "Test suite: FAILED" and a low feature-completion score. Dev
  dependencies are now installed, and a throw-away `.env` with an `APP_KEY` is created inside the image (never in the
  repository) so Laravel can boot.

### Added
- Failure reasons are visible in the dashboard: the Results tab shows the error message and build/run logs; failed test
  runs show their output; an unreachable app shows what it printed and why port discovery failed; build errors say why
  (for example exit code 137 = out of memory) instead of only "Build failed". The Markdown report includes the test output tail.

## 0.8.1

Found by running the 0.8.0 build against real repositories (laravel/framework, a monorepo, a Docker image).

### Fixed
- **Monorepos were evaluated at the wrong level.** A repository with `backend/` and `frontend/` folders and only a
  docker-compose file at the root was judged as an empty Node project ("Missing package.json", generated Dockerfile
  exits immediately). The platform now evaluates the most likely server folder, counts the root README/.gitignore
  for it, and tells the user which folder was evaluated and which were not.
- **Test files inflated security findings.** `eval()`, `unserialize()` and dummy passwords in tests (and bandit's
  "assert used" in every pytest file) pushed laravel/framework's security score to 0. Test files are now skipped, and
  oversized test files no longer count as architecture problems.
- **"Missing requirements" was flooded** with one line per undeclared environment variable (200+ lines). It is now one line.
- **A test suite that hit the 60 s limit was reported as "failing".** It is now reported as timed out.
- Docker-image submissions no longer report "No automated tests found".
- Complexity suggestions showed an empty file name ("in  has high cyclomatic complexity").

## 0.8.0

### Fixed
- **Timestamps shown hours off in the dashboard.** The API returned UTC times without a timezone marker, so
  browsers displayed them as local time (5 hours early in Pakistan). All timestamps now carry `+00:00`.
- **Leaderboard showed stale scores.** It ranked each repository by its best score ever, so runs made with an
  older, more lenient scoring version outranked current results (76 on the leaderboard vs 50 in Results).
  It now uses each repository's latest successful run (`?mode=best` restores the old behaviour), treats
  `repo`, `repo/` and `repo.git` as one repository, and records a `scoring_version` on every evaluation.
- **Docker-image submissions displayed 100/100.** Their "maturity" was an average of a single check. It is now
  flagged `partial` ("health check only") and never ranked.
- **Tests were never run for Node projects that use `test/` or `__tests__/`**, and those projects lost the
  20 structure points for "tests". Test folders and loose test files are now detected; pytest exit code 5
  ("no tests collected") is no longer reported as a failing suite.
- **Apps on non-standard ports were reported unreachable.** The health probe now reads the container's
  listening sockets and `EXPOSE` list, sets `PORT=8000`, waits longer for slow starters, and explains
  "listens on localhost only" (e.g. Flask `app.run()`) instead of a generic failure.
- **Crashed evaluations vanished.** An unexpected engine error left no database row, so the submission was
  missing from Results and from the Metrics success rate. It is now stored as `failed` with the error.
  Uploaded ZIPs are deleted even when the task crashes.
- ZIPs made on macOS (`__MACOSX` folder) and ZIPs whose root folder contains a same-named subfolder
  (`app/app/...`) no longer break extraction.
- p95 latency used the wrong sample index.

### Security
- Repository URLs must be `https://` on an allow-listed host: blocks `file://`, local paths, internal hosts
  (SSRF) and the git `ext::` transport. Clones time out and never wait for a password.
- Docker image references are validated.
- ZIP uploads: limits on unpacked size (300 MB) and file count, path-traversal check.
- The public API container no longer mounts the Docker socket (Compose and Kubernetes).
- CORS no longer combines `*` with credentials; origins are configurable with `CORS_ORIGINS`.
- API key comparison is constant-time. Postgres port is bound to localhost.

### Added
- One-click re-evaluation (`POST /results/{id}/reevaluate` + dashboard button).
- Dashboard: test types, API health and build time in results; readable validation errors; API key checked
  before saving; metrics auto-refresh; accessible tab buttons; evaluation dates on the leaderboard.
- Unit tests (`backend/tests`, 37 tests, no Docker needed).
- Documentation: architecture and workflow diagrams, database schema, API reference, deployment guide,
  updated README with an honest case-study coverage table.
