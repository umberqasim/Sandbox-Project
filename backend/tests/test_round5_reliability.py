"""Worker reliability: late acks + time limits, redelivery handling, leftover cleanup."""
import time

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from app import celery_app as celery_module, janitor, tasks, testing_engine
from app.schemas import EvaluationResult

NOW = time.mktime((2026, 9, 21, 12, 0, 0, 0, 0, 0))  # arbitrary fixed "now" (local time is fine, only differences matter)


# ---------------------------------------------------------------- celery configuration

def test_celery_is_configured_for_late_ack_and_time_limits():
    conf = celery_module.celery_app.conf
    assert conf.task_acks_late is True and conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert 0 < conf.task_soft_time_limit < conf.task_time_limit
    assert conf.broker_transport_options["visibility_timeout"] > conf.task_time_limit
    assert janitor.STALE_AFTER_SECONDS > conf.task_time_limit  # a live evaluation is never "stale"


# ---------------------------------------------------------------- redelivery / time limit handling

def _capture(monkeypatch):
    saved = []
    monkeypatch.setattr(tasks, "_persist", lambda result, label, ptype: saved.append((result, label, ptype)))
    return saved


def _ok_result():
    return EvaluationResult(submission_id="abc12345", status="success", build_success=True,
                            execution_success=True, logs="", duration_seconds=1.0)


def test_first_delivery_runs_the_engine(monkeypatch):
    saved = _capture(monkeypatch)
    monkeypatch.setattr(tasks, "_is_first_attempt", lambda task_id: True)
    monkeypatch.setattr(tasks, "run_evaluation", lambda **kw: _ok_result())
    out = tasks.evaluate_task.run("https://github.com/u/r", "python")
    assert out == {"submission_id": "abc12345", "status": "success"}
    assert saved[0][1] == "https://github.com/u/r"


def test_redelivered_task_is_recorded_as_interrupted_not_rerun(monkeypatch):
    saved = _capture(monkeypatch)
    monkeypatch.setattr(tasks, "_is_first_attempt", lambda task_id: False)

    def must_not_run(**kw):
        raise AssertionError("engine must not run a second time")
    monkeypatch.setattr(tasks, "run_evaluation", must_not_run)

    out = tasks.evaluate_task.run("https://github.com/u/r", "python")
    assert out["status"] == "failed"
    result = saved[0][0]
    assert "interrupted" in result.error and "Re-evaluate" in result.error


def test_redelivery_of_an_already_recovered_task_is_ignored_and_not_recorded_twice(monkeypatch):
    from celery.exceptions import Ignore
    saved = _capture(monkeypatch)
    monkeypatch.setattr(tasks, "_is_first_attempt", lambda task_id: False)
    monkeypatch.setattr(tasks, "_was_recovered", lambda task_id: True)
    with pytest.raises(Ignore):
        tasks.evaluate_task.run("https://github.com/u/r", "python")
    assert saved == []


def test_zip_task_tells_the_user_to_upload_again_and_still_deletes_the_file(monkeypatch, tmp_path):
    saved = _capture(monkeypatch)
    monkeypatch.setattr(tasks, "_is_first_attempt", lambda task_id: False)
    upload = tmp_path / "x.zip"
    upload.write_bytes(b"zip")
    tasks.evaluate_zip_task.run(str(upload), "python", "x.zip")
    assert "Upload the ZIP again" in saved[0][0].error
    assert saved[0][1] == "upload:x.zip" and not upload.exists()


def test_soft_time_limit_becomes_a_clear_failed_result():
    def slow(**kw):
        raise SoftTimeLimitExceeded()
    result = tasks._run_safely(slow, time.time())
    assert result.status == "failed" and "time limit" in result.error


def test_other_crashes_are_still_recorded():
    def boom(**kw):
        raise RuntimeError("docker down")
    result = tasks._run_safely(boom, time.time())
    assert result.status == "failed" and "RuntimeError: docker down" in result.error


class _FakeRedis:
    claimed = set()

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.claimed:
            return None
        self.claimed.add(key)
        return True


def test_attempt_marker_is_claimed_once_per_task_id(monkeypatch):
    import redis
    _FakeRedis.claimed = set()
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda *a, **kw: _FakeRedis()))
    assert tasks._is_first_attempt("task-1") is True
    assert tasks._is_first_attempt("task-1") is False
    assert tasks._is_first_attempt("task-2") is True


