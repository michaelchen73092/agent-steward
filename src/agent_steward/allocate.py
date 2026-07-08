"""
agent-steward — allocation layer (R-series, docs/02)

Zero-manual cold start: no human writes the tier table.
  1. An LLM agent (the harness session that already lives in the project)
     reads the project's own docs and rates every task class on four axes,
     following the published rubric (`steward allocate rubric`). Output: axes.yaml.
  2. `steward allocate init` maps those ratings to tiers/floors/canaries with the
     DETERMINISTIC matrix below and writes .allocation.yaml. The mapping is
     versioned and auditable — the LLM supplies ratings + rationale, never tiers.
  3. `steward allocate tune` recursively adjusts: reads the usage ledger,
     computes per-task escalation rates, proposes promotions/demotions
     (respecting floors). `autotune: propose` stops and tells the user;
     `autotune: auto` (or --apply) applies and records history.

The human's only mandatory inputs live elsewhere (rule conflicts the machine
cannot decide, surfaced by `steward check`). Savings math feeds the reports:
estimated cost vs an everything-on-top counterfactual and vs the cold-start
table, from est_tokens × declared cost weights (estimates, not billing data).
"""
import datetime as dt
import fnmatch
import json
import os

import yaml

RUBRIC_VERSION = "v1"

RUBRIC = """\
# steward allocation rubric (v1)

Goal: produce axes.yaml — one entry per task class — with ZERO human table-writing.
You (an LLM agent working inside the target project) read the project's own docs
(CLAUDE.md, pipeline docs, prompt files, run ledgers) and rate every task class
on four axes. `steward allocate init --axes axes.yaml` then maps your ratings to
tiers with the deterministic matrix below. You supply evidence; the matrix decides.

## Axes (rate low | med | high, one-line rationale each)

- verifiable: can failures be caught by executable checks (steward probes, evals,
  tests)? high = output flows through probes; low = quality visible only to judgment.
- judgment: does the task adjudicate, synthesize across sources, or make calls —
  or does it follow a template? high = admission decisions, cross-domain synthesis;
  low = mechanical transform of structured input.
- blast_radius: cost of one wrong output escaping. high = touches money, is
  irreversible, or gets published; med = pollutes stored knowledge until caught;
  low = discarded or re-checked downstream anyway.
- volume: rough share of total token volume (high/med/low) — sets saving priority.

## Deterministic tier matrix (applied by the engine, not by you)

tier:
  judgment=high                      -> top
  judgment=med                       -> mid
  judgment=low and verifiable=high   -> cheap
  judgment=low and verifiable<high   -> mid
floor (tune may never demote below):
  blast_radius=high and judgment=high -> top
  blast_radius=high                   -> mid
  otherwise                           -> cheap
canary (shadow-run sample one tier lower, R3):
  0.05 if tier != cheap and verifiable=high else 0
escalate_on:
  vr_fail if verifiable=high else low_confidence

## axes.yaml format

tasks:
  - id: extract_structured
    verifiable: high
    judgment: low
    blast_radius: med
    volume: high
    rationale: "schema-checked extraction from structured sources (probes catch failures)"
"""

LEVELS = ("low", "med", "high")
AXES = ("verifiable", "judgment", "blast_radius", "volume")
DEFAULT_TIERS = ["cheap", "mid", "top"]
DEFAULT_WEIGHTS = {"cheap": 1, "mid": 8, "top": 25}  # docs/02 §5 relative economics
DEFAULT_TIER_PATTERNS = {  # generated-file defaults — data, editable; engine logic never hardcodes these
    "cheap": ["*haiku*"],
    "mid": ["*sonnet*", "*opus*"],
    "top": ["*fable*", "*mythos*", "human"],
}
DEFAULT_TUNE = {"min_samples": 20, "demote_below": 0.02, "promote_above": 0.10,
                "canary_min": 5, "canary_same": 0.9}
ESCALATED_RESULTS = ("fail", "escalated")


def now_iso():
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _validate_axes(task):
    missing = [a for a in AXES if a not in task]
    bad = [a for a in AXES if a in task and str(task[a]) not in LEVELS]
    if missing or bad:
        raise ValueError(
            f"task '{task.get('id', '?')}': missing axes {missing}, invalid {bad} "
            f"(each of {list(AXES)} must be one of {list(LEVELS)})")


