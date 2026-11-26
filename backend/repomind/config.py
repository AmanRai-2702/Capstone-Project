import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


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

    # --- Retrieval. tfidf = offline default; dense/hybrid need `pip install sentence-transformers`.
    retrieval_mode: str = "tfidf"        # tfidf | dense | hybrid
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # --- LLM (answer generation). Everything here is read on the server; none of it reaches the browser.
    llm_provider: str = "template"       # template (offline, default) | openrouter | anthropic
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_timeout_s: float = 60.0
    allow_external_llm: bool = True      # False => never send repository text to any outside service
    openrouter_model: str = ""           # e.g. "anthropic/claude-haiku-5.5"; must be chosen by the user
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # repr=False keeps the key out of logs, tracebacks and debug prints of Settings.
    openrouter_api_key: str = field(default="", repr=False)

    @property
    def store_dir(self) -> Path:
        return self.workspace_dir / "analyses"

    @classmethod
    def from_env(cls) -> "Settings":
        # Reads a local .env (never committed). Real environment variables take precedence.
        load_dotenv(find_dotenv(usecwd=True), override=False)
        extra = tuple(p.strip() for p in os.getenv("REPOMIND_IGNORE", "").split(",") if p.strip())
        return cls(
            workspace_dir=Path(os.getenv("REPOMIND_WORKSPACE", "./workspace")),
            log_level=os.getenv("REPOMIND_LOG_LEVEL", "INFO"),
            extra_ignored=extra,
            max_file_bytes=_int_env("REPOMIND_MAX_FILE_BYTES", 1_000_000),
            max_files=_int_env("REPOMIND_MAX_FILES", 20_000),
            max_zip_mb=_int_env("REPOMIND_MAX_ZIP_MB", 200),
            retrieval_mode=os.getenv("REPOMIND_RETRIEVAL_MODE", "tfidf").strip().lower() or "tfidf",
            embedding_model=os.getenv("REPOMIND_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2").strip(),
            llm_provider=os.getenv("REPOMIND_LLM_PROVIDER", "template").strip().lower() or "template",
            llm_temperature=_float_env("REPOMIND_LLM_TEMPERATURE", 0.2),
            llm_max_tokens=_int_env("REPOMIND_LLM_MAX_TOKENS", 2048),
            llm_timeout_s=_float_env("REPOMIND_LLM_TIMEOUT", 60.0),
            allow_external_llm=_bool_env("REPOMIND_ALLOW_EXTERNAL_LLM", True),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "").strip(),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        )
