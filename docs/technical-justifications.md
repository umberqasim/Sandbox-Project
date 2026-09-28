# Technology Choice Justifications

## SonarQube → bandit + radon + flake8/eslint/php -l

SonarQube requires running a separate server (its own database, web UI,
and background analysis workers) with a non-trivial setup and resource
footprint. On this project's development machine (4GB RAM), running
SonarQube alongside the existing Postgres/Redis/backend/worker/frontend
stack was not practical.

Instead, this platform uses lightweight, pip/apt-installable static
analysis tools that cover the same core ground:

- **bandit** - AST-based security analysis for Python (the same class
  of checks SonarQube runs: hardcoded secrets, SQL injection patterns,
  unsafe deserialization, debug-mode exposure, etc.)
- **radon** - cyclomatic complexity analysis for Python (SonarQube's
  "code smells" / complexity metrics, narrower scope but same signal)
- **flake8 / eslint-equivalent (node --check) / php -l** - code quality
  and syntax validation per language

This is a deliberate, resource-appropriate trade-off, not a missing
feature - the case study explicitly allows justified alternative
technologies.

## Playwright / Newman -> lightweight HTTP checks

Playwright needs a browser (300-500 MB RAM per run) and Newman needs a Postman collection per submission,
which interns do not provide. On a 4 GB development machine the platform therefore uses:

- a root-URL smoke check that reports status code and whether an HTML page is returned,
- a 15-request burst that measures average, p95 and maximum latency and the success rate.

Both run against the already-running sandbox container. Browser-based UI tests and collection-driven API
tests are the natural next step on a larger machine.

## Plagiarism detection -> fingerprint comparison, not a MOSS/JPlag service

Each submission's application code (comments and string contents stripped) is tokenised per file and
turned into two sets of hashed token windows: an *exact* set, and a *structural* set in which every name
becomes `ID` and every number `NUM`, so renaming variables, functions or classes does not hide a copy.
Submissions of the same project type are compared by Jaccard similarity of those sets, and file by file
(containment) so a single copied file inside a larger project is found. A window that already appears in
many earlier submissions is treated as starter/scaffold code and ignored (needs at least 5 earlier
submissions to judge). Only hashes are stored, never source code, and the result is a mentor-facing signal
that never changes a score.

Thresholds were calibrated on real code: unrelated stdlib packages stay below ~13% structural similarity
and two different CRUD apps below 20%, against a note level of 35% and a flag level of 65%.

Why not MOSS/JPlag/Dolos: they need an external service or a parser per language plus more memory than the
4 GB target machine and the offline worker image allow. What this does not do: cross-language detection,
searching GitHub or the web, or finding copies whose logic was genuinely rewritten. Shared starter code
below the boilerplate threshold can still register, which is why the result is worded as a heuristic.

## Celery + Redis, MLflow file store

Celery/Redis give the asynchronous, horizontally scalable queue the case study asks for. MLflow uses a
local file store instead of a tracking server, which keeps memory use low while still recording every
run's metrics for comparison.

## Optional AI review (Groq) - advisory only

The evaluation itself is rule-based (static analysis, test execution, live health probing). A rule engine is
reproducible and explainable, which is what a score needs, but its feedback is templated. An optional review layer
adds a short, plain-language mentor summary written by a language model.

**Why it cannot affect scores.** Scores and rankings decide a student's standing, so they must be reproducible and must not
depend on a model's mood or on text inside a submission. The review is produced after all scores are final, stored under
`scores.ai_review`, shown with an "advisory" label, and ignored by the leaderboard, metrics and maturity calculation.

**Privacy trade-off.** Using a hosted model means data leaves the platform. By default only evaluation results are sent:
score numbers, finding descriptions and file paths - no source code, repository URL or logs. Setting
`AI_REVIEW_SEND_CODE=true` adds at most five short excerpts (5 lines each, 400 characters) around "dangerous call" findings
after redacting secrets, tokens, e-mail addresses and credentials in URLs; secret findings never produce an excerpt.
File paths and finding text can still reveal what a project is about, so the feature is off unless a key is configured
and should not be used for confidential submissions.

**Untrusted input.** Everything sent comes from analysing a submitted project, so it can contain text aimed at the model
(for example a file named "ignore previous instructions"). Mitigations: the data is passed as JSON, the system prompt
declares it data and forbids following instructions inside it, the model may only return a fixed JSON structure, and the
answer is validated and sanitised (length limits, no links, no HTML, no control characters) before it is stored. The model
cannot change a score even if it is manipulated, which is the main protection.

**Non-determinism and reliability.** Model output varies between runs even at low temperature, so each evaluation stores
the review it got with the model name and time. A missing key, network error, rate limit, timeout or unusable answer
returns no review and the evaluation continues unchanged; the model call has its own timeout (30 s by default).

**Model choice.** Default `openai/gpt-oss-120b`, the replacement Groq recommends on its deprecations page.
`llama-3.3-70b-versatile` was shut down for free and developer keys on 2026-08-16, and `qwen/qwen3.6-27b` is itself being
replaced, which is why the model is an environment variable (`AI_REVIEW_MODEL`) and a failing model is reported in the
worker log (`AI review skipped: ... check AI_REVIEW_MODEL`) instead of being hidden.

**Verification.** The feature is covered by unit tests with a mocked HTTP layer (no real network calls); it has to be
confirmed once against the real Groq API with a real key.
