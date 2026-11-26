"""Phase 8: the unique-method-name heuristic, seed ranking fixes for vague questions, and timing/tokens on answers."""
from pathlib import Path
from types import SimpleNamespace

from repomind.config import Settings
from repomind.graph import RepositoryGraph
from repomind.qa import OpenRouterProvider, QAService
from repomind.services.analysis_service import AnalysisService
from repomind.services.impact_service import ImpactService


def analyze(tmp_path, files):
    repo = tmp_path / "repo"
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    return AnalysisService(Settings(workspace_dir=tmp_path / "ws", log_level="WARNING")).analyze(str(repo))


def calls(a, kind=None):
    return {(r.source_id.split(":", 2)[-1], r.target_id.split(":", 2)[-1], r.kind.value, r.confidence)
            for r in a.relationships if r.type.value == "CALLS" and (kind is None or r.kind.value == kind)}


MODEL = "class User:\n    def check_password(self, pw): pass\n    def get(self): pass\n    def run(self): pass\n"


# --------------------------------------------------------------------------- unique-method-name heuristic
def test_untyped_receiver_with_a_unique_method_name_gets_a_labelled_guess(tmp_path):
    a = analyze(tmp_path, {"models.py": MODEL, "views.py": "def login(form, db):\n    user = db.session.scalar(form)\n    return user.check_password(form.password)\n"})
    assert ("login", "User.check_password", "heuristic", 0.5) in calls(a)


def test_the_guess_is_never_made_for_common_short_or_ambiguous_names(tmp_path):
    a = analyze(tmp_path, {"models.py": MODEL + "\nclass Group:\n    def verify_member(self): pass\n\nclass Team:\n    def verify_member(self): pass\n"
                                                 "\nclass Thing:\n    def short(self): pass\n",
                           "views.py": "def f(x):\n    x.get()\n    x.run()\n    x.verify_member()\n    x.short()\n    x.setdefault('a', 1)\n"})
    assert not calls(a, "heuristic")              # get/run: common; verify_member: two classes; short: < 6 letters; setdefault: dict method


def test_the_guess_is_not_made_for_self_modules_or_typed_receivers(tmp_path):
    a = analyze(tmp_path, {"models.py": MODEL,
                           "views.py": "import os\nfrom models import User\n\nclass V:\n    def f(self, u: User):\n        self.check_password()\n"
                                       "        os.path.check_password()\n        u.check_password('x')\n"})
    got = calls(a)
    assert not [c for c in got if c[2] == "heuristic"]
    assert ("V.f", "User.check_password", "inferred", 0.85) in got          # the typed receiver is resolved properly, not guessed


def test_a_method_calling_a_same_named_method_on_another_object_is_not_guessed(tmp_path):
    a = analyze(tmp_path, {"loader.py": "class Wrapper:\n    def get_source(self, env, name):\n        loader = pick(env)\n        return loader.get_source(env, name)\n"})
    assert not calls(a, "heuristic")


def test_a_guessed_caller_shows_up_in_impact_as_uncertain(tmp_path):
    a = analyze(tmp_path, {"models.py": MODEL, "views.py": "def login(form, db):\n    user = db.session.scalar(form)\n    return user.check_password(form.password)\n"})
    report = ImpactService(RepositoryGraph.from_analysis(a)).analyze_impact("python:models.py:User.check_password")
    hit = {c.entity_id.split(":", 2)[-1]: c for c in report.impacted}
    assert hit["login"].uncertain and hit["login"].confidence == 0.5 and hit["login"].has_inferred_hop


# --------------------------------------------------------------------------- seed ranking for vague questions
def vanilla_repo(tmp_path):
    return analyze(tmp_path, {
        "movie/index.html": '<div class="container"><div class="seat"></div></div><script src="script.js"></script>',
        "movie/script.js": "const container = document.querySelector('.container');\nfunction updateCount() { return 1; }\n"
                           "container.addEventListener('click', e => { if (e.target.classList.contains('seat')) { updateCount(); } });",
        "users/index.html": '<button id="add-user">Add user</button><script src="script.js"></script>',
        "users/script.js": "const btn = document.getElementById('add-user');\nasync function getRandomUser() { return 1; }\nbtn.addEventListener('click', getRandomUser);"})


def test_the_actor_in_a_question_is_not_searched_for(tmp_path):
    a = vanilla_repo(tmp_path)
    qa = QAService(a, settings=Settings(workspace_dir=tmp_path, log_level="WARNING"))
    ans = qa.ask("What happens when the user clicks a seat?")
    assert "updateCount" in ans.answer and ".container click handler" in ans.answer
    assert "getRandomUser" not in ans.answer


def test_the_word_user_is_still_searched_when_it_is_the_subject_matter(tmp_path):
    a = vanilla_repo(tmp_path)
    qa = QAService(a, settings=Settings(workspace_dir=tmp_path, log_level="WARNING"))
    assert "getRandomUser" in qa.ask("Where is the add user button handled?").answer


# --------------------------------------------------------------------------- timing and tokens
def test_answers_report_where_the_time_went(tmp_path):
    a = vanilla_repo(tmp_path)
    ans = QAService(a, settings=Settings(workspace_dir=tmp_path, log_level="WARNING")).ask("What happens when the user clicks a seat?")
    assert ans.retrieval_ms >= 0 and ans.generation_ms >= 0 and (ans.prompt_tokens, ans.completion_tokens) == (0, 0)


def test_llm_token_usage_is_recorded_and_local_time_is_separate(tmp_path):
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kw):
            msg = SimpleNamespace(content="The container handler runs updateCount.")
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=321, completion_tokens=45))

    s = Settings(workspace_dir=tmp_path, log_level="WARNING", openrouter_api_key="sk-or-v1-0123456789abcdef0123456789abcdef", openrouter_model="x/y")
    qa = QAService(vanilla_repo(tmp_path), provider=OpenRouterProvider(s, client=Client()), settings=s)
    ans = qa.ask("What happens when the user clicks a seat?")
    assert (ans.prompt_tokens, ans.completion_tokens) == (321, 45) and ans.provider == "openrouter"
    assert ans.retrieval_ms >= 0 and ans.generation_ms >= 0
