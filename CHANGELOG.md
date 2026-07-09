# Changelog

All notable changes to agent-steward. Version numbers follow semver-ish
pragmatism: minor bumps for features, patch bumps for docs/fixes.

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
