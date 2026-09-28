"""
Static analysis checks. These run against the cloned source directly and
never execute submitted code, so they're safe to run even before/without
building or running the sandbox container.

Hardening notes:
  - All file walking goes through _iter_files(), which skips dependency /
    build folders (node_modules, venv, ...), symlinks and huge files.
  - flake8 runs with --isolated: without it, a submission's own .flake8 /
    setup.cfg / tox.ini can declare [flake8:local-plugins] and get
    arbitrary Python executed inside the worker, outside the sandbox.
"""

import json
import os
import re
import subprocess
from pathlib import Path

EXCLUDED_DIRS = {
    ".git", "node_modules", "vendor", "venv", ".venv", "__pycache__",
    "site-packages", ".tox", "dist", "build", ".dart_tool", ".idea",
    ".mypy_cache", ".pytest_cache",
}
MAX_READ_BYTES = 1_000_000  # skip files bigger than ~1MB (also keeps RAM low)

STRUCTURE_CHECKS = {
    "python": {
        "README.md": 25,
        "requirements.txt": 25,
        ".gitignore": 15,
        "tests": 20,
        ".env.example": 15,
    },
    "node": {
        "README.md": 25,
        "package.json": 25,
        ".gitignore": 15,
        "tests": 20,
        ".env.example": 15,
    },
    "php": {
        "README.md": 25,
        "composer.json": 25,
        ".gitignore": 15,
        "tests": 20,
        ".env.example": 15,
    },
    "flutter": {
        "README.md": 30,
        "pubspec.yaml": 30,
        ".gitignore": 15,
        "test": 25,
    },
}

