import logging
import os
from collections import Counter
from pathlib import Path

from repomind.models import FileCategory, FileRecord

from .classifier import classify_file

log = logging.getLogger(__name__)

DEFAULT_MAX_FILE_BYTES = 1_000_000  # skip files over ~1 MB (usually data/generated)

IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", "coverage", ".pytest_cache", ".mypy_cache",
    ".idea", ".vscode", "site-packages", "workspace",
}
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock"}
MINIFIED_SUFFIXES = (".min.js", ".min.css", ".map")
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".exe", ".dll", ".so", ".pyc",
    ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".sqlite", ".db",
}
SECRET_EXTS = {".pem", ".key", ".p12", ".pfx"}


def _skip_reason(filename: str, size: int, max_bytes: int) -> str | None:
    name = filename.lower()
    ext = Path(name).suffix
    # Secrets are never inventoried, so they cannot reach the index or an LLM prompt.
    if (name == ".env" or (name.startswith(".env.") and name != ".env.example")
            or ext in SECRET_EXTS):
        return "sensitive_file"
    if name in LOCK_FILES:
        return "lock_file"
    if name.endswith(MINIFIED_SUFFIXES):
        return "minified_or_map"
    if ext in BINARY_EXTS:
        return "binary_file"
    if size > max_bytes:
        return "too_large"
    return None


def _count_lines(path: Path) -> int | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def discover_files(
    root: Path, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
) -> tuple[list[FileRecord], dict[str, int]]:
    """Walk `root` and return (kept files, {skip_reason: count})."""
    records: list[FileRecord] = []
    skipped: Counter[str] = Counter()

    # os.walk does not follow symlinked directories by default; we keep it that way
    # so a malicious repo cannot make us read files outside its own folder.
    for current, dirnames, filenames in os.walk(root):
        kept_dirs = []
        for d in dirnames:
            if d in IGNORED_DIRS or d.endswith(".egg-info"):
                skipped["ignored_directory"] += 1
            else:
                kept_dirs.append(d)
        dirnames[:] = sorted(kept_dirs)  # sorted => same order on every run

        for filename in sorted(filenames):
            full = Path(current) / filename
            if full.is_symlink():
                skipped["symlink"] += 1
                continue
            try:
                size = full.stat().st_size
            except OSError:
                skipped["unreadable"] += 1
                continue
            reason = _skip_reason(filename, size, max_file_bytes)
            if reason:
                skipped[reason] += 1
                continue

            rel = full.relative_to(root).as_posix()
            language, category = classify_file(rel)
            line_count = _count_lines(full) if category != FileCategory.OTHER else None
            records.append(FileRecord(
                path=rel, language=language, category=category,
                size_bytes=size, line_count=line_count,
            ))

    log.debug("Discovery kept %d files, skipped %s", len(records), dict(skipped))
    return records, dict(skipped)