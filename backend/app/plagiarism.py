"""
Bonus challenge: AI Plagiarism Detection (case study bonus challenges list).

A lightweight, dependency-free similarity check in the spirit of MOSS / JPlag / Dolos - not a
replacement for them. It is a mentor-facing signal only: it never changes any score, and a
submission with nothing to compare (a Docker image, a near-empty repo, or the very first
submission of a given project type) is simply not checked, never penalised. It makes no legal
or academic-integrity determination.

How it works
  1. Every application source file (same file set as the rest of static analysis - see
     analysis._source_files, test files and vendored/build folders excluded) is read on its own,
     comments and string-literal contents are stripped, and the rest is tokenised.
  2. Two fingerprints are made per file, both as sets of hashed token windows ("shingles"):
       exact       identifiers kept          -> catches verbatim / lightly edited copies
       structural  every identifier -> ID,   -> catches copies whose variables, functions and
                   numbers -> NUM               classes were renamed (structure and keywords stay)
  3. The new submission is compared with earlier submissions of the same project type:
       - project level: Jaccard similarity of the exact sets and of the structural sets
       - file level:    which of this submission's files sit inside which of the other's files
                        (containment, so a single copied file inside a bigger project is found)
  4. Common code is ignored automatically: a window that already appears in many earlier
     submissions (starter template, framework scaffold, tutorial boilerplate) says nothing about
     copying, so it is removed from both sides before comparing - once there are enough earlier
     submissions (BOILERPLATE_MIN_CORPUS) to tell "common" from "copied".
  5. The fingerprint is stored on the submission's own scores (`_plagiarism_fingerprint`,
     underscore-prefixed - internal bookkeeping, never rendered) for future comparisons. Only
     hashes are stored, never source code.

Known limits (see docs/technical-justifications.md): no cross-language detection, no search of
GitHub / the web, and a copy that is heavily restructured (logic rewritten, not just renamed or
reordered) will not be found. Shared starter code below the boilerplate threshold can still
register, which is why the result is worded as a heuristic.
"""
import math
import re
import zlib
from collections import Counter
from pathlib import Path

from . import analysis

# analysis.CODE_EXTENSIONS ({".py", ".js", ".ts", ".php"}) is deliberately narrow: it only needs
# the languages that carry route/auth/db evidence for the rest of static analysis. Plagiarism
# detection needs a wider net so it covers every stack this case study lists (Laravel, MERN,
# Python/AI, Flutter, DevOps) - notably React (.jsx/.tsx) and Flutter (.dart), which the narrow
# list would silently skip, disabling the check for those submissions without any error.
PLAGIARISM_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".php", ".java", ".go", ".rb",
    ".dart", ".c", ".cpp", ".cs", ".vue",
}

FINGERPRINT_VERSION = 2
EXACT_SHINGLE = 5          # tokens per exact shingle
STRUCT_SHINGLE = 8         # tokens per structural shingle (longer: identifiers are erased, so
                           # short windows of pure structure match between unrelated programs)
MAX_FILES = 300            # files fingerprinted per submission
MAX_TOTAL_SHINGLES = 80_000  # storage cap per submission (exact + structural, all files)

# Project-level thresholds (percent Jaccard). Calibrated on real code, see tests.
NOTE_EXACT = 25.0
FLAG_EXACT = 50.0
NOTE_STRUCT = 35.0
FLAG_STRUCT = 65.0
# File-level: a file counts as matched when this share of ITS windows is found in one file of the
# other submission; ignore files too small to say anything.
MIN_FILE_SHINGLES = 60   # ~70 tokens (roughly a dozen lines): a 5-line helper matching says nothing
FILE_NOTE = 60.0
FILE_FLAG = 85.0
MAX_FILES_SHOWN = 5
FILE_CHECK_MIN_CONTAINMENT = 0.15   # project-level containment needed to bother with file level
MAX_FILE_CHECKS = 10

BOILERPLATE_MIN_CORPUS = 5   # need at least this many earlier submissions before ignoring common code
BOILERPLATE_MIN_COUNT = 3    # ... and a window must appear in at least this many of them
BOILERPLATE_MIN_SHARE = 0.30  # ... and in at least this share of them

