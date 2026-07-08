"""
agent-steward — transcript usage ingester (zero-manual metering)

Claude Code already writes every session's per-message usage (model, input/
output/cache tokens, timestamps) to ~/.claude/projects/<slug>/, including one
file per spawned worker under <session>/subagents/. This module turns those
transcripts into usage-ledger entries, replacing hand-estimated `log-task
--est-tokens` for the SPEND side of metering.

Contract (the split that keeps loops honest):
- Ingested entries carry `via: transcript` and MEASURED token counts. Money
  views (savings, tier tables, CPAU) count them.
- Quality loops (tune escalation stats, canary cadence) ignore them — quality
  is a verdict, and verdicts stay explicit (`log-task --result ...`).
- Fail-open everywhere: the transcript format is Claude Code internal and
  undocumented; anything unreadable is skipped, and manual logging always
  works as the fallback.
"""
import fnmatch
import json
import os
import re

CURSOR_FILE = "ingest_cursor.json"
TASK_MARKER_RE = re.compile(r"\[task[=:]([\w-]+)\]")
SKIP_MODELS = {"<synthetic>", "", "None"}


def project_slug(root):
    """Claude Code names its per-project transcript dir by mangling the abs
    path: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(root))


def default_transcript_dir(root):
    return os.path.join(os.path.expanduser("~"), ".claude", "projects",
                        project_slug(root))


def scan_transcripts(tdir):
    """Session files at the top level + one file per worker under
    <session>/subagents/. Sorted for deterministic ingest order."""
    out = []
    if not os.path.isdir(tdir):
        return out
    for name in os.listdir(tdir):
        p = os.path.join(tdir, name)
        if name.endswith(".jsonl") and os.path.isfile(p):
            out.append(p)
        elif os.path.isdir(p):
            sub = os.path.join(p, "subagents")
            if os.path.isdir(sub):
                out.extend(os.path.join(sub, f) for f in os.listdir(sub)
                           if f.endswith(".jsonl"))
    return sorted(out)


def read_usage(path, offset=0):
    """Read a transcript from `offset`; aggregate usage per model.
    Returns (per_model dict, new_offset, first_user_text). Malformed lines
    are skipped — fail-open by contract."""
    per_model, first_user = {}, None
    try:
        size = os.path.getsize(path)
        if offset > size:            # rotated/truncated: start over
            offset = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                m = r.get("message") or {}
                if not isinstance(m, dict):
                    continue
                if first_user is None and r.get("type") == "user":
                    c = m.get("content")
                    first_user = c if isinstance(c, str) else json.dumps(
                        c, ensure_ascii=False)[:2000]
                u = m.get("usage") or {}
                model = str(m.get("model", ""))
                if not u or model in SKIP_MODELS:
                    continue
                agg = per_model.setdefault(model, {
                    "in": 0, "out": 0, "cache_read": 0, "cache_write": 0,
                    "messages": 0, "last_ts": None})
                agg["in"] += u.get("input_tokens", 0) or 0
                agg["out"] += u.get("output_tokens", 0) or 0
                agg["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
                agg["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
                agg["messages"] += 1
                ts = str(r.get("timestamp", ""))[:19]
                if ts:
                    agg["last_ts"] = ts
            new_offset = f.tell()
    except OSError:
        return {}, offset, None
    return per_model, new_offset, first_user


def attribute_task(first_user, task_map, default):
    """Dispatch-prompt -> task id. Order: explicit [task=x] marker (the
    lightweight protocol), then user-supplied regex map, then the default."""
    text = first_user or ""
    m = TASK_MARKER_RE.search(text)
    if m:
        return m.group(1)
    for task_id, patterns in (task_map or {}).items():
        for pat in patterns if isinstance(patterns, list) else [patterns]:
            try:
                if re.search(str(pat), text):
                    return str(task_id)
            except re.error:
                continue
    return default


def tier_for(model, alloc):
    for tier, pats in ((alloc or {}).get("tier_patterns") or {}).items():
        pats = pats if isinstance(pats, list) else [pats]
        if any(fnmatch.fnmatch(str(model).lower(), str(p).lower())
               for p in pats):
            return str(tier)
    return "_unknown"


def load_cursor(state_dir):
    try:
        with open(os.path.join(state_dir, CURSOR_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cursor(state_dir, cursor):
    with open(os.path.join(state_dir, CURSOR_FILE), "w", encoding="utf-8") as f:
        json.dump(cursor, f, indent=1)


def ingest(paths, state_dir, alloc=None, task_map=None,
           session_task="_session", worker_default="_unattributed",
           project=None, now="", dry_run=False):
    """Turn new transcript content into ledger entries. One entry per
    (file, model) per ingest run — a worker file is one worker run; a session
    file is the main window. Returns the entries (appended unless dry_run)."""
    cursor = load_cursor(state_dir)
    entries = []
    for path in paths:
        is_worker = os.sep + "subagents" + os.sep in path
        per_model, new_offset, first_user = read_usage(
            path, int(cursor.get(path, 0)))
        if new_offset == cursor.get(path, 0) and not per_model:
            continue
        for model, agg in sorted(per_model.items()):
            if not (agg["in"] or agg["out"] or agg["cache_read"]):
                continue
            task = (attribute_task(first_user, task_map, worker_default)
                    if is_worker else session_task)
            e = {"ts": agg["last_ts"] or now, "task": task,
                 "tier": tier_for(model, alloc), "model": model,
                 "est_tokens": agg["in"] + agg["out"],
                 "via": "transcript",
                 "measured": {k: agg[k] for k in
                              ("in", "out", "cache_read", "cache_write",
                               "messages")},
                 "note": f"ingested from {os.path.basename(path)}"}
            if project:
                e["project"] = project
            entries.append(e)
        cursor[path] = new_offset
    if not dry_run:
        os.makedirs(state_dir, exist_ok=True)
        lpath = os.path.join(state_dir, "usage_ledger.jsonl")
        with open(lpath, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        save_cursor(state_dir, cursor)
    return entries
