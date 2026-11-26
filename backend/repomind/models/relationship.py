from typing import Any

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
    # Extra facts about the link that are not part of its identity, e.g. for REQUESTS the JSON keys the
    # client sends / reads (API contracts). Optional and empty for most relationships.
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        """Identity for dedupe and ground-truth comparison."""
        return f"{self.source_id} -{self.type.value}-> {self.target_id}"