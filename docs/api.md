# API Reference

Base URL `http://localhost:8000`. Interactive documentation: `/docs` (Swagger UI) and `/openapi.json`.
Every endpoint except `/health` requires the header `X-API-Key: <API_KEY>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check (no key needed) |
| POST | `/evaluate` | Queue a repository evaluation. Body: `{"repo_url": "https://github.com/user/repo", "project_type": "python\|node\|php", "callback_url": "https://portal.example.com/hook"}` (`callback_url` optional) |
| POST | `/evaluate/upload` | Queue a ZIP evaluation. Multipart: `file` (.zip), `project_type`, optional `callback_url` |
| POST | `/evaluate/docker-image` | Queue a Docker image health evaluation. Body: `{"image": "user/image:tag", "callback_url": "..."}` (`callback_url` optional) |
| GET | `/tasks/{task_id}` | Task state: `PENDING`, `STARTED`, `SUCCESS`, `FAILURE`; on success includes `submission_id` |
| GET | `/results?limit=20` | Recent evaluations (newest first, max 500) |
| GET | `/results/{submission_id}` | Full record including logs, scores and feedback |
| GET | `/results/{submission_id}/report` | Markdown report |
| POST | `/results/{submission_id}/reevaluate` | Queue a fresh run of the same repository or image (not available for ZIP uploads) |
| GET | `/leaderboard?mode=latest\|best` | Six rankings; `latest` (default) uses each repository's most recent successful run |
| GET | `/metrics` | Totals, status breakdown, `success_rate_percent` (built and ran), `completion_rate_percent` (evaluations that ended with a verdict) and `platform_error_count` (internal errors, worker interruptions, time-limit stops), average duration, queue depth |

Queued responses look like `{"task_id": "...", "status": "queued"}`. Poll `/tasks/{task_id}`, then fetch
`/results/{submission_id}`.

## Errors

| Code | Meaning |
|---|---|
| 400 | Unsupported project type, non-ZIP upload, invalid `mode` |
| 401 | Missing or wrong API key |
| 404 | Unknown submission |
| 409 | Re-evaluation of a ZIP upload (file no longer stored) |
| 413 | ZIP larger than `MAX_UPLOAD_MB` |
| 422 | Invalid repository URL, image reference or `callback_url` (message explains why) |

Failures that happen while an evaluation runs (clone failed, unsafe or oversized ZIP, build error, engine
crash) are stored as submissions with `status` `failed` or `build_error` and an `error` message.

## Portal callback

Pass `callback_url` when queueing an evaluation (`/evaluate`, `/evaluate/upload`, `/evaluate/docker-image`) and the
worker sends one `POST` to it when the evaluation has finished - with any verdict, including `failed` and
"interrupted by a worker restart". Without `callback_url` nothing changes. **Re-evaluate does not send a callback.**

Rules for the URL (checked when queueing; a bad URL is answered with 422 and nothing is queued): `https://` only,
no credentials in the URL, not `localhost` or a private/internal address, and - when `CALLBACK_ALLOWED_HOSTS` is set -
only those hosts. The host name is resolved again before every attempt and refused if any address is private, so a
name that points to an internal service is not called. Redirects are not followed.

Body (`application/json`, no logs, no source code):

```json
{
  "event": "evaluation.finished",
  "submission_id": "abc12345",
  "status": "success",
  "repo_url": "https://github.com/user/repo",
  "project_type": "node",
  "build_success": true,
  "execution_success": true,
  "duration_seconds": 92.4,
  "error": null,
  "scoring_version": 2,
  "engineering_maturity_partial": false,
  "scores": {"engineering_maturity": 41, "architecture": 15, "security": 100, "database_connectivity": 0, "...": null},
  "result_path": "/results/abc12345",
  "report_path": "/results/abc12345/report"
}
```

`repo_url` is the repository URL, `upload:<file name>` or `image:<reference>`. Each score is the 0-100 number, or `null`
when the category does not apply. Fetch the full record with `GET /results/{submission_id}` (needs the API key).

Headers: `X-Sandbox-Event`, `X-Sandbox-Delivery` (same value on every retry - use it to ignore duplicates),
`X-Sandbox-Timestamp`, and, when `CALLBACK_SECRET` is set, `X-Sandbox-Signature: sha256=<hex>`, the HMAC-SHA256 of
`"<timestamp>.<raw body>"` with that secret. Verify it on the portal side and reject old timestamps:

```python
import hashlib, hmac, time

def is_valid(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

Delivery: up to `CALLBACK_MAX_ATTEMPTS` (3) attempts with exponential back-off for network errors, HTTP 5xx, 408, 425
and 429; any other answer is final. Answer with any 2xx. A callback that cannot be delivered is only logged - fetch
`GET /results` if the portal missed one. For local testing only, `CALLBACK_ALLOW_INSECURE=1` allows `http://` and
`CALLBACK_ALLOW_PRIVATE=1` allows private addresses.
