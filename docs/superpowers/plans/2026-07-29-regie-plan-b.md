# Régie Plan B — Real Adapters, Git/PR Stages, Full Pipeline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Régie's engine to the real world: Claude/Codex adapters with quota detection, the run worktree + branch + squash + push + PR git layer, the planner stage with the `regie approve` checkpoint, finalize/PR stages with CI-watch debugger rounds, and notifications — completing brief→PR.

**Architecture:** Everything extends Plan A's seams: new adapters register in the existing registry; git operations extend `gitops.py` (orchestrator owns git — agents never run git); new pipeline stages are plain functions called by the CLI in sequence (`plan → approve → tasks → finalize → pr`). E2E remains fake-adapter-driven with a local bare "origin" and a stubbed `gh` on PATH; real-CLI validation is a supervised manual smoke test.

**Tech Stack:** unchanged (Python 3.12+, uv, typer, pydantic, pytest). External binaries used at runtime: `claude`, `codex`, `git`, `gh`.

## Global Constraints (apply to every task; Plan A's constraints still hold)

- The orchestrator owns git: agents edit files only; every git command is `regie.gitops`.
- Agents run with cwd = the RUN WORKTREE, never the user's checkout.
- No force-push, ever, in v1: history rewriting happens strictly BEFORE the first push (squash pre-push; debugger rounds append commits post-push).
- History rewrite safety: backup ref `refs/regie/backup/<run-id>` created first; post-rewrite tree hash must equal pre-rewrite tree hash or restore + raise.
- Quota errors map to `AgentResult(outcome="quota")` → ladder halts immediately (never burns retries).
- Cross-model review: at review dispatch, if the reviewer profile's `binding.cli` equals the builder's actual last-attempt `cli`, flip to the builder profile's configured binding.
- Claude headless: `--output-format json`, `--max-turns`; never `--bare` (breaks subscription auth).
- CLI copy: halts and completions send a desktop notification (macOS `osascript`; print fallback).
- Fixture-based adapter tests define the SUPPORTED output contract; unrecognized shapes must degrade to `outcome="error"` (never crash the engine).
- Branch naming `regie/<run-id>`; base branch from `regie.toml` `base_branch` (default `"main"`).

## File Structure

```
src/regie/agents/claude.py   # Claude Code adapter (result-JSON parsing, quota)
src/regie/agents/codex.py    # Codex adapter (JSONL event parsing, quota)
src/regie/gitops.py          # + worktree/branch/squash/push/PR/CI helpers
src/regie/pipeline.py        # + binding-flip; plan/finalize/pr stage functions
src/regie/notify.py          # desktop notification
src/regie/cli.py             # + approve/clean commands, worktree wiring, guards
src/regie/models.py          # + RunState fields (worktree_path, base_branch, pr_url, planner_attempts, autonomous)
profiles/debugger.yaml       # debugger binding (md exists)
tests/test_claude_adapter.py tests/test_codex_adapter.py tests/test_binding_flip.py
tests/test_gitops_flow.py    tests/test_plan_stage.py  tests/test_finalize_pr.py
tests/test_e2e_full.py       (+ existing files extended)
```

---

### Task 1: Claude adapter

**Files:**
- Create: `src/regie/agents/claude.py`
- Modify: `src/regie/agents/__init__.py` (import claude for registration)
- Test: `tests/test_claude_adapter.py`

**Interfaces:**
- Consumes: `regie.agents.base` (AgentRequest/AgentResult/register).
- Produces: `ClaudeAdapter` registered as cli `"claude"`.
  - `build_command(req)` → `["claude", "-p", req.prompt, "--output-format", "json", "--max-turns", str(req.budgets.turns), "--model", req.binding.model, "--permission-mode", "acceptEdits"]`; when `req.output_schema` is set, first write it to `<req.cwd>/.regie_schema.json` and append `["--json-schema", str(path)]`.
  - `parse(stdout, exit_code)` — stdout's LAST line that parses as a JSON object is the result document (the CLI may print nothing else; be tolerant of leading noise). Mapping rules (this is the supported contract):
    - quota: `api_error_status` in (429, 529) OR `terminal_reason` containing "quota"/"limit" (case-insensitive) OR is_error with result text matching r"(usage|rate).?limit" → `outcome="quota"`.
    - blocked: result text contains a line starting `blocked:` → `outcome="blocked"`, `blocked_question` = rest of that line.
    - error: `is_error` true, or exit_code != 0, or no parseable JSON line → `outcome="error"`, text = last 2000 chars.
    - else `outcome="done"`; `text` = `result` field; `structured` = `json.loads(result)` if it parses to a dict else None; `turns` = `num_turns` (default 0); `usage` = `{**usage, "total_cost_usd": total_cost_usd, "modelUsage": modelUsage}` (missing keys tolerated).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_claude_adapter.py
import json

from regie.agents.base import AgentRequest, get_adapter
from regie.models import Binding, Budgets


def _req(tmp_path, schema=None):
    return AgentRequest(prompt="do the task", cwd=tmp_path,
                        binding=Binding(cli="claude", model="opus"),
                        budgets=Budgets(turns=7), output_schema=schema)


def _doc(**over):
    base = {"is_error": False, "subtype": "success", "result": "done the thing",
            "num_turns": 3, "usage": {"input_tokens": 10, "output_tokens": 5},
            "modelUsage": {}, "total_cost_usd": 0.12}
    base.update(over)
    return json.dumps(base)


def test_build_command_flags(tmp_path):
    cmd = get_adapter("claude").build_command(_req(tmp_path))
    assert cmd[:3] == ["claude", "-p", "do the task"]
    for flag, val in (("--output-format", "json"), ("--max-turns", "7"),
                      ("--model", "opus"), ("--permission-mode", "acceptEdits")):
        assert val == cmd[cmd.index(flag) + 1]
    assert "--json-schema" not in cmd and "--bare" not in cmd


def test_build_command_writes_schema_file(tmp_path):
    cmd = get_adapter("claude").build_command(_req(tmp_path, schema={"type": "object"}))
    path = cmd[cmd.index("--json-schema") + 1]
    assert json.loads(open(path).read()) == {"type": "object"}
    assert path.startswith(str(tmp_path))


