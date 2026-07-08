"""
agent-steward — attention router (V2, docs/01 §5-6)

The deterministic probes (L0) already ran via `steward check`; what they could
not auto-decide is residue. This module spends expensive attention well:

  L2 sorter (deterministic, always on): score = impact × judgment-worthiness.
     impact = severity weight × the probe's manifest-declared risk_weight —
     the project states what a violation costs, the engine never guesses.
  L1 judge (optional, --judge): the ONLY network call in the whole steward,
     as per the trust contract: the user's OWN Anthropic key (ANTHROPIC_API_KEY),
     hard-whitelisted to api.anthropic.com, and fail-open — no key, no network,
     bad response all degrade to the deterministic order, never block.
     Unjudged items default to judgment-worthiness 1.0: the failure direction
     is "send one item too many", never "silently drop one".

violations ≠ verdicts: the queue sorts, the human decides. `steward approve`
records the human's worth/not-worth verdict per item — that stream is M4
(residue precision), the calibration loop that measures the sorter itself.
"""
import hashlib
import json
import os
import shutil
import subprocess

SEVERITY_WEIGHT = {"fail": 1.0, "warn": 0.6, "skipped": 0.0}
QUEUE_FILE = "queue.jsonl"
JUDGE_URL = "https://api.anthropic.com/v1/messages"  # trust contract: sole allowed host
JUDGE_MODEL_DEFAULT = "claude-haiku-4-5"


def item_id(probe, text):
    return hashlib.sha1(f"{probe}\n{text}".encode("utf-8")).hexdigest()[:10]


def load_queue(state_dir):
    path = os.path.join(state_dir, QUEUE_FILE)
    items = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        it = json.loads(line)
                        items[it["id"]] = it
                    except (json.JSONDecodeError, KeyError):
                        continue
    return items


