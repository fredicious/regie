# Per-profile binding lists

Replace the single `binding:` per profile and the global `binding_strength` order with an ordered `bindings:` list per profile, so retry escalation and quota failover are one mechanism: walk down the profile's own list.

## Current behaviour (as read from the code)

- `config.Profile` holds one `binding: Binding`; `RegieConfig.binding_strength: list[str]` is a required `regie.toml` key of `"cli:model"` strings.
- `ladder.next_action(attempts, binding, strength_order)` returns `retry` for <2 attempts, `escalate` at exactly 2 (by string-index lookup into the global order), `halt` otherwise, and `halt` immediately if any attempt has outcome `quota`.
- `pipeline._dispatch` / `_should_halt` / `plan_stage` pass `cfg.binding_strength`; `pipeline._review_binding` and `_debug_review_binding` compare the reviewer's `cli` against the builder's last actual attempt binding and fall back to the builder's profile binding on collision.

## Ladder semantics (new)

For a profile with bindings `B[0..L-1]`, given the attempts already recorded for a stage, let `f` = number of attempts with outcome other than `quota` and `q` = number of attempts with outcome `quota`:

```
index = max(0, f - 1) + q
```

- `index >= L` → `halt`
- `index` equals the index the previous attempt ran at → `retry`
- otherwise → `escalate`, dispatching on `B[index]`

This reproduces today's shape for `L == 2` (attempts 1 and 2 on `B[0]`, attempt 3 on `B[1]`, then halt) and makes a quota outcome advance one entry without consuming a retry.

## Acceptance criteria

**AC1 — bindings list loads in order.** Given a profile yaml `bindings: [{cli: claude, model: sonnet}, {cli: claude, model: opus}]`, When `load_config` runs, Then `cfg.profiles[name].bindings` is a two-element list in that order and `cfg.profiles[name].primary` is the sonnet binding.

**AC2 — singular key backward compatibility.** Given a profile yaml with `binding: {cli: claude, model: sonnet}` and no `bindings:` key, When `load_config` runs, Then `cfg.profiles[name].bindings == [Binding(cli="claude", model="sonnet")]`.

**AC3 — empty list is a config error.** Given a profile yaml with `bindings: []`, When `load_config` runs, Then `ConfigError` is raised and its message names the profile and the empty bindings list.

**AC4 — missing binding keys is a config error.** Given a profile yaml with neither `binding:` nor `bindings:`, When `load_config` runs, Then `ConfigError` is raised naming the profile.

**AC5 — `binding_strength` is gone and stale keys are tolerated.** Given a `regie.toml` with no `binding_strength` key, When `load_config` runs, Then it succeeds and `RegieConfig` has no `binding_strength` attribute; and given a `regie.toml` that still contains `binding_strength = [...]`, When `load_config` runs, Then it succeeds and the key is ignored (no error, no warning-as-error).

**AC6 — shipped profiles are migrated.** Given this repo's `profiles/` directory, When `load_config` runs against it, Then every profile loads and `builder`, `test-writer` and `debugger` each expose `bindings == [claude:sonnet, claude:opus]` while `planner` and `reviewer` expose a one-element `[claude:opus]`.

**AC7 — two retries on the primary binding.** Given a stage with 1 recorded failed attempt on `B[0]` and a profile list of length ≥1, When `next_action` is called, Then it returns `("retry", B[0])`.

**AC8 — attempt 3 moves one entry down.** Given 2 recorded failed attempts on `B[0]` and a list `[B0, B1]`, When `next_action` is called, Then it returns `("escalate", B1)`.

**AC9 — one entry per subsequent attempt.** Given a list `[B0, B1, B2]` and 3 recorded failed attempts, When `next_action` is called, Then it returns `("escalate", B2)`.

**AC10 — exhaustion halts.** Given a list `[B0, B1]` and 3 recorded failed attempts, When `next_action` is called, Then it returns `halt`; and given a one-element list `[B0]` and 2 recorded failed attempts, When `next_action` is called, Then it returns `halt`.

**AC11 — escalation dispatches on the profile's second binding.** Given a builder profile with `bindings: [fake:m1, fake:m2]` and a build stage whose first two attempts failed, When the pipeline dispatches the third attempt, Then the recorded `Attempt.binding` is `fake:m2`.