def test_parse_done_with_usage_and_noise(tmp_path):
    out = "some log noise\n" + _doc()
    r = get_adapter("claude").parse(out, 0)
    assert r.outcome == "done" and r.turns == 3
    assert r.usage["total_cost_usd"] == 0.12 and r.text == "done the thing"


def test_parse_structured_result(tmp_path):
    doc = _doc(result=json.dumps({"findings": []}))
    r = get_adapter("claude").parse(doc, 0)
    assert r.structured == {"findings": []}


def test_parse_quota_from_api_error_status():
    r = get_adapter("claude").parse(_doc(is_error=True, api_error_status=429), 0)
    assert r.outcome == "quota"


def test_parse_quota_from_terminal_reason():
    r = get_adapter("claude").parse(_doc(terminal_reason="usage_limit_reached"), 0)
    assert r.outcome == "quota"


def test_parse_blocked_line():
    r = get_adapter("claude").parse(_doc(result="analysis...\nblocked: cache per user or global?"), 0)
    assert r.outcome == "blocked" and "cache per user" in r.blocked_question


def test_parse_error_on_garbage_and_nonzero_exit():
    assert get_adapter("claude").parse("not json at all", 0).outcome == "error"
    assert get_adapter("claude").parse(_doc(), 1).outcome == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_claude_adapter.py -v`
Expected: FAIL — KeyError `'claude'` from the registry.

- [ ] **Step 3: Implement**

```python
# src/regie/agents/claude.py
from __future__ import annotations

import json
import re

from regie.agents.base import AgentRequest, AgentResult, register

_QUOTA_STATUS = {429, 529}
_QUOTA_TEXT = re.compile(r"(usage|rate).?limit", re.I)


