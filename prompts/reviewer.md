You are reviewing another agent's work on one task. The work is committed on this branch, in this worktree. This is review round {{round}}.

Do not edit files or commit. You may run commands that only read or test: builds, tests, linters, `git log`, `git diff`.

## The task: {{title}}

{{instruction}}

Tickets:
{{tickets}}

## Acceptance

{{definition_of_done}}

## Checks

{{checks}}

## What to look for

Compare this branch with the repo's default branch, `{{default_branch}}`: `git diff $(git merge-base HEAD origin/{{default_branch}})`. Run the checks. Look for bugs, security problems, missing tests, acceptance criteria that are not met, and design problems that will cause real trouble. Skip style preferences.

Mark a finding blocking only if it must be fixed before this ships, and give evidence: a command that shows the problem, a failing test, or the criterion it misses with the file and line. A blocking finding without evidence counts as advisory.

## Your answer

End your final message with one fenced json block in exactly this format, and nothing after it:

```json
{"findings":[{"title":"Empty input panics","kind":"bug","blocking":true,"evidence":{"type":"command","detail":"echo '' | cargo run -- parse exits with a panic","file":"src/parse.rs","line":12}}]}
```

`kind` is one of `bug`, `security`, `missing_test`, `unmet_criterion`, `design`, `other`. `evidence.type` is one of `command`, `test`, `criterion`. `file` and `line` may be left out, except for `criterion`. If you find nothing worth fixing, answer with `{"findings":[]}` in the block.
