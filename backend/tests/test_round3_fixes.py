"""Regressions found while testing 0.8.1 in Docker (laravel/framework, failed-clone message, build timing)."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from git.exc import GitCommandError

from app import analysis, main, sandbox_engine
from app.database import get_session, Submission


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ----- tests must not provide "feature evidence"

def test_test_files_do_not_create_routes_env_vars_or_auth(tmp_path):
    _write(tmp_path / "composer.json", "{}")
    _write(tmp_path / "src" / "Lib.php", "<?php class Lib {}")
    _write(tmp_path / "tests" / "RouteTest.php", "<?php Route::get('/x'); env('FOO'); use Firebase\\JWT;")
    api = analysis.check_api_quality(tmp_path)
    assert api["route_count"] == 0 and api.get("not_applicable") is True
    assert analysis.check_environment_configuration(tmp_path).get("not_applicable") is True
    assert analysis.check_authentication_flow(tmp_path, is_web=True)["auth_detected"] is False


def test_real_app_code_still_counts(tmp_path):
    _write(tmp_path / "app.js", "const app = express(); app.get('/x', h); process.env.PORT; app.use(jwt())")
    assert analysis.check_api_quality(tmp_path)["route_count"] == 1
    assert "PORT" in analysis.check_environment_configuration(tmp_path)["used_keys"]


def test_php_test_name_rule_is_not_a_substring_match(tmp_path):
    from pathlib import Path
    assert analysis._is_test_file(Path("src/FooTest.php"))
    assert not analysis._is_test_file(Path("src/contest.php"))
    assert not analysis._is_test_file(Path("src/latest.php"))


# ----- failed clone: readable message, no git command line / internal temp path

def test_missing_or_private_repo_message():
    raw = GitCommandError(
        ["git", "clone", "-v", "--", "https://github.com/nobody/x", "/tmp/submission-abc"], 128,
        stderr="fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    )
    msg = sandbox_engine._friendly_clone_error(raw)
    assert "not found" in msg and "private" in msg
    assert "/tmp" not in msg and "git clone" not in msg


def test_clone_timeout_and_generic_messages():
    assert "longer than" in sandbox_engine._friendly_clone_error(GitCommandError(["git"], -9, stderr="Timeout"))
    assert "logs" in sandbox_engine._friendly_clone_error(GitCommandError(["git"], 1, stderr="weird"))


# ----- leaderboard: only an app that answers "/" may win Best Performance

@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def _perf_row(session, sid, repo, smoke_passed, latency):
    session.add(Submission(
        submission_id=sid, repo_url=repo, project_type="php", status="success", build_success=True,
        execution_success=True, duration_seconds=1.0,
        scores={"ui_smoke_check": {"passed": smoke_passed}, "load_test": {"avg_latency_ms": latency}},
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    session.commit()


def test_404_library_does_not_win_best_performance(client):
    s = get_session()
    _perf_row(s, "perf0001", "https://github.com/lib/notanapp", False, 3.0)
    _perf_row(s, "perf0002", "https://github.com/real/app", True, 9.0)
    s.close()
    board = client.get("/leaderboard", headers={"X-API-Key": "test-key"}).json()["best_performance"]
    repos = [r["repo_url"] for r in board]
    assert "https://github.com/real/app" in repos and "https://github.com/lib/notanapp" not in repos


def test_build_uses_no_cache_by_default():
    assert sandbox_engine.BUILD_NO_CACHE is True


# ----- port discovery must ignore Docker's embedded DNS (127.0.0.11) - found with flask-localhost.zip

FLASK_IN_DOCKER = """  sl  local_address rem_address   st tx_queue rx_queue
   0: 0B00007F:A2C3 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 111 1
   1: 0100007F:1388 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 222 1
"""


def test_docker_dns_listener_is_ignored_and_localhost_bind_detected():
    from app import testing_engine
    assert testing_engine._parse_listening_sockets(FLASK_IN_DOCKER) == [(5000, False)]
    # no reachable port known -> fall back to probing the usual candidates instead of only the DNS port
    assert 8000 in testing_engine._candidate_ports([(5000, False)], exposed=[])


def test_any_127_address_is_loopback():
    from app import testing_engine
    assert testing_engine._is_loopback("0200007F")                      # 127.0.0.2
    assert testing_engine._is_loopback("0000000000000000FFFF00000100007F")  # ::ffff:127.0.0.1
    assert not testing_engine._is_loopback("00000000")                  # 0.0.0.0
    assert not testing_engine._is_loopback("0F00000A")                  # 10.0.0.15


def test_php_template_installs_dev_dependencies_and_app_key():
    template = sandbox_engine.BASE_DOCKERFILES["php"]
    assert "--no-dev" not in template and "key:generate" in template


def test_report_shows_why_tests_failed():
    from app.report_generator import generate_report
    record = {
        "submission_id": "abc", "repo_url": "https://github.com/a/b", "project_type": "php", "status": "success",
        "build_success": True, "execution_success": True, "duration_seconds": 1, "created_at": "",
        "scores": {
            "testing": {"ran": True, "passed": False, "output": "line1\nphpunit not available or not installed"},
        },
    }
    report = generate_report(record)
    assert "FAILED" in report and "phpunit not available" in report
