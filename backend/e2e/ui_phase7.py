"""End-to-end check of the Phase 7 UI features (optional; not part of the pytest suite).

Drives the real web UI in headless Edge: dashboard, file tree, graph filters, edge evidence, neighbourhood focus,
execution tracing, impact (risk factors, strict mode), API contracts, provider status and the fallback notice,
large-repository mode and error states.

Prerequisites:  pip install playwright   (uses the Edge already installed on Windows)

    # terminal 1, from the project root (a fresh workspace keeps saved repositories out of the way):
    set REPOMIND_WORKSPACE=%TEMP%\\repomind_e2e & set PYTHONPATH=backend & set REPOMIND_LLM_PROVIDER=template
    python -m repomind.cli serve --port 8766
    # terminal 2:
    python backend/e2e/ui_phase7.py [screenshot.png] [base_url]

The script starts a second server itself (port 8767, OpenRouter requested, no key) to check the fallback notice.
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOT = sys.argv[1] if len(sys.argv) > 1 else "ui_phase7.png"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8766"
ROOT = Path(__file__).resolve().parents[2]
FORM = "html:static/index.html:#login-form"
FIND = "python:repositories/user_repository.py:UserRepository.find_by_username"
errors, results = [], []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if detail and not cond else ""))


def analyse(page, location, expect):
    page.fill("#loc", location)
    page.click("#go")
    page.wait_for_function(f"document.querySelector('#status').textContent.includes('{expect}')", timeout=60000)
    page.wait_for_function("typeof cy !== 'undefined' && cy && cy.nodes().length > 0 && document.querySelector('#graphInfo').textContent.includes('nodes')", timeout=30000)


def settle(page, ms=700):
    page.wait_for_timeout(ms)          # the graph controls debounce for 250 ms, then fetch


def start_fallback_server():
    env = dict(os.environ, PYTHONPATH=str(ROOT / "backend"), REPOMIND_LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="",
               OPENROUTER_MODEL="", REPOMIND_WORKSPACE=tempfile.mkdtemp(prefix="repomind_e2e_fb_"))
    proc = subprocess.Popen([sys.executable, "-m", "repomind.cli", "serve", "--port", "8767"], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:8767/api/llm/status", timeout=1)
            return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    raise SystemExit("fallback server did not start")


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1700, "height": 1000})
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.goto(BASE + "/")

    # ---------------------------------------------------------------- dashboard
    analyse(page, "test_apps/notes_app", "notes_app")
    check("dashboard shows four KPIs", page.locator("#left .kpi").count() == 4)
    check("language distribution bar has a segment per language", page.locator("#left .bar i").count() >= 3,
          page.locator("#left .bar i").count())
    page.wait_for_function("!document.querySelector('#overview').textContent.includes('Loading')", timeout=15000)
    check("summary lists languages and components (deterministic, no LLM)",
          "Source files" in page.inner_text("#overview") and "holds" in page.inner_text("#overview"), page.inner_text("#overview")[:200])

    # ---------------------------------------------------------------- explorer tree and source navigation
    check("explorer is a directory tree", page.locator("#tree details.dir").count() >= 2, page.locator("#tree details.dir").count())
    page.locator("#tree .filerow", has_text="app.py").first.click()
    page.wait_for_selector("#p-src.on pre")
    check("clicking a file opens it in the source viewer", "Flask" in page.inner_text("#p-src"))
    page.fill("#p-src input[type=number]", "12")
    page.press("#p-src input[type=number]", "Enter")
    page.wait_for_selector("#p-src .hl")
    check("go-to-line highlights the requested line", "12" in page.inner_text("#p-src .hl").split()[0], page.inner_text("#p-src .hl")[:40])
    page.fill("#left .filters input", "zzz-no-such-component")
    check("search with no match hides every folder", page.evaluate("[...document.querySelectorAll('#tree details.dir')].every(d => d.style.display === 'none')"))
    page.fill("#left .filters input", "")

    # ---------------------------------------------------------------- graph controls (server-side filters)
    page.select_option("#typeSel", "function"); settle(page)
    check("type filter keeps only functions", page.evaluate("cy.nodes().length > 3 && cy.nodes().filter(n => n.data('type') !== 'function').length === 0"))
    page.select_option("#typeSel", ""); settle(page)
    page.select_option("#langSel", "javascript"); settle(page)
    check("language filter keeps only JavaScript", page.evaluate("cy.nodes().length > 3 && cy.nodes().filter(n => n.data('language') !== 'javascript').length === 0"))
    page.select_option("#langSel", ""); settle(page)
    page.evaluate("document.querySelectorAll('#relList input').forEach(b => { b.checked = (b.dataset.rel === 'CALLS'); b.dispatchEvent(new Event('change')); })"); settle(page)
    check("relationship filter keeps only CALLS edges", page.evaluate("cy.edges().length > 0 && cy.edges().filter(e => e.data('label') !== 'CALLS').length === 0"))
    page.evaluate("document.querySelectorAll('#relList input').forEach(b => { b.checked = true; b.dispatchEvent(new Event('change')); })"); settle(page)
    page.evaluate("document.querySelector('#minConf').value = 0.95; document.querySelector('#minConf').dispatchEvent(new Event('input'))"); settle(page)
    check("confidence slider hides weaker edges", page.evaluate("cy.edges().filter(e => e.data('confidence') < 0.95).length === 0"))
    page.evaluate("document.querySelector('#minConf').value = 0; document.querySelector('#minConf').dispatchEvent(new Event('input'))"); settle(page)
    page.fill("#maxNodes", "10"); page.dispatch_event("#maxNodes", "change"); settle(page)
    check("max nodes truncates and says so", page.evaluate("cy.nodes().length") == 10 and "best-connected" in page.inner_text("#banner"), page.inner_text("#banner"))
    page.fill("#maxNodes", "500"); page.dispatch_event("#maxNodes", "change"); settle(page)
    page.select_option("#level", "file"); settle(page)
    check("file level collapses the graph to files", page.evaluate("cy.nodes().length > 2 && cy.nodes().filter(n => !n.id().startsWith('file:')).length === 0"))
    page.evaluate("cy.edges()[0].emit('tap')"); page.wait_for_selector("#p-detail.on")
    check("a file-level edge explains it is an aggregate", "relationship(s)" in page.inner_text("#p-detail"), page.inner_text("#p-detail")[:100])
    page.select_option("#level", "entity"); settle(page)
    page.fill("#nodeSearch", "addNote")
    check("node search highlights matches", page.evaluate("cy.nodes('.found').length") >= 1)
    page.fill("#nodeSearch", "")

    # ---------------------------------------------------------------- edge evidence and neighbourhood focus
    page.evaluate("cy.edges().filter(e => e.data('label') === 'REQUESTS')[0].emit('tap')")
    page.wait_for_selector("#p-detail.on")
    page.wait_for_function("document.querySelector('#p-detail').textContent.includes('Evidence')", timeout=10000)
    detail = page.inner_text("#p-detail")
    check("clicking an edge shows its evidence snippet and confidence", "fetch" in detail and "confidence" in detail, detail[:200])
    page.locator("#p-detail .cite").first.click()
    page.wait_for_selector("#p-src.on pre")
    check("evidence citation opens the source", len(page.inner_text("#p-src")) > 20)
    page.evaluate("cy.nodes().filter(n => n.data('label') === 'addNote')[0].emit('dbltap')")
    page.wait_for_function("document.querySelector('#graphInfo').textContent.startsWith('Focused on')", timeout=10000)
    focused = page.evaluate("cy.nodes().length")
    check("double-click focuses on the neighbourhood", 2 <= focused < 30, focused)
    page.click("#banner button")
    page.wait_for_function("/^\\d+ nodes/.test(document.querySelector('#graphInfo').textContent)", timeout=10000)   # redraw finished
    check("back to the full graph", page.evaluate("cy.nodes().length") > focused)

    # ---------------------------------------------------------------- login_app: trace, impact, strict mode, contracts
    analyse(page, "test_apps/login_app", "login_app")
    page.click(".tabs div[data-p=flows]")
    page.fill("#traceFrom", FORM); page.fill("#traceTo", FIND); page.click("#traceGo")
    page.wait_for_function("document.querySelector('#traceOut').textContent.includes('Path 1')", timeout=15000)
    trace = page.inner_text("#traceOut")
    check("trace shows hops, relation kinds and a confidence", "REQUESTS" in trace and "HANDLES" in trace and "confidence" in trace, trace[:200])
    check("trace explains a hop that runs against the raw edge", "read against the raw edge" in trace)
    check("trace highlights the path in the graph", page.evaluate("cy.nodes('.impacted').length") >= 5)
    page.fill("#traceFrom", FIND); page.fill("#traceTo", FORM); page.click("#traceGo")
    page.wait_for_function("document.querySelector('#traceOut').textContent.includes('No path')", timeout=15000)
    check("no path is reported honestly and hints at the impact view", "impact view" in page.inner_text("#traceOut"))
    page.select_option("#traceView", "impact"); page.click("#traceGo")
    page.wait_for_function("document.querySelector('#traceOut').textContent.includes('Path 1')", timeout=15000)
    check("the impact view answers the reverse question", "Path 1" in page.inner_text("#traceOut"))

    page.evaluate(f"selectNode('{FIND}')")
    page.wait_for_function("document.querySelector('#impactBody').textContent.includes('Why this risk level')", timeout=15000)
    impact = page.inner_text("#impactBody")
    check("impact lists risk factors with evidence", "reaches 2 user-facing entry point" in impact and "Blast radius: 7" in impact, impact[:300])
    page.check("#strictRefs")
    page.wait_for_function("document.querySelector('#impactBody').textContent.includes('Blast radius: 5')", timeout=15000)
    check("strict mode drops the element referrers (7 -> 5)", "Blast radius: 5" in page.inner_text("#impactBody"))
    page.uncheck("#strictRefs")

    page.click(".tabs div[data-p=contracts]")
    page.wait_for_function("document.querySelector('#p-contracts').textContent.includes('issue(s)')", timeout=15000)
    contracts = page.inner_text("#p-contracts")
    check("contracts tab lists endpoints, keys and clients", "POST /api/login" in contracts and "username" in contracts and "handleLogin" in contracts, contracts[:300])
    card = page.locator("#p-contracts .card", has_text="POST /api/login").first
    card.locator("input").first.fill("message")
    card.locator("button", has_text="Who breaks?").click()
    page.wait_for_function("document.querySelector('#p-contracts').textContent.includes('reads')", timeout=15000)
    check("contract change names the client that reads the key", "certain" in card.inner_text() and "handleLogin" in card.inner_text(), card.inner_text()[-200:])

    # ---------------------------------------------------------------- ask: provider status, suggestions, answer metadata
    page.click(".tabs div[data-p=ask]")
    check("provider status says answers are offline", "offline template" in page.inner_text("#llmStatus"), page.inner_text("#llmStatus"))
    check("suggested repository questions are offered", page.locator("#chips .chip").count() >= 4)
    page.locator("#chips .chip").first.click()
    page.wait_for_function("document.querySelector('#chatlog').textContent.includes('answered by')", timeout=20000)
    answer = page.inner_text("#chatlog")
    check("repository-level question is answered with provider metadata", "intent: overview" in answer and "answered by: template" in answer, answer[-250:])

    # ---------------------------------------------------------------- large repository: file level by default
    analyse(page, ".", "Capstone-Project")
    check("a large repository opens at the file level", page.input_value("#level") == "file" and "Large repository" in page.inner_text("#banner"))
    check("the large graph is small enough to read", 10 < page.evaluate("cy.nodes().length") < 250, page.evaluate("cy.nodes().length"))
    check("file-level labels are short, readable names",
          page.evaluate("cy.nodes().every(n => n.data('label').split('/').length <= 2)"))
    check("switching repository clears the previous conversation",
          "answered by" not in page.inner_text("#chatlog") and "Capstone-Project" in page.inner_text("#chatlog"), page.inner_text("#chatlog")[:120])
    page.wait_for_function("!document.querySelector('#overview').textContent.includes('Loading')", timeout=60000)
    check("the large repository's summary loads and cites its README", "README.md" in page.inner_text("#overview"), page.inner_text("#overview")[:200])
    page.screenshot(path=SHOT)
    clean = len(errors)

    # ---------------------------------------------------------------- provider fallback (second server, OpenRouter requested, no key)
    fb = start_fallback_server()
    try:
        fb_page = browser.new_page(viewport={"width": 1400, "height": 900})
        fb_page.goto("http://127.0.0.1:8767/")
        fb_page.wait_for_function("!document.querySelector('#llmStatus').textContent.includes('Checking')", timeout=15000)
        status = fb_page.inner_text("#llmStatus")
        check("UI says the requested provider is unavailable and why", "openrouter" in status and "missing_key" in status and "offline template" in status, status)
        fb_page.fill("#loc", "test_apps/login_app"); fb_page.click("#go")
        fb_page.wait_for_function("document.querySelector('#status').textContent.includes('login_app')", timeout=60000)
        fb_page.fill("#q", "What are the main components?"); fb_page.click("#askBtn")
        fb_page.wait_for_function("document.querySelector('#chatlog').textContent.includes('answered by')", timeout=20000)
        reply = fb_page.inner_text("#chatlog")
        check("every answer carries a fallback badge", "fallback from openrouter: missing_key" in reply, reply[-250:])
        check("the API key never reaches the page", "sk-" not in fb_page.content() and "OPENROUTER_API_KEY" not in fb_page.content())
    finally:
        fb.kill()

    # ---------------------------------------------------------------- error states (these produce expected console errors)
    page.fill("#loc", "/definitely/not/a/real/path"); page.click("#go")
    page.wait_for_function("document.querySelector('#status').classList.contains('err')", timeout=20000)
    check("an invalid location shows a red error", "Error" in page.inner_text("#status"), page.inner_text("#status"))
    page.click(".tabs div[data-p=flows]")
    page.fill("#traceFrom", "zzz-nothing-matches"); page.fill("#traceTo", "also-nothing"); page.click("#traceGo")
    page.wait_for_selector("#traceOut .card.err", timeout=15000)
    check("an unknown trace endpoint shows an error card, not a blank panel", "No entity matches" in page.inner_text("#traceOut"))
    browser.close()

real_errors = [e for e in errors[:clean]]
check("no JavaScript errors in normal use", not real_errors, "; ".join(real_errors[:3]))
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} Phase 7 UI checks passed")
sys.exit(1 if failed else 0)
