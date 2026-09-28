"""
Leftover-resource cleanup for the sandbox worker.

Every evaluation cleans up after itself in a `finally` block, but that cannot run when the worker
process is killed (docker restart, OOM, hard time limit). Those runs used to leave a built image
(3.6 GB in one real case) and stopped containers on the host until somebody pruned them by hand.

Everything the engine creates is labelled `sandbox.managed=1` and `sandbox.worker=<worker id>`.
When a worker starts it removes:
  - resources that carry ITS OWN worker id (they belong to the run that was just killed), and
  - any managed resource older than STALE_AFTER_SECONDS, whoever owns it (dead workers, old runs).
Resources of other workers that are younger than that are left alone, so scaling out workers can
never delete an evaluation that is still running. Compose service containers are never touched.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone

MANAGED_LABEL = "sandbox.managed"
WORKER_LABEL = "sandbox.worker"
IMAGE_PREFIX = os.environ.get("SANDBOX_IMAGE_PREFIX", "sandbox-run")
# Must be longer than the hard task time limit so a live evaluation is never considered stale.
STALE_AFTER_SECONDS = int(os.environ.get("JANITOR_STALE_AFTER_SECONDS", 2400))

logger = logging.getLogger("sandbox.janitor")


def worker_id() -> str:
    """Stable id of this worker: SANDBOX_WORKER_ID if set (survives container re-creation), else the hostname."""
    return os.environ.get("SANDBOX_WORKER_ID") or os.environ.get("HOSTNAME", "unknown-worker")


def managed_labels() -> dict:
    return {MANAGED_LABEL: "1", WORKER_LABEL: worker_id()}


def _age_seconds(created: str, now: float) -> float:
    """Docker returns RFC3339 with nanoseconds ('2026-09-21T10:00:00.123456789Z'); trim to microseconds."""
    try:
        trimmed = re.sub(r"(\.\d{6})\d+", r"\1", created).replace("Z", "+00:00")
        return now - datetime.fromisoformat(trimmed).astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0.0  # unknown age -> treat as brand new (never delete on a guess)


def _should_remove(labels: dict, age: float, own_worker: str, max_age: float) -> bool:
    if labels.get("com.docker.compose.project"):
        return False  # never touch the platform's own services
    return labels.get(WORKER_LABEL) == own_worker or age > max_age


def cleanup_stale_resources(client=None, own_worker=None, max_age_seconds=None, now=None) -> dict:
    """Remove leftover sandbox containers and images. Returns counts; never raises."""
    own_worker = own_worker or worker_id()
    max_age = STALE_AFTER_SECONDS if max_age_seconds is None else max_age_seconds
    now = time.time() if now is None else now
    removed = {"containers": 0, "images": 0}

    try:
        if client is None:
            import docker
            client = docker.from_env(timeout=60)

        for container in client.containers.list(all=True):
            labels = container.labels or {}
            config_image = (container.attrs.get("Config") or {}).get("Image", "")
            ours = labels.get(MANAGED_LABEL) == "1" or config_image.startswith(f"{IMAGE_PREFIX}:")
            if not ours:
                continue
            age = _age_seconds(container.attrs.get("Created", ""), now)
            if _should_remove(labels, age, own_worker, max_age):
                try:
                    container.remove(force=True)
                    removed["containers"] += 1
                except Exception as exc:  # noqa: BLE001 - keep cleaning the rest
                    logger.warning("Could not remove container %s: %s", container.short_id, exc)

        for image in client.images.list(name=IMAGE_PREFIX):
            labels = image.labels or {}
            age = _age_seconds(image.attrs.get("Created", ""), now)
            if _should_remove(labels, age, own_worker, max_age):
                try:
                    client.images.remove(image.id, force=False)  # in-use images are refused, which is fine
                    removed["images"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not remove image %s: %s", image.short_id, exc)
    except Exception as exc:  # noqa: BLE001 - a broken janitor must never stop the worker
        logger.warning("Janitor skipped: %s", exc)

    if removed["containers"] or removed["images"]:
        logger.info("Janitor removed leftovers", extra=removed)
    return removed
