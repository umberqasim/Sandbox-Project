import os
import uuid
import logging
from datetime import timezone
from pathlib import Path

import redis
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, or_

from .schemas import EvaluationRequest, DockerImageRequest
from .callback import validate_callback_url
from .report_generator import generate_report
from .database import init_db, get_session, Submission
from .tasks import evaluate_task, evaluate_zip_task, evaluate_docker_image_task
from .auth import verify_api_key
from .logging_config import setup_logging
from .feedback_engine import MIN_COMPONENTS_FOR_FULL_SCORE

setup_logging()
logger = logging.getLogger("sandbox.api")

app = FastAPI(
    title="Ezitech AI-022 - Enterprise Engineering Sandbox",
    description="Auto-evaluation platform for internship project submissions",
    version="0.10.4",
)

# Authentication is an X-API-Key header (no cookies), so credentialed CORS is not needed.
# "*" keeps the dashboard working from any host/IP in development; set CORS_ORIGINS to a
# comma-separated allow-list (e.g. https://sandbox.example.com) for production.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)

UPLOAD_DIR = Path("/tmp/sandbox-runs/uploads")
REDIS_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", 50))

# Project types accepted by the API. Flutter is intentionally NOT enabled by
# default: its build needs several GB of RAM (this dev machine has 4GB).
# Enable on a bigger machine with ENABLED_PROJECT_TYPES=python,node,php,flutter
ENABLED_PROJECT_TYPES = {
    t.strip() for t in os.environ.get("ENABLED_PROJECT_TYPES", "python,node,php").split(",") if t.strip()
}


def _check_project_type(project_type: str) -> None:
    if project_type not in ENABLED_PROJECT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported project_type '{project_type}'. Enabled: {sorted(ENABLED_PROJECT_TYPES)}",
        )