def save_queue(state_dir, items):
    path = os.path.join(state_dir, QUEUE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        for it in sorted(items.values(), key=lambda x: -x.get("score", 0)):
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return path


def probe_meta(manifest):
    meta = {}
    for spec in manifest.get("probes", []) or []:
        meta[str(spec.get("id"))] = {
            "severity": str(spec.get("severity", spec.get("on_fail", "fail"))),
            "risk_weight": float(spec.get("risk_weight", 1.0)),
            "source": str(spec.get("source", "")),
            # route: false = this probe's findings belong to a machine loop
            # (e.g. lint feeding the worker self-repair hook), never to the
            # human attention queue
            "route": bool(spec.get("route", True)),
        }
    return meta


def build_queue(manifest, violations, prev_items, now):
    """violations: {probe_id: [text, ...]} from state.json. Returns items dict.
    Verdicts already recorded on an item (by stable id) survive re-routing;
    items whose violation disappeared are dropped unless already adjudicated."""
    meta = probe_meta(manifest)
    items = {}
    for probe, texts in (violations or {}).items():
        m = meta.get(str(probe), {"severity": "warn", "risk_weight": 1.0,
                                  "source": "", "route": True})
        if not m.get("route", True):
            continue
        impact = SEVERITY_WEIGHT.get(m["severity"], 0.6) * m["risk_weight"]
        for text in texts:
            iid = item_id(probe, text)
            prev = prev_items.get(iid, {})
            judge = prev.get("judge")  # keep an earlier L1 score if any
            worthiness = judge["score"] if judge else 1.0
            items[iid] = {
                "id": iid, "probe": str(probe), "text": str(text),
                "source": m["source"],
                "impact": round(impact, 4),
                "score": round(impact * worthiness, 4),
                "judge": judge,
                "status": prev.get("status", "pending"),
                "verdict_note": prev.get("verdict_note"),
                "queued_at": prev.get("queued_at", now),
            }
    # adjudicated history survives even after the violation is fixed (M4 data)
    for iid, it in prev_items.items():
        if iid not in items and it.get("status") in ("worth", "not_worth"):
            items[iid] = it
    return items


def m4_precision(items):
    worth = sum(1 for it in items.values() if it.get("status") == "worth")
    not_worth = sum(1 for it in items.values() if it.get("status") == "not_worth")
    judged = worth + not_worth
    return (round(worth / judged, 4) if judged else None), worth, not_worth

# ---------------------------------------------------------------- distill (V3)
# Verdict memory: adjudication reasons are the most expensive data the system
# produces — each one is a human (or top-tier) judgment. The distiller finds
# reasons that keep recurring: three rejections with the same shape are not
# three decisions, they are one rule waiting to be written. Output is always
# a CANDIDATE — a human turns it into a probe/rubric line; the tool never
# writes rules into the manifest by itself.

REASON_SPLITS = ("—", " - ", ";", ",")


def reason_key(text, splits=REASON_SPLITS):
    """Normalize an adjudication reason to its leading clause — the part
    before the first separator is the pattern, the rest is the instance."""
    s = str(text).strip()
    cut = len(s)
    for sep in splits:
        i = s.find(sep)
        if 0 < i < cut:
            cut = i
    return " ".join(s[:cut].split())[:80]


def distill(rows, field="reason", min_count=3, splits=REASON_SPLITS):
    """Cluster rows by the leading clause of `field`. Returns clusters
    (count >= min_count, largest first): {key, n, examples}."""
    buckets = {}
    for r in rows:
        v = r.get(field)
        if not v:
            continue
        k = reason_key(v, splits)
        b = buckets.setdefault(k, {"key": k, "n": 0, "examples": []})
        b["n"] += 1
        if len(b["examples"]) < 3:
            b["examples"].append(str(v)[:160])
    return sorted((b for b in buckets.values() if b["n"] >= min_count),
                  key=lambda b: -b["n"])


def distill_queue(items, min_count=2):
    """The queue's own verdict memory: not-worth notes cluster into 'this rule
    emits noise' candidates; worth notes into 'this rule catches real things
    — consider promoting severity' evidence. Grouped per probe."""
    noise, signal = {}, {}
    for it in items.values():
        st, note = it.get("status"), it.get("verdict_note")
        if st not in ("worth", "not_worth"):
            continue
        side = signal if st == "worth" else noise
        b = side.setdefault(it["probe"], {"probe": it["probe"], "n": 0, "notes": []})
        b["n"] += 1
        if note and len(b["notes"]) < 3:
            b["notes"].append(str(note)[:160])
    return ([b for b in noise.values() if b["n"] >= min_count],
            [b for b in signal.values() if b["n"] >= min_count])

JUDGE_PROMPT = """\
You are a triage judge for a verification pipeline. Deterministic checks
flagged the items below; a human adjudicator has limited attention. For each
item, score how much it needs HUMAN JUDGMENT (not how severe it sounds):
1.0 = genuinely ambiguous or high-stakes, a human must look;
0.5 = unclear;
0.0 = mechanical noise a script or the producing agent can fix alone.
{rubric}
Items (JSON): {items}
Reply with ONLY a JSON array: [{{"id": "...", "score": 0.0, "reason": "one line"}}]
"""


def judge_rubric(manifest):
    j = manifest.get("judge") or {}
    if j.get("rubric"):
        return f"Project rubric:\n{j['rubric']}"
    rules = [r.get("rule") for r in manifest.get("rulebook", []) or [] if r.get("rule")]
    if rules:
        return "The project's own rules (violations of riskier rules deserve " \
               "more attention):\n- " + "\n- ".join(rules[:20])
    return ""


def parse_judge_reply(payload, valid_ids):
    """Extract [{id, score, reason}] from a model reply; drop anything not
    ours or out of range. Any parse failure -> empty list (fail-open)."""
    try:
        text = "".join(b.get("text", "") for b in payload.get("content", [])
                       if b.get("type") == "text")
        start, end = text.find("["), text.rfind("]")
        arr = json.loads(text[start:end + 1])
        out = []
        for row in arr:
            iid = str(row.get("id"))
            if iid in valid_ids:
                score = max(0.0, min(1.0, float(row.get("score", 1.0))))
                out.append({"id": iid, "score": score,
                            "reason": str(row.get("reason", ""))[:200]})
        return out
    except (ValueError, TypeError, AttributeError, KeyError):
        return []


def run_judge(items, manifest, model=None, batch=20, _post=None):
    """Score pending items with a cheap model on the user's OWN credentials,
    two ways: ANTHROPIC_API_KEY -> direct API (headless/CI); otherwise the
    `claude` CLI on PATH -> the terminal login the user already pays for
    (the default for Claude Code users — no separate key, no second bill).
    Returns number judged. Every failure path is silent degradation —
    the deterministic queue stands on its own."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    cli = shutil.which("claude")
    if not key and not cli and _post is None:
        return 0, ("no ANTHROPIC_API_KEY and no `claude` CLI on PATH — "
                   "deterministic order only")
    pending = [it for it in items.values() if it.get("status") == "pending"
               and not it.get("judge")]
    if not pending:
        return 0, "nothing new to judge"
    model = model or (manifest.get("judge") or {}).get("model", JUDGE_MODEL_DEFAULT)
    rubric = judge_rubric(manifest)
    judged = 0

    def post_api(body):  # user's own key, whitelisted host only
        import urllib.request
        req = urllib.request.Request(
            JUDGE_URL, data=json.dumps(body).encode("utf-8"),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_cli(body):  # user's own logged-in Claude Code session
        p = subprocess.run([cli, "-p", "--model", body["model"]],
                           input=body["messages"][0]["content"],
                           capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or "claude CLI failed").strip()[:200])
        return {"content": [{"type": "text", "text": p.stdout}]}

    post = _post or (post_api if key else post_cli)
    via = "API key" if key else "claude CLI (existing login)"
    for i in range(0, len(pending), batch):
        chunk = pending[i:i + batch]
        brief = [{"id": it["id"], "probe": it["probe"], "rule_source": it["source"],
                  "violation": it["text"][:300]} for it in chunk]
        prompt = JUDGE_PROMPT.format(rubric=rubric,
                                     items=json.dumps(brief, ensure_ascii=False))
        try:
            payload = post({"model": model, "max_tokens": 1500,
                            "messages": [{"role": "user", "content": prompt}]})
        except Exception as e:  # noqa: BLE001 — fail-open by contract
            return judged, f"judge call failed ({type(e).__name__}) — " \
                           f"deterministic order only"
        for row in parse_judge_reply(payload, {it["id"] for it in chunk}):
            it = items[row["id"]]
            it["judge"] = {"score": row["score"], "reason": row["reason"],
                           "model": model}
            it["score"] = round(it["impact"] * row["score"], 4)
            judged += 1
    return judged, f"judged {judged} item(s) with {model} via {via}"
