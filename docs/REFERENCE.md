# agent-steward — Reference

Everything the README summarizes, in full. Each section: what it does, the
command, the design rule that makes it trustworthy.

## Command cheatsheet

| Command | What it does |
|---|---|
| `steward` | Where am I, what should I do next |
| `steward init [--out F]` | Print the manifest-authoring rubric; write a skeleton |
| `steward baseline --manifest F` | First check; seeds the diff state |
| `steward check --manifest F [--diff]` | Run all probes + metrics; `--diff` reports only changes |
| `steward install-hook --manifest F` | Auto-run check + refresh cumulative report after every agent session (two Claude Code Stop hooks) |
| `steward report` | Cumulative view; opens with **What needs you** |
| `steward route --manifest F [--judge]` | Sort leftover violations into an attention queue |
| `steward approve <id> --verdict worth\|not-worth` | Grade a queue item (feeds M4) |
| `steward ingest-usage` | Measured spend from Claude Code transcripts, zero-manual |
| `steward log-task ...` | Manual ledger entry (quality verdicts, canary pairs) |
| `steward stamp F --produced-by M --task T` | Provenance stamp into a file's frontmatter |
| `steward canary --task T` | Should this run also shadow-run one tier lower? |
| `steward allocate rubric\|init\|tune` | Tier table: print rubric → generate → tune from evidence |
| `steward distill [--path F] [--queue]` | Cluster recurring adjudication reasons into rule candidates |

## Verification (the quality gate)

**14 probe types**, all deterministic and read-only: `cmd`, `jsonl_wellformed`,
`frontmatter_required`, `single_source_cap`, `field_value_rule`, `bash_syntax`,
`csv_required_columns`, `tsv_wellformed`, `file_exists`, `filename_pattern`,
`staleness_flag`, `ref_integrity`, `scope_guard`, `allocation_compliance`.

Design rules that keep them honest:

- **Observe first.** New probes default to `severity: warn`; you promote to
  `fail` only after measuring the false-positive rate.
- **Provenance.** Every probe carries `source:` naming the doc + section it
  enforces — a violation always points back to *your* rule.
- **Anti-transcription guard.** Probes that copy numbers out of a rule doc pin
  `source_file:` + verbatim `source_quote:`; the engine verifies the quotes
  still exist. Doc changes surface as one SOURCE DRIFT problem instead of a
  false-positive storm. (Born from a real incident: one cap mistyped 0.6 vs
  the doc's 0.9 = 241 false positives.)
- **Rule coverage (M5).** Inventory every written rule in `rulebook:` — each
  either `covered_by:` probes, explicitly `judgment_only`, or visibly
  uncovered (your next probe candidate).
- **Documented exceptions.** `single_source_cap` accepts `exempt_field:`
  (e.g. `g1_exempt`) — a file carrying that field with a written reason is
  skipped and counted. A reviewable exception beats a rule everyone ignores.
- **The over-delivery guard.** Agents that "just do stuff" create files
  nobody asked for, and checks that only ask "is the required output
  present?" never notice. `scope_guard` flags any file outside your
  declared `expected` areas; with `--diff`, only newly appearing strays
  reach you.
- **Missing tools are not violations.** A `cmd` probe whose binary is absent
  reports "missing tool — install it or fix the manifest", not a violation.
- **Manifest pre-flight.** Every check validates the manifest first: unknown
  probe types and wrong parameter names get a did-you-mean, bad regexes are
  caught at read time. Warnings only — fail-open holds.
- **Fixes scoreboard.** `check --diff` appends resolved violations to
  `fixes.jsonl`; the report shows "Fixed so far" with counts and examples.

**The hook.** `install-hook` registers two Claude Code Stop hooks: (1)
`check --diff --exit-new` — no new violations → silence; new violations →
exit 2 with the list on stderr, which Claude Code feeds back to the agent for
self-repair; (2) `report --out <state-dir>/REPORT.md` — refreshes the
cumulative report (CPAU / savings / what-needs-you) at a fixed path after
every session, so you open one file for the latest. Projects installed before
0.20 get the report hook added on the next `install-hook` run (the check hook
is left untouched).

## Metering (measured, not estimated)

**`ingest-usage`** reads Claude Code's own transcripts (main session + one
file per spawned worker) and appends measured entries: model, input/output/
cache tokens, timestamps.

