import zipfile
from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.ingestion.errors import IngestionError
from repomind.ingestion.sources import ZipSource, source_from_location
from repomind.services.analysis_service import AnalysisService
from repomind.services.repository_service import RepositoryService
from repomind.storage import AnalysisStore


def _settings(tmp_path, **kw):
    return Settings(workspace_dir=tmp_path / "ws", log_level="WARNING", **kw)


def _repo(tmp_path, files):
    root = tmp_path / "repo"
    for name, text in files.items():
        f = root / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def _paths(inv):
    return {f.path for f in inv.files}


# ------------------------------------------------------------------ configurable exclusions
def test_extra_ignored_patterns_from_settings(tmp_path):
    root = _repo(tmp_path, {"a.py": "x=1", "gen/b.py": "x=1", "c_test.py": "x=1"})
    inv = RepositoryService(_settings(tmp_path, extra_ignored=("gen", "*_test.py"))).ingest(str(root))
    assert _paths(inv) == {"a.py"} and inv.skipped["excluded_by_rule"] == 2


def test_repomindignore_file_in_the_repository(tmp_path):
    root = _repo(tmp_path, {"a.py": "x=1", "legacy/old.py": "x=1", ".repomindignore": "# old code\nlegacy/\n"})
    assert "legacy/old.py" not in _paths(RepositoryService(_settings(tmp_path)).ingest(str(root)))


def test_per_call_exclude_and_file_limit(tmp_path):
    root = _repo(tmp_path, {f"m{i}.py": "x=1" for i in range(5)})
    svc = RepositoryService(_settings(tmp_path, max_files=3))
    inv = svc.ingest(str(root))
    assert len(inv.files) == 3 and inv.skipped["file_limit_reached"] == 2
    assert "m0.py" not in _paths(RepositoryService(_settings(tmp_path)).ingest(str(root), exclude=["m0.py"]))


def test_oversized_files_are_skipped(tmp_path):
    root = _repo(tmp_path, {"big.py": "x=1\n" * 1000, "small.py": "x=1"})
    inv = RepositoryService(_settings(tmp_path, max_file_bytes=100)).ingest(str(root))
    assert _paths(inv) == {"small.py"} and inv.skipped["too_large"] == 1


# ------------------------------------------------------------------ zip ingestion
def _zip(tmp_path, entries, name="proj.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for n, data in entries.items():
            z.writestr(n, data)
    return path


def test_zip_is_extracted_and_single_top_folder_is_flattened(tmp_path):
    z = _zip(tmp_path, {"repo-main/app.py": "def f():\n    pass\n", "repo-main/README.md": "hi"})
    assert isinstance(source_from_location(str(z)), ZipSource)
    analysis = AnalysisService(_settings(tmp_path)).analyze(str(z))
    assert analysis.name == "repo-main" and analysis.origin == "zip:proj.zip"
    assert "python:app.py:f" in {e.id for e in analysis.entities}


@pytest.mark.parametrize("evil", ["../escape.py", "/abs.py", "C:/win.py", "a/../../escape.py"])
def test_zip_slip_and_absolute_paths_are_rejected(tmp_path, evil):
    z = _zip(tmp_path, {evil: "x=1"})
    with pytest.raises(IngestionError):
        ZipSource(z).resolve(tmp_path / "ws")
    assert not (tmp_path / "escape.py").exists()


def test_zip_symlink_entry_is_rejected(tmp_path):
    path = tmp_path / "link.zip"
    with zipfile.ZipFile(path, "w") as z:
        info = zipfile.ZipInfo("evil")
        info.external_attr = (0o120777 << 16)
        z.writestr(info, "/etc/passwd")
    with pytest.raises(IngestionError, match="Symlink"):
        ZipSource(path).resolve(tmp_path / "ws")


def test_zip_bomb_and_garbage_are_rejected(tmp_path):
    big = _zip(tmp_path, {"a.txt": "0" * 3_000_000}, "bomb.zip")
    with pytest.raises(IngestionError, match="limit"):
        ZipSource(big, max_zip_mb=1).resolve(tmp_path / "ws")
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip at all")
    with pytest.raises(IngestionError, match="valid zip"):
        ZipSource(junk).resolve(tmp_path / "ws")


# ------------------------------------------------------------------ robustness
def test_syntax_error_file_is_reported_and_does_not_stop_analysis(tmp_path):
    root = _repo(tmp_path, {"good.py": "def ok():\n    pass\n", "bad.py": "def broken(:\n"})
    a = AnalysisService(_settings(tmp_path)).analyze(str(root))
    assert "python:good.py:ok" in {e.id for e in a.entities}
    assert any("bad.py" in e and "syntax error" in e for e in a.stats["analysis_errors"])


def test_analyzer_crash_is_isolated(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"a.py": "def a():\n    pass\n", "b.js": "function b() {}"})
    svc = AnalysisService(_settings(tmp_path))
    monkeypatch.setattr(svc.python_analyzer, "analyze_file", lambda *a, **k: 1 / 0)
    a = svc.analyze(str(root))
    assert "javascript:b.js:b" in {e.id for e in a.entities}                 # other languages unaffected
    assert any("analyzer crashed" in e for e in a.stats["analysis_errors"])


def test_undecodable_file_is_reported(tmp_path):
    root = _repo(tmp_path, {"ok.py": "def ok():\n    pass\n"})
    (root / "latin.py").write_bytes("# caf\xe9\ndef x():\n    pass\n".encode("latin-1"))
    a = AnalysisService(_settings(tmp_path)).analyze(str(root))
    assert any("latin.py" in e and "unreadable" in e for e in a.stats["analysis_errors"])


# ------------------------------------------------------------------ storage
def test_store_roundtrip_and_unsafe_ids(tmp_path):
    root = _repo(tmp_path, {"a.py": "def a():\n    pass\n"})
    analysis = AnalysisService(_settings(tmp_path)).analyze(str(root))
    store = AnalysisStore(tmp_path / "store")
    store.save(analysis)
    assert store.ids() == ["repo"]
    loaded = store.load("repo")
    assert loaded.entity_count == analysis.entity_count and loaded.relationship_count == analysis.relationship_count
    assert store.load("missing") is None
    for bad in ("../x", "a/b", "..", ""):
        with pytest.raises(ValueError):
            store.load(bad)
    assert store.delete("repo") and store.ids() == []


def test_corrupt_stored_analysis_is_ignored(tmp_path):
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "bad.json").write_text("{not json", encoding="utf-8")
    assert AnalysisStore(tmp_path / "store").load("bad") is None
