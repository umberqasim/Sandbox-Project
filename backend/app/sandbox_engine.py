"""
Core sandbox execution engine.

Entry points:
  - run_evaluation(repo_url, project_type)  - clones a GitHub/GitLab repo
  - run_evaluation_from_zip(zip_path, project_type) - extracts an uploaded ZIP
  - run_evaluation_from_image(image_ref) - pulls a pre-built Docker image

Flow once the source is on disk:
    1. Static analysis (structure, quality, security, API, architecture,
       database config, auth flow, error handling, security config,
       required features, environment configuration)
    2. Build a Docker image
    3. Run it under strict isolation (or probe it if it looks like an API)
    4. Score, generate feedback, persist logs, ALWAYS clean up
"""

import os
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import docker
from docker.errors import BuildError, ContainerError, APIError, ImageNotFound
from git import Repo
from git.exc import GitError

from .schemas import EvaluationResult
from . import analysis
from . import testing_engine
from . import janitor
from . import ai_review
from . import feedback_engine
from . import github_api
from . import plagiarism
from .validation import validate_repo_url

MAX_EXECUTION_SECONDS = int(os.environ.get("MAX_EXECUTION_SECONDS", 120))
MAX_MEMORY_MB = int(os.environ.get("MAX_MEMORY_MB", 512))
# Cap Dockerfile build-step memory too, so dependency installation cannot exhaust a 4 GB host.
MAX_BUILD_MEMORY_MB = int(os.environ.get("MAX_BUILD_MEMORY_MB", 1024))
SANDBOX_IMAGE_PREFIX = os.environ.get("SANDBOX_IMAGE_PREFIX", "sandbox-run")
CLONE_TIMEOUT_SECONDS = int(os.environ.get("CLONE_TIMEOUT_SECONDS", 120))
# Zip-bomb protection: a 50MB upload can legitimately be far smaller than what it unpacks to.
MAX_UNZIPPED_MB = int(os.environ.get("MAX_UNZIPPED_MB", 300))
# Layer caching makes the same repository build in 27s once and 55s another time, which makes the
# "Fastest Build" leaderboard meaningless. Build without cache by default (base images stay cached).
BUILD_NO_CACHE = os.environ.get("BUILD_NO_CACHE", "1") != "0"
MAX_ZIP_FILES = int(os.environ.get("MAX_ZIP_FILES", 20000))


def _build_container_limits() -> dict:
    """Docker build memory cap, with swap set equal where the host kernel supports it."""
    memory_bytes = MAX_BUILD_MEMORY_MB * 1024 * 1024
    return {"memory": memory_bytes, "memswap": memory_bytes}


class UnsafeArchiveError(Exception):
    """Raised for ZIPs that are too large when unpacked, have too many files, or escape the target dir."""


