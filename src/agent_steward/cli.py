#!/usr/bin/env python3
"""
agent-steward — V1/R1 engine (portable, project-agnostic)

Design contract (docs/01_MASTER_PLAN.md + docs/02_RESOURCE_ALLOCATOR_PLAN.md):
  1. ZERO-POLLUTION: engine reads the target project read-only; it writes ONLY
     under its own --out dir and its own state dir (default: ./.steward under
     the *invoking* directory, never under a readonly target). `readonly` mode
     additionally refuses `cmd` probes unless marked `readonly_safe: true`.
  2. ENGINE/MANIFEST SPLIT: engine knows nothing about any project. All
     project knowledge lives in a YAML manifest (the only per-project cost).
  3. DEGRADE GRACEFULLY: missing tool / missing files → probe status
     "skipped", never a crash. A verification layer that crashes the
     pipeline it verifies is a bug.
  4. VIOLATIONS ≠ VERDICTS: deterministic probes flag residue for the
     adjudicator; the steward routes and sorts, it never decides.
  5. OBSERVE FIRST: new probes default to severity "warn"; they may only be
     promoted to "fail" after their false-positive rate has been measured.

V1 additions (verification): --diff (report only violations added/resolved
since the last check), per-probe `source:` rule-provenance field, rulebook
coverage metric (M5).
R1 additions (metering): provenance stamping (`steward stamp`), usage ledger
(`steward log-task`), `allocation_compliance` probe (declared tier vs actual
model per provenance stamps).

Usage:
  steward check --manifest configs/foo.yaml [--root PATH] [--out DIR] [--diff]
  steward stamp FILE --produced-by MODEL --task TASK_ID [--round N]
  steward log-task --task TASK_ID --tier TIER [--model M] [--est-tokens N] [--result R]
"""
import argparse
import csv as csvmod
import datetime as dt
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys

try:
    import yaml
