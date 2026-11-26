import json
from pathlib import Path

from repomind.cli import main

LOGIN_APP = str(Path(__file__).resolve().parents[2] / "test_apps" / "login_app")


def test_analyze_json(capsys):
    assert main(["analyze", LOGIN_APP, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["entity_count"] == 40


def test_legacy_bare_path_still_ingests(capsys):
    assert main([LOGIN_APP]) == 0
    assert "Files kept" in capsys.readouterr().out


def test_trace(capsys):
    code = main(["trace", LOGIN_APP, "--source", "#login-form", "--target", "UserRepository.find_by_username"])
    out = capsys.readouterr().out
    assert code == 0 and "--TRIGGERS-->" in out and "--CALLS-->" in out


def test_impact_and_unknown_target(capsys):
    assert main(["impact", LOGIN_APP, "--target", "UserRepository.find_by_username"]) == 0
    assert "Risk: HIGH" in capsys.readouterr().out
    assert main(["impact", LOGIN_APP, "--target", "nothing_xyz"]) == 1


def test_ask_and_refusal(capsys):
    assert main(["ask", LOGIN_APP, "How does the login flow work?"]) == 0
    assert "handleLogin" in capsys.readouterr().out
    assert main(["ask", LOGIN_APP, "how does payment checkout work"]) == 2


def test_bad_path_returns_error(capsys):
    assert main(["analyze", "/no/such/dir"]) == 1


def test_graph_export_json_and_graphml(tmp_path, capsys):
    out = tmp_path / "g.graphml"
    assert main(["graph", LOGIN_APP, "--format", "graphml", "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").count("<node ") == 40
    assert main(["graph", LOGIN_APP, "-o", str(tmp_path / "g.json")]) == 0
    assert len(json.loads((tmp_path / "g.json").read_text(encoding="utf-8"))["nodes"]) == 40
    assert main(["graph", LOGIN_APP, "--format", "graphml"]) == 1          # graphml needs --output


def test_exclude_flag(capsys):
    assert main(["analyze", LOGIN_APP, "--json", "--exclude", "static"]) == 0
    assert json.loads(capsys.readouterr().out)["entity_count"] < 40


def test_impact_from_diff_file(tmp_path, capsys):
    diff = tmp_path / "c.diff"
    diff.write_text("--- a/services/auth_service.py\n+++ b/services/auth_service.py\n@@ -9,1 +9,1 @@\n-a\n+b\n",
                    encoding="utf-8")
    assert main(["impact", LOGIN_APP, "--diff-file", str(diff)]) == 0
    out = capsys.readouterr().out
    assert "diff touching 1 component" in out and "Risk: HIGH" in out and "execution_flow" in out


def test_git_refs_that_look_like_options_are_rejected():
    import pytest
    from repomind.cli import git_diff
    for bad in ("--output=x", "-p", "a b", ""):
        with pytest.raises(ValueError):
            git_diff(LOGIN_APP, bad)
