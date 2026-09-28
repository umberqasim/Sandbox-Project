"""Bonus challenge: AI Plagiarism Detection. No Docker or internet needed.

compute_fingerprint() is tested against real temp-directory files (it walks the filesystem via
analysis._source_files), and check_plagiarism() against the same SQLite test database every other
test file in this suite shares (see conftest.py). submission_ids here use a "plag-" prefix reserved
to this file, and every test uses its own project_type, so nothing collides with other tests.

The "different programs" used throughout come from gen_code(): deterministic Python source whose
STRUCTURE (not just names) differs per seed, so two seeds behave like two unrelated projects.
"""
import itertools
import json
import random
import re
from datetime import datetime, timedelta, timezone

from app import plagiarism
from app.database import Submission, get_session

_counter = itertools.count()


def gen_code(seed, functions=14):
    rnd = random.Random(seed)
    ops = ["+", "-", "*", "//", "%", "&", "|", "^"]
    cmps = ["<", ">", "==", "!=", "<=", ">="]

    def expr(depth=0):
        if depth > 2 or rnd.random() < 0.35:
            return rnd.choice(["a", "b", "c", str(rnd.randint(2, 90)), "len(a)", "abs(b)"])
        form = rnd.choice(["bin", "call", "paren", "idx", "cond"])
        if form == "bin":
            return f"{expr(depth + 1)} {rnd.choice(ops)} {expr(depth + 1)}"
        if form == "call":
            return f"{rnd.choice(['max', 'min', 'sum', 'round'])}([{expr(depth + 1)}, {expr(depth + 1)}])"
        if form == "paren":
            return f"({expr(depth + 1)}) {rnd.choice(ops)} {expr(depth + 1)}"
        if form == "idx":
            return f"[{expr(depth + 1)}, {expr(depth + 1)}, {expr(depth + 1)}][{rnd.randint(0, 2)}]"
        return f"{expr(depth + 1)} if {expr(depth + 1)} {rnd.choice(cmps)} {expr(depth + 1)} else {expr(depth + 1)}"

    def stmt(ind, depth=0):
        pad = "    " * ind
        kind = rnd.choice(["assign", "if", "for", "while", "try", "aug", "assert", "with", "listcomp"])
        if depth > 1 and kind in ("if", "for", "while", "try", "with"):
            kind = "assign"
        if kind == "assign":
            return [f"{pad}v{rnd.randint(1, 9)} = {expr()}"]
        if kind == "aug":
            return [f"{pad}v{rnd.randint(1, 9)} {rnd.choice(ops)}= {expr()}"]
        if kind == "assert":
            return [f"{pad}assert {expr()} {rnd.choice(cmps)} {expr()}"]
        if kind == "listcomp":
            return [f"{pad}v{rnd.randint(1, 9)} = [{expr()} for q in range({rnd.randint(2, 30)}) if q {rnd.choice(cmps)} {expr()}]"]
        body = [line for _ in range(rnd.randint(1, 3)) for line in stmt(ind + 1, depth + 1)]
        if kind == "if":
            out = [f"{pad}if {expr()} {rnd.choice(cmps)} {expr()}:"] + body
            if rnd.random() < 0.5:
                out += [f"{pad}else:"] + [line for line in stmt(ind + 1, depth + 1)]
            return out
        if kind == "for":
            return [f"{pad}for q in range({expr()}):"] + body
        if kind == "while":
            return [f"{pad}while {expr()} {rnd.choice(cmps)} {expr()}:"] + body + [f"{pad}    break"]
        if kind == "with":
            return [f"{pad}with open({expr()}) as fh:"] + body
        return [f"{pad}try:"] + body + [f"{pad}except {rnd.choice(['ValueError', 'KeyError', 'OSError'])}:", f"{pad}    pass"]

    out = []
    for i in range(functions):
        out.append(f"def fn_{seed}_{i}(a, b, c):")
        for _ in range(rnd.randint(5, 10)):
            out.extend(stmt(1))
        out.append(f"    return {expr()}")
        out.append("")
        out.append("")
    return "\n".join(out)


def rename_identifiers(code):
    """What someone hiding a copy does: rename every function, argument and variable."""
    code = re.sub(r"fn_(\d+)_(\d+)", r"process_\1_\2", code)
    code = re.sub(r"\bv(\d)\b", r"tmp\1", code)
    for old, new in {"a": "alpha", "b": "beta", "c": "gamma", "q": "idx", "fh": "handle"}.items():
        code = re.sub(rf"\b{old}\b", new, code)
    return code


