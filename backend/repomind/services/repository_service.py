import logging

from repomind.config import Settings
from repomind.ingestion.discovery import discover_files
from repomind.ingestion.sources import source_from_location
from repomind.models import RepositoryInventory

log = logging.getLogger(__name__)


class RepositoryService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()

    def ingest(self, location: str, exclude: list[str] | None = None) -> RepositoryInventory:
        """GitHub URL, local folder or .zip in, typed file inventory out."""
        source = source_from_location(location, max_zip_mb=self.settings.max_zip_mb)
        resolved = source.resolve(self.settings.workspace_dir)
        files, skipped = discover_files(
            resolved.root,
            max_file_bytes=self.settings.max_file_bytes,
            extra_ignored=[*self.settings.extra_ignored, *(exclude or [])],
            max_files=self.settings.max_files,
        )

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