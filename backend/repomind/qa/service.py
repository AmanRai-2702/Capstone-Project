from __future__ import annotations

import logging
import re

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.models import RepositoryAnalysis
from repomind.services.impact_service import ImpactService

from .models import Answer, Context
from .providers import LLMProvider, ProviderError, TemplateProvider
from .retriever import Retriever

log = logging.getLogger(__name__)

CITATION_RE = re.compile(r"([\w./\\-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?")
# A file-looking token (name.ext) in prose. Used only for a warning: libraries like "Chart.js" look the same.
FILE_MENTION_RE = re.compile(r"(?<![\w/.-])([\w./-]+\.(?:py|js|mjs|jsx|html|css|md|json|txt|toml|ya?ml))\b(?!:)")

REFUSAL = ("I could not find anything in the analysed repository that matches this question. "
           "Try naming a function, class, endpoint, file or UI element (e.g. 'handleLogin'), or ask about the "
           "repository as a whole (its purpose, main components, technologies or architecture).")


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

    1. Retriever pulls facts (with citations) from the knowledge graph, docs and source files.
    2. The provider phrases them (offline template by default, optionally an LLM via OpenRouter).
    3. Citations in the final text are verified against the retrieved facts; an answer with an
       unsupported citation is replaced by the verified facts.
    4. If the provider fails for any reason, the template answer is used and the response says so.
    """

    def __init__(self, analysis: RepositoryAnalysis, graph: RepositoryGraph | None = None,
                 provider: LLMProvider | None = None, settings: Settings | None = None):
        self.analysis = analysis
        self.graph = graph or RepositoryGraph.from_analysis(analysis)
        self.provider = provider or TemplateProvider()
        settings = settings or Settings.from_env()
        self.retriever = Retriever(analysis, self.graph, ImpactService(self.graph),
                                   settings.retrieval_mode, settings.embedding_model,
                                   settings.workspace_dir / "embeddings")
        self._template = TemplateProvider()
        self._files = {e.qualified_name for e in analysis.entities if e.type.value == "file"}
        self._basenames = {p.rsplit("/", 1)[-1] for p in self._files}

    def status(self) -> dict:
        """Provider status for the UI/CLI. Contains no secrets."""
        requested = getattr(self.provider, "requested", self.provider.name)
        reason = getattr(self.provider, "fallback_reason", "")
        return {"provider": self.provider.name, "model": getattr(self.provider, "model", ""),
                "requested_provider": requested, "fallback": bool(reason), "fallback_reason": reason,
                "external": self.provider.name not in ("template",),
                "retrieval_mode": self.retriever.index.mode, "retrieval_requested": self.retriever.index.requested_mode}

    def _repo_summary(self) -> str:
        from collections import Counter
        langs = Counter(e.language.value for e in self.analysis.entities
                        if e.type.value == "file" and e.metadata.get("category") == "source")
        return f"{self.analysis.name} ({', '.join(f'{n} {l} files' for l, n in sorted(langs.items()))})"

    def ask(self, question: str) -> Answer:
        ctx = self.retriever.retrieve(question)
        st = self.status()
        base = dict(question=question, intent=ctx.intent, provider=self.provider.name, model=st["model"],
                    requested_provider=st["requested_provider"])
        if ctx.empty:
            return Answer(answer=REFUSAL, grounded=False, **base)
        ctx.repo_summary = self._repo_summary()

        warnings: list[str] = []
        used, model, fallback, reason = self.provider, st["model"], st["fallback"], st["fallback_reason"]
        if reason:
            warnings.append(f"Requested provider '{st['requested_provider']}' is unavailable ({reason}); "
                            "showing the offline template answer.")
        try:
            text = self.provider.generate(ctx)
        except ProviderError as exc:
            log.warning("Provider %s failed (%s): %s", self.provider.name, exc.reason, exc.message)
            text, used, model, fallback, reason = self._template.generate(ctx), self._template, "", True, exc.reason
            warnings.append(f"{self.provider.name} failed ({exc.reason}): {exc.message} Showing the offline template answer instead.")
        except Exception as exc:      # a provider bug must not take the app down either
            log.exception("Provider %s raised an unexpected error", self.provider.name)
            text, used, model, fallback, reason = self._template.generate(ctx), self._template, "", True, "unexpected_error"
            warnings.append(f"{self.provider.name} failed unexpectedly ({type(exc).__name__}). Showing the offline template answer instead.")

        if used.name != "template":
            bad = verify_citations(text, ctx)
            if bad:
                warnings.append(f"Removed an answer with unverified citations: {', '.join(bad)}; showing grounded facts instead.")
                log.warning("Provider %s produced unverified citations %s", used.name, bad)
                text, model = ctx.draft_answer(), ""
                used, fallback, reason = self._template, True, "unverified_citations"
            else:
                unknown = sorted({m for m in FILE_MENTION_RE.findall(text)
                                  if m not in self._files and m.rsplit("/", 1)[-1] not in self._basenames})
                if unknown:
                    warnings.append("The answer mentions file name(s) that are not in this repository "
                                    f"({', '.join(unknown)}); verify them before relying on the answer.")
        citations = sorted({f.citation for f in ctx.facts if f.citation})
        return Answer(question=question, intent=ctx.intent, answer=text, grounded=True, citations=citations,
                      entities=ctx.entity_ids, warnings=warnings, provider=used.name, model=model,
                      requested_provider=st["requested_provider"], fallback=fallback, fallback_reason=reason)
