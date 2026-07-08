# CLAUDE.md — agent-steward

> Working guidance for agent sessions in this repo. Agent-facing usage docs:
> `AGENTS.md`. Full reference: `docs/REFERENCE.md`. Contributor rules:
> `CONTRIBUTING.md`.
>
> **Maintainer sessions**: the internal companion repo (private,
> `michaelchen73092/agent-steward-internal`, local clone at
> `~/agent-steward-internal/`) holds the design plans, product/business
> analysis, the dev journal (`CLAUDE-internal.md` — read it for 現況), and
> live manifests for readonly targets. Read it before substantive work;
> write journal updates THERE, not here.

## Iron rules (violating one = a bug)

1. **Zero project knowledge in the engine.** `if project == X` anywhere in
   `src/` is a bug. If a manifest can't express something, add a generic
   probe parameter or probe type.
2. **The trust contract is non-negotiable**: read-only on targets / zero
   network by default (judge excepted: user's own credential, whitelisted,
   fail-open) / fail-open everywhere / violations ≠ verdicts — the steward
   flags, sorts, meters, proposes; it never decides.
3. **Public-facing output is English** (CLI, errors, README).
4. **Observe first**: new probes default to `severity: warn`; promotion to
   `fail` only after the false-positive rate is measured.
5. **Every probe type ships with a fixture test.** Before any commit:
   `python -m pytest tests/ -q` and `python -m ruff check src/ tests/`
   both green.

## Layout

- `src/agent_steward/cli.py` — verification core + CLI (keep readable by one person)
- `src/agent_steward/allocate.py` — allocation layer (tiers, tune, canary, savings)
- `src/agent_steward/route.py` — attention queue, judge, distiller
- `src/agent_steward/ingest.py` — transcript usage ingestion
- `examples/` — generic, anonymized teaching manifests only (live project
  manifests belong in their target projects, or in the internal repo for
  readonly targets)
- `local/` — gitignored working area

## Releasing

Bump `version` in `pyproject.toml`, update `CHANGELOG.md`, commit, push a
`v*` tag. CI tests, builds, and publishes to PyPI via Trusted Publishing —
no tokens exist anywhere.

## Contact

Wei-Chih Chen — michaelchen73092@gmail.com
