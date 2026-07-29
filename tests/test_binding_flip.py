from regie.config import Profile
from regie.models import Attempt, Binding, Budgets, RunState, TaskSpec, TaskState
from regie.pipeline import _review_binding


def _cfg_profiles(tmp_path, reviewer_cli, builder_cli):
    (tmp_path / "p.md").write_text("x")
    mk = lambda name, cli: Profile(name=name, binding=Binding(cli=cli, model="m"),
                                   prompt_path=tmp_path / "p.md", budgets=Budgets())

    class Cfg:
        def __init__(self):
            self.profiles = {"reviewer": mk("reviewer", reviewer_cli),
                             "builder": mk("builder", builder_cli)}

    return Cfg()


def _run_with_build_attempt(cli):
    run = RunState(id="r", target_repo="/x", branch="b")
    run.tasks["T1"] = TaskState(spec=TaskSpec(id="T1", title="t", profile="builder", criteria=["c"]))
    run.tasks["T1"].attempts["build"].append(
        Attempt(binding=Binding(cli=cli, model="m"), outcome="done"))
    return run


def test_flip_when_builder_used_reviewer_family(tmp_path):
    cfg = _cfg_profiles(tmp_path, reviewer_cli="claude", builder_cli="codex")
    run = _run_with_build_attempt("claude")   # builder escalated onto claude
    assert _review_binding(run, "T1", cfg).cli == "codex"


def test_no_flip_when_families_differ(tmp_path):
    cfg = _cfg_profiles(tmp_path, reviewer_cli="claude", builder_cli="codex")
    run = _run_with_build_attempt("codex")
    assert _review_binding(run, "T1", cfg).cli == "claude"


def test_no_build_attempts_uses_reviewer_default(tmp_path):
    cfg = _cfg_profiles(tmp_path, reviewer_cli="claude", builder_cli="codex")
    run = _run_with_build_attempt("codex")
    run.tasks["T1"].attempts["build"].clear()
    assert _review_binding(run, "T1", cfg).cli == "claude"
