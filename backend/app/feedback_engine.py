"""
Rule-based feedback synthesis. Turns the raw scores/findings we already
collected into the structured feedback mentors actually want to read:
strengths, weaknesses, missing requirements, security risks, performance
suggestions, refactoring suggestions, and a prioritized improvement roadmap.
"""

# Bump whenever the scoring formulas change, so old records can be recognised as stale
# (older runs were scored with fewer checks and are not comparable with new ones).
SCORING_VERSION = 6

# An engineering-maturity average built from fewer components than this (e.g. a Docker
# image that only got a health check) is flagged "partial" and never ranked.
MIN_COMPONENTS_FOR_FULL_SCORE = 5

LABELS = {
    "structure": "Project structure",
    "code_quality": "Code quality",
    "security": "Security",
    "api_quality": "API design",
    "architecture": "Architecture",
    "feature_completion": "Feature completion",
    "deployment_readiness": "Deployment readiness",
    "database_connectivity": "Database connectivity",
    "authentication_flow": "Authentication flow",
    "error_handling": "Error handling",
    "security_configuration": "Security configuration",
    "required_features": "Required features",
    "environment_configuration": "Environment configuration",
}

ROADMAP_SUGGESTIONS = {
    "structure": "Add a README, dependency manifest, and a tests/ folder.",
    "code_quality": "Run a linter locally and fix the reported style issues.",
    "security": "Remove hardcoded secrets and avoid risky calls like eval()/exec()/shell=True.",
    "api_quality": "Document routes and add an OpenAPI/Swagger spec if this is meant to be an API.",
    "architecture": "Split logic into dedicated folders (models/routes/services) instead of one large file.",
    "feature_completion": (
        "Add automated tests, or make sure the app starts and responds so functionality can be verified."
    ),
    "deployment_readiness": "Include your own Dockerfile and document required environment variables.",
    "database_connectivity": "Add database configuration (connection string/env vars) and migration files.",
    "authentication_flow": (
        "Add an authentication mechanism (JWT, session-based login, or a framework's built-in auth)."
    ),
    "error_handling": "Add try/catch blocks or centralized error-handling middleware around risky operations.",
    "security_configuration": (
        "Configure CORS explicitly and load secrets from environment variables instead of hardcoding them."
    ),
    "required_features": "Add routes, a framework, tests, and a descriptive README to demonstrate core functionality.",
    "environment_configuration": "Add a .env.example documenting every environment variable the code actually reads.",
}


def _roadmap_text(key: str, scores: dict) -> str:
    """Roadmap line for a weak category - specific to what was actually found where possible."""
    if key == "structure":
        missing = (scores.get("structure") or {}).get("missing", [])
        if missing:
            return f"Add the missing project files: {', '.join(missing)}."
    if key == "environment_configuration":
        undeclared = (scores.get("environment_configuration") or {}).get("used_but_not_declared", [])
        if undeclared:
            shown = ", ".join(undeclared[:8])
            return f".env.example is missing these variables the code reads: {shown}."
    return ROADMAP_SUGGESTIONS.get(key, f"Improve {LABELS.get(key, key).lower()}.")


def compute_engineering_maturity(scores: dict) -> dict:
    keys = list(LABELS.keys())
    vals = [scores[k]["score"] for k in keys if scores.get(k) and scores[k].get("score") is not None]
    if not vals:
        return {"score": None, "components_averaged": 0, "partial": True}
    return {
        "score": round(sum(vals) / len(vals)),
        "components_averaged": len(vals),
        "partial": len(vals) < MIN_COMPONENTS_FOR_FULL_SCORE,
    }


def _refactoring_suggestions(scores: dict) -> list:
    suggestions = []
    architecture = scores.get("architecture") or {}

    for f in architecture.get("oversized_files", [])[:3]:
        suggestions.append(f"{f['file']} is {f['lines']} lines - consider splitting it into smaller modules.")

    complexity = architecture.get("complexity") or {}
    for fn in complexity.get("complex_functions", [])[:3]:
        suggestions.append(
            f"{fn['name']}() in {fn['file']} has high cyclomatic complexity ({fn['complexity']}, rank {fn['rank']}) - "
            f"consider breaking it into smaller functions."
        )

    return suggestions


# Images are built without Docker's layer cache (BUILD_NO_CACHE), so every build is a cold build and
# routinely takes 1-3 minutes. Only a build far beyond that says something about the submission.
SLOW_BUILD_SECONDS = 300