# "Configure Environment": if the repo declares its config in .env.example but ships no real .env (the
# normal, safe way to commit config to a public repo), copy the example in as a real .env so
# dotenv-reading apps (python-dotenv, Node's dotenv) have values to load instead of crashing on missing
# config. Only ever copies what the submission itself published - no secrets are invented or supplied.
# PHP template notes: composer installs dev dependencies on purpose (phpunit is a require-dev package, so
# without them the test step could never run and every PHP project looked like "tests failing"), and a
# throw-away .env with APP_KEY is created inside the image only, because Laravel will not boot without it.
BASE_DOCKERFILES = {
    "python": """
FROM python:3.11-slim
WORKDIR /submission
COPY . .
RUN if [ -f .env.example ] && [ ! -f .env ]; then cp .env.example .env; fi
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
CMD ["python", "main.py"]
""",
    "node": """
FROM node:20-slim
WORKDIR /submission
COPY . .
RUN if [ -f .env.example ] && [ ! -f .env ]; then cp .env.example .env; fi
RUN if [ -f package.json ]; then npm install; fi
CMD ["node", "index.js"]
""",
    "php": """
FROM php:8.3-cli
RUN apt-get update && apt-get install -y --no-install-recommends git unzip curl libzip-dev zip \\
    && docker-php-ext-install pdo pdo_mysql zip \\
    && rm -rf /var/lib/apt/lists/*
RUN curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer
WORKDIR /submission
COPY . .
RUN if [ -f composer.json ]; then composer install --no-interaction --optimize-autoloader || true; fi
RUN if [ -f artisan ] && [ -f .env.example ] && [ ! -f .env ]; then \\
      cp .env.example .env && php artisan key:generate --force || true; \\
    fi
# The sandbox has no MySQL/Redis. Laravel's defaults (database sessions/cache) would answer HTTP 500 on
# "/" because the tables do not exist, so inside the image only: file sessions/cache, SQLite, migrated.
RUN if [ -f artisan ] && [ -f .env ]; then \\
      sed -i '/^SESSION_DRIVER=/d; /^CACHE_STORE=/d; /^CACHE_DRIVER=/d; /^QUEUE_CONNECTION=/d' .env; \\
      sed -i '/^DB_CONNECTION=/d; /^DB_DATABASE=/d' .env; \\
      echo '' >> .env; \\
      echo 'SESSION_DRIVER=file' >> .env; \\
      echo 'CACHE_STORE=file' >> .env; \\
      echo 'CACHE_DRIVER=file' >> .env; \\
      echo 'QUEUE_CONNECTION=sync' >> .env; \\
      echo 'DB_CONNECTION=sqlite' >> .env; \\
      echo 'DB_DATABASE=/submission/database/database.sqlite' >> .env; \\
      mkdir -p database storage/framework/sessions storage/framework/cache storage/framework/views storage/logs bootstrap/cache; \\
      touch database/database.sqlite; \\
      (php artisan migrate --force || true); \\
    fi
CMD ["sh", "-c", "if [ -f artisan ]; then php artisan serve --host=0.0.0.0 --port=8000; else php -S 0.0.0.0:8000; fi"]
""",
    "flutter": """
FROM ghcr.io/cirruslabs/flutter:stable
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
WORKDIR /submission
COPY . .
RUN flutter pub get || true
RUN flutter build web || true
WORKDIR /submission/build/web
EXPOSE 8000
CMD ["python3", "-m", "http.server", "8000"]
""",
}


def _get_docker_client():
    return docker.from_env(timeout=1200)


def _clone_repo(repo_url: str, dest: Path) -> None:
    repo_url = validate_repo_url(repo_url)  # defence in depth: the API validates too
    Repo.clone_from(
        repo_url, dest, depth=1,
        env={"GIT_TERMINAL_PROMPT": "0"},          # private repo -> fail fast, never wait for a password
        kill_after_timeout=CLONE_TIMEOUT_SECONDS,  # a huge/slow repo can't hang the worker forever
    )
    # History is not needed and only bloats the Docker build context.
    shutil.rmtree(dest / ".git", ignore_errors=True)


def _friendly_clone_error(exc: Exception) -> str:
    """A message for the submitter - not git's command line and our internal temp path."""
    text = str(exc).lower()
    if isinstance(exc, ValueError):
        return str(exc)
    if "could not read username" in text or "not found" in text or "authentication failed" in text:
        return "Clone failed: repository not found, or it is private (only public repositories can be evaluated)"
    if "timeout" in text or "timed out" in text or "kill" in text:
        return f"Clone failed: took longer than {CLONE_TIMEOUT_SECONDS}s (repository too large or host unreachable)"
    return "Clone failed: git could not download the repository (details are in the logs)"


