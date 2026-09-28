from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import get_session, Submission

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def _add(session, sid, repo, maturity=50, arch=10, when=None, status="success", components=13, doc=None):
    scores = {
        "engineering_maturity": {"score": maturity, "components_averaged": components, "partial": components < 5},
        "architecture": {"score": arch},
    }
    if doc is not None:
        scores["documentation"] = {"score": doc}
    session.add(Submission(
        submission_id=sid, repo_url=repo, project_type="node", status=status,
        build_success=True, execution_success=True, duration_seconds=10.0,
        scores=scores, created_at=when or datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    session.commit()


def test_auth_required(client):
    assert client.get("/results").status_code == 401
    assert client.get("/results", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/results", headers=HEADERS).status_code == 200


def test_unsafe_url_is_rejected_with_readable_error(client):
    r = client.post("/evaluate", headers=HEADERS, json={"repo_url": "file:///etc/passwd", "project_type": "python"})
    assert r.status_code == 422
    assert "https" in r.text


def test_timestamps_carry_timezone(client):
    s = get_session()
    _add(s, "tz000001", "https://github.com/a/tz")
    s.close()
    created = client.get("/results/tz000001", headers=HEADERS).json()["created_at"]
    assert created.endswith("+00:00")  # without it browsers show UTC time as local time


def test_leaderboard_uses_latest_run_not_stale_best(client):
    """Old lenient run (76) must not beat the repo's current score (50)."""
    s = get_session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _add(s, "old00001", "https://github.com/x/stale", maturity=76, when=now - timedelta(days=30))
    _add(s, "new00001", "https://github.com/x/stale/", maturity=50, when=now)   # trailing slash = same repo
    s.close()

    board = client.get("/leaderboard", headers=HEADERS).json()
    rows = [r for r in board["highest_engineering_score"] if "x/stale" in r["repo_url"]]
    assert len(rows) == 1 and rows[0]["value"] == 50

    best = client.get("/leaderboard?mode=best", headers=HEADERS).json()
    rows = [r for r in best["highest_engineering_score"] if "x/stale" in r["repo_url"]]
    assert rows[0]["value"] == 76


def test_partial_maturity_is_not_ranked(client):
    s = get_session()
    _add(s, "img00001", "image:crccheck/hello-world", maturity=100, components=1)
    s.close()
    board = client.get("/leaderboard", headers=HEADERS).json()
    assert all("crccheck" not in r["repo_url"] for r in board["highest_engineering_score"])


def test_reevaluate_queues_repo_and_refuses_zip(client, monkeypatch):
    queued = {}

    class FakeTask:
        id = "fake-task-id"

    monkeypatch.setattr(main.evaluate_task, "delay", lambda *a: queued.setdefault("args", a) and FakeTask())
    s = get_session()
    _add(s, "re000001", "https://github.com/a/re")
    _add(s, "re000002", "upload:project.zip")
    s.close()

    r = client.post("/results/re000001/reevaluate", headers=HEADERS)
    assert r.status_code == 200 and queued["args"] == ("https://github.com/a/re", "node")
    assert client.post("/results/re000002/reevaluate", headers=HEADERS).status_code == 409
    assert client.post("/results/nope/reevaluate", headers=HEADERS).status_code == 404


def test_cors_has_no_credentials(client):
    r = client.options("/results", headers={"Origin": "http://x.test", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}
