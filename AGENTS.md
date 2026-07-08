# AGENTS.md — instructions for AI agents working with agent-steward

You are an agent asked to set up, operate, or integrate agent-steward in a
target project. This file is your operating manual.

## Setting it up in a target project

1. Install: `pipx install agent-steward` (or `python3 -m pip install --user agent-steward`).
2. `steward init --out steward.yaml` prints an authoring rubric and writes a
   skeleton. **Read the rubric fully**, then fill the skeleton from the
   project's OWN written rules (CLAUDE.md, docs/, schema files). Never invent
   rules the project didn't write down.
3. Discipline for every probe you author:
   - `severity: warn` (observe first; the human promotes to fail later)
   - `source:` — the doc + section this rule comes from
   - `fix:` — how a violation of this category gets fixed, in plain words
   - `fixable_by:` — `agent` | `script` | `human`
   - If you copy numbers/enums out of a rule doc, pin `source_file:` +
     verbatim `source_quote:` lines (the engine verifies them — this catches
     your own transcription errors).
4. Inventory EVERY written rule in `rulebook:` — each entry `covered_by:`
   probe ids, or `judgment_only: true`, or left visibly uncovered.
5. Show the human the draft. The only decisions that are theirs: `severity`
   promotions and (if allocation is drafted) `floor:` lines.
6. After approval: `steward baseline --manifest steward.yaml`, then
   `steward install-hook --manifest steward.yaml`, then `steward ingest-usage`.

## Operating rules (non-negotiable)

- **You fix only authorized categories.** The report's "authorize fixes per
  row" table is the contract: the human names which rows you may fix; the
  `fix:` note on each rule tells you how. Never touch `human` rows.
- **Never edit the usage ledger or provenance stamps** — they are history.
  A mis-logged entry is corrected by the next entry, not by rewriting.
- **Never run `steward allocate tune --apply` without explicit human
  approval** (propose is always safe).
- When the Stop hook exits 2, the stderr lists NEW violations — fix those,
  they are yours by default (that is the self-repair loop).

## Fleet dispatch protocol (Level 3)

If you dispatch worker agents:
1. Tag every worker prompt with `[task=<task_id>]` (ids from `.allocation.yaml`).
2. Before dispatching a canary-enabled task: `steward canary --task <id>`;
   exit 0 = also dispatch one shadow worker one tier lower on the same input
   (shadow output goes to scratch, never into the corpus).
3. When a worker's file lands: `steward stamp <file> --produced-by <model> --task <id>`
   — use the model id YOU dispatched with, never the worker's self-report.
4. At collection, judge shadow vs primary:
   `steward log-task --task <id> --tier <t> --canary shadow --pair <run> --quality same|worse|better`.
5. Record failures explicitly: `steward log-task ... --result fail|escalated`
   (a redo logs the failed attempt too — those tokens were spent).
   Silence means accepted; spend is metered automatically by `ingest-usage`.
6. Dispatch cheap-first for non-top tasks: never skip tiers. If you believe
   the table is wrong, report it for re-assessment — don't route around it.

## Repository conventions (for agents contributing to THIS repo)

- Engine contains **zero project-specific knowledge** — if a target project's
  manifest can't express something, add a generic probe parameter, never a
  special case.
- Every probe type ships with at least one fixture test; `python -m pytest
  tests/ -q` and `python -m ruff check src/ tests/` must both pass before
  any commit.
- Public-facing output (CLI messages, README, errors) is English.
- Releases: bump `version` in pyproject.toml, update CHANGELOG.md, push a
  `v*` tag — CI publishes to PyPI via Trusted Publishing.
