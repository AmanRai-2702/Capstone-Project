from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .redact import redact

DEFAULT_PROMPT_CHARS = 24_000      # ~6k tokens: enough for ~40 facts + snippets, far below any model limit


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
    intent: str                     # overview | architecture | tech | components | docs | flow | impact | relations | explain | none
    headline: str = ""
    facts: list[Fact] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)
    repo_summary: str = ""          # one line: repository name and languages (given to the LLM as metadata)

    @property
    def empty(self) -> bool:
        return not self.facts

    def to_prompt(self, max_chars: int = DEFAULT_PROMPT_CHARS, secrets: tuple[str, ...] = ()) -> str:
        """The user message sent to an LLM: metadata, numbered facts, fenced source, valid citations.

        * Repository text is untrusted, so source is fenced in <source> tags and the closing tag is
          stripped from it (it cannot break out of the fence).
        * Secrets are redacted because this text leaves the machine.
        * `max_chars` is a context budget: facts are kept in retrieval order until it is used up."""
        header = [f"Question: {self.question}", f"Detected intent: {self.intent}"]
        if self.repo_summary:
            header.append(f"Repository: {self.repo_summary}")
        header += ["", "FACTS (detected = read from code; [inferred] = heuristic link with a confidence):"]
        lines, used, kept, cites = list(header), sum(len(h) + 1 for h in header), 0, []
        for i, f in enumerate(self.facts, 1):
            block = [f"{i}. {f.text}" + (f" [{f.citation}]" if f.citation else "")]
            if f.snippet:
                safe = f.snippet.replace("<source>", "").replace("</source>", "")
                block += ["      <source>", *("      | " + s for s in safe.splitlines()), "      </source>"]
            size = sum(len(b) + 1 for b in block)
            if kept and used + size > max_chars:
                lines.append(f"({len(self.facts) - kept} further facts omitted to fit the context budget)")
                break
            lines += block
            used, kept = used + size, kept + 1
            if f.citation:
                cites.append(f.citation)
        if cites:
            lines += ["", "VALID CITATIONS (cite only these, exactly as written): " + ", ".join(dict.fromkeys(cites))]
        return redact("\n".join(lines), secrets)

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
    provider: str = ""                              # the provider that actually produced the text
    model: str = ""                                 # LLM model id when an LLM was used
    requested_provider: str = ""                    # what the user asked for
    fallback: bool = False                          # True => requested provider failed; template answer shown
    fallback_reason: str = ""                       # machine-readable reason code, e.g. "rate_limit"
    # Where the time went: retrieval (local, graph + index) versus generation (the provider; for an LLM this is the
    # network round trip). Tokens are what the provider reported, 0 when it reported nothing (offline template).
    retrieval_ms: float = 0.0
    generation_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
