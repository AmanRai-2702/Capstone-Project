from abc import ABC, abstractmethod
from pydantic import BaseModel, Field

from repomind.models import Entity, EntityType, Relationship


class AnalysisResult(BaseModel):
    """Holds all entities and relationships extracted from a single file or unit."""

    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)

    def add_entity(self, entity: Entity) -> None:
        self.entities.append(entity)

    def add_relationship(self, relationship: Relationship) -> None:
        self.relationships.append(relationship)

    def entities_by_type(self, entity_type: EntityType) -> list[Entity]:
        return [e for e in self.entities if e.type == entity_type]

    def entity_ids(self) -> set[str]:
        return {e.id for e in self.entities}

    def relationship_keys(self) -> set[str]:
        return {r.key for r in self.relationships}


class BaseAnalyzer(ABC):
    """Common interface for single-language source code analyzers."""

    @abstractmethod
    def analyze_file(self, rel_path: str, content: str) -> AnalysisResult:
        """Parse source code string from `rel_path` and return extracted entities and relationships."""
        pass