except ImportError:
    print("FATAL: pyyaml required (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

try:
    from agent_steward import allocate as alloc_mod
    from agent_steward import ingest as ingest_mod
    from agent_steward import route as route_mod
except ImportError:  # running as a bare script without package context
    import allocate as alloc_mod
    import ingest as ingest_mod
    import route as route_mod

STATE_DIR_DEFAULT = ".steward"

# ---------------------------------------------------------------- helpers

def now_iso():
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _glob_match(rel, pat):
    """Proper ** glob semantics (zero or more directories), unlike raw
    fnmatch where `a/**/*` refuses to match `a/x`. Used by scope_guard,
    where 'expected areas' must mean what a human thinks they mean."""
    out = []
    i, pat = 0, str(pat)
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 3] == "**/":
                out.append(r"(?:.*/)?")
                i += 3
                continue
            if pat[i:i + 2] == "**":
                out.append(r".*")
                i += 2
                continue
            out.append(r"[^/]*")
        elif c == "?":
            out.append(r"[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.fullmatch("".join(out), rel) is not None


def rglob(root, pattern):
    """Recursive glob relative to root with PROPER ** semantics (zero or
    more directories — `records/**/*.md` matches `records/a.md` too).
    Raw fnmatch treats ** like *, which silently skips depth-1 files: the
    single most common first-run confusion for new users. Returns sorted paths."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if _glob_match(rel, pattern):
                out.append(full)
    return sorted(out)


FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def read_frontmatter(path):
    """Return (dict|None, error|None). None dict = no frontmatter block."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(16384)
    except OSError as e:
        return None, f"unreadable: {e}"
    m = FM_RE.match(head)
    if not m:
        return None, "no frontmatter block"
    try:
        data = yaml.safe_load(m.group(1))
        if not isinstance(data, dict):
            return None, "frontmatter not a mapping"
        return data, None
    except yaml.YAMLError as e:
        return None, f"yaml error: {str(e).splitlines()[0]}"


def fm_match(fm, where):
    """Shared where-clause semantics: '!null' = non-null, '*' = key present,
    anything else = string equality."""
    for k, v in (where or {}).items():
        if v == "!null":
            if fm.get(k) is None:
                return False
        elif v == "*":
            if k not in fm:
                return False
        elif str(fm.get(k)) != str(v):
            return False
    return True


def parse_when(value):
    """Best-effort date parse of a frontmatter value -> datetime or None."""
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(value[:19], fmt)
            except ValueError:
                continue
    return None


def result(spec, ptype, status, detail="", violations=None, n_checked=0):
    """spec may be a probe spec dict (carries id + source) or a plain id str."""
    if isinstance(spec, dict):
        pid, source = spec.get("id", "?"), spec.get("source", "")
    else:
        pid, source = spec, ""
    return {
        "probe": pid, "type": ptype, "status": status,  # pass|warn|fail|skipped
        "n_checked": n_checked, "n_violations": len(violations or []),
        "source": source, "detail": detail, "violations": violations or [],
    }

# ---------------------------------------------------------------- probes
# Every probe: fn(root, spec) -> result dict. Deterministic. Read-only.

def probe_cmd(root, spec):
    try:
        p = subprocess.run(
            spec["cmd"], shell=True, cwd=root, capture_output=True,
            text=True, timeout=spec.get("timeout", 120),
        )
    except subprocess.TimeoutExpired:
        return result(spec, "cmd", "fail", "timeout")
    except OSError as e:
        return result(spec, "cmd", "skipped", f"cannot exec: {e}")
    ok = p.returncode == 0
    tail = (p.stdout + p.stderr).strip().splitlines()[-15:]
    if p.returncode == 127:  # shell: command not found — a missing dependency
        # is the operator's problem, not a code violation; say so loudly once
        return result(spec, "cmd", "skipped",
                      f"missing tool — the command cannot run: "
                      f"{(tail or ['?'])[-1]}. Install the tool (or grant the "
                      f"agent permission to) or fix the manifest cmd.")
    return result(spec, "cmd", "pass" if ok else spec.get("on_fail", "fail"),
                  f"rc={p.returncode}", violations=[] if ok else tail)


def probe_jsonl_wellformed(root, spec):
    path = os.path.join(root, spec["path"])
    if not os.path.exists(path):
        return result(spec, "jsonl_wellformed", "skipped", "file missing")
    bad, n = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            n += 1
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                bad.append(f"line {i}: {str(e)[:80]}")
    return result(spec, "jsonl_wellformed", "pass" if not bad else "fail",
                  spec["path"], violations=bad, n_checked=n)


def probe_frontmatter_required(root, spec):
    files = rglob(root, spec["glob"])
    if not files:
        return result(spec, "frontmatter_required", "skipped", "no files match")
    req = spec["required"]
    bad = []
    for fp in files:
        fm, err = read_frontmatter(fp)
        rel = os.path.relpath(fp, root)
        if fm is None:
            bad.append(f"{rel}: {err}")
            continue
        missing = [k for k in req if k not in fm]
        if missing:
            bad.append(f"{rel}: missing {missing}")
    sev = spec.get("severity", "fail")
    return result(spec, "frontmatter_required",
                  "pass" if not bad else sev, f"{spec['glob']} req={req}",
                  violations=bad, n_checked=len(files))


def probe_single_source_cap(root, spec):
    """Nodes with <2 sources must respect a confidence cap. Cap is
    class-dependent (manifest-configurable). Violations = residue for the
    adjudicator, so default severity is warn, not fail."""
    files = rglob(root, spec["glob"])
    if not files:
        return result(spec, "single_source_cap", "skipped", "no files match")
    default_cap = float(spec.get("default_cap", 0.5))
    class_caps = spec.get("class_caps", {})  # e.g. {self_declarative: 0.9}
    class_field = str(spec.get("class_field", "claim_class"))  # facts use
    # claim_class; other node kinds may key their caps off another field
    # (e.g. an insight's origin) — the manifest names it, never the engine
    exempt_field = spec.get("exempt_field")  # e.g. g1_exempt — a documented,
    # reviewable exception beats a rule everyone silently ignores
    bad, n, exempted = [], 0, 0
    for fp in files:
        fm, _err = read_frontmatter(fp)
        if fm is None:
            continue
        n += 1
        if exempt_field and fm.get(exempt_field):
            exempted += 1
            continue
        sources = fm.get("sources") or []
        conf = fm.get("confidence")
        if not isinstance(conf, (int, float)):
            continue
        if isinstance(sources, list) and len(sources) < 2:
            cap = float(class_caps.get(str(fm.get(class_field, "")), default_cap))
            if conf > cap + 1e-9:
                rel = os.path.relpath(fp, root)
                bad.append(f"{rel}: 1 source, conf={conf} > cap {cap} "
                           f"({class_field}={fm.get(class_field, '-')})")
    return result(spec, "single_source_cap",
                  "pass" if not bad else spec.get("severity", "warn"),
                  f"caps default={default_cap} {class_caps}"
                  + (f", {exempted} exempted via '{exempt_field}'" if exempted else ""),
                  violations=bad, n_checked=n)


def probe_field_value_rule(root, spec):
    """Generic: field must be in allowed set (e.g. origin ∈ expert|user|synthesis)."""
    files = rglob(root, spec["glob"])
    if not files:
        return result(spec, "field_value_rule", "skipped", "no files match")
    field, allowed = spec["field"], set(spec["allowed"])
    require_present = spec.get("require_present", True)
    bad = []
    for fp in files:
        fm, _ = read_frontmatter(fp)
        if fm is None:
            continue
        rel = os.path.relpath(fp, root)
        if field not in fm:
            if require_present:
                bad.append(f"{rel}: {field} missing")
            continue
        if str(fm[field]) not in allowed:
            bad.append(f"{rel}: {field}={fm[field]!r} not in {sorted(allowed)}")
    return result(spec, "field_value_rule",
                  "pass" if not bad else spec.get("severity", "fail"),
                  f"{field} in {sorted(allowed)}", violations=bad, n_checked=len(files))


def probe_bash_syntax(root, spec):
    files = rglob(root, spec["glob"])
    if not files:
        return result(spec, "bash_syntax", "skipped", "no files match")
    bad = []
    for fp in files:
        p = subprocess.run(["bash", "-n", fp], capture_output=True, text=True)
        if p.returncode != 0:
            bad.append(f"{os.path.relpath(fp, root)}: {p.stderr.strip()[:120]}")
    return result(spec, "bash_syntax", "pass" if not bad else "fail",
                  spec["glob"], violations=bad, n_checked=len(files))


def probe_csv_required_columns(root, spec):
    files = rglob(root, spec["glob"])
    if not files:
        return result(spec, "csv_required_columns", "skipped",
                      f"no files match {spec['glob']}")
    req = spec["columns"]
    bad = []
    for fp in files:
        try:
            with open(fp, encoding="utf-8-sig", errors="replace") as f:
                header = next(csvmod.reader(io.StringIO(f.readline())))
        except (OSError, StopIteration):
            bad.append(f"{os.path.relpath(fp, root)}: unreadable/empty")
            continue
        missing = [c for c in req if c not in header]
        if missing:
            bad.append(f"{os.path.relpath(fp, root)}: missing cols {missing}")
    return result(spec, "csv_required_columns",
                  "pass" if not bad else spec.get("severity", "fail"),
                  f"req={req}", violations=bad, n_checked=len(files))


def probe_tsv_wellformed(root, spec):
    path = os.path.join(root, spec["path"])
    if not os.path.exists(path):
        return result(spec, "tsv_wellformed", "skipped", "file missing")
    min_cols = int(spec.get("min_cols", 2))
    bad, n = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            n += 1
            cols = line.rstrip("\n").split("\t")
            if len(cols) < min_cols:
                bad.append(f"line {i}: {len(cols)} cols < {min_cols}")
    return result(spec, "tsv_wellformed", "pass" if not bad else "fail",
                  spec["path"], violations=bad, n_checked=n)


def probe_file_exists(root, spec):
    ok = os.path.exists(os.path.join(root, spec["path"]))
    return result(spec, "file_exists", "pass" if ok else spec.get("severity", "fail"),
                  spec["path"], n_checked=1)


def probe_filename_pattern(root, spec):
    """Naming conventions as code: every file matching glob must have a
    basename matching at least one of the given regex patterns."""
    files = rglob(root, spec["glob"])
    if not files:
        return result(spec, "filename_pattern", "skipped", "no files match")
    try:
        pats = [re.compile(p) for p in spec["patterns"]]
    except re.error as e:
        return result(spec, "filename_pattern", "skipped", f"bad regex: {e}")
    bad = []
    for fp in files:
        name = os.path.basename(fp)
        if not any(p.search(name) for p in pats):
            bad.append(f"{os.path.relpath(fp, root)}: name matches none of "
                       f"{len(pats)} allowed pattern(s)")
    return result(spec, "filename_pattern",
                  "pass" if not bad else spec.get("severity", "warn"),
                  f"{len(spec['patterns'])} pattern(s)", violations=bad,
                  n_checked=len(files))


def probe_staleness_flag(root, spec):
    """Time-decay debt: files matching `where` that are older than
    max_age_days get flagged (e.g. verification_status still 'unverified'
    a month after creation). Age comes from `date_field` in frontmatter when
    present, else file mtime — so it works on schemas with no date field.
    Flags, not verdicts: default severity is warn."""
    files = rglob(root, spec["glob"])
    if not files:
        return result(spec, "staleness_flag", "skipped", "no files match")
    max_age = float(spec["max_age_days"])
    date_field = spec.get("date_field")
    where = spec.get("where", {})
    now = dt.datetime.now()
    bad, n = [], 0
    for fp in files:
        fm, _ = read_frontmatter(fp)
        if fm is None or not fm_match(fm, where):
            continue
        n += 1
        when, basis = None, "mtime"
        if date_field and date_field in fm:
            when = parse_when(fm.get(date_field))
            basis = date_field
        if when is None:
            try:
                when = dt.datetime.fromtimestamp(os.path.getmtime(fp))
                basis = "mtime"
            except OSError:
                continue
        age = (now - when).days
        if age > max_age:
            bad.append(f"{os.path.relpath(fp, root)}: {age}d old (by {basis}) "
                       f"> {int(max_age)}d, still matches {where}")
    return result(spec, "staleness_flag",
                  "pass" if not bad else spec.get("severity", "warn"),
                  f"max_age={int(max_age)}d where={where}", violations=bad, n_checked=n)


def _walk_refs(value, path):
    """Yield scalar leaves reached by walking `path` (list of keys) through
    nested dicts, expanding lists at every level — so `edges.to` collects
    edges[*].to and a plain `builds_on` collects the whole list/scalar."""
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_refs(v, path)
        return
    if path:
        if isinstance(value, dict):
            yield from _walk_refs(value.get(path[0]), path[1:])
        return
    if isinstance(value, (str, int, float)):
        yield str(value)


def probe_ref_integrity(root, spec):
    """Frontmatter reference integrity: node ids referenced by `field` (a
    dotted path or list of paths, e.g. `edges.to` or `builds_on`; values may
    be scalars or lists) must exist as `id_field` (default: id) in the corpus
    matched by `target_glob` (default: same as `glob`). A dangling reference
    is a violation. `glob`/`target_glob` accept a string or a list of globs;
    `ignore` (fnmatch patterns) skips expected-dangling ids such as template
    placeholders. All names and globs come from the manifest — the engine
    knows nothing about any schema."""
    fields = spec.get("field") or []
    fields = fields if isinstance(fields, list) else [fields]
    if not fields:
        return result(spec, "ref_integrity", "skipped", "no field configured")
    globs = spec["glob"] if isinstance(spec["glob"], list) else [spec["glob"]]
    tglobs = spec.get("target_glob", globs)
    tglobs = tglobs if isinstance(tglobs, list) else [tglobs]
    id_field = spec.get("id_field", "id")
    ignore = spec.get("ignore") or []

    ids, no_id = set(), 0
    for fp in sorted({p for g in tglobs for p in rglob(root, g)}):
        fm, _ = read_frontmatter(fp)
        if fm is None or fm.get(id_field) is None:
            no_id += 1
            continue
        ids.add(str(fm[id_field]))
    if not ids:
        return result(spec, "ref_integrity", "skipped",
                      f"no ids under target_glob (id_field '{id_field}')")
    files = sorted({p for g in globs for p in rglob(root, g)})
    if not files:
        return result(spec, "ref_integrity", "skipped", "no files match")
    bad, n, n_refs = [], 0, 0
    for fp in files:
        fm, _ = read_frontmatter(fp)
        if fm is None:
            continue
        n += 1
        rel = os.path.relpath(fp, root)
        for f in fields:
            path = str(f).split(".")
            for ref in _walk_refs(fm.get(path[0]), path[1:]):
                n_refs += 1
                if ref in ids or any(fnmatch.fnmatch(ref, p) for p in ignore):
                    continue
                bad.append(f"{rel}: {f} -> '{ref}' not found in target corpus")
    return result(spec, "ref_integrity",
                  "pass" if not bad else spec.get("severity", "warn"),
                  f"{n_refs} refs across {n} files vs {len(ids)} ids"
                  + (f" ({no_id} target files without '{id_field}')" if no_id else ""),
                  violations=bad, n_checked=n)


def _tier_at(history, task, when, current):
    """Replay the allocation history: the tier `task` was assigned at `when`
    (ISO string compare). A stamp is judged against the table of ITS day —
    yesterday's compliant work does not become a violation because the table
    moved on."""
    changes = sorted((h for h in history
                      if str(h.get("task")) == task and h.get("at")),
                     key=lambda h: str(h["at"]))
    if not changes or not when:
        return current
    tier = str(changes[0].get("from", current))
    for h in changes:
        if str(h["at"]) <= str(when):
            tier = str(h.get("to", tier))
    return tier


def probe_scope_guard(root, spec):
    """Wizard-mode guard (the over-delivery half of the problem): agents that
    'just do stuff' create files nobody asked for, and checks that only ask
    'is the required output present?' never notice. Every file under `within`
    (default: everything) must match at least one `expected` glob — anything
    else is out-of-scope output for a human to glance at. Pair with --diff:
    the baseline absorbs the existing tree, so only NEWLY appearing strays
    are reported. All globs come from the manifest; the engine has no idea
    what a project's shape should be."""
    expected = spec.get("expected") or []
    expected = expected if isinstance(expected, list) else [expected]
    if not expected:
        return result(spec, "scope_guard", "skipped", "no expected globs configured")
    within = spec.get("within", "**/*")
    within = within if isinstance(within, list) else [within]
    ignore = spec.get("ignore") or [".git/**", ".steward/**", ".claude/**",
                                    "__pycache__/**", "node_modules/**"]
    bad, n = [], 0
    for dirpath, _dn, fns in os.walk(root):
        for fn in fns:
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            if not any(_glob_match(rel, g) for g in within):
                continue
            if any(_glob_match(rel, g) for g in ignore):
                continue
            n += 1
            if not any(_glob_match(rel, g) for g in expected):
                bad.append(f"{rel}: outside every expected area — nobody asked "
                           f"for this file; keep it (then add its home to "
                           f"`expected`) or remove it")
    return result(spec, "scope_guard",
                  "pass" if not bad else spec.get("severity", "warn"),
                  f"{n} files vs {len(expected)} expected area(s)",
                  violations=bad, n_checked=n)


def probe_allocation_compliance(root, spec):
    """R2 loop-one sensor (observe-first): declared tier per task class vs the
    model actually recorded in each artifact's provenance stamp
    (produced_by/task frontmatter, see `steward stamp`).

    Allocation table comes from `allocation_file` (an .allocation.yaml inside
    the target, read-only) and/or inline `tasks: {task_id: tier}`.
    `tier_patterns: {tier: [glob, ...]}` maps tiers to model-name patterns —
    supplied by the manifest/allocation file, never hardcoded in the engine.
    Stamps carrying `stamped_at` are compared against the tier the allocation
    HISTORY records for that moment, not today's table.
    Unstamped files are counted, not flagged (stamping adoption is gradual)."""
    table, tier_patterns, history = {}, {}, []
    alloc_rel = spec.get("allocation_file")
    if alloc_rel:
        ap = alloc_rel if os.path.isabs(alloc_rel) else os.path.join(root, alloc_rel)
        if os.path.exists(ap):
            try:
                with open(ap, encoding="utf-8") as f:
                    alloc = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                return result(spec, "allocation_compliance", "skipped",
                              f"allocation file unparsable: {str(e).splitlines()[0]}")
            for t in alloc.get("tasks", []) or []:
                if isinstance(t, dict) and "id" in t and "tier" in t:
                    table[str(t["id"])] = str(t["tier"])
            tier_patterns.update(alloc.get("tier_patterns") or {})
            history = alloc.get("history") or []
    for k, v in (spec.get("tasks") or {}).items():
        table[str(k)] = str(v)
    tier_patterns.update(spec.get("tier_patterns") or {})
    if not table:
        return result(spec, "allocation_compliance", "skipped",
                      "no allocation table (allocation_file missing and no inline tasks)")
    globs = spec["glob"] if isinstance(spec["glob"], list) else [spec["glob"]]
    files = sorted({p for g in globs for p in rglob(root, g)})
    if not files:
        return result(spec, "allocation_compliance", "skipped", "no files match")
    bad, n, unstamped = [], 0, 0
    for fp in files:
        fm, _ = read_frontmatter(fp)
        if fm is None:
            continue
        task = fm.get("task")
        if task is None:
            unstamped += 1
            continue
        n += 1
        rel = os.path.relpath(fp, root)
        task = str(task)
        model = fm.get("produced_by")
        if task not in table:
            bad.append(f"{rel}: task '{task}' not in allocation table")
            continue
        if not model:
            bad.append(f"{rel}: task stamped but produced_by missing")
            continue
        # a table transition leaves ambiguity in both directions: work that
        # matched the table of ITS day stays compliant after a promote, and
        # early adoption that the table later ratified is not retroactively
        # guilty — only a stamp matching NEITHER tier is a real violation
        then = _tier_at(history, task, fm.get("stamped_at"), table[task])
        ok, missing = False, []
        for tier in dict.fromkeys((then, table[task])):
            pats = tier_patterns.get(tier)
            if not pats:
                missing.append(tier)
                continue
            if any(fnmatch.fnmatch(str(model).lower(), str(p).lower()) for p in pats):
                ok = True
                break
        if missing and not ok:
            bad.append(f"{rel}: no tier_patterns entry for tier(s) {missing}")
            continue
        if not ok:
            span = (f"'{then}' at stamp time / '{table[task]}' now"
                    if then != table[task] else f"'{table[task]}'")
            bad.append(f"{rel}: task '{task}' declared tier {span}, "
                       f"got produced_by='{model}'")
    return result(spec, "allocation_compliance",
                  "pass" if not bad else spec.get("severity", "warn"),
                  f"table={len(table)} tasks, stamped={n}, unstamped={unstamped}",
                  violations=bad, n_checked=n)


PROBES = {
    "cmd": probe_cmd,
    "jsonl_wellformed": probe_jsonl_wellformed,
    "frontmatter_required": probe_frontmatter_required,
    "single_source_cap": probe_single_source_cap,
    "field_value_rule": probe_field_value_rule,
    "bash_syntax": probe_bash_syntax,
    "csv_required_columns": probe_csv_required_columns,
    "tsv_wellformed": probe_tsv_wellformed,
    "file_exists": probe_file_exists,
    "filename_pattern": probe_filename_pattern,
    "staleness_flag": probe_staleness_flag,
    "ref_integrity": probe_ref_integrity,
    "scope_guard": probe_scope_guard,
    "allocation_compliance": probe_allocation_compliance,
}

# ---------------------------------------------------------------- metrics

def metric_jsonl_count(root, spec):
    path = os.path.join(root, spec["path"])
    if not os.path.exists(path):
        return None
    where = spec.get("where", {})
    exclude = spec.get("exclude", {})
    n = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if all(obj.get(k) == v for k, v in where.items()) and \
               not any(obj.get(k) == v for k, v in exclude.items()):
                n += 1
    return n


def metric_frontmatter_count(root, spec):
    files = rglob(root, spec["glob"])
    where = spec.get("where", {})  # value "!null" => must be non-null
    n = 0
    for fp in files:
        fm, _ = read_frontmatter(fp)
        if fm is not None and fm_match(fm, where):
            n += 1
    return n


def metric_file_count(root, spec):
    return len(rglob(root, spec["glob"]))


def metric_git_commits(root, spec):
    days = int(spec.get("days", 7))
    p = subprocess.run(
        ["git", "log", f"--since={days} days ago", "--oneline"],
        cwd=root, capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return len([ln for ln in p.stdout.splitlines() if ln.strip()])


METRICS = {
    "jsonl_count": metric_jsonl_count,
    "frontmatter_count": metric_frontmatter_count,
    "file_count": metric_file_count,
    "git_commits": metric_git_commits,
}

# ---------------------------------------------------------------- rulebook (M5)

def eval_rulebook(mf, root):
    """M5 rule coverage: of the rules written down in the project's own docs
    (inventoried in the manifest `rulebook:` section), what fraction has an
    executable counterpart? Each entry: {rule, source, covered_by | judgment_only}.
    `form: probe` (default) — covered_by must name existing probe ids;
    `form: test` — covered_by must name paths that exist under root.
    A broken covered_by pointer counts as drift (rule/checker desync), not
    as coverage."""
    rb = mf.get("rulebook") or []
    if not rb:
        return None
    probe_ids = {s.get("id") for s in mf.get("probes", [])}
    covered, judgment, uncovered, drift = 0, 0, [], []
    for r in rb:
        label = f"{r.get('rule', '?')} (source: {r.get('source', '?')})"
        if r.get("judgment_only"):
            judgment += 1
            continue
        cov = r.get("covered_by") or []
        if isinstance(cov, str):
            cov = [cov]
        if not cov:
            uncovered.append(label)
            continue
        form = r.get("form", "probe")
        missing = [c for c in cov
                   if (form == "probe" and c not in probe_ids)
                   or (form == "test" and not os.path.exists(os.path.join(root, c)))]
        if missing:
            drift.append(f"{label}: covered_by {missing} not found")
            uncovered.append(label)
        else:
            covered += 1
    total = len(rb)
    return {
        "rules_total": total, "rules_covered": covered,
        "rules_judgment_only": judgment,
        "rule_coverage": round(covered / total, 4) if total else None,
        "uncovered": uncovered, "drift": drift,
    }

# ---------------------------------------------------------------- rule conflicts
# docs/01 §3-2 "conflict witness": two rules governing the same field on
# overlapping files with incompatible parameters. These are exactly the
# decisions the machine must NOT make — each conflict names both probe ids and
# the disagreement, and is routed to the user. Everything else stays automatic.

def _globs_overlap(a, b):
    return a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)


def detect_rule_conflicts(mf):
    probes = mf.get("probes", [])
    conflicts = []
    seen = {}
    for s in probes:
        pid = s.get("id", "?")
        if pid in seen:
            conflicts.append(f"duplicate probe id '{pid}' — second definition "
                             f"shadows nothing but confuses provenance; rename one")
        seen[pid] = s
    fvr = [s for s in probes if s.get("type") == "field_value_rule"]
    for i, a in enumerate(fvr):
        for b in fvr[i + 1:]:
            if a.get("field") == b.get("field") and _globs_overlap(a.get("glob", ""), b.get("glob", "")):
                sa, sb = set(a.get("allowed", [])), set(b.get("allowed", []))
                if sa != sb:
                    conflicts.append(
                        f"probes '{a['id']}' and '{b['id']}' both constrain field "
                        f"'{a.get('field')}' on overlapping files "
                        f"('{a.get('glob')}' vs '{b.get('glob')}') with different "
                        f"allowed sets {sorted(sa)} vs {sorted(sb)} — same artifact "
                        f"can pass one and fail the other; reconcile in the manifest")
    ssc = [s for s in probes if s.get("type") == "single_source_cap"]
    for i, a in enumerate(ssc):
        for b in ssc[i + 1:]:
            if _globs_overlap(a.get("glob", ""), b.get("glob", "")):
                if (a.get("default_cap", 0.5) != b.get("default_cap", 0.5)
                        or (a.get("class_caps") or {}) != (b.get("class_caps") or {})):
                    conflicts.append(
                        f"probes '{a['id']}' and '{b['id']}' cap confidence on "
                        f"overlapping files ('{a.get('glob')}' vs '{b.get('glob')}') "
                        f"with different caps (default {a.get('default_cap', 0.5)} vs "
                        f"{b.get('default_cap', 0.5)}, class_caps "
                        f"{a.get('class_caps') or {}} vs {b.get('class_caps') or {}}) "
                        f"— reconcile in the manifest")
    return conflicts

# ---------------------------------------------------------------- state / diff

def load_state(state_file):
    try:
        with open(state_file, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def check_source_quotes(mf, root):
    """Anti-transcription-error mechanism: a probe that encodes numbers copied
    from an authoritative doc can carry `source_file` (path under root) and
    `source_quote` (verbatim snippet). The quote sits next to the parameters
    at review time, and the engine verifies it still exists in the doc — the
    day the doc changes, the quote breaks and the desync surfaces here instead
    of as a false-positive storm. Returns drift findings (rule-problem tier:
    these must reach the user)."""
    out = []
    for spec in mf.get("probes", []) or []:
        quote, sf = spec.get("source_quote"), spec.get("source_file")
        if not quote:
            continue
        pid = spec.get("id", "?")
        if not sf:
            out.append(f"SOURCE DRIFT: probe '{pid}' has source_quote but no "
                       f"source_file — nothing to verify it against")
            continue
        path = sf if os.path.isabs(sf) else os.path.join(root, sf)
        if not os.path.exists(path):
            out.append(f"SOURCE DRIFT: probe '{pid}' source_file '{sf}' does "
                       f"not exist — the rule this probe enforces may be gone")
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                doc = " ".join(f.read().split())
        except OSError as e:
            out.append(f"SOURCE DRIFT: probe '{pid}' source_file unreadable ({e})")
            continue
        for q in (quote if isinstance(quote, list) else [quote]):
            if " ".join(str(q).split()) not in doc:
                out.append(f"SOURCE DRIFT: probe '{pid}' quote no longer appears "
                           f"in '{sf}' — the doc changed; re-verify the probe's "
                           f"parameters against it (quote: \"{str(q)[:80]}...\")")
    return out


def record_fixes(sdir, project, resolved):
    """Append resolved violations to fixes.jsonl — the tool's own scoreboard.
    'Resolved' means the violation stopped appearing: fixed in the target, or
    the rule itself was corrected after review; both are the loop working."""
    if not resolved:
        return
    path = os.path.join(sdir, "fixes.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for pid, items in sorted(resolved.items()):
            f.write(json.dumps({"ts": now_iso(), "project": project,
                                "probe": pid, "n": len(items),
                                "examples": items[:3]},
                               ensure_ascii=False) + "\n")


def diff_violations(prev, cur):
    """prev/cur: {probe_id: [violation, ...]}. Returns (new, resolved)."""
    new, resolved = {}, {}
    for pid, vs in cur.items():
        pv = set(prev.get(pid, []))
        added = [v for v in vs if v not in pv]
        if added:
            new[pid] = added
    for pid, vs in prev.items():
        cv = set(cur.get(pid, []))
        gone = [v for v in vs if v not in cv]
        if gone:
            resolved[pid] = gone
    return new, resolved

# ---------------------------------------------------------------- runner

def run(manifest_path, root_override=None, out_override=None,
        diff=False, state_dir=None, exit_new=False):
    with open(manifest_path, encoding="utf-8") as f:
        mf = yaml.safe_load(f)
    for issue in validate_manifest(mf):  # pre-flight; warnings only (fail-open)
        print(f"[steward] manifest: {issue}", file=sys.stderr)
    root = os.path.abspath(root_override or mf["root"])
    mode = mf.get("mode", "apply")  # apply | readonly
    project = mf.get("project", os.path.basename(root))
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    sdir = os.path.abspath(state_dir or os.path.join(os.getcwd(), STATE_DIR_DEFAULT))
    # per-run artifacts live under the steward's own state dir — never inside
    # the installed package (site-packages is not a writable workspace)
    out_dir = os.path.abspath(out_override or os.path.join(sdir, "runs", f"{project}-{ts}"))

    # zero-pollution guard: out/state dirs must not be inside a readonly target
    if mode == "readonly":
        for d, name in ((out_dir, "out dir"), (sdir, "state dir")):
            if os.path.commonpath([d, root]) == root:
                print(f"FATAL: readonly target but {name} inside target: {d}",
                      file=sys.stderr)
                sys.exit(2)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(sdir, exist_ok=True)

    results = []
    for spec in mf.get("probes", []):
        ptype = spec["type"]
        if ptype == "cmd" and mode == "readonly" and not spec.get("readonly_safe"):
            results.append(result(spec, "cmd", "skipped",
                                  "cmd probe refused in readonly mode"))
            continue
        fn = PROBES.get(ptype)
        if fn is None:
            results.append(result(spec, ptype, "skipped", "unknown probe type"))
            continue
        try:
            results.append(fn(root, spec))
        except Exception as e:  # a verifier must not crash the pipeline
            results.append(result(spec, ptype, "skipped", f"probe error: {e}"))

    metrics = {}
    for spec in mf.get("metrics", []):
        fn = METRICS.get(spec["type"])
        if fn is None:
            metrics[spec["id"]] = None
            continue
        try:
            metrics[spec["id"]] = fn(root, spec)
        except Exception:
            metrics[spec["id"]] = None

    # derived metrics (simple expressions: "a / (a + b)")
    for spec in mf.get("derived", []):
        try:
            metrics[spec["id"]] = round(
                eval(spec["expr"], {"__builtins__": {}}, dict(metrics)), 4)  # noqa: S307
        except Exception:
            metrics[spec["id"]] = None

    # rulebook coverage (M5)
    coverage = eval_rulebook(mf, root)
    if coverage:
        for k in ("rules_total", "rules_covered", "rules_judgment_only", "rule_coverage"):
            metrics[k] = coverage[k]

    # rule conflicts + source drift (the things that MUST reach the user)
    conflicts = detect_rule_conflicts(mf) + check_source_quotes(mf, root)
    metrics["rule_conflicts"] = len(conflicts)

    # spend summary (if a usage ledger exists in the state dir)
    alloc = None
    for spec in mf.get("probes", []):
        af = spec.get("allocation_file")
        if spec.get("type") == "allocation_compliance" and af:
            ap = af if os.path.isabs(af) else os.path.join(root, af)
            if os.path.exists(ap):
                try:
                    alloc = alloc_mod.load_allocation(ap)
                except yaml.YAMLError:
                    pass
    ledger = alloc_mod.read_ledger(sdir, project=project)
    savings = alloc_mod.compute_savings(ledger, alloc) if ledger else None

    # diff vs last check (state lives OUTSIDE the target — zero-pollution)
    state_file = os.path.join(sdir, "state.json")
    state = load_state(state_file)
    prev = (state.get("projects", {}).get(project, {})).get("violations", {})
    prev_ran_at = (state.get("projects", {}).get(project, {})).get("ran_at")
    cur = {r["probe"]: r["violations"] for r in results if r["violations"]}
    new_v, resolved_v = diff_violations(prev, cur)
    if prev_ran_at:  # don't count the very first baseline as "fixes"
        record_fixes(sdir, project, resolved_v)
    probe_meta = {str(s.get("id")): s for s in mf.get("probes", []) or []}
    state.setdefault("projects", {})[project] = {
        "ran_at": now_iso(), "root": root, "violations": cur,
        "metrics": metrics, "conflicts": conflicts,
        "coverage": ({k: coverage[k] for k in ("uncovered", "drift")} if coverage else None),
        # per-probe stats + fix guidance so the report can render the
        # "authorize fixes per category" view without re-reading the manifest
        "probe_stats": [{
            "probe": r["probe"], "type": r["type"], "status": r["status"],
            "n_violations": r["n_violations"], "n_checked": r["n_checked"],
            "fix": str(probe_meta.get(r["probe"], {}).get("fix", "")),
            "fixable_by": str(probe_meta.get(r["probe"], {}).get("fixable_by", "")),
            "source": r.get("source", ""),
        } for r in results],
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    # persist
    with open(os.path.join(out_dir, "probe_results.jsonl"), "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({**r, "violations": r["violations"][:50]},
                               ensure_ascii=False) + "\n")  # cap spam
    with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump({"project": project, "root": root, "mode": mode,
                   "ran_at": now_iso(), "metrics": metrics}, f,
                  ensure_ascii=False, indent=2)

    # report — ordered by what the operator needs first:
    # savings -> rule problems (needs their decision) -> what changed -> coverage -> detail
    lines = [f"# agent-steward report: {project}",
             f"- ran_at: {now_iso()}  mode: **{mode}**  root: `{root}`",
             "- engine: steward V1 (L0 deterministic only; no LLM calls)"]

    if savings:
        lines += ["", "## Spend (estimated savings so far)", ""]
        lines += alloc_mod.spend_summary_lines(savings)
        if savings["esc_rate"] is not None:
            lines.append(f"- trade-off: {savings['escalations']} escalations "
                         f"(rate {savings['esc_rate']}) — each cost one lower-tier redo")

    lines += ["", "## Rule problems (needs your decision — the steward cannot pick a side)", ""]
    if conflicts:
        lines += [f"- CONFLICT: {c}" for c in conflicts]
    if coverage and coverage["drift"]:
        lines += [f"- DRIFT: {d} — the doc or the manifest moved; realign them"
                  for d in coverage["drift"]]
    if not conflicts and not (coverage and coverage["drift"]):
        lines.append("(none)")

    def violation_block(title, vmap, cap=20):
        blk = ["", title, ""]
        if not vmap:
            blk.append("(none)")
            return blk
        for pid in sorted(vmap):
            blk.append(f"### {pid}")
            blk += [f"- {v}" for v in vmap[pid][:cap]]
            if len(vmap[pid]) > cap:
                blk.append(f"- ...(+{len(vmap[pid]) - cap} more, see probe_results.jsonl)")
            blk.append("")
        return blk

    if diff:
        base = f"since last check ({prev_ran_at})" if prev_ran_at \
            else "(no previous state — first check, everything counts as new)"
        lines += violation_block(f"## New violations {base}", new_v)
        lines += violation_block("## Resolved since last check", resolved_v)
        lines += ["", f"(unchanged violations suppressed by --diff: "
                      f"{sum(len(v) for v in cur.values()) - sum(len(v) for v in new_v.values())})"]
    else:
        lines += violation_block(
            "## Violations (residue for the adjudicator — the steward never decides)", cur)

    if coverage:
        lines += ["", "## Rule coverage (M5)", "",
                  f"- rules in rulebook: {coverage['rules_total']}",
                  f"- with executable counterpart: {coverage['rules_covered']}",
                  f"- judgment-only (explicitly flagged): {coverage['rules_judgment_only']}",
                  f"- **coverage: {coverage['rule_coverage']}**"]
        if coverage["uncovered"]:
            lines += ["", "Uncovered rules (candidates for the next probe):"]
            lines += [f"- {u}" for u in coverage["uncovered"]]

    lines += ["", "## Probes (L0 deterministic floor)", "",
              "| probe | status | checked | violations | source | detail |",
              "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['probe']} | {r['status']} | {r['n_checked']} "
                     f"| {r['n_violations']} | {r['source'][:40]} | {r['detail'][:60]} |")

    lines += ["", "## Baseline metrics", "", "| metric | value |", "|---|---|"]
    for k, v in metrics.items():
        lines.append(f"| {k} | {v} |")
    with open(os.path.join(out_dir, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # console summary
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    n_new = sum(len(v) for v in new_v.values())
    n_res = sum(len(v) for v in resolved_v.values())
    diff_note = f" new=+{n_new} resolved=-{n_res}" if diff else ""
    cov_note = f" coverage={metrics['rule_coverage']}" if coverage else ""
    conf_note = f" CONFLICTS={len(conflicts)}" if conflicts else ""
    saved_note = (f" saved={savings['saved_vs_top_pct']}%"
                  if savings and savings["saved_vs_top_pct"] is not None else "")
    print(f"[steward] {project} mode={mode} probes={counts}{diff_note}{cov_note}"
          f"{conf_note}{saved_note} out={out_dir}")

    # hook contract: exit 2 + new violations on stderr, so a Claude Code Stop
    # hook can feed them straight back to the agent for self-repair
    if exit_new and n_new:
        print(f"[steward] {n_new} new violation(s) since last check:", file=sys.stderr)
        for pid in sorted(new_v):
            for v in new_v[pid][:20]:
                print(f"  {pid}: {v}", file=sys.stderr)
            if len(new_v[pid]) > 20:
                print(f"  {pid}: ...(+{len(new_v[pid]) - 20} more)", file=sys.stderr)
        print(f"[steward] full report: {os.path.join(out_dir, 'REPORT.md')}",
              file=sys.stderr)
        sys.exit(2)
    return out_dir

# ---------------------------------------------------------------- stamp (R1)

def stamp_file(path, fields):
    """Insert/update provenance keys in a file's frontmatter, preserving all
    other lines verbatim (no YAML re-dump — comments and ordering survive).
    Creates a frontmatter block if the file has none."""
    with open(path, encoding="utf-8") as f:
        text = f.read()

    def fmt(v):
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v)
        return json.dumps(s) if re.search(r"[:#'\"{}\[\],&*?|>%@`]", s) or s != s.strip() else s

    m = FM_RE.match(text)
    if m:
        block_lines = m.group(1).split("\n")
        for k, v in fields.items():
            pat = re.compile(rf"^{re.escape(k)}\s*:")
            for i, ln in enumerate(block_lines):
                if pat.match(ln):
                    block_lines[i] = f"{k}: {fmt(v)}"
                    break
            else:
                block_lines.append(f"{k}: {fmt(v)}")
        new_text = "---\n" + "\n".join(block_lines) + "\n---\n" + text[m.end():]
    else:
        block = "\n".join(f"{k}: {fmt(v)}" for k, v in fields.items())
        new_text = f"---\n{block}\n---\n" + text
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)


def cmd_stamp(args):
    fields = {"produced_by": args.produced_by, "task": args.task}
    if args.round is not None:
        fields["round"] = args.round
    fields["stamped_at"] = now_iso()
    for path in args.files:
        if not os.path.exists(path):
            print(f"[steward] stamp: no such file: {path}", file=sys.stderr)
            return 1
        stamp_file(path, fields)
        print(f"[steward] stamped {path} "
              f"(produced_by={args.produced_by} task={args.task} round={args.round})")
    return 0

# ---------------------------------------------------------------- ledger (R1)

def cmd_log_task(args):
    sdir = os.path.abspath(args.state_dir or os.path.join(os.getcwd(), STATE_DIR_DEFAULT))
    os.makedirs(sdir, exist_ok=True)
    entry = {"ts": now_iso(), "task": args.task, "tier": args.tier}
    for k in ("model", "est_tokens", "result", "project", "note",
              "canary", "pair", "quality"):
        v = getattr(args, k.replace("-", "_"), None)
        if v is not None:
            entry[k] = v
    if entry.get("quality") and entry.get("canary") != "shadow":
        print("[steward] warning: --quality records the shadow-vs-primary "
              "verdict and belongs on the shadow entry (--canary shadow)",
              file=sys.stderr)
    path = os.path.join(sdir, "usage_ledger.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[steward] logged to {path}: {json.dumps(entry, ensure_ascii=False)}")
    # observe-only data-quality warning: the ledger is append-only, so a
    # tier/model contradiction is flagged at write time (while the logging
    # agent can still fix its next entry) but the row is kept as-is
    apath = args.allocation or ".allocation.yaml"
    if entry.get("model") and os.path.exists(apath):
        try:
            alloc = alloc_mod.load_allocation(apath)
        except yaml.YAMLError:
            alloc = None
        if alloc:
            mism, unknown = alloc_mod.ledger_mismatches(alloc, [entry])
            for m in mism:
                print(f"[steward] warning: tier '{m['tier']}' but model "
                      f"'{m['model']}' matches tier(s) "
                      f"{', '.join(m['matches_tiers'])} per tier_patterns — "
                      f"entry kept (append-only); if mis-logged, correct the "
                      f"next entry, do not edit the ledger", file=sys.stderr)
            for u in unknown:
                print(f"[steward] warning: model '{u['model']}' matches no "
                      f"tier_patterns entry — new model name or a typo? "
                      f"(use the dispatcher-declared model id)", file=sys.stderr)
    return 0

# ---------------------------------------------------------------- route (V2)

def cmd_route(args):
    """L2 sorter + optional L1 judge: turn the latest check's violations into
    a sorted attention queue. The queue proposes an order; it never decides."""
    with open(args.manifest, encoding="utf-8") as f:
        mf = yaml.safe_load(f)
    project = args.project or mf.get("project")
    sdir = os.path.abspath(args.state_dir or os.path.join(os.getcwd(), STATE_DIR_DEFAULT))
    state = load_state(os.path.join(sdir, "state.json"))
    pstate = state.get("projects", {}).get(project)
    if not pstate:
        print(f"[steward] no check results for project '{project}' in {sdir} — "
              f"run `steward check` first", file=sys.stderr)
        return 1
    prev = route_mod.load_queue(sdir)
    items = route_mod.build_queue(mf, pstate.get("violations", {}), prev, now_iso())
    if args.judge:
        _, msg = route_mod.run_judge(items, mf, model=args.model)
        print(f"[steward] L1 judge: {msg}")
    path = route_mod.save_queue(sdir, items)
    pending = sorted((it for it in items.values() if it["status"] == "pending"),
                     key=lambda x: -x["score"])
    m4, worth, not_worth = route_mod.m4_precision(items)
    print(f"[steward] attention queue: {len(pending)} pending "
          f"(checked at {pstate.get('ran_at', '?')}) -> {path}")
    for it in pending[:args.top]:
        reason = f"  <- {it['judge']['reason']}" if it.get("judge") else ""
        print(f"  {it['id']}  {it['score']:>6}  [{it['probe']}] {it['text'][:100]}{reason}")
    if len(pending) > args.top:
        print(f"  ... {len(pending) - args.top} more in {path}")
    if m4 is not None:
        print(f"[steward] M4 residue precision so far: {m4} "
              f"({worth} worth / {not_worth} not worth) — verdicts via "
              f"`steward approve <id> --verdict worth|not-worth`")
    return 0


def cmd_approve(args):
    """Adjudicator feedback per queue item — the M4 calibration stream.
    The human's verdict is data about the SORTER, never about the artifact."""
    sdir = os.path.abspath(args.state_dir or os.path.join(os.getcwd(), STATE_DIR_DEFAULT))
    items = route_mod.load_queue(sdir)
    it = items.get(args.item)
    if not it:
        print(f"[steward] no queue item '{args.item}' — run `steward route` first",
              file=sys.stderr)
        return 1
    it["status"] = "worth" if args.verdict == "worth" else "not_worth"
    if args.note:
        it["verdict_note"] = args.note
    it["adjudicated_at"] = now_iso()
    route_mod.save_queue(sdir, items)
    m4, worth, not_worth = route_mod.m4_precision(items)
    print(f"[steward] {args.item} -> {it['status']}; M4 residue precision: "
          f"{m4} ({worth} worth / {not_worth} not worth)")
    return 0

# ------------------------------------------------------- manifest validation

# per-type parameter contract, kept next to the registry it validates.
# COMMON keys are legal on every probe.
COMMON_PROBE_KEYS = {"id", "type", "severity", "on_fail", "source",
                     "source_file", "source_quote", "risk_weight", "route",
                     "readonly_safe", "timeout", "fix", "fixable_by"}
PROBE_PARAMS = {
    "cmd": ({"cmd"}, set()),
    "jsonl_wellformed": ({"path"}, set()),
    "frontmatter_required": ({"glob", "required"}, set()),
    "single_source_cap": ({"glob"}, {"default_cap", "class_caps",
                                     "class_field", "exempt_field"}),
    "field_value_rule": ({"glob", "field", "allowed"}, {"require_present"}),
    "bash_syntax": ({"glob"}, set()),
    "csv_required_columns": ({"glob", "columns"}, set()),
    "tsv_wellformed": ({"path"}, {"min_cols"}),
    "file_exists": ({"path"}, set()),
    "filename_pattern": ({"glob", "patterns"}, set()),
    "staleness_flag": ({"glob", "max_age_days"}, {"where", "date_field"}),
    "ref_integrity": ({"glob", "field"}, {"target_glob", "id_field", "ignore"}),
    "scope_guard": ({"expected"}, {"within", "ignore"}),
    "allocation_compliance": ({"glob"}, {"allocation_file", "tasks",
                                         "tier_patterns"}),
}


def validate_manifest(mf):
    """Pre-flight schema check: catch the mistakes people (the author
    included, twice) actually make — wrong parameter names, unknown probe
    types, bad regexes — at read time with a did-you-mean, instead of at
    run time as a cryptic 'probe error'. Warnings only: fail-open holds."""
    import difflib
    problems = []
    for spec in mf.get("probes", []) or []:
        pid = spec.get("id", "?")
        ptype = spec.get("type")
        if ptype not in PROBES:
            near = difflib.get_close_matches(str(ptype), PROBES, n=1)
            problems.append(f"probe '{pid}': unknown type '{ptype}'"
                            + (f" — did you mean '{near[0]}'?" if near else
                               f" (known: {', '.join(sorted(PROBES))})"))
            continue
        required, optional = PROBE_PARAMS.get(ptype, (set(), set()))
        keys = set(spec.keys())
        legal = required | optional | COMMON_PROBE_KEYS
        for m in sorted(required - keys):
            hint = difflib.get_close_matches(m, keys - legal, n=1)
            problems.append(f"probe '{pid}' ({ptype}): missing required "
                            f"parameter '{m}'"
                            + (f" — you wrote '{hint[0]}', did you mean '{m}'?"
                               if hint else ""))
        for u in sorted(keys - legal):
            near = difflib.get_close_matches(u, sorted(legal), n=1)
            problems.append(f"probe '{pid}' ({ptype}): unknown parameter '{u}'"
                            + (f" — did you mean '{near[0]}'?" if near else ""))
        if ptype == "filename_pattern":
            for p in spec.get("patterns") or []:
                try:
                    re.compile(str(p))
                except re.error as e:
                    problems.append(f"probe '{pid}': bad regex '{p}' ({e})")
    for r in mf.get("rulebook", []) or []:
        if not r.get("rule"):
            problems.append("rulebook entry without a 'rule' text")
    return problems

# ---------------------------------------------------------------- status (T1)

def cmd_status():
    """No-args entry point. The tool's whole point is routing attention;
    its own CLI starts by telling you where you are and what to do next."""
    cwd = os.getcwd()
    print(f"agent-steward — {cwd}\n")
    manifests = []
    import glob as globmod
    for pat in ("*.yaml", "*.yml", "examples/*.yaml"):
        for p in sorted(globmod.glob(pat)):
            try:
                with open(p, encoding="utf-8") as f:
                    mf = yaml.safe_load(f)
                if isinstance(mf, dict) and "probes" in mf:
                    manifests.append((p, mf))
            except Exception:  # noqa: BLE001 — status must never crash
                continue
    if manifests:
        for p, mf in manifests:
            issues = validate_manifest(mf)
            flag = f"  [!] {len(issues)} manifest issue(s) — run `steward check` to see them" if issues else ""
            print(f"manifest: {p} (project: {mf.get('project', '?')}, "
                  f"{len(mf.get('probes', []))} probes){flag}")
            for i in issues[:3]:
                print(f"    - {i}")
    else:
        print("manifest: none found here")
    sdir = os.path.join(cwd, STATE_DIR_DEFAULT)
    state = load_state(os.path.join(sdir, "state.json"))
    projects = state.get("projects", {})
    for name, p in sorted(projects.items()):
        nv = sum(len(v) for v in (p.get("violations") or {}).values())
        print(f"state: {name} — last check {p.get('ran_at', '?')}, "
              f"{nv} violation(s), M5 {p.get('metrics', {}).get('rule_coverage', '?')}")
    ledger = alloc_mod.read_ledger(sdir) if os.path.isdir(sdir) else []
    if ledger:
        print(f"ledger: {len(ledger)} entries, last {ledger[-1].get('ts', '?')}")
    qitems = route_mod.load_queue(sdir) if os.path.isdir(sdir) else {}
    if qitems:
        pending = sum(1 for it in qitems.values() if it.get("status") == "pending")
        m4, w, nw = route_mod.m4_precision(qitems)
        print(f"queue: {pending} pending, M4 {m4 if m4 is not None else '-'} "
              f"({w} worth / {nw} not worth)")

    print("\nnext:")
    if not manifests:
        print("  steward init --out your-project.yaml   # encode your project's rules (an agent can fill it)")
    elif not projects:
        mp = manifests[0][0]
        print(f"  steward baseline --manifest {mp}       # first run seeds the diff state")
    else:
        pending = sum(1 for it in qitems.values()
                      if it.get("status") == "pending") if qitems else 0
        if pending:
            mp = manifests[0][0] if manifests else "<manifest>"
            print(f"  steward route --manifest {mp} --judge   # {pending} items await sorting/verdicts")
        else:
            print("  steward report        # cumulative savings/quality view")
            print("  steward check --diff  # re-check (or let the Stop hook do it)")
    return 0

# ---------------------------------------------------------------- init (V4)

INIT_RUBRIC = """\
# steward manifest rubric (v1)

Goal: produce <project>.yaml — the project's ENTIRE verification footprint —
with zero human table-writing. You (an LLM agent working inside the target
project) read the project's own written rules (CLAUDE.md, docs/, schema
files, run ledgers) and encode them. The engine knows nothing about any
project; this manifest is the only per-project cost.

## Discipline (non-negotiable)

1. OBSERVE FIRST: every probe starts `severity: warn`. Promotion to `fail`
   only after the false-positive rate has been measured on a real run.
2. PROVENANCE: every probe carries `source:` — the authoritative doc + section
   it enforces. A violation must always point back to THEIR rule, not yours.
3. ANTI-TRANSCRIPTION: any probe that copies numbers/enums out of a rule doc
   also pins `source_file:` + `source_quote:` (verbatim snippets containing
   those numbers). The engine verifies quotes still exist — doc changes then
   surface as SOURCE DRIFT instead of false-positive storms.
4. RISK WEIGHTS: give each probe a `risk_weight:` (default 1.0) stating what
   a violation costs the project — state-corruption high, cosmetics low.
   The attention queue sorts by it; guesses are better than silence.

## What to inventory

- Executable floors: schema fields, enum values, well-formedness of state
  files, naming patterns, staleness windows, reference integrity (see the 13
  probe types in the README; prefer an existing type over asking for a new one).
- The rulebook: EVERY written rule, each either `covered_by:` probe ids,
  `judgment_only: true` (explicitly not machine-checkable), or left uncovered
  (your next probe candidate). This feeds M5 rule coverage.
- Metrics: adjudication volume, defect escapes (superseded nodes), debt
  stocks. Derived expressions welcome.

Then: `steward baseline` to seed state, `steward install-hook` to run it
after every agent session, `steward route` when residue needs sorting.
"""

INIT_SKELETON = """\
# agent-steward manifest — {project}
# Generated by `steward init` — an agent (or you) fills this in following
# `steward init` (the rubric). Engine contains zero project knowledge.
project: {project}
root: {root}
mode: apply            # apply | readonly (readonly refuses cmd probes + hooks)

probes:
  # - id: example-schema
  #   type: frontmatter_required
  #   glob: "records/**/*.md"
  #   required: [id, status]
  #   severity: warn                # observe first — measure before enforcing
  #   risk_weight: 1.5              # what does one escaped violation cost?
  #   source: "CLAUDE.md rule 3"    # whose rule this is
  #   source_file: CLAUDE.md        # + verbatim quote(s) if numbers were copied
  #   source_quote: "..."
  #   fixable_by: agent             # agent | script | human — who may fix this category
  #   fix: "how a violation of this rule gets fixed, in plain words — the
  #         report shows it so the user can authorize fixes per category"

rulebook: []
  # - rule: "every written rule, one entry each"
  #   source: "CLAUDE.md rule 1"
  #   covered_by: [example-schema]  # or judgment_only: true, or leave uncovered

metrics: []
derived: []
"""


def cmd_init(args):
    """Zero-manual manifest cold start, the verification twin of `allocate
    rubric`: print the authoring rubric; with --out, also write a skeleton.
    The agent supplies the encoding; the engine never guesses project rules."""
    if args.out:
        if os.path.exists(args.out) and not args.force:
            print(f"[steward] {args.out} already exists — use --force to overwrite",
                  file=sys.stderr)
            return 1
        project = args.project or os.path.basename(
            os.path.abspath(args.root or os.getcwd()))
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(INIT_SKELETON.format(project=project,
                                         root=os.path.abspath(args.root or os.getcwd())))
        print(f"[steward] wrote skeleton {args.out} — fill it in per the rubric below\n")
    print(INIT_RUBRIC)
    return 0

# ---------------------------------------------------------------- distill (V3)

def cmd_distill(args):
    """Verdict memory -> rule candidates. Reads adjudication records (any
    jsonl with a reason-like field, e.g. an admission log; and/or the queue's
    own verdicts) and clusters recurring reasons: three identical rejections
    are one rule waiting to be written. Prints candidates only — a human
    turns them into probes/rubric lines; the steward never edits your rules."""
    printed = 0
    if args.path:
        rows = []
        try:
            with open(args.path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            print(f"[steward] distill: cannot read {args.path}: {e}", file=sys.stderr)
            return 1
        for cond in args.where or []:
            if "=" not in cond:
                print(f"[steward] distill: --where wants key=value, got '{cond}'",
                      file=sys.stderr)
                return 1
            k, v = cond.split("=", 1)
            rows = [r for r in rows if str(r.get(k)) == v]
        clusters = route_mod.distill(rows, field=args.field, min_count=args.min)
        print(f"[steward] {len(rows)} records, {len(clusters)} recurring "
              f"pattern(s) with >= {args.min} occurrences:")
        for b in clusters:
            print(f"  {b['n']:>4} ×  {b['key']}")
            for ex in b["examples"][:2]:
                print(f"          e.g. {ex}")
        if clusters:
            print("[steward] each pattern is a rule candidate: encode it as a "
                  "probe (or an L1 rubric line) and those adjudications stop "
                  "costing top-tier attention")
            if args.emit_rubric:
                print("\n# --- ready to paste into the manifest "
                      "(review before adopting; candidates, not verdicts) ---")
                print("judge:")
                print("  rubric: |")
                for b in clusters:
                    print(f"    - Items matching \"{b['key']}\" were adjudicated "
                          f"{b['n']}x with the same shape — machine-decidable: "
                          f"score 0.1 unless it carries genuinely new evidence.")
        printed += len(clusters)
    if args.queue or not args.path:
        sdir = os.path.abspath(args.state_dir or os.path.join(os.getcwd(), STATE_DIR_DEFAULT))
        noise, signal = route_mod.distill_queue(route_mod.load_queue(sdir))
        for b in noise:
            print(f"[steward] noisy rule '{b['probe']}': {b['n']} items judged "
                  f"not-worth — revisit its parameters or scope")
            for note in b["notes"][:2]:
                print(f"          note: {note}")
        for b in signal:
            print(f"[steward] confirmed rule '{b['probe']}': {b['n']} items "
                  f"judged worth — earning its place; consider promoting severity")
        printed += len(noise) + len(signal)
    if not printed:
        print("[steward] nothing recurring yet — verdict memory grows as "
              "adjudications accumulate")
    return 0

# ------------------------------------------------------ ingest-usage (metering)

def cmd_ingest_usage(args):
    """Zero-manual spend metering: read Claude Code's own transcripts (main
    session + one file per worker) and append MEASURED usage to the ledger.
    Ingested entries carry via=transcript: money views count them; quality
    loops (tune/canary) keep requiring explicit verdicts. Incremental via a
    byte cursor; fail-open on anything unreadable."""
    sdir = os.path.abspath(args.state_dir or os.path.join(os.getcwd(), STATE_DIR_DEFAULT))
    if args.transcript:
        paths = [os.path.abspath(args.transcript)]
    else:
        tdir = os.path.abspath(args.transcript_dir) if args.transcript_dir \
            else ingest_mod.default_transcript_dir(args.root or os.getcwd())
        paths = ingest_mod.scan_transcripts(tdir)
        if not paths:
            print(f"[steward] no transcripts found under {tdir} — pass "
                  f"--transcript/--transcript-dir, or meter manually with "
                  f"`steward log-task`")
            return 0
    alloc = None
    apath = args.allocation or ".allocation.yaml"
    if os.path.exists(apath):
        try:
            alloc = alloc_mod.load_allocation(apath)
        except yaml.YAMLError:
            alloc = None
    task_map = {}
    for spec in args.task_map or []:
        if "=" not in spec:
            print(f"[steward] --task-map wants task_id=regex, got '{spec}'",
                  file=sys.stderr)
            return 1
        tid, pat = spec.split("=", 1)
        task_map.setdefault(tid, []).append(pat)
    entries = ingest_mod.ingest(
        paths, sdir, alloc=alloc, task_map=task_map,
        session_task=args.session_task, project=args.project,
        now=now_iso(), dry_run=args.dry_run)
    if not entries:
        print("[steward] nothing new to ingest (cursor is up to date)")
        return 0
    tok = sum(e["est_tokens"] for e in entries)
    by_tier = {}
    for e in entries:
        by_tier[e["tier"]] = by_tier.get(e["tier"], 0) + e["est_tokens"]
    verb = "would append" if args.dry_run else "appended"
    print(f"[steward] {verb} {len(entries)} measured entries "
          f"({tok:,} in+out tokens; by tier: "
          + ", ".join(f"{t}: {v:,}" for t, v in sorted(by_tier.items())) + ")")
    for e in entries[:args.top]:
        mm = e["measured"]
        print(f"  {e['ts']}  {e['task']:<18} {e['tier']:<8} {e['model']:<22} "
              f"in+out={e['est_tokens']:>9,}  cache_r={mm['cache_read']:>11,}")
    if len(entries) > args.top:
        print(f"  ... {len(entries) - args.top} more")
    if any(e["task"] == "_unattributed" for e in entries):
        print("[steward] tip: unattributed workers — add [task=<id>] to your "
              "dispatch prompts, or pass --task-map 'task_id=regex'")
    return 0

# ---------------------------------------------------------------- canary (R3)

def cmd_canary(args):
    """Loop two, dispatcher side: ask right before dispatching a task whether
    this run should also shadow-run one tier lower. Deterministic (counted off
    the ledger, no randomness — auditable and replayable). Exit 0 = canary
    this run (shadow tier printed); exit 1 = don't. Any problem = don't
    canary (fail-open: the steward must never block the pipeline)."""
    apath = args.allocation or ".allocation.yaml"
    if not os.path.exists(apath):
        print(f"[steward] canary: no — no allocation file at {apath}")
        return 1
    try:
        alloc = alloc_mod.load_allocation(apath)
    except yaml.YAMLError as e:
        print(f"[steward] canary: no — allocation unparsable ({str(e).splitlines()[0]})")
        return 1
    sdir = os.path.abspath(args.state_dir or os.path.join(os.getcwd(), STATE_DIR_DEFAULT))
    entries = alloc_mod.read_ledger(sdir, project=args.project)
    d = alloc_mod.canary_decision(alloc, entries, args.task)
    if d["run"]:
        print(f"[steward] canary: yes — shadow-run tier '{d['shadow_tier']}' "
              f"alongside the primary ({d['reason']}); log both with "
              f"--canary primary/shadow --pair <id>, judge the shadow with "
              f"--quality same|worse|better")
        return 0
    print(f"[steward] canary: no — {d['reason']}")
    return 1

# ---------------------------------------------------------------- install-hook

def cmd_install_hook(args):
    """Register `steward check --diff --exit-new` as a Claude Code Stop hook in
    the target project's .claude/settings.json — after every agent session the
    steward runs automatically; new violations (exit 2) are fed back to the
    agent for self-repair. Deterministic, never forgets (docs/01 §9)."""
    manifest = os.path.abspath(args.manifest)
    with open(manifest, encoding="utf-8") as f:
        mf = yaml.safe_load(f)
    if mf.get("mode") == "readonly":
        print("[steward] install-hook refused: readonly manifest — a hook would "
              "write state into the target; run checks from outside instead.",
              file=sys.stderr)
        return 1
    root = os.path.abspath(args.root or mf["root"])
    sdir = os.path.abspath(args.state_dir or os.path.join(root, STATE_DIR_DEFAULT))
    settings_path = os.path.abspath(
        args.settings or os.path.join(root, ".claude", "settings.json"))
    # zero-install friendly: if `steward` isn't on PATH, pin the hook to the
    # exact interpreter + engine file that is running right now
    if getattr(args, "steward_cmd", None):
        prefix = args.steward_cmd
    elif shutil.which("steward"):
        prefix = "steward"
    else:
        prefix = f"{sys.executable} {os.path.abspath(__file__)}"
    command = (f"{prefix} check --manifest {manifest} --root {root} "
               f"--state-dir {sdir} --diff --exit-new")

    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except json.JSONDecodeError as e:
            print(f"[steward] {settings_path} is not valid JSON ({e}) — fix it "
                  f"first; refusing to overwrite.", file=sys.stderr)
            return 1
    stop = settings.setdefault("hooks", {}).setdefault("Stop", [])
    for entry in stop:
        for h in entry.get("hooks", []):
            hcmd = h.get("command", "")
            if manifest in hcmd and "--exit-new" in hcmd:
                print(f"[steward] hook already installed in {settings_path}")
                return 0
    stop.append({"hooks": [{"type": "command", "command": command}]})
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[steward] installed Stop hook in {settings_path}\n"
          f"          command: {command}\n"
          f"          effect: after each agent session you see only NEW "
          f"violations; exit 2 feeds them back to the agent.")
    return 0

# ---------------------------------------------------------------- allocate (R2)

def cmd_allocate(args):
    if args.action == "rubric":
        print(alloc_mod.RUBRIC)
        return 0
    if args.action == "init":
        if not args.axes:
            print("[steward] allocate init needs --axes AXES.yaml — print the "
                  "assessment rubric with `steward allocate rubric`, have your "
                  "agent fill it from the project's docs, then re-run.",
                  file=sys.stderr)
            return 1
        with open(args.axes, encoding="utf-8") as f:
            axes = yaml.safe_load(f) or {}
        try:
            alloc = alloc_mod.build_allocation(axes)
        except ValueError as e:
            print(f"[steward] allocate init: {e}", file=sys.stderr)
            return 1
        out = args.out or ".allocation.yaml"
        if os.path.exists(out) and not args.force:
            print(f"[steward] {out} already exists — use --force to overwrite "
                  f"(history will be lost) or `steward allocate tune` to adjust it.",
                  file=sys.stderr)
            return 1
        alloc_mod.write_allocation(alloc, out)
        print(f"[steward] wrote {out}: {len(alloc['tasks'])} task classes, "
              f"tiers mapped deterministically from axes (rubric "
              f"{alloc_mod.RUBRIC_VERSION}); no human wrote this table")
        return 0
    if args.action == "tune":
        path = args.allocation or ".allocation.yaml"
        if not os.path.exists(path):
            print(f"[steward] no allocation file at {path} — run `steward allocate init` first",
                  file=sys.stderr)
            return 1
        alloc = alloc_mod.load_allocation(path)
        sdir = os.path.abspath(args.state_dir or os.path.join(os.getcwd(), STATE_DIR_DEFAULT))
        entries = alloc_mod.read_ledger(sdir, project=args.project)
        if not entries:
            print(f"[steward] usage ledger empty ({sdir}/usage_ledger.jsonl) — "
                  f"nothing to tune yet; log tasks with `steward log-task`")
            return 0
        mism, unknown = alloc_mod.ledger_mismatches(alloc, entries)
        for m in mism:
            print(f"[steward] ledger data-quality: {m['ts']} task '{m['task']}' "
                  f"tier '{m['tier']}' but model '{m['model']}' matches tier(s) "
                  f"{', '.join(m['matches_tiers'])} — likely mis-logged; treat "
                  f"this task's stats with caution")
        if unknown:
            print(f"[steward] ledger data-quality: {len(unknown)} entries with "
                  f"a model name matching no tier pattern")
        proposals, unallocated = alloc_mod.tune_proposals(alloc, entries)
        for tid in unallocated:
            print(f"[steward] ledger has task '{tid}' not in the allocation table — "
                  f"assess it via the rubric and add it (recursive growth path)")
        if not proposals:
            print(f"[steward] {len(entries)} ledger entries, no tier changes warranted")
            return 0
        for p in proposals:
            print(f"[steward] proposal: task '{p['task']}' {p['from']} -> {p['to']} "
                  f"({p['reason']}; n={p['n']}, esc_rate={p['esc_rate']})")
        if args.only:
            kept = [p for p in proposals if p["task"] == args.only]
            skipped = sorted(p["task"] for p in proposals if p["task"] != args.only)
            if skipped:
                print(f"[steward] --only {args.only}: leaving {', '.join(skipped)} untouched")
            if not kept:
                print(f"[steward] no proposal for task '{args.only}' — nothing to do")
                return 0
            proposals = kept
        if args.apply or alloc.get("autotune") == "auto":
            alloc_mod.apply_proposals(alloc, proposals)
            alloc_mod.write_allocation(alloc, path)
            print(f"[steward] applied {len(proposals)} change(s) to {path} "
                  f"(history recorded; floors were respected)")
        else:
            print("[steward] propose mode: nothing changed — re-run with --apply, "
                  "or set `autotune: auto` in the allocation file")
        return 0
    return 1

# ---------------------------------------------------------------- report

def _parse_when_arg(value, name):
    if not value:
        return None
    when = parse_when(value)
    if when is None:
        print(f"[steward] cannot parse --{name} '{value}' "
              f"(use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)", file=sys.stderr)
        sys.exit(1)
    return when


def cmd_report(args):
    """Cumulative view: savings first, then trend, measured tuning effect,
    trade-offs, coverage, rule problems."""
    sdir = os.path.abspath(args.state_dir or os.path.join(os.getcwd(), STATE_DIR_DEFAULT))
    alloc = None
    apath = args.allocation or ".allocation.yaml"
    if os.path.exists(apath):
        alloc = alloc_mod.load_allocation(apath)
    since = _parse_when_arg(args.since, "since")
    until = _parse_when_arg(args.until, "until")
    entries = alloc_mod.read_ledger(sdir, project=args.project)
    entries = alloc_mod.filter_window(entries, since, until)
    sav = alloc_mod.compute_savings(entries, alloc)
    state = load_state(os.path.join(sdir, "state.json"))
    projects = state.get("projects", {})
    if args.project:
        projects = {k: v for k, v in projects.items() if k == args.project}

    weights = (alloc or {}).get("cost_weights", alloc_mod.DEFAULT_WEIGHTS)
    lines = ["# steward cumulative report", f"- generated: {now_iso()}"]
    if since or until:
        lines.append(f"- window: {args.since or 'start'} → {args.until or 'now'}")

    # ---- What needs you — the whole point of the tool, so it goes first.
    # Only things a machine could not decide, ranked; everything below this
    # section is evidence, not homework. Kept deliberately short: if this
    # list is ever full of items you judge not-worth, the tool is failing
    # (and M4 will say so).
    qitems = route_mod.load_queue(sdir)
    needs = []
    for name, p in sorted(projects.items()):          # 1. rule problems: the
        for c in (p.get("conflicts") or [])[:5]:      # machine cannot decide
            needs.append(f"- **rule problem** ({name}): {c}")
    if alloc and entries:                             # 2. pending tier changes
        proposals, _un = alloc_mod.tune_proposals(alloc, entries)
        for p in proposals:
            needs.append(f"- **tier change proposed**: {p['task']} "
                         f"{p['from']} → {p['to']} ({p['reason']}; n={p['n']}, "
                         f"esc_rate={p['esc_rate']}) — apply with "
                         f"`steward allocate tune --apply --only {p['task']}`")
    if qitems:                                        # 3. rules emitting noise
        noise, _sig = route_mod.distill_queue(qitems)
        for b in noise:
            needs.append(f"- **noisy rule**: '{b['probe']}' — {b['n']} of its "
                         f"items judged not-worth; revisit its parameters or scope"
                         + (f" (note: {b['notes'][-1]})" if b["notes"] else ""))
        top_pending = sorted((it for it in qitems.values()
                              if it.get("status") == "pending"),
                             key=lambda x: -x.get("score", 0))[:3]
        for it in top_pending:                        # 4. top of the queue
            needs.append(f"- **queue top**: [{it['probe']}] {it['text'][:110]} "
                         f"(`steward approve {it['id']} ...`)")
    lines += ["", "## What needs you", ""]
    lines += needs if needs else ["(nothing — no rule problems, no pending "
                                  "proposals, an empty queue. Enjoy it.)"]

    # ---- Rule check at a glance + fix authorization by category. Users
    # authorize fixes per CATEGORY (one row, one decision), never per item —
    # 241 items with the same shape are one authorization, not 241 reads.
    for name, p in sorted(projects.items()):
        stats = p.get("probe_stats") or []
        if not stats:
            continue
        by_status = {}
        for s in stats:
            by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        open_v = sum(s["n_violations"] for s in stats)
        lines += ["", f"## Rule check — {name}", "",
                  f"- {len(stats)} rules checked: "
                  + ", ".join(f"{v} {k}" for k, v in sorted(by_status.items()))
                  + f"; {open_v} open finding(s)"]
        cats = [s for s in stats if s["n_violations"]]
        conflicts_here = p.get("conflicts") or []
        if cats or conflicts_here:
            lines += ["", "### Open findings by category — authorize fixes per row",
                      "",
                      "| category | findings | how it gets fixed | who fixes it |",
                      "|---|---|---|---|"]
            if conflicts_here:
                # the one category no machine may resolve: two of the user's
                # own rules disagree — always first, always human
                lines.append(f"| **rule conflicts** | {len(conflicts_here)} "
                             f"| two of your rules disagree (or a rule drifted "
                             f"from its source doc) — steward names both sides; "
                             f"only you can say which is right | **human** |")
            for s in sorted(cats, key=lambda x: -x["n_violations"]):
                fix = s.get("fix") or "(no fix note in the manifest — add a " \
                                      "`fix:` line to this probe)"
                who = s.get("fixable_by") or "?"
                lines.append(f"| {s['probe']} | {s['n_violations']} "
                             f"| {fix} | {who} |")
            lines.append("\n(tell your agent which rows it may fix — e.g. "
                         "\"fix all agent-fixable categories\"; 'human' rows "
                         "are yours, one decision per row)")
            for c in conflicts_here:
                lines.append(f"- rule conflict detail: {c}")

    lines += ["", "## Savings (estimated)", ""]
    if entries:
        lines += alloc_mod.spend_summary_lines(
            sav, weights_note=f" (weights: {weights})")
        tier_order = (alloc or {}).get("tiers", alloc_mod.DEFAULT_TIERS)
        seen_tiers = [t for t in tier_order if t in sav["tokens_by_tier"]] + \
                     sorted(set(sav["tokens_by_tier"]) - set(tier_order))
        if seen_tiers:
            tot_tok = sum(sav["tokens_by_tier"].values()) or 1
            tot_cost = sum(sav["cost_by_tier"].values()) or 1
            lines += ["", "### Where the money goes (by tier)", "",
                      "| tier | weight | runs | tokens | % volume | cost index | % cost |",
                      "|---|---|---|---|---|---|---|"]
            for t in seen_tiers:
                tok, cost = sav["tokens_by_tier"][t], sav["cost_by_tier"].get(t, 0)
                lines.append(
                    f"| {t} | {weights.get(t, '?')} | {sav['entries_by_tier'].get(t, 0)} "
                    f"| {round(tok):,} | {round(100 * tok / tot_tok, 1)}% "
                    f"| {round(cost):,} | {round(100 * cost / tot_cost, 1)}% |")
    else:
        lines.append("(usage ledger is empty — savings appear once tasks are "
                     "logged with `steward log-task`)")

    # the tool's own scoreboard: what got flagged and then went away
    fixes = []
    fpath = os.path.join(sdir, "fixes.jsonl")
    if os.path.exists(fpath):
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not args.project or row.get("project") == args.project:
                    fixes.append(row)
    if fixes:
        total = sum(r.get("n", 0) for r in fixes)
        by_probe = {}
        for r in fixes:
            by_probe[r["probe"]] = by_probe.get(r["probe"], 0) + r.get("n", 0)
        lines += ["", f"## Fixed so far: {total} violations resolved", ""]
        lines += [f"- {pid}: {n} resolved"
                  for pid, n in sorted(by_probe.items(), key=lambda x: -x[1])]
        ex = next((r for r in reversed(fixes) if r.get("examples")), None)
        if ex:
            lines.append(f"- latest examples ({ex['probe']}):")
            lines += [f"  - {e}" for e in ex["examples"]]
        lines.append("- ('resolved' = flagged, then gone: fixed in the target "
                     "or the rule itself was corrected after review — both are "
                     "the loop working)")

    if entries:
        gran, periods, no_ts = alloc_mod.slice_periods(entries)
        if len(periods) > 1:
            lines += ["", f"## Trend (per {gran})", "",
                      "| period | entries | tokens | cost index | saved vs top | escalation |",
                      "|---|---|---|---|---|---|"]
            for label, rows in periods:
                p = alloc_mod.compute_savings(rows, alloc)
                pct = f"{p['saved_vs_top_pct']}%" if p["saved_vs_top_pct"] is not None else "-"
                esc = f"{p['esc_rate']}" if p["esc_rate"] is not None else "-"
                tok = round(sum(p["tokens_by_tier"].values()))
                lines.append(f"| {label} | {len(rows)} | {tok:,} "
                             f"| {round(p['actual_cost']):,} | {pct} | {esc} |")
            if no_ts:
                lines.append(f"\n({no_ts} entries without a parsable ts excluded from trend)")

    if entries:
        cpau = alloc_mod.cpau_by_task(entries, alloc)
        if cpau:
            lines += ["", "## CPAU (cost per accepted unit — the north star)", "",
                      "| task | runs | accepted | CPAU |", "|---|---|---|---|"]
            for tid, a in sorted(cpau.items()):
                c = f"{a['cpau']:,}" if a["cpau"] is not None else "-"
                lines.append(f"| {tid} | {a['runs']} | {a['accepted']} | {c} |")
            lines.append("\n(cost of every primary run, failures included, "
                         "divided by accepted runs — waste is part of the price)")
        esc_rows = alloc_mod.escalation_matrix(alloc, entries)
        if esc_rows:
            # trigger enums are internal vocabulary — the report speaks plainly
            why = {"vr_fail": "output failed your written rules at this tier",
                   "low_confidence": "output fell below the confidence bar"}
            lines += ["", "## Escalations (loop one: where a tier proved too low)", "",
                      "| task | from | redone at | n | why |",
                      "|---|---|---|---|---|"]
            for r in esc_rows:
                reason = why.get(r["trigger"], r["trigger"])
                if r["notes"]:
                    reason += " — " + "; ".join(r["notes"])[:80]
                lines.append(f"| {r['task']} | {r['from']} | {r['to']} | {r['n']} "
                             f"| {reason} |")
            lines.append("\n(each row is bounded waste — one lower-tier redo per "
                         "count; a persistent row is the table telling you that "
                         "tier assignment is wrong)")

        cst = alloc_mod.canary_stats(entries)
        if cst:
            lines += ["", "## Canary (loop two: quality diff vs cost diff)", "",
                      "| task | shadow runs | judged | quality parity | shadow tokens |",
                      "|---|---|---|---|---|"]
            for tid, s in sorted(cst.items()):
                judged = s["n"] - s["unjudged"]
                parity = f"{s['same_rate']}" if s["same_rate"] is not None else "-"
                lines.append(f"| {tid} | {s['n']} | {judged} | {parity} "
                             f"| {round(s['shadow_tokens']):,} |")
            lines.append("\n(parity >= tune.canary_same over >= tune.canary_min "
                         "judged runs proposes a demote; a measured gap vetoes "
                         "demotion — cheapest at the same quality, never "
                         "cheapest at any quality)")

    # attention economics: M1 (top-tier tokens per accepted top-tier run,
    # the adjudicator's unit price) and M4 (residue precision from approve)
    att = []
    if entries:
        tiers = (alloc or {}).get("tiers", alloc_mod.DEFAULT_TIERS)
        top_tier = tiers[-1]
        top_rows = [e for e in entries if str(e.get("tier")) == top_tier
                    and not alloc_mod.is_shadow(e)]
        top_tok = sum(e.get("est_tokens", 0) for e in top_rows
                      if isinstance(e.get("est_tokens"), (int, float)))
        top_ok = sum(1 for e in top_rows if str(e.get("result")) == "pass")
        if top_ok:
            att.append(f"- M1 proxy (top-tier tokens per accepted top-tier run): "
                       f"{round(top_tok / top_ok):,} — V2 hardgate wants this "
                       f"down >=50% as the queue absorbs triage")
    if qitems:
        m4, worth, not_worth = route_mod.m4_precision(qitems)
        pending = sum(1 for it in qitems.values() if it.get("status") == "pending")
        att.append(f"- attention queue: {pending} pending item(s) (`steward route`)")
        if m4 is not None:
            att.append(f"- M4 residue precision: {m4} ({worth} worth / "
                       f"{not_worth} not worth; target >0.7, <0.5 means the "
                       f"sorter is mis-ranking)")
        # verdicts feed back into rule quality: a rule whose violations keep
        # being judged not-worth is asking to be re-parameterised
        by_probe = {}
        for it in qitems.values():
            st = it.get("status")
            if st in ("worth", "not_worth"):
                s = by_probe.setdefault(it["probe"], {"worth": 0, "not_worth": 0,
                                                      "notes": []})
                s[st] += 1
                if it.get("verdict_note"):
                    s["notes"].append(str(it["verdict_note"]))
        for pid, s in sorted(by_probe.items()):
            judged = s["worth"] + s["not_worth"]
            line = f"- rule feedback '{pid}': {s['worth']} worth / {s['not_worth']} not worth"
            if judged >= 3 and s["worth"] / judged < 0.5:
                line += " — **mostly noise: revisit this rule's parameters or scope**"
            att.append(line)
            for note in s["notes"][-2:]:
                att.append(f"  - adjudicator's note: {note}")
    if att:
        lines += ["", "## Attention economics (M1/M4)", ""] + att

    effects = alloc_mod.tune_effect(alloc, entries) if alloc else []
    if effects:
        lines += ["", "## Tuning effect (measured, not modeled)", ""]
        for ef in effects:
            b, a = ef["before"], ef["after"]
            lines.append(f"- {ef['task']}: {ef['from']} → {ef['to']} at {ef['at']}")
            if b["n"] and a["n"]:
                lines.append(f"  - before: {b['n']} entries, esc_rate {b['esc_rate']}, "
                             f"cost/1k tokens {b['cost_per_1k']}")
                lines.append(f"  - after:  {a['n']} entries, esc_rate {a['esc_rate']}, "
                             f"cost/1k tokens {a['cost_per_1k']}")
                if b["cost_per_1k"] and a["cost_per_1k"]:
                    delta = round(100 * (b["cost_per_1k"] - a["cost_per_1k"]) / b["cost_per_1k"], 1)
                    lines.append(f"  - **cost/1k {'-' if delta >= 0 else '+'}{abs(delta)}%; "
                                 f"quality guard: escalation "
                                 f"{'did not rise' if (a['esc_rate'] or 0) <= (b['esc_rate'] or 0) else 'ROSE — consider reverting'}**")
            else:
                lines.append(f"  - insufficient data (before n={b['n']}, after n={a['n']}) "
                             f"— verdict comes as post-change entries accumulate")

    lines += ["", "## Trade-offs", ""]
    if entries:
        lines.append(f"- escalations: {sav['escalations']} of {sav['entries']} "
                     f"entries (rate {sav['esc_rate']}) — bounded cost: one "
                     f"lower-tier redo each")
        for tid, n in sorted(sav["esc_by_task"].items(), key=lambda x: -x[1]):
            lines.append(f"  - {tid}: {n}")
    for name, p in projects.items():
        m = p.get("metrics") or {}
        if m.get("escaped_defect_rate") is not None:
            lines.append(f"- escaped defect rate ({name}, last check): "
                         f"{m['escaped_defect_rate']} — savings must not push this up")
    if len(lines) and lines[-1] == "":
        lines.append("(no data yet)")
    lines += ["", "## Rule coverage (M5)", ""]
    any_cov = False
    for name, p in projects.items():
        m = p.get("metrics") or {}
        if m.get("rule_coverage") is not None:
            any_cov = True
            lines.append(f"- {name}: {m['rule_coverage']} "
                         f"({m.get('rules_covered')}/{m.get('rules_total')} rules executable, "
                         f"{m.get('rules_judgment_only')} judgment-only)")
            for u in (p.get("coverage") or {}).get("uncovered", []):
                lines.append(f"  - uncovered: {u}")
    if not any_cov:
        lines.append("(no check state yet — run `steward check` to populate)")
    lines += ["", "## Rule problems (needs your decision)", ""]
    any_problem = False
    for name, p in projects.items():
        for c in p.get("conflicts") or []:
            lines.append(f"- CONFLICT ({name}): {c}")
            any_problem = True
        for d in (p.get("coverage") or {}).get("drift", []):
            lines.append(f"- DRIFT ({name}): {d}")
            any_problem = True
    if not any_problem:
        lines.append("(none — nothing requires you this round)")
    lines += ["", "---", "Cadence: every `steward check` writes a per-run report; "
              "this cumulative view is on demand (`steward report`)."]
    text = "\n".join(lines) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[steward] wrote {args.out}")
    else:
        print(text)
    return 0

# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(prog="steward")
    sub = ap.add_subparsers(dest="cmd")  # optional: bare `steward` = status

    for name in ("check", "run", "baseline"):  # "run" = V0 alias; "baseline"
    # = first check that seeds diff state (same engine, friendlier intent)
        cp = sub.add_parser(name, help="run all probes + metrics from a manifest")
        cp.add_argument("--manifest", required=True)
        cp.add_argument("--root", help="override manifest root")
        cp.add_argument("--out", help="output dir (default: <tool>/runs/<project>-<ts>)")
        cp.add_argument("--diff", action="store_true",
                        help="report only violations added/resolved since last check")
        cp.add_argument("--exit-new", action="store_true",
                        help="with --diff: exit 2 and print new violations to "
                             "stderr (hook contract)")
        cp.add_argument("--state-dir",
                        help=f"where state.json lives (default: ./{STATE_DIR_DEFAULT})")

    sp = sub.add_parser("stamp", help="write provenance frontmatter into artifact(s)")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--produced-by", required=True, help="model/actor that produced this")
    sp.add_argument("--task", required=True, help="task class id (see .allocation.yaml)")
    sp.add_argument("--round", type=int, help="pipeline round number")

    lp = sub.add_parser("log-task", help="append one task to the usage ledger")
    lp.add_argument("--task", required=True, help="task class id")
    lp.add_argument("--tier", required=True, help="tier actually used (e.g. cheap/mid/top)")
    lp.add_argument("--model", help="concrete model name")
    lp.add_argument("--est-tokens", type=int, dest="est_tokens")
    lp.add_argument("--result", help="outcome (e.g. pass/fail/escalated)")
    lp.add_argument("--project")
    lp.add_argument("--note")
    lp.add_argument("--canary", choices=["primary", "shadow"],
                    help="mark this entry as one half of a canary pair (R3)")
    lp.add_argument("--pair", help="canary pair id linking primary and shadow")
    lp.add_argument("--quality", choices=["same", "worse", "better"],
                    help="shadow entry only: adjudicated quality vs the primary")
    lp.add_argument("--allocation",
                    help="allocation file used to warn on tier/model mismatch "
                         "(default: ./.allocation.yaml if present)")
    lp.add_argument("--state-dir",
                    help=f"where usage_ledger.jsonl lives (default: ./{STATE_DIR_DEFAULT})")

    alp = sub.add_parser("allocate",
                         help="tier table lifecycle: rubric -> init (auto cold start) -> tune")
    alp.add_argument("action", choices=["rubric", "init", "tune"])
    alp.add_argument("--axes", help="axes.yaml produced by an agent following the rubric")
    alp.add_argument("--out", help="init: where to write (default: ./.allocation.yaml)")
    alp.add_argument("--force", action="store_true", help="init: overwrite existing file")
    alp.add_argument("--allocation", help="tune: allocation file (default: ./.allocation.yaml)")
    alp.add_argument("--apply", action="store_true",
                     help="tune: apply proposals (default: propose only)")
    alp.add_argument("--only",
                     help="tune: act on this task's proposal only (others are "
                          "printed but left untouched)")
    alp.add_argument("--project", help="tune: filter ledger entries by project")
    alp.add_argument("--state-dir")

    ip = sub.add_parser("init", help="print the manifest-authoring rubric; "
                                     "--out also writes a skeleton (V4 cold start)")
    ip.add_argument("--out", help="write a skeleton manifest here")
    ip.add_argument("--project", help="project name for the skeleton")
    ip.add_argument("--root", help="target project root for the skeleton")
    ip.add_argument("--force", action="store_true", help="overwrite existing --out")

    dst = sub.add_parser("distill", help="cluster recurring adjudication "
                                         "reasons into rule candidates (V3)")
    dst.add_argument("--path", help="jsonl of adjudication records "
                                    "(e.g. an admission log)")
    dst.add_argument("--field", default="reason",
                     help="field holding the reason text (default: reason)")
    dst.add_argument("--where", action="append",
                     help="key=value filter, repeatable (e.g. verdict=reject)")
    dst.add_argument("--min", type=int, default=3,
                     help="minimum occurrences to surface (default 3)")
    dst.add_argument("--queue", action="store_true",
                     help="also distill the attention queue's own verdicts")
    dst.add_argument("--emit-rubric", action="store_true", dest="emit_rubric",
                     help="render the clusters as a ready-to-paste judge rubric "
                          "block for the manifest")
    dst.add_argument("--state-dir")

    rt = sub.add_parser("route", help="sort the latest check's violations into "
                                      "an attention queue (V2 L2 sorter)")
    rt.add_argument("--manifest", required=True,
                    help="manifest (severity/risk_weight/source per probe)")
    rt.add_argument("--project", help="default: manifest's project field")
    rt.add_argument("--judge", action="store_true",
                    help="also score judgment-worthiness with a cheap model on "
                         "YOUR key (ANTHROPIC_API_KEY; api.anthropic.com only; "
                         "fail-open)")
    rt.add_argument("--model", help="judge model override")
    rt.add_argument("--top", type=int, default=15, help="items to print (default 15)")
    rt.add_argument("--state-dir")

    apv = sub.add_parser("approve", help="record your verdict on a queue item (M4)")
    apv.add_argument("item", help="queue item id (from `steward route`)")
    apv.add_argument("--verdict", required=True, choices=["worth", "not-worth"])
    apv.add_argument("--note")
    apv.add_argument("--state-dir")

    ing = sub.add_parser("ingest-usage",
                         help="append MEASURED usage from Claude Code "
                              "transcripts to the ledger (zero-manual metering)")
    ing.add_argument("--transcript", help="one transcript file (e.g. from a "
                                          "Stop hook's transcript_path)")
    ing.add_argument("--transcript-dir",
                     help="a project's transcript dir (default: derived from "
                          "--root/cwd as ~/.claude/projects/<slug>)")
    ing.add_argument("--root", help="target project root for slug derivation")
    ing.add_argument("--task-map", action="append",
                     help="task_id=regex matched against worker dispatch "
                          "prompts, repeatable ([task=<id>] markers win)")
    ing.add_argument("--session-task", default="_session",
                     help="task id for main-session (non-worker) usage")
    ing.add_argument("--allocation", help="for tier lookup via tier_patterns "
                                          "(default: ./.allocation.yaml)")
    ing.add_argument("--project")
    ing.add_argument("--dry-run", action="store_true",
                     help="show what would be appended, touch nothing")
    ing.add_argument("--top", type=int, default=10, help="entries to print")
    ing.add_argument("--state-dir")

    cnp = sub.add_parser("canary",
                         help="should this run shadow-run one tier lower? "
                              "exit 0 = yes (R3 loop two)")
    cnp.add_argument("--task", required=True, help="task class id")
    cnp.add_argument("--allocation", help="default: ./.allocation.yaml")
    cnp.add_argument("--project", help="filter ledger entries by project")
    cnp.add_argument("--state-dir")

    rp2 = sub.add_parser("report", help="cumulative report: savings, trade-offs, "
                                        "coverage, rule problems")
    rp2.add_argument("--allocation", help="allocation file for cost weights/history")
    rp2.add_argument("--project")
    rp2.add_argument("--since", help="only ledger entries at/after this time "
                                     "(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)")
    rp2.add_argument("--until", help="only ledger entries at/before this time")
    rp2.add_argument("--out", help="write to file instead of stdout")
    rp2.add_argument("--state-dir")

    ih = sub.add_parser("install-hook",
                        help="register check --diff as a Claude Code Stop hook")
    ih.add_argument("--manifest", required=True)
    ih.add_argument("--root", help="override manifest root")
    ih.add_argument("--settings", help="settings.json path "
                                       "(default: <root>/.claude/settings.json)")
    ih.add_argument("--state-dir", help=f"default: <root>/{STATE_DIR_DEFAULT}")
    ih.add_argument("--steward-cmd", dest="steward_cmd",
                    help="override the steward invocation used in the hook "
                         "(default: `steward` if on PATH, else absolute "
                         "python + engine path)")

    args = ap.parse_args()
    if args.cmd is None:
        sys.exit(cmd_status())
    if args.cmd in ("check", "run", "baseline"):
        if args.cmd == "baseline":
            print("[steward] seeding baseline — current violations become the "
                  "reference; future `check --diff` reports only changes")
        run(args.manifest, args.root, args.out, diff=args.diff,
            state_dir=args.state_dir, exit_new=args.exit_new)
    elif args.cmd == "stamp":
        sys.exit(cmd_stamp(args))
    elif args.cmd == "log-task":
        sys.exit(cmd_log_task(args))
    elif args.cmd == "allocate":
        sys.exit(cmd_allocate(args))
    elif args.cmd == "canary":
        sys.exit(cmd_canary(args))
    elif args.cmd == "ingest-usage":
        sys.exit(cmd_ingest_usage(args))
    elif args.cmd == "route":
        sys.exit(cmd_route(args))
    elif args.cmd == "distill":
        sys.exit(cmd_distill(args))
    elif args.cmd == "init":
        sys.exit(cmd_init(args))
    elif args.cmd == "approve":
        sys.exit(cmd_approve(args))
    elif args.cmd == "report":
        sys.exit(cmd_report(args))
    elif args.cmd == "install-hook":
        sys.exit(cmd_install_hook(args))


if __name__ == "__main__":
    main()