def _extract_zip(zip_path: Path, dest: Path) -> None:
    dest = Path(dest)
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_FILES:
            raise UnsafeArchiveError(f"ZIP contains too many files ({len(infos)} > {MAX_ZIP_FILES})")
        total = sum(i.file_size for i in infos)
        if total > MAX_UNZIPPED_MB * 1024 * 1024:
            raise UnsafeArchiveError(f"ZIP expands to more than {MAX_UNZIPPED_MB} MB")
        root = dest.resolve()
        for info in infos:
            target = (dest / info.filename).resolve()
            if target != root and root not in target.parents:
                raise UnsafeArchiveError(f"Unsafe path in ZIP: {info.filename}")
        zf.extractall(dest)

    # Zips made on macOS carry a __MACOSX metadata folder next to the project folder,
    # which would otherwise stop us from recognising the single project root below.
    shutil.rmtree(dest / "__MACOSX", ignore_errors=True)

    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        # Rename first: the inner folder may contain an item with its own name (app/app/...).
        inner = entries[0].rename(dest / f".inner-{uuid.uuid4().hex[:8]}")
        for item in inner.iterdir():
            shutil.move(str(item), str(dest / item.name))
        inner.rmdir()


def _ensure_dockerfile(project_dir: Path, project_type: str) -> None:
    dockerfile_path = project_dir / "Dockerfile"
    if not dockerfile_path.exists():
        template = BASE_DOCKERFILES.get(project_type, BASE_DOCKERFILES["python"])
        dockerfile_path.write_text(template)


def _compute_feature_completion(test_result: dict, api_health_result: dict, execution_success: bool,
                                 route_count: int = 0) -> dict:
    """
    `route_count` (from check_api_quality) is only used on the "api_health" basis (no tests ran): a
    reachable app with at least one detected route has real, evidenced functionality (100); a reachable
    app where no route could be found statically (a bare health probe answered, nothing else is known
    about it) is graded on that weaker evidence instead of getting the same 100 as a real feature set.
    """
    if test_result.get("ran"):
        score = 100 if test_result.get("passed") else 30
        basis = "tests"
    elif api_health_result.get("checked"):
        if not api_health_result.get("reachable"):
            score, basis = 30, "api_health"
        elif route_count > 0:
            score, basis = 100, "api_health"
        else:
            score, basis = 50, "api_health_no_routes_detected"
    elif execution_success:
        score = 60
        basis = "script_ran_cleanly"
    else:
        score = 0
        basis = "no_signal"
    return {"score": score, "basis": basis}


def _apply_auth_db_probe_evidence(auth_result: dict, db_result: dict, probe: dict) -> tuple:
    """
    Fold runtime auth/DB probe evidence into the static authentication_flow/database_connectivity
    results. Additive only: a positive probe result can raise a score, never lower one, and a probe
    that found nothing (every guessed path 404'd, or the app isn't reachable) leaves both untouched.
    This means a wrong guess can never make an evaluation worse than the static check alone.
    """
    if not probe or not probe.get("attempted"):
        return auth_result, db_result

    auth_result = dict(auth_result or {})
    db_result = dict(db_result or {})
    login = probe.get("login")
    register = probe.get("register")
    protected = probe.get("protected_route")

    auth_evidence = []
    if login and login.get("rejected_bad_credentials"):
        auth_evidence.append(
            f"login endpoint '{login['path']}' rejected bad credentials (HTTP {login['status_code']})"
        )
    if protected and protected.get("requires_auth"):
        auth_evidence.append(
            f"protected route '{protected['path']}' required authentication (HTTP {protected['status_code']})"
        )
    if auth_evidence:
        auth_result["score"] = max(auth_result.get("score") or 0, 100)
        auth_result["runtime_evidence"] = auth_evidence

    if register:
        if register.get("looks_like_db_error"):
            # Recorded for the mentor but never used to raise OR lower the score: the sandbox provides no
            # real database service, so a connection error here is expected for many fine submissions.
            db_result["runtime_evidence"] = [
                f"register endpoint '{register['path']}' failed with what looks like a database "
                f"connection error (HTTP {register['status_code']})"
            ]
        elif register.get("status_code") in (200, 201, 400, 409, 422):
            db_result["score"] = max(db_result.get("score") or 0, min(100, (db_result.get("score") or 0) + 40))
            db_result["runtime_evidence"] = [
                f"register endpoint '{register['path']}' responded normally "
                f"(HTTP {register['status_code']}), consistent with a working database"
            ]

    return auth_result, db_result


