from typing import Any

from pydantic import BaseModel, Field

from .enums import EntityType, Language
from .location import SourceLocation


class Entity(BaseModel):
    id: str                       # stable, see ids.py
    type: EntityType
    name: str                     # "login"
    qualified_name: str           # "AuthService.login"
    language: Language
    location: SourceLocation | None = None
    parent_id: str | None = None  # e.g. method -> class
    metadata: dict[str, Any] = Field(default_factory=dict)