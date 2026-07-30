# Per-profile binding lists

Replace the single `binding:` per profile plus the global `binding_strength`
order with an ordered list of bindings per profile, so retry escalation and
quota failover become one mechanism: walk down the profile's own list.

## In scope

- Profile yaml accepts `bindings:` — an ordered list of `{cli, model, auth}`
  entries, first entry = primary, later entries = progressively stronger or
  alternative-provider fallbacks. The old singular `binding:` key must still
  load, treated as a one-element list.
- Ladder semantics generalized: attempts 1 and 2 run on the profile's first
  binding; each subsequent attempt moves one entry down the list; when the
  list is exhausted, halt (same "bounded, then halt" philosophy as today).
- Quota outcome on binding N advances immediately to binding N+1 (this is the
  provider-failover behavior — a quota hit must never burn a retry and must
  never re-dispatch on the exhausted binding). Quota with no next binding
  halts with a reason naming the exhausted provider.
- The global `binding_strength` key in regie.toml is removed; config loading
  and all call sites (ladder, pipeline dispatch, reviewer binding flip) use
  the per-profile lists. The reviewer cross-model flip keeps comparing the
  ACTUAL binding used by the builder's last attempt.
- All profile yaml files in profiles/ migrated to the list form.
- Tests updated/added: list walking, quota skip-ahead, exhaustion halt,
  singular-key backward compatibility, config error when a profile has an
  empty bindings list.

## Out of scope

- Capability matrices or telemetry-driven generation of the lists (roadmap).
- Any change to how adapters work or which CLIs are installed.
- Cross-profile fallback (a profile only ever uses its own list).
- Changes to budgets, gates, or stage semantics.

## Acceptance signals

- A profile with `bindings: [claude:sonnet, claude:opus]` retries twice on
  sonnet, escalates to opus for attempt 3, halts after attempt 3 fails.
- A quota outcome on attempt 1 (sonnet) dispatches attempt 2 on opus, with
  the quota attempt recorded but not counted against retry budget.
- A profile whose yaml still says `binding: {cli: claude, model: sonnet}`
  behaves exactly like `bindings:` with one entry (two retries, then halt).
- `regie.toml` without `binding_strength` loads fine; a stale
  `binding_strength` key is ignored (no error), so existing repos don't break.
- Full existing test suite still passes (adapted where it referenced
  binding_strength).
