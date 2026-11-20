from pydantic import BaseModel

from .location import SourceLocation


class UnresolvedReference(BaseModel):
    """A relationship the analyzers saw but that could not be connected to an entity.

    Kept (not silently dropped) so the user can see what RepoMind does NOT know.
    """
    source_id: str
    target_id: str
    type: str
    reason: str            # external_package | no_matching_endpoint | method_mismatch |
                           # no_html_element | unresolved_target
    detail: str = ""
    snippet: str = ""
    location: SourceLocation | None = None
