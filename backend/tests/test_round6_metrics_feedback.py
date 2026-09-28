"""Round 6: fair performance suggestion and a metrics split between platform reliability and submission outcome."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import feedback_engine, main
from app.database import get_session, Submission

HEADERS = {"X-API-Key": "test-key"}


# ----- "Build+run took Ns" must not be reported for ordinary builds

def test_long_total_duration_alone_is_not_a_suggestion():
    scores = {"build_seconds": 71.7}
    suggestions = feedback_engine._performance_suggestions(scores, duration_seconds=200)
    assert not any("build" in s.lower() for s in suggestions)


def test_only_an_unusually_slow_cold_build_is_reported():
    slow = feedback_engine._performance_suggestions({"build_seconds": 412}, duration_seconds=0)
    assert len(slow) == 1 and "412s" in slow[0] and "from scratch" in slow[0]
    assert feedback_engine._performance_suggestions({"build_seconds": 158.8}, duration_seconds=173) == []
    assert feedback_engine._performance_suggestions({}, duration_seconds=500) == []  # e.g. Docker image, no build


# ----- metrics: completion rate (platform) vs success rate (submission)

@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def _row(session, sid, status, error=None):
    session.add(Submission(
        submission_id=sid, repo_url=f"https://github.com/x/{sid}", project_type="python", status=status,
        build_success=status == "success", execution_success=status == "success", duration_seconds=5.0,
        error=error, created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    session.commit()


def test_negative_tests_do_not_count_as_platform_failures(client):
    before = client.get("/metrics", headers=HEADERS).json()

    s = get_session()
    _row(s, "m0000001", "success")
    _row(s, "m0000002", "build_error", "Build failed: pip exited with 1")                       # verdict
    _row(s, "m0000003", "failed", "Clone failed: repository not found, or it is private")       # verdict
    _row(s, "m0000004", "failed", None)                                                          # app not reachable
    _row(s, "m0000005", "failed", "Internal error: APIError: docker daemon not running")         # platform
    _row(s, "m0000006", "failed", "Evaluation was interrupted (the worker restarted). Re-evaluate.")  # platform
    _row(s, "m0000007", "failed", "Evaluation stopped: it exceeded the 1500s time limit.")       # platform
    s.close()

    after = client.get("/metrics", headers=HEADERS).json()
    assert after["total_submissions"] - before["total_submissions"] == 7
    assert after["platform_error_count"] - before["platform_error_count"] == 3

    added_completed = 7 - 3
    completed_before = before["total_submissions"] - before["platform_error_count"]
    completed_after = after["total_submissions"] - after["platform_error_count"]
    assert completed_after - completed_before == added_completed
    assert after["completion_rate_percent"] == round(completed_after / after["total_submissions"] * 100, 1)
    # the "built and ran" rate keeps its old meaning and is lower than the completion rate
    assert after["success_rate_percent"] < after["completion_rate_percent"]