MAX_MATCHES_SHOWN = 3
MAX_PRIOR_SUBMISSIONS_COMPARED = 200  # newest-first cap, same pattern as /leaderboard

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/|\"\"\".*?\"\"\"|'''.*?'''", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(#|//).*")
_STRING_LITERAL_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
_TOKEN_RE = re.compile(r"[A-Za-z_]\w*|\d+(?:\.\d+)?|[^\sA-Za-z0-9_]")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*$")

# Words that carry program STRUCTURE across the supported languages. Everything else that looks
# like a name (variables, functions, classes, imported modules, framework calls) becomes ID in the
# structural fingerprint - which is exactly what someone renaming things to hide a copy changes.
KEYWORDS = frozenset("""
STR
False None True and as assert async await break class continue def del elif else except finally for
from global if import in is lambda nonlocal not or pass raise return try while with yield self
const let var function new delete typeof instanceof void this super extends export default switch
case do of static get set interface type enum implements public private protected readonly abstract
namespace declare throw catch null undefined true false
echo print elseif foreach trait use final array isset unset empty list xor parent require include
require_once include_once endif endforeach endwhile
int long float double boolean char byte short string bool synchronized package throws override
virtual internal sealed struct func go defer chan map range select fallthrough goto dynamic late
required mixin on end module unless until begin rescue ensure then nil val fun when object
""".split())


def _normalize(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    text = _STRING_LITERAL_RE.sub(" STR ", text)
    return text


def _canon(token: str) -> str:
    """Structural form of a token: keep keywords and punctuation, erase names and numbers."""
    if token[0].isdigit():
        return "NUM"
    if _IDENT_RE.match(token) and token not in KEYWORDS:
        return "ID"
    return token


def _shingle_set(tokens: list, size: int) -> set:
    out = set()
    for i in range(len(tokens) - size + 1):
        out.add(zlib.crc32(" ".join(tokens[i:i + size]).encode("utf-8", errors="ignore")))
    return out


def compute_fingerprint(project_dir: Path) -> dict:
    """
    {"version": 2, "files": {relative/path: {"e": [exact shingles], "n": [structural shingles]}},
     "file_count", "token_count", "truncated"}.
    A submission with nothing to fingerprint (empty, or every file below the window size) gets
    files == {} - callers treat that as "not checked", never as "0% similar to everything".
    """
    project_dir = Path(project_dir)
    files = {}
    token_count = 0
    total = 0
    truncated = False
    for path in sorted(analysis._source_files(project_dir, PLAGIARISM_EXTENSIONS)):
        if len(files) >= MAX_FILES or total >= MAX_TOTAL_SHINGLES:
            truncated = True
            break
        text = analysis._read_text(path)
        if not text:
            continue
        tokens = _TOKEN_RE.findall(_normalize(text))
        token_count += len(tokens)
        exact = _shingle_set(tokens, EXACT_SHINGLE)
        struct = _shingle_set([_canon(t) for t in tokens], STRUCT_SHINGLE)
        if not exact and not struct:
            continue
        total += len(exact) + len(struct)
        # Sorted purely for compact, deterministic JSON - comparison treats these as sets.
        files[path.relative_to(project_dir).as_posix()] = {"e": sorted(exact), "n": sorted(struct)}
    return {"version": FINGERPRINT_VERSION, "files": files, "file_count": len(files),
            "token_count": token_count, "truncated": truncated}


def _project_sets(fingerprint: dict):
    """(exact_set, structural_set_or_None). Also reads the older v1 shape {"shingles": [...]}
    that earlier submissions were stored with - those can only be compared on exact windows."""
    if not fingerprint:
        return set(), None
    files = fingerprint.get("files")
    if files:
        exact, struct = set(), set()
        for entry in files.values():
            exact.update(entry.get("e") or ())
            struct.update(entry.get("n") or ())
        return exact, (struct or None)
    return set(fingerprint.get("shingles") or ()), None


def _jaccard_percent(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return round((len(a & b) / union) * 100, 1) if union else 0.0


def _containment(a: set, b: set) -> float:
    """Share of `a` that is also in `b` (0..1)."""
    return (len(a & b) / len(a)) if a else 0.0


def _file_matches(cur_files: dict, other_fp: dict, common: set) -> list:
    """Which of this submission's files are (mostly) contained in one file of the other
    submission. Structural windows, boilerplate removed, small files ignored."""
    other_files = (other_fp or {}).get("files") or {}
    if not cur_files or not other_files:
        return []
    other_sets = {name: set(e.get("n") or ()) - common for name, e in other_files.items()}
    found = []
    for name, entry in cur_files.items():
        a = set(entry.get("n") or ()) - common
        if len(a) < MIN_FILE_SHINGLES:
            continue
        best_name, best = None, 0.0
        for other_name, b in other_sets.items():
            if len(b) < MIN_FILE_SHINGLES:
                continue
            c = _containment(a, b)
            if c > best:
                best_name, best = other_name, c
        if best_name and best * 100 >= FILE_NOTE:
            found.append({"file": name, "matched_file": best_name,
                          "match_percent": round(best * 100, 1), "windows": len(a)})
    found.sort(key=lambda m: (-m["match_percent"], -m["windows"]))
    return found[:MAX_FILES_SHOWN]


def check_plagiarism(fingerprint: dict, project_type: str, repo_url: str = None) -> dict:
    """
    Compares `fingerprint` (from compute_fingerprint) against stored fingerprints of earlier
    submissions of the same project_type. Best-effort: any database problem is reported as
    checked=False rather than failing the evaluation.

    `repo_url`, when given, excludes earlier rows with that exact repo_url - otherwise the
    dashboard's "Re-evaluate" button (or re-uploading the same ZIP) would compare a project with
    its own previous run and flag it as 100% plagiarism of itself.
    """
    cur_exact, cur_struct = _project_sets(fingerprint)
    if not cur_exact and not cur_struct:
        return {"checked": False, "reason": "no application source code to fingerprint"}

    try:
        from .database import get_session, Submission
        session = get_session()
    except Exception as exc:  # noqa: BLE001 - must never fail the evaluation
        return {"checked": False, "reason": f"database unavailable ({type(exc).__name__})"}

    try:
        rows = (
            session.query(Submission)
            .filter(Submission.project_type == project_type)
            .order_by(Submission.created_at.desc())
            .limit(MAX_PRIOR_SUBMISSIONS_COMPARED)
            .all()
        )
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "reason": f"database query failed ({type(exc).__name__})"}
    finally:
        session.close()

    candidates = []
    for row in rows:
        if repo_url and row.repo_url == repo_url:
            continue
        other_fp = (row.scores or {}).get("_plagiarism_fingerprint")
        exact, struct = _project_sets(other_fp)
        if not exact and not struct:
            continue
        candidates.append({"row": row, "fp": other_fp, "exact": exact, "struct": struct})
    compared = len(candidates)

    # Automatic boilerplate: windows that occur in many earlier submissions are the starter
    # template / framework scaffold everybody shares, not evidence of copying between two people.
    common_exact, common_struct = set(), set()
    boilerplate_percent = 0.0
    if compared >= BOILERPLATE_MIN_CORPUS:
        need = max(BOILERPLATE_MIN_COUNT, math.ceil(BOILERPLATE_MIN_SHARE * compared))
        df_e, df_n = Counter(), Counter()
        for c in candidates:
            df_e.update(c["exact"])
            if c["struct"]:
                df_n.update(c["struct"])
        common_exact = {s for s, n in df_e.items() if n >= need}
        common_struct = {s for s, n in df_n.items() if n >= need}
        basis, common = (cur_struct, common_struct) if cur_struct else (cur_exact, common_exact)
        boilerplate_percent = round(_containment(basis, common) * 100, 1)

    cur_e = cur_exact - common_exact
    cur_n = (cur_struct - common_struct) if cur_struct else None
    cur_files = (fingerprint or {}).get("files") or {}

    scored = []
    for c in candidates:
        e = c["exact"] - common_exact
        n = (c["struct"] - common_struct) if c["struct"] else None
        exact_pct = _jaccard_percent(cur_e, e)
        struct_pct = _jaccard_percent(cur_n, n) if (cur_n and n) else None
        contain = max(_containment(cur_n, n), _containment(n, cur_n)) if (cur_n and n) else 0.0
        scored.append({"c": c, "exact": exact_pct, "struct": struct_pct, "contain": contain})

    def project_level(s):
        return max(s["exact"], s["struct"] or 0.0)

    # File-level check only where it can matter: an already noticeable project match, or enough
    # shared windows that one copied file inside a bigger project is plausible.
    to_check = sorted(
        (s for s in scored if project_level(s) >= min(NOTE_EXACT, NOTE_STRUCT) or s["contain"] >= FILE_CHECK_MIN_CONTAINMENT),
        key=lambda s: (-project_level(s), -s["contain"]),
    )[:MAX_FILE_CHECKS]
    checked_ids = {id(s) for s in to_check}

    matches = []
    for s in scored:
        files = _file_matches(cur_files, s["c"]["fp"], common_struct) if id(s) in checked_ids else []
        top_file = files[0]["match_percent"] if files else 0.0
        flagged = (
            s["exact"] >= FLAG_EXACT
            or (s["struct"] is not None and s["struct"] >= FLAG_STRUCT)
            or top_file >= FILE_FLAG
        )
        noted = (
            flagged
            or s["exact"] >= NOTE_EXACT
            or (s["struct"] is not None and s["struct"] >= NOTE_STRUCT)
            or top_file >= FILE_NOTE
        )
        if not noted:
            continue
        row = s["c"]["row"]
        matches.append({
            "submission_id": row.submission_id,
            "repo_url": row.repo_url,
            "similarity_percent": project_level(s),
            "exact_percent": s["exact"],
            "structural_percent": s["struct"],
            "matched_files": files,
            "flagged": flagged,
        })

    matches.sort(key=lambda m: (not m["flagged"], -m["similarity_percent"]))
    matches = matches[:MAX_MATCHES_SHOWN]
    highest = max((m["similarity_percent"] for m in matches), default=0.0)

    return {
        "checked": True,
        "compared_against": compared,
        "matches": matches,
        "highest_similarity_percent": highest,
        "flagged": any(m["flagged"] for m in matches),
        "boilerplate_ignored": compared >= BOILERPLATE_MIN_CORPUS,
        "boilerplate_ignored_percent": boilerplate_percent,
        "truncated": bool((fingerprint or {}).get("truncated")),
    }
