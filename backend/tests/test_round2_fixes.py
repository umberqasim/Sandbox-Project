"""Regressions found by evaluating real repositories (laravel/framework, a monorepo, a Docker image)."""
from app import analysis, feedback_engine
from app.report_generator import generate_report


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ----- monorepo: real project lives in backend/, root only has docker-compose

def test_monorepo_picks_backend_over_frontend(tmp_path):
    _write(tmp_path / "docker-compose.yml")
    _write(tmp_path / "README.md", "# Monorepo")
    _write(tmp_path / "backend" / "requirements.txt")
    _write(tmp_path / "backend" / "Dockerfile")
    _write(tmp_path / "frontend" / "package.json", "{}")
    _write(tmp_path / "frontend" / "Dockerfile")

    root, note = analysis.find_project_root(tmp_path, declared_type="node")
    assert root.name == "backend"
    assert "frontend" in note  # tells the user the other project was not evaluated


def test_normal_repo_keeps_its_root(tmp_path):
    _write(tmp_path / "package.json", "{}")
    _write(tmp_path / "backend" / "requirements.txt")
    assert analysis.find_project_root(tmp_path, "node") == (tmp_path, None)


def test_readme_and_gitignore_at_repo_root_count_for_subfolder(tmp_path):
    _write(tmp_path / "README.md", "# " + "docs " * 200)
    _write(tmp_path / ".gitignore")
    sub = tmp_path / "backend"
    _write(sub / "requirements.txt")
    assert "README.md" in analysis.check_structure(sub, "python", tmp_path)["found"]
    assert "README.md" not in analysis.check_structure(sub, "python")["found"]
    assert analysis.check_documentation(sub, tmp_path)["score"] > analysis.check_documentation(sub)["score"]


# ----- test files must not drive the security score

def test_security_scan_ignores_test_files(tmp_path):
    _write(tmp_path / "composer.json", "{}")
    _write(tmp_path / "src" / "app.php", "<?php echo 1;")
    _write(tmp_path / "tests" / "FooTest.php", "<?php eval($x); unserialize($y); $password = 'secret123';")
    result = analysis.check_security(tmp_path, "php")
    assert result["findings_count"] == 0 and result["score"] == 100


def test_security_scan_still_flags_real_code(tmp_path):
    _write(tmp_path / "src" / "app.php", "<?php eval($x);")
    assert analysis.check_security(tmp_path, "php")["findings_count"] == 1


def test_oversized_test_files_are_not_architecture_findings(tmp_path):
    _write(tmp_path / "tests" / "BigTest.php", "line\n" * 500)
    _write(tmp_path / "src" / "a.php", "x")
    assert analysis.check_architecture(tmp_path, "php")["oversized_files"] == []


# ----- feedback / report quality

def test_undeclared_env_vars_are_collapsed_to_one_line():
    scores = {"environment_configuration": {"score": 0, "used_but_not_declared": [f"VAR_{i}" for i in range(200)]}}
    missing = feedback_engine.generate_feedback(scores)["missing_requirements"]
    env_lines = [m for m in missing if "environment variable" in m]
    assert len(env_lines) == 1 and "200" in env_lines[0] and "+192 more" in env_lines[0]


def test_no_tests_line_is_not_reported_for_docker_images_or_failed_builds():
    image = {"structure": None, "code_quality": None, "feature_completion": {"score": 100}}
    assert "No automated tests found" not in feedback_engine.generate_feedback(image)["missing_requirements"]


def test_test_timeout_is_not_called_failing():
    scores = {"structure": {"score": 50, "missing": []}, "testing": {"ran": True, "passed": False, "timed_out": True}}
    fb = feedback_engine.generate_feedback(scores, 10)
    assert any("did not finish" in m for m in fb["missing_requirements"])
    assert not any("failing" in m for m in fb["missing_requirements"] + fb["performance_suggestions"])


def test_report_mentions_root_note_and_timeout():
    record = {
        "submission_id": "abc", "repo_url": "https://github.com/a/b", "project_type": "node", "status": "failed",
        "build_success": True, "execution_success": False, "duration_seconds": 1, "created_at": "",
        "scores": {
            "project_root_note": "evaluated the 'backend/' folder",
            "testing": {"ran": True, "passed": False, "timed_out": True},
        },
    }
    report = generate_report(record)
    assert "evaluated the 'backend/' folder" in report and "TIMED OUT" in report


def test_complexity_findings_name_their_file(tmp_path):
    body = "\n".join(f"    if x == {i}:\n        return {i}" for i in range(15))
    _write(tmp_path / "big.py", f"def f(x):\n{body}\n    return 0\n")
    complex_fns = analysis._radon_complexity(tmp_path)["complex_functions"]
    assert complex_fns and complex_fns[0]["file"].endswith("big.py")