def test_unreachable_redis_means_run_anyway(monkeypatch):
    import redis

    def down(*a, **kw):
        raise ConnectionError("redis down")
    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(down))
    assert tasks._is_first_attempt("task-3") is True


# ---------------------------------------------------------------- janitor

class _Container:
    def __init__(self, labels, created, image="sandbox-run:x", fail=False):
        self.labels = labels
        self.attrs = {"Created": created, "Config": {"Image": image}}
        self.short_id = "c1"
        self.removed = False
        self._fail = fail

    def remove(self, force=False):
        if self._fail:
            raise RuntimeError("busy")
        self.removed = True


class _Image:
    def __init__(self, labels, created):
        self.labels = labels
        self.attrs = {"Created": created}
        self.id = f"sha256:{id(self)}"
        self.short_id = "i1"


class _Client:
    def __init__(self, containers=(), images=(), image_remove_error=False):
        self._containers, self._images = list(containers), list(images)
        self.removed_images = []
        outer = self

        class Conts:
            def list(self, all=False):
                return outer._containers

        class Imgs:
            def list(self, name=None):
                return outer._images

            def remove(self, image_id, force=False):
                if image_remove_error:
                    raise RuntimeError("image is being used")
                outer.removed_images.append(image_id)

        self.containers, self.images = Conts(), Imgs()


def _iso(seconds_ago):
    t = time.gmtime(NOW - seconds_ago)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + ".123456789Z"  # Docker sends nanoseconds


def _run(client, **kw):
    return janitor.cleanup_stale_resources(client, own_worker="me", max_age_seconds=2400, now=NOW - 0, **kw)


def test_own_workers_leftovers_are_removed_even_when_young():
    mine = _Container({"sandbox.managed": "1", "sandbox.worker": "me"}, _iso(30))
    assert _run(_Client([mine]))["containers"] == 1 and mine.removed


def test_other_workers_young_resources_are_left_alone_but_old_ones_go():
    young = _Container({"sandbox.managed": "1", "sandbox.worker": "other"}, _iso(300))
    old = _Container({"sandbox.managed": "1", "sandbox.worker": "dead"}, _iso(5000))
    counts = _run(_Client([young, old]))
    assert counts["containers"] == 1 and old.removed and not young.removed


def test_legacy_unlabelled_sandbox_containers_are_cleaned_by_age_only():
    young = _Container({}, _iso(100))
    old = _Container({}, _iso(9000))
    _run(_Client([young, old]))
    assert old.removed and not young.removed


def test_unrelated_and_compose_containers_are_never_touched():
    other = _Container({}, _iso(99999), image="nginx:alpine")
    service = _Container({"com.docker.compose.project": "sandbox-project", "sandbox.worker": "me"}, _iso(99999))
    _run(_Client([other, service]))
    assert not other.removed and not service.removed


def test_images_are_removed_by_ownership_or_age_and_in_use_errors_are_swallowed():
    mine = _Image({"sandbox.worker": "me"}, _iso(10))
    foreign_young = _Image({"sandbox.worker": "other"}, _iso(10))
    legacy_old = _Image({}, _iso(86400))  # like the 3.6 GB sandbox-run:41e6b300 found on disk
    client = _Client(images=[mine, foreign_young, legacy_old])
    assert _run(client)["images"] == 2 and set(client.removed_images) == {mine.id, legacy_old.id}
    assert _run(_Client(images=[legacy_old], image_remove_error=True))["images"] == 0


def test_unknown_age_is_treated_as_new_and_daemon_errors_never_raise():
    weird = _Container({"sandbox.managed": "1", "sandbox.worker": "other"}, "not-a-date")
    _run(_Client([weird]))
    assert not weird.removed

    class Broken:
        class containers:
            @staticmethod
            def list(all=False):
                raise RuntimeError("daemon down")
    assert _run(Broken()) == {"containers": 0, "images": 0}


def test_worker_id_prefers_the_stable_setting_over_the_hostname(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "3f9a1c")
    monkeypatch.delenv("SANDBOX_WORKER_ID", raising=False)
    assert janitor.worker_id() == "3f9a1c"
    monkeypatch.setenv("SANDBOX_WORKER_ID", "sandbox_worker")
    assert janitor.worker_id() == "sandbox_worker"


def test_everything_the_engine_starts_is_labelled_for_the_janitor():
    labels = testing_engine._hardening_kwargs()["labels"]
    assert labels["sandbox.managed"] == "1" and labels["sandbox.worker"] == janitor.worker_id()
    with pytest.raises(KeyError):
        labels["com.docker.compose.project"]
