import json
from pathlib import Path

import pytest

from repomind.config import Settings
from repomind.ingestion.classifier import classify_file
from repomind.ingestion.discovery import discover_files
from repomind.ingestion.errors import IngestionError
from repomind.ingestion.sources import GitHubSource
from repomind.models import EntityType, FileCategory, FileRecord, Language
from repomind.services.repository_service import RepositoryService

LOGIN_APP = Path(__file__).resolve().parents[2] / "test_apps" / "login_app"


def make(root: Path, rel: str, text: str = "x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.mark.parametrize("path, language, category", [
    ("app.py", Language.PYTHON, FileCategory.SOURCE),
    ("static/login.js", Language.JAVASCRIPT, FileCategory.SOURCE),
    ("static/index.html", Language.HTML, FileCategory.SOURCE),
    ("static/style.css", Language.CSS, FileCategory.SOURCE),
    ("README.md", Language.UNKNOWN, FileCategory.DOC),
    ("package.json", Language.UNKNOWN, FileCategory.CONFIG),
    ("requirements.txt", Language.UNKNOWN, FileCategory.CONFIG),
    ("notes.xyz", Language.UNKNOWN, FileCategory.OTHER),
])
def test_classify_file(path, language, category):
    assert classify_file(path) == (language, category)


def test_discovery_skips_junk(tmp_path):
    make(tmp_path, "main.py")
    make(tmp_path, "src/app.js")
    make(tmp_path, "node_modules/lib/index.js")
    make(tmp_path, ".git/config")
    make(tmp_path, "dist/bundle.js")
    make(tmp_path, "static/app.min.js")
    make(tmp_path, ".env", "SECRET=1")
    make(tmp_path, "logo.png")

    files, skipped = discover_files(tmp_path)

    assert [f.path for f in files] == ["main.py", "src/app.js"]
    assert skipped["ignored_directory"] == 3   # node_modules, .git, dist
    assert skipped["minified_or_map"] == 1
    assert skipped["sensitive_file"] == 1
    assert skipped["binary_file"] == 1


def test_discovery_skips_large_files(tmp_path):
    make(tmp_path, "big.py", "x" * 100)
    files, skipped = discover_files(tmp_path, max_file_bytes=10)
    assert files == []
    assert skipped["too_large"] == 1


@pytest.mark.parametrize("url", [
    "https://evil.com/a/b",
    "https://github.com/onlyowner",
    "https://github.com/a/b;rm -rf",
])
def test_github_url_rejects_bad_input(url):
    with pytest.raises(IngestionError):
        GitHubSource(url)


def test_github_url_parsed():
    s = GitHubSource("https://github.com/tiangolo/fastapi.git")
    assert (s.owner, s.repo) == ("tiangolo", "fastapi")
    assert s.url == "https://github.com/tiangolo/fastapi.git"


def test_file_record_to_entity():
    rec = FileRecord(path="static/login.js", language=Language.JAVASCRIPT,
                     category=FileCategory.SOURCE, size_bytes=10, line_count=3)
    e = rec.to_entity()
    assert e.id == "file:static/login.js"
    assert e.type == EntityType.FILE
    assert e.name == "login.js"


def test_login_app_inventory(tmp_path):
    service = RepositoryService(Settings(workspace_dir=tmp_path, log_level="INFO"))
    inv = service.ingest(str(LOGIN_APP))

    assert inv.language_counts == {"python": 3, "javascript": 1, "html": 1, "css": 1}

    # Every file entity in the Stage 0 ground truth must be found by ingestion.
    gt = json.loads((LOGIN_APP / "ground_truth.json").read_text())
    expected = {eid for eid, _ in gt["entities"] if eid.startswith("file:")}
    assert expected <= {f.id for f in inv.files}