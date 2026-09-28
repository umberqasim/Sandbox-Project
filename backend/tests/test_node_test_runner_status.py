from app import sandbox_engine, testing_engine


def test_node_project_without_test_script_is_reported_not_run(monkeypatch):
    commands = []

    def fake_run(client, image_tag, command, **kwargs):
        commands.append(command)
        return {"ran": True, "passed": True, "exit_code": 86,
                "output": "SANDBOX_NO_NPM_TEST_SCRIPT"}

    monkeypatch.setattr(testing_engine, "_run_test_container", fake_run)
    result = testing_engine.run_tests(object(), "image:tag", True, project_type="node")
    assert "SANDBOX_NO_NPM_TEST_SCRIPT" in commands[0]
    assert "&& npm test" in commands[0]
    assert "--if-present" not in commands[0]
    assert result["ran"] is False
    assert result["reason"] == "package.json has no test script"
    assert "no test suite was executed" in result["output"]
    assert result["network_used"] is False


def test_node_project_with_test_script_keeps_real_failure(monkeypatch):
    monkeypatch.setattr(testing_engine, "_run_test_container", lambda *args, **kwargs: {
        "ran": True, "passed": False, "exit_code": 1, "output": "test failed"
    })
    result = testing_engine.run_tests(object(), "image:tag", True, project_type="node")
    assert result["ran"] is True
    assert result["passed"] is False
    assert result["exit_code"] == 1


def test_docker_build_has_memory_and_swap_caps():
    assert sandbox_engine._build_container_limits() == {
        "memory": sandbox_engine.MAX_BUILD_MEMORY_MB * 1024 * 1024,
        "memswap": sandbox_engine.MAX_BUILD_MEMORY_MB * 1024 * 1024,
    }


def test_node_test_process_exit_86_is_not_confused_with_missing_script(monkeypatch):
    monkeypatch.setattr(testing_engine, "_run_test_container", lambda *args, **kwargs: {
        "ran": True, "passed": False, "exit_code": 86, "output": "test process exited 86"
    })
    result = testing_engine.run_tests(object(), "image:tag", True, project_type="node")
    assert result["ran"] is True
    assert result["passed"] is False
    assert result["exit_code"] == 86