def assess(task):
    """The published matrix: axis ratings -> {tier, floor, canary, escalate_on}."""
    _validate_axes(task)
    v, j, b = str(task["verifiable"]), str(task["judgment"]), str(task["blast_radius"])
    if j == "high":
        tier = "top"
    elif j == "med":
        tier = "mid"
    else:
        tier = "cheap" if v == "high" else "mid"
    if b == "high" and j == "high":
        floor = "top"
    elif b == "high":
        floor = "mid"
    else:
        floor = "cheap"
    # floor can exceed the matrix tier (e.g. money-touching mechanical task): lift tier
    if DEFAULT_TIERS.index(tier) < DEFAULT_TIERS.index(floor):
        tier = floor
    canary = 0.05 if tier != "cheap" and v == "high" else 0
    escalate_on = "vr_fail" if v == "high" else "low_confidence"
    return {"tier": tier, "floor": floor, "canary": canary, "escalate_on": escalate_on}


def build_allocation(axes):
    """axes: parsed axes.yaml dict -> full allocation dict (deterministic)."""
    tasks = []
    for t in axes.get("tasks", []) or []:
        a = assess(t)
        tasks.append({
            "id": str(t["id"]), "tier": a["tier"], "floor": a["floor"],
            "canary": a["canary"], "escalate_on": a["escalate_on"],
            "assessed": {k: str(t[k]) for k in AXES},
            "rationale": t.get("rationale", ""),
        })
    if not tasks:
        raise ValueError("axes file has no tasks")
    return {
        "version": 1, "rubric": RUBRIC_VERSION, "generated_at": now_iso(),
        "autotune": "propose",  # propose | auto
        "tiers": list(DEFAULT_TIERS),
        "cost_weights": dict(DEFAULT_WEIGHTS),
        "tier_patterns": {k: list(v) for k, v in DEFAULT_TIER_PATTERNS.items()},
        "tune": dict(DEFAULT_TUNE),
        "tasks": tasks,
        "history": [],
    }


ALLOCATION_HEADER = """\
# .allocation.yaml — generated by `steward allocate init` (rubric %s)
# Cold start was assessed by an LLM agent against the published rubric
# (`steward allocate rubric`); tiers were mapped DETERMINISTICALLY from the
# recorded axes — no human wrote this table. `steward allocate tune` adjusts it
# recursively from usage-ledger evidence (floors are never crossed).
# Humans adjudicate rule conflicts, not tables.
"""


def render_allocation(alloc):
    return (ALLOCATION_HEADER % alloc.get("rubric", RUBRIC_VERSION)
            + yaml.safe_dump(alloc, sort_keys=False, allow_unicode=True,
                             default_flow_style=None))


