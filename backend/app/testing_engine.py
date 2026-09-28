"""
Dynamic analysis - unlike analysis.py, these checks actually execute
submitted code, so they run under the same sandbox isolation rules as
the main execution step (resource limits, timeouts, always cleaned up).

Three things live here:
  - run_tests(): if the submission has a tests/ folder, run its test
    suite (pytest/phpunit/npm test/flutter test) inside an isolated,
    network-disabled container
  - check_api_health(): if the submission looks like a web API, start
    it on an internet-isolated internal Docker network, probe a health
    endpoint, then run a lightweight UI smoke check and a small load
    test on the same running container before cleanup
"""

import math
import os
import time
import statistics

import docker
import requests
from docker.errors import NotFound, APIError

from . import janitor

MAX_TEST_SECONDS = int(os.environ.get("MAX_TEST_SECONDS", 60))
MAX_MEMORY_MB = int(os.environ.get("MAX_MEMORY_MB", 512))
INTERNAL_NETWORK_NAME = "sandbox-internal-net"

COMMON_HEALTH_PATHS = ["/health", "/healthz", "/api/health", "/"]
COMMON_PORTS = [8000, 5000, 3000, 8080]
# Injected into the sandboxed app: many frameworks honour PORT (Heroku-style contract).
DEFAULT_APP_PORT = 8000
# Each attempt waits 2s; slower stacks (Laravel, Django migrations) need a longer window.
STARTUP_ATTEMPTS = int(os.environ.get("API_STARTUP_ATTEMPTS", 10))

LOAD_TEST_REQUESTS = 15
PIDS_LIMIT = int(os.environ.get("MAX_PIDS", 256))

# Heavy (runtime) auth/DB check: conventional endpoint names to try on the already-running health-check
# container. A guessed path that 404s/405s everywhere is inconclusive, not a failure - see
# sandbox_engine._apply_auth_db_probe_evidence for how this evidence is (only ever additively) used.
AUTH_PROBE_REGISTER_PATHS = ["/register", "/signup", "/api/register", "/api/signup", "/auth/register", "/auth/signup"]
AUTH_PROBE_LOGIN_PATHS = ["/login", "/signin", "/api/login", "/api/signin", "/auth/login", "/auth/signin"]
AUTH_PROBE_PROTECTED_PATHS = ["/me", "/profile", "/api/me", "/api/profile", "/dashboard", "/api/user"]
AUTH_PROBE_TIMEOUT_SECONDS = 3
AUTH_PROBE_REGISTER_BODY = {
    "username": "sandbox_probe_user", "email": "sandbox_probe@example.com", "password": "Sandbox!Probe123",
}
AUTH_PROBE_LOGIN_BODY = {"username": "sandbox_probe_user", "password": "definitely-wrong-password"}
# Substrings seen in real driver/ORM error messages when the app cannot reach a database at all -
# used only to *explain* a register-endpoint crash, never to lower a score by itself (the sandbox
# provides no real database service, so this is expected for many otherwise-fine submissions).
DB_ERROR_SIGNATURES = (
    "econnrefused", "etimedout", "enotfound", "sequelizeconnectionerror", "mongonetworkerror",
    "mongooseserverselectionerror", "operationalerror", "sqlstate", "could not connect to server",
    "connection refused", "p1001", "ora-", "econnreset", "database system is starting up",
)


def _looks_like_db_error(text: str) -> bool:
    lowered = (text or "").lower()
    return any(sig in lowered for sig in DB_ERROR_SIGNATURES)


def _probe_first_answer(base_url: str, paths, method: str, json_body: dict = None):
    """Try each path until one answers with anything other than a connection error, 404 or 405 ('nothing
    registered here'). Returns (path, response) for the first such answer, or (None, None)."""
    call = requests.post if method == "post" else requests.get
    for path in paths:
        try:
            kwargs = {"timeout": AUTH_PROBE_TIMEOUT_SECONDS}
            if json_body is not None:
                kwargs["json"] = json_body
            resp = call(f"{base_url.rstrip(chr(47))}{path}", **kwargs)
        except requests.RequestException:
            continue
        if resp.status_code in (404, 405):
            continue
        return path, resp
    return None, None