def _web_app_verdict(execution_success: bool, entrypoint_check: dict) -> tuple:
    """
    Decide status/execution_success for a project detected as a web app whose health check did not
    get a response. A library submitted on its own (no runnable start script / .listen() call, e.g.
    the 'express' package's own source) never had anything to reach, so it is not a broken submission -
    it is graded like a non-web project instead of being marked "failed" for not serving HTTP.
    Returns (status, execution_success_for_scoring, treated_as_non_web).
    """
    if execution_success:
        return "success", True, False
    if not entrypoint_check.get("present", True):
        return "success", True, True
    return "failed", False, False


def _compute_deployment_readiness(has_own_dockerfile: bool, structure_result: dict, api_health_result: dict) -> dict:
    score = 0
    if has_own_dockerfile:
        score += 40
    if ".env.example" in structure_result.get("found", []):
        score += 30
    if api_health_result.get("checked") and api_health_result.get("reachable"):
        score += 30
    elif not api_health_result.get("checked"):
        score += 15
    return {"score": min(score, 100), "has_own_dockerfile": has_own_dockerfile}


def _finalize_scores(scores: dict, duration_seconds: float = 0, source_dir=None) -> dict:
    scores["scoring_version"] = feedback_engine.SCORING_VERSION
    scores["engineering_maturity"] = feedback_engine.compute_engineering_maturity(scores)
    scores["feedback"] = feedback_engine.generate_feedback(scores, duration_seconds)
    # Optional advisory AI review (GROQ_API_KEY). Added LAST, after everything that decides scores and
    # rankings, and wrapped so that it can never fail or change an evaluation.
    try:
        review = ai_review.generate_ai_review(scores, source_dir)
    except Exception:  # noqa: BLE001
        review = None
    if review:
        scores["ai_review"] = review
    return scores


def _static_checks(work_dir: Path, project_type: str, repo_root: Path = None) -> dict:
    structure_result = analysis.check_structure(work_dir, project_type, repo_root)
    quality_result = analysis.check_code_quality(work_dir, project_type)
    security_result = analysis.check_security(work_dir, project_type)
    api_quality_result = analysis.check_api_quality(work_dir)
    is_web = analysis._is_web_project(api_quality_result)
    architecture_result = analysis.check_architecture(work_dir, project_type)
    db_result = analysis.check_database_connectivity(work_dir, is_web)
    auth_result = analysis.check_authentication_flow(work_dir, is_web)
    error_handling_result = analysis.check_error_handling(work_dir)
    security_config_result = analysis.check_security_configuration(work_dir, is_web)
    documentation_result = analysis.check_documentation(work_dir, repo_root)
    required_features_result = analysis.check_required_features(work_dir, api_quality_result, structure_result)
    environment_config_result = analysis.check_environment_configuration(work_dir)

    return {
        "structure": structure_result,
        "code_quality": quality_result,
        "security": security_result,
        "api_quality": api_quality_result,
        "architecture": architecture_result,
        "database_connectivity": db_result,
        "authentication_flow": auth_result,
        "error_handling": error_handling_result,
        "security_configuration": security_config_result,
        "required_features": required_features_result,
        "environment_configuration": environment_config_result,
        "documentation": documentation_result,
    }


