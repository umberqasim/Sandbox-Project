"""Bonus challenge: Public Engineering Scorecard. No Docker, Redis or internet needed."""
import itertools
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import main
from app.database import Submission, get_session

HEADERS = {"X-API-Key": "test-key"}
_counter = itertools.count()


def _seed(n=8):
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        for i in range(n):
            sid = f"pubsc-{next(_counter)}"
            session.add(Submission(
                submission_id=sid,
                repo_url=f"https://github.com/example/{sid}",
                project_type="python",
                status="success",
                build_success=True,
                execution_success=True,
                duration_seconds=10.0,
                scores={
                    "engineering_maturity": {"score": 100 - i, "components_averaged": 8},
                    "architecture": {"score": 90 - i},
                    "api_quality": {"score": 80 - i},
                    "documentation": {"score": 70 - i},
                },
                created_at=now - timedelta(minutes=n - i),
            ))
        session.commit()
    finally:
        session.close()


def test_public_scorecard_requires_no_api_key():
    with TestClient(main.app) as c:
        _seed()
        res = c.get("/public/scorecard")
        assert res.status_code == 200


def test_leaderboard_still_requires_api_key():
    with TestClient(main.app) as c:
        res = c.get("/leaderboard")
        assert res.status_code == 401


def test_public_scorecard_matches_leaderboard_ranking():
    with TestClient(main.app) as c:
        _seed()
        public = c.get("/public/scorecard").json()
        private = c.get("/leaderboard", headers=HEADERS).json()
        for key in ("highest_engineering_score", "best_architecture", "best_api_design", "best_documentation"):
            assert public[key] == private[key][:5]


def test_public_scorecard_caps_rows_even_with_more_data():
    with TestClient(main.app) as c:
        _seed(n=12)
        public = c.get("/public/scorecard").json()
        private = c.get("/leaderboard?limit=5000", headers=HEADERS).json()
        assert len(public["highest_engineering_score"]) <= 5
        assert len(public["highest_engineering_score"]) <= len(private["highest_engineering_score"])


def test_public_scorecard_exposes_only_known_fields():
    with TestClient(main.app) as c:
        _seed()
        public = c.get("/public/scorecard").json()
        allowed_top_level = {
            "mode", "highest_engineering_score", "fastest_build", "best_architecture",
            "best_api_design", "best_documentation", "best_performance",
        }
        assert set(public.keys()) <= allowed_top_level
        for row in public["highest_engineering_score"]:
            assert set(row.keys()) == {"submission_id", "repo_url", "value", "evaluated_at"}


def test_public_scorecard_is_always_latest_mode():
    with TestClient(main.app) as c:
        _seed()
        public = c.get("/public/scorecard").json()
        assert public["mode"] == "latest"
