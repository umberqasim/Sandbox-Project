import logging
import os

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger, worker_ready

from .logging_config import setup_logging

# An evaluation normally takes 30 s - 6 min. The soft limit raises SoftTimeLimitExceeded inside the
# task (so the engine's `finally` blocks still remove containers/images); the hard limit kills the
# worker child if that does not work. The janitor's STALE_AFTER_SECONDS must stay above the hard limit.
SOFT_TIME_LIMIT = int(os.environ.get("EVAL_SOFT_TIME_LIMIT", 1500))
HARD_TIME_LIMIT = int(os.environ.get("EVAL_HARD_TIME_LIMIT", 1800))
# Redis only re-delivers an unacknowledged task after this long, so it must exceed the hard limit.
VISIBILITY_TIMEOUT = int(os.environ.get("CELERY_VISIBILITY_TIMEOUT", HARD_TIME_LIMIT * 2))

celery_app = Celery(
    "sandbox",
    broker=os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0"),
    backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/0"),
    include=["app.tasks"],
)
celery_app.conf.update(
    task_track_started=True,
    # Acknowledge a task only AFTER it finished: if the worker is killed mid-evaluation the message
    # is delivered again (tasks.py then records an "interrupted" result instead of re-running it).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,          # one long evaluation per slot, never hoarded
    task_soft_time_limit=SOFT_TIME_LIMIT,
    task_time_limit=HARD_TIME_LIMIT,
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT},
)


@after_setup_logger.connect
@after_setup_task_logger.connect
def _apply_json_logging(**kwargs):
    setup_logging()


@worker_ready.connect
def _recover_after_restart(**kwargs):
    """
    1. Record evaluations that were running when this worker was killed as "interrupted" (so the
       dashboard shows a result instead of "STARTED" forever).
    2. Remove the images/containers those runs left behind (see janitor.py).
    """
    log = logging.getLogger("sandbox.janitor")
    try:
        from .tasks import recover_interrupted_tasks
        recover_interrupted_tasks()
    except Exception:  # noqa: BLE001 - never block worker start-up
        log.exception("Recovery of interrupted tasks failed")
    try:
        from .janitor import cleanup_stale_resources
        cleanup_stale_resources()
    except Exception:  # noqa: BLE001
        log.exception("Janitor failed")