def _evaluate_work_dir(work_dir: Path, submission_id: str, project_type: str, start: float,
                       repo_url: str = None) -> EvaluationResult:
    image_tag = f"{SANDBOX_IMAGE_PREFIX}:{submission_id}"
    client = _get_docker_client()
    container = None
    logs = ""
    repo_root = work_dir  # cleanup must remove the whole checkout even if we evaluate a sub-folder

    try:
        declared_type = project_type
        # Monorepo: evaluate backend/ (etc.) when the repository root has no project files.
        work_dir, root_note = analysis.find_project_root(repo_root, declared_type)
        detected_type = analysis.detect_project_type(work_dir)
        type_meta = {"evaluated_as": project_type}
        if detected_type and detected_type != project_type:
            project_type = detected_type
            type_meta = {
                "evaluated_as": detected_type,
                "project_type_note": f"Declared '{declared_type}' but the repository looks like '{detected_type}' "
                                     f"- it was evaluated as {detected_type}.",
            }
        if root_note:
            type_meta["project_root_note"] = root_note
            type_meta["project_root"] = work_dir.name

        static = _static_checks(work_dir, project_type, repo_root)
        static.update(type_meta)
        # Bonus: AI Plagiarism Detection - fingerprint this submission's source and compare it
        # against every earlier submission of the same project_type. Never changes a score;
        # `repo_url` excludes this repo's own earlier runs so "Re-evaluate" cannot flag a
        # submission as plagiarising itself. The fingerprint is stashed on this submission's
        # own scores (underscore-prefixed - internal, never rendered) so future submissions
        # can compare against it in turn.
        fingerprint = plagiarism.compute_fingerprint(work_dir)
        # Compare within the type the submission is STORED under (what the person declared), not the
        # type it was detected as: tasks._persist saves the declared type, so a project declared
        # 'python' but detected as 'node' would otherwise search the 'node' rows and never find peers.
        static["plagiarism_check"] = plagiarism.check_plagiarism(fingerprint, declared_type, repo_url=repo_url)
        static["_plagiarism_fingerprint"] = fingerprint
        structure_result = static["structure"]
        api_quality_result = static["api_quality"]

        has_own_dockerfile = (work_dir / "Dockerfile").exists()
        _ensure_dockerfile(work_dir, project_type)

        build_started = time.time()
        try:
            image, build_logs = client.images.build(
                path=str(work_dir), tag=image_tag, rm=True, forcerm=True, timeout=1200,
                nocache=BUILD_NO_CACHE, labels=janitor.managed_labels(),
                container_limits=_build_container_limits(),
            )
            build_seconds = round(time.time() - build_started, 2)
            logs += "".join(chunk.get("stream", "") for chunk in build_logs if "stream" in chunk)
        except BuildError as e:
            tail = ""
            try:  # the last build steps show WHY it failed (e.g. exit code 137 = out of memory)
                tail = "".join(c.get("stream", "") for c in list(e.build_log)[-40:] if isinstance(c, dict))
            except Exception:  # noqa: BLE001
                pass
            reason = (str(e).strip().splitlines() or ["unknown error"])[0][:300]
            return EvaluationResult(
                submission_id=submission_id, status="build_error", build_success=False,
                execution_success=False, logs=(tail + "\n" + str(e))[-6000:],
                duration_seconds=round(time.time() - start, 2), error=f"Build failed: {reason}",
                scores=_finalize_scores({
                    **static,
                    "feature_completion": None, "deployment_readiness": None,
                }, round(time.time() - start, 2), work_dir),
            )

        test_target = analysis.find_test_target(work_dir)
        test_categories = analysis.detect_test_categories(work_dir)
        # Give the submission the configuration it says it needs (.env.example values) when it runs -
        # previously nothing from .env.example reached the container, so an app that legitimately needs
        # e.g. DATABASE_URL to boot had no way to get it. PHP/Laravel already gets its own throw-away
        # .env baked into the image, so this is Python/Node only (see BASE_DOCKERFILES above).
        env_overrides = analysis.parse_env_example(work_dir) if project_type in ("python", "node") else {}
        test_result = testing_engine.run_tests(client, image_tag, test_target is not None, project_type,
                                                test_target, extra_env=env_overrides)

        looks_like_api = analysis._is_web_project(api_quality_result)  # noqa: keep next line right after
        treated_as_non_web = False  # only ever set True inside the looks_like_api branch below

        if looks_like_api:
            api_health_result = testing_engine.check_api_health(client, image_tag, looks_like_api=True,
                                                                 extra_env=env_overrides)
            raw_reachable = api_health_result.get("reachable", False)
            entrypoint_check = analysis.check_has_runnable_entrypoint(work_dir, project_type)
            status, execution_success, treated_as_non_web = _web_app_verdict(raw_reachable, entrypoint_check)
            if treated_as_non_web:
                # Score and report it like any other non-web project (checked=False): a library was never
                # going to answer HTTP, so "not reachable" should not read as a broken deployment.
                api_health_result = {**api_health_result, "checked": False, "skipped_reason": entrypoint_check["reason"]}
            if not treated_as_non_web and api_health_result.get("auth_db_probe"):
                static["authentication_flow"], static["database_connectivity"] = _apply_auth_db_probe_evidence(
                    static.get("authentication_flow"), static.get("database_connectivity"),
                    api_health_result["auth_db_probe"],
                )
            logs += "\n---API HEALTH CHECK---\n" + str(api_health_result)
            duration = round(time.time() - start, 2)
        else:
            api_health_result = {"checked": False, "reason": "not detected as a web API"}
            try:
                container = client.containers.run(
                    image=image_tag, detach=True, network_disabled=True,
                    read_only=True, tmpfs={"/tmp": ""}, environment=env_overrides or None,
                    **testing_engine._hardening_kwargs(),
                )
                try:
                    exit_status = container.wait(timeout=MAX_EXECUTION_SECONDS)
                    exec_logs = container.logs().decode(errors="replace")
                    logs += "\n---RUNTIME LOGS---\n" + exec_logs
                    execution_success = exit_status.get("StatusCode") == 0
                    status = "success" if execution_success else "failed"
                except Exception:
                    status = "timeout"
                    execution_success = False
                    logs += "\n---RUNTIME LOGS---\nExecution exceeded time limit and was terminated."
            except (ContainerError, APIError) as e:
                return EvaluationResult(
                    submission_id=submission_id, status="failed", build_success=True,
                    execution_success=False, logs=logs,
                    duration_seconds=round(time.time() - start, 2), error=f"Run failed: {e}",
                    scores=_finalize_scores({
                        **static,
                        "feature_completion": None, "deployment_readiness": None,
                    }, round(time.time() - start, 2), work_dir),
                )
            duration = round(time.time() - start, 2)

        feature_completion_result = _compute_feature_completion(
            test_result, api_health_result, execution_success,
            route_count=api_quality_result.get("route_count", 0),
        )
        deployment_readiness_result = _compute_deployment_readiness(
            has_own_dockerfile, structure_result, api_health_result,
        )

        merged = {
            **static,
            "feature_completion": feature_completion_result,
            "deployment_readiness": deployment_readiness_result,
            "testing": {**test_result, "categories": test_categories},
            "api_health": api_health_result,
            "ui_smoke_check": api_health_result.get("ui_smoke_check"),
            "load_test": api_health_result.get("load_test"),
            "resource_usage": api_health_result.get("resource_usage"),
            "build_seconds": build_seconds,
        }
        if treated_as_non_web:
            merged["library_note"] = (
                "No web server was reachable, but this looks like a library/package rather than a runnable "
                f"app ({entrypoint_check['reason']}), so it was not marked as a failed submission."
            )
        if env_overrides:
            # Names only, never values: .env.example is meant to hold placeholder/example values, but
            # the report should not echo back whatever a submission happened to put in that file.
            merged["env_injected"] = sorted(env_overrides.keys())
        scores = _finalize_scores(merged, duration, work_dir)

        return EvaluationResult(
            submission_id=submission_id, status=status, build_success=True,
            execution_success=execution_success, logs=logs,
            duration_seconds=duration, scores=scores,
        )
    finally:
        if container is not None:
            try:
                container.stop(timeout=5)
            except Exception:
                pass
            try:
                container.remove(force=True)
            except Exception:
                pass
        try:
            client.images.remove(image_tag, force=True)
        except Exception:
            pass
        shutil.rmtree(repo_root, ignore_errors=True)


