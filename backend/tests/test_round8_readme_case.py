"""README detection must not depend on letter case (expressjs/express ships 'Readme.md')."""
import pytest

from app import analysis, feedback_engine

LONG = "# Project\n\n## Usage\n\n" + "Describes what the project does and how to run it. " * 12


@pytest.mark.parametrize("name", ["README.md", "Readme.md", "readme.md", "ReadMe.MD"])
def test_readme_is_found_in_any_letter_case(tmp_path, name):
    (tmp_path / name).write_text(LONG)
    (tmp_path / "package.json").write_text("{}")
    structure = analysis.check_structure(tmp_path, "node")
    assert "README.md" in structure["found"] and "README.md" not in structure["missing"]
    assert structure["score"] == 25 + 25
    doc = analysis.check_documentation(tmp_path)
    assert "README.md present" in doc["signals"] and doc["score"] >= 70
    features = analysis.check_required_features(tmp_path, {}, structure)
    assert "README describes the project (200+ chars)" in features["signals"]


def test_all_casings_score_the_same(tmp_path_factory):
    scores = set()
    for name in ("README.md", "Readme.md"):
        d = tmp_path_factory.mktemp("p")
        (d / name).write_text(LONG)
        scores.add((analysis.check_structure(d, "python")["score"], analysis.check_documentation(d)["score"]))
    assert len(scores) == 1


def test_missing_readme_is_still_reported(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    structure = analysis.check_structure(tmp_path, "node")
    assert "README.md" in structure["missing"]
    assert analysis.check_documentation(tmp_path)["score"] == 0


def test_a_folder_or_symlink_named_readme_does_not_count(tmp_path):
    (tmp_path / "Readme.md").mkdir()
    assert analysis.check_documentation(tmp_path)["score"] == 0
    other = tmp_path / "other"
    other.mkdir()
    target = tmp_path / "real.txt"
    target.write_text(LONG)
    (other / "readme.md").symlink_to(target)
    assert analysis._find_readme(other) is None


def test_monorepo_root_readme_in_any_case_counts(tmp_path):
    sub = tmp_path / "backend"
    sub.mkdir()
    (tmp_path / "Readme.md").write_text(LONG)
    assert "README.md" in analysis.check_structure(sub, "python", tmp_path)["found"]
    assert analysis.check_documentation(sub, tmp_path)["score"] > analysis.check_documentation(sub)["score"]


def test_scoring_version_was_bumped_because_scores_changed():
    assert feedback_engine.SCORING_VERSION >= 3
