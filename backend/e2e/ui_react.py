"""Browser end-to-end check of the React frontend (frontend/), served by the API from its production build.

    cd frontend; npm install; npm run build        (once)
    python backend/e2e/ui_react.py [screenshot-prefix]

Starts its own server on port 8768 with the offline provider, analyses test_apps/login_app through the UI and
walks every page. Needs Playwright with Microsoft Edge (as the other e2e scripts do).
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SHOT = sys.argv[1] if len(sys.argv) > 1 else "ui_react"
PORT = 8768
BASE = f"http://127.0.0.1:{PORT}"
FORM = "html:static/index.html:#login-form"
FIND = "python:repositories/user_repository.py:UserRepository.find_by_username"
errors, results = [], []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if detail and not cond else ""))


if not (ROOT / "frontend" / "dist" / "index.html").exists():
    raise SystemExit("frontend/dist is missing: run `npm run build` in frontend/ first")

env = {**os.environ, "PYTHONPATH": str(ROOT / "backend"), "REPOMIND_WORKSPACE": tempfile.mkdtemp(prefix="repomind_react_"),
       "REPOMIND_LLM_PROVIDER": "template", "REPOMIND_RETRIEVAL_MODE": "tfidf"}
server = subprocess.Popen([sys.executable, "-m", "repomind.cli", "serve", "--port", str(PORT)], cwd=str(ROOT), env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/api/llm/status", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        raise SystemExit("server did not start")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 950})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)

        # ------------------------------------------------------------ home + analysis
        page.goto(BASE + "/")
        check("the React app is served at /", page.locator("#root .brand").count() == 1 and "RepoMind" in page.title())
        check("empty state before any analysis", page.get_by_text("No repositories analyzed yet.").count() == 1)
        page.get_by_role("button", name="Analyze", exact=True).click()
        check("empty GitHub URL is rejected in the form", page.get_by_text("Enter a GitHub URL.").count() == 1)
        page.get_by_role("tab", name="Local folder").click()
        page.get_by_placeholder("C:\\projects\\my-app").fill(str(ROOT / "test_apps" / "login_app"))
        page.get_by_role("button", name="Analyze", exact=True).click()
        page.wait_for_url("**/repo/*", timeout=60000)
        check("analysis navigates to the repository", "/repo/" in page.url, page.url)
        repo_id = page.url.rstrip("/").split("/repo/")[1].split("/")[0]
        page.get_by_role("heading", name="login_app").wait_for(timeout=15000)

        # ------------------------------------------------------------ overview
        page.get_by_text("What this repository is").wait_for()
        check("overview shows four statistic cards", page.locator(".stat").count() == 4)
        check("overview lists the languages", page.locator(".card", has_text="Languages").get_by_text("python").count() >= 1)
        check("overview explains link certainty", page.get_by_text("a labelled guess").count() == 1)
        check("overview lists deterministic facts with citations", page.locator("ol li").count() >= 1)
        check("sidebar shows the answer engine", page.get_by_label("LLM status").count() == 1 and "template" in page.get_by_label("LLM status").inner_text())
        page.screenshot(path=f"{SHOT}_overview.png")

        # ------------------------------------------------------------ explorer
        page.get_by_role("link", name="Explorer").click()
        page.get_by_role("button", name="app.py").first.wait_for(timeout=15000)
        check("explorer shows a file tree with folders", page.locator(".tree .node").count() >= 4 and page.get_by_role("button", name="services").count() >= 1)
        page.get_by_role("button", name="app.py").first.click()
        page.locator(".code .ln").first.wait_for(timeout=15000)
        check("clicking a file shows its numbered source", page.locator(".code .ln").count() > 5)
        page.get_by_label("Entities in this file").locator("button.chip").first.wait_for(timeout=15000)
        page.get_by_label("Entities in this file").locator("button.chip").first.click()
        page.get_by_text("Used by (incoming)").wait_for(timeout=15000)
        check("selecting an entity shows its incoming and outgoing links", page.get_by_text("Uses / calls (outgoing)").count() == 1)
        page.get_by_label("Filter files").fill("zzzz-nothing")
        check("file filter with no match says so", page.get_by_text("No files match.").count() == 1)
        page.get_by_label("Filter files").fill("")

        # ------------------------------------------------------------ graph
        page.get_by_role("link", name="Graph", exact=True).click()
        page.locator("[aria-label^='Graph with']").wait_for(timeout=20000)
        label = page.locator("[aria-label^='Graph with']").get_attribute("aria-label")
        n_all = int(label.split()[2])
        check("graph renders nodes and edges", n_all > 5 and page.locator(".graph-canvas canvas").count() >= 1, label)
        page.get_by_role("button", name="function", exact=True).click()
        page.wait_for_function(f"!document.querySelector(\"[aria-label^='Graph with']\").getAttribute('aria-label').startsWith('Graph with {n_all} ')", timeout=15000)
        n_fn = int(page.locator("[aria-label^='Graph with']").get_attribute("aria-label").split()[2])
        check("type filter narrows the graph", 0 < n_fn < n_all, f"{n_fn} of {n_all}")
        page.get_by_role("button", name="function", exact=True).click()
        page.get_by_label("View").select_option("file")
        page.wait_for_function("document.querySelector(\"[aria-label^='Graph with']\") !== null", timeout=15000)
        check("file-level view works", "Graph with" in page.locator("[aria-label^='Graph with']").get_attribute("aria-label"))
        page.get_by_label("View").select_option("entity")
        page.wait_for_timeout(1500)
        page.screenshot(path=f"{SHOT}_graph.png")
        page.get_by_placeholder("e.g. login").fill("zzzz-no-such-thing")
        page.get_by_text("Nothing matches these filters.").wait_for(timeout=15000)
        check("a filter with no result shows a message, not a blank canvas", True)
        page.get_by_placeholder("e.g. login").fill("")

        # ------------------------------------------------------------ trace
        page.get_by_role("link", name="Trace").click()
        page.get_by_label("From", exact=True).fill(FORM)
        page.get_by_label("To", exact=True).fill(FIND)
        page.get_by_role("button", name="Trace", exact=True).click()
        page.get_by_text("Path 1").wait_for(timeout=15000)
        check("trace finds a path from the login form to the repository lookup", page.locator("ol li").count() >= 4)
        check("each hop shows how certain it is", page.locator(".badge.detected, .badge.inferred, .badge.heuristic").count() >= 1)
        page.get_by_label("From", exact=True).fill("zzz-nothing-matches")
        page.get_by_role("button", name="Trace", exact=True).click()
        page.get_by_role("alert").wait_for(timeout=15000)
        check("unknown trace endpoint shows an error", "No entity matches" in page.get_by_role("alert").inner_text())

        # ------------------------------------------------------------ impact
        page.get_by_role("link", name="Impact").click()
        page.get_by_label("Component to change").fill(FIND)
        page.get_by_role("button", name="Analyze impact").click()
        page.get_by_text("Impacted components (").wait_for(timeout=20000)
        check("impact shows a risk level", page.locator(".stat .badge.low, .stat .badge.medium, .stat .badge.high").count() == 1)
        check("impact lists impacted components", page.locator("table.t tbody tr").count() >= 2)
        page.locator("table.t button.chip").first.click()
        check("a component expands into its evidence chain", page.get_by_text("→").count() >= 1)
        page.get_by_role("button", name="A git diff").click()
        check("diff mode offers a textarea and is disabled when empty", page.get_by_role("button", name="Analyze impact").is_disabled())
        page.screenshot(path=f"{SHOT}_impact.png")
        page.goto(f"{BASE}/repo/{repo_id}/impact?target={FIND}")
        page.get_by_text("Impacted components (").wait_for(timeout=20000)
        check("deep link with ?target= runs the analysis automatically", True)

        # ------------------------------------------------------------ contracts
        page.get_by_role("link", name="API contracts").click()
        page.get_by_text("API contracts", exact=True).first.wait_for()
        page.wait_for_function("document.querySelectorAll('.card .badge').length > 0 || document.querySelector('.empty')", timeout=15000)
        check("contracts page lists endpoints with their callers", page.locator(".card h3 .mono").count() >= 1, page.inner_text("main")[:200])

        # ------------------------------------------------------------ chat
        page.get_by_role("link", name="Ask").click()
        page.get_by_role("button", name="Explain the architecture").click()
        page.get_by_label("Answer").wait_for(timeout=30000)
        check("a suggested question produces a grounded answer", len(page.get_by_label("Answer").inner_text()) > 80)
        cite = page.locator("button.cite").first
        cite.wait_for(timeout=5000)
        cite.click()
        page.locator(".msg .code .ln").first.wait_for(timeout=15000)
        check("clicking a citation shows the cited code", page.locator(".msg .code .ln.hl").count() >= 1)
        check("the answer shows which engine produced it and the timings", "template" in page.get_by_label("Answer").inner_text() and "retrieval" in page.get_by_label("Answer").inner_text())
        page.get_by_label("Question").fill("What is the weather in Paris?")
        page.get_by_role("button", name="Ask", exact=True).click()
        page.wait_for_function("document.querySelectorAll('[aria-label=Answer]').length === 2", timeout=30000)
        check("an off-topic question is refused, not invented", page.get_by_text("not grounded").count() == 1)
        page.screenshot(path=f"{SHOT}_chat.png")

        # ------------------------------------------------------------ routing / persistence
        page.goto(f"{BASE}/repo/{repo_id}/graph")
        page.locator("[aria-label^='Graph with']").wait_for(timeout=20000)
        check("refreshing a deep link reloads the same page", True)
        page.goto(f"{BASE}/repo/does-not-exist")
        page.get_by_role("alert").wait_for(timeout=15000)
        check("an unknown repository shows an error, not a blank page", page.get_by_role("alert").count() == 1)
        page.goto(f"{BASE}/nowhere/at/all")
        check("an unknown route shows a not-found page", page.get_by_text("Page not found.").count() == 1)
        page.goto(BASE + "/")
        page.get_by_role("link", name="login_app").wait_for()
        check("the repository list shows the analysed repository", page.get_by_role("link", name="login_app").count() == 1)
        page.on("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Delete login_app").click()
        page.get_by_text("No repositories analyzed yet.").wait_for(timeout=15000)
        check("deleting a repository removes it from the list", True)

        # ------------------------------------------------------------ narrow screen
        page.set_viewport_size({"width": 390, "height": 800})
        page.goto(BASE + "/")
        check("no horizontal scrolling on a phone-sized screen", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
              page.evaluate("document.documentElement.scrollWidth"))
        browser.close()
finally:
    server.kill()

expected = ("404", "422", "Failed to load resource")          # the deliberate error-state checks above
real = [e for e in errors if not any(x in e for x in expected)]
check("no unexpected JavaScript errors", not real, "; ".join(real[:3]))
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} React UI checks passed")
sys.exit(1 if failed else 0)