def probe_auth_and_db(base_url: str) -> dict:
    """
    Best-effort runtime evidence for authentication_flow and database_connectivity: hits a handful of
    conventional register/login/protected-route paths on the already-running health-check container.
    Never raises; a path that answers nowhere leaves the corresponding key None (inconclusive).
    """
    result = {"attempted": True, "register": None, "login": None, "protected_route": None}
    try:
        reg_path, reg_resp = _probe_first_answer(base_url, AUTH_PROBE_REGISTER_PATHS, "post",
                                                 AUTH_PROBE_REGISTER_BODY)
        if reg_path:
            body = reg_resp.text[:2000]
            result["register"] = {
                "path": reg_path, "status_code": reg_resp.status_code,
                "looks_like_db_error": reg_resp.status_code >= 500 and _looks_like_db_error(body),
            }

        login_path, login_resp = _probe_first_answer(base_url, AUTH_PROBE_LOGIN_PATHS, "post",
                                                      AUTH_PROBE_LOGIN_BODY)
        if login_path:
            result["login"] = {
                "path": login_path, "status_code": login_resp.status_code,
                "rejected_bad_credentials": login_resp.status_code in (400, 401, 403, 422),
            }

        protected_path, protected_resp = _probe_first_answer(base_url, AUTH_PROBE_PROTECTED_PATHS, "get")
        if protected_path:
            result["protected_route"] = {
                "path": protected_path, "status_code": protected_resp.status_code,
                "requires_auth": protected_resp.status_code in (401, 403),
            }
    except Exception:  # noqa: BLE001 - this probe is pure evidence-gathering; it must never break an evaluation
        result["error"] = "auth/db probe failed unexpectedly"
    return result


def _hardening_kwargs() -> dict:
    """Common container limits: memory (no swap), CPU, PIDs, no capabilities."""
    return {
        "mem_limit": f"{MAX_MEMORY_MB}m",
        "memswap_limit": f"{MAX_MEMORY_MB}m",  # no swap -> can't push the host into swapping
        "nano_cpus": int(0.5 * 1e9),
        "pids_limit": PIDS_LIMIT,              # blocks fork bombs
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "labels": janitor.managed_labels(),    # lets the janitor find leftovers after a crash
    }


def _run_test_container(client: "docker.DockerClient", image_tag: str, command: str, network_disabled: bool,
                         extra_env: dict = None) -> dict:
    container = None
    try:
        container = client.containers.run(
            image=image_tag,
            command=["sh", "-c", command],
            detach=True,
            network_disabled=network_disabled,
            environment=extra_env or None,
            **_hardening_kwargs(),
        )
        try:
            exit_status = container.wait(timeout=MAX_TEST_SECONDS)
            output = container.logs().decode(errors="replace")
            code = exit_status.get("StatusCode")
            return {"ran": True, "passed": code == 0, "exit_code": code, "output": output[-4000:]}
        except Exception:
            return {
                "ran": True, "passed": False, "timed_out": True,
                "output": f"Test run did not finish within {MAX_TEST_SECONDS}s (or the container errored)",
            }
    except APIError as e:
        return {"ran": False, "reason": f"could not start test container: {e}"}
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


def run_tests(client: "docker.DockerClient", image_tag: str, has_tests: bool, project_type: str = "python",
              test_target: str = None, extra_env: dict = None) -> dict:
    """
    Run the submission's test suite in an isolated container.

    `test_target` is the folder (tests/, test/, __tests__/) or "." for loose test
    files, as found by analysis.find_test_target().

    Tests run with the network DISABLED. Only if a Python submission
    doesn't ship pytest ("No module named pytest") do we retry once with
    network enabled so the trusted `pip install pytest` step can work -
    and the result is flagged with network_used=True so it's visible.
    """
    if not has_tests:
        return {"ran": False, "reason": "no test folder or test files found"}

    py_target = "" if test_target in (None, ".") else test_target

    if project_type == "node":
        # npm's --if-present exits successfully when scripts.test is absent. Use a
        # reserved exit code to report an absent test runner as "not run", never "passed".
        command = """node -e "try { const p = require('./package.json'); if (p.scripts && p.scripts.test) process.exit(0) } catch (_) {} console.log('SANDBOX_NO_NPM_TEST_SCRIPT'); process.exit(86)" && npm test"""
    elif project_type == "php":
        command = (
            "vendor/bin/phpunit --version >/dev/null 2>&1 && vendor/bin/phpunit || "
            "(echo 'phpunit not available or not installed' && exit 1)"
        )
    elif project_type == "flutter":
        command = "flutter test || (echo 'flutter test failed or no tests found' && exit 1)"
    else:
        command = f"python -m pytest -q --no-header -p no:cacheprovider {py_target}".strip()

    result = _run_test_container(client, image_tag, command, network_disabled=True, extra_env=extra_env)
    result["network_used"] = False

    if (project_type == "node" and result.get("ran") and result.get("exit_code") == 86
            and "SANDBOX_NO_NPM_TEST_SCRIPT" in result.get("output", "")):
        return {
            "ran": False,
            "reason": "package.json has no test script",
            "output": "No npm test script found; no test suite was executed.",
            "network_used": False,
        }

    needs_pytest = (
        project_type not in ("node", "php", "flutter")
        and result.get("ran")
        and not result.get("passed")
        and "No module named pytest" in result.get("output", "")
    )
    if needs_pytest:
        retry_cmd = (
            f"pip install --quiet pytest && python -m pytest -q --no-header -p no:cacheprovider {py_target}"
        ).strip()
        result = _run_test_container(client, image_tag, retry_cmd, network_disabled=False, extra_env=extra_env)
        result["network_used"] = True

    # pytest exit code 5 = "no tests collected": that is not a failing suite.
    if project_type not in ("node", "php", "flutter") and result.get("exit_code") == 5:
        result = {
            "ran": False, "reason": "pytest collected no tests",
            "network_used": result.get("network_used", False),
        }

    return result


