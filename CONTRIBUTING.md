# Contributing

Thanks for looking. Scope first, mechanics second.

## Scope (what will and won't merge)

agent-steward flags, sorts, meters, and **proposes** — it never decides,
never edits the target project, and never phones home. PRs that add silent
network calls, auto-applied changes, or per-project special cases in the
engine will be declined regardless of quality. If a target project's rules
can't be expressed in a manifest, the fix is a new **generic** probe
parameter or probe type — never `if project == X` in the engine.

Good contribution shapes:
- A new generic probe type (with a fixture test and a real-world rationale)
- A manifest for a project shape we haven't seen (rule packs welcome)
- Bug reports with the manifest + report output that reproduces them

## Mechanics

```bash
git clone https://github.com/michaelchen73092/agent-steward
cd agent-steward
pip install pyyaml pytest ruff
python -m pytest tests/ -q      # 60+ tests, must stay green
python -m ruff check src/ tests/
```

- Every probe type ships with at least one fixture test (`tests/test_steward.py`).
- New probes default to `severity: warn` — observe-first is a product rule,
  not a style preference.
- The engine stays small enough for one person to read. Complexity goes into
  probe parameters, not core control flow.

Maintainer: single. Response times are best-effort; the tool is maintained
as it is used, daily, on the author's own projects.
