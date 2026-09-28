"""
Two related fixes:
1. Feature completion no longer gives a flat 100 for merely being "reachable" when no tests ran and
   no route could be found - it needs evidence (route_count) of real functionality (analysis.py,
   sandbox_engine._compute_feature_completion).
2. A project detected as a web app whose health check found nothing listening is no longer marked
   "failed" when it has no runnable entry point (a library submitted on its own, e.g. the 'express'
   package's own source) - sandbox_engine._web_app_verdict / analysis.check_has_runnable_entrypoint.
Pure-function tests: no Docker needed.
"""
from app import analysis
from app.sandbox_engine import _compute_feature_completion, _web_app_verdict


def test_reachable_with_a_detected_route_still_scores_100():
    result = _compute_feature_completion({"ran": False}, {"checked": True, "reachable": True},
                                         execution_success=True, route_count=1)
    assert result == {"score": 100, "basis": "api_health"}


def test_reachable_with_no_detected_route_is_downgraded():
    result = _compute_feature_completion({"ran": False}, {"checked": True, "reachable": True},
                                         execution_success=True, route_count=0)
    assert result["score"] == 50 and result["basis"] == "api_health_no_routes_detected"


def test_unreachable_is_unchanged_regardless_of_route_count():
    for routes in (0, 3):
        result = _compute_feature_completion({"ran": False}, {"checked": True, "reachable": False},
                                             execution_success=False, route_count=routes)
        assert result == {"score": 30, "basis": "api_health"}


def test_passing_tests_still_win_over_route_evidence():
    result = _compute_feature_completion({"ran": True, "passed": True}, {"checked": True, "reachable": True},
                                         execution_success=True, route_count=0)
    assert result == {"score": 100, "basis": "tests"}


def test_route_count_defaults_to_zero_for_old_call_sites():
    result = _compute_feature_completion({"ran": False}, {"checked": True, "reachable": True}, True)
    assert result["basis"] == "api_health_no_routes_detected"


def test_non_web_paths_are_unaffected():
    assert _compute_feature_completion({"ran": False}, {"checked": False}, execution_success=True) == \
        {"score": 60, "basis": "script_ran_cleanly"}
    assert _compute_feature_completion({"ran": False}, {"checked": False}, execution_success=False) == \
        {"score": 0, "basis": "no_signal"}


def test_node_start_script_counts_as_runnable(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"start": "node index.js"}}')
    out = analysis.check_has_runnable_entrypoint(tmp_path, "node")
    assert out["present"] is True and "start" in out["reason"]


def test_node_listen_call_counts_as_runnable(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {}}')
    (tmp_path / "index.js").write_text("const app = require('express')(); app.listen(3000);")
    out = analysis.check_has_runnable_entrypoint(tmp_path, "node")
    assert out["present"] is True and "index.js" in out["reason"]


def test_node_library_with_neither_is_not_runnable(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "express", "scripts": {"test": "mocha"}}')
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "express.js").write_text("exports = module.exports = function() {};")
    out = analysis.check_has_runnable_entrypoint(tmp_path, "node")
    assert out["present"] is False and "library" in out["reason"]


def test_listen_call_only_in_examples_or_tests_does_not_count(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "express", "scripts": {}}')
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "demo.js").write_text("app.listen(3000);")
    tests = tmp_path / "test"
    tests.mkdir()
    (tests / "app.test.js").write_text("app.listen(0);")
    out = analysis.check_has_runnable_entrypoint(tmp_path, "node")
    assert out["present"] is False


def test_node_no_package_json_falls_back_to_listen_scan(tmp_path):
    (tmp_path / "server.js").write_text("app.listen(8080);")
    assert analysis.check_has_runnable_entrypoint(tmp_path, "node")["present"] is True

def test_listen_inside_a_jsdoc_comment_does_not_count(tmp_path):
    """Regression: expressjs/express's own lib/application.js documents usage with
    '*    http.createServer(app).listen(80);' inside a /** */ comment - this is not a real entry point."""
    (tmp_path / "package.json").write_text('{"name": "express", "scripts": {"test": "mocha"}}')
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "application.js").write_text(
        "/**\n"
        " * A node `http.Server` is returned...\n"
        " *\n"
        " *    http.createServer(app).listen(80);\n"
        " *    https.createServer({ ... }, app).listen(443);\n"
        " *\n"
        " * @return {http.Server}\n"
        " */\n"
        "exports = module.exports = function() {};\n"
    )
    out = analysis.check_has_runnable_entrypoint(tmp_path, "node")
    assert out["present"] is False and "library" in out["reason"]


def test_line_commented_listen_call_does_not_count(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {}}')
    (tmp_path / "index.js").write_text("// app.listen(3000);\nmodule.exports = {};")
    assert analysis.check_has_runnable_entrypoint(tmp_path, "node")["present"] is False


def test_commented_out_python_run_call_does_not_count(tmp_path):
    (tmp_path / "app.py").write_text("# app.run(host='0.0.0.0')\ndef helper():\n    pass\n")
    out = analysis.check_has_runnable_entrypoint(tmp_path, "python")
    assert out["present"] is False


def test_python_flask_style_run_call_is_runnable(tmp_path):
    (tmp_path / "app.py").write_text("app = Flask(__name__)\napp.run(host='0.0.0.0')")
    assert analysis.check_has_runnable_entrypoint(tmp_path, "python")["present"] is True


def test_python_uvicorn_run_is_runnable(tmp_path):
    (tmp_path / "main.py").write_text("uvicorn.run(app, host='0.0.0.0', port=8000)")
    assert analysis.check_has_runnable_entrypoint(tmp_path, "python")["present"] is True


def test_python_manage_py_is_runnable(tmp_path):
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n")
    assert analysis.check_has_runnable_entrypoint(tmp_path, "python")["present"] is True


def test_python_library_with_no_run_call_is_not_runnable(tmp_path):
    (tmp_path / "mylib").mkdir()
    (tmp_path / "mylib" / "__init__.py").write_text("def helper():\n    return 1\n")
    out = analysis.check_has_runnable_entrypoint(tmp_path, "python")
    assert out["present"] is False and "library" in out["reason"]


def test_php_is_always_treated_as_runnable(tmp_path):
    assert analysis.check_has_runnable_entrypoint(tmp_path, "php")["present"] is True


def test_reachable_app_is_always_success():
    status, exec_ok, treated = _web_app_verdict(True, {"present": False, "reason": "n/a"})
    assert (status, exec_ok, treated) == ("success", True, False)


def test_unreachable_library_is_success_not_failed():
    status, exec_ok, treated = _web_app_verdict(False, {"present": False, "reason": "looks like a library"})
    assert (status, exec_ok, treated) == ("success", True, True)


def test_unreachable_app_with_an_entrypoint_still_fails():
    status, exec_ok, treated = _web_app_verdict(False, {"present": True, "reason": "has a start script"})
    assert (status, exec_ok, treated) == ("failed", False, False)


def test_verdict_defaults_to_present_when_entrypoint_check_is_empty():
    status, exec_ok, treated = _web_app_verdict(False, {})
    assert (status, exec_ok, treated) == ("failed", False, False)
