"""End-to-end UI check (optional; not part of the pytest suite).

Drives the real web UI in a headless Edge/Chromium and verifies the main user journeys.
Prerequisites:  pip install playwright   (uses the Edge already installed on Windows; for Chromium run
`playwright install chromium` and change channel="msedge" below)

    # terminal 1 (fresh workspace so saved repositories do not leak in):
    set REPOMIND_WORKSPACE=%TEMP%epomind_e2e & set PYTHONPATH=backend & python -m repomind.cli serve --port 8766
    # terminal 2:
    python backend/e2e/ui_check.py [screenshot.png] [base_url]
"""
import sys, time
from playwright.sync_api import sync_playwright

SHOT = sys.argv[1] if len(sys.argv) > 1 else "ui_check.png"
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8766"
errors, results = [], []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 950})
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)

    page.goto(BASE + "/legacy?location=test_apps/notes_app")
    page.wait_for_function("document.querySelector('#status').textContent.includes('notes_app')", timeout=30000)
    check("repository analysed from the UI", "52 entities" in page.inner_text("#status"), page.inner_text("#status"))
    page.wait_for_function("typeof cy !== 'undefined' && cy.nodes().length > 0")
    check("graph drawn", page.evaluate("cy.nodes().length") > 20, str(page.evaluate("cy.nodes().length")))
    check("saved-repositories picker populated", page.locator("#repoSel option").count() >= 2)

    # sidebar search filters the component list
    page.fill("#left .filters input", "removeNote")
    visible = page.evaluate("[...document.querySelectorAll('#left .comp')].filter(n => n.style.display !== 'none').length")
    check("sidebar search filters components", visible == 1, str(visible))
    page.click("#left .comp:not([style*='none'])")

    # click -> impact tab with categories + highlighting
    page.wait_for_selector("#p-impact.on")
    impact_text = page.inner_text("#p-impact")
    check("impact panel shows blast radius", "Blast radius" in impact_text, impact_text[:120])
    check("impact panel shows categories", "By category" in impact_text)
    check("blast radius highlighted in graph", page.evaluate("cy.nodes('.impacted').length") >= 1)
    check("source node marked", page.evaluate("cy.nodes('.source').length") == 1)

    # detail tab
    page.click(".tabs div[data-p=detail]")
    check("detail tab lists relationships", "outgoing" in page.inner_text("#p-detail").lower())

    # chat -> grounded answer with clickable citation -> source viewer
    page.click(".tabs div[data-p=ask]")
    page.fill("#q", "How does the note form work?")
    page.click("#chatform button")
    page.wait_for_selector(".msg.a .cite", timeout=15000)
    answer = page.inner_text("#chatlog")
    check("chat answer is grounded and mentions the real flow", "addNote" in answer and "create_note" in answer, answer[-300:])
    page.locator(".msg.a .cite").first.click()
    page.wait_for_selector("#p-src.on pre")
    check("citation opens the source viewer", len(page.inner_text("#p-src")) > 20)
    check("source viewer highlights cited lines", page.locator("#p-src .hl").count() >= 1)

    # unanswerable question is refused in the UI
    page.click(".tabs div[data-p=ask]")
    page.fill("#q", "How does payment checkout work?")
    page.click("#chatform button")
    page.wait_for_function("document.querySelector('#chatlog').textContent.includes('could not find')")
    check("unanswerable question refused", True)

    # diff impact
    page.click(".tabs div[data-p=diff]")
    page.fill("#diffText", "--- a/repositories/note_repository.py\n+++ b/repositories/note_repository.py\n@@ -17,1 +17,1 @@\n-x\n+y\n")
    page.click("#diffGo")
    page.wait_for_function("document.querySelector('#diffOut').textContent.includes('Blast radius')", timeout=15000)
    check("diff impact analysed", "NoteService" in page.inner_text("#diffOut") or "create_note" in page.inner_text("#diffOut"),
          page.inner_text("#diffOut")[:200])

    # gaps tab
    page.click(".tabs div[data-p=gaps]")
    gaps = page.inner_text("#p-gaps")
    check("gaps tab lists external packages", "external_package" in gaps and "flask" in gaps.lower(), gaps[:200])

    # flows tab
    page.click(".tabs div[data-p=flows]")
    check("flows tab lists entry points", "Entry" in page.inner_text("#p-flows"))

    # zip upload
    import io, zipfile, pathlib
    zp = pathlib.Path(SHOT).with_name("up.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("tiny/app.py", "def hello():\n    return 1\n")
    page.set_input_files("#zip", str(zp))
    page.wait_for_function("document.querySelector('#status').textContent.includes('tiny')", timeout=30000)
    check("zip upload analysed through the UI", "tiny" in page.inner_text("#status"))

    page.click(".tabs div[data-p=impact]")
    check("stale impact data cleared when switching repository", "removeNote" not in page.inner_text("#p-impact"))
    check("single-node graph is not zoomed absurdly", page.evaluate("cy.zoom()") <= 1.6, str(page.evaluate("cy.zoom()")))
    page.screenshot(path=SHOT)
    browser.close()

check("no JavaScript errors in the console", not errors, "; ".join(errors[:3]))
failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} UI checks passed")
sys.exit(1 if failed else 0)
