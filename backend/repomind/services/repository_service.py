import logging

from repomind.config import Settings
from repomind.ingestion.discovery import discover_files
from repomind.ingestion.sources import source_from_location
from repomind.models import RepositoryInventory

log = logging.getLogger(__name__)


class RepositoryService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()

    def ingest(self, location: str) -> RepositoryInventory:
        """GitHub URL or local path in, typed file inventory out."""
        source = source_from_location(location)
        resolved = source.resolve(self.settings.workspace_dir)
        files, skipped = discover_files(resolved.root)

        inventory = RepositoryInventory(
            repo_id=resolved.repo_id,
            name=resolved.name,
            origin=resolved.origin,
            root_path=str(resolved.root),
            files=files,
            skipped=skipped,
        )
        log.info("Ingested %s: %d files kept", inventory.name, len(files))
        return inventory