import zipfile
import pytest

from app import sandbox_engine
from app.sandbox_engine import UnsafeArchiveError, _extract_zip


def _zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def test_single_root_folder_is_flattened(tmp_path):
    z = _zip(tmp_path / "a.zip", {"proj/main.py": "1", "proj/README.md": "x"})
    out = tmp_path / "out"
    out.mkdir()
    _extract_zip(z, out)
    assert sorted(p.name for p in out.iterdir()) == ["README.md", "main.py"]


def test_macos_metadata_folder_does_not_break_flattening(tmp_path):
    z = _zip(tmp_path / "a.zip", {"proj/main.py": "1", "__MACOSX/proj/._main.py": "x"})
    out = tmp_path / "out"
    out.mkdir()
    _extract_zip(z, out)
    assert [p.name for p in out.iterdir()] == ["main.py"]


def test_root_folder_containing_same_named_subfolder(tmp_path):
    z = _zip(tmp_path / "a.zip", {"app/app/x.py": "1", "app/main.py": "2"})
    out = tmp_path / "out"
    out.mkdir()
    _extract_zip(z, out)
    assert (out / "app" / "x.py").exists() and (out / "main.py").exists()


def test_zip_bomb_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_engine, "MAX_UNZIPPED_MB", 1)
    z = _zip(tmp_path / "bomb.zip", {"big.txt": "0" * (3 * 1024 * 1024)})
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UnsafeArchiveError):
        _extract_zip(z, out)


def test_too_many_files_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_engine, "MAX_ZIP_FILES", 5)
    z = _zip(tmp_path / "many.zip", {f"f{i}.txt": "x" for i in range(10)})
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UnsafeArchiveError):
        _extract_zip(z, out)


def test_path_traversal_is_rejected(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../evil.txt", "x")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(UnsafeArchiveError):
        _extract_zip(z, out)
