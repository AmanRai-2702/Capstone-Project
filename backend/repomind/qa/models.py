from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field


@dataclass
class Fact:
    """One verifiable statement taken from the graph, with where it comes from."""
    text: str
    citation: str | None = None     # "path:start-end"
    snippet: str = ""               # source lines for the citation (may be empty)
    quote: bool = False             # offline answers print the snippet too (used for README/doc excerpts)


@dataclass
class Context:
    question: str
    intent: str                     # flow | impact | relations | explain | none
    headline: str = ""
    facts: list[Fact] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.facts

    def to_prompt(self) -> str:
        lines = [f"Question: {self.question}", f"Detected intent: {self.intent}", "", "FACTS:"]
        for i, f in enumerate(self.facts, 1):
            cite = f" [{f.citation}]" if f.citation else ""
            lines.append(f"{i}. {f.text}{cite}")
            if f.snippet:
                # Repository text is untrusted: fence it, and remove anything that could close the fence.
                safe = f.snippet.replace("<source>", "").replace("</source>", "")
                lines.append("      <source>")
                lines.extend("      | " + s for s in safe.splitlines())
                lines.append("      </source>")
        return "\n".join(lines)

    def draft_answer(self) -> str:
        """Deterministic answer built only from facts (used by the offline provider)."""
        out = [self.headline] if self.headline else []
        for i, f in enumerate(self.facts, 1):
            cite = f"  [{f.citation}]" if f.citation else ""
            out.append(f"{i}. {f.text}{cite}")
            if f.quote and f.snippet:
                out.extend("      > " + ln for ln in f.snippet.splitlines()[:12])
        return "\n".join(out)


class Answer(BaseModel):
    question: str
    intent: str
    answer: str
    grounded: bool                                  # False => nothing found, or verification failed
    citations: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provider: str = ""
