# RepoMind: evaluation on unfamiliar repositories (Phase 8)

Everything before Phase 8 was measured on six small apps written for this project. This document reports what
happened when RepoMind was pointed at **real open-source code it had never seen**, how that was measured, what it
found, what was fixed, and, as importantly, what these numbers do **not** show.

All raw results are in `backend/eval_data/phase8_*.json`. Everything here is reproducible:

```powershell
$env:PYTHONPATH="backend"
python backend/evaluate_external.py clone         # pinned clones into workspace/external (git-ignored, read-only use)
python backend/evaluate_external.py robustness    # success, time, memory, independent censuses
python backend/evaluate_external.py sample        # the stratified relationship sample reviewed by hand
python backend/evaluate_external.py retrieval flask-lib
python backend/evaluate_external.py impact
python backend/evaluate_external.py qa            # needs OPENROUTER_API_KEY + OPENROUTER_MODEL; `qa offline` needs neither
```

## 1. What was evaluated

| Target (pinned commit in `external_repos.json`) | What it is | Source files |
|---|---|---|
| `microblog` | Flask app, Jinja templates | 34 py, 18 html |
| `flask-lib` | the Flask library itself (tests, examples) | 83 py, 20 html, 2 css |
| `flask-javascript-example` | Flask + vanilla JS (`fetch`, XHR, jQuery) | 4 py, 4 html |
| `flask-tutorial` | the `flaskr` app | 9 py, 6 html, 1 css |
| `fastapi-backend` | FastAPI + SQLModel backend | 40 py, 3 html |
| `fastapi-fullstack` | same + React/**TypeScript** frontend (unsupported language) | 43 py, 4 html, 1 css (+109 ts/tsx **not analysed**) |
| `vanilla-web-projects` | 21 unrelated vanilla JS/HTML/CSS mini-apps | 21 js, 21 html, 21 css |

RepoMind only **reads** these repositories. No repository code is ever executed.

## 2. Robustness and independent censuses

No target crashed. Two files that use Python 3.14-only syntax (`except A, B:`) cannot be parsed by the Python 3.13
the tool ran on; they are reported in `analysis_errors`, not hidden (and they cascade, see section 7).

A *census* is an independent re-count with separate code. It checks that nothing is silently dropped; it is **not** a
full ground truth (both sides can miss the same thing, and the Python side shares `ast`).

| Check | Result |
|---|---|
| Python `def`/`class` found vs `ast` census | 100% on all 5 Python targets (1,298 definitions in `flask-lib`) |
| Route decorators found vs `ast` census | 100% (141 routes in `flask-lib`, 35 in `microblog`, 21 in the FastAPI template) |
| JavaScript function definitions vs regex census | 99% (102/103; the one miss is the word "based" in a comment, a census artefact) |
| `fetch`/axios call sites | 9 sites, all 9 extracted (reported as unresolved: they call external APIs) |
| HTML `id` attributes vs regex census | 100% |

Cost: 0.03 s (12 files) to ~1.5 s warm (227 files, 1,825 entities); peak Python heap 0.4–33 MB. The first run on freshly
cloned files was 8.4 s because of Windows file-cache effects, **not** slow code (an A/B of warm runs before/after the
fixes: 1.65 s vs 1.56 s).

## 3. What the first run found, and what was fixed

Unresolved references that are **not** just "an external library" (the gaps that matter): **312 → 131 (-58%)**.
`microblog` and the Flask JS example went to 0.

| Found on real code | Fix |
|---|---|
| `self.assertTrue()` on a class inheriting `unittest.TestCase` created a bogus edge (my Phase 6 regression) | `self.x()` is never invented when the method may come from an external base |
| Methods inherited from library bases (`form.validate_on_submit()`) reported as unresolved targets | classified as `external_package` with the library name |
| `from x import A as B` used the alias in ids | original names |
| `SessionDep = Annotated[Session, ...]` treated as a class | type aliases resolved to what they stand for |
| `https://randomuser.me/api` matched against a local `/api` route (possible false link) | external hosts never match local routes: new reason `external_api` |
| Jinja `{{ url_for(...) }}` hrefs, image/font assets reported as missing files | reasons `dynamic_reference`, `asset_not_analysed` |
| `<script src="/script.js">` in a sub-project folder | root-relative paths resolved to the one/nearest matching file (inferred) |
| Jinja `{% extends %}`: a child template's script cannot see elements declared in the parent | extends/include followed; ancestors' elements are searched |
| Functions declared inside other functions were not extracted | extracted (ambiguous names skipped, not guessed) |
| Unresolved `DATA_FLOW`/`READS_STATE`/`RETURNS`/alias `USES` drowned the real gaps | dropped, and counted in `stats` |
| `el.querySelector(sel)` treated as a document-wide lookup | scoped to the receiver element (descendant selector), or no edge if the receiver is unknown |
| `from pkg import submodule` reached only `pkg/__init__.py` | the submodule file is linked too |
| Two same-tag elements on one line (`<li>…</li><li>…</li>`) collapsed into ONE entity | `tag@line`, `tag@line:2`, … |

Each fix has a regression test (`test_external_fixes*.py`).

## 4. Relationship precision (manual review, small n)

A seeded sample of **133** relationships (stratified by type and detection kind, six repositories) was checked against the
real source by one reviewer. Before the fixes above: **127/133 correct = 95.5%** (95% Wilson interval 90.5–97.9%).

| Slice | Correct |
|---|---|
| inferred | 49/49 |
| detected | 74/77 |
| **heuristic** | **4/7 (57%)**: the weakest class, which is exactly why it carries its own label |
| IMPORTS | 9/12 (the submodule bug) |

The 6 errors were two defect classes (submodule imports, scoped `querySelector`), both fixed and re-verified on those
cases. Wrong guesses were **not** re-sampled after the fixes, so 95.5% is the pre-fix figure.

New in this phase, **labelled heuristic (confidence 0.5)**: a call on an *untyped* receiver is linked to the single class
that defines a method of that name (skipping common/builtin names, short names, and calls inside the defining class).
Reviewed on two independent samples: 26/30 before two guards were added, **34/34** after (lower 95% bound ≈ 90%). The
second sample is dominated by Flask's regular `app.<method>()` test pattern, so its effective diversity is smaller than 34.

## 5. Retrieval on real code

200 docstring-as-query examples over `flask-lib` (442 chunks; the docstring is **removed** from the indexed text so
lexical search cannot cheat):

| | MRR | Recall@1 | Recall@5 | Recall@10 | ms/query |
|---|---|---|---|---|---|
| TF-IDF | 0.420 | 0.26 | 0.64 | 0.76 | 0.6 |
| dense (MiniLM) | 0.493 | 0.38 | 0.65 | 0.77 | 60 |
| **hybrid** | **0.518** | 0.37 | **0.71** | **0.83** | 55 |

On the hardest slice (43 queries sharing *no word* with the target's name): TF-IDF MRR 0.14, dense 0.22, hybrid 0.20.
With n=43, dense and hybrid are not distinguishable. Cost of the dense side: 105 s to load the model and encode 442 chunks
cold. TF-IDF stays the default; hybrid is opt-in (`REPOMIND_RETRIEVAL_MODE=hybrid`).

## 6. Question answering, with a live LLM (`anthropic/claude-haiku-5.5` via OpenRouter)

15 questions over four repositories (repository-level, code-level, flow, impact, and one out-of-scope question that must
be refused). Expectations (facts that must appear, file the citations must point into) were written by reading the
repositories.

| Configuration | Correct | Fallbacks | Invalid-citation rewrites | Hallucinated files | Retrieval (median) | Generation (median) | Tokens in/out |
|---|---|---|---|---|---|---|---|
| template, TF-IDF | 15/15 | 0 | 0 | 0 | 36 ms | 0 | n/a |
| LLM, TF-IDF | 15/15 | 0 | 0 | 0 | 22 ms | 4.7 s | 28.8k / 10.9k |
| LLM, hybrid | 15/15 | 0 | 0 | 0 | 81 ms | 4.3 s | 29.0k / 10.2k |

**Read this carefully.** 15/15 is on a small benchmark that I iterated against: two questions failed at first
(`mb-impact`, `van-seat`), and fixing them (the labelled heuristic; ranking changes) is part of why they pass. The
substring metric cannot tell a good LLM answer from the template's raw fact dump, so it measures that **retrieval found
the right evidence**, not answer quality. Cost is negligible (< 1 cent for all 15 questions).

Answer quality was therefore judged by reading: the LLM answers were grounded, named the heuristic links as "0.50
confidence, verify them", said what the facts did not show, did not invent the Jinja-built URL in the one case where the
UI-to-server link is genuinely untraceable, and the out-of-scope question was refused with **no LLM call**. That is
n=4 read by one reviewer, not a statistic.

## 7. Impact analysis on real code

Direct callers of 21 unique module-level functions across 4 repositories (~120 call sites), compared with an independent
`ast` census of call sites: **micro-precision 98.4%, recall 100%**. Both "extras" are correct callers through an import
alias that the census cannot see (effective precision 100%). **Scope:** this is the *easiest* impact case (named
functions, direct callers). Methods on dynamically-typed receivers are covered only by the labelled heuristic; data-flow
and transitive impact on real code were **not** independently verified.

## 8. Scale

| Synthetic modules | Entities | Warm analysis (best of 3) | Q&A | Peak heap |
|---|---|---|---|---|
| 200 | 1,281 | 0.7 s | n/a | 10 MB |
| 800 | 5,121 | 3.9–9.0 s (noisy) | n/a | 43 MB |
| 1,600 | 10,241 | 7.6–17 s (noisy) | 37–270 ms | 82 MB |

Analysis time per entity is flat (~0.57 ms) from 200 to 400 modules; at 800+ the best runs give ~0.75 ms/entity but runs
of the *same* repository varied by 2× on this machine, so scaling is approximately linear, not precisely measured.

Found and fixed here: question answering took **6.7 s** (entity linking: a regex per entity name per question) and
**8.2 s** (components summary: files × entities) at 10k entities. Now 37 ms and 80 ms; the architecture answer is capped
(25 endpoints, 10 entry-point flows) and says how many it left out. Local time and external time are now reported
separately on every answer (`retrieval_ms`, `generation_ms`, tokens).

## 9. Limits of this evaluation

- Seven targets from four repositories; Python-heavy. No Java/Go/TypeScript (TypeScript files are silently out of scope,
  and reported as such).
- One reviewer judged the precision sample and the answers; no inter-rater agreement.
- The QA benchmark was tuned against its own failures (section 6). A fresh question set would be a fairer test.
- Files using syntax newer than the host Python cannot be parsed; the failure is reported but cascades (a type alias in
  such a file is invisible, which produced 29 noisy unresolved calls in the FastAPI template).
- Dynamic typing remains the main source of missed links (ORM results, `**kwargs`, containers); the labelled heuristic
  recovers many but is a guess by design.
- Nothing here measures how useful the answers are to a developer; it measures grounding, retrieval and link accuracy.