**AC12 — quota advances immediately without burning a retry.** Given a build stage whose attempt 1 returned outcome `quota` on `B[0]` of `[B0, B1]`, When the pipeline continues, Then attempt 2 is dispatched on `B1`, the quota attempt remains recorded in `task.attempts["build"]` with outcome `quota`, and `B1` still gets two attempts before the ladder halts.

**AC13 — quota with no next binding halts naming the provider.** Given a profile with a one-element list `[fake:m1]` and a stage whose attempt returned outcome `quota`, When the pipeline continues, Then the run halts with `run.stage == "halted"` and `run.halt_reason` containing `quota`, `fake:m1`, and the stage name; and no further dispatch occurs on `fake:m1`.

**AC14 — planner stage follows the same rule.** Given a planner profile with `bindings: [fake:m1, fake:m2]` and a plan attempt that returned `quota`, When `plan_stage` continues, Then the next planner attempt is dispatched on `fake:m2`; and given a one-element planner list, Then the run halts with a reason naming the exhausted binding.

**AC15 — reviewer flip still compares the actual binding used.** Given a build stage whose last attempt actually ran on `claude:opus` and a reviewer profile whose primary is also `claude`, When the review stage dispatches, Then the binding is the builder profile's primary and subsequent review-stage ladder steps walk the builder profile's `bindings` list; and given a last build attempt on `codex:*`, Then the reviewer's own primary and list are used.

**AC16 — suite stays green.** Given the full existing test suite, When `uv run pytest -q` and `uvx ruff check src tests` run, Then both pass with every `binding_strength` reference removed or adapted.

## Non-goals (forbidden scope)

- Capability matrices, telemetry, or any automatic generation/ordering of binding lists.
- Any change to adapters (`agents/claude.py`, `agents/codex.py`, `agents/fake.py`) or to which CLIs are installed.
- Cross-profile fallback: a profile only ever walks its own list (the review-stage flip is the one existing, deliberate exception and its semantics are unchanged).
- Changes to budgets, gates, stage sequencing, the bad-test escape hatch, or PR/finalize behaviour.
- Adding a ladder to the debugger rounds or the scribe dispatch — they stay single-shot on the profile's primary binding, and quota there stays terminal.
- Migrating `RunState` JSON already on disk: recorded `Attempt.binding` values are read as-is and never looked up in a list.

## Risks

- **Signature churn**: `next_action` is called from four places (`_dispatch`, `_should_halt`, `plan_stage` twice). A partial migration leaves the suite red; the ladder change and its call sites must land together.
- **Halt-reason contract**: `_should_halt` currently emits `"{stage} ladder exhausted on {task_id}"`. Quota exhaustion needs a distinct, provider-naming reason; tests and `regie status` output read `halt_reason` as free text, so only the new-string assertions are affected.
- **Quota is no longer terminal in `run_task`/`plan_stage`.** If the loop's quota path is changed to `continue` without `_should_halt` covering the exhausted case, the run spins re-dispatching on a dead provider. The exhaustion check must run before any dispatch on every iteration.
- **`Profile.binding` removal** is a breaking field rename for anything constructing `Profile(...)` — `tests/test_binding_flip.py` and `pipeline._debugger_profile` both do.
- **Backwards compat of `regie.toml`**: dropping a required key is safe; ignoring a stale one must be genuinely silent, since existing target repos still carry it.

## Decisions

- **D1 — position by attempt count, not list lookup.** The ladder derives its index from `max(0, non_quota_attempts - 1) + quota_attempts` rather than locating the current binding in the list. This removes the "binding not in order → halt" branch entirely, so a stale or flipped binding recorded in `RunState` can never wedge the ladder.
- **D2 — `Profile.bindings` is the field; `Profile.primary` is a read-only property returning `bindings[0]`.** The old `Profile.binding` attribute is removed rather than aliased, so no call site silently keeps single-binding semantics.
- **D3 — review-stage ladder walks the list of the profile that supplied the binding.** When the cross-model flip selects the builder's binding, subsequent review attempts escalate down the *builder's* list; otherwise down the reviewer's. The flip decision itself is unchanged and still keys off the builder's last actual attempt binding.
- **D4 — quota exhaustion gets its own halt reason**, formatted `quota exhausted on {cli}:{model} during {stage}`, distinct from `{stage} ladder exhausted on {task_id}`.
- **D5 — `binding_strength` is dropped from `RegieConfig` entirely** (not deprecated-but-parsed), and removed from `regie.toml` and `docs/USAGE.md`; unknown top-level `regie.toml` keys are already ignored by `load_config`, which is what makes a stale key harmless.
