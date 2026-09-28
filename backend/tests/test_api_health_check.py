"""check_api_health with a fake Docker client - verifies port discovery without needing Docker."""
import requests

from app import testing_engine

LISTEN_ALL_3001 = """  sl  local_address rem_address   st
   0: 00000000:0BB9 00000000:0000 0A 00000000:00000000
"""
LISTEN_LOOPBACK_5000 = """  sl  local_address rem_address   st
   0: 0100007F:1388 00000000:0000 0A 00000000:00000000
"""


class FakeContainer:
    status = "running"

    def __init__(self, proc_net):
        self.proc_net = proc_net
        net = {testing_engine.INTERNAL_NETWORK_NAME: {"IPAddress": "10.0.0.5"}}
        self.attrs = {"NetworkSettings": {"Networks": net}}
        self.env = None

    def reload(self): pass
    def stop(self, timeout=5): pass
    def remove(self, force=True): pass
    def logs(self, tail=30): return b""

    def exec_run(self, cmd):
        return (0, self.proc_net.encode()) if cmd[-1] == "/proc/net/tcp" else (1, b"")


class FakeClient:
    def __init__(self, container):
        self._container = container
        net = type("Net", (), {"connect": lambda s, h: None})()
        img = type("Img", (), {"attrs": {"Config": {"ExposedPorts": {}}}})()
        self.networks = type("N", (), {"get": lambda self_, name: net})()
        self.images = type("I", (), {"get": lambda self_, tag: img})()
        self.containers = type("C", (), {"run": lambda self_, **kw: self._run(kw)})()
        self.run_kwargs = None

    def _run(self, kw):
        self.run_kwargs = kw
        return self._container


def _patch(monkeypatch, urls_hit):
    monkeypatch.setattr(testing_engine.time, "sleep", lambda s: None)
    monkeypatch.setattr(testing_engine, "STARTUP_ATTEMPTS", 2)

    class Resp:
        status_code = 200
        text = "<html>hi</html>"

    def fake_get(url, timeout=0):
        urls_hit.append(url)
        if ":3001" in url:
            return Resp()
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(testing_engine.requests, "get", fake_get)


def test_finds_app_on_non_standard_port(monkeypatch):
    hit = []
    _patch(monkeypatch, hit)
    client = FakeClient(FakeContainer(LISTEN_ALL_3001))
    result = testing_engine.check_api_health(client, "img:tag", looks_like_api=True)
    assert result["reachable"] is True and result["port"] == 3001
    assert client.run_kwargs["environment"] == {"PORT": "8000"}
    assert not any(":8000" in u or ":5000" in u for u in hit)  # no blind guessing when the port is known


def test_localhost_only_binding_gets_actionable_reason(monkeypatch):
    hit = []
    _patch(monkeypatch, hit)
    result = testing_engine.check_api_health(FakeClient(FakeContainer(LISTEN_LOOPBACK_5000)), "img:tag", True)
    assert result["reachable"] is False
    assert "0.0.0.0" in result["reason"] and result["listening_ports"] == [5000]


def test_flask_bound_to_localhost_inside_docker_is_diagnosed(monkeypatch):
    """Regression: Docker's DNS (127.0.0.11) used to count as 'the app's reachable port', hiding the diagnosis."""
    proc = (
        "  sl  local_address rem_address   st\n"
        "   0: 0B00007F:A2C3 00000000:0000 0A 00000000:00000000\n"   # Docker embedded DNS
        "   1: 0100007F:1388 00000000:0000 0A 00000000:00000000\n"   # Flask on 127.0.0.1:5000
    )
    container = FakeContainer(proc)
    container.logs = lambda tail=30: b" * Running on http://127.0.0.1:5000\n"
    _patch(monkeypatch, [])
    result = testing_engine.check_api_health(FakeClient(container), "img:tag", True)
    assert result["reachable"] is False
    assert "0.0.0.0" in result["reason"] and result["listening_ports"] == [5000]
    assert "127.0.0.1:5000" in result["container_logs_tail"]


def test_failed_port_discovery_is_reported_not_silent(monkeypatch):
    container = FakeContainer("")
    container.exec_run = lambda cmd: (126, b"permission denied")
    _patch(monkeypatch, [])
    result = testing_engine.check_api_health(FakeClient(container), "img:tag", True)
    assert result["reachable"] is False and "exited with 126" in result["port_discovery_error"]
