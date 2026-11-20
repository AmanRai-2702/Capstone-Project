from __future__ import annotations

import logging
import re

from repomind.graph import RepositoryGraph
from repomind.models import RepositoryAnalysis
from repomind.services.impact_service import ImpactService

from .models import Answer, Context
from .providers import LLMProvider, TemplateProvider
from .retriever import Retriever

log = logging.getLogger(__name__)

CITATION_RE = re.compile(r"([\w./\\-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?")


def parse_citation(citation: str) -> tuple[str, int, int] | None:
    m = CITATION_RE.fullmatch(citation)
    return (m[1], int(m[2]), int(m[3] or m[2])) if m else None


def verify_citations(text: str, context: Context) -> list[str]:
    """Every path:line cited in `text` must fall inside a range that was actually retrieved."""
    allowed = [p for f in context.facts if f.citation and (p := parse_citation(f.citation))]
    problems = []
    for m in CITATION_RE.finditer(text):
        path, start, end = m[1], int(m[2]), int(m[3] or m[2])
        if not any(path == p and start >= s and end <= e for p, s, e in allowed):
            problems.append(m[0])
    return problems


class QAService:
    """Question in -> grounded answer out.

    1. Retriever pulls facts (with citations) from the knowledge graph + source files.
    2. The provider phrases them (offline template by default, optional LLM).
    3. Citations in the final text are verified against the retrieved facts.
    """

    def __init__(self, analysis: RepositoryAnalysis, graph: RepositoryGraph | None = None,
                 provider: LLMProvider | None = None):
        self.graph = graph or RepositoryGraph.from_analysis(analysis)
        self.provider = provider or TemplateProvider()
        self.retriever = Retriever(analysis, self.graph, ImpactService(self.graph))

    def ask(self, question: str) -> Answer:
        ctx = self.retriever.retrieve(question)
        if ctx.empty:
            return Answer(
                question=question, intent=ctx.intent, grounded=False, provider=self.provider.name,
                answer=("I could not find anything in the analysed repository that matches this question. "
                        "Try naming a function, class, endpoint, file or UI element (e.g. 'handleLogin')."),
            )
        warnings: list[str] = []
        text = self.provider.generate(ctx)
        bad = verify_citations(text, ctx)
        if bad:
            warnings.append(f"Removed an answer with unverified citations: {', '.join(bad)}; showing grounded facts instead.")
            log.warning("Provider %s produced unverified citations %s", self.provider.name, bad)
            text = ctx.draft_answer()
        citations = sorted({f.citation for f in ctx.facts if f.citation})
        return Answer(question=question, intent=ctx.intent, answer=text, grounded=True,
                      citations=citations, entities=ctx.entity_ids, warnings=warnings,
                      provider=self.provider.name)
