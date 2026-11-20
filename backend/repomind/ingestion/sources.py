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


def source_from_location(location: str) -> RepositorySource:
    if location.startswith(("http://", "https://")):
        return GitHubSource(location)
    return LocalSource(location)