- Task attribution: `[task=extract]` markers in dispatch prompts win, then
  `--task-map 'task_id=regex'`, then `_unattributed` (still metered by tier).
- Incremental (byte cursor), idempotent, fail-open — the transcript format is
  Claude Code internal; anything unreadable is skipped, and `log-task` always
  works as the manual fallback.
- **Measurement ≠ judgment.** Ingested entries (`via: transcript`) count in
  money views (savings, CPAU, tier tables); quality loops (tune, canary)
  only trust explicit verdicts from `log-task --result ...`.

**`log-task`** stays as the quality channel: `--result pass|fail|escalated`,
canary pairs (`--canary primary|shadow --pair <id> --quality same|worse|better`),
and anything transcripts can't know.

## Allocation (the spend audit)

**Zero-manual cold start:** `allocate rubric` prints a published assessment
rubric → the agent in your project rates every task class on four axes
(verifiable / judgment / blast_radius / volume) → `allocate init` maps ratings
to tiers with a **deterministic, versioned matrix**. The LLM supplies evidence
and rationale; the matrix decides. Floors bind blast radius: tuning can never
demote below a floor.

**Loop one — escalation (catches "allocated too low").** Cheap-tier failures
get redone one tier up and logged; per-task escalation rates drive `tune`
proposals. Bounded cost: one cheap redo each.

**Loop two — canary (catches "allocated too high", which is silent).**
`steward canary --task X` before dispatch: deterministic sampling (counted off
the ledger, replayable) says whether to also shadow-run one tier lower — never
below the floor. You judge the shadow against the primary; enough judged runs
at quality parity → `tune` proposes the demotion with direct evidence; a
measured quality gap **vetoes** demotion even when escalation looks clean.
*Cheapest at the same quality, never cheapest at any quality.*

**`tune`** proposes, you apply: `--apply --only <task>` acts on one proposal
and leaves the rest pending. Applied changes land in the allocation history
with a measured before/after in the report. Compliance is transition-aware: a
stamp is judged against the table of *its* day OR today's — only matching
neither is a violation.

**Report money sections:** savings vs everything-on-top, per-tier
"Where the money goes" table, per-task **CPAU** (all-run cost ÷ accepted
runs — waste is part of the price), escalation matrix, canary quality table.

## Attention (route what's left)

`route` sorts unresolved violations by severity × your manifest-declared
`risk_weight`. `--judge` adds a cheap-model judgment-worthiness score per item
— **no API key needed**: it runs through the `claude` CLI login you already
pay for (set `ANTHROPIC_API_KEY` only for CI; either way your own credential,
hard-whitelisted, fail-open to the deterministic order).

You grade the queue as you work it (`approve`). Those verdicts are **M4,
residue precision** — the tool measuring its own sorting; rules whose items
you keep judging not-worth get named for re-parameterization. **M1** (top-tier
tokens per accepted top-tier run) tracks the price of your attention.
Probes with `route: false` (e.g. lint that the worker self-repair loop
handles) never enter the human queue at all.

## Verdict memory

`distill` clusters recurring adjudication reasons — from any jsonl log
(`--path state/admission_log.jsonl --where verdict=reject`) or from your own
queue verdicts (`--queue`). Three same-shaped rejections are one rule waiting
to be written; `--emit-rubric` renders candidates as a ready-to-paste judge
rubric block. Candidates only: a human writes the rule; the steward never
edits your manifest.

## Trust contract (structural, non-negotiable)

1. Read-only on the target; writes only under its own state dir.
2. Zero network by default; the judge is the one exception, on your own
   credential, whitelisted, fail-open.
3. Fail-open everywhere — a watchdog must never take down what it watches.
4. Violations ≠ verdicts — it flags, sorts, proposes; it never decides.
