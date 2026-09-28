# Database Schema

PostgreSQL 16. Tables are created automatically on backend start-up (`init_db()`).

## `submissions`

| Column | Type | Notes |
|---|---|---|
| `submission_id` | varchar, primary key | 8-character id |
| `repo_url` | varchar, not null | repository URL, `upload:<file.zip>` or `image:<ref>` |
| `project_type` | varchar, not null | `python`, `node`, `php` or `docker_image` (as declared) |
| `status` | varchar, not null | `success`, `failed`, `build_error`, `timeout` |
| `build_success` | boolean, not null | |
| `execution_success` | boolean, not null | |
| `duration_seconds` | float, not null | total evaluation time |
| `logs` | varchar | build and runtime logs |
| `scores` | JSON | all scores, findings, feedback and dynamic-test results (below) |
| `error` | varchar | failure reason |
| `created_at` | timestamp (UTC, timezone-naive) | API returns it with a `+00:00` marker |

### `scores` document

```
{
  "scoring_version": 2,
  "evaluated_as": "node", "project_type_note": "...",
  "structure|code_quality|security|api_quality|architecture|database_connectivity|
   authentication_flow|error_handling|security_configuration|required_features|
   environment_configuration|documentation|feature_completion|deployment_readiness":
        { "score": 0-100 | null, ...evidence fields... },
  "engineering_maturity": { "score": 50, "components_averaged": 13, "partial": false },
  "testing": { "ran": true, "passed": true, "categories": { "unit_tests": true, ... } },
  "api_health": { "checked": true, "reachable": true, "port": 3001, ... },
  "ui_smoke_check": { ... }, "load_test": { "avg_latency_ms": 2.6, "p95_latency_ms": 5.4, ... },
  "build_seconds": 27.06,
  "github_metadata": { "stars": 16, ... },
  "feedback": { "strengths": [], "weaknesses": [], "missing_requirements": [], "security_risks": [],
                "performance_suggestions": [], "refactoring_suggestions": [], "improvement_roadmap": [] }
}
```

`scoring_version` identifies which formulas produced a record. Records created before this field
existed were scored with fewer checks; re-run them with **Re-evaluate** to make them comparable.

## Evaluation metrics (MLflow)

Each evaluation is also logged as an MLflow run (file store under `/tmp/sandbox-runs/mlflow`) with the
duration and every numeric score as metrics.
