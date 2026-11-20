# RepoMind

AI-powered multi-language repository intelligence and change-impact analysis.
Read `HANDOVER.md` for the spec-compliance table, architecture, design decisions, limitations and viva notes.

## Requirements

Python **3.11 or newer** (3.13 recommended). Check with `python --version`. If `python` is older, use `py -3.13`.

## Quick start

**PowerShell**

    py -3.13 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r backend\requirements.txt
    $env:PYTHONPATH="backend"

**cmd.exe**

    py -3.13 -m venv .venv
    .venv\Scripts\activate
    pip install -r backend\requirements.txt
    set PYTHONPATH=backend

**Git Bash**

    py -3.13 -m venv .venv
    source .venv/Scripts/activate
    pip install -r backend/requirements.txt
    export PYTHONPATH=backend

## Use it

    python -m repomind.cli serve                       # web UI at http://127.0.0.1:8000  (API docs at /docs)

    python -m repomind.cli analyze test_apps/login_app
    python -m repomind.cli ask     test_apps/login_app "How does the login flow work?"
    python -m repomind.cli impact  test_apps/login_app --target UserRepository.find_by_username
    python -m repomind.cli impact  <your repo> --since HEAD~1        # what did my last commit affect?
    python -m repomind.cli trace   test_apps/login_app --source "#login-form" --target UserRepository.find_by_username
    python -m repomind.cli graph   test_apps/login_app --format graphml -o graph.graphml

In the web UI, paste a folder path or GitHub URL, or upload a `.zip`.

## Verify

    python -m pytest -c backend/pytest.ini backend/tests      # 181 tests
    python backend/evaluate_ground_truth.py                   # both benchmark apps; exit code 1 on regression
    python backend/benchmark_performance.py                   # scalability numbers

Optional browser test (needs `pip install playwright` and Edge): start the server on port 8766 with a fresh
`REPOMIND_WORKSPACE`, then `python backend/e2e/ui_check.py`.

## Safety notes

RepoMind only *reads* repositories; it never executes their code. Set `REPOMIND_ALLOW_LOCAL_PATHS=0` before
exposing the server to anyone else, and note that it has no authentication.
