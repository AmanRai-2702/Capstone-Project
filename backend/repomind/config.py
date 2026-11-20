import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    workspace_dir: Path   # where cloned repos will live
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            workspace_dir=Path(os.getenv("REPOMIND_WORKSPACE", "./workspace")),
            log_level=os.getenv("REPOMIND_LOG_LEVEL", "INFO"),
        )