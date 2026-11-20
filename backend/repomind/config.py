import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    workspace_dir: Path   # where cloned/extracted repos and saved analyses live
    log_level: str
    # Extra directory names / glob patterns to skip, on top of the built-in list
    # (and on top of a `.repomindignore` file in the repository root).
    extra_ignored: tuple[str, ...] = ()
    max_file_bytes: int = 1_000_000      # larger files are skipped (usually data or generated)
    max_files: int = 20_000              # resource limit: stop discovery after this many files
    max_zip_mb: int = 200                # resource limit for uploaded/extracted archives

    @property
    def store_dir(self) -> Path:
        return self.workspace_dir / "analyses"

    @classmethod
    def from_env(cls) -> "Settings":
        extra = tuple(p.strip() for p in os.getenv("REPOMIND_IGNORE", "").split(",") if p.strip())
        return cls(
            workspace_dir=Path(os.getenv("REPOMIND_WORKSPACE", "./workspace")),
            log_level=os.getenv("REPOMIND_LOG_LEVEL", "INFO"),
            extra_ignored=extra,
            max_file_bytes=_int_env("REPOMIND_MAX_FILE_BYTES", 1_000_000),
            max_files=_int_env("REPOMIND_MAX_FILES", 20_000),
            max_zip_mb=_int_env("REPOMIND_MAX_ZIP_MB", 200),
        )
