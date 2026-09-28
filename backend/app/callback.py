"""
Portal callback: tell the Ezitech Internship Portal (or any other caller) when an evaluation has finished.

The caller may pass an optional `callback_url` when it queues an evaluation. When the evaluation ends -
whatever the verdict, including "failed" and "interrupted by a worker restart" - the worker POSTs a small
JSON summary to that URL. The full record (logs, all scores, feedback) stays behind the API-key protected
`GET /results/{submission_id}`; the callback deliberately carries no logs and no source code.

Safety rules (the URL is chosen by the caller, so the worker must not become a way to reach internal services):
  - https only (http only when CALLBACK_ALLOW_INSECURE=1, meant for local testing)
  - no credentials inside the URL, optional host allow-list (CALLBACK_ALLOWED_HOSTS)
  - the host is resolved right before every attempt and refused if ANY address is private, loopback,
    link-local, reserved or multicast (CALLBACK_ALLOW_PRIVATE=1 switches this off for local testing)
  - redirects are never followed
  - delivery can never fail or change an evaluation: send_callback() does not raise

Signing: when CALLBACK_SECRET is set every request carries
    X-Sandbox-Timestamp: <unix seconds>
    X-Sandbox-Signature: sha256=<hex HMAC-SHA256 of "<timestamp>.<raw request body>" using CALLBACK_SECRET>
so the portal can check that the call really came from this platform (and reject old timestamps).
X-Sandbox-Delivery is the same for every retry of one notification, so the receiver can ignore duplicates.

Known limit: the address check and the connection are two separate steps, so a hostile DNS server could in theory
answer differently the second time (DNS rebinding). Use CALLBACK_ALLOWED_HOSTS in production to remove that risk.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import time
import uuid
from urllib.parse import urlparse

import requests

logger = logging.getLogger("sandbox.callback")

MAX_URL_LENGTH = 500
EVENT_NAME = "evaluation.finished"

# Score categories copied into the callback (each as its 0-100 number, or null when not applicable).
SUMMARY_SCORE_KEYS = (
    "engineering_maturity", "feature_completion", "code_quality", "architecture", "security", "api_quality",
    "deployment_readiness", "structure", "documentation", "database_connectivity", "authentication_flow",
    "error_handling", "security_configuration", "required_features", "environment_configuration",
)


class CallbackError(Exception):
    """The callback must not be sent (unsafe destination). Never retried."""


# ------------------------------------------------------------------ settings (read at call time)

def _allowed_hosts() -> set:
    raw = os.environ.get("CALLBACK_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _flag(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes")


def _number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# ------------------------------------------------------------------ validation

def _is_public_ip(text: str) -> bool:
    ip = ipaddress.ip_address(text.split("%", 1)[0])  # drop an IPv6 zone id such as fe80::1%eth0
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def validate_callback_url(url: str) -> str:
    """Return the cleaned URL or raise ValueError with a message meant for the caller."""
    url = (url or "").strip()
    if not url:
        raise ValueError("callback_url must not be empty")
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"callback_url is too long (max {MAX_URL_LENGTH} characters)")
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("callback_url must not contain spaces or control characters")

    parsed = urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and _flag("CALLBACK_ALLOW_INSECURE")):
        raise ValueError("callback_url must start with https://")
    if parsed.username or parsed.password:
        raise ValueError("callback_url must not contain credentials")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("callback_url has no host")
    try:
        parsed.port  # noqa: B018 - raises ValueError for a port such as :99999 or :abc
    except ValueError:
        raise ValueError("callback_url has an invalid port") from None

    allowed = _allowed_hosts()
    if allowed and host not in allowed:
        raise ValueError(f"callback_url host '{host}' is not allowed. Allowed hosts: {', '.join(sorted(allowed))}")

    if not _flag("CALLBACK_ALLOW_PRIVATE"):
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("callback_url must not point to localhost")
        try:
            literal_is_public = _is_public_ip(host)
        except ValueError:
            literal_is_public = None  # a name, not an IP address: resolved (and checked) when sending
        if literal_is_public is False:
            raise ValueError("callback_url must not point to a private or internal address")
    return url


def _check_destination(host: str, port: int) -> None:
    """Refuse to connect when the host resolves to anything that is not a public address."""
    if _flag("CALLBACK_ALLOW_PRIVATE"):
        return
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise CallbackError(f"host '{host}' does not resolve") from None
    for info in infos:
        if not _is_public_ip(info[4][0]):
            raise CallbackError(f"host '{host}' resolves to a private or internal address")


# ------------------------------------------------------------------ payload + signature

def build_payload(result, label: str, project_type: str) -> dict:
    """Small, log-free summary of a finished evaluation. `result` is an EvaluationResult."""
    scores = result.scores or {}

    def score_of(key):
        entry = scores.get(key)
        return entry.get("score") if isinstance(entry, dict) else None

    maturity = scores.get("engineering_maturity")
    return {
        "event": EVENT_NAME,
        "submission_id": result.submission_id,
        "status": result.status,
        "repo_url": label,  # repository URL, "upload:<file name>" or "image:<reference>"
        "project_type": project_type,
        "build_success": result.build_success,
        "execution_success": result.execution_success,
        "duration_seconds": result.duration_seconds,
        "error": result.error,
        "scoring_version": scores.get("scoring_version"),
        "engineering_maturity_partial": bool(maturity.get("partial")) if isinstance(maturity, dict) else None,
        "scores": {key: score_of(key) for key in SUMMARY_SCORE_KEYS},
        "result_path": f"/results/{result.submission_id}",
        "report_path": f"/results/{result.submission_id}/report",
    }


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


# ------------------------------------------------------------------ delivery

def _host_for_logs(url: str) -> str:
    """Log the host only: the path or query string of a callback URL may contain a secret token."""
    try:
        return urlparse(url).hostname or "?"
    except ValueError:
        return "?"


def send_callback(url: str, payload: dict, sleep=time.sleep) -> dict:
    """
    POST `payload` to `url`. Retries network errors, HTTP 5xx, 408, 425 and 429 with exponential back-off
    (CALLBACK_MAX_ATTEMPTS, default 3; CALLBACK_BACKOFF_SECONDS, default 2). Other HTTP answers, an unsafe
    destination and redirects are final. Never raises; returns
    {"delivered": bool, "attempts": int, "status_code": int|None, "error": str|None}.
    """
    outcome = {"delivered": False, "attempts": 0, "status_code": None, "error": None}
    try:
        return _deliver(url, payload, sleep, outcome)
    except Exception as exc:  # noqa: BLE001 - a callback must never affect the evaluation
        outcome["error"] = f"unexpected {type(exc).__name__}"
        logger.warning("Callback to %s failed unexpectedly: %s", _host_for_logs(url), outcome["error"])
        return outcome


def _deliver(url: str, payload: dict, sleep, outcome: dict) -> dict:
    max_attempts = max(1, int(_number("CALLBACK_MAX_ATTEMPTS", 3)))
    timeout = _number("CALLBACK_TIMEOUT_SECONDS", 10)
    backoff = _number("CALLBACK_BACKOFF_SECONDS", 2)
    secret = os.environ.get("CALLBACK_SECRET", "")
    host_for_logs = _host_for_logs(url)

    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    delivery_id = uuid.uuid4().hex
    if not secret:
        logger.warning("CALLBACK_SECRET is not set: the callback is sent unsigned")

    for attempt in range(1, max_attempts + 1):
        outcome["attempts"] = attempt
        try:
            _check_destination(host, port)
        except CallbackError as exc:
            outcome["error"] = str(exc)
            logger.warning("Callback to %s not sent: %s", host_for_logs, outcome["error"])
            return outcome

        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ezitech-sandbox-callback/1",
            "X-Sandbox-Event": EVENT_NAME,
            "X-Sandbox-Delivery": delivery_id,
            "X-Sandbox-Timestamp": timestamp,
        }
        if secret:
            headers["X-Sandbox-Signature"] = sign(secret, timestamp, body)

        retry = True
        try:
            response = requests.post(url, data=body, headers=headers, timeout=timeout, allow_redirects=False)
            outcome["status_code"] = response.status_code
            response.close()
            if 200 <= response.status_code < 300:
                outcome.update(delivered=True, error=None)
                logger.info("Callback to %s delivered (attempt %d)", host_for_logs, attempt)
                return outcome
            outcome["error"] = f"HTTP {response.status_code}"
            retry = response.status_code in (408, 425, 429) or response.status_code >= 500
        except requests.RequestException as exc:
            # The exception text can contain the full URL; keep only its type.
            outcome["error"] = type(exc).__name__

        if not retry:
            break
        if attempt < max_attempts:
            sleep(backoff * 2 ** (attempt - 1))

    logger.warning("Callback to %s not delivered after %d attempt(s): %s",
                   host_for_logs, outcome["attempts"], outcome["error"])
    return outcome
