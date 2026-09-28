"""
Input validation for untrusted submission references.

Why this exists: the worker runs `git clone <url>` and `docker pull <ref>` on
values typed by users. Without validation a submitter could point the clone at
`file:///...` (local file disclosure), an internal hostname (SSRF), or a git
transport such as `ext::` that executes commands.
"""

import os
import re
from urllib.parse import urlparse

ALLOWED_GIT_HOSTS = {
    h.strip().lower()
    for h in os.environ.get("ALLOWED_GIT_HOSTS", "github.com,gitlab.com,bitbucket.org").split(",")
    if h.strip()
}

MAX_URL_LENGTH = 300
DOCKER_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:@]{0,199}$")


def validate_repo_url(url: str) -> str:
    """Return the cleaned URL or raise ValueError with a user-facing message."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Repository URL is required")
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"Repository URL is too long (max {MAX_URL_LENGTH} characters)")
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("Repository URL must not contain spaces or control characters")

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only https:// repository URLs are accepted")
    if parsed.username or parsed.password:
        raise ValueError("Credentials inside the URL are not allowed - use a public repository")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_GIT_HOSTS:
        raise ValueError(
            f"Host '{host or '?'}' is not allowed. Allowed hosts: {', '.join(sorted(ALLOWED_GIT_HOSTS))}"
        )

    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        raise ValueError("URL must point to a repository, e.g. https://github.com/<user>/<repo>")
    return url


def validate_docker_image(ref: str) -> str:
    ref = (ref or "").strip()
    if not DOCKER_IMAGE_PATTERN.match(ref):
        raise ValueError("Invalid Docker image reference (expected something like 'user/image:tag')")
    return ref
