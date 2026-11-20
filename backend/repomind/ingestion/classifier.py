from pathlib import PurePosixPath

from repomind.models import FileCategory, Language

LANGUAGE_BY_EXT = {
    ".py": Language.PYTHON,
    ".js": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".html": Language.HTML,
    ".htm": Language.HTML,
    ".css": Language.CSS,
}

DOC_EXTS = {".md", ".rst", ".txt", ".adoc"}
CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
# Checked BEFORE the doc rule, so requirements.txt is config, not documentation.
CONFIG_NAMES = {
    "requirements.txt", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "makefile", ".gitignore", ".env.example", "package.json", "pyproject.toml",
}


def classify_file(rel_path: str) -> tuple[Language, FileCategory]:
    path = PurePosixPath(rel_path)
    name = path.name.lower()
    ext = path.suffix.lower()

    if ext in LANGUAGE_BY_EXT:
        return LANGUAGE_BY_EXT[ext], FileCategory.SOURCE
    if name in CONFIG_NAMES or ext in CONFIG_EXTS:
        return Language.UNKNOWN, FileCategory.CONFIG
    if ext in DOC_EXTS:
        return Language.UNKNOWN, FileCategory.DOC
    return Language.UNKNOWN, FileCategory.OTHER