SECRET_PATTERNS = {
    "generic_api_key": re.compile(r"(?i)(api[_-]?key|secret[_-]?key)\s*=\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
    "hardcoded_password": re.compile(r"(?i)password\s*=\s*['\"][^'\"]{4,}['\"]"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key_block": re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
}

# .env files use KEY=value without quotes, so they need their own pattern.
ENV_FILE_SECRET_PATTERN = re.compile(
    r"(?im)^\s*[A-Z0-9_]*(API_KEY|SECRET|TOKEN|PASSWORD|PASSWD)[A-Z0-9_]*\s*=\s*['\"]?[^\s'\"#]{8,}"
)

# name -> (regex, description). Lookbehinds avoid the classic false
# positives: model.eval() (PyTorch), ast.literal_eval(), regex.exec().
DANGEROUS_CALLS = {
    "eval(": (re.compile(r"(?<![\w.])eval\("), "Use of eval() - arbitrary code execution risk"),
    "exec(": (re.compile(r"(?<![\w.])exec\("), "Use of exec() - arbitrary code execution risk"),
    "child_process.exec(": (
        re.compile(r"\bchild_process\.exec(?:Sync)?\("), "child_process.exec() - shell injection risk",
    ),
    "os.system(": (re.compile(r"\bos\.system\("), "Use of os.system() - shell injection risk"),
    "shell=True": (re.compile(r"shell\s*=\s*True"), "subprocess call with shell=True - shell injection risk"),
    "pickle.loads(": (re.compile(r"\bpickle\.loads\("), "Use of pickle.loads() - unsafe deserialization risk"),
    "system(": (re.compile(r"(?<![\w.>:])system\("), "Use of system() - shell injection risk (PHP)"),
    "unserialize(": (re.compile(r"(?<![\w.>:])unserialize\("), "Use of unserialize() - PHP object injection risk"),
}

CORS_PATTERNS = [
    re.compile(r"(?i)CORS|cross-origin"),
    re.compile(r"flask_cors|flask-cors"),
    re.compile(r"cors\(\)"),
    re.compile(r"Access-Control-Allow-Origin"),
]

SECRETS_MGMT_PATTERNS = [
    re.compile(r"(?i)os\.environ|process\.env|getenv\("),
    re.compile(r"config\(\)\.get\(|dotenv"),
    re.compile(r"(?<![\w>:$])env\(\s*['\"]"),  # Laravel / PHP: env('KEY')
]

# Reads with NO fallback value: the app really needs these set. A read WITH a default -
# env('X', 'fallback'), os.environ.get('X', 'fallback'), process.env.X || 'fallback' - is optional
# configuration and must not be reported as "missing from .env.example"
# (Laravel's config/*.php alone contains ~100 optional env() calls).
ENV_REQUIRED_PATTERNS = [
    re.compile(r"os\.environ\[['\"]([A-Z0-9_]+)['\"]\]"),
    re.compile(r"(?<![\w>:$])env\(\s*['\"]([A-Z0-9_]+)['\"]\s*\)"),
    re.compile(r"process\.env\.([A-Z0-9_]+)(?![A-Z0-9_])(?!\s*(?:\|\||\?\?))"),
]

ENV_VAR_USAGE_PATTERNS = [
    re.compile(r"os\.environ(?:\.get)?\(['\"]([A-Z0-9_]+)['\"]"),
    re.compile(r"os\.environ\[['\"]([A-Z0-9_]+)['\"]\]"),
    re.compile(r"os\.getenv\(['\"]([A-Z0-9_]+)['\"]"),
    re.compile(r"process\.env\.([A-Z0-9_]+)"),
    re.compile(r"env\(['\"]([A-Z0-9_]+)['\"]"),  # Laravel's env() helper
]

DB_CONFIG_PATTERNS = [
    re.compile(r"(?i)DATABASE_URL"),
    re.compile(r"(?i)DB_HOST"),
    re.compile(r"(?i)MONGO_URI"),
    re.compile(r"mysqli?|pg_connect|PDO\("),
    re.compile(r"mongoose\.connect|createConnection"),
]

AUTH_PATTERNS = [
    re.compile(r"(?i)jwt"),
    re.compile(r"(?i)passport"),
    re.compile(r"(?i)flask_login|flask-login"),
    re.compile(r"(?i)sanctum|laravel/passport"),
    re.compile(r"(?i)session\[.?['\"]user"),
    re.compile(r"(?i)@login_required|middleware\(['\"]auth"),
]

ERROR_HANDLING_PATTERNS = [
    re.compile(r"\btry\s*[:{]"),
    re.compile(r"except\s+\w*Exception"),
    re.compile(r"\.catch\s*\("),
    re.compile(r"app\.use\(\s*\(err"),
    re.compile(r"@app\.errorhandler"),
    re.compile(r"function\s+handle\w*[Ee]xception"),
    re.compile(r"->withExceptions\s*\("),  # Laravel 11+: central exception handling in bootstrap/app.php
]

CODE_EXTENSIONS = {".py", ".js", ".ts", ".php"}


# ---------------------------------------------------------------- helpers

def _iter_files(project_dir: Path, suffixes=None):
    """Walk the project, pruning dependency/build folders and skipping symlinks."""
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for name in files:
            path = Path(root) / name
            if path.is_symlink():
                continue
            if suffixes is None or path.suffix in suffixes:
                yield path


def _read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_READ_BYTES:
            return ""
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def _find_readme(*locations):
    """
    Path of the README.md in any letter case, or None. Linux file systems are case-sensitive, so
    `(dir / "README.md").exists()` missed 'Readme.md' (expressjs/express) and 'readme.md'.
    """
    for loc in locations:
        try:
            for entry in Path(loc).iterdir():
                if entry.name.lower() == "readme.md" and entry.is_file() and not entry.is_symlink():
                    return entry
        except OSError:
            continue
    return None


def detect_project_type(project_dir: Path):
    """Best-effort guess from root files. Returns None when unsure (then the declared type is kept)."""
    def has(name):
        return (project_dir / name).exists()

    if has("composer.json") or has("artisan"):
        return "php"  # checked first: Laravel repos also ship a package.json
    node = has("package.json")
    py = has("requirements.txt") or has("pyproject.toml") or has("setup.py") or has("Pipfile")
    if node and py:
        return None  # ambiguous (e.g. monorepo) - trust the declared type
    if node:
        return "node"
    if py:
        return "python"
    if has("pubspec.yaml"):
        return None
    root_py = list(project_dir.glob("*.py"))
    root_js = list(project_dir.glob("*.js"))
    if root_py and not root_js:
        return "python"
    if root_js and not root_py:
        return "node"
    return None


PROJECT_MARKERS = (
    "package.json", "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
    "composer.json", "artisan", "pubspec.yaml", "Dockerfile",
)
SERVER_FOLDER_NAMES = {"backend", "server", "api", "service", "app"}
CLIENT_FOLDER_NAMES = {"frontend", "client", "web", "ui"}


def _has_project_marker(directory: Path) -> bool:
    return any((directory / m).exists() for m in PROJECT_MARKERS) or bool(
        list(directory.glob("*.py")) or list(directory.glob("*.js"))
    )


def find_project_root(repo_root: Path, declared_type: str = "python"):
    """
    Monorepos keep the real projects in sub-folders (backend/, frontend/) and only a
    docker-compose file at the top. Judging the repository root there gives nonsense
    ("Missing package.json", generated Dockerfile that exits immediately). When the root
    has no project files, pick the most likely server project one level down.

    Returns (project_dir, note_or_None).
    """
    if _has_project_marker(repo_root):
        return repo_root, None

    candidates = [
        d for d in sorted(repo_root.iterdir())
        if d.is_dir() and d.name not in EXCLUDED_DIRS and not d.name.startswith(".") and _has_project_marker(d)
    ]
    if not candidates:
        return repo_root, None

    def rank(d: Path) -> int:
        name = d.name.lower()
        score = 0
        if name in SERVER_FOLDER_NAMES:
            score += 3
        if name in CLIENT_FOLDER_NAMES:
            score -= 1
        if (d / "Dockerfile").exists():
            score += 1
        if detect_project_type(d) == declared_type:
            score += 1
        return score

    best = max(candidates, key=rank)
    others = [d.name for d in candidates if d != best]
    note = f"No project files at the repository root - evaluated the '{best.name}/' folder"
    if others:
        note += f" (other projects found: {', '.join(others)}; submit them separately to evaluate them)"
    return best, note


def _is_web_project(api_quality: dict) -> bool:
    return bool(api_quality.get("framework_detected")) or api_quality.get("route_count", 0) > 0


def _not_applicable(note: str, **extra) -> dict:
    return {"score": None, "not_applicable": True, "note": note, **extra}


# ---------------------------------------------------------- structure/quality

TEST_DIR_NAMES = ("tests", "test", "__tests__", "spec")


def find_test_dir(project_dir: Path):
    """Name of the first conventional test folder (tests/, test/, __tests__/, spec/), else None."""
    for name in TEST_DIR_NAMES:
        if (project_dir / name).is_dir():
            return name
    return None


def find_test_target(project_dir: Path):
    """
    What the test runner should be pointed at: a test folder name, "." when only
    loose test files exist (e.g. app.test.js, test_api.py in the root), else None.
    """
    named = find_test_dir(project_dir)
    if named:
        return named
    for f in _iter_files(project_dir):
        if _is_test_file(f.relative_to(project_dir)):
            return "."
    return None


def check_structure(project_dir: Path, project_type: str = "python", repo_root: Path = None) -> dict:
    """`repo_root`: when a sub-folder is evaluated (monorepo), README/.gitignore/tests at the root count too."""
    locations = [project_dir] + ([repo_root] if repo_root and repo_root != project_dir else [])
    found = []
    missing = []
    score = 0
    checks = STRUCTURE_CHECKS.get(project_type, STRUCTURE_CHECKS["python"])

    for name, weight in checks.items():
        # Node/Jest/Mocha projects use test/ or __tests__/, not only tests/.
        if name in ("tests", "test"):
            present = any(find_test_dir(loc) is not None for loc in locations)
        elif name == "README.md":
            present = _find_readme(*locations) is not None
        else:
            present = any((loc / name).exists() for loc in locations)
        if present:
            found.append(name)
            score += weight
        else:
            missing.append(name)

    return {"score": min(score, 100), "found": found, "missing": missing}


def check_documentation(project_dir: Path, repo_root: Path = None) -> dict:
    """Documentation quality (feeds the 'Best Documentation' leaderboard)."""
    score = 0
    signals = []

    readme_text = ""
    for loc in [project_dir] + ([repo_root] if repo_root and repo_root != project_dir else []):
        readme_path = _find_readme(loc)
        if readme_path is not None:
            readme_text = _read_text(readme_path)
            break
    if readme_text:
        score += 30
        signals.append("README.md present")
        if len(readme_text) > 500:
            score += 20
            signals.append("README is detailed (500+ chars)")
        if re.search(r"(?im)^#+\s*(install|setup|usage|getting started|how to run|run)", readme_text):
            score += 20
            signals.append("README has setup/usage section")

    doc_locations = [project_dir] + ([repo_root] if repo_root and repo_root != project_dir else [])
    if any(
        (loc / "docs").is_dir() or any((loc / n).exists() for n in ["openapi.json", "openapi.yaml", "swagger.json"])
        for loc in doc_locations
    ):
        score += 15
        signals.append("docs/ folder or API spec present")

    if (project_dir / ".env.example").exists():
        score += 15
        signals.append(".env.example present")

    return {"score": min(score, 100), "signals": signals}


def check_code_quality(project_dir: Path, project_type: str = "python") -> dict:
    if project_type == "node":
        return _check_js_quality(project_dir)
    if project_type == "php":
        return _check_php_quality(project_dir)
    if project_type != "python":
        return _not_applicable(f"No linter configured for project type '{project_type}'")

    py_files = list(_iter_files(project_dir, {".py"}))
    if not py_files:
        return _not_applicable("No Python files found", files_scanned=0)

    try:
        result = subprocess.run(
            [
                "flake8", "--isolated",  # --isolated: never load the submission's own flake8 config
                "--max-line-length=120", "--count",
                f"--exclude={','.join(sorted(EXCLUDED_DIRS))}",
                str(project_dir),
            ],
            capture_output=True, text=True, timeout=60,
        )
        output_lines = result.stdout.strip().splitlines()
        issue_count = 0
        if output_lines and output_lines[-1].strip().isdigit():
            issue_count = int(output_lines[-1].strip())

        num_files = max(len(py_files), 1)
        issues_per_file = issue_count / num_files
        score = max(0, round(100 - (issues_per_file * 5)))

        return {
            "score": score,
            "issue_count": issue_count,
            "files_scanned": len(py_files),
            "raw_output": "\n".join(output_lines[:-1][-50:]),
        }
    except subprocess.TimeoutExpired:
        return {"score": None, "error": "flake8 timed out"}
    except FileNotFoundError:
        return {"score": None, "error": "flake8 not installed"}


def _check_syntax(project_dir: Path, suffix: str, cmd, error_stream: str) -> dict:
    files = list(_iter_files(project_dir, {suffix}))
    if not files:
        return _not_applicable(f"No {suffix} files found", files_scanned=0)

    syntax_errors = []
    checker_missing = False
    for f in files:
        try:
            result = subprocess.run(
                cmd + [str(f)], capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                msg = getattr(result, error_stream).strip()[:200]
                syntax_errors.append({"file": str(f.relative_to(project_dir)), "error": msg})
        except FileNotFoundError:
            checker_missing = True
            break
        except subprocess.TimeoutExpired:
            continue

    if checker_missing:
        return {"score": None, "error": f"{cmd[0]} not installed", "files_scanned": len(files)}

    errors_per_file = len(syntax_errors) / max(len(files), 1)
    score = max(0, round(100 - (errors_per_file * 40)))
    return {
        "score": score,
        "issue_count": len(syntax_errors),
        "files_scanned": len(files),
        "syntax_errors": syntax_errors[:20],
    }


def _check_js_quality(project_dir: Path) -> dict:
    return _check_syntax(project_dir, ".js", ["node", "--check"], "stderr")


def _check_php_quality(project_dir: Path) -> dict:
    return _check_syntax(project_dir, ".php", ["php", "-l"], "stdout")


# ------------------------------------------------------------------ security

def _regex_security_findings(project_dir: Path) -> list:
    findings = []

    for file_path in _iter_files(project_dir):
        is_env_file = file_path.name == ".env"
        if not is_env_file and file_path.suffix not in CODE_EXTENSIONS:
            continue
        text = _read_text(file_path)
        if not text:
            continue
        rel_path = str(file_path.relative_to(project_dir))

        if not is_env_file and _is_test_file(file_path.relative_to(project_dir)):
            # Tests legitimately use eval(), unserialize() and dummy passwords; flagging them
            # drove e.g. laravel/framework's security score to 0 with 100% test-file findings.
            continue

        if is_env_file:
            if ENV_FILE_SECRET_PATTERN.search(text):
                findings.append({
                    "source": "pattern_scan", "severity": "HIGH",
                    "type": "hardcoded_secret", "rule": "env_file_secret", "file": rel_path,
                    "description": "Secret value found in a committed .env file",
                })
            continue

        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append({
                    "source": "pattern_scan", "severity": "HIGH",
                    "type": "hardcoded_secret", "rule": name, "file": rel_path,
                    "description": f"Possible hardcoded secret ({name})",
                })

        for name, (pattern, description) in DANGEROUS_CALLS.items():
            if pattern.search(text):
                findings.append({
                    "source": "pattern_scan", "severity": "MEDIUM",
                    "type": "dangerous_call", "rule": name, "file": rel_path,
                    "description": description,
                })

    if (project_dir / ".env").exists():
        findings.append({
            "source": "pattern_scan", "severity": "HIGH",
            "type": "committed_env_file", "rule": ".env", "file": ".env",
            "description": ".env file committed - should be gitignored",
        })

    return findings


def _bandit_findings(project_dir: Path) -> tuple:
    excludes = ",".join(str(project_dir / d) for d in sorted(EXCLUDED_DIRS))
    try:
        result = subprocess.run(
            ["bandit", "-r", str(project_dir), "-f", "json", "-q", "-x", excludes],
            capture_output=True, text=True, timeout=60,
        )
        if not result.stdout.strip():
            return [], True
        data = json.loads(result.stdout)
        findings = []
        for issue in data.get("results", []):
            rel = issue.get("filename", "").replace(str(project_dir) + "/", "")
            if _is_test_file(Path(rel)):
                continue  # e.g. bandit B101 "assert used" in every pytest file
            findings.append({
                "source": "bandit",
                "severity": issue.get("issue_severity", "LOW"),
                "type": issue.get("test_id", "unknown"),
                "rule": issue.get("test_name", "unknown"),
                "file": issue.get("filename", "").replace(str(project_dir) + "/", ""),
                "description": issue.get("issue_text", "")[:200],
            })
        return findings, True
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return [], False


def check_security(project_dir: Path, project_type: str = "python") -> dict:
    findings = _regex_security_findings(project_dir)
    bandit_used = False

    if project_type == "python":
        bandit_results, bandit_used = _bandit_findings(project_dir)
        findings.extend(bandit_results)

    high = sum(1 for f in findings if f.get("severity") == "HIGH")
    medium = sum(1 for f in findings if f.get("severity") == "MEDIUM")
    low = len(findings) - high - medium

    score = 100 - min(high * 25, 75) - min(medium * 10, 30) - min(low * 3, 15)
    score = max(0, score)

    return {
        "score": score,
        "findings_count": len(findings),
        "findings": findings[:30],
        "bandit_used": bandit_used,
    }


# ---------------------------------------------------------------- API quality

def check_api_quality(project_dir: Path) -> dict:
    framework = None
    route_count = 0
    code_files = list(_source_files(project_dir, {".py", ".js", ".ts", ".php"}))

    route_patterns = [
        re.compile(r"@app\.(get|post|put|delete|patch)\("),
        re.compile(r"@(app|blueprint)\.route\("),
        re.compile(r"(app|router)\.(get|post|put|delete|patch)\("),
        re.compile(r"Route::(get|post|put|delete|patch|resource)\("),
    ]

    if (project_dir / "artisan").exists():
        framework = "laravel"

    for file_path in code_files:
        text = _read_text(file_path)
        if not text:
            continue

        if framework is None:
            if re.search(r"FastAPI\s*\(", text):
                framework = "fastapi"
            elif re.search(r"Flask\s*\(__name__\)", text):
                framework = "flask"
            elif re.search(r"express\s*\(\s*\)", text):
                framework = "express"

        for pattern in route_patterns:
            route_count += len(pattern.findall(text))

    if framework is None:
        composer_path = project_dir / "composer.json"
        if composer_path.exists():
            if "laravel/framework" in _read_text(composer_path):
                framework = "laravel"

    has_api_docs = any(
        (project_dir / name).exists()
        for name in ["openapi.json", "openapi.yaml", "swagger.json", "docs/api.md"]
    )

    is_web = bool(framework) or route_count > 0
    score = 0
    if framework:
        score += 40
    if route_count > 0:
        score += 40
    if has_api_docs:
        score += 20

    result = {
        "score": score if is_web else None,
        "framework_detected": framework,
        "route_count": route_count,
        "has_api_docs": has_api_docs,
    }
    if not is_web:
        result["not_applicable"] = True
        result["note"] = "No web framework or routes detected - not an API project"
    return result


_ENTRYPOINT_IGNORE_DIRS = {"examples", "example", "demo", "demos", "benchmark", "benchmarks", "docs", "doc"}

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_JS_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", re.MULTILINE)
_PY_LINE_COMMENT_RE = re.compile(r"^\s*#.*$", re.MULTILINE)


def _strip_js_comments(text: str) -> str:
    """Remove /* */ and full-line // comments, so a JSDoc example like '* app.listen(3000);' inside a
    comment (seen in expressjs/express's own lib/application.js) doesn't look like a real entry point."""
    return _JS_LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def _strip_py_comments(text: str) -> str:
    """Remove full-line # comments, so a commented-out 'app.run()' doesn't count as an entry point."""
    return _PY_LINE_COMMENT_RE.sub("", text)


def _entrypoint_source_files(project_dir: Path, suffixes):
    """Like _source_files, but also skips examples/demo/docs folders - a code sample showing how to
    call app.listen() is not the submission's own runnable entry point."""
    for f in _source_files(project_dir, suffixes):
        rel_parts = {p.lower() for p in f.relative_to(project_dir).parts[:-1]}
        if rel_parts & _ENTRYPOINT_IGNORE_DIRS:
            continue
        yield f


def check_has_runnable_entrypoint(project_dir: Path, project_type: str) -> dict:
    """
    Whether this looks like something meant to be run as a server, as opposed to a library/module
    (e.g. the 'express' package's own source, not an app built with it). Used only to decide the
    verdict when a project is detected as a web project but nothing was found listening, so a
    library is not marked "failed" for not doing something it never tried to do.
    Errs toward "present" (assume runnable) whenever the evidence is inconclusive - the goal is to
    stop clear libraries from being marked failed, not to excuse apps that are genuinely broken.
    """
    if project_type == "node":
        pkg = project_dir / "package.json"
        if pkg.exists():
            try:
                data = json.loads(_read_text(pkg) or "{}")
            except ValueError:
                data = {}
            start_script = (data.get("scripts") or {}).get("start") if isinstance(data, dict) else None
            if str(start_script or "").strip():
                return {"present": True, "reason": "package.json declares a 'start' script"}
        for f in _entrypoint_source_files(project_dir, {".js", ".ts", ".mjs", ".cjs"}):
            if re.search(r"\.listen\s*\(", _strip_js_comments(_read_text(f))):
                return {"present": True, "reason": f"'{f.name}' calls .listen(...)"}
        return {
            "present": False,
            "reason": "no package.json 'start' script and no .listen(...) call found outside "
                      "tests/examples - this looks like a library/package, not a runnable server",
        }

    if project_type == "python":
        for f in _entrypoint_source_files(project_dir, {".py"}):
            if f.name == "manage.py":
                return {"present": True, "reason": "manage.py found (Django)"}
            if re.search(r"\.run\s*\(|uvicorn\.run\s*\(", _strip_py_comments(_read_text(f))):
                return {"present": True, "reason": f"'{f.name}' starts a server"}
        return {
            "present": False,
            "reason": "no .run(...)/uvicorn.run(...) call and no manage.py found outside "
                      "tests/examples - this looks like a library/package, not a runnable server",
        }

    # PHP/Laravel and anything else: the generated Dockerfile always serves the app (php artisan
    # serve / built-in router), so there is always something to reach - no library case to detect.
    return {"present": True, "reason": "not checked for this project type"}


def _radon_complexity(project_dir: Path) -> dict:
    exclude = ",".join(f"*/{d}/*" for d in sorted(EXCLUDED_DIRS))
    try:
        result = subprocess.run(
            ["radon", "cc", str(project_dir), "-j", "-e", exclude],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        # radon keys the JSON by file name; the blocks themselves carry no "filename".
        all_blocks = [
            {**block, "filename": filename}
            for filename, blocks in data.items()
            if isinstance(blocks, list)
            for block in blocks
        ]
        if not all_blocks:
            return {"available": True, "avg_complexity": None, "complex_functions": [], "total_functions": 0}

        complexities = [b["complexity"] for b in all_blocks]
        avg = round(sum(complexities) / len(complexities), 1)
        complex_functions = [
            {"name": b["name"], "file": b.get("filename", "").replace(str(project_dir) + "/", ""),
             "complexity": b["complexity"], "rank": b["rank"]}
            for b in all_blocks if b["rank"] not in ("A", "B")
        ]
        return {
            "available": True,
            "avg_complexity": avg,
            "total_functions": len(all_blocks),
            "complex_functions": complex_functions[:15],
        }
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return {"available": False}


def check_architecture(project_dir: Path, project_type: str = "python") -> dict:
    expected_dirs = ["models", "routes", "controllers", "services", "utils", "config", "app", "database"]
    found_dirs = [d for d in expected_dirs if (project_dir / d).exists()]

    code_files = list(_source_files(project_dir, {".py", ".js", ".ts", ".php"}))

    oversized_files = []
    for f in code_files:
        if _is_test_file(f.relative_to(project_dir)):
            continue
        try:
            with f.open(errors="ignore") as fh:
                line_count = sum(1 for _ in fh)
        except OSError:
            continue
        if line_count > 300:
            oversized_files.append({"file": str(f.relative_to(project_dir)), "lines": line_count})

    folder_score = min(len(found_dirs) * 12, 50)
    file_split_score = 20 if len(code_files) > 1 and not oversized_files else 0

    complexity = {"available": False}
    complexity_score = 15
    if project_type == "python":
        complexity = _radon_complexity(project_dir)
        if complexity.get("available") and complexity.get("avg_complexity") is not None:
            avg = complexity["avg_complexity"]
            complexity_score = max(0, round(30 - max(0, avg - 5) * 2))

    score = min(folder_score + file_split_score + complexity_score, 100)

    return {
        "score": score,
        "found_dirs": found_dirs,
        "total_code_files": len(code_files),
        "oversized_files": oversized_files[:10],
        "complexity": complexity,
    }


# ------------------------------------------------------------- test detection

def _is_test_file(rel_path: Path) -> bool:
    parts = [p.lower() for p in rel_path.parts]
    name = parts[-1]
    if any(p in ("tests", "test", "__tests__", "spec") for p in parts[:-1]):
        return True
    original = rel_path.name  # case-sensitive: PHPUnit uses FooTest.php, but "contest.php" is not a test
    return (
        name.startswith("test_") or name.endswith("_test.py")
        or ".test." in name or ".spec." in name
        or original.endswith(("Test.php", "_test.php")) or name == "test.php"
    )


def _source_files(project_dir: Path, suffixes=None):
    """
    Application code only. Feature evidence (routes, auth, database, error handling,
    environment variables) must come from the app itself: test files use fake passwords,
    throw-away routes and variables such as FOO, which made libraries look like apps and
    polluted the report (e.g. laravel/framework "reads" FOO and SOMETHING_FROM_ENV).
    """
    for f in _iter_files(project_dir, suffixes):
        if not _is_test_file(f.relative_to(project_dir)):
            yield f


def detect_test_categories(project_dir: Path) -> dict:
    all_files = [f.relative_to(project_dir) for f in _iter_files(project_dir)]
    lowered = [str(f).lower() for f in all_files]
    text_blob = " ".join(lowered)

    return {
        "unit_tests": any(_is_test_file(f) and "integration" not in str(f).lower() for f in all_files),
        "integration_tests": any(_is_test_file(f) and "integration" in str(f).lower() for f in all_files),
        "ui_smoke_tests": any(
            name in text_blob for name in ["cypress", "playwright.config", "selenium", "puppeteer"]
        ),
        "database_tests": any(
            name in text_blob for name in ["dbtest", "db_test", "database_test", "test_db"]
        ),
    }


# ----------------------------------------------- config/feature heuristics

def _scan_patterns(project_dir: Path, patterns: list) -> tuple:
    matching_files = []
    files_to_scan = list(_source_files(project_dir, CODE_EXTENSIONS))
    for name in (".env.example", ".env"):
        p = project_dir / name
        if p.is_file() and not p.is_symlink():
            files_to_scan.append(p)

    for file_path in files_to_scan:
        text = _read_text(file_path)
        if text and any(p.search(text) for p in patterns):
            matching_files.append(str(file_path.relative_to(project_dir)))

    return bool(matching_files), matching_files[:10]


def check_database_connectivity(project_dir: Path, is_web: bool = True) -> dict:
    found, files = _scan_patterns(project_dir, DB_CONFIG_PATTERNS)
    has_migrations = (project_dir / "database" / "migrations").exists() or (project_dir / "migrations").exists()
    if not found and not has_migrations and not is_web:
        return _not_applicable("No database usage detected in a non-web project")
    score = 0
    if found:
        score += 60
    if has_migrations:
        score += 40
    return {
        "score": min(score, 100),
        "db_config_detected": found,
        "has_migrations": has_migrations,
        "evidence_files": files,
    }


def check_authentication_flow(project_dir: Path, is_web: bool = True) -> dict:
    if not is_web:
        return _not_applicable("Not a web/API project - authentication not expected")
    found, files = _scan_patterns(project_dir, AUTH_PATTERNS)
    return {
        "score": 100 if found else 0,
        "auth_detected": found,
        "evidence_files": files,
    }


def check_error_handling(project_dir: Path) -> dict:
    found, files = _scan_patterns(project_dir, ERROR_HANDLING_PATTERNS)
    code_files = list(_source_files(project_dir, CODE_EXTENSIONS))
    coverage_ratio = len(files) / max(len(code_files), 1)
    score = min(round(coverage_ratio * 300), 100)
    return {
        "score": score,
        "error_handling_detected": found,
        "evidence_files": files,
    }


def check_security_configuration(project_dir: Path, is_web: bool = True) -> dict:
    secrets_mgmt_found, secrets_files = _scan_patterns(project_dir, SECRETS_MGMT_PATTERNS)

    if not is_web:
        # CORS is meaningless for scripts/ML projects - judge secrets handling only.
        return {
            "score": 100 if secrets_mgmt_found else 0,
            "cors_configured": None,
            "uses_env_based_secrets": secrets_mgmt_found,
            "evidence_files": secrets_files,
            "note": "Non-web project: CORS not assessed",
        }

    cors_found, cors_files = _scan_patterns(project_dir, CORS_PATTERNS)
    score = 0
    if cors_found:
        score += 50
    if secrets_mgmt_found:
        score += 50

    return {
        "score": score,
        "cors_configured": cors_found,
        "uses_env_based_secrets": secrets_mgmt_found,
        "evidence_files": list(set(cors_files + secrets_files))[:10],
    }


def check_required_features(project_dir: Path, api_quality: dict, structure: dict) -> dict:
    signals = []
    score = 0

    if api_quality.get("route_count", 0) > 0:
        signals.append(f"{api_quality['route_count']} route(s) defined")
        score += 30

    if api_quality.get("framework_detected"):
        signals.append(f"web framework detected ({api_quality['framework_detected']})")
        score += 20

    if "README.md" in structure.get("found", []):
        readme_path = _find_readme(project_dir)
        if readme_path is not None and len(_read_text(readme_path)) > 200:
            signals.append("README describes the project (200+ chars)")
            score += 20

    if find_test_dir(project_dir) is not None:
        signals.append("test suite present")
        score += 30

    return {"score": min(score, 100), "signals": signals}


# Values this large in a single .env.example entry are not realistic config and would bloat every
# container's environment; keep them but truncate instead of injecting megabytes of "value".
MAX_ENV_VALUE_LENGTH = 4000
MAX_INJECTED_ENV_VARS = 200


def parse_env_example(project_dir: Path) -> dict:
    """
    Parse KEY=VALUE pairs out of .env.example (comments and blank lines ignored, surrounding matched
    quotes stripped, `export ` prefix tolerated). Used to give submissions the configuration they
    declare they need when they run inside the sandbox - previously nothing from .env.example was
    ever injected, so an app that legitimately needs, say, DATABASE_URL to boot had no way to get it.
    Returns at most MAX_INJECTED_ENV_VARS pairs; a name that cannot be a shell/Docker environment
    variable name (must match [A-Za-z_][A-Za-z0-9_]*) is skipped rather than guessed at.
    """
    env_example = project_dir / ".env.example"
    if not env_example.exists() or env_example.is_symlink():
        return {}
    values = {}
    for line in _read_text(env_example).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value[:MAX_ENV_VALUE_LENGTH]
        if len(values) >= MAX_INJECTED_ENV_VARS:
            break
    return values


def check_environment_configuration(project_dir: Path) -> dict:
    """
    Formal config validation: parses .env.example for declared config
    keys, scans code for actual environment-variable reads, and
    cross-references the two - flags keys the code needs but the repo
    never declares (a real deployment risk), and keys declared but
    unused (likely stale).
    """
    env_example = project_dir / ".env.example"
    declared_keys = set()
    if env_example.exists() and not env_example.is_symlink():
        for line in _read_text(env_example).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                declared_keys.add(line.split("=", 1)[0].strip())

    is_laravel = (project_dir / "artisan").exists()
    used_keys = set()
    required_keys = set()
    for file_path in _source_files(project_dir, CODE_EXTENSIONS):
        text = _read_text(file_path)
        for pattern in ENV_VAR_USAGE_PATTERNS:
            used_keys.update(pattern.findall(text))
        # Laravel's config/*.php read dozens of optional variables (env('DB_URL') is simply null when
        # unset), so only reads outside config/ can make a variable mandatory.
        if is_laravel and file_path.relative_to(project_dir).parts[0] == "config":
            continue
        for pattern in ENV_REQUIRED_PATTERNS:
            required_keys.update(pattern.findall(text))

    used_not_declared = sorted(required_keys - declared_keys)
    declared_not_used = sorted(declared_keys - used_keys)

    if not used_keys and not env_example.exists():
        return _not_applicable("Code reads no environment variables, so there is nothing to declare")

    score = 0
    if env_example.exists():
        score += 40
    if used_keys and not used_not_declared:
        score += 60
    elif used_keys:
        covered = len(used_keys) - len(used_not_declared)
        score += round(60 * (covered / max(len(used_keys), 1)))

    return {
        "score": min(score, 100),
        "has_env_example": env_example.exists(),
        "declared_keys": sorted(declared_keys),
        "used_keys": sorted(used_keys),
        "used_but_not_declared": used_not_declared,
        "declared_but_unused": declared_not_used,
    }
