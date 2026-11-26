"""Large-repository behaviour found in Phase 8: question answering used to take seconds at 10k entities."""
import time
from pathlib import Path

from repomind.config import Settings
from repomind.qa import QAService
from repomind.services.analysis_service import AnalysisService


def analyze(tmp_path, files):
    repo = tmp_path / "repo"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(repo))


def many_endpoints(n):
    routes = "".join(f"@app.get('/api/item{i}')\ndef item{i}():\n    return {{'i': {i}}}\n\n" for i in range(n))
    return {"app.py": "from fastapi import FastAPI\napp = FastAPI()\n\n" + routes}


def test_architecture_answer_is_bounded_and_says_what_it_left_out(tmp_path):
    a = analyze(tmp_path, many_endpoints(60))
    ans = QAService(a, settings=Settings(workspace_dir=tmp_path, log_level="WARNING")).ask("Explain the architecture")
    endpoint_lines = [ln for ln in ans.answer.splitlines() if "is handled by" in ln]
    assert len(endpoint_lines) == 25                                      # capped
    assert "35 further API endpoint(s) are not listed here (showing the 25 best connected of 60)" in ans.answer


def test_small_repositories_are_not_truncated(tmp_path):
    a = analyze(tmp_path, many_endpoints(5))
    ans = QAService(a, settings=Settings(workspace_dir=tmp_path, log_level="WARNING")).ask("Explain the architecture")
    assert len([ln for ln in ans.answer.splitlines() if "is handled by" in ln]) == 5 and "further API endpoint" not in ans.answer


def test_entry_flow_summary_is_capped_for_many_entry_points(tmp_path):
    files = {"app.py": "from fastapi import FastAPI\napp = FastAPI()\n\n" + "".join(
        f"@app.get('/r{i}')\ndef r{i}():\n    return helper{i}()\n\ndef helper{i}():\n    return {i}\n\n" for i in range(30))}
    ans = QAService(analyze(tmp_path, files), settings=Settings(workspace_dir=tmp_path, log_level="WARNING")).ask("Explain the architecture")
    assert len([ln for ln in ans.answer.splitlines() if "starts a flow reaching" in ln]) == 10
    assert "30 entry points exist; flows are summarised for the 10 with the most outgoing links" in ans.answer


def test_questions_stay_fast_on_a_few_thousand_entities(tmp_path):
    """Generous bound (the old code took ~1 s per question already at this size on a busy machine)."""
    files = {f"m{i}.py": f"def f{i}(x):\n    return g{i}(x)\n\ndef g{i}(x):\n    return x\n\nclass C{i}:\n    def run(self):\n        return f{i}(1)\n"
             for i in range(400)}
    qa = QAService(analyze(tmp_path, files), settings=Settings(workspace_dir=tmp_path, log_level="WARNING"))
    for question in ("What is the purpose of this repository?", "Explain the architecture", "Who calls f7?", "What breaks if I change g12?"):
        start = time.perf_counter()
        assert qa.ask(question).answer
        assert time.perf_counter() - start < 3.0, question


def test_exact_names_still_win_after_the_substring_prefilter(tmp_path):
    files = {"a.py": "def handle_login(x):\n    return x\n\ndef handle_logout(x):\n    return x\n"}
    qa = QAService(analyze(tmp_path, files), settings=Settings(workspace_dir=tmp_path, log_level="WARNING"))
    ans = qa.ask("What does handle_login do?")
    assert "handle_login" in ans.answer and "handle_logout" not in ans.answer.split("handle_login")[0]
