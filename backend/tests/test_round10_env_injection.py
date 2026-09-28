"""
.env.example values are now given to the sandbox container (Python/Node only) instead of never
reaching it. analysis.parse_env_example() is pure and directly testable; the testing_engine/
sandbox_engine wiring is tested with a fake Docker client (no real Docker needed).
"""
import pytest

from app import analysis, testing_engine


def test_basic_key_value_pairs_are_parsed(tmp_path):
    (tmp_path / ".env.example").write_text("DATABASE_URL=postgres://localhost/db\nDEBUG=true\n")
    assert analysis.parse_env_example(tmp_path) == {"DATABASE_URL": "postgres://localhost/db", "DEBUG": "true"}


def test_comments_and_blank_lines_are_ignored(tmp_path):
    (tmp_path / ".env.example").write_text("# a comment\n\nFOO=bar\n   # indented comment\nBAZ=qux\n")
    assert analysis.parse_env_example(tmp_path) == {"FOO": "bar", "BAZ": "qux"}


def test_export_prefix_and_quotes_are_stripped(tmp_path):
    (tmp_path / ".env.example").write_text('export NAME="hello world"\nOTHER=\'single quoted\'\n')
    assert analysis.parse_env_example(tmp_path) == {"NAME": "hello world", "OTHER": "single quoted"}


def test_value_with_equals_sign_is_kept_whole(tmp_path):
    (tmp_path / ".env.example").write_text("JWT_SECRET=abc=def=ghi\n")
    assert analysis.parse_env_example(tmp_path) == {"JWT_SECRET": "abc=def=ghi"}


def test_key_with_invalid_characters_is_skipped(tmp_path):
    (tmp_path / ".env.example").write_text("VALID_KEY=1\nnot a valid key=2\nALSO-INVALID=3\n")
    assert analysis.parse_env_example(tmp_path) == {"VALID_KEY": "1"}


def test_missing_file_returns_empty_dict(tmp_path):
    assert analysis.parse_env_example(tmp_path) == {}


def test_symlinked_env_example_is_ignored(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("SECRET=hax\n")
    (tmp_path / ".env.example").symlink_to(target)
    assert analysis.parse_env_example(tmp_path) == {}


def test_value_is_truncated_at_the_length_cap(tmp_path):
    long_value = "x" * 5000
    (tmp_path / ".env.example").write_text(f"BIG={long_value}\n")
    result = analysis.parse_env_example(tmp_path)
    assert len(result["BIG"]) == analysis.MAX_ENV_VALUE_LENGTH


def test_number_of_variables_is_capped(tmp_path):
    lines = "\n".join(f"VAR_{i}=v" for i in range(analysis.MAX_INJECTED_ENV_VARS + 20))
    (tmp_path / ".env.example").write_text(lines)
    assert len(analysis.parse_env_example(tmp_path)) == analysis.MAX_INJECTED_ENV_VARS


def test_empty_value_is_allowed(tmp_path):
    (tmp_path / ".env.example").write_text("EMPTY=\nFILLED=x\n")
    assert analysis.parse_env_example(tmp_path) == {"EMPTY": "", "FILLED": "x"}


class _FakeContainer:
    def __init__(self):
        self.status = "running"
        self.attrs = {"NetworkSettings": {"Networks": {}, "Ports": {}}}

    def wait(self, timeout=None):
        return {"StatusCode": 0}

    def logs(self, **kw):
        return b""

    def reload(self):
        pass

    def stop(self, timeout=None):
        pass

    def remove(self, force=None):
        pass


class _FakeClient:
    def __init__(self):
        self.containers = self
        self.last_run_kwargs = None

    def run(self, **kwargs):
        self.last_run_kwargs = kwargs
        return _FakeContainer()


def test_run_tests_passes_extra_env_into_the_container(monkeypatch):
    client = _FakeClient()
    testing_engine.run_tests(client, "img:tag", has_tests=True, project_type="python", test_target=".",
                             extra_env={"DATABASE_URL": "sqlite:///x"})
    assert client.last_run_kwargs["environment"] == {"DATABASE_URL": "sqlite:///x"}


def test_run_tests_with_no_extra_env_passes_none(monkeypatch):
    client = _FakeClient()
    testing_engine.run_tests(client, "img:tag", has_tests=True, project_type="node", test_target=".")
    assert client.last_run_kwargs["environment"] is None


def test_check_api_health_merges_extra_env_with_port(monkeypatch):
    monkeypatch.setattr(testing_engine.time, "sleep", lambda s: None)
    monkeypatch.setattr(testing_engine, "_ensure_internal_network", lambda client: None)
    monkeypatch.setattr(testing_engine, "_ensure_backend_connected", lambda client, network: None)
    monkeypatch.setattr(testing_engine, "_exposed_ports", lambda client, tag: [])
    client = _FakeClient()
    testing_engine.check_api_health(client, "img:tag", looks_like_api=True,
                                    extra_env={"NODE_ENV": "production", "PORT": "9999"})
    env = client.last_run_kwargs["environment"]
    assert env["NODE_ENV"] == "production"
    assert env["PORT"] == str(testing_engine.DEFAULT_APP_PORT)


def test_check_api_health_without_extra_env_still_sets_port(monkeypatch):
    monkeypatch.setattr(testing_engine.time, "sleep", lambda s: None)
    monkeypatch.setattr(testing_engine, "_ensure_internal_network", lambda client: None)
    monkeypatch.setattr(testing_engine, "_ensure_backend_connected", lambda client, network: None)
    monkeypatch.setattr(testing_engine, "_exposed_ports", lambda client, tag: [])
    client = _FakeClient()
    testing_engine.check_api_health(client, "img:tag", looks_like_api=True)
    assert client.last_run_kwargs["environment"] == {"PORT": str(testing_engine.DEFAULT_APP_PORT)}
