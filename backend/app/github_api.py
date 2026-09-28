"""
Lightweight GitHub REST API integration - fetches public repo metadata
(stars, description, language, last push) to enrich evaluation results.
Uses the unauthenticated public API (60 req/hour is plenty for this use
case); silently skips for non-GitHub URLs or on any failure so it never
blocks an evaluation.
"""

import re
import requests

GITHUB_URL_PATTERN = re.compile(r"github\.com[/:]([\w.\-]+)/([\w.\-]+?)(?:\.git)?/?$")


def fetch_github_metadata(repo_url: str) -> dict:
    match = GITHUB_URL_PATTERN.search(repo_url)
    if not match:
        return {"available": False, "reason": "not a github.com URL"}

    owner, repo = match.group(1), match.group(2)
    try:
        resp = requests.get(f"https://api.github.com/repos/{owner}/{repo}", timeout=5)
        if resp.status_code != 200:
            return {"available": False, "reason": f"GitHub API returned {resp.status_code}"}
        data = resp.json()
        return {
            "available": True,
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues": data.get("open_issues_count"),
            "language": data.get("language"),
            "description": data.get("description"),
            "last_pushed_at": data.get("pushed_at"),
            "default_branch": data.get("default_branch"),
        }
    except requests.RequestException as e:
        return {"available": False, "reason": str(e)}
