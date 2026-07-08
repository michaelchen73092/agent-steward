# Security

## Model

agent-steward's security posture is structural:

- **Read-only on targets.** No code path edits or deletes files in the
  project being checked; all writes go to its own state directory.
- **Offline by default.** The deterministic checks make zero network calls.
  The optional judge is the single exception: violation excerpts only, via
  the user's own `claude` CLI login or their own `ANTHROPIC_API_KEY`,
  hard-coded to `api.anthropic.com`, failing open to deterministic ranking.
- **No credentials held.** The tool stores no keys, no tokens, no accounts.
- **Fail-open.** A crash produces a missing report, never a blocked pipeline.

## Reporting

Found a way to make agent-steward write outside its state dir, reach an
unexpected host, or execute content from a target project? That's a security
bug. Email **michaelchen73092@gmail.com** with the manifest and steps;
please don't open a public issue first.

Note: `cmd` probes execute shell commands **from the user's own manifest** —
that is by design (the manifest is the user's code). Readonly mode refuses
them unless explicitly marked `readonly_safe: true`.
