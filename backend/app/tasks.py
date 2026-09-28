import json
import os
import logging
import time
import uuid

from celery.exceptions import Ignore, SoftTimeLimitExceeded

from . import callback
from .celery_app import celery_app, SOFT_TIME_LIMIT
from .janitor import worker_id
from .sandbox_engine import run_evaluation, run_evaluation_from_zip, run_evaluation_from_image
from .database import get_session, Submission
from .mlflow_tracking import log_evaluation
from .schemas import EvaluationResult

logger = logging.getLogger("sandbox.tasks")

ATTEMPT_TTL_SECONDS = 24 * 3600


def _persist(result, repo_url, project_type):
    log_evaluation(result.submission_id, repo_url, project_type, result.scores, result.duration_seconds)
    session = get_session()
    try:
        record = Submission(
            submission_id=result.submission_id,
            repo_url=repo_url,
            project_type=project_type,
            status=result.status,
            build_success=result.build_success,
            execution_success=result.execution_success,
            duration_seconds=result.duration_seconds,
            logs=result.logs,
            scores=result.scores,
            error=result.error,
        )
        session.add(record)
        session.commit()
    finally:
        session.close()


def _failed_result(start: float, error: str) -> EvaluationResult:
    return EvaluationResult(
        submission_id=str(uuid.uuid4())[:8], status="failed", build_success=False,
        execution_success=False, logs="", duration_seconds=round(time.time() - start, 2),
        error=error[:500],
    )


def _run_safely(fn, start: float, **kwargs) -> EvaluationResult:
    """
    Run an evaluation; if the engine itself crashes (Docker daemon down, unexpected
    exception...) turn that into a recorded "failed" submission instead of a lost task.
    Previously a crash left no database row, so the submission vanished from Results and
    the Metrics success-rate ignored it.
    """
    try:
        return fn(**kwargs)
    except SoftTimeLimitExceeded:
        # The engine's `finally` blocks have already removed its containers and image.
        logger.error("Evaluation stopped: time limit reached", extra={"limit_seconds": SOFT_TIME_LIMIT})
        return _failed_result(
            start, f"Evaluation stopped: it exceeded the {SOFT_TIME_LIMIT}s time limit. Try again or simplify the build."
        )
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all at the task boundary
        logger.exception("Evaluation crashed")
        return _failed_result(start, f"Internal error: {type(exc).__name__}: {exc}")


def _is_first_attempt(task_id) -> bool:
    """
    Tasks are acknowledged late, so a worker killed mid-evaluation makes the broker deliver the
    same task again. Re-running it could crash the worker again in an endless loop, so the first
    delivery claims a marker in Redis and any later delivery is recorded as "interrupted" instead.
    If Redis cannot be reached we run the task (better a duplicate than a lost evaluation).
    """
    if not task_id:
        return True
    try:
        import redis
        client = redis.Redis.from_url(celery_app.conf.broker_url, socket_connect_timeout=3, socket_timeout=3)
        return bool(client.set(f"sandbox:attempt:{task_id}", "1", nx=True, ex=ATTEMPT_TTL_SECONDS))
    except Exception:  # noqa: BLE001
        logger.warning("Attempt marker unavailable; running the task anyway")
        return True


def _redis():
    import redis
    return redis.Redis.from_url(celery_app.conf.broker_url, socket_connect_timeout=3, socket_timeout=3)


def _inflight_key() -> str:
    return f"sandbox:inflight:{worker_id()}"


def _notify(callback_url, result, label, stored_type) -> None:
    """Tell the caller (the portal) that the evaluation ended. Never raises, never changes the result."""
    if not callback_url:
        return
    try:
        callback.send_callback(callback_url, callback.build_payload(result, label, stored_type))
    except Exception:  # noqa: BLE001 - delivery problems must not touch the evaluation
        logger.exception("Callback could not be sent")


def _register_inflight(task_id, label, stored_type, hint, start, callback_url=None) -> None:
    """Remember what this worker is running, so a restart can report it instead of losing it."""
    if not task_id:
        return
    try:
        info = {"label": label, "project_type": stored_type, "hint": hint, "started": start,
                "callback_url": callback_url}
        _redis().hset(_inflight_key(), task_id, json.dumps(info))
    except Exception:  # noqa: BLE001 - best effort only
        logger.warning("Could not register in-flight task")