def _performance_suggestions(scores: dict, duration_seconds: float) -> list:
    """
    `duration_seconds` (whole evaluation: build + tests + start-up waits + load check) is kept in the
    signature for callers but no longer used: it mostly measures the sandbox, not the submission,
    so "Build+run took 88s - trim dependencies" was unfair to small, healthy projects.
    """
    suggestions = []
    build_seconds = scores.get("build_seconds")
    if isinstance(build_seconds, (int, float)) and build_seconds > SLOW_BUILD_SECONDS:
        suggestions.append(
            f"The image took {round(build_seconds)}s to build from scratch - heavy dependencies or a large base "
            f"image slow every deployment; consider trimming dependencies or using a slimmer base image."
        )
    testing = scores.get("testing") or {}
    if testing.get("ran") and not testing.get("passed") and not testing.get("timed_out"):
        suggestions.append(
            "Some checks are failing - a failing pipeline itself is a performance/reliability drag on iteration speed."
        )

    load_test = scores.get("load_test") or {}
    if load_test:
        avg = load_test.get("avg_latency_ms")
        p95 = load_test.get("p95_latency_ms")
        success = load_test.get("success_rate_percent")
        root_status = (scores.get("ui_smoke_check") or {}).get("status_code")
        root_error = isinstance(root_status, int) and root_status >= 500
        if success == 0 and root_error:
            suggestions.append(
                f"The load check could not measure performance: '/' answered HTTP {root_status} on every request. "
                f"Fix the server error at '/' first."
            )
        elif success is not None and success < 100:
            suggestions.append(
                f"Only {success}% of {load_test.get('requests_sent', '?')} burst requests succeeded - "
                f"check error handling and startup readiness under repeated requests."
            )
        if avg is not None and avg > 500:
            suggestions.append(
                f"Average response time was {avg}ms - profile slow handlers and add caching where sensible."
            )
        elif p95 is not None and p95 > 1000:
            suggestions.append(
                f"p95 latency was {p95}ms - a few requests are much slower than the rest; look for blocking calls."
            )

    smoke = scores.get("ui_smoke_check") or {}
    if smoke and not smoke.get("passed"):
        code = smoke.get("status_code")
        if isinstance(code, int) and code >= 500:
            suggestions.append(
                f"Root URL returned a server error (HTTP {code}). The sandbox provides no external database or "
                f"services, so this can also mean the app needs one - check the app's own error log."
            )
        else:
            suggestions.append(
                f"Root URL check failed (HTTP {code if code is not None else 'no response'}) - "
                f"make sure the app serves something at '/'."
            )

    return suggestions


def generate_feedback(scores: dict, duration_seconds: float = 0) -> dict:
    strengths = []
    weaknesses = []
    missing_requirements = []
    security_risks = []
    roadmap = []

    for key, label in LABELS.items():
        entry = scores.get(key)
        if not entry or entry.get("score") is None:
            continue
        s = entry["score"]
        if s >= 80:
            strengths.append(f"{label} is strong ({s}/100)")
        elif s < 50:
            weaknesses.append(f"{label} needs improvement ({s}/100)")
            roadmap.append(_roadmap_text(key, scores))

    structure = scores.get("structure") or {}
    for missing in structure.get("missing", []):
        missing_requirements.append(f"Missing {missing}")

    testing = scores.get("testing")
    if testing is not None:  # absent for Docker images (no source) and for builds that failed
        if not testing.get("ran"):
            missing_requirements.append("No automated tests found")
        elif testing.get("timed_out"):
            missing_requirements.append(
                "Automated tests did not finish within the time limit (large suite, or waiting for a database/network)"
            )
        elif not testing.get("passed"):
            missing_requirements.append("Automated tests are present but failing")

    security = scores.get("security") or {}
    for finding in security.get("findings", []):
        desc = finding.get("description") or finding.get("rule")
        security_risks.append(f"{finding.get('file', '?')}: {desc}")

    api_quality = scores.get("api_quality") or {}
    if api_quality.get("framework_detected") and not api_quality.get("has_api_docs"):
        missing_requirements.append("No API documentation (openapi.json/swagger) found")

    env_config = scores.get("environment_configuration") or {}
    undeclared = env_config.get("used_but_not_declared", [])
    if undeclared:
        # One line, not one per variable: big projects read 200+ variables and drowned the report.
        shown = ", ".join(undeclared[:8])
        more = f" (+{len(undeclared) - 8} more)" if len(undeclared) > 8 else ""
        missing_requirements.append(
            f"{len(undeclared)} environment variable(s) read by the code are not declared in "
            f".env.example: {shown}{more}"
        )

    if "No automated tests found" in missing_requirements:
        # Same finding as the structure check's "Missing tests" - report it once.
        missing_requirements = [m for m in missing_requirements if m not in ("Missing tests", "Missing test")]

    if scores.get("structure") is None and scores.get("code_quality") is None and scores.get("feature_completion"):
        # Docker-image submission: no source, so most categories are N/A.
        missing_requirements.append(
            "Source code was not available (Docker image submission) - only runtime health was assessed"
        )

    if not strengths:
        strengths.append("No standout strengths yet - see the improvement roadmap below.")

    return {
        "strengths": strengths,
        "weaknesses": weaknesses,
        "missing_requirements": missing_requirements,
        "security_risks": security_risks,
        "performance_suggestions": _performance_suggestions(scores, duration_seconds),
        "refactoring_suggestions": _refactoring_suggestions(scores),
        "improvement_roadmap": roadmap,
    }
