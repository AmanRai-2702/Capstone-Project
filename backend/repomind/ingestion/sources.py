import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import IngestionError

log = logging.getLogger(__name__)

# Only plain https://github.com/<owner>/<repo>[.git]. Rejecting everything else blocks
# odd git protocols (ext::, file://) and shell-style tricks in the "URL".
GITHUB_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class ResolvedRepository:
    repo_id: str
    name: str
    root: Path       # folder on disk containing the files
    origin: str


class RepositorySource(Protocol):
    def resolve(self, workspace: Path) -> ResolvedRepository: ...


class LocalSource:
    """Use a folder already on disk. Files are read in place, never modified."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def resolve(self, workspace: Path) -> ResolvedRepository:
        root = self.path.expanduser().resolve()
        if not root.is_dir():
            raise IngestionError(f"Local path is not a directory: {root}")
        return ResolvedRepository(repo_id=root.name, name=root.name, root=root, origin=str(root))


class GitHubSource:
    """Shallow-clone a public GitHub repo into the workspace folder."""

    def __init__(self, url: str):
        match = GITHUB_URL.match(url.strip())
        if not match:
            raise IngestionError(
                "Only URLs like https://github.com/<owner>/<repo> are supported."
            )
        self.owner = match["owner"]
        self.repo = match["repo"]
        self.url = f"https://github.com/{self.owner}/{self.repo}.git"

    def resolve(self, workspace: Path) -> ResolvedRepository:
        repo_id = f"{self.owner}__{self.repo}"
        target = workspace / repo_id

        if (target / ".git").exists():
            log.info("Reusing existing clone at %s (delete the folder to refresh)", target)
        else:
            # Imported here so local-only use works even if git is not installed.
            from git import Repo
            from git.exc import GitCommandError

            workspace.mkdir(parents=True, exist_ok=True)
            log.info("Cloning %s ...", self.url)
            try:
                Repo.clone_from(self.url, target, depth=1)  # depth=1: latest snapshot only
            except GitCommandError as exc:
                raise IngestionError(
                    f"Could not clone {self.url}. Is the repo public and spelled correctly?"
                ) from exc

        return ResolvedRepository(
            repo_id=repo_id, name=self.repo, root=target, origin=self.url
        )


class ZipSource:
    """Extract an uploaded .zip into the workspace, defensively.

    Archives are untrusted input. We reject: path traversal ("zip slip"), absolute paths,
    symlink entries, too many files, and archives whose declared uncompressed size exceeds the
    limit (zip bombs). Nothing inside the archive is ever executed.
    """

    MAX_FILES = 50_000

    def __init__(self, path: str | Path, max_zip_mb: int = 200):
        self.path = Path(path)
        self.max_bytes = max_zip_mb * 1024 * 1024

    def resolve(self, workspace: Path) -> ResolvedRepository:
        import hashlib
        import shutil
        import zipfile

        if not self.path.is_file():
            raise IngestionError(f"Archive not found: {self.path}")
        try:
            archive = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as exc:
            raise IngestionError("The uploaded file is not a valid zip archive.") from exc

        with archive:
            infos = archive.infolist()
            if len(infos) > self.MAX_FILES:
                raise IngestionError(f"Archive has too many entries ({len(infos)} > {self.MAX_FILES}).")
            if sum(i.file_size for i in infos) > self.max_bytes:
                raise IngestionError(f"Archive expands beyond the {self.max_bytes // (1024 * 1024)} MB limit.")

            stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.path.stem).strip("._") or "upload"
            digest = hashlib.sha256(self.path.read_bytes()).hexdigest()[:8]
            workspace.mkdir(parents=True, exist_ok=True)
            target = (workspace / f"{stem}__{digest}").resolve()
            if target.exists():
                shutil.rmtree(target)             # same bytes, same folder: re-extract cleanly
            target.mkdir(parents=True)

            for info in infos:
                name = info.filename
                if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
                    raise IngestionError(f"Unsafe path in archive: {name}")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:      # symlink entry
                    raise IngestionError(f"Symlinks are not allowed in archives: {name}")
                dest = (target / name).resolve()
                if not dest.is_relative_to(target):
                    raise IngestionError(f"Unsafe path in archive (path traversal): {name}")
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, open(dest, "wb") as out:
                    remaining = self.max_bytes
                    while chunk := src.read(65536):          # also bound the *actual* bytes written
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise IngestionError("Archive expands beyond the size limit.")
                        out.write(chunk)

        # GitHub-style archives wrap everything in a single top-level folder: step into it.
        entries = [p for p in target.iterdir()]
        root = entries[0] if len(entries) == 1 and entries[0].is_dir() else target
        return ResolvedRepository(repo_id=root.name if root != target else stem, name=root.name if root != target else stem,
                                  root=root, origin=f"zip:{self.path.name}")


def source_from_location(location: str, max_zip_mb: int = 200) -> RepositorySource:
    if location.startswith(("http://", "https://")):
        return GitHubSource(location)
    if location.lower().endswith(".zip") and Path(location).expanduser().is_file():
        return ZipSource(Path(location).expanduser(), max_zip_mb)
    return LocalSource(location)
