"""
Bonus: Resource Usage Monitoring. _capture_resource_usage() takes a one-shot Docker stats snapshot
of the already-running sandbox container right after the load test. It never raises and never
affects any score - purely additional information shown in the report/dashboard.
"""
import pytest

from app import testing_engine


class _StatsContainer:
    def __init__(self, stats=None, raises=None):
        self._stats = stats
        self._raises = raises

    def stats(self, stream=False):
        if self._raises:
            raise self._raises
        return self._stats


def _stats(mem_usage=None, cache_key=None, cache_value=0, cpu_delta=None, system_delta=None,
           online_cpus=2, percpu=None):
    memory_stats = {}
    if mem_usage is not None:
        memory_stats["usage"] = mem_usage
        memory_stats["stats"] = {cache_key: cache_value} if cache_key else {}
    cpu_stats, precpu_stats = {}, {}
    if cpu_delta is not None and system_delta is not None:
        cpu_stats = {"cpu_usage": {"total_usage": 1_000_000 + cpu_delta}, "system_cpu_usage": 5_000_000 + system_delta,
                     "online_cpus": online_cpus}
        precpu_stats = {"cpu_usage": {"total_usage": 1_000_000}, "system_cpu_usage": 5_000_000}
        if percpu is not None:
            cpu_stats["cpu_usage"]["percpu_usage"] = percpu
            del cpu_stats["online_cpus"]
    return {"memory_stats": memory_stats, "cpu_stats": cpu_stats, "precpu_stats": precpu_stats}


def test_stats_call_failure_is_reported_not_raised():
    container = _StatsContainer(raises=RuntimeError("docker daemon unreachable"))
    result = testing_engine._capture_resource_usage(container)
    assert result == {"captured": False, "reason": "stats unavailable"}


def test_memory_and_cpu_are_computed_from_a_normal_sample():
    stats = _stats(mem_usage=100 * 1024 * 1024, cache_key="cache", cache_value=20 * 1024 * 1024,
                   cpu_delta=200_000, system_delta=1_000_000, online_cpus=2)
    result = testing_engine._capture_resource_usage(_StatsContainer(stats=stats))
    assert result["captured"] is True
    assert result["memory_usage_mb"] == 80.0
    assert result["memory_limit_mb"] == testing_engine.MAX_MEMORY_MB
    assert result["memory_percent"] == round(80 * 1024 * 1024 / (testing_engine.MAX_MEMORY_MB * 1024 * 1024) * 100, 2)
    assert result["cpu_percent"] == round((200_000 / 1_000_000) * 2 * 100, 2)


def test_cgroup_v2_inactive_file_is_used_as_the_cache_fallback():
    stats = _stats(mem_usage=50 * 1024 * 1024, cache_key="inactive_file", cache_value=10 * 1024 * 1024)
    result = testing_engine._capture_resource_usage(_StatsContainer(stats=stats))
    assert result["memory_usage_mb"] == 40.0


def test_no_cache_key_at_all_uses_the_raw_usage_figure():
    stats = _stats(mem_usage=30 * 1024 * 1024)
    result = testing_engine._capture_resource_usage(_StatsContainer(stats=stats))
    assert result["memory_usage_mb"] == 30.0


def test_missing_memory_stats_leaves_memory_fields_out_but_still_captured():
    stats = {"memory_stats": {}, "cpu_stats": {}, "precpu_stats": {}}
    result = testing_engine._capture_resource_usage(_StatsContainer(stats=stats))
    assert result["captured"] is True
    assert "memory_usage_mb" not in result and "cpu_percent" not in result


def test_zero_system_delta_does_not_produce_a_cpu_percent():
    stats = _stats(cpu_delta=100_000, system_delta=0)
    result = testing_engine._capture_resource_usage(_StatsContainer(stats=stats))
    assert "cpu_percent" not in result


def test_negative_cpu_delta_is_ignored_not_reported_as_negative():
    stats = _stats(cpu_delta=-50_000, system_delta=1_000_000)
    result = testing_engine._capture_resource_usage(_StatsContainer(stats=stats))
    assert "cpu_percent" not in result


def test_online_cpus_falls_back_to_percpu_usage_length_when_absent():
    stats = _stats(cpu_delta=100_000, system_delta=1_000_000, percpu=[0, 0, 0, 0])
    result = testing_engine._capture_resource_usage(_StatsContainer(stats=stats))
    assert result["cpu_percent"] == round((100_000 / 1_000_000) * 4 * 100, 2)


def test_memory_usage_lower_than_cache_never_goes_negative():
    stats = _stats(mem_usage=5 * 1024 * 1024, cache_key="cache", cache_value=50 * 1024 * 1024)
    result = testing_engine._capture_resource_usage(_StatsContainer(stats=stats))
    assert result["memory_usage_mb"] == 0.0


_LISTEN_ALL_3001 = ("  sl  local_address rem_address   st\n"
                    "   0: 00000000:0BB9 00000000:0000 0A 00000000:00000000\n")


class _FakeContainer:
    status = "running"

    def __init__(self):
        net = {testing_engine.INTERNAL_NETWORK_NAME: {"IPAddress": "10.0.0.5"}}
        self.attrs = {"NetworkSettings": {"Networks": net}}

    def reload(self):
        pass

    def stop(self, timeout=5):
        pass

    def remove(self, force=True):
        pass

    def logs(self, tail=30):
        return b""

    def exec_run(self, cmd):
        return (0, _LISTEN_ALL_3001.encode()) if cmd[-1] == "/proc/net/tcp" else (1, b"")

    def stats(self, stream=False):
        return _stats(mem_usage=42 * 1024 * 1024)


class _FakeClient:
    def __init__(self, container):
        self._container = container
        net = type("Net", (), {"connect": lambda s, h: None})()
        img = type("Img", (), {"attrs": {"Config": {"ExposedPorts": {}}}})()
        self.networks = type("N", (), {"get": lambda self_, name: net})()
        self.images = type("I", (), {"get": lambda self_, tag: img})()
        self.containers = type("C", (), {"run": lambda self_, **kw: self._container})()


def test_check_api_health_includes_a_resource_usage_snapshot(monkeypatch):
    monkeypatch.setattr(testing_engine.time, "sleep", lambda s: None)
    monkeypatch.setattr(testing_engine, "STARTUP_ATTEMPTS", 2)

    class Resp:
        status_code = 200
        text = "ok"

    def fake_get(url, timeout=0):
        if ":3001" in url:
            return Resp()
        raise __import__("requests").ConnectionError("refused")

    monkeypatch.setattr(testing_engine.requests, "get", fake_get)
    monkeypatch.setattr(testing_engine.requests, "post", lambda *a, **k: Resp())

    client = _FakeClient(_FakeContainer())
    result = testing_engine.check_api_health(client, "img:tag", looks_like_api=True)
    assert result["reachable"] is True
    assert result["resource_usage"]["captured"] is True
    assert result["resource_usage"]["memory_usage_mb"] == 42.0