def _iso(dt):
    """
    ISO-8601 in UTC *with* a timezone marker. The DB column is timezone-naive, so the old
    output ("2026-09-19T13:46:28") was parsed by the browser as LOCAL time and shown hours off
    (5h for Pakistan). Rows are stored in UTC, so tag them as UTC here.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@app.on_event("startup")
def on_startup():
    init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Application startup complete")


@app.get("/health")
def health():
    return {"status": "ok"}


# Failures caused by the platform itself, not by the submission: the engine crashed, the run was
# interrupted by a worker restart, or it hit the evaluation time limit (texts come from tasks.py).
# Everything else that "fails" - a repository that does not exist, a build error, an app that is not
# reachable - is a correct verdict, i.e. the platform did its job.
PLATFORM_ERROR_PREFIXES = ("Internal error:", "Evaluation was interrupted", "Evaluation stopped:")


@app.get("/metrics")
def metrics(_auth: bool = Depends(verify_api_key)):
    session = get_session()
    try:
        total = session.query(func.count(Submission.submission_id)).scalar() or 0

        status_counts = dict(
            session.query(Submission.status, func.count(Submission.submission_id))
            .group_by(Submission.status)
            .all()
        )

        avg_duration = session.query(func.avg(Submission.duration_seconds)).scalar()
        avg_success_duration = (
            session.query(func.avg(Submission.duration_seconds))
            .filter(Submission.status == "success")
            .scalar()
        )
        success_count = status_counts.get("success", 0)
        success_rate = round((success_count / total) * 100, 1) if total else 0

        platform_errors = (
            session.query(func.count(Submission.submission_id))
            .filter(
                Submission.status == "failed",
                or_(*[Submission.error.like(f"{prefix}%") for prefix in PLATFORM_ERROR_PREFIXES]),
            )
            .scalar()
            or 0
        )
        completion_rate = round(((total - platform_errors) / total) * 100, 1) if total else 0

        try:
            r = redis.from_url(REDIS_URL)
            queue_depth = r.llen("celery")
        except Exception:
            queue_depth = None

        return {
            "total_submissions": total,
            "status_breakdown": status_counts,
            # Submissions that built and ran (includes deliberately broken test submissions in the count).
            "success_rate_percent": success_rate,
            # Platform reliability: evaluations that ended with a verdict, whatever that verdict was.
            "completion_rate_percent": completion_rate,
            "platform_error_count": platform_errors,
            "avg_duration_seconds": round(avg_duration, 2) if avg_duration else None,
            "avg_success_duration_seconds": round(avg_success_duration, 2) if avg_success_duration else None,
            "queue_depth": queue_depth,
        }
    finally:
        session.close()


@app.post("/evaluate")
def evaluate(req: EvaluationRequest, _auth: bool = Depends(verify_api_key)):
    _check_project_type(req.project_type)
    # callback_url is only passed on when the caller gave one, so requests without it queue exactly as before.
    extra = (req.callback_url,) if req.callback_url else ()
    task = evaluate_task.delay(req.repo_url, req.project_type, *extra)
    logger.info("Evaluation request queued", extra={"repo_url": req.repo_url, "project_type": req.project_type})
    return {
        "task_id": task.id,
        "status": "queued",
        "message": "Evaluation queued. Poll GET /tasks/{task_id} for progress, or GET /results once complete.",
    }


@app.post("/evaluate/upload")
async def evaluate_upload(
    file: UploadFile = File(...),
    project_type: str = Form("python"),
    callback_url: str = Form(None),
    _auth: bool = Depends(verify_api_key),
):
    _check_project_type(project_type)
    if callback_url and callback_url.strip():
        try:  # checked before the upload is written to disk
            callback_url = validate_callback_url(callback_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    else:
        callback_url = None
    original_name = Path(file.filename or "").name  # never trust client-supplied paths
    if not original_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")

    save_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.zip"
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    too_large = False
    with save_path.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        save_path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail=f"ZIP too large (limit {MAX_UPLOAD_MB} MB)")

    extra = (callback_url,) if callback_url else ()
    task = evaluate_zip_task.delay(str(save_path), project_type, original_name, *extra)
    logger.info(
        "ZIP evaluation request queued",
        extra={"repo_url": f"upload:{original_name}", "project_type": project_type},
    )
    return {
        "task_id": task.id,
        "status": "queued",
        "message": "Evaluation queued. Poll GET /tasks/{task_id} for progress, or GET /results once complete.",
    }


@app.post("/evaluate/docker-image")
def evaluate_docker_image(req: DockerImageRequest, _auth: bool = Depends(verify_api_key)):
    extra = (req.callback_url,) if req.callback_url else ()
    task = evaluate_docker_image_task.delay(req.image, *extra)
    return {
        "task_id": task.id,
        "status": "queued",
        "message": "Evaluation queued. Poll GET /tasks/{task_id} for progress, or GET /results once complete.",
    }


@app.post("/results/{submission_id}/reevaluate")
def reevaluate(submission_id: str, _auth: bool = Depends(verify_api_key)):
    """One-click re-evaluation: queue a fresh run of the same repository / image."""
    session = get_session()
    try:
        row = session.get(Submission, submission_id)
        if not row:
            raise HTTPException(status_code=404, detail="Submission not found")
        repo_url, project_type = row.repo_url, row.project_type
    finally:
        session.close()

    if repo_url.startswith("upload:"):
        raise HTTPException(status_code=409, detail="ZIP uploads are deleted after evaluation - upload the file again")
    if repo_url.startswith("image:"):
        task = evaluate_docker_image_task.delay(repo_url[len("image:"):])
    else:
        task = evaluate_task.delay(repo_url, project_type)
    return {"task_id": task.id, "status": "queued", "repo_url": repo_url}


@app.get("/results/{submission_id}/report")
def get_report(submission_id: str, _auth: bool = Depends(verify_api_key)):
    session = get_session()
    try:
        row = session.get(Submission, submission_id)
        if not row:
            raise HTTPException(status_code=404, detail="Submission not found")
        record = {
            "submission_id": row.submission_id, "repo_url": row.repo_url,
            "project_type": row.project_type, "status": row.status,
            "build_success": row.build_success, "execution_success": row.execution_success,
            "duration_seconds": row.duration_seconds, "scores": row.scores,
            "created_at": _iso(row.created_at),
        }
        report_md = generate_report(record)
        return Response(content=report_md, media_type="text/markdown")
    finally:
        session.close()


@app.get("/tasks/{task_id}")
def get_task_status(task_id: str, _auth: bool = Depends(verify_api_key)):
    task = evaluate_task.AsyncResult(task_id)
    response = {"task_id": task_id, "status": task.status}
    if task.ready():
        if task.status == "FAILURE":
            response["error"] = str(task.result)[:500]
        else:
            response["result"] = task.result
    return response


@app.get("/results")
def list_results(limit: int = 20, _auth: bool = Depends(verify_api_key)):
    limit = max(1, min(limit, 500))
    session = get_session()
    try:
        rows = (
            session.query(Submission)
            .order_by(Submission.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "submission_id": r.submission_id,
                "repo_url": r.repo_url,
                "status": r.status,
                "scores": r.scores,
                "created_at": _iso(r.created_at),
            }
            for r in rows
        ]
    finally:
        session.close()


@app.get("/results/{submission_id}")
def get_result(submission_id: str, _auth: bool = Depends(verify_api_key)):
    session = get_session()
    try:
        row = session.get(Submission, submission_id)
        if not row:
            raise HTTPException(status_code=404, detail="Submission not found")
        return {
            "submission_id": row.submission_id,
            "repo_url": row.repo_url,
            "project_type": row.project_type,
            "status": row.status,
            "build_success": row.build_success,
            "execution_success": row.execution_success,
            "duration_seconds": row.duration_seconds,
            "logs": row.logs,
            "scores": row.scores,
            "error": row.error,
            "created_at": _iso(row.created_at),
        }
    finally:
        session.close()


# A ranking needs enough evidence: a Docker-image submission is only health-checked,
# so its "maturity" is a single metric and must not outrank fully analysed projects.
MIN_COMPONENTS_FOR_RANKING = MIN_COMPONENTS_FOR_FULL_SCORE


def _repo_key(repo_url: str) -> str:
    """Same repository however it was typed: trailing slash, .git suffix, letter case."""
    key = repo_url.strip().rstrip("/").lower()
    return key[:-4] if key.endswith(".git") else key


def _top_n(rows, key_fn, n=5, reverse=True, mode="latest"):
    """
    One entry per repository, then top n.

    mode="latest": use each repository's most recent successful evaluation (default). The old
    behaviour ("best ever") let a repository keep a score from an early run made with an older,
    more lenient scoring version - e.g. 76 on the leaderboard while the same repo now scores 50.
    mode="best": best value ever recorded per repository.

    `rows` must be ordered newest first.
    """
    chosen = {}
    for r in rows:
        if r.status != "success":
            continue
        key = _repo_key(r.repo_url)
        if mode == "latest":
            chosen.setdefault(key, r)  # first seen == newest
        else:
            v = key_fn(r)
            if v is None:
                continue
            cur = chosen.get(key)
            if cur is None or (v > key_fn(cur) if reverse else v < key_fn(cur)):
                chosen[key] = r

    ranked = []
    for r in chosen.values():
        v = key_fn(r)
        if v is not None:
            ranked.append((v, r))
    ranked.sort(key=lambda x: x[0], reverse=reverse)
    return [
        {"submission_id": r.submission_id, "repo_url": r.repo_url, "value": v, "evaluated_at": _iso(r.created_at)}
        for v, r in ranked[:n]
    ]


def _build_leaderboard(limit: int = 1000, mode: str = "latest") -> dict:
    """Shared by the authenticated /leaderboard and the public, unauthenticated
    /public/scorecard - identical ranking logic, two audiences."""
    if mode not in ("latest", "best"):
        raise HTTPException(status_code=400, detail="mode must be 'latest' or 'best'")
    limit = max(1, min(limit, 5000))
    session = get_session()
    try:
        rows = (
            session.query(Submission)
            .order_by(Submission.created_at.desc())
            .limit(limit)
            .all()
        )

        def score_of(r, key):
            entry = (r.scores or {}).get(key)
            return entry.get("score") if isinstance(entry, dict) else None

        def maturity(r):
            entry = (r.scores or {}).get("engineering_maturity")
            if not isinstance(entry, dict):
                return None
            if entry.get("partial"):
                return None
            components = entry.get("components_averaged")
            if components is not None and components < MIN_COMPONENTS_FOR_RANKING:
                return None
            return entry.get("score")

        def build_time(r):
            # Real image-build time only: image submissions are pulled, not built, and
            # older records without build_seconds only have total duration (not comparable).
            # Note: Docker layer caching makes repeat builds of the same repo faster.
            if not r.build_success or r.repo_url.startswith("image:"):
                return None
            return (r.scores or {}).get("build_seconds")

        def avg_latency(r):
            load = (r.scores or {}).get("load_test")
            smoke = (r.scores or {}).get("ui_smoke_check")
            # Only rank latency of an app that really answers at "/": a library that returns
            # 404 in 3 ms (laravel/framework) is not "fast", it is not serving anything.
            if not isinstance(load, dict) or not (isinstance(smoke, dict) and smoke.get("passed")):
                return None
            return load.get("avg_latency_ms")

        def top(fn, **kw):
            return _top_n(rows, fn, mode=mode, **kw)

        return {
            "mode": mode,
            "highest_engineering_score": top(maturity),
            "fastest_build": top(build_time, reverse=False),
            "best_architecture": top(lambda r: score_of(r, "architecture")),
            "best_api_design": top(lambda r: score_of(r, "api_quality")),
            "best_documentation": top(lambda r: score_of(r, "documentation")),
            # Lowest average response time from the load check (ms).
            "best_performance": top(avg_latency, reverse=False),
        }
    finally:
        session.close()


@app.get("/leaderboard")
def leaderboard(limit: int = 1000, mode: str = "latest", _auth: bool = Depends(verify_api_key)):
    return _build_leaderboard(limit, mode)


# Bonus: "Public Engineering Scorecard" (case study bonus challenges list).
# Deliberately unauthenticated - the point is a link that can be shared with anyone
# (other interns, recruiters, mentors) without handing out the platform's API key.
# Capped at a small, fixed limit and reuses the exact same ranking logic as
# /leaderboard, so it can never show more, or different, data than the authenticated
# view - only a smaller, public-safe slice of it (top 5 per category, "latest" mode only).
PUBLIC_SCORECARD_LIMIT = 5


@app.get("/public/scorecard")
def public_scorecard():
    full = _build_leaderboard(limit=1000, mode="latest")
    return {
        key: (value[:PUBLIC_SCORECARD_LIMIT] if isinstance(value, list) else value)
        for key, value in full.items()
    }