def _ensure_internal_network(client: "docker.DockerClient"):
    try:
        return client.networks.get(INTERNAL_NETWORK_NAME)
    except NotFound:
        return client.networks.create(INTERNAL_NETWORK_NAME, driver="bridge", internal=True)


def _ensure_backend_connected(client: "docker.DockerClient", network):
    hostname = os.environ.get("HOSTNAME")
    if not hostname:
        return
    try:
        network.connect(hostname)
    except APIError:
        pass


def _run_ui_smoke_check(base_url: str) -> dict:
    """
    Lightweight smoke check - not a real browser render (Playwright/
    Selenium would need 300-500MB+ RAM we don't have to spare on this
    dev machine). Passes when the root URL answers without an error
    status. Whether the body is HTML is reported separately: API-only
    apps legitimately answer with JSON or plain text.
    """
    try:
        resp = requests.get(base_url, timeout=3)
        body_lower = resp.text.lower()
        looks_like_html = "<html" in body_lower or "<!doctype html" in body_lower
        result = {
            "ran": True,
            "status_code": resp.status_code,
            "returns_html": looks_like_html,
            "passed": resp.status_code < 400,
        }
        if resp.status_code < 400 and not looks_like_html:
            result["note"] = "Root responds but not with an HTML page (API-only or plain-text app)"
        return result
    except requests.RequestException as e:
        return {"ran": True, "passed": False, "error": str(e)}


def _run_lightweight_load_test(base_url: str) -> dict:
    """
    Small burst of sequential requests against the already-running
    container to get real latency numbers - not a substitute for a
    proper load-testing tool (k6/Locust), but gives an honest
    performance signal without needing spare resources to run a
    separate load generator on this machine.
    """
    latencies = []
    successes = 0
    for _ in range(LOAD_TEST_REQUESTS):
        t0 = time.time()
        try:
            resp = requests.get(base_url, timeout=3)
            latencies.append(time.time() - t0)
            if resp.status_code < 500:
                successes += 1
        except requests.RequestException:
            latencies.append(3.0)

    return {
        "requests_sent": LOAD_TEST_REQUESTS,
        "success_rate_percent": round((successes / LOAD_TEST_REQUESTS) * 100, 1),
        "avg_latency_ms": round(statistics.mean(latencies) * 1000, 1) if latencies else None,
        "max_latency_ms": round(max(latencies) * 1000, 1) if latencies else None,
        "p95_latency_ms": (
            round(sorted(latencies)[max(math.ceil(len(latencies) * 0.95) - 1, 0)] * 1000, 1) if latencies else None
        ),
    }


