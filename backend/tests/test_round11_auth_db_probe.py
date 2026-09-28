"""
Heavy (runtime) auth/DB verification: probe_auth_and_db() hits conventional register/login/protected
paths on the running health-check container, and _apply_auth_db_probe_evidence() folds that evidence
into the static authentication_flow/database_connectivity scores - additively only, never lowering a
score below what static analysis already found. No real Docker/network needed: requests is monkeypatched.
"""
import pytest

from app import testing_engine
from app.sandbox_engine import _apply_auth_db_probe_evidence


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _fake_requests(monkeypatch, post_map=None, get_map=None):
    post_map, get_map = post_map or {}, get_map or {}

    def fake_post(url, json=None, timeout=None):
        path = "/" + url.split("/", 3)[-1] if url.count("/") > 2 else "/"
        outcome = post_map.get(path, _Resp(404))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def fake_get(url, timeout=None):
        path = "/" + url.split("/", 3)[-1] if url.count("/") > 2 else "/"
        outcome = get_map.get(path, _Resp(404))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(testing_engine.requests, "post", fake_post)
    monkeypatch.setattr(testing_engine.requests, "get", fake_get)


def test_nothing_found_is_fully_inconclusive(monkeypatch):
    _fake_requests(monkeypatch)
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result == {"attempted": True, "register": None, "login": None, "protected_route": None}


def test_register_endpoint_found_and_responds_normally(monkeypatch):
    _fake_requests(monkeypatch, post_map={"/register": _Resp(201, "user created")})
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result["register"] == {"path": "/register", "status_code": 201, "looks_like_db_error": False}


def test_register_endpoint_crashes_with_a_db_error_signature(monkeypatch):
    _fake_requests(monkeypatch, post_map={"/register": _Resp(500, "Error: connect ECONNREFUSED 127.0.0.1:5432")})
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result["register"]["looks_like_db_error"] is True


def test_register_500_without_a_db_signature_is_not_flagged_as_a_db_error(monkeypatch):
    _fake_requests(monkeypatch, post_map={"/register": _Resp(500, "TypeError: cannot read property of undefined")})
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result["register"]["looks_like_db_error"] is False


def test_first_matching_register_path_wins(monkeypatch):
    _fake_requests(monkeypatch, post_map={"/signup": _Resp(400), "/api/register": _Resp(201)})
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result["register"]["path"] == "/signup"


def test_login_rejecting_bad_credentials_is_recorded(monkeypatch):
    _fake_requests(monkeypatch, post_map={"/login": _Resp(401)})
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result["login"] == {"path": "/login", "status_code": 401, "rejected_bad_credentials": True}


def test_login_accepting_bad_credentials_is_recorded_as_not_rejected(monkeypatch):
    _fake_requests(monkeypatch, post_map={"/login": _Resp(200)})
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result["login"]["rejected_bad_credentials"] is False


def test_protected_route_requiring_auth_is_recorded(monkeypatch):
    _fake_requests(monkeypatch, get_map={"/me": _Resp(401)})
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result["protected_route"] == {"path": "/me", "status_code": 401, "requires_auth": True}


def test_connection_errors_are_treated_like_a_404(monkeypatch):
    _fake_requests(monkeypatch, post_map={"/register": testing_engine.requests.exceptions.ConnectionError()})
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result["register"] is None


def test_probe_never_raises_even_on_unexpected_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(testing_engine.requests, "post", boom)
    monkeypatch.setattr(testing_engine.requests, "get", boom)
    result = testing_engine.probe_auth_and_db("http://10.0.0.5:8000/")
    assert result["attempted"] is True and "error" in result


def test_no_probe_leaves_static_results_untouched():
    auth, db = {"score": 0}, {"score": 0}
    out_auth, out_db = _apply_auth_db_probe_evidence(auth, db, None)
    assert out_auth == {"score": 0} and out_db == {"score": 0}


def test_inconclusive_probe_leaves_static_results_untouched():
    auth, db = {"score": 0}, {"score": 0}
    probe = {"attempted": True, "register": None, "login": None, "protected_route": None}
    out_auth, out_db = _apply_auth_db_probe_evidence(auth, db, probe)
    assert out_auth == {"score": 0} and out_db == {"score": 0}


def test_rejected_bad_credentials_raises_auth_score_to_100():
    auth = {"score": 0, "auth_detected": False}
    probe = {"attempted": True, "login": {"path": "/login", "status_code": 401, "rejected_bad_credentials": True}}
    out_auth, _ = _apply_auth_db_probe_evidence(auth, {}, probe)
    assert out_auth["score"] == 100 and "login" in out_auth["runtime_evidence"][0]


def test_login_accepting_bad_credentials_does_not_raise_the_score():
    auth = {"score": 0}
    probe = {"attempted": True, "login": {"path": "/login", "status_code": 200, "rejected_bad_credentials": False}}
    out_auth, _ = _apply_auth_db_probe_evidence(auth, {}, probe)
    assert out_auth == {"score": 0}


def test_a_score_already_at_100_is_not_lowered_by_an_unrelated_probe():
    auth = {"score": 100, "auth_detected": True}
    probe = {"attempted": True, "login": None, "protected_route": None, "register": None}
    out_auth, _ = _apply_auth_db_probe_evidence(auth, {}, probe)
    assert out_auth["score"] == 100


def test_register_responding_normally_raises_db_score():
    db = {"score": 0, "db_config_detected": False}
    probe = {"attempted": True, "register": {"path": "/register", "status_code": 201, "looks_like_db_error": False}}
    _, out_db = _apply_auth_db_probe_evidence({}, db, probe)
    assert out_db["score"] == 40


def test_db_score_never_exceeds_100():
    db = {"score": 90}
    probe = {"attempted": True, "register": {"path": "/register", "status_code": 201, "looks_like_db_error": False}}
    _, out_db = _apply_auth_db_probe_evidence({}, db, probe)
    assert out_db["score"] == 100


def test_db_error_signature_does_not_change_the_score_either_way():
    db = {"score": 20}
    probe = {"attempted": True, "register": {"path": "/register", "status_code": 500, "looks_like_db_error": True}}
    _, out_db = _apply_auth_db_probe_evidence({}, db, probe)
    assert out_db["score"] == 20 and "connection error" in out_db["runtime_evidence"][0]


def test_db_score_is_never_lowered_by_a_db_error_signature():
    db = {"score": 60}
    probe = {"attempted": True, "register": {"path": "/register", "status_code": 500, "looks_like_db_error": True}}
    _, out_db = _apply_auth_db_probe_evidence({}, db, probe)
    assert out_db["score"] == 60


def test_protected_route_evidence_alone_also_raises_auth_score():
    auth = {"score": 0}
    probe = {"attempted": True, "protected_route": {"path": "/me", "status_code": 403, "requires_auth": True}}
    out_auth, _ = _apply_auth_db_probe_evidence(auth, {}, probe)
    assert out_auth["score"] == 100


def test_none_static_results_are_handled():
    probe = {"attempted": True, "login": {"path": "/login", "status_code": 401, "rejected_bad_credentials": True}}
    out_auth, out_db = _apply_auth_db_probe_evidence(None, None, probe)
    assert out_auth["score"] == 100 and out_db == {}