def _project(tmp_path, files):
    for name, content in files.items():
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return plagiarism.compute_fingerprint(tmp_path)


def _sim(fp_a, fp_b):
    ea, na = plagiarism._project_sets(fp_a)
    eb, nb = plagiarism._project_sets(fp_b)
    return plagiarism._jaccard_percent(ea, eb), plagiarism._jaccard_percent(na, nb)


def _seed_submission(project_type, fingerprint, repo_url=None, n=0):
    sid = f"plag-{next(_counter)}"
    session = get_session()
    try:
        session.add(Submission(
            submission_id=sid,
            repo_url=repo_url or f"https://github.com/example/{sid}",
            project_type=project_type,
            status="success",
            build_success=True,
            execution_success=True,
            duration_seconds=5.0,
            scores={"_plagiarism_fingerprint": fingerprint},
            created_at=datetime.now(timezone.utc) - timedelta(minutes=100 - n),
        ))
        session.commit()
    finally:
        session.close()
    return sid


# --------------------------------------------------------------- compute_fingerprint

def test_empty_directory_has_no_fingerprint(tmp_path):
    assert plagiarism.compute_fingerprint(tmp_path)["files"] == {}


def test_a_normal_source_file_produces_a_fingerprint(tmp_path):
    fp = _project(tmp_path, {"app.py": gen_code(1)})
    assert fp["file_count"] == 1
    assert fp["files"]["app.py"]["e"] and fp["files"]["app.py"]["n"]


def test_very_short_file_has_no_fingerprint(tmp_path):
    assert _project(tmp_path, {"app.py": "x = 1"})["files"] == {}