# Bonus: Resource Usage Monitoring (case study bonus challenges list).
def _capture_resource_usage(container) -> dict:
    """
    One-shot CPU/memory snapshot of the already-running sandbox container, taken via the Docker
    stats API right after the load test - so there is some real activity to measure instead of an
    idle reading. Best-effort and never raises: a submission that behaves badly enough to break
    stats collection should not also break its evaluation.

    CPU % follows Docker's own formula (delta of container CPU time over delta of host CPU time,
    scaled by online CPU count) using the single stats sample's built-in cpu_stats/precpu_stats
    pair - no need for a second network round trip just to sample twice.

    Memory is reported against MAX_MEMORY_MB (the hardening limit every sandbox container is
    started with - see _hardening_kwargs), not the host's total RAM, since the limit that
    actually matters here is the sandbox's own cap. Page cache is subtracted from the raw
    cgroup usage figure where available, matching what `docker stats` itself shows, so a
    submission is not penalised for the kernel's disk cache.
    """
    try:
        stats = container.stats(stream=False)
    except Exception:
        return {"captured": False, "reason": "stats unavailable"}

    result = {"captured": True}

    try:
        mem_stats = stats.get("memory_stats", {}) or {}
        mem_usage = mem_stats.get("usage")
        inner_stats = mem_stats.get("stats") or {}
        # cgroup v1 reports "cache", cgroup v2 reports "inactive_file" - either is close enough
        # to "page cache that isn't really this app's own memory pressure".
        cache = inner_stats.get("cache", inner_stats.get("inactive_file", 0)) or 0
        if mem_usage is not None:
            net_usage = max(mem_usage - cache, 0)
            result["memory_usage_mb"] = round(net_usage / (1024 * 1024), 2)
            result["memory_limit_mb"] = MAX_MEMORY_MB
            result["memory_percent"] = round((net_usage / (MAX_MEMORY_MB * 1024 * 1024)) * 100, 2)
    except Exception:  # noqa: BLE001 - resource monitoring must never fail the evaluation
        pass

    try:
        cpu_stats = stats.get("cpu_stats", {}) or {}
        precpu_stats = stats.get("precpu_stats", {}) or {}
        cpu_delta = (cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
                     - precpu_stats.get("cpu_usage", {}).get("total_usage", 0))
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)
        online_cpus = (cpu_stats.get("online_cpus")
                       or len(cpu_stats.get("cpu_usage", {}).get("percpu_usage") or []) or 1)
        # A submission queried right after it starts (before the daemon has two samples to diff)
        # legitimately has no delta yet - that is "captured: True" with no cpu_percent, not an error.
        if system_delta > 0 and cpu_delta >= 0:
            result["cpu_percent"] = round((cpu_delta / system_delta) * online_cpus * 100, 2)
    except Exception:  # noqa: BLE001
        pass

    return result


# ---------------------------------------------------------- port discovery

_LOOPBACK_V6 = "00000000000000000000000001000000"
_MAPPED_V4_PREFIX = "0000000000000000FFFF0000"  # ::ffff:a.b.c.d
# 127.0.0.11 = Docker's embedded DNS resolver. It listens inside every container on a user-defined
# network, but it is not the application - it must never be probed or reported as "the app's port".
_DOCKER_DNS_V4 = "0B00007F"


def _is_loopback(ip_hex: str) -> bool:
    """/proc/net/tcp prints IPv4 little-endian, so any 127.x.y.z address ends in '7F'."""
    ip_hex = ip_hex.upper()
    if len(ip_hex) == 8:
        return ip_hex.endswith("7F")
    return ip_hex == _LOOPBACK_V6 or (ip_hex.startswith(_MAPPED_V4_PREFIX) and ip_hex.endswith("7F"))


def _parse_listening_sockets(proc_net_text: str) -> list:
    """
    Parse /proc/net/tcp or /proc/net/tcp6 into [(port, reachable_from_outside)].
    State 0A = LISTEN. An address bound to loopback only (127.0.0.1 / ::1) can't be
    reached from another container - a very common mistake (e.g. Flask's app.run()).
    """
    sockets = []
    for line in proc_net_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "0A" or ":" not in parts[1]:
            continue
        ip_hex, port_hex = parts[1].rsplit(":", 1)
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        if ip_hex.upper() == _DOCKER_DNS_V4:
            continue
        sockets.append((port, not _is_loopback(ip_hex)))
    return sockets


def _discover_listening_ports(container):
    """
    Ask the running container which TCP ports it listens on.
    Returns (sockets, error). `error` says why nothing could be read, so a failing discovery is
    visible in the result instead of silently degrading to guessing ports.
    """
    found = []
    error = None
    for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            exit_code, output = container.exec_run(["cat", proc_file])
            if exit_code == 0:
                found.extend(_parse_listening_sockets(output.decode(errors="replace")))
            else:
                error = f"cat {proc_file} exited with {exit_code}"
        except Exception as exc:  # noqa: BLE001 - diagnostics only, must never break the evaluation
            error = f"{type(exc).__name__}: {exc}"[:200]
    return found, (error if not found else None)