def _last_json_object(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                doc = json.loads(line)
                if isinstance(doc, dict):
                    return doc
            except json.JSONDecodeError:
                continue
    return None


class ClaudeAdapter:
    def build_command(self, req: AgentRequest) -> list[str]:
        cmd = ["claude", "-p", req.prompt, "--output-format", "json",
               "--max-turns", str(req.budgets.turns),
               "--model", req.binding.model, "--permission-mode", "acceptEdits"]
        if req.output_schema is not None:
            schema_path = req.cwd / ".regie_schema.json"
            schema_path.write_text(json.dumps(req.output_schema))
            cmd += ["--json-schema", str(schema_path)]
        return cmd

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        doc = _last_json_object(stdout)
        if doc is None:
            return AgentResult(outcome="error", text=stdout[-2000:])
        text = str(doc.get("result", ""))
        reason = str(doc.get("terminal_reason", ""))
        if (doc.get("api_error_status") in _QUOTA_STATUS
                or re.search(r"quota|limit", reason, re.I)
                or (doc.get("is_error") and _QUOTA_TEXT.search(text))):
            return AgentResult(outcome="quota", text=text[-2000:])
        if doc.get("is_error") or exit_code != 0:
            return AgentResult(outcome="error", text=text[-2000:] or stdout[-2000:])
        for line in text.splitlines():
            if line.strip().lower().startswith("blocked:"):
                return AgentResult(outcome="blocked", text=text,
                                   blocked_question=line.split(":", 1)[1].strip())
        structured = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                structured = parsed
        except (json.JSONDecodeError, TypeError):
            pass
        usage = dict(doc.get("usage") or {})
        usage["total_cost_usd"] = doc.get("total_cost_usd")
        usage["modelUsage"] = doc.get("modelUsage") or {}
        return AgentResult(outcome="done", text=text, structured=structured,
                           usage=usage, turns=int(doc.get("num_turns") or 0))


register("claude", ClaudeAdapter())
```

Add to `src/regie/agents/__init__.py`: `from regie.agents import claude  # noqa: F401`.

- [ ] **Step 4: Run tests to verify they pass** — `uv run pytest tests/test_claude_adapter.py -v`, expected 8 PASS.
- [ ] **Step 5: Full suite + commit**

```bash
uv run pytest -q && uvx ruff check src tests
git add src/regie/agents tests/test_claude_adapter.py
git commit -m "feat(agents): claude headless adapter with quota and blocked detection"
```

---

### Task 2: Codex adapter

**Files:**
- Create: `src/regie/agents/codex.py`
- Modify: `src/regie/agents/__init__.py`
- Test: `tests/test_codex_adapter.py`

**Interfaces:**
- Produces: `CodexAdapter` registered as `"codex"`.
  - `build_command(req)` → `["codex", "exec", "--json", "-m", req.binding.model, "--sandbox", "workspace-write", "--skip-git-repo-check", req.prompt]`; with `output_schema`: write `<cwd>/.regie_schema.json`, append `["--output-schema", str(path)]`.
  - `parse(stdout, exit_code)` — stdout is a JSONL event stream. Supported contract: each parseable line is a dict; collect agent text from events where `type == "item.completed"` and `item.type == "agent_message"` (text at `item.text`) OR `type == "agent_message"` (text at `text`); last such text wins. Events with `type == "error"`: message at `message`; if it matches r"(usage|rate).?limit|quota" (case-insensitive) → quota, else error. `turns` = count of agent-message events. Same `blocked:` line rule as claude. Nonzero exit with no quota/error event → error. No agent message at all → error. `structured` = final text json.loads'd if dict. `usage` = `{}` (Codex exec exposes no usage telemetry — known limitation, documented in the module docstring).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_codex_adapter.py
import json

from regie.agents.base import AgentRequest, get_adapter
from regie.models import Binding, Budgets


def _req(tmp_path):
    return AgentRequest(prompt="build it", cwd=tmp_path,
                        binding=Binding(cli="codex", model="gpt-5.x"),
                        budgets=Budgets())


def _lines(*events) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_build_command_flags(tmp_path):
    cmd = get_adapter("codex").build_command(_req(tmp_path))
    assert cmd[:3] == ["codex", "exec", "--json"]
    assert cmd[cmd.index("-m") + 1] == "gpt-5.x"
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[-1] == "build it"


def test_parse_takes_last_agent_message():
    out = _lines({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
                 {"type": "item.completed", "item": {"type": "reasoning", "text": "x"}},
                 {"type": "agent_message", "text": "final answer"})
    r = get_adapter("codex").parse(out, 0)
    assert r.outcome == "done" and r.text == "final answer" and r.turns == 2


def test_parse_quota_from_error_event():
    out = _lines({"type": "error", "message": "You've hit your usage limit"})
    assert get_adapter("codex").parse(out, 1).outcome == "quota"


def test_parse_plain_error_event():
    out = _lines({"type": "error", "message": "sandbox denied"})
    assert get_adapter("codex").parse(out, 1).outcome == "error"


def test_parse_blocked_and_structured(tmp_path):
    out = _lines({"type": "agent_message", "text": "blocked: bad-test: asserts impossible"})
    r = get_adapter("codex").parse(out, 0)
    assert r.outcome == "blocked" and r.blocked_question.startswith("bad-test:")
    out2 = _lines({"type": "agent_message", "text": json.dumps({"findings": []})})
    assert get_adapter("codex").parse(out2, 0).structured == {"findings": []}


def test_parse_no_message_is_error():
    assert get_adapter("codex").parse("", 0).outcome == "error"
    assert get_adapter("codex").parse("garbage\nlines", 0).outcome == "error"
```

- [ ] **Step 2: Verify failure** — KeyError `'codex'`.
- [ ] **Step 3: Implement**

```python
# src/regie/agents/codex.py
"""Codex exec adapter. NOTE: codex exec --json exposes no usage/quota telemetry
(rate_limits always null in exec mode) — usage stays empty; quota is detected
only via error events."""
from __future__ import annotations

import json
import re

from regie.agents.base import AgentRequest, AgentResult, register

_QUOTA = re.compile(r"(usage|rate).?limit|quota", re.I)


class CodexAdapter:
    def build_command(self, req: AgentRequest) -> list[str]:
        cmd = ["codex", "exec", "--json", "-m", req.binding.model,
               "--sandbox", "workspace-write", "--skip-git-repo-check"]
        if req.output_schema is not None:
            schema_path = req.cwd / ".regie_schema.json"
            schema_path.write_text(json.dumps(req.output_schema))
            cmd += ["--output-schema", str(schema_path)]
        return cmd + [req.prompt]

    def parse(self, stdout: str, exit_code: int) -> AgentResult:
        text, turns, error_msg = None, 0, None
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "agent_message":
                text, turns = str(ev.get("text", "")), turns + 1
            elif (ev.get("type") == "item.completed"
                  and isinstance(ev.get("item"), dict)
                  and ev["item"].get("type") == "agent_message"):
                text, turns = str(ev["item"].get("text", "")), turns + 1
            elif ev.get("type") == "error":
                error_msg = str(ev.get("message", ""))
        if error_msg is not None:
            outcome = "quota" if _QUOTA.search(error_msg) else "error"
            return AgentResult(outcome=outcome, text=error_msg[-2000:])
        if text is None:
            return AgentResult(outcome="error", text=stdout[-2000:])
        if exit_code != 0:
            return AgentResult(outcome="error", text=text[-2000:])
        for line in text.splitlines():
            if line.strip().lower().startswith("blocked:"):
                return AgentResult(outcome="blocked", text=text, turns=turns,
                                   blocked_question=line.split(":", 1)[1].strip())
        structured = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                structured = parsed
        except json.JSONDecodeError:
            pass
        return AgentResult(outcome="done", text=text, structured=structured, turns=turns)


register("codex", CodexAdapter())
```

Add `from regie.agents import codex  # noqa: F401` to `agents/__init__.py`.

- [ ] **Step 4: Verify pass** (6 tests), **Step 5: full suite + ruff + commit** `feat(agents): codex exec adapter with JSONL event parsing`.

---

### Task 3: Reviewer binding-flip (carried MUST from Plan A)

**Files:**
- Modify: `src/regie/pipeline.py` (in `_dispatch`, review stage only)
- Modify: `src/regie/agents/fake.py` (also `register("fake2", FakeAdapter())` — a second CLI identity for tests)
- Test: `tests/test_binding_flip.py`

**Interfaces:**
- Produces: in `_dispatch`, when `stage == "review"` and no prior review attempts (first review dispatch — retries/escalations then proceed from that binding via the ladder as usual): let `builder_binding` = `run.tasks[task_id].attempts["build"][-1].binding` (if any build attempt exists). If `builder_binding.cli == profile.binding.cli`, use `cfg.profiles["builder"].binding` instead of the reviewer profile's binding. Function extracted as `_review_binding(run, task_id, cfg) -> Binding` for testability.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_binding_flip.py
from regie.config import Profile
from regie.models import Attempt, Binding, Budgets, RunState, TaskSpec, TaskState
from regie.pipeline import _review_binding


def _cfg_profiles(tmp_path, reviewer_cli, builder_cli):
    (tmp_path / "p.md").write_text("x")
    mk = lambda name, cli: Profile(name=name, binding=Binding(cli=cli, model="m"),
                                   prompt_path=tmp_path / "p.md", budgets=Budgets())
    class Cfg: profiles = {"reviewer": mk("reviewer", reviewer_cli),
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
```

- [ ] **Step 2: Verify failure** (ImportError `_review_binding`).
- [ ] **Step 3: Implement** — in pipeline.py:

```python
def _review_binding(run: RunState, task_id: str, cfg: RegieConfig) -> Binding:
    """Cross-model rule: reviewer must not share the builder's model family."""
    reviewer = cfg.profiles["reviewer"].binding
    builds = run.tasks[task_id].attempts["build"]
    if builds and builds[-1].binding.cli == reviewer.cli:
        return cfg.profiles["builder"].binding
    return reviewer
```

In `_dispatch`, replace `binding = profile.binding` with:
```python
    binding = (_review_binding(run, task_id, cfg) if stage == "review"
               else profile.binding)
```
(the existing `if attempts:` ladder override below it stays — retries continue from the recorded binding). In `fake.py`, add `register("fake2", FakeAdapter())` beside the existing register.

- [ ] **Step 4: Verify pass; full suite; commit** `feat(pipeline): enforce cross-model reviewer binding at dispatch`.

---

### Task 4: Gitops — run worktree, base pinning, push, remote fixture

**Files:**
- Modify: `src/regie/gitops.py`
- Test: `tests/test_gitops_flow.py`; Modify: `tests/conftest.py` (add `remote_repo` fixture)

**Interfaces:**
- Produces (all raise `GitError` on failure, all built on the existing `git()`):
  - `head_sha(repo: Path, ref: str = "HEAD") -> str` (full sha, `rev-parse`)
  - `fetch_base_sha(repo: Path, base_branch: str) -> str` — `git fetch origin <base>` then rev-parse `origin/<base>`
  - `create_run_worktree(repo: Path, branch: str, base_sha: str, dest: Path) -> Path` — `git worktree add -b <branch> <dest> <base_sha>`
  - `remove_run_worktree(repo: Path, dest: Path) -> None` — `worktree remove --force` + `worktree prune`; missing worktree is not an error
  - `delete_branch(repo: Path, branch: str) -> None` — refuses (raises GitError) unless branch starts with `"regie/"`
  - `push_branch(worktree: Path, branch: str) -> None` — `git push -u origin <branch>` (plain push; no force variants exist in this module)
- Conftest fixture `remote_repo(fixture_repo)`: creates a bare clone as `origin` — `git clone --bare fixture_repo <tmp>/origin.git` then `git remote add origin <path>` in fixture_repo (or `set-url` if exists); returns the bare path.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gitops_flow.py
import subprocess

import pytest
from regie.gitops import (GitError, create_run_worktree, delete_branch,
                          fetch_base_sha, head_sha, push_branch,
                          remove_run_worktree)


def test_worktree_lifecycle_off_pinned_base(fixture_repo, tmp_path):
    base = head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r1", base, tmp_path / "wt")
    assert (wt / "src" / "calc.py").exists()
    assert head_sha(wt) == base
    remove_run_worktree(fixture_repo, wt)
    assert not wt.exists()
    remove_run_worktree(fixture_repo, wt)  # idempotent


def test_fetch_base_and_push(fixture_repo, remote_repo, tmp_path):
    base = fetch_base_sha(fixture_repo, "main")
    assert base == head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r2", base, tmp_path / "wt2")
    (wt / "new.txt").write_text("x")
    from regie.gitops import commit_all
    commit_all(wt, "feat(T1): x")
    push_branch(wt, "regie/r2")
    out = subprocess.run(["git", "-C", str(remote_repo), "branch"],
                         capture_output=True, text=True).stdout
    assert "regie/r2" in out


def test_delete_branch_refuses_non_regie(fixture_repo):
    with pytest.raises(GitError):
        delete_branch(fixture_repo, "main")
```

Conftest addition:

```python
@pytest.fixture
def remote_repo(fixture_repo, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", "-q", str(fixture_repo), str(bare)], check=True)
    subprocess.run(["git", "-C", str(fixture_repo), "remote", "add", "origin", str(bare)], check=True)
    # fixture_repo's default branch must be named main for fetch_base_sha tests
    subprocess.run(["git", "-C", str(fixture_repo), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(fixture_repo), "push", "-q", "-u", "origin", "main"], check=True)
    return bare
```

- [ ] **Step 2: Verify failure. Step 3: Implement** (each helper is 2-5 lines on top of `git()`; `remove_run_worktree` wraps in try/except GitError for idempotence, then always runs `worktree prune`). **Step 4: verify pass, full suite. Step 5: commit** `feat(gitops): run worktree lifecycle, base pinning, push`.

---

### Task 5: Gitops — history rebuild (squash per task), PR + CI via gh

**Files:**
- Modify: `src/regie/gitops.py`
- Test: `tests/test_gitops_flow.py` (extend)

**Interfaces:**
- Produces:
  - `rebuild_history(worktree: Path, base_sha: str, groups: list[tuple[str, list[str]]], run_id: str) -> None` — groups = ordered `(commit_message, [commit_shas])`. Safety: `pre_tree = git(worktree, "rev-parse", "HEAD^{tree}")`; `git update-ref refs/regie/backup/<run_id> HEAD`; `git reset --hard <base_sha>`; per group: `git cherry-pick -n <sha>...` then `git commit -m <message>`; verify `rev-parse HEAD^{tree}` == pre_tree, else `git reset --hard refs/regie/backup/<run_id>` and raise GitError("tree mismatch after rewrite").
  - `run_commit_groups(worktree: Path, base_sha: str) -> list[tuple[str, list[str]]]` — walk `git log --reverse --format=%H%x09%s <base>..HEAD`; group consecutive commits by the task id in `test(<id>):`/`feat(<id>):` prefixes; group message defaults to the task's feat subject (the message argument for rebuild comes from the caller after the scribe agent runs; this helper only groups shas + provides default messages).
  - `create_pr(worktree: Path, base_branch: str, title: str, body_file: Path) -> str` — runs `gh pr create --base <base> --title <t> --body-file <f>`, returns stdout.strip() (the URL).
  - `ci_status(worktree: Path) -> str` — runs `gh pr checks --json state --jq '[.[].state] | unique | join(",")'`; maps: any "FAILURE" → `"red"`, all "SUCCESS" → `"green"`, else `"pending"`. On GitError (no checks configured) → `"green"` (a repo without CI gates on nothing).
- Tests use the `remote_repo` fixture and a **stub `gh`**: write an executable `gh` script into a tmp dir prepended to PATH via monkeypatch that echoes a canned URL for `pr create` and canned JSON for `pr checks` (both variants tested). rebuild_history is tested for: two tasks' four commits → two commits with given messages, tree identical, backup ref exists; and a sabotage case (monkeypatch one cherry-pick to inject a wrong file — simplest: pass groups that omit one commit sha → tree mismatch → GitError raised and HEAD restored).

- [ ] **Step 1: Write the failing tests**

```python
# appended to tests/test_gitops_flow.py
import os
import stat

from regie.gitops import (ci_status, commit_all, create_pr, git,
                          rebuild_history, run_commit_groups)


def _mk_task_commits(wt):
    (wt / "tests" / "test_a.py").parent.mkdir(exist_ok=True)
    (wt / "tests" / "test_a.py").write_text("def test_a(): assert False\n")
    commit_all(wt, "test(T1): red tests")
    (wt / "src" / "a.py").write_text("A = 1\n")
    commit_all(wt, "feat(T1): implement")
    (wt / "tests" / "test_b.py").write_text("def test_b(): assert False\n")
    commit_all(wt, "test(T2): red tests")
    (wt / "src" / "b.py").write_text("B = 2\n")
    commit_all(wt, "feat(T2): implement")


def test_rebuild_history_squashes_per_task(fixture_repo, tmp_path):
    from regie.gitops import create_run_worktree, head_sha
    base = head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r3", base, tmp_path / "wt3")
    _mk_task_commits(wt)
    pre_tree = git(wt, "rev-parse", "HEAD^{tree}").strip()
    groups = run_commit_groups(wt, base)
    assert [g[0] for g in groups] == ["feat(T1): implement", "feat(T2): implement"]
    rebuild_history(wt, base, [("feat(t1): task one", groups[0][1]),
                               ("feat(t2): task two", groups[1][1])], "r3")
    log = git(wt, "log", "--format=%s", f"{base}..HEAD").split()
    assert len(git(wt, "log", "--format=%H", f"{base}..HEAD").split()) == 2
    assert git(wt, "rev-parse", "HEAD^{tree}").strip() == pre_tree
    assert git(wt, "rev-parse", "refs/regie/backup/r3")


def test_rebuild_history_restores_on_tree_mismatch(fixture_repo, tmp_path):
    import pytest
    from regie.gitops import GitError, create_run_worktree, head_sha
    base = head_sha(fixture_repo)
    wt = create_run_worktree(fixture_repo, "regie/r4", base, tmp_path / "wt4")
    _mk_task_commits(wt)
    head_before = head_sha(wt)
    groups = run_commit_groups(wt, base)
    bad = [("feat: incomplete", groups[0][1][:1])]  # drops commits → tree differs
    with pytest.raises(GitError):
        rebuild_history(wt, base, bad, "r4")
    assert head_sha(wt) == head_before  # restored from backup ref


def _stub_gh(tmp_path, monkeypatch, checks_json):
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text("#!/bin/sh\n"
                  'if [ "$1 $2" = "pr create" ]; then echo "https://github.com/x/y/pull/1"; exit 0; fi\n'
                  f"echo '{checks_json}'\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{gh.parent}:{os.environ['PATH']}")


def test_create_pr_and_ci_status(fixture_repo, tmp_path, monkeypatch):
    _stub_gh(tmp_path, monkeypatch, "SUCCESS")
    body = tmp_path / "body.md"; body.write_text("PR body")
    url = create_pr(fixture_repo, "main", "feat: x", body)
    assert url == "https://github.com/x/y/pull/1"
    assert ci_status(fixture_repo) == "green"
    _stub_gh(tmp_path, monkeypatch, "FAILURE,SUCCESS")
    assert ci_status(fixture_repo) == "red"
```

- [ ] **Step 2: Verify failure. Step 3: Implement.** `run_commit_groups` regex: `^(?:test|feat|fix)\(([^)]+)\):` — group key = capture; consecutive same-key commits merge; default message = the group's LAST subject (the feat). `create_pr`/`ci_status` run `gh` via `subprocess.run` in the worktree cwd (same GitError-on-nonzero pattern as `git()` — factor a private `_tool(cwd, *argv)` helper). **Step 4: verify, full suite. Step 5: commit** `feat(gitops): per-task history rebuild with backup ref, gh pr and ci helpers`.

---

### Task 6: Models/config/CLI groundwork — worktree wiring, guards, clean, notify

**Files:**
- Modify: `src/regie/models.py`, `src/regie/config.py`, `src/regie/cli.py`
- Create: `src/regie/notify.py`
- Test: `tests/test_cli.py` (extend)

**Interfaces:**
- Models: `RunState` gains `worktree_path: str = ""`, `base_branch: str = "main"`, `pr_url: str = ""`, `autonomous: bool = False`, `planner_attempts: list[Attempt] = Field(default_factory=list)`.
- Config: `RegieConfig` gains `base_branch: str = "main"` (from regie.toml, optional) and optional `commands` keys `typecheck`, `eval` (no new required keys).
- notify.py: `notify(title: str, message: str) -> None` — on darwin run `osascript -e 'display notification "<msg>" with title "<title>"'` (shell-safe via list argv), swallow failures; else print `[notify] title: message`.
- CLI changes:
  - `run`: friendly errors — brief missing → exit 2 with message; run dir already exists → exit 2 "run <id> already exists — pick a different brief name or `regie clean <id>`" (catch FileExistsError from RunDir.create); a `.regie-active-<hash-of-repo-path>` marker under `$REGIE_HOME` guards two live runs per target repo: written (with run id) after lock acquisition, removed on process exit via `atexit`; if present and its run's lock is still held → exit 2 "another run is live against this repo".
  - `run` wiring: `base = fetch_base_sha(repo, cfg.base_branch)` (fallback `head_sha(repo, cfg.base_branch)` when no origin — GitError caught); `wt = create_run_worktree(repo, f"regie/{run_id}", base, home/"worktrees"/run_id)`; state records worktree_path/base_branch/base_sha; **all stage functions and gates now receive `Path(state.worktree_path)` as the repo argument** (agents work in the worktree; the user's checkout is never touched). `--autonomous` flag stored on state.
  - `notify()` called on halt (with halt reason) and on run completion in `_finish`.
  - New `clean` command: `regie clean <run-id> --repo <path>` — remove_run_worktree + delete_branch(regie/<id>) (GitError tolerated), keep the run dir, print what was removed.
- Plan A's `tasks.json` intake stays in this task (planner replaces it in Task 7).

- [ ] **Step 1: failing tests** (extend tests/test_cli.py):

```python
def test_run_missing_brief_friendly_error(regie_home, fixture_repo):
    result = runner.invoke(app, ["run", "/nope/brief.md", "--repo", str(fixture_repo)])
    assert result.exit_code == 2 and "brief" in result.output.lower()


def test_run_duplicate_id_friendly_error(regie_home, fixture_repo, fake_profiles, tmp_path):
    brief = tmp_path / "dup.md"; brief.write_text("x")
    (tmp_path / "tasks.json").write_text("[]")
    _toml(fixture_repo)  # helper writing the minimal regie.toml used by existing tests
    runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo), "--profiles", str(fake_profiles)])
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo), "--profiles", str(fake_profiles)])
    assert result.exit_code == 2 and "already exists" in result.output


def test_run_executes_in_worktree_not_checkout(regie_home, fixture_repo, fake_profiles, tmp_path):
    brief = tmp_path / "wtrun.md"; brief.write_text("x")
    (tmp_path / "tasks.json").write_text(json.dumps([{"id": "T1", "title": "t",
        "profile": "builder", "criteria": ["c"]}]))
    _toml(fixture_repo)
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "blocked", "blocked_question": "?"}}))
    result = runner.invoke(app, ["run", str(brief), "--repo", str(fixture_repo),
                                 "--profiles", str(fake_profiles)])
    state = RunDir.open(regie_home, _last_run_id(regie_home)).read_state()
    assert state.worktree_path and state.worktree_path != str(fixture_repo)
    # the fake agent reads .fake_agent.json from ITS cwd — the worktree:
    # blocked halt proves the dispatch happened in the worktree only if the file
    # exists there; base commit contains no .fake_agent.json, so instead assert
    # the worktree exists and is a git worktree of fixture_repo
    from pathlib import Path
    assert (Path(state.worktree_path) / ".git").exists()


def test_clean_removes_worktree_and_branch(regie_home, fixture_repo, fake_profiles, tmp_path):
    # after the previous run: clean it
    rid = _last_run_id(regie_home)
    result = runner.invoke(app, ["clean", rid, "--repo", str(fixture_repo)])
    assert result.exit_code == 0
    assert not Path(RunDir.open(regie_home, rid).read_state().worktree_path).exists()
```

Note for the implementer: `.fake_agent.json` must be seeded INTO THE WORKTREE for fake runs to act — for CLI-level tests, commit `.fake_agent.json` into the fixture repo before `regie run` (so the worktree checkout contains it) or write it into the worktree between creation and dispatch; simplest is committing it in the test (`commit_all(fixture_repo, "chore: fake script")`) before invoking run. Add tiny helpers `_toml(repo)` and `_last_run_id(home)` at the top of test_cli.py and reuse them in the older tests where duplicated.

- [ ] **Step 2: verify failures. Step 3: implement** (models fields, config key, notify.py, cli rework — run/resume signature keeps `--repo` pointing at the user checkout; stages receive the worktree path from state). Resume must also route to the worktree (`Path(state.worktree_path)`) and recreate it from `base_sha` if missing (crash after clean). **Step 4: full suite** — several existing CLI/e2e tests will need the fixture-repo-commit adjustment for `.fake_agent.json`/queue seeding; update those tests' arrangement only, never their assertions. **Step 5: commit** `feat(cli): run worktrees, live-run guard, friendly errors, clean command, notifications`.

---

### Task 7: Planner stage + approve checkpoint

**Files:**
- Modify: `src/regie/pipeline.py`, `src/regie/cli.py`
- Test: `tests/test_plan_stage.py`

**Interfaces:**
- Produces in pipeline.py:
  - `PLAN_SCHEMA` (module constant): `{"type": "object", "required": ["spec_markdown", "tasks"], "properties": {"spec_markdown": {"type": "string"}, "tasks": {"type": "array"}}}`.
  - `CRITERION_RE = re.compile(r"given.+when.+then", re.I | re.S)`
  - `plan_stage(rundir, run, cfg, worktree) -> None`: renders the planner packet (brief text + conventions + decisions), dispatches the planner profile (attempts tracked in `run.planner_attempts`, ladder applies, quota/blocked halt as in tasks), validates the structured result:
    1. every task dict parses as `TaskSpec` (+ non-empty `planned_tests: list[str]` — extend TaskSpec with `planned_tests: list[str] = []`),
    2. every criterion matches `CRITERION_RE`,
    3. every task's profile exists in cfg.profiles,
    4. DAG acyclic (`ordered_task_ids` on a probe RunState).
    Validation failure = failed attempt (details into the retry packet's Notes via the run-level note `note-plan.md`), ladder → halt. Success: write `spec/spec.md` (the spec_markdown), populate `run.tasks`, set `run.stage = "approve"` (or `"tasks"` when `run.autonomous`), write state.
- CLI:
  - `run` calls `plan_stage` (tasks.json stand-in REMOVED — if a tasks.json exists next to the brief it is still honored for tests/back-compat via `--tasks-file` explicit option only); after plan_stage, if stage == "approve": print spec path + `regie approve <id>` instructions and exit 0 (not an error).
  - New `approve` command: requires stage == "approve", sets stage = "tasks", prints "approved — run `regie resume <id> --repo <path>`". `resume` now dispatches on stage: plan → rerun plan_stage; approve → just print instructions; tasks → run_tasks_stage (existing); finalize/pr stages arrive in Tasks 8-9.

- [ ] **Step 1: failing tests**

```python
# tests/test_plan_stage.py
import json

from regie.models import RunState
from regie.pipeline import plan_stage
from regie.rundir import RunDir

PLAN = {"spec_markdown": "# Spec\n...", "tasks": [
    {"id": "T1", "title": "divide", "profile": "builder",
     "criteria": ["Given 6 and 3, When divide, Then 2"],
     "planned_tests": ["test_divide_exact"], "depends_on": []}]}


def _seed(regie_home, fixture_repo, plan_result):
    rd = RunDir.create(regie_home, "r1")
    (rd.path / "brief.md").write_text("# brief")
    run = RunState(id="r1", target_repo=str(fixture_repo), branch="regie/r1",
                   stage="plan", worktree_path=str(fixture_repo))
    (fixture_repo / ".fake_agent.json").write_text(json.dumps(
        {"result": {"outcome": "done", "structured": plan_result}}))
    return rd, run


def test_plan_stage_success_populates_tasks_and_stops_at_approve(
        regie_home, fixture_repo, cfg):
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    plan_stage(rd, run, cfg, fixture_repo)
    assert run.stage == "approve"
    assert (rd.path / "spec" / "spec.md").read_text().startswith("# Spec")
    assert run.tasks["T1"].spec.planned_tests == ["test_divide_exact"]


def test_plan_stage_autonomous_skips_approve(regie_home, fixture_repo, cfg):
    rd, run = _seed(regie_home, fixture_repo, PLAN)
    run.autonomous = True
    plan_stage(rd, run, cfg, fixture_repo)
    assert run.stage == "tasks"


def test_plan_stage_rejects_non_gwt_criteria_and_retries(regie_home, fixture_repo, cfg):
    bad = {"spec_markdown": "s", "tasks": [{"id": "T1", "title": "t",
           "profile": "builder", "criteria": ["it should work"],
           "planned_tests": ["test_x"]}]}
    rd, run = _seed(regie_home, fixture_repo, bad)
    plan_stage(rd, run, cfg, fixture_repo)   # fake returns same bad plan every attempt
    assert run.stage == "halted" and len(run.planner_attempts) >= 3
    assert "note-plan.md" in [p.name for p in (rd.path / "tasks" / "PLAN").iterdir()]


def test_plan_stage_rejects_unknown_profile(regie_home, fixture_repo, cfg):
    bad = {"spec_markdown": "s", "tasks": [{"id": "T1", "title": "t",
           "profile": "wizard", "criteria": ["Given a When b Then c"],
           "planned_tests": ["test_x"]}]}
    rd, run = _seed(regie_home, fixture_repo, bad)
    plan_stage(rd, run, cfg, fixture_repo)
    assert run.stage == "halted"
```

(Planner attempt artifacts — packet, transcripts, notes — live under the pseudo-task dir `tasks/PLAN/`.) CLI-level approve test: seed a run at stage "approve", invoke `approve`, assert stage == "tasks"; invoke on a non-approve run → exit 2.

- [ ] **Step 2: verify failure. Step 3: implement** (plan_stage mirrors run_task's dispatch/ladder shape but over `run.planner_attempts` with task_id "PLAN"; TaskSpec gains `planned_tests: list[str] = Field(default_factory=list)`). **Step 4: full suite — existing e2e keeps working via `--tasks-file`; update e2e invocations accordingly. Step 5: commit** `feat(pipeline): planner stage with GWT gates and approve checkpoint`.

---

### Task 8: Finalize stage

**Files:**
- Modify: `src/regie/pipeline.py`, `src/regie/gates.py` (export glob matcher), `src/regie/cli.py` (resume routes finalize)
- Test: `tests/test_finalize_pr.py`

**Interfaces:**
- gates.py: expose `match_globs(path: str, globs: list[str]) -> bool` (the Task-7-Plan-A translator, factored for reuse; diff_gate now calls it).
- pipeline.py: `finalize_stage(rundir, run, cfg, worktree) -> None`:
  1. run command gates: `test` (rerun_on_fail=True), `lint`, plus `typecheck` if configured — any failure → halt (reason names the gate; debugger rounds only exist at the PR stage in v1).
  2. eval predicate: `git diff --name-only <base_sha>..HEAD` in worktree; if any path matches `cfg.eval_trigger_globs` and `commands["eval"]` configured → run it as a gate.
  3. rebase: `git fetch origin` + `git rebase origin/<base_branch>`; on GitError → `git rebase --abort` (tolerated if it fails too) → halt "rebase conflict — resolve manually in <worktree> then regie resume".
  4. success → `run.stage = "pr"`, write state.
- Tests fake the command gates via regie.toml commands (`true`/`false`) in a worktree with a remote (reuse `remote_repo`), and prove: green path advances to "pr"; failing eval only runs when trigger matches; rebase conflict halts (create conflicting commit on origin/main between worktree creation and finalize).

- [ ] **Step 1: failing tests**

```python
# tests/test_finalize_pr.py
import subprocess

from regie.gitops import commit_all, create_run_worktree, fetch_base_sha
from regie.models import RunState
from regie.pipeline import finalize_stage
from regie.rundir import RunDir


def _wt_run(regie_home, fixture_repo, remote_repo, tmp_path, commands_extra=""):
    (fixture_repo / "regie.toml").write_text(
        'test_globs = ["tests/**"]\nbinding_strength = ["fake:m1"]\n'
        'eval_trigger_globs = ["src/**"]\n'
        f'[commands]\ntest = "true"\nlint = "true"\n{commands_extra}')
    base = fetch_base_sha(fixture_repo, "main")
    wt = create_run_worktree(fixture_repo, "regie/rf", base, tmp_path / "wtf")
    (wt / "src" / "x.py").write_text("X = 1\n")
    commit_all(wt, "feat(T1): implement")
    rd = RunDir.create(regie_home, "rf")
    run = RunState(id="rf", target_repo=str(fixture_repo), branch="regie/rf",
                   stage="finalize", base_sha=base, worktree_path=str(wt))
    return rd, run, wt


def test_finalize_green_advances_to_pr(regie_home, fixture_repo, remote_repo,
                                       tmp_path, fake_profiles):
    from regie.config import load_config
    rd, run, wt = _wt_run(regie_home, fixture_repo, remote_repo, tmp_path)
    finalize_stage(rd, run, load_config(fixture_repo, fake_profiles), wt)
    assert run.stage == "pr"


def test_finalize_runs_eval_gate_when_triggered(regie_home, fixture_repo,
                                                remote_repo, tmp_path, fake_profiles):
    from regie.config import load_config
    rd, run, wt = _wt_run(regie_home, fixture_repo, remote_repo, tmp_path,
                          commands_extra='eval = "false"')
    finalize_stage(rd, run, load_config(fixture_repo, fake_profiles), wt)
    assert run.stage == "halted" and "eval" in run.halt_reason


def test_finalize_halts_on_rebase_conflict(regie_home, fixture_repo, remote_repo,
                                           tmp_path, fake_profiles):
    from regie.config import load_config
    rd, run, wt = _wt_run(regie_home, fixture_repo, remote_repo, tmp_path)
    # move origin/main with a conflicting change to the same file
    (fixture_repo / "src" / "x.py").write_text("X = 99\n")
    commit_all(fixture_repo, "conflict on main")
    subprocess.run(["git", "-C", str(fixture_repo), "push", "-q", "origin", "main"], check=True)
    finalize_stage(rd, run, load_config(fixture_repo, fake_profiles), wt)
    assert run.stage == "halted" and "rebase" in run.halt_reason.lower()
```

- [ ] **Step 2-5**: verify failure → implement → verify (full suite) → commit `feat(pipeline): finalize stage with eval predicate and rebase halt`.

---

### Task 9: PR stage — squash, scribe, push, CI watch, debugger rounds

**Files:**
- Modify: `src/regie/pipeline.py`, `src/regie/cli.py` (resume routes pr), `src/regie/config.py` test update
- Create: `profiles/debugger.yaml` (`binding: {cli: claude, model: strongest, auth: subscription}`, budgets like builder)
- Test: `tests/test_finalize_pr.py` (extend), `tests/test_config.py` (5 profiles now)

**Interfaces:**
- `pr_stage(rundir, run, cfg, worktree) -> None`:
  1. `groups = run_commit_groups(worktree, run.base_sha)`; scribe = ONE dispatch of the planner profile (label task "SCRIBE", artifacts under `tasks/SCRIBE/`) with the spec + `git log` subjects, `output_schema = {"type":"object","required":["commit_messages","pr_title","pr_body"], ...}` where commit_messages is a list as long as groups; on scribe failure/mismatch fall back deterministically: group default messages, title = first line of spec, body = spec text (scribe is a polish step, never a blocker).
  2. `rebuild_history(worktree, run.base_sha, zip(messages, group_shas), run.id)`.
  3. Append minors: read every `tasks/*/minor-findings.json`; append a "## Review notes (minor)" section to the PR body.
  4. `push_branch(worktree, run.branch)`; `run.pr_url = create_pr(worktree, run.base_branch, title, body_file)`; write state.
  5. CI loop: poll `ci_status(worktree)` every 30s (module constant `CI_POLL_SECONDS = 30`, monkeypatchable) until green (→ `run.stage = "done"`, notify, write state) or red. On red: debugger round — dispatch `debugger` profile (build-stage gates: test/lint/diff_gate) with a packet whose Notes carry the last CI failure context (`gh pr checks` output), then a review dispatch (2c semantics with binding flip vs the debugger's binding); on gates+review green: `commit_all` happened via gates path → `push_branch` again (plain push — appends). Max 2 debugger rounds (module constant), then halt. "pending" keeps polling with a wall-clock cap `CI_WALL_MINUTES = 30` → halt "CI timeout".
- Tests (fake adapter + stub gh, monkeypatch CI_POLL_SECONDS=0): green-CI path → stage done + pr_url set + history squashed (one commit per task) + minors in body file; red-then-green path with one debugger round → done, debugger attempt recorded under the task dir `tasks/DEBUG-1/`; red persisting → halt after 2 rounds. Scribe fallback path: fake returns error → deterministic messages used.

- [ ] **Step 1: failing tests** (three tests mirroring the paths above — construct the run as in Task 8's helper but with two task-commit groups from `_mk_task_commits`, stub `gh` writable per-call so the checks output can change between polls: the stub reads a counter file and emits FAILURE first call, SUCCESS after; assert `git log` count == 2 post-squash and body file contains "Review notes" when a minor-findings.json is seeded).
- [ ] **Step 2-5**: fail → implement → verify full suite (config test updated: profiles set now includes "debugger") → commit `feat(pipeline): pr stage with scribe squash, ci watch, and gated debugger rounds`.

---

### Task 10: Full-pipeline e2e + docs

**Files:**
- Test: `tests/test_e2e_full.py`
- Modify: `docs/USAGE.md`, `README.md` (drop Plan A stand-in notes; document approve/autonomous/clean; real-adapter smoke-test checklist)

**Interfaces:** none new — this is the Plan B exit criterion.

- [ ] **Step 1: Write the e2e test**: brief in, stage through plan (fake planner returns a 2-task PLAN via queue) → approve via CLI → resume → tasks (queue: red-test/build/review per task) → finalize (real pytest commands in the worktree fixture) → pr (stub gh green) → assert: stage "done", pr_url set, remote branch exists with exactly 2 squashed commits, backup ref exists, spec/spec.md present, minors section present when seeded. Second test: `--autonomous` skips approve and runs straight through.
- [ ] **Step 2: Make it pass** (this test integrates everything; expect and fix seam bugs — production fixes require the same discipline as any task: minimal, tested).
- [ ] **Step 3: Docs** — update USAGE.md: remove "Plan A stand-in" notes, document `regie approve`, `--autonomous`, `regie clean`, notifications; add a "First real run (supervised smoke test)" checklist: pick a trivial brief on a sandbox repo, watch `events.jsonl`, verify adapter parsing against real `claude -p`/`codex exec` output, tune the contract fixtures if the real shapes differ. README: status section → "Plan B complete — full pipeline brief→PR (fake-verified); real-adapter smoke test pending".
- [ ] **Step 4: Full suite + ruff. Step 5: commit** `test(e2e): full pipeline brief→PR with approve checkpoint` + `docs: plan B usage`.

## Self-review notes

- Spec coverage vs the carry-over list: adapters (T1/T2), binding-flip (T3), worktree/branch/base (T4/T6), squash+backup+tree-check/push/PR/CI (T5/T9), planner+approve+autonomous (T7), finalize+eval+rebase-halt (T8), debugger rounds (T9), notifications+clean+friendly errors+live-run guard (T6), quota-resume (Plan A fix + adapters' quota outcome). ai-search-platform Make targets are an external prerequisite (tracked in USAGE), not a task here.
- Adapter fixtures define a supported contract; the smoke-test checklist (T10) is the mechanism for reconciling with reality — deliberate, since only live runs can validate vendor output.
- Type consistency: `TaskSpec.planned_tests` added in T7 and used in T7 gates only; `match_globs` factored in T8 and used by diff_gate + eval predicate; `run_commit_groups` (T5) feeds `pr_stage` (T9) with the same `list[tuple[str, list[str]]]` shape.
