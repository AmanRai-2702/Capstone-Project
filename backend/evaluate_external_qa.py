"""Question answering on unfamiliar repositories: template vs a live LLM (OpenRouter), tfidf vs hybrid retrieval.

    python backend/evaluate_external_qa.py            # via: python backend/evaluate_external.py qa [--model ID]

Each question has facts that a correct answer must contain and (usually) the file its citations must point into.
The expectations were written by reading the repositories (not the tool's output). Scoring is mechanical:
  facts     share of the required terms present in the answer text (case-insensitive substring)
  cited     some citation points into the expected file (when one is specified)
  correct   facts == 100% and cited
  refusal   for out-of-scope questions: the system must decline instead of inventing an answer
  valid     no answer was replaced because the LLM cited something that was not retrieved
  halluc.   the answer names a file that does not exist in the repository
This measures grounding and retrieval of the right evidence; it does NOT judge writing quality (read the transcripts).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

import evaluate_external as base
from evaluate_external import DATA, TARGETS, target_path

from repomind.config import Settings
from repomind.qa import QAService, get_provider
from repomind.services.analysis_service import AnalysisService

QUESTIONS = [
    # (id, target, question, must terms, cite file prefix or None, kind)
    ("mb-purpose", "microblog", "What is the purpose of this repository?", ["microblog"], "README.md", "repo"),
    ("mb-tech", "microblog", "Which programming languages and frameworks are used?", ["python", "flask"], None, "repo"),
    ("mb-arch", "microblog", "How is the frontend connected to the backend?", ["translate"], None, "repo"),
    ("mb-login", "microblog", "Where is user login implemented?", ["login", "auth/routes.py"], "app/auth/routes.py", "code"),
    ("mb-export", "microblog", "Which function handles the /export_posts endpoint?", ["export_posts"], "app/main/routes.py", "code"),
    ("mb-impact", "microblog", "What breaks if I change User.check_password?", ["login", "verify_password"], None, "impact"),
    ("js-flow", "flask-javascript-example", "Explain how submitting the calculator form works", ["addSubmit"], None, "flow"),
    ("js-add", "flask-javascript-example", "What does the /add endpoint do?", ["add", "views.py"], "js_example/views.py", "code"),
    ("fa-email", "fastapi-backend", "Where is the password reset email generated?", ["generate_reset_password_email", "utils.py"], "app/utils.py", "code"),
    ("fa-user", "fastapi-backend", "Which endpoint creates a new user?", ["create_user"], "app/api/routes", "code"),
    ("fa-tech", "fastapi-backend", "Which languages and frameworks are used?", ["python", "fastapi"], None, "repo"),
    ("fa-impact", "fastapi-backend", "What is the blast radius of changing get_password_hash?", ["create_user"], None, "impact"),
    ("van-form", "vanilla-web-projects", "How does the form validator check the email address?", ["checkEmail"], "form-validator/script.js", "flow"),
    ("van-seat", "vanilla-web-projects", "What happens when the user clicks a seat?", ["updateSelectedCount"], "movie-seat-booking", "flow"),
    ("mb-refuse", "microblog", "How does the payment checkout work?", [], None, "refusal"),
]


def score(q, a, files: set[str]) -> dict:
    kind = q[5]
    text = a.answer.lower()
    must = q[3]
    facts = sum(t.lower() in text for t in must) / len(must) if must else 1.0
    cited = True if q[4] is None else any(c.startswith(q[4]) or q[4] in c for c in a.citations)
    row = {"id": q[0], "kind": kind, "intent": a.intent, "provider": a.provider, "model": a.model, "grounded": a.grounded,
           "fallback": a.fallback, "fallback_reason": a.fallback_reason, "facts": round(facts, 2), "cited": cited,
           "valid": a.fallback_reason != "unverified_citations",
           "hallucinated_file": any("not in this repository" in w for w in a.warnings),
           "retrieval_ms": a.retrieval_ms, "generation_ms": a.generation_ms, "prompt_tokens": a.prompt_tokens,
           "completion_tokens": a.completion_tokens, "answer": a.answer}
    if kind == "refusal":
        row["correct"] = not a.grounded
    else:
        row["correct"] = facts == 1.0 and cited and a.grounded
    return row


def build(settings: Settings, provider_name: str):
    services = {}
    for tid in {q[1] for q in QUESTIONS}:
        t = TARGETS[tid]
        analysis = AnalysisService(settings).analyze(str(target_path(t)))
        provider = get_provider(provider_name, settings=settings)
        services[tid] = (QAService(analysis, provider=provider, settings=settings),
                         {e.qualified_name for e in analysis.entities if e.type.value == "file"})
    return services


def run(ids: list[str]) -> int:
    model = os.environ.get("OPENROUTER_MODEL") or "anthropic/claude-haiku-5.5"
    os.environ["OPENROUTER_MODEL"] = model
    configs = [("template/tfidf", "template", "tfidf"), ("llm/tfidf", "openrouter", "tfidf"), ("llm/hybrid", "openrouter", "hybrid")]
    if "offline" in ids:
        configs = configs[:1]
    results = {}
    for label, provider, mode in configs:
        s = Settings(workspace_dir=Path(base.settings().workspace_dir), log_level="ERROR", retrieval_mode=mode, llm_provider=provider,
                     openrouter_model=model, openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""), llm_max_tokens=900,
                     allow_external_llm=True)
        services = build(s, provider)
        rows = []
        for q in QUESTIONS:
            qa, files = services[q[1]]
            a = qa.ask(q[2])
            rows.append(score(q, a, files))
            time.sleep(0.2)
        results[label] = rows
        n = len(rows)
        ok = [r for r in rows if r["kind"] != "refusal"]
        print(f"{label:16} correct {sum(r['correct'] for r in rows)}/{n} | facts {statistics.mean(r['facts'] for r in ok):.0%} | cited "
              f"{sum(r['cited'] for r in ok)}/{len(ok)} | fallbacks {sum(r['fallback'] for r in rows)} | invalid-citation rewrites "
              f"{sum(not r['valid'] for r in rows)} | hallucinated files {sum(r['hallucinated_file'] for r in rows)} | "
              f"retrieval {statistics.median(r['retrieval_ms'] for r in rows):.0f} ms median | generation {statistics.median(r['generation_ms'] for r in rows):.0f} ms median | "
              f"tokens in/out {sum(r['prompt_tokens'] for r in rows)}/{sum(r['completion_tokens'] for r in rows)}")
    out = DATA / "phase8_qa.json"
    out.write_text(json.dumps({"model": model, "questions": [q[:6] for q in QUESTIONS], "results": results}, indent=1), encoding="utf-8")
    print("\nper question (correct?):")
    print(f"{'id':12}" + "".join(f"{k:>16}" for k in results))
    for i, q in enumerate(QUESTIONS):
        print(f"{q[0]:12}" + "".join(f"{('ok' if results[k][i]['correct'] else 'MISS') + ' f' + str(results[k][i]['facts']):>16}" for k in results))
    return 0
