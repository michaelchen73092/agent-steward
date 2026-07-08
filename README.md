<img src="https://raw.githubusercontent.com/michaelchen73092/agent-steward/main/assets/icon.svg" alt="agent-steward — the canary" width="96" align="right">

# agent-steward

[![PyPI](https://img.shields.io/pypi/v/agent-steward)](https://pypi.org/project/agent-steward/)
[![CI](https://github.com/michaelchen73092/agent-steward/actions/workflows/ci.yml/badge.svg)](https://github.com/michaelchen73092/agent-steward/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/agent-steward)](https://pypi.org/project/agent-steward/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**A quality gate and spend auditor for your AI agent fleet.**

You run AI agents that work for hours on your behalf — extracting data, writing code, building knowledge bases. Two questions keep nagging you: *"Is what they produced actually correct?"* and *"Am I burning expensive model credits on work a cheap model could do?"*

agent-steward answers both, automatically, after every agent session. It checks your agents' output against **your own rules**, shows you only the handful of items that truly need human judgment, and tells you which tasks deserve an expensive model and which don't.

It never edits your files, never sends your data anywhere, and can be removed in 30 seconds.

---

## The problem in one paragraph

When one AI agent does one task, you review it yourself. When a fleet of agents does hundreds of tasks per day, you can't. So either you review everything (you become the bottleneck), or you review nothing (silent errors pile up), or you cap spending with a blunt dollar limit (Tesla-style $200/week caps — which can't tell money well spent from money wasted). agent-steward is the fourth option: **machines check what machines can check, you judge only what needs judgment, and every dollar gets a task-level justification.**

The two halves are one loop, and the loop is the product: the **verification** half defines what "correct" means (your rules, executable); the **allocation** half uses that definition to prove where a cheaper model is safe; fixes get **authorized by category** and executed by your own agent; and everything the loop learns — measured spend, quality verdicts, recurring decisions — shrinks what reaches your judgment next round. Verify → allocate → fix → less for you to judge. Each pass around the loop, your attention buys more.

## Set it up with one prompt

You don't run the onboarding yourself — your agent does. Open a Claude Code (or similar) session **inside your project** and paste this:

```text
Set up agent-steward for this project:
1. Install it: pipx install agent-steward (or: python3 -m pip install --user agent-steward).
2. Run `steward init --out steward.yaml`, read the rubric it prints, and fill in the
   skeleton from THIS project's own written rules (CLAUDE.md, docs/, schema files).
   Every probe: severity warn, plus source:, fix:, and fixable_by: lines.
3. Show me the drafted steward.yaml. Walk me through only the decisions that are mine:
   which severities to keep at warn, and (if you also draft .allocation.yaml via
   `steward allocate rubric` + `allocate init`) which floor: lines protect
   irreversible work.
4. After I approve: `steward baseline --manifest steward.yaml`, then
   `steward install-hook --manifest steward.yaml`, then `steward ingest-usage`.
5. Run `steward report` and explain the "What needs you" section to me.
```

That's the whole setup. From then on the hook runs after every session, and `steward report` is your dashboard.

## Who is this for — find your level

You use Claude Code, Cowork, or a similar terminal-based AI agent, and your agent's work lands **as files in a repo** (notes, data, code, knowledge bases — if the output is files, it's checkable; if it isn't files, this tool has nothing to check). Three levels. Each level's commands are written so an agent can run them for you — paste this README at it.

### Level 1 — you just run sessions (the setup prompt above gives you exactly this)

No pipelines, no automation, no logging discipline. You get **both halves** on day one:

1. **Rule check, every session.** The hook runs your manifest after each session; new violations are fed back to the agent to self-repair. Your report shows the *Rule check* summary and the *authorize-fixes-per-row* table (below).
2. **Measured spend + room-to-move evidence.** `steward ingest-usage` reads the harness's own transcripts — model, tokens, per session, zero manual logging. Your report shows *Where the money goes* by tier. To judge whether your model choice has downgrade room at this level: run your usual work on a cheaper model for a few days and watch the Rule check section — **if violations don't rise while the tier column shifts down, the expensive default was habit, not need.** (Evidence-based but manual at L1; L2 automates exactly this judgment.)

What your L1 report contains — **Rule check** ("12 rules checked: 10 pass, 2 warn; 23 open findings") followed by the authorization table:

| category | findings | how it gets fixed | who fixes it |
|---|---|---|---|
| **rule conflicts** | 1 | two of your rules disagree — steward names both sides; only you can decide | **human** |
| schema-floor | 15 | fill missing fields from the source doc | agent |
| code lint | 7 | ruff --fix + the worker self-repair hook | script |

…and **Where the money goes**:

| tier | weight | runs | tokens | % volume | % cost |
|---|---|---|---|---|---|
| cheap | 1 | 6 | 619,000 | 7.4% | 0.9% |
| mid | 8 | 45 | 7,530,226 | 90.1% | 91.2% |
| top | 25 | 7 | 208,000 | 2.5% | 7.9% |

The **rule conflicts** row is the one category no machine may resolve — two of your own written rules disagree (e.g. rule A allows `origin: synthesis`, rule B doesn't). steward tracks it, names both sides, and holds it for human review. Every other row states how it gets fixed, so you authorize per row — "fix everything marked agent/script" is one sentence, not 22 reads.

### Level 2 — you have repeating jobs (a scheduled run, a daily command)

Everything in L1, **plus automated detection of "could this job run a tier lower at the same correctness?"** — and the reverse: jobs that keep failing on the cheap tier get promotion proposals.

Enable it (once):

```bash
steward allocate rubric                  # your agent rates each job on 4 published axes
steward allocate init --axes axes.yaml   # deterministic matrix -> .allocation.yaml
# tag each job's prompt with [task=daily-ingest]  <- one word, that's the whole protocol
```

From then on, before a run of a canary-enabled job: `steward canary --task daily-ingest` — exit 0 means "also run this once on the tier below and compare" (5% sampling by default, deterministic, never below the task's floor). Record the comparison on the shadow entry: `steward log-task --task daily-ingest --tier cheap --canary shadow --pair run-12 --quality same`.

> **How "same correctness" is judged:** correctness = *your rules*, not a benchmark. A shadow run counts as equal if (a) it passes the same deterministic checks the primary passed, and (b) you (or your collecting agent) mark its `--quality same` against the primary at review time. Enough judged runs at parity → a demote proposal with the evidence attached; **one measured quality gap vetoes the demote** even if failure rates look clean. Cheapest at the same quality, never cheapest at any quality.

What appears in your report that L1 doesn't have — the up/down evidence. A **high→low proposal** looks like this (in *What needs you*):

> **tier change proposed**: condense mid → cheap — 12 of 12 shadow runs (5% sampling) passed the same checks at equal quality (see *How "same correctness" is judged* above) — apply: `steward allocate tune --apply --only condense`

**Escalations — where a tier proved too low** (the automatic up direction):

| task | from | redone at | n | why |
|---|---|---|---|---|
| extract | cheap | mid | 3 | output failed your written rules at this tier |

**Canary — where a tier may be too high** (the evidence behind the down direction):

| task | shadow runs | judged | quality parity | shadow tokens |
|---|---|---|---|---|
| condense | 12 | 12 | 1.0 | 240,000 |

Nothing applies itself: `steward allocate tune` prints proposals; you accept one with `--apply --only <task>`.

### Level 3 — you run fleets (a dispatcher spawning parallel workers)

**Why a fleet needs more than L2, in one sentence: at fleet scale, anything that requires a human per item stops working.** Three per-item things break, and L3 exists to replace each one:

| What breaks at fleet scale | Why | The L3 replacement |
|---|---|---|
| *"Which model made this file?"* has no answer | One session, you know. Six workers writing forty files in parallel, and a bad one surfaces two weeks later — the answer must live **on the artifact**, or dispatch policy is unauditable | **Provenance stamps**: `steward stamp <file> --produced-by <model> --task <t>`; the `allocation_compliance` probe then audits stamps against your tier table |
| The leftovers exceed what you can read | L2 leaves a handful of flagged items; a fleet leaves hundreds per week. Read all = you're the bottleneck; read none = silent rot | **Attention queue**: `steward route --judge` ranks everything by impact × judgment-worthiness; you read from the top, grade as you go (`steward approve <id> --verdict worth\|not-worth`), stop when scores go low |
| You make the same judgment call again and again | One session never notices its own repetition; a fleet makes you reject the same *shape* of thing nine times | **Verdict memory**: `steward distill` clusters recurring reasons into rule candidates — the tenth one never reaches you |

**How to enable it — there is no switch. L3 is a protocol your dispatcher agent follows.** Paste this into the CLAUDE.md of the project that spawns workers, and the machinery lights up:

```markdown
## steward fleet protocol (for the dispatcher)
1. Tag every worker prompt with [task=<task_id>]  (ids from .allocation.yaml).
2. Before dispatching a canary-enabled task: `steward canary --task <id>` —
   exit 0 = also dispatch one shadow worker one tier lower on the same input.
3. When a worker's file lands: `steward stamp <file> --produced-by <model> --task <id>`.
4. At collection, judge shadow vs primary: `steward log-task --task <id>
   --tier <t> --canary shadow --pair <run-id> --quality same|worse|better`.
5. Record failures explicitly: `steward log-task ... --result fail|escalated`.
   Silence means accepted; spend is metered automatically by ingest-usage.
```

Report additions at L3: *Attention economics* (M1 — top-tier tokens per accepted decision, the price of your attention; M4 — residue precision, whether the queue deserved your trust) and per-rule feedback ("this rule's findings keep being judged not-worth — revisit it").

## Quickstart, manual version (the same steps, typed yourself)

```bash
# 1. Install (isolated, doesn't touch your other Python stuff)
pipx install agent-steward

# 2. Draft your project's rules — the agent already working in your
#    project reads the printed rubric and fills in the skeleton from
#    YOUR docs (CLAUDE.md, schema files); you review, you don't write
cd your-project
steward init --out steward.yaml

# 3. Get your "before" numbers (observation only, nothing is blocked)
steward baseline --manifest steward.yaml

# 4. Optional: make it run automatically after every agent session
steward install-hook --manifest steward.yaml

# Lost? Bare `steward` always tells you where you are and what's next.
steward
```

## The two decisions the drafts will bring to you (everything else is drafted)

You never face these decisions cold — they arrive **inside a draft your agent shows you** during step 3 of the setup prompt, as specific lines to keep or change:

| Decision | When you'll face it | Where it lives | What you do | If you do nothing |
|---|---|---|---|---|
| *"Which work must never be delegated to a cheaper model?"* | When your agent shows you the drafted `.allocation.yaml` (Level 2+; skip at Level 1) | The `floor:` line on each task | Read the drafted floors, raise any that guard irreversible or high-stakes work — e.g. `floor: top` on final decisions | Floors stay as drafted from your own docs; tuning can never demote below a floor either way |
| *"Warn or block?"* | When your agent shows you the drafted `steward.yaml` | The `severity:` line on each probe | Nothing, at first — **keep everything `warn`** (observe mode). After a couple of weeks, promote your few most-trusted, zero-false-positive rules to `fail` | Everything stays warn: you get reports, nothing is ever blocked. This is the recommended start |

(A third choice appears only if you later turn on the optional queue-sorting judge at Level 3 — it's explained there, and doing nothing means it stays off.)

## What actually gets added to your project — all drafted, none hand-written

| File | What it is | How it appears |
|---|---|---|
| `steward.yaml` (name it anything) | Your rules as executable checks | Your agent drafts it from your own docs (`steward init` prints the how-to); you approve |
| `.allocation.yaml` (Level 2+ only) | Which task types use which model tier | Generated by `steward allocate init` from agent-rated axes; you approve the floors |
| One hook entry in `.claude/settings.json` | "Run steward when the agent finishes" | Written by `steward install-hook` |

Everything steward *produces* (state, spend ledger, attention queue, fixes scoreboard) lives in `.steward/` — its own folder.

**Who edits your files: never steward, sometimes your agent, always with your authorization.** steward itself has no code path that edits or deletes your files — it only reports. Fixing what it finds is your agent's job, and the report is built so you can authorize that in one sentence: every finding category states *how it gets fixed* and *who can fix it* (agent / script / human), so you say **"fix all the agent- and script-fixable rows"** and your agent executes exactly those categories — the `fix:` note on each rule tells it how. Rows marked *human* wait for you.

## How it works (60-second version)

**Checking (the quality gate).** After each agent session, steward scans what changed against your rules — schema fields present, confidence caps respected, code passes lint, files that must exist do exist. Hard rule violations are listed instantly and for free (no LLM involved). Borderline items optionally go to a cheap model for a first opinion. What survives both filters — typically 2-5 items, ranked by how much they matter — is the only thing you look at.

**Allocating (the spend audit).** Every task gets logged: what type of work, which model tier, roughly how much it cost. Over time steward learns where you're overpaying: if a task type running on the expensive model would pass all your quality checks on a cheaper one (it tests this on a small sample — a "canary"), it proposes a downgrade. If a cheap model keeps failing your checks, it proposes an upgrade. **You approve every change; steward never reroutes silently.**

**Learning (the case file).** When you overrule or confirm a flagged item, your reasoning is recorded. Recurring decisions get proposed as new automatic rules — so the same question never reaches your desk twice.

## Reading your first report

After each run you get a short terminal summary; `steward report` gives the cumulative view. Its first section is literally titled **What needs you** — if it's empty, you're done for the day. Example:

> **What needs you**
> - **tier change proposed**: condense mid → cheap (12/12 canaries at quality parity) — apply: `steward allocate tune --apply --only condense`
> - **queue top**: two knowledge entries contradict each other (affects 2 published conclusions) — decide which stands, or keep both flagged

Below it come the evidence sections you've seen in the level guides above — *Rule check* with the authorize-per-row table, *Where the money goes*, *Escalations*, *Canary* — and a trend line:

> cost per accepted output: **−31%** vs baseline · escaped defects: 0.11% → **0.09%** — the two numbers to watch: cheaper AND not sloppier.

**How to read it as a habit:** read **What needs you** (usually under 5 items), authorize fix categories by row ("fix everything marked script/agent"), glance at the trend once a week. Total: 2-3 minutes per session instead of re-reviewing everything your agents did.

## Safety promises (structural, not pinky-swears)

1. **Read-only on your project.** steward writes only inside `.steward/`. There is no code path that deletes or edits your files.
2. **Offline by default.** The rule check makes zero network calls. The only model touch is the optional judge — violation excerpts only (never whole files), through your own logged-in `claude` CLI or your own API key (hard-whitelisted to api.anthropic.com), and it fails open to the deterministic ranking.
3. **Auditable.** The engine is a few hundred lines of Python with one dependency. You can read all of it. Prefer paranoia? Copy the single file into your repo and skip the package manager entirely.
4. **Fail-open.** If steward crashes, your pipeline runs exactly as before — you just don't get a report that round. A watchdog must never take down what it watches.
5. **30-second uninstall.** Delete `.steward/`, remove one hook line. No migration, no residue.

## FAQ

**Does my data leave my machine?** No, unless you enable the judge — and then only violation excerpts, through your own login or key, to the model you chose.

**Will it slow my agents down?** No. It runs *after* sessions (seconds, in batch), not between your agent and the model.

**What if it flags too much?** That's expected in week one. Stay in warn mode, tune the two YAML files (they're short and human-readable), and watch the false-alarm rate drop. Our own first deployment went from 1,270 flags to 251 real ones by fixing two config lines.

**Is this a model router / API gateway?** No. It never sits between you and the model, never proxies traffic, never holds your keys. It's closer to `pre-commit` or `pytest` — a checker you invoke, not infrastructure you live inside.

## Real numbers, real projects

Measured on the author's own fleet (a 3,000-node research graph, a personal knowledge system, a trading-ops repo, a docs-only design project):

- **68% estimated spend saved** vs running everything on the top model — with the quality guard flat (escaped-defect rate 0.0009, unchanged)
- **289 flagged items → 5 decisions**: the queue + judge compressed a day's residue into what actually needed a human; residue precision (M4) 0.93
- **400+ violations resolved** and receipted in the fixes scoreboard — including one rule-doc transcription error that alone caused 241 false positives
- **6 minutes** to onboard two projects the tool had never seen, using zero new probe types

Full command list and design details: **[docs/REFERENCE.md](docs/REFERENCE.md)**.

## Status

v0.16 on PyPI. The verification and allocation loops are complete and battle-tested on four real projects. Single maintainer, maintained as I use it — the tool audits its own example configs, so drift shows up in its own reports. Expect fast iteration.

## License & contact

MIT. Use it, fork it, sell it, we don't mind.

Questions, ideas, or want to tell us about your setup? **michaelchen73092@gmail.com**
