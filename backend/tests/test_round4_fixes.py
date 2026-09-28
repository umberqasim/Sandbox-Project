"""Regressions found by running 0.8.2 on laravel/laravel and express-hello-world."""
from app import analysis, feedback_engine, sandbox_engine


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ----- PHP/Laravel false negatives

def test_laravel_env_helper_counts_as_env_based_secrets(tmp_path):
    _write(tmp_path / "artisan", "")
    _write(tmp_path / "app" / "Service.php", "<?php $key = env('APP_KEY');")
    result = analysis.check_security_configuration(tmp_path, is_web=True)
    assert result["uses_env_based_secrets"] is True


def test_laravel_with_exceptions_counts_as_error_handling(tmp_path):
    _write(tmp_path / "bootstrap" / "app.php", "<?php return Application::configure()->withExceptions(function ($e) {})->create();")
    result = analysis.check_error_handling(tmp_path)
    assert result["error_handling_detected"] is True and result["score"] > 0


def test_optional_env_reads_are_not_reported_as_missing(tmp_path):
    _write(tmp_path / "artisan", "")
    _write(tmp_path / "config" / "app.php", "<?php return ['a' => env('OPTIONAL_A', 'x'), 'b' => env('NULLABLE_B')];")
    _write(tmp_path / "app" / "Thing.php", "<?php $c = env('REALLY_NEEDED');")
    _write(tmp_path / ".env.example", "APP_NAME=x\n")
    result = analysis.check_environment_configuration(tmp_path)
    assert result["used_but_not_declared"] == ["REALLY_NEEDED"]
    assert {"OPTIONAL_A", "NULLABLE_B", "REALLY_NEEDED"} <= set(result["used_keys"])


def test_python_and_node_defaults_are_optional_strict_reads_are_required(tmp_path):
    _write(tmp_path / "m.py", "import os\na = os.environ['DB_URL']\nb = os.environ.get('MODE', 'x')\n")
    _write(tmp_path / "index.js", "const p = process.env.PORT || 3000; const t = process.env.API_TOKEN;")
    _write(tmp_path / ".env.example", "MODE=x\n")
    result = analysis.check_environment_configuration(tmp_path)
    assert result["used_but_not_declared"] == ["API_TOKEN", "DB_URL"]


def test_fully_declared_project_with_defaults_scores_100(tmp_path):
    _write(tmp_path / "m.py", "import os\nMODE = os.environ.get('MODE', 'x')\n")
    _write(tmp_path / ".env.example", "MODE=x\n")
    assert analysis.check_environment_configuration(tmp_path)["score"] == 100


# ----- Laravel must be able to answer "/" inside the sandbox (no MySQL, no Redis)

def test_php_template_gives_laravel_file_sessions_and_sqlite():
    template = sandbox_engine.BASE_DOCKERFILES["php"]
    for expected in ("SESSION_DRIVER=file", "CACHE_STORE=file", "DB_CONNECTION=sqlite", "database.sqlite", "migrate --force"):
        assert expected in template
    assert "--no-dev" not in template and "key:generate" in template


# ----- feedback must not blame the student for a sandbox-side 5xx

def test_load_check_with_root_5xx_is_explained_not_blamed():
    scores = {
        "load_test": {"requests_sent": 15, "success_rate_percent": 0.0, "avg_latency_ms": 500, "p95_latency_ms": 600},
        "ui_smoke_check": {"passed": False, "status_code": 500},
    }
    text = " ".join(feedback_engine._performance_suggestions(scores, 10))
    assert "could not measure" in text and "burst requests succeeded" not in text
    assert "server error" in text and "HTTP 500" in text


def test_partial_load_failures_still_get_the_generic_advice():
    scores = {"load_test": {"requests_sent": 15, "success_rate_percent": 60.0}, "ui_smoke_check": {"passed": True, "status_code": 200}}
    assert "60.0% of 15 burst requests succeeded" in " ".join(feedback_engine._performance_suggestions(scores, 10))
