import json
from types import SimpleNamespace

from regie.config import Profile, WorkflowConfig
from regie.gitops import commit_all, head_sha
from regie.models import Binding, Budgets, RunState, TaskSpec, TaskState, TokenPolicy
from regie.pipeline import (
    PipelineContext,
    _run_specialist_reviews,
    _specialist_profiles,
)
from regie.rundir import RunDir


def test_specialists_activate_from_committed_risk_evidence(fixture_repo):
    base = head_sha(fixture_repo)
    ui = fixture_repo / "src" / "login.tsx"
    ui.write_text("export const Login = () => null\n")
    commit_all(fixture_repo, "feat: auth login endpoint")
    cfg = SimpleNamespace(profiles={name: object() for name in (
        "security-reviewer", "migration-reviewer", "api-reviewer",
        "ui-reviewer", "architecture-reviewer")})
    task = TaskSpec(
        id="T1", title="Add auth login endpoint", profile="builder",
        criteria=["Given a token, When login runs, Then authorization is checked"],
        file_scope=["src/login.tsx"])

    selected = _specialist_profiles(task, fixture_repo, base, cfg)

    assert "security-reviewer" in selected
    assert "api-reviewer" in selected
    assert "ui-reviewer" in selected
    assert "migration-reviewer" not in selected


def test_hard_task_activates_architecture_review_without_keyword(fixture_repo):
    base = head_sha(fixture_repo)
    (fixture_repo / "src" / "calc.py").write_text("X = 1\n")
    commit_all(fixture_repo, "refactor: calc")
    cfg = SimpleNamespace(profiles={"architecture-reviewer": object()})
    task = TaskSpec(id="T1", title="Refactor calculation", profile="builder",
                    criteria=["Given input, When calculated, Then it returns"],
                    complexity="hard")

    assert _specialist_profiles(task, fixture_repo, base, cfg) == [
        "architecture-reviewer"]


def test_risk_labels_do_not_manufacture_specialist_evidence(fixture_repo):
    base = head_sha(fixture_repo)
    cfg = SimpleNamespace(profiles={name: object() for name in (
        "api-reviewer", "migration-reviewer", "integration-reviewer")})
    task = TaskSpec(
        id="T1", title="Version local persistence", profile="builder",
        criteria=["Given stored tasks When loaded Then they are preserved"],
        risk_tags=["api"],
        review_lenses=["migration-reviewer", "integration-reviewer"],
    )

    assert _specialist_profiles(task, fixture_repo, base, cfg) == [
        "migration-reviewer"]


def test_internal_routing_language_does_not_trigger_api_review(fixture_repo):
    base = head_sha(fixture_repo)
    cfg = SimpleNamespace(profiles={"api-reviewer": object()})
    task = TaskSpec(
        id="T1", title="Persist loaded tasks", profile="builder",
        criteria=["Given tasks When loaded Then the app routes them through saveTasks"],
    )

    assert _specialist_profiles(task, fixture_repo, base, cfg) == []


def test_post_recovery_specialists_are_limited_to_explicit_lenses(fixture_repo):
    base = head_sha(fixture_repo)
    cfg = SimpleNamespace(profiles={name: object() for name in (
        "migration-reviewer", "api-reviewer")})
    task = TaskSpec(
        id="T1", title="Migrate API endpoint storage", profile="builder",
        criteria=["Given an endpoint When migrated Then storage remains valid"],
        review_lenses=["migration-reviewer"],
    )

    assert _specialist_profiles(
        task, fixture_repo, base, cfg, explicit_only=True
    ) == ["migration-reviewer"]


def test_triggered_specialist_records_evidence_and_findings(
        fixture_repo, regie_home, tmp_path):
    base = head_sha(fixture_repo)
    (fixture_repo / "src" / "auth.py").write_text("TOKEN = 'x'\n")
    commit_all(fixture_repo, "feat: auth token")
    prompt = tmp_path / "security.md"
    prompt.write_text("Review security. Return JSON.")
    profile = Profile(
        name="security-reviewer", bindings=[Binding(cli="fake", model="m1")],
        prompt_path=prompt, budgets=Budgets(),
        token_policy=TokenPolicy(tools=["list", "read", "search"], sandbox="read-only"))
    cfg = SimpleNamespace(profiles={"security-reviewer": profile},
                          workflow=WorkflowConfig(default_tier="standard"))
    task = TaskSpec(
        id="T1", title="Handle auth token", profile="builder",
        criteria=["Given a token, When checked, Then authorization is enforced"])
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1")
    run.tasks["T1"] = TaskState(spec=task, start_sha=base)
    rd = RunDir.create(regie_home, "r1")
    (fixture_repo / ".fake_agent.json").write_text(json.dumps({"result": {
        "outcome": "done", "structured": {"findings": [{
            "severity": "major", "title": "token bypass", "detail": "missing check",
            "file": "src/auth.py"}]}}}))

    findings, error = _run_specialist_reviews(
        rd, run, "T1", cfg, fixture_repo,
        PipelineContext(spec_excerpt="", decisions_path=rd.path / "decisions.md"))

    assert error is None and findings[0].title == "token bypass"
    assert run.tasks["T1"].specialist_attempts["security-reviewer"][0].outcome == "done"
