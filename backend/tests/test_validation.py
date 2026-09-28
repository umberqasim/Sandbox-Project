import pytest
from pydantic import ValidationError

from app.schemas import EvaluationRequest, DockerImageRequest
from app.validation import validate_repo_url


@pytest.mark.parametrize("url", [
    "https://github.com/jatins/express-hello-world",
    "https://github.com/user/repo.git",
    "https://gitlab.com/group/project",
])
def test_accepts_normal_repo_urls(url):
    assert EvaluationRequest(repo_url=url).repo_url == url


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",  # local file disclosure
    "/app",  # local path
    "ext::sh -c 'id'",  # git command-execution transport
    "http://github.com/a/b",  # not https
    "https://redis:6379/a/b",  # internal host (SSRF)
    "https://evil.example.com/a/b",  # host not on the allow-list
    "https://user:pw@github.com/a/b",  # credentials in URL
    "https://github.com/onlyuser",  # not a repository
    "https://github.com/a/b c",  # whitespace
    "",
])
def test_rejects_unsafe_repo_urls(url):
    with pytest.raises(ValidationError):
        EvaluationRequest(repo_url=url)


def test_error_message_is_readable():
    with pytest.raises(ValueError, match="not allowed"):
        validate_repo_url("https://evil.example.com/a/b")


def test_docker_image_validation():
    assert DockerImageRequest(image="crccheck/hello-world").image == "crccheck/hello-world"
    assert DockerImageRequest(image="nginx:1.27-alpine").image
    for bad in ["", "-v /:/host alpine", "a b", "img;rm -rf /"]:
        with pytest.raises(ValidationError):
            DockerImageRequest(image=bad)
