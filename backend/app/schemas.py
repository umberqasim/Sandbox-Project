from pydantic import BaseModel, Field, field_validator
from typing import Optional

from .validation import validate_repo_url, validate_docker_image
from .callback import validate_callback_url


CALLBACK_DESCRIPTION = (
    "Optional https URL that receives a JSON summary (POST) when the evaluation has finished. "
    "See docs/api.md, 'Portal callback'."
)


def _clean_callback_url(v):
    """Empty means 'no callback' (form fields arrive as empty strings); anything else must be a safe URL."""
    if v is None or not str(v).strip():
        return None
    return validate_callback_url(v)


class EvaluationRequest(BaseModel):
    repo_url: str = Field(..., description="Public GitHub/GitLab repo URL (https) to clone and evaluate")
    project_type: str = Field(
        default="python",
        description="Project type: python | node | php (auto-corrected if the repository looks different)",
    )
    callback_url: Optional[str] = Field(default=None, description=CALLBACK_DESCRIPTION)

    @field_validator("repo_url")
    @classmethod
    def _check_repo_url(cls, v: str) -> str:
        return validate_repo_url(v)

    @field_validator("callback_url")
    @classmethod
    def _check_callback_url(cls, v):
        return _clean_callback_url(v)


class EvaluationResult(BaseModel):
    submission_id: str
    status: str  # "success" | "failed" | "timeout" | "build_error"
    build_success: bool
    execution_success: bool
    logs: str
    duration_seconds: float
    scores: Optional[dict] = None
    error: Optional[str] = None


class DockerImageRequest(BaseModel):
    image: str = Field(..., description="Docker image reference, e.g. 'user/image:tag'")
    callback_url: Optional[str] = Field(default=None, description=CALLBACK_DESCRIPTION)

    @field_validator("callback_url")
    @classmethod
    def _check_callback_url(cls, v):
        return _clean_callback_url(v)

    @field_validator("image")
    @classmethod
    def _check_image(cls, v: str) -> str:
        return validate_docker_image(v)