def test_comments_and_strings_do_not_change_the_fingerprint(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    lines = gen_code(1).split("\n")
    noisy = []
    for i, line in enumerate(lines):
        noisy.append(line + ("  # note %d" % i if line.strip() and i % 3 == 0 else ""))
    fp_a = _project(a, {"app.py": gen_code(1)})
    fp_b = _project(b, {"app.py": "# header comment\n" + "\n".join(noisy) + "\ns = 'anything at all'\n"})
    exact, struct = _sim(fp_a, fp_b)
    assert exact >= 90.0 and struct >= 90.0


def test_unrelated_code_has_low_similarity(tmp_path):
    fp_a = _project(tmp_path / "a", {"app.py": gen_code(1)})
    fp_b = _project(tmp_path / "b", {"app.py": gen_code(2)})
    exact, struct = _sim(fp_a, fp_b)
    assert exact < plagiarism.NOTE_EXACT
    assert struct < plagiarism.NOTE_STRUCT


def test_test_files_are_excluded_like_the_rest_of_static_analysis(tmp_path):
    assert _project(tmp_path, {"tests/test_app.py": gen_code(1)})["files"] == {}


def test_react_and_flutter_files_are_fingerprinted(tmp_path):
    jsx = "\n".join(f"const Item{i} = ({{ a, b }}) => {{ return a.map((x) => x + b * {i}); }};" for i in range(20))
    dart = "\n".join(f"int total{i}(List<int> a, int b) {{ return a.fold(b, (x, y) => x + y * {i}); }}" for i in range(20))
    fp = _project(tmp_path, {"src/App.jsx": jsx, "lib/main.dart": dart})
    assert set(fp["files"]) == {"src/App.jsx", "lib/main.dart"}


def test_only_hashes_are_stored_never_source_text(tmp_path):
    fp = _project(tmp_path, {"app.py": gen_code(1)})
    blob = json.dumps(fp)
    assert "fn_1_0" not in blob and "def " not in blob


# ------------------------------------------------- renaming / reordering (structural)

def test_renamed_copy_is_missed_by_exact_but_caught_by_structural(tmp_path):
    original = gen_code(7)
    fp_a = _project(tmp_path / "a", {"app.py": original})
    fp_b = _project(tmp_path / "b", {"app.py": rename_identifiers(original)})
    exact, struct = _sim(fp_a, fp_b)
    assert exact < plagiarism.NOTE_EXACT        # the old exact-only check would have said "not similar"
    assert struct >= 90.0


def test_reordered_functions_still_match(tmp_path):
    original = gen_code(8)
    blocks = [b for b in original.split("\n\n\n") if b.strip()]
    shuffled = "\n\n\n".join(reversed(blocks))
    fp_a = _project(tmp_path / "a", {"app.py": original})
    fp_b = _project(tmp_path / "b", {"app.py": rename_identifiers(shuffled)})
    _, struct = _sim(fp_a, fp_b)
    assert struct >= 90.0


# --------------------------------------------------------------- check_plagiarism

def test_no_source_code_is_not_checked():
    result = plagiarism.check_plagiarism({"version": 2, "files": {}}, "python")
    assert result == {"checked": False, "reason": "no application source code to fingerprint"}


def test_no_prior_submissions_of_this_project_type(tmp_path):
    fp = _project(tmp_path, {"app.py": gen_code(1)})
    result = plagiarism.check_plagiarism(fp, "plagtype-never-seen-before")
    assert result["checked"] is True
    assert result["matches"] == [] and result["compared_against"] == 0 and result["flagged"] is False


def test_verbatim_copy_is_flagged(tmp_path):
    fp = _project(tmp_path, {"app.py": gen_code(1)})
    _seed_submission("plagtype-a", fp, repo_url="https://github.com/other/repo")
    result = plagiarism.check_plagiarism(fp, "plagtype-a", repo_url="https://github.com/mine/repo")
    assert result["flagged"] is True
    match = result["matches"][0]
    assert match["repo_url"] == "https://github.com/other/repo"
    assert match["exact_percent"] == 100.0 and match["structural_percent"] == 100.0


def test_renamed_copy_is_flagged_end_to_end(tmp_path):
    original = gen_code(11)
    theirs = _project(tmp_path / "a", {"app.py": original})
    mine = _project(tmp_path / "b", {"app.py": rename_identifiers(original)})
    _seed_submission("plagtype-rename", theirs, repo_url="https://github.com/other/repo")
    result = plagiarism.check_plagiarism(mine, "plagtype-rename", repo_url="https://github.com/mine/repo")
    assert result["flagged"] is True
    match = result["matches"][0]
    assert match["exact_percent"] < plagiarism.NOTE_EXACT
    assert match["structural_percent"] >= plagiarism.FLAG_STRUCT
    assert match["similarity_percent"] == match["structural_percent"]


def test_different_project_type_is_not_compared(tmp_path):
    fp = _project(tmp_path, {"app.py": gen_code(1)})
    _seed_submission("plagtype-b-other-lang", fp)
    result = plagiarism.check_plagiarism(fp, "plagtype-b-this-lang")
    assert result["compared_against"] == 0 and result["matches"] == []


def test_own_repo_url_is_excluded_so_reevaluate_cannot_flag_itself(tmp_path):
    fp = _project(tmp_path, {"app.py": gen_code(1)})
    same_repo = "https://github.com/mine/repo"
    _seed_submission("plagtype-c", fp, repo_url=same_repo)
    result = plagiarism.check_plagiarism(fp, "plagtype-c", repo_url=same_repo)
    assert result["matches"] == [] and result["compared_against"] == 0


def test_unrelated_prior_submission_is_compared_but_not_reported(tmp_path):
    other = _project(tmp_path / "a", {"app.py": gen_code(2)})
    _seed_submission("plagtype-d", other)
    mine = _project(tmp_path / "b", {"app.py": gen_code(1)})
    result = plagiarism.check_plagiarism(mine, "plagtype-d")
    assert result["compared_against"] == 1 and result["matches"] == []


def test_matches_are_capped_and_flagged_ones_come_first(tmp_path):
    mine = _project(tmp_path / "me", {"app.py": gen_code(1)})
    exact_copy = _project(tmp_path / "x", {"app.py": gen_code(1)})
    half = gen_code(1).split("\n\n\n")
    partial = _project(tmp_path / "p", {"app.py": "\n\n\n".join(half[:9]) + "\n\n\n" + gen_code(50, 5)})
    _seed_submission("plagtype-e", partial, repo_url="https://github.com/x/partial", n=1)
    _seed_submission("plagtype-e", exact_copy, repo_url="https://github.com/x/exact", n=2)
    for i in range(4):
        _seed_submission("plagtype-e", _project(tmp_path / f"z{i}", {"app.py": gen_code(60 + i)}), n=3 + i)
    result = plagiarism.check_plagiarism(mine, "plagtype-e")
    assert len(result["matches"]) <= plagiarism.MAX_MATCHES_SHOWN
    assert result["matches"][0]["repo_url"] == "https://github.com/x/exact"
    assert result["matches"][0]["flagged"] is True


def test_rows_without_a_stored_fingerprint_are_skipped_not_counted(tmp_path):
    session = get_session()
    try:
        session.add(Submission(
            submission_id="plag-nofp", repo_url="https://github.com/x/old",
            project_type="plagtype-f", status="success", build_success=True,
            execution_success=True, duration_seconds=1.0,
            scores={},  # no "_plagiarism_fingerprint" key - an older record
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()
    finally:
        session.close()
    mine = _project(tmp_path, {"app.py": gen_code(1)})
    result = plagiarism.check_plagiarism(mine, "plagtype-f")
    assert result["compared_against"] == 0 and result["matches"] == []


def test_older_v1_fingerprints_are_still_compared_on_exact_windows(tmp_path):
    mine = _project(tmp_path, {"app.py": gen_code(1)})
    exact, _ = plagiarism._project_sets(mine)
    _seed_submission("plagtype-v1", {"shingles": sorted(exact)}, repo_url="https://github.com/old/format")
    result = plagiarism.check_plagiarism(mine, "plagtype-v1")
    assert result["compared_against"] == 1
    match = result["matches"][0]
    assert match["exact_percent"] == 100.0 and match["structural_percent"] is None
    assert match["flagged"] is True


# ------------------------------------------------------------ file-level (partial copy)

def test_one_copied_file_inside_a_bigger_project_is_found(tmp_path):
    victim = _project(tmp_path / "victim", {"core.py": gen_code(1), "util.py": gen_code(2)})
    _seed_submission("plagtype-partial", victim, repo_url="https://github.com/victim/repo")
    mine = _project(tmp_path / "mine", {
        "a.py": gen_code(30), "b.py": gen_code(31), "c.py": gen_code(32), "d.py": gen_code(33),
        "helpers/my_module.py": gen_code(1),    # "wrote" this one... it is core.py
    })
    result = plagiarism.check_plagiarism(mine, "plagtype-partial")
    assert result["flagged"] is True
    match = result["matches"][0]
    assert match["similarity_percent"] < plagiarism.NOTE_STRUCT        # the project as a whole looks fine
    assert match["matched_files"][0]["file"] == "helpers/my_module.py"
    assert match["matched_files"][0]["matched_file"] == "core.py"
    assert match["matched_files"][0]["match_percent"] >= plagiarism.FILE_FLAG


def test_small_files_are_not_matched_at_file_level(tmp_path):
    tiny = "def f(a):\n    return a + 1\n\ndef g(b):\n    return b * 2\n\nprint(f(1), g(2))\n"
    victim = _project(tmp_path / "v", {"tiny.py": tiny, "big.py": gen_code(2)})
    mine = _project(tmp_path / "m", {"tiny.py": tiny, "big.py": gen_code(3)})
    _seed_submission("plagtype-tiny", victim)
    result = plagiarism.check_plagiarism(mine, "plagtype-tiny")
    for match in result["matches"]:
        assert all(f["file"] != "tiny.py" for f in match["matched_files"])


# ------------------------------------------------------------------ boilerplate

def _starter():
    return gen_code(100, functions=30)     # the shared starter template, bigger than any student's own code


def _student(tmp_path, seed):
    return _project(tmp_path, {"starter.py": _starter(), "app.py": gen_code(seed, functions=8)})


def test_shared_starter_code_is_flagged_when_there_is_too_little_history_to_tell(tmp_path):
    for i in range(2):                      # fewer than BOILERPLATE_MIN_CORPUS earlier submissions
        _seed_submission("plagtype-few", _student(tmp_path / f"p{i}", 200 + i), n=i)
    result = plagiarism.check_plagiarism(_student(tmp_path / "me", 300), "plagtype-few")
    assert result["boilerplate_ignored"] is False
    assert result["flagged"] is True        # documented limit: with 2 examples, common code cannot be recognised


def test_shared_starter_code_is_ignored_once_enough_submissions_share_it(tmp_path):
    for i in range(6):
        _seed_submission("plagtype-many", _student(tmp_path / f"p{i}", 200 + i), n=i)
    result = plagiarism.check_plagiarism(_student(tmp_path / "me", 300), "plagtype-many")
    assert result["boilerplate_ignored"] is True
    assert result["boilerplate_ignored_percent"] > 50.0
    assert result["matches"] == [] and result["flagged"] is False


def test_real_copying_is_still_caught_when_starter_code_is_ignored(tmp_path):
    for i in range(6):
        _seed_submission("plagtype-copy", _student(tmp_path / f"p{i}", 200 + i), repo_url=f"https://github.com/s/{i}", n=i)
    copier = _student(tmp_path / "me", 202)           # same starter AND the same own code as student 2
    result = plagiarism.check_plagiarism(copier, "plagtype-copy")
    assert result["flagged"] is True
    assert result["matches"][0]["repo_url"] == "https://github.com/s/2"
    assert result["matches"][0]["structural_percent"] >= plagiarism.FLAG_STRUCT
