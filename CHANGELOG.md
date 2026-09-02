# Changelog

All notable changes to agent-steward. Version numbers follow semver-ish
pragmatism: minor bumps for features, patch bumps for docs/fixes.

## 0.27.1 — 2026-09-01
- Fix (T-20260901-123): **`log-task` guard 2 (tier/model SSoT) silently
  no-op'd when run one directory below repo root.** `apath = args.allocation
  or ".allocation.yaml"` was a bare cwd-relative string — `find_state_dir()`
  already had to walk up for `.steward/` for the exact same reason (see its
  docstring), but `.allocation.yaml` never got the same treatment. A
  contradictory `--tier`/`--model` pair run from a subdirectory landed in the
  ledger unrejected instead of failing with `REJECTED` (7 real rows written
  this way after the guard shipped in 0.27.0). Added `find_allocation_file()`
  mirroring `find_state_dir`'s walk-up (nearest existing file, stop at `.git`
  boundary or `$HOME`) and wired it into guard 2 only — guard 1 (dedup) and
  the other three `.allocation.yaml` lookups (canary/tune/etc.) are untouched,
  out of scope for this fix.

## 0.27.0 — 2026-08-24
- Fix (T-20260824-91, E-21 下沉條款②): **`log-task` had zero write-time
  guards.** A worker re-running `log-task` for a card already logged by its
  dispatcher (E-21, 2026-08-15) produced a second, permanent row in an
  append-only ledger — nothing checked whether that `(task, note)` pair had
  already been written. Added a write-time dedup guard: a repeat of the same
  `(task, note)` is now a no-op (exit 0, "already logged ... skipping
  duplicate"), not a second line. Rows logged without `--note` are unaffected
  (no key to dedup on, same as before).
- Fix: **tier/model contradictions were warn-only.** `--tier top --model
  claude-sonnet-5` used to append the row anyway with a warning on stderr —
  "entry kept (append-only)". `.allocation.yaml` `tier_patterns` is now the
  single SSoT for which tier a model belongs to: a declared tier that
  contradicts it is REJECTED before anything is written (exit 1, message
  names the correct tier). Models that match no tier pattern at all (new
  model name, typo) stay warn-only — rejecting those would block every
  legitimate new model on day one.
- Both guards run before the row is appended, never after — the ledger is
  append-only and a bad row can't be taken back, so write-time is the only
  safe place to stop it.

## 0.26.0 — 2026-08-24
- Fix: **main's test suite had been red since 0.25.0 (2026-08-21) without
  anyone noticing.** `tests/test_steward.py` already carried tests for a
  real-money pricing / tuning-honesty feature (`patterns_at`, `price_tier`,
  `money`, `tuned_tasks`, `tier_patterns_history`) committed at 8ee6cb4, but
  the implementation in `src/agent_steward/allocate.py` was never `git add`ed
  — it sat as an uncommitted local edit on this machine only (last touched
  2026-08-21, same day as the 0.25.0 commit, whose message claims "pytest 90
  passed" — almost certainly run against this same uncommitted tree). A
  fresh clone or CI checkout would have shown 10 failing tests since that
  commit; nothing surfaced it because the only machine that runs this repo's
  tests still had the uncommitted fix sitting in the working tree. Found
  and recovered while working T-20260824-91 (unrelated task, same
  `usage_ledger.jsonl` write path). Committing the implementation as-is
  (already fully tested, 93 passed) rather than leaving it stashed.
- Feature carried by this recovery: `cost_unit: usd_per_mtok` prints real
  dollars instead of a unitless index; cost is priced from the model actually
  recorded rather than the declared tier when the two disagree; the
  tuning-effect comparison only counts tasks tuning actually touched (a
  tier restructure used to swing the reported effect by 60+ points with no
  dispatch changing); `tier_patterns_history` lets `tier_patterns` be
  restructured without turning yesterday's correct entries into today's
  "mis-logged" warnings.

## 0.24.0 — 2026-08-21
- Fix: **`state.json`'s read-diff-write had no lock.** Every `steward check
  --diff` process — one per session's Stop hook, and this project routinely
  runs several sessions concurrently — reads `state.json`, diffs against it,
  and writes a new snapshot back, unguarded. Two processes racing that
  window let the later writer silently rewind the earlier writer's snapshot,
  so the next run diffs against a stale `violations` map and re-reports an
  already-seen, fingerprint-matched violation as new. Added `locked_state()`
  (an `fcntl.flock` exclusive lock scoped to the read-diff-write only, not
  the probe run itself) around the one call site in `run()`.
- Note for anyone chasing a similar false-new report: check the *deployed*
  copy first. This exact fix sat correct and tested in source for two hours
  before it deployed, because `steward` on this box runs from a site-packages
  install (non-editable) that only picks up source changes on `pip install
  --user .` — see T-20260821-54's return note and the earlier T-20260814-120
  precedent it names. If the deployed `cli.py` doesn't `grep -c locked_state`
  greater than zero, the fix hasn't shipped yet regardless of what source
  says.

## 0.22.0 — 2026-08-04
- Fix: **a clean probe and a probe that never ran looked identical in
  `state.json`.** `violations` only ever got a key for a probe when it found
  something, so any downstream script reading `violations['<probe-id>']` to
  ask "is this clean?" got a `KeyError` instead of `[]` the moment a rule got
  fixed to zero findings — the better the fix, the sooner it broke. Found via
  a real case: a scope-guard manifest fix drove violations to 0, and the
  acceptance check that read `violations['scope-guard']` blew up on the exact
  run that proved the fix worked.
- Added an opt-in manifest field, `always_report: true`, settable per probe.
  A probe that declares it keeps its key in `violations` even at 0 findings
  (as `[]`), so callers can distinguish "checked, nothing wrong" from "not
  configured / didn't run". Probes that don't opt in keep the old behavior
  unchanged — nothing downstream that already depends on "only list problems"
  breaks. `always_report` is also now a recognized common probe key, so
  `validate_manifest` doesn't flag it as unknown.
- Lesson for the flywheel: don't fix this one script's `KeyError` — the same
  shape of bug reappears for every future probe someone reads by key, unless
  the engine itself can say "ran clean" instead of just "ran".

## 0.21.0 — 2026-07-30
- Fix: **CI was red on every run since 2026-07-23** and it was never the code.
  Both workflows `pip install ruff` unpinned; ruff 0.16.0 (released 2026-07-23)
  widened its default rule set, so `ruff check src/ tests/` started reporting
  75 findings in files that had not changed. `pytest` was green throughout
  (63 passed on 3.9 and 3.12) — the gate, not the engine, had drifted.
- The lint contract now lives in `pyproject.toml` (`[tool.ruff.lint] select`)
  instead of being inherited from whatever ruff version the runner installs
  that day. Same rules the repo was written against (`E4`, `E7`, `E9`, `F`),
  now reproducible across ruff versions; widening them is a deliberate commit.
- Why not just adopt the new defaults: most of them contradict the trust
  contract on purpose. `BLE001` (blind except), `PLW1510` (subprocess without
  `check`) and `S112` are *how* "fail-open everywhere" is implemented — the
  engine must never raise at a target — and `DTZ*` would force tz-aware
  timestamps into a ledger whose history is naive-local. A gate that fights
  the design is a broken gate.
- Lesson for the flywheel: an unpinned tool in a gate is an unversioned
  dependency on someone else's release schedule.
- Fix: **state-dir discovery — a ledger fork was a silent loss of history.**
  Every cwd-relative `.steward` lookup now walks up to the nearest existing
  state dir (stopping at a `.git` boundary or `$HOME`) instead of creating a
  fresh one wherever it happened to be invoked. Found by dogfooding: this
  repo had grown a second ledger at `src/.steward/usage_ledger.jsonl` — two
  entries, ~180k tokens — because a `log-task` ran one directory too deep.
  Nothing errored; the spend just left the books, which is the one failure
  mode an append-only ledger cannot self-correct. A sub-project with its own
  `.steward/` still keeps its own books (nearest wins), and a nested repo can
  no longer write into its parent's. `--state-dir` still wins outright.
- Fix: **`scope_guard`'s built-in ignore list was root-anchored**, so it only
  ever protected a *top-level* `node_modules/` or `__pycache__/`. Any project
  with a sub-app (`pwa/node_modules/…`) got one "an agent created a file
  nobody asked for" finding per vendored README — 140 of 241 open findings in
  a real target, all of them npm's doing, none an agent's. Defaults are now
  depth-agnostic (`**/node_modules/**`, plus `.venv`/`site-packages`) and the
  walk no longer descends into them at all. A manifest-supplied `ignore:` is
  still taken literally — only the built-in defaults were broadened. An
  attention queue that cries wolf 140 times is not a queue.
- Fix: a *negative* tuning effect no longer prints as "saved -297,576,026
  (-47.0%)". Tuning is allowed to cost more — promotions buy quality — but
  the line now says so: "costs 297,576,026 MORE (47.0%) — tuning bought
  quality, not spend".

### Real money (air/ACE dogfood)

- **`cost_unit: usd_per_mtok`** — `cost_weights` can now be declared as US$ per
  million tokens and the report prints real currency: `$872.68` instead of a
  `931,057,246` index. Default is unchanged (unitless index), so existing
  allocations read exactly as before. ACE plans to sell per-tenant cost
  reporting; an index is not sellable.
- **Cost follows the model, not the declaration.** An entry that records a
  `model` is priced at the tier that model matches, not the tier the
  dispatcher declared. In air's real ledger that is 849 of 1105 metered
  entries — the old math priced 840 opus runs at the sonnet-shared `mid`
  weight and understated them 2.5x.
- **`tier_patterns_history`** — restructuring a tier table no longer rewrites
  the past. Splitting opus out of `mid` into its own `high` tier would have
  turned 849 of 1111 historical entries into "mis-logged" warnings, every one
  of them correct under the table of its own day; mismatch checks now replay
  the patterns in force at each entry's timestamp (the same principle
  `_tier_at` already applied to provenance stamps). Pricing deliberately does
  the opposite and always uses today's table — a compliance judgment belongs
  to its day, a price is a fact about the world.
- **Tuning effect counts only tasks tuning actually moved.** Tasks with no
  history contributed a guaranteed-zero delta *and* dragged the baseline,
  because the cold-start reconstruction falls back to today's tier. Result:
  air's 3→4 tier restructure swung the reported tuning effect from -15.6% to
  +18.8% with not one dispatch changed. A number that moves when nothing
  happened is not a measurement. The line now names its scope
  ("across N tuned task(s)").

### Behaviour changes (no config edit required — read these)

Both are default changes, which §2.A of the flywheel release checklist says to
treat as breaking. Neither needs a migration step; both make a previously
silent failure stop happening.

1. State-dir lookup walks up (see above). If you *relied* on `log-task`
   creating a fresh ledger in whatever directory you were standing in, pass
   `--state-dir` explicitly.
2. `scope_guard`'s built-in ignore list is depth-agnostic (see above). If you
   genuinely want vendored trees audited, set an explicit `ignore:` — a
   manifest-supplied list is still taken literally.

Rollback: `pip install agent-steward==0.20.0`, or yank the release on PyPI.
Nothing in this version writes a new on-disk format, so downgrading is safe.

## 0.20.0 — 2026-07-09
- `install-hook` now installs **two** Stop hooks, not one: the existing
  `check` (violations → self-repair) plus a new `report` hook that refreshes
  a stable cumulative report at `<state-dir>/REPORT.md` after every session —
  CPAU / savings / what-needs-you / pending tune proposals, at a fixed path,
  with zero per-project wiring. Open one file to see the latest.
  - Backward-compatible upgrade path: projects installed before 0.20 keep
    their check hook and get the report hook **added** on the next
    `install-hook` run (the check hook is never touched or duplicated).
  - `|| true` on the report hook: a report failure can never block the
    check hook's self-repair loop.
  - Written inside the already-gitignored state dir → fresh each run, no
    working-tree churn.
  - Born from the air (ai-industry-research) flywheel: the auto-report was
    first wired by hand at the *project* level (a Stop hook writing
    `dev/STEWARD_REPORT.md`); generalised here to the *tool* so every
    project that runs `install-hook` gets it. Shipped through the new
    product-flywheel `S4_RELEASE_CHECKLIST.md`.

## 0.19.1 — 2026-07-08
- Fix: `**` in every probe glob now means "zero or more directories" —
  `records/**/*.md` matches `records/a.md`. Raw fnmatch's behavior silently
  skipped depth-1 files, the most likely first-run confusion for new users.

## 0.19.0 — 2026-07-08
- `scope_guard` (14th probe): the over-delivery guard. Born from Mollick's
  GPT-5 field test (models proactively produce unrequested artifacts) —
  files outside your declared `expected` areas get flagged; with `--diff`
  only new strays reach you. Proper `**` glob semantics (zero-or-more dirs).

## 0.18.1 — 2026-07-08
- The canary gets the job: official mark (assets/icon.svg) — amber canary
  in the steward's teal ring. Teal does the checking; amber is the one
  thing that needs you.

## 0.17.2 — 2026-07-08
- README rewritten reader-first: one-prompt setup, decision table
  (when/where/what/default), levels L1–L3 with per-level report shapes.
- Rule conflicts lead the authorize-fixes table (always human).
- examples/ reduced to two clean generic manifests; live project manifests
  moved into their own projects.

## 0.17.1 — 2026-07-08
- README report samples as real tables; depth moved to docs/REFERENCE.md.
- Tokenless releases via PyPI Trusted Publishing (tag push = release).
- Fix: run artifacts land under the state dir, never inside site-packages.

## 0.17.0 — 2026-07-08
- Report: "Rule check" summary + authorize-fixes-per-category table
  (probes carry `fix:` and `fixable_by:`); escalation reasons in plain
  language.
- Scope ladder documented (L1 solo sessions / L2 repeating jobs / L3 fleets).

## 0.16.0 — 2026-07-07
- `ingest-usage`: zero-manual spend metering from Claude Code transcripts
  (main session + every worker), incremental, fail-open. Measurement ≠
  judgment: ingested entries feed money views only.

## 0.15.0 — 2026-07-07
- Bare `steward` = status + next step; manifest pre-flight validator with
  did-you-mean; CI; packaging verified end-to-end.

## 0.14.0 — 2026-07-07
- Transition-aware allocation compliance (a stamp matches the table of its
  day OR today's — only matching neither violates).
- `route: false` keeps machine-loop findings out of the human queue.

## 0.13.x — 2026-07-07
- L1 judge runs on your existing `claude` login — no API key required.
- `single_source_cap` gains `class_field` (caps keyed off any field).

## 0.12.0 and earlier — 2026-07-05 → 07-07
- Core build-out: 13 deterministic probe types, rule provenance +
  source-quote anti-transcription guard, rulebook coverage (M5), diff
  state + Stop-hook self-repair loop, usage ledger, zero-manual allocation
  (rubric → deterministic matrix → tune), canary shadow runs, attention
  queue + judge + M4, verdict-memory distiller, fixes scoreboard.