def _exposed_ports(client, image_tag: str) -> list:
    """Ports declared with EXPOSE in the image (e.g. '3001/tcp')."""
    try:
        exposed = client.images.get(image_tag).attrs.get("Config", {}).get("ExposedPorts") or {}
        return [int(k.split("/")[0]) for k in exposed if k.split("/")[0].isdigit()]
    except Exception:
        return []


def _candidate_ports(listening: list, exposed: list) -> list:
    """Order: ports the app really listens on (reachable ones), then EXPOSEd, then common guesses."""
    ordered = [p for p, reachable in listening if reachable]
    if ordered:
        return list(dict.fromkeys(ordered))  # we know what is listening - no need to guess
    return list(dict.fromkeys(exposed + [DEFAULT_APP_PORT] + COMMON_PORTS))


def check_api_health(client: "docker.DockerClient", image_tag: str, looks_like_api: bool,
                     extra_env: dict = None) -> dict:
    """
    If the submission looks like a web API, start it in isolation and
    try to hit a health endpoint - with retries, since apps can take a
    few seconds to actually start listening. On success, also runs a
    lightweight UI smoke check and load test on the same running
    container before cleanup. Container never gets internet access -
    only reachable from our own backend.

    Port detection: instead of only guessing 8000/5000/3000/8080 we read the
    container's listening sockets and its EXPOSE list, and pass PORT=8000 to the app.
    """
    if not looks_like_api:
        return {"checked": False, "reason": "no web framework detected"}

    network = _ensure_internal_network(client)
    _ensure_backend_connected(client, network)
    exposed = _exposed_ports(client, image_tag)

    container = None
    try:
        container = client.containers.run(
            image=image_tag,
            detach=True,
            network=INTERNAL_NETWORK_NAME,
            # The submission's own .env.example values first, PORT last so our port contract always wins
            # over anything (unlikely but possible) declared as PORT in the submission's own file.
            environment={**(extra_env or {}), "PORT": str(DEFAULT_APP_PORT)},
            **_hardening_kwargs(),
        )

        ip = None
        attempts_tried = []
        listening = []
        discovery_error = None

        for attempt in range(STARTUP_ATTEMPTS):
            time.sleep(2)
            container.reload()

            if container.status == "exited":
                logs_tail = container.logs(tail=30).decode(errors="replace")
                return {
                    "checked": True, "reachable": False,
                    "reason": "container exited before responding",
                    "container_logs_tail": logs_tail,
                }

            networks = container.attrs["NetworkSettings"]["Networks"]
            ip = networks.get(INTERNAL_NETWORK_NAME, {}).get("IPAddress")
            if not ip:
                continue

            listening, discovery_error = _discover_listening_ports(container)
            for port in _candidate_ports(listening, exposed):
                for path in COMMON_HEALTH_PATHS:
                    attempts_tried.append(f"{ip}:{port}{path}")
                    try:
                        resp = requests.get(f"http://{ip}:{port}{path}", timeout=2)
                        base_url = f"http://{ip}:{port}/"
                        return {
                            "checked": True,
                            "reachable": True,
                            "endpoint": f"{path} on port {port}",
                            "port": port,
                            "status_code": resp.status_code,
                            "attempts_before_success": attempt + 1,
                            "ui_smoke_check": _run_ui_smoke_check(base_url),
                            "load_test": _run_lightweight_load_test(base_url),
                            "auth_db_probe": probe_auth_and_db(base_url),
                            "resource_usage": _capture_resource_usage(container),
                        }
                    except requests.RequestException:
                        continue

        result = {
            "checked": True, "reachable": False,
            "reason": "no response on the app's ports after retries",
            "last_known_ip": ip,
            "ports_paths_tried": attempts_tried[-8:],
        }
        loopback_only = [p for p, reachable in listening if not reachable]
        if loopback_only and not any(reachable for _, reachable in listening):
            result["reason"] = (
                f"app listens on localhost only (port {loopback_only[0]}) so it is unreachable from other "
                f"containers - bind to 0.0.0.0 instead of 127.0.0.1"
            )
            result["listening_ports"] = loopback_only
        elif listening:
            result["listening_ports"] = [p for p, _ in listening]
        elif discovery_error:
            result["port_discovery_error"] = discovery_error
        try:  # what the app printed usually explains why it is not answering
            result["container_logs_tail"] = container.logs(tail=20).decode(errors="replace")[-1500:]
        except Exception:  # noqa: BLE001
            pass
        return result

    except APIError as e:
        return {"checked": True, "reachable": False, "reason": f"could not start container: {e}"}
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