def run_evaluation(repo_url: str, project_type: str = "python") -> EvaluationResult:
    submission_id = str(uuid.uuid4())[:8]
    start = time.time()
    work_dir = Path(tempfile.mkdtemp(prefix=f"submission-{submission_id}-"))

    try:
        _clone_repo(repo_url, work_dir)
    except (GitError, ValueError) as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return EvaluationResult(
            submission_id=submission_id, status="failed", build_success=False,
            execution_success=False, logs=str(e)[-1500:],
            duration_seconds=round(time.time() - start, 2), error=_friendly_clone_error(e),
        )

    result = _evaluate_work_dir(work_dir, submission_id, project_type, start, repo_url=repo_url)
    if result.scores is not None:
        result.scores["github_metadata"] = github_api.fetch_github_metadata(repo_url)
    return result


def run_evaluation_from_zip(zip_path: str, project_type: str = "python",
                            original_filename: str = None) -> EvaluationResult:
    submission_id = str(uuid.uuid4())[:8]
    start = time.time()
    work_dir = Path(tempfile.mkdtemp(prefix=f"submission-{submission_id}-"))

    try:
        _extract_zip(Path(zip_path), work_dir)
    except (zipfile.BadZipFile, UnsafeArchiveError, OSError) as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return EvaluationResult(
            submission_id=submission_id, status="failed", build_success=False,
            execution_success=False, logs="",
            duration_seconds=round(time.time() - start, 2), error=f"ZIP extraction failed: {e}",
        )

    # Same synthetic label tasks.py._persist stores as this submission's repo_url
    # ("upload:<filename>") - without it, plagiarism.check_plagiarism has no repo_url to
    # exclude, so re-uploading the exact same ZIP a second time (e.g. to test the platform,
    # or a mentor re-checking a resubmission) would be flagged as ~100% plagiarism of itself.
    repo_label = f"upload:{original_filename}" if original_filename else None
    return _evaluate_work_dir(work_dir, submission_id, project_type, start, repo_url=repo_label)


