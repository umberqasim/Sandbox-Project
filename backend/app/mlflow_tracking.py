"""
Lightweight MLflow integration - logs each evaluation's scores as an
MLflow run so score trends can be compared over time. Uses local
file-based tracking (no separate MLflow server needed) - the case
study lists MLflow "for evaluation metrics", which is exactly this use.
"""

import os
import mlflow

TRACKING_DIR = "/tmp/sandbox-runs/mlflow"
os.makedirs(TRACKING_DIR, exist_ok=True)
mlflow.set_tracking_uri(f"file://{TRACKING_DIR}")
mlflow.set_experiment("sandbox-evaluations")


def log_evaluation(submission_id: str, repo_url: str, project_type: str, scores: dict, duration_seconds: float):
    if not scores:
        return
    try:
        with mlflow.start_run(run_name=submission_id):
            mlflow.set_tag("repo_url", repo_url)
            mlflow.set_tag("project_type", project_type)
            mlflow.log_metric("duration_seconds", duration_seconds)
            for key in [
                "structure", "code_quality", "security", "api_quality", "architecture",
                "database_connectivity", "authentication_flow", "error_handling",
                "feature_completion", "deployment_readiness", "engineering_maturity",
            ]:
                entry = scores.get(key)
                if entry and entry.get("score") is not None:
                    mlflow.log_metric(key, entry["score"])
    except Exception:
        # Never let metrics logging break the actual evaluation.
        pass
