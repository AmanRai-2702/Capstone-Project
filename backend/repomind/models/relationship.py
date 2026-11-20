from pydantic import BaseModel, Field

from .enums import RelationKind, RelationType
from .location import SourceLocation


class Evidence(BaseModel):
    snippet: str                          # e.g. fetch('/api/login')
    location: SourceLocation | None = None


class Relationship(BaseModel):
    source_id: str
    target_id: str
    type: RelationType
    kind: RelationKind = RelationKind.DETECTED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)

    @property
    def key(self) -> str:
        """Identity for dedupe and ground-truth comparison."""
        return f"{self.source_id} -{self.type.value}-> {self.target_id}"