def load_allocation(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_allocation(alloc, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_allocation(alloc))

# ---------------------------------------------------------------- ledger

def read_ledger(state_dir, project=None):
    path = os.path.join(state_dir, "usage_ledger.jsonl")
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if project and e.get("project") not in (None, project):
                continue
            entries.append(e)
    return entries

def _model_in_tier(model, patterns):
    return any(fnmatch.fnmatch(str(model).lower(), str(p).lower())
               for p in patterns or [])


def ledger_mismatches(alloc, entries):
    """Ledger data-quality check (observe-only): entries whose recorded model
    name contradicts the declared tier per the allocation's tier_patterns
    (e.g. tier=top logged with a mid-tier model). Mis-logged tiers pollute
    escalation rates and savings math, so tune/report surface them instead of
    silently trusting the row. Returns (mismatches, unknown_models):
    mismatches = model matches some OTHER tier's patterns but not the declared
    one; unknown_models = model matches no tier at all (new model name or typo).
    Entries missing tier or model are skipped — completeness is rule 6's job,
    not this check's."""
    pats = alloc.get("tier_patterns") or {}
    mismatches, unknown = [], []
    if not pats:
        return mismatches, unknown
    for e in entries:
        tier, model = e.get("tier"), e.get("model")
        if not tier or not model or str(tier) not in pats:
            continue
        if _model_in_tier(model, pats.get(str(tier))):
            continue
        others = sorted(t for t, ps in pats.items()
                        if t != str(tier) and _model_in_tier(model, ps))
        rec = {"ts": e.get("ts"), "task": e.get("task"), "tier": str(tier),
               "model": str(model), "matches_tiers": others}
        (mismatches if others else unknown).append(rec)
    return mismatches, unknown

# ---------------------------------------------------------------- canary (loop 2, R3)
# "Too-high is silent": an expensive model that succeeds emits no signal that a
# cheaper one would have done the job. The canary shadow-runs a sample of a
# task class one tier lower; both outputs go through verification and the
# dispatcher records a quality verdict on the shadow entry. Regret is bounded
# by the sampling rate. The engine only decides WHEN to canary and aggregates
# verdicts — it never runs a model.

def is_shadow(entry):
    return str(entry.get("canary", "")) == "shadow"


def is_measured(entry):
    """Transcript-ingested entries are the SPEND record; quality loops keep
    requiring explicit verdicts — a measurement is not a judgment."""
    return str(entry.get("via", "")) == "transcript"


def canary_decision(alloc, entries, task_id):
    """Deterministic, ledger-driven sampling: the Nth primary run of a task
    canaries when N % round(1/rate) == 0 (first run included, so a freshly
    enabled canary produces evidence immediately). Returns a dict:
    {run: bool, shadow_tier, interval, n, reason}. Never raises — unknown
    task / rate 0 / already at floor all come back run=False with a reason."""
    tiers = alloc.get("tiers", DEFAULT_TIERS)
    t = next((x for x in alloc.get("tasks", []) if str(x.get("id")) == str(task_id)), None)
    if t is None:
        return {"run": False, "reason": f"task '{task_id}' not in allocation table"}
    rate = float(t.get("canary") or 0)
    if rate <= 0:
        return {"run": False, "reason": "canary rate is 0 for this task"}
    tier = str(t.get("tier"))
    floor = str(t.get("floor", tiers[0]))
    if tier not in tiers or tiers.index(tier) == 0:
        return {"run": False, "reason": f"tier '{tier}' has nothing below it"}
    shadow = tiers[tiers.index(tier) - 1]
    if floor in tiers and tiers.index(shadow) < tiers.index(floor):
        return {"run": False,
                "reason": f"floor '{floor}' forbids probing below tier '{tier}'"}
    n = sum(1 for e in entries
            if str(e.get("task")) == str(task_id)
            and not is_shadow(e) and not is_measured(e))
    interval = max(1, round(1 / rate))
    fire = (n % interval) == 0
    return {"run": fire, "shadow_tier": shadow, "interval": interval, "n": n,
            "reason": f"primary run #{n} of '{task_id}', canary every {interval}"}


def canary_stats(entries):
    """Aggregate shadow verdicts per task: n, quality counts, same-rate,
    exploration cost (est_tokens of shadow runs)."""
    out = {}
    for e in entries:
        if not is_shadow(e):
            continue
        tid = str(e.get("task"))
        s = out.setdefault(tid, {"n": 0, "same": 0, "worse": 0, "better": 0,
                                 "unjudged": 0, "shadow_tokens": 0})
        s["n"] += 1
        q = str(e.get("quality", ""))
        s[q if q in ("same", "worse", "better") else "unjudged"] += 1
        if isinstance(e.get("est_tokens"), (int, float)):
            s["shadow_tokens"] += e["est_tokens"]
    for s in out.values():
        judged = s["same"] + s["worse"] + s["better"]
        s["same_rate"] = round((s["same"] + s["better"]) / judged, 4) if judged else None
    return out

# ---------------------------------------------------------------- tune (loop 1)

def tune_proposals(alloc, entries):
    """Two evidence paths. Loop one (escalation rate): rate ~ 0 with enough
    samples -> propose demote (never below floor); rate above threshold ->
    propose promote. Loop two (canary, R3 — stronger evidence): enough judged
    shadow runs at quality parity -> propose demote; judged shadow runs showing
    a quality gap VETO the escalation-rate demote for that task — "cheapest at
    the same quality", never cheapest at any quality. Shadow entries are
    exploration, excluded from escalation stats.
    Returns (proposals, unallocated_task_ids)."""
    tiers = alloc.get("tiers", DEFAULT_TIERS)
    cfg = {**DEFAULT_TUNE, **(alloc.get("tune") or {})}
    table = {t["id"]: t for t in alloc.get("tasks", [])}
    stats = {}
    for e in entries:
        if is_shadow(e) or is_measured(e):
            continue
        tid = str(e.get("task"))
        # only runs at the task's CURRENT tier are evidence about that tier —
        # escalations suffered at a lower tier before a promote are the reason
        # the promote happened, not grounds for promoting again
        cur = table.get(tid, {}).get("tier")
        if cur is not None and str(e.get("tier")) != str(cur):
            continue
        s = stats.setdefault(tid, {"n": 0, "esc": 0})
        s["n"] += 1
        if str(e.get("result", "")) in ESCALATED_RESULTS:
            s["esc"] += 1
    cstats = canary_stats(entries)
    proposals, unallocated = [], sorted(set(stats) - set(table))
    for tid, t in table.items():
        s = stats.get(tid)
        if not s:
            continue
        rate = s["esc"] / s["n"]
        ti = tiers.index(t["tier"]) if t["tier"] in tiers else None
        fi = tiers.index(t.get("floor", tiers[0])) if t.get("floor", tiers[0]) in tiers else 0
        if ti is None:
            continue
        c = cstats.get(tid, {})
        judged = c.get("n", 0) - c.get("unjudged", 0)
        if rate >= cfg["promote_above"] and s["n"] >= 5 and ti < len(tiers) - 1:
            proposals.append({"task": tid, "from": t["tier"], "to": tiers[ti + 1],
                              "reason": "escalation rate above threshold",
                              "n": s["n"], "esc_rate": round(rate, 4)})
        elif judged >= cfg["canary_min"] and ti > fi:
            if c["same_rate"] >= cfg["canary_same"]:
                proposals.append({"task": tid, "from": t["tier"], "to": tiers[ti - 1],
                                  "reason": f"canary quality parity "
                                            f"({judged} judged shadow runs)",
                                  "n": s["n"], "esc_rate": round(rate, 4),
                                  "canary_same_rate": c["same_rate"]})
            # else: canary measured a quality gap — no demote, and the
            # escalation-rate path below is vetoed for this task
        elif (rate <= cfg["demote_below"] and s["n"] >= cfg["min_samples"]
              and ti > fi and not judged):
            proposals.append({"task": tid, "from": t["tier"], "to": tiers[ti - 1],
                              "reason": "escalation rate ~0 with enough samples "
                                        "(floor respected)",
                              "n": s["n"], "esc_rate": round(rate, 4)})
    return proposals, unallocated


def apply_proposals(alloc, proposals):
    tiers = alloc.get("tiers", DEFAULT_TIERS)
    table = {t["id"]: t for t in alloc.get("tasks", [])}
    for p in proposals:
        t = table.get(p["task"])
        if not t:
            continue
        t["tier"] = p["to"]
        # re-derive canary from the recorded axes with the published matrix
        # (rubric v1): sampling only makes sense above the bottom tier and
        # where verification can actually judge the shadow output
        v = str(t.get("assessed", {}).get("verifiable", ""))
        t["canary"] = 0.05 if p["to"] != tiers[0] and v == "high" else 0
        alloc.setdefault("history", []).append({
            "at": now_iso(), "task": p["task"], "from": p["from"], "to": p["to"],
            "reason": p["reason"], "n": p["n"], "esc_rate": p["esc_rate"],
        })
    return alloc

# ---------------------------------------------------------------- savings

def initial_tiers(alloc):
    """Reconstruct the cold-start tier per task from history (first 'from' wins)."""
    init = {t["id"]: t["tier"] for t in alloc.get("tasks", [])}
    seen = set()
    for h in alloc.get("history", []):
        if h["task"] not in seen:
            init[h["task"]] = h["from"]
            seen.add(h["task"])
    return init


def compute_savings(entries, alloc=None):
    """Savings vs everything-on-top and (if tune history exists) vs the
    cold-start table. All numbers are estimates: est_tokens × declared
    cost_weights, not billing data."""
    alloc = alloc or {}
    tiers = alloc.get("tiers", DEFAULT_TIERS)
    weights = alloc.get("cost_weights", DEFAULT_WEIGHTS)
    top_w = weights.get(tiers[-1], max(weights.values()))
    init = initial_tiers(alloc)
    out = {"entries": len(entries), "metered": 0, "no_tokens": 0,
           "unknown_tier": 0, "tokens_by_tier": {}, "entries_by_tier": {},
           "cost_by_tier": {},
           "actual_cost": 0, "top_cost": 0, "initial_cost": 0,
           "escalations": 0, "esc_by_task": {},
           "canary_runs": 0, "canary_cost": 0}
    for e in entries:
        tier, tok = str(e.get("tier", "")), e.get("est_tokens")
        if is_shadow(e):
            # exploration spend, kept out of the production savings math
            out["canary_runs"] += 1
            if isinstance(tok, (int, float)) and tier in weights:
                out["canary_cost"] += tok * weights[tier]
            continue
        if str(e.get("result", "")) in ESCALATED_RESULTS:
            out["escalations"] += 1
            tid = str(e.get("task"))
            out["esc_by_task"][tid] = out["esc_by_task"].get(tid, 0) + 1
        if not isinstance(tok, (int, float)):
            out["no_tokens"] += 1
            continue
        if tier not in weights:
            out["unknown_tier"] += 1
            continue
        out["metered"] += 1
        out["tokens_by_tier"][tier] = out["tokens_by_tier"].get(tier, 0) + tok
        out["entries_by_tier"][tier] = out["entries_by_tier"].get(tier, 0) + 1
        out["cost_by_tier"][tier] = out["cost_by_tier"].get(tier, 0) + tok * weights[tier]
        out["actual_cost"] += tok * weights[tier]
        out["top_cost"] += tok * top_w
        init_tier = init.get(str(e.get("task")), tier)
        out["initial_cost"] += tok * weights.get(init_tier, weights[tier])
    out["saved_vs_top"] = out["top_cost"] - out["actual_cost"]
    out["saved_vs_top_pct"] = (round(100 * out["saved_vs_top"] / out["top_cost"], 1)
                               if out["top_cost"] else None)
    out["saved_vs_initial"] = out["initial_cost"] - out["actual_cost"]
    out["saved_vs_initial_pct"] = (round(100 * out["saved_vs_initial"] / out["initial_cost"], 1)
                                   if out["initial_cost"] and alloc.get("history") else None)
    out["esc_rate"] = round(out["escalations"] / len(entries), 4) if entries else None
    return out


def escalation_matrix(alloc, entries):
    """Shape of loop one, per (task, from-tier): how many runs escalated, the
    tier they get redone at (one up, per the table), the configured trigger,
    and any notes the dispatcher left on the escalated entries."""
    tiers = (alloc or {}).get("tiers", DEFAULT_TIERS)
    table = {t["id"]: t for t in (alloc or {}).get("tasks", [])}
    out = {}
    for e in entries:
        if is_shadow(e) or str(e.get("result", "")) not in ESCALATED_RESULTS:
            continue
        tid, tier = str(e.get("task")), str(e.get("tier", "?"))
        key = (tid, tier)
        to = "?"
        if tier in tiers and tiers.index(tier) < len(tiers) - 1:
            to = tiers[tiers.index(tier) + 1]
        r = out.setdefault(key, {"task": tid, "from": tier, "to": to, "n": 0,
                                 "result": {}, "trigger": str(
                                     table.get(tid, {}).get("escalate_on", "-")),
                                 "notes": []})
        r["n"] += 1
        res = str(e.get("result"))
        r["result"][res] = r["result"].get(res, 0) + 1
        if e.get("note"):
            r["notes"].append(str(e["note"]))
    return [out[k] for k in sorted(out)]


def cpau_by_task(entries, alloc=None):
    """CPAU — cost per accepted unit (docs/02 north star), per task class:
    est cost of ALL primary runs (failures and escalations included; waste is
    part of the price) divided by accepted (result=pass) runs. Shadow runs are
    exploration, not production, and stay out of both sides."""
    weights = (alloc or {}).get("cost_weights", DEFAULT_WEIGHTS)
    agg = {}
    for e in entries:
        if is_shadow(e):
            continue
        tid = str(e.get("task"))
        a = agg.setdefault(tid, {"cost": 0, "accepted": 0, "runs": 0})
        a["runs"] += 1
        # explicit pass, or a measured run with no verdict recorded —
        # silence means accepted; failures must be recorded to count
        if str(e.get("result", "")) == "pass" or (
                is_measured(e) and "result" not in e):
            a["accepted"] += 1
        tok, tier = e.get("est_tokens"), str(e.get("tier", ""))
        if isinstance(tok, (int, float)) and tier in weights:
            a["cost"] += tok * weights[tier]
    for a in agg.values():
        a["cpau"] = round(a["cost"] / a["accepted"]) if a["accepted"] else None
    return agg


def parse_ts(entry):
    try:
        return dt.datetime.fromisoformat(str(entry.get("ts", ""))[:19])
    except ValueError:
        return None


def filter_window(entries, since=None, until=None):
    """Keep entries whose ts falls in [since, until]. Entries without a
    parsable ts are kept only when no window is requested."""
    if not since and not until:
        return entries
    out = []
    for e in entries:
        t = parse_ts(e)
        if t is None:
            continue
        if since and t < since:
            continue
        if until and t > until:
            continue
        out.append(e)
    return out


def slice_periods(entries):
    """Group entries by day (span <= 14 days) or ISO week (longer).
    Returns (granularity, [(label, entries), ...] sorted, n_unstamped_ts)."""
    stamped = [(parse_ts(e), e) for e in entries]
    no_ts = sum(1 for t, _ in stamped if t is None)
    stamped = [(t, e) for t, e in stamped if t is not None]
    if not stamped:
        return "day", [], no_ts
    span = (max(t for t, _ in stamped) - min(t for t, _ in stamped)).days
    gran = "day" if span <= 14 else "week"
    buckets = {}
    for t, e in stamped:
        label = t.strftime("%Y-%m-%d") if gran == "day" else f"{t.isocalendar()[0]}-W{t.isocalendar()[1]:02d}"
        buckets.setdefault(label, []).append(e)
    return gran, sorted(buckets.items()), no_ts


def tune_effect(alloc, entries):
    """Measured (not modeled) before/after around each tier change in the
    allocation history: same task, entries before the change vs after."""
    weights = (alloc or {}).get("cost_weights", DEFAULT_WEIGHTS)

    def stats(rows):
        n = len(rows)
        esc = sum(1 for e in rows if str(e.get("result", "")) in ESCALATED_RESULTS)
        tok = sum(e.get("est_tokens", 0) for e in rows
                  if isinstance(e.get("est_tokens"), (int, float)))
        cost = sum(e["est_tokens"] * weights.get(str(e.get("tier")), 0) for e in rows
                   if isinstance(e.get("est_tokens"), (int, float)))
        return {"n": n, "esc_rate": round(esc / n, 4) if n else None,
                "tokens": tok, "cost_per_1k": round(cost / tok * 1000, 2) if tok else None}

    out = []
    for h in (alloc or {}).get("history", []):
        try:
            at = dt.datetime.fromisoformat(str(h.get("at", ""))[:19])
        except ValueError:
            continue
        rows = [e for e in entries if str(e.get("task")) == str(h.get("task"))]
        before = [e for e in rows if (parse_ts(e) or at) < at]
        after = [e for e in rows if (parse_ts(e) or at) >= at]
        out.append({"task": h.get("task"), "at": h.get("at"),
                    "from": h.get("from"), "to": h.get("to"),
                    "before": stats(before), "after": stats(after)})
    return out


def spend_summary_lines(sav, weights_note=""):
    """Short savings block shared by check-report and cumulative report."""
    lines = [f"- metered: {sav['metered']} ledger entries"
             + (f" ({sav['no_tokens']} without est_tokens,"
                f" {sav['unknown_tier']} with unknown tier)"
                if sav["no_tokens"] or sav["unknown_tier"] else ""),
             f"- estimated cost index: {round(sav['actual_cost']):,}{weights_note}"]
    if sav["saved_vs_top_pct"] is not None:
        lines.append(f"- **vs everything-on-top: saved {round(sav['saved_vs_top']):,} "
                     f"({sav['saved_vs_top_pct']}%)**")
    if sav["saved_vs_initial_pct"] is not None:
        lines.append(f"- vs cold-start table (tuning effect): saved "
                     f"{round(sav['saved_vs_initial']):,} ({sav['saved_vs_initial_pct']}%)")
    if sav.get("canary_runs"):
        lines.append(f"- canary exploration: {sav['canary_runs']} shadow runs, "
                     f"cost {round(sav['canary_cost']):,} (bounded regret, "
                     f"kept out of the savings math)")
    lines.append("- caveat: estimates from est_tokens × declared cost weights, "
                 "not billing data")
    return lines
