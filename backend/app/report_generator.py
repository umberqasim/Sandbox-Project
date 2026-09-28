"""
Formatted Markdown report generator - turns a stored submission record
into a human-readable evaluation report a mentor could read directly.
"""

SCORE_ROWS = [
    ("Structure", "structure"),
    ("Code Quality", "code_quality"),
    ("Security", "security"),
    ("API Quality", "api_quality"),
    ("Architecture", "architecture"),
    ("Database Connectivity", "database_connectivity"),
    ("Authentication Flow", "authentication_flow"),
    ("Error Handling", "error_handling"),
    ("Security Configuration", "security_configuration"),
    ("Required Features", "required_features"),
    ("Environment Configuration", "environment_configuration"),
    ("Documentation", "documentation"),
    ("Feature Completion", "feature_completion"),
    ("Deployment Readiness", "deployment_readiness"),
]


def _fmt_score(entry):
    if not entry or entry.get("score") is None:
        return "N/A"
    return f"{entry['score']}/100"


def _section(lines, title, items, numbered=False):
    if not items:
        return
    lines.append(f"## {title}")
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item}" if numbered else f"- {item}")
    lines.append("")


def generate_report(record: dict) -> str:
    """Build a concise, shareable evaluation summary with the complete scorecard."""
    scores = record.get("scores") or {}
    feedback = scores.get("feedback") or {}
    maturity = scores.get("engineering_maturity") or {}
    score = maturity.get("score")
    partial = bool(maturity.get("partial"))

    if partial:
        verdict = "Health check only - not comparable with full evaluations"
    elif score is None:
        verdict = "Score unavailable"
    elif score >= 80:
        verdict = "Excellent - close to production ready"
    elif score >= 60:
        verdict = "Good - a few gaps to close"
    elif score >= 40:
        verdict = "Needs work - several weak areas"
    else:
        verdict = "Critical gaps - major rework needed"

    status = str(record.get("status", "unknown")).replace("_", " ").title()
    build = "Passed" if record.get("build_success") else "Failed"
    execution = "Passed" if record.get("execution_success") else "Failed"
    evaluated_as = scores.get("evaluated_as") or record.get("project_type", "unknown")
    lines = [
        f"# Evaluation Summary - {record.get('submission_id', 'unknown')}",
        "",
        f"**Repository:** {record.get('repo_url', 'unknown')} | **Project type:** {evaluated_as}",
        f"**Status:** {status} | **Build:** {build} | **Execution:** {execution}",
        f"**Duration:** {record.get('duration_seconds', 'n/a')}s | **Evaluated:** {record.get('created_at', 'n/a')}",
        "",
        "## Overall Score",
        "",
        f"**Engineering Maturity:** {_fmt_score(maturity)} - {verdict}",
        "",
        "## Score Breakdown",
        "",
        "| Category | Score |",
        "|---|---|",
    ]

    for label, key in SCORE_ROWS:
        lines.append(f"| {label} | {_fmt_score(scores.get(key))} |")
    lines.append(f"| Engineering Maturity | {_fmt_score(maturity)} |")
    lines.append("")

    notes = [scores.get(key) for key in ("project_root_note", "project_type_note", "library_note") if scores.get(key)]
    if notes:
        lines.append("## Evaluation Notes")
        lines.extend(f"- {note}" for note in notes)
        lines.append("")

    review = scores.get("ai_review") or {}
    if review.get("summary"):
        lines += [
            "## AI review (advisory)",
            "",
            "This review is advisory and does not change any score.",
            "",
            f"> {review['summary']}",
            "",
        ]
        _section(lines, "AI Review Strengths", review.get("strengths") or [])
        _section(lines, "AI Review Concerns", review.get("concerns") or [])

    testing = scores.get("testing") or {}
    if testing:
        lines += ["## Automated Testing", ""]
        if testing.get("timed_out"):
            test_status = "TIMED OUT"
        elif testing.get("ran"):
            test_status = "PASSED" if testing.get("passed") else "FAILED"
        else:
            test_status = "NOT RUN"
        lines += [f"**Result:** {test_status}", ""]
        if testing.get("reason"):
            lines += [f"**Details:** {testing['reason']}", ""]
        output = testing.get("output")
        if output:
            lines += ["**Test output:**", ""]
            lines.extend(f"> {line}" for line in str(output).splitlines()[-12:])
            lines.append("")

    _section(lines, "Key Strengths", (feedback.get("strengths") or [])[:3])
    _section(lines, "Main Improvement Areas", (feedback.get("weaknesses") or [])[:3])
    _section(lines, "Missing Requirements", (feedback.get("missing_requirements") or [])[:5])

    next_steps = feedback.get("improvement_roadmap") or review.get("next_steps") or []
    _section(lines, "Recommended Next Steps", next_steps[:5], numbered=True)
    lines += [
        "Detailed test results, runtime evidence, and build logs are available in the dashboard.",
        "",
    ]
    return "\n".join(lines)