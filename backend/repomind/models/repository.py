from pathlib import PurePosixPath

from pydantic import BaseModel, Field, computed_field

from .entity import Entity
from .enums import EntityType, FileCategory, Language
from .ids import make_file_id


class FileRecord(BaseModel):
    path: str                       # relative to repo root, always forward slashes
    language: Language
    category: FileCategory
    size_bytes: int
    line_count: int | None = None   # None for files we don't read

    @computed_field
    @property
    def id(self) -> str:
        return make_file_id(self.path)

    def to_entity(self) -> Entity:
        """Convert to the unified Entity so files can join the graph later."""
        return Entity(
            id=self.id,
            type=EntityType.FILE,
            name=PurePosixPath(self.path).name,
            qualified_name=self.path,
            language=self.language,
            metadata={
                "category": self.category.value,
                "size_bytes": self.size_bytes,
                "line_count": self.line_count,
            },
        )


class RepositoryInventory(BaseModel):
    repo_id: str
    name: str
    origin: str                     # URL or local path it came from
    root_path: str                  # where the files live on disk (internal use)
    files: list[FileRecord]
    skipped: dict[str, int] = Field(default_factory=dict)  # reason -> count

    @computed_field
    @property
    def language_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.files:
            if f.category == FileCategory.SOURCE:
                counts[f.language.value] = counts.get(f.language.value, 0) + 1
        return counts

    @computed_field
    @property
    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.files:
            counts[f.category.value] = counts.get(f.category.value, 0) + 1
        return counts