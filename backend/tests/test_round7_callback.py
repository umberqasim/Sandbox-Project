"""Portal callback: URL safety, signing, retries, API wiring and task wiring. No Docker, Redis or internet needed."""
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from app import callback, main, tasks
from app.schemas import EvaluationResult

HEADERS = {"X-API-Key": "test-key"}
SECRET = "s3cret-value"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("CALLBACK_SECRET", "CALLBACK_ALLOWED_HOSTS", "CALLBACK_ALLOW_INSECURE", "CALLBACK_ALLOW_PRIVATE",
                 "CALLBACK_MAX_ATTEMPTS", "CALLBACK_BACKOFF_SECONDS", "CALLBACK_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def _result(status="success", scores=None, error=None):
    return EvaluationResult(submission_id="abc12345", status=status, build_success=True, execution_success=True,
                            logs="SECRET LOG LINE", duration_seconds=12.5, scores=scores, error=error)


# ------------------------------------------------------------------ URL validation

@pytest.mark.parametrize("url", [
    "http://portal.example.com/hook",            # not https
    "ftp://portal.example.com/hook",
    "https://user:pw@portal.example.com/hook",   # credentials
    "https://localhost/hook",
    "https://app.localhost/hook",
    "https://127.0.0.1/hook",
    "https://10.0.0.5/hook",
    "https://192.168.1.10/hook",
    "https://169.254.169.254/latest/meta-data",  # cloud metadata address
    "https://[::1]/hook",
    "https://[::ffff:127.0.0.1]/hook",
    "https://0.0.0.0/hook",
    "https://portal.example.com:99999/hook",     # invalid port
    "https://portal.example.com/has space",
    "https:///nohost",
    "",
    "https://portal.example.com/" + "a" * 600,
])
def test_unsafe_or_malformed_callback_urls_are_rejected(url):
    with pytest.raises(ValueError):
        callback.validate_callback_url(url)


def test_public_https_url_is_accepted():
    assert callback.validate_callback_url(" https://portal.example.com/api/hook?x=1 ") == \
        "https://portal.example.com/api/hook?x=1"


def test_allow_list_is_enforced(monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOWED_HOSTS", "portal.example.com, other.example.org")
    assert callback.validate_callback_url("https://portal.example.com/x")
    with pytest.raises(ValueError, match="not allowed"):
        callback.validate_callback_url("https://evil.example.net/x")


def test_local_testing_switches(monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOW_INSECURE", "1")
    monkeypatch.setenv("CALLBACK_ALLOW_PRIVATE", "1")
    assert callback.validate_callback_url("http://localhost:9000/hook")
    assert callback.validate_callback_url("http://10.0.0.5/hook")


def test_hostname_resolving_to_a_private_address_is_refused_at_send_time(monkeypatch):
    monkeypatch.setattr(callback.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
    posted = []
    monkeypatch.setattr(callback.requests, "post", lambda *a, **k: posted.append(1))
    out = callback.send_callback("https://portal.example.com/hook", {"a": 1}, sleep=lambda s: None)
    assert out["delivered"] is False and "private" in out["error"] and out["attempts"] == 1
    assert posted == []  # nothing was sent


def test_one_private_address_among_several_is_enough_to_refuse(monkeypatch):
    monkeypatch.setattr(callback.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))])
    out = callback.send_callback("https://portal.example.com/hook", {}, sleep=lambda s: None)
    assert out["delivered"] is False and "private" in out["error"]


def test_unresolvable_host_is_reported_not_raised(monkeypatch):
    def fail(*a, **k):
        raise callback.socket.gaierror("no such host")
    monkeypatch.setattr(callback.socket, "getaddrinfo", fail)
    out = callback.send_callback("https://nope.invalid/hook", {}, sleep=lambda s: None)
    assert out["delivered"] is False and "does not resolve" in out["error"]


# ------------------------------------------------------------------ payload + signature

def test_payload_is_a_log_free_summary():
    scores = {"engineering_maturity": {"score": 41, "partial": False}, "architecture": {"score": 15},
              "feature_completion": {"score": 100}, "database_connectivity": None, "scoring_version": 2,
              "feedback": {"strengths": ["x"]}}
    payload = callback.build_payload(_result(scores=scores), "https://github.com/u/r", "node")
    assert payload["event"] == "evaluation.finished" and payload["submission_id"] == "abc12345"
    assert payload["status"] == "success" and payload["repo_url"] == "https://github.com/u/r"
    assert payload["scores"]["engineering_maturity"] == 41 and payload["scores"]["architecture"] == 15
    assert payload["scores"]["database_connectivity"] is None and payload["scores"]["security"] is None
    assert payload["result_path"] == "/results/abc12345" and payload["report_path"] == "/results/abc12345/report"
    assert "SECRET LOG LINE" not in json.dumps(payload) and "feedback" not in payload
    json.dumps(payload)  # must be serialisable


def test_payload_for_a_result_without_scores():
    payload = callback.build_payload(_result(status="failed", error="Clone failed"), "upload:x.zip", "python")
    assert payload["status"] == "failed" and payload["error"] == "Clone failed"
    assert set(payload["scores"].values()) == {None} and payload["engineering_maturity_partial"] is None


def test_signature_matches_an_independent_computation():
    expected = "sha256=" + hmac.new(b"k", b"1700000000." + b'{"a":1}', hashlib.sha256).hexdigest()
    assert callback.sign("k", "1700000000", b'{"a":1}') == expected


# ------------------------------------------------------------------ delivery against a real local HTTP server

class _Hook(BaseHTTPRequestHandler):
    received = []
    script = []          # status codes to answer with, one per request; the last one repeats
    redirect_to = None

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        code = type(self).script[min(len(type(self).received) - 1, len(type(self).script) - 1)]
        self.send_response(code)
        if code in (301, 302, 307) and type(self).redirect_to:
            self.send_header("Location", type(self).redirect_to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def hook(monkeypatch):
    _Hook.received, _Hook.script, _Hook.redirect_to = [], [200], None
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("CALLBACK_ALLOW_INSECURE", "1")   # the fake portal is http://127.0.0.1
    monkeypatch.setenv("CALLBACK_ALLOW_PRIVATE", "1")
    yield f"http://127.0.0.1:{server.server_port}", _Hook
    server.shutdown()
    server.server_close()


def test_delivery_is_signed_and_carries_the_payload(hook, monkeypatch):
    url, server = hook
    monkeypatch.setenv("CALLBACK_SECRET", SECRET)
    payload = callback.build_payload(_result(scores={"engineering_maturity": {"score": 41}}), "https://github.com/u/r", "node")
    out = callback.send_callback(url + "/portal/hook?token=abc", payload, sleep=lambda s: None)
    assert out == {"delivered": True, "attempts": 1, "status_code": 200, "error": None}
    got = server.received[0]
    assert got["path"] == "/portal/hook?token=abc"
    assert json.loads(got["body"])["submission_id"] == "abc12345"
    headers = {k.lower(): v for k, v in got["headers"].items()}
    assert headers["x-sandbox-event"] == "evaluation.finished" and headers["content-type"] == "application/json"
    timestamp = headers["x-sandbox-timestamp"]
    expected = "sha256=" + hmac.new(SECRET.encode(), timestamp.encode() + b"." + got["body"], hashlib.sha256).hexdigest()
    assert headers["x-sandbox-signature"] == expected
    assert abs(int(timestamp) - time.time()) < 30


def test_unsigned_when_no_secret_is_configured(hook):
    url, server = hook
    callback.send_callback(url + "/h", {"a": 1}, sleep=lambda s: None)
    assert "x-sandbox-signature" not in {k.lower() for k in server.received[0]["headers"]}


def test_server_errors_are_retried_with_the_same_delivery_id(hook):
    url, server = hook
    server.script = [500, 503, 200]
    pauses = []
    out = callback.send_callback(url + "/h", {"a": 1}, sleep=pauses.append)
    assert out["delivered"] is True and out["attempts"] == 3
    assert pauses == [2.0, 4.0]  # exponential back-off
    ids = {r["headers"]["X-Sandbox-Delivery"] for r in server.received}
    assert len(server.received) == 3 and len(ids) == 1


def test_gives_up_after_the_configured_attempts(hook, monkeypatch):
    url, server = hook
    monkeypatch.setenv("CALLBACK_MAX_ATTEMPTS", "2")
    server.script = [500]
    out = callback.send_callback(url + "/h", {}, sleep=lambda s: None)
    assert out["delivered"] is False and out["attempts"] == 2 and out["error"] == "HTTP 500"


def test_client_errors_are_final_and_not_retried(hook):
    url, server = hook
    server.script = [400]
    out = callback.send_callback(url + "/h", {}, sleep=lambda s: None)
    assert out["delivered"] is False and out["attempts"] == 1 and len(server.received) == 1


def test_redirects_are_never_followed(hook):
    url, server = hook
    server.script = [302]
    server.redirect_to = url + "/elsewhere"
    out = callback.send_callback(url + "/h", {}, sleep=lambda s: None)
    assert out["delivered"] is False and out["attempts"] == 1
    assert [r["path"] for r in server.received] == ["/h"]


def test_unreachable_receiver_never_raises(monkeypatch):
    monkeypatch.setenv("CALLBACK_ALLOW_INSECURE", "1")
    monkeypatch.setenv("CALLBACK_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("CALLBACK_MAX_ATTEMPTS", "2")
    out = callback.send_callback("http://127.0.0.1:1/h", {}, sleep=lambda s: None)  # nothing listens on port 1
    assert out["delivered"] is False and out["attempts"] == 2 and out["error"]


def test_unexpected_error_inside_delivery_is_contained(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")
    monkeypatch.setattr(callback, "_deliver", boom)
    out = callback.send_callback("https://portal.example.com/h", {}, sleep=lambda s: None)
    assert out["delivered"] is False and "RuntimeError" in out["error"]


# ------------------------------------------------------------------ API wiring

@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


class _FakeTask:
    id = "fake-task"


def _capture_delay(monkeypatch, task):
    calls = []
    monkeypatch.setattr(task, "delay", lambda *a: calls.append(a) or _FakeTask())
    return calls


def test_evaluate_without_callback_queues_exactly_as_before(client, monkeypatch):
    calls = _capture_delay(monkeypatch, main.evaluate_task)
    r = client.post("/evaluate", headers=HEADERS, json={"repo_url": "https://github.com/u/r", "project_type": "node"})
    assert r.status_code == 200 and calls == [("https://github.com/u/r", "node")]


def test_evaluate_passes_a_valid_callback_on(client, monkeypatch):
    calls = _capture_delay(monkeypatch, main.evaluate_task)
    r = client.post("/evaluate", headers=HEADERS, json={
        "repo_url": "https://github.com/u/r", "project_type": "node", "callback_url": "https://portal.example.com/hook"})
    assert r.status_code == 200
    assert calls == [("https://github.com/u/r", "node", "https://portal.example.com/hook")]


@pytest.mark.parametrize("bad", ["http://portal.example.com/h", "https://127.0.0.1/h", "https://169.254.169.254/x"])
def test_evaluate_rejects_an_unsafe_callback_before_queueing(client, monkeypatch, bad):
    calls = _capture_delay(monkeypatch, main.evaluate_task)
    r = client.post("/evaluate", headers=HEADERS, json={
        "repo_url": "https://github.com/u/r", "project_type": "node", "callback_url": bad})
    assert r.status_code == 422 and "callback_url" in r.text and calls == []


def test_empty_callback_means_none(client, monkeypatch):
    calls = _capture_delay(monkeypatch, main.evaluate_task)
    r = client.post("/evaluate", headers=HEADERS, json={
        "repo_url": "https://github.com/u/r", "project_type": "node", "callback_url": ""})
    assert r.status_code == 200 and calls == [("https://github.com/u/r", "node")]


def test_docker_image_callback(client, monkeypatch):
    calls = _capture_delay(monkeypatch, main.evaluate_docker_image_task)
    assert client.post("/evaluate/docker-image", headers=HEADERS, json={"image": "user/img:1"}).status_code == 200
    assert client.post("/evaluate/docker-image", headers=HEADERS, json={
        "image": "user/img:1", "callback_url": "https://portal.example.com/h"}).status_code == 200
    assert client.post("/evaluate/docker-image", headers=HEADERS, json={
        "image": "user/img:1", "callback_url": "http://x/h"}).status_code == 422
    assert calls == [("user/img:1",), ("user/img:1", "https://portal.example.com/h")]


def test_zip_upload_callback_is_validated_before_the_file_is_saved(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)
    calls = _capture_delay(monkeypatch, main.evaluate_zip_task)
    files = {"file": ("p.zip", b"PK\x03\x04data", "application/zip")}
    bad = client.post("/evaluate/upload", headers=HEADERS, files=files,
                      data={"project_type": "python", "callback_url": "https://10.0.0.1/h"})
    assert bad.status_code == 422 and calls == [] and list(tmp_path.iterdir()) == []
    ok = client.post("/evaluate/upload", headers=HEADERS, files=files,
                     data={"project_type": "python", "callback_url": "https://portal.example.com/h"})
    assert ok.status_code == 200 and calls[0][1:] == ("python", "p.zip", "https://portal.example.com/h")
    plain = client.post("/evaluate/upload", headers=HEADERS, files=files, data={"project_type": "python"})
    assert plain.status_code == 200 and calls[1][1:] == ("python", "p.zip")


def test_reevaluate_still_queues_without_a_callback(client, monkeypatch):
    from app.database import get_session, Submission
    calls = _capture_delay(monkeypatch, main.evaluate_task)
    s = get_session()
    s.add(Submission(submission_id="cb000001", repo_url="https://github.com/a/cb", project_type="node",
                     status="success", build_success=True, execution_success=True, duration_seconds=1.0, scores={}))
    s.commit()
    s.close()
    assert client.post("/results/cb000001/reevaluate", headers=HEADERS).status_code == 200
    assert calls == [("https://github.com/a/cb", "node")]


# ------------------------------------------------------------------ task wiring

def _wire(monkeypatch, first_attempt=True):
    saved, sent = [], []
    monkeypatch.setattr(tasks, "_persist", lambda result, label, ptype: saved.append((result, label, ptype)))
    monkeypatch.setattr(tasks, "_is_first_attempt", lambda task_id: first_attempt)
    monkeypatch.setattr(tasks.callback, "send_callback", lambda url, payload: sent.append((url, payload)))
    return saved, sent


def test_callback_is_sent_after_the_result_is_stored(monkeypatch):
    saved, sent = _wire(monkeypatch)
    order = []
    monkeypatch.setattr(tasks, "_persist", lambda r, l, p: order.append("persist"))
    monkeypatch.setattr(tasks.callback, "send_callback", lambda u, p: order.append("callback"))
    monkeypatch.setattr(tasks, "run_evaluation", lambda **kw: _result(scores={"engineering_maturity": {"score": 50}}))
    out = tasks.evaluate_task.run("https://github.com/u/r", "python", "https://portal.example.com/h")
    assert out == {"submission_id": "abc12345", "status": "success"}
    assert order == ["persist", "callback"]


def test_callback_payload_and_url(monkeypatch):
    saved, sent = _wire(monkeypatch)
    monkeypatch.setattr(tasks, "run_evaluation", lambda **kw: _result(scores={"architecture": {"score": 15}}))
    tasks.evaluate_task.run("https://github.com/u/r", "python", "https://portal.example.com/h")
    url, payload = sent[0]
    assert url == "https://portal.example.com/h"
    assert payload["repo_url"] == "https://github.com/u/r" and payload["scores"]["architecture"] == 15


def test_no_callback_when_none_given(monkeypatch):
    saved, sent = _wire(monkeypatch)
    monkeypatch.setattr(tasks, "run_evaluation", lambda **kw: _result())
    tasks.evaluate_task.run("https://github.com/u/r", "python")
    assert sent == [] and len(saved) == 1


def test_a_crashing_engine_still_notifies_the_portal(monkeypatch):
    saved, sent = _wire(monkeypatch)

    def boom(**kw):
        raise RuntimeError("docker down")
    monkeypatch.setattr(tasks, "run_evaluation", boom)
    tasks.evaluate_task.run("https://github.com/u/r", "python", "https://portal.example.com/h")
    assert sent[0][1]["status"] == "failed" and "docker down" in sent[0][1]["error"]


def test_a_failing_callback_never_changes_the_evaluation(monkeypatch):
    saved, _ = _wire(monkeypatch)
    monkeypatch.setattr(tasks.callback, "send_callback", lambda u, p: (_ for _ in ()).throw(RuntimeError("portal down")))
    monkeypatch.setattr(tasks, "run_evaluation", lambda **kw: _result())
    out = tasks.evaluate_task.run("https://github.com/u/r", "python", "https://portal.example.com/h")
    assert out["status"] == "success" and len(saved) == 1


def test_redelivered_task_notifies_with_the_interrupted_result(monkeypatch):
    saved, sent = _wire(monkeypatch, first_attempt=False)
    monkeypatch.setattr(tasks, "_was_recovered", lambda task_id: False)
    tasks.evaluate_task.run("https://github.com/u/r", "python", "https://portal.example.com/h")
    assert sent[0][1]["status"] == "failed" and "interrupted" in sent[0][1]["error"]


def test_already_recovered_redelivery_does_not_notify_twice(monkeypatch):
    from celery.exceptions import Ignore
    saved, sent = _wire(monkeypatch, first_attempt=False)
    monkeypatch.setattr(tasks, "_was_recovered", lambda task_id: True)
    with pytest.raises(Ignore):
        tasks.evaluate_task.run("https://github.com/u/r", "python", "https://portal.example.com/h")
    assert sent == [] and saved == []


def test_zip_and_image_tasks_pass_the_callback_through(monkeypatch, tmp_path):
    saved, sent = _wire(monkeypatch)
    monkeypatch.setattr(tasks, "run_evaluation_from_zip", lambda **kw: _result())
    monkeypatch.setattr(tasks, "run_evaluation_from_image", lambda **kw: _result())
    upload = tmp_path / "x.zip"
    upload.write_bytes(b"zip")
    tasks.evaluate_zip_task.run(str(upload), "python", "x.zip", "https://portal.example.com/z")
    tasks.evaluate_docker_image_task.run("user/img:1", "https://portal.example.com/i")
    assert [(u, p["repo_url"]) for u, p in sent] == [
        ("https://portal.example.com/z", "upload:x.zip"), ("https://portal.example.com/i", "image:user/img:1")]
    assert not upload.exists()


class _FakeRedis:
    def __init__(self, entries):
        self.entries, self.set_keys, self.deleted = entries, [], []

    def hgetall(self, key):
        return self.entries

    def hdel(self, key, field):
        self.deleted.append(field)

    def set(self, key, value, ex=None, nx=False):
        self.set_keys.append(key)


def test_in_flight_registration_remembers_the_callback(monkeypatch):
    stored = {}

    class R:
        def hset(self, key, field, value):
            stored[field] = json.loads(value)
    monkeypatch.setattr(tasks, "_redis", lambda: R())
    tasks._register_inflight("t1", "https://github.com/u/r", "python", "hint", 1.0, "https://portal.example.com/h")
    assert stored["t1"]["callback_url"] == "https://portal.example.com/h"


def test_recovery_after_a_worker_restart_notifies_the_portal(monkeypatch):
    saved, sent = _wire(monkeypatch)
    info = {"label": "https://github.com/u/r", "project_type": "python", "hint": "Use Re-evaluate to run it again.",
            "started": time.time(), "callback_url": "https://portal.example.com/h"}
    old = {"label": "https://github.com/u/old", "project_type": "python", "hint": "", "started": time.time()}  # no callback key
    fake = _FakeRedis({b"t1": json.dumps(info), b"t2": json.dumps(old)})
    monkeypatch.setattr(tasks, "_redis", lambda: fake)
    marked = []
    monkeypatch.setattr(tasks.celery_app.backend, "mark_as_failure", lambda tid, exc: marked.append(tid), raising=False)
    assert tasks.recover_interrupted_tasks() == 2
    assert marked == ["t1", "t2"] and len(saved) == 2
    assert len(sent) == 1 and sent[0][0] == "https://portal.example.com/h" and "interrupted" in sent[0][1]["error"]