def _clear_inflight(task_id) -> None:
    if not task_id:
        return
    try:
        _redis().hdel(_inflight_key(), task_id)
    except Exception:  # noqa: BLE001
        pass


def _was_recovered(task_id) -> bool:
    try:
        return bool(_redis().exists(f"sandbox:recovered:{task_id}"))
    except Exception:  # noqa: BLE001
        return False


def recover_interrupted_tasks() -> int:
    """
    Called when this worker starts. Anything still registered under this worker's id was running
    when the previous process died (docker restart, OOM, hard kill): record it as "interrupted",
    mark the Celery task as failed so the dashboard stops polling, and remember that the broker's
    later re-delivery of the same message must be ignored.
    """
    try:
        client = _redis()
        entries = client.hgetall(_inflight_key())
    except Exception:  # noqa: BLE001
        return 0
    recovered = 0
    for raw_id, raw_info in entries.items():
        task_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        try:
            info = json.loads(raw_info)
            result = _interrupted_result(info.get("started") or time.time(), info.get("hint", ""))
            _persist(result, info["label"], info["project_type"])
            celery_app.backend.mark_as_failure(task_id, RuntimeError("Evaluation interrupted by a worker restart"))
            client.set(f"sandbox:recovered:{task_id}", "1", ex=ATTEMPT_TTL_SECONDS)
            recovered += 1
            # Last, so a slow or unreachable portal cannot delay the recovery bookkeeping above.
            _notify(info.get("callback_url"), result, info["label"], info["project_type"])
        except Exception:  # noqa: BLE001 - keep recovering the others
            logger.exception("Could not recover interrupted task", extra={"task_id": task_id})
        finally:
            client.hdel(_inflight_key(), task_id)
    if recovered:
        logger.warning("Recorded interrupted evaluations after restart", extra={"count": recovered})
    return recovered


def _interrupted_result(start: float, retry_hint: str) -> EvaluationResult:
    return _failed_result(
        start,
        "Evaluation was interrupted (the worker restarted or crashed while it was running). " + retry_hint,
    )


def _execute(task, label: str, stored_type: str, fn, retry_hint: str, callback_url=None, **kwargs) -> dict:
    logger.info("Evaluation started", extra={"repo_url": label, "project_type": stored_type})
    start = time.time()
    task_id = getattr(task.request, "id", None)
    if _is_first_attempt(task_id):
        _register_inflight(task_id, label, stored_type, retry_hint, start, callback_url)
        try:
            result = _run_safely(fn, start, **kwargs)
            _persist(result, label, stored_type)
        finally:
            _clear_inflight(task_id)
        _notify(callback_url, result, label, stored_type)
    else:
        logger.warning("Task redelivered after a worker interruption", extra={"repo_url": label})
        if _was_recovered(task_id):
            # Already recorded (and the Celery task marked FAILED) when the worker restarted. Ignore keeps
            # that FAILED state instead of overwriting it with a fake success.
            raise Ignore()
        result = _interrupted_result(start, retry_hint)
        _persist(result, label, stored_type)
        _notify(callback_url, result, label, stored_type)
    logger.info(
        "Evaluation completed",
        extra={"submission_id": result.submission_id, "repo_url": label, "status": result.status,
               "duration_seconds": round(time.time() - start, 2)},
    )
    return {"submission_id": result.submission_id, "status": result.status}


@celery_app.task(bind=True)
def evaluate_task(self, repo_url: str, project_type: str, callback_url: str = None):
    return _execute(self, repo_url, project_type, run_evaluation, "Use Re-evaluate to run it again.",
                    callback_url=callback_url, repo_url=repo_url, project_type=project_type)


@celery_app.task(bind=True)
def evaluate_zip_task(self, zip_path: str, project_type: str, original_filename: str, callback_url: str = None):
    try:
        return _execute(self, f"upload:{original_filename}", project_type, run_evaluation_from_zip,
                        "Upload the ZIP again.", callback_url=callback_url,
                        zip_path=zip_path, project_type=project_type, original_filename=original_filename)
    finally:
        # Always delete the upload - before, a crash left every failed ZIP on disk forever.
        try:
            os.remove(zip_path)
        except OSError:
            pass


@celery_app.task(bind=True)
def evaluate_docker_image_task(self, image_ref: str, callback_url: str = None):
    return _execute(self, f"image:{image_ref}", "docker_image", run_evaluation_from_image,
                    "Use Re-evaluate to run it again.", callback_url=callback_url, image_ref=image_ref)