def run_evaluation_from_image(image_ref: str) -> EvaluationResult:
    submission_id = str(uuid.uuid4())[:8]
    start = time.time()
    client = _get_docker_client()
    logs = ""

    try:
        client.images.get(image_ref)
        pre_existing = True
    except ImageNotFound:
        pre_existing = False
    except APIError:
        pre_existing = True  # be safe: never delete something we're unsure about

    try:
        client.images.pull(image_ref)
        logs += f"Pulled image: {image_ref}\n"
    except APIError as e:
        return EvaluationResult(
            submission_id=submission_id, status="failed", build_success=False,
            execution_success=False, logs=logs,
            duration_seconds=round(time.time() - start, 2), error=f"Image pull failed: {e}",
        )

    try:
        api_health_result = testing_engine.check_api_health(client, image_ref, looks_like_api=True)
    finally:
        if not pre_existing:
            # Free disk: only remove images WE pulled (force=False keeps anything still in use).
            try:
                client.images.remove(image_ref, force=False)
            except Exception:
                pass
    execution_success = api_health_result.get("reachable", False)
    logs += "\n---API HEALTH CHECK---\n" + str(api_health_result)
    status = "success" if execution_success else "failed"

    feature_completion_result = {"score": 100 if execution_success else 0, "basis": "api_health"}
    deployment_readiness_result = {
        "score": None, "has_own_dockerfile": None, "not_applicable": True,
        "note": "Submitted as a pre-built Docker image - no source available to assess deployment readiness.",
    }

    scores = _finalize_scores({
        "structure": None, "code_quality": None, "security": None,
        "api_quality": None, "architecture": None,
        "feature_completion": feature_completion_result,
        "deployment_readiness": deployment_readiness_result,
        "database_connectivity": None, "authentication_flow": None,
        "error_handling": None, "security_configuration": None,
        "required_features": None, "environment_configuration": None,
        "documentation": None,
        "api_health": api_health_result,
        "ui_smoke_check": api_health_result.get("ui_smoke_check"),
        "load_test": api_health_result.get("load_test"),
        "resource_usage": api_health_result.get("resource_usage"),
    }, round(time.time() - start, 2))

    return EvaluationResult(
        submission_id=submission_id, status=status, build_success=True,
        execution_success=execution_success, logs=logs,
        duration_seconds=round(time.time() - start, 2), scores=scores,
    )
