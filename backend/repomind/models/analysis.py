from typing import Any
from pydantic import BaseModel, Field, computed_field

from .entity import Entity
from .enums import EntityType, RelationKind, RelationType
from .relationship import Relationship
from .resolution import UnresolvedReference


class RepositoryAnalysis(BaseModel):
    """Unified container for all entities and relationships discovered in a repository."""

    repo_id: str
    name: str
    origin: str
    root_path: str
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    unresolved: list[UnresolvedReference] = Field(default_factory=list)

    @computed_field
    @property
    def entity_count(self) -> int:
        return len(self.entities)

    @computed_field
    @property
    def relationship_count(self) -> int:
        return len(self.relationships)

    @computed_field
    @property
    def entity_counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entities:
            counts[e.type.value] = counts.get(e.type.value, 0) + 1
        return counts

    @computed_field
    @property
    def relationship_counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.relationships:
            counts[r.type.value] = counts.get(r.type.value, 0) + 1
        return counts

    @computed_field
    @property
    def relationship_counts_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.relationships:
            counts[r.kind.value] = counts.get(r.kind.value, 0) + 1
        return counts

    @computed_field
    @property
    def unresolved_counts_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for u in self.unresolved:
            counts[u.reason] = counts.get(u.reason, 0) + 1
        return counts

    def get_entity(self, entity_id: str) -> Entity | None:
        for e in self.entities:
            if e.id == entity_id:
                return e
        return None

    def get_entities_by_type(self, entity_type: EntityType) -> list[Entity]:
        return [e for e in self.entities if e.type == entity_type]

    def get_relationships_by_type(self, rel_type: RelationType) -> list[Relationship]:
        return [r for r in self.relationships if r.type == rel_type]
