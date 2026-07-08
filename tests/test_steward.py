"""Fixture tests — one per probe type (discipline: every probe type ships with
at least one fixture) plus the V1/R1 features: --diff state, source provenance,
rulebook coverage (M5), stamp, log-task, allocation_compliance.

All fixtures are built in tmp_path; nothing touches a real project.
"""
import json
import os
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agent_steward import cli  # noqa: E402


def write(root, rel, content):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


FACT_OK = """---
id: f1
statement: something
sources: [a, b]
verification_status: verified
confidence: 0.8
---
body
"""

FACT_SINGLE_SOURCE_HIGH_CONF = """---
id: f2
statement: bold claim
sources: [a]
verification_status: unverified
confidence: 0.9
claim_class: world_claim
---
body
"""

# ---------------------------------------------------------------- probes

def test_probe_cmd(tmp_path):
    r = cli.probe_cmd(str(tmp_path), {"id": "p", "cmd": "true"})
    assert r["status"] == "pass"
    r = cli.probe_cmd(str(tmp_path), {"id": "p", "cmd": "false", "on_fail": "warn"})
    assert r["status"] == "warn"


def test_probe_jsonl_wellformed(tmp_path):
    write(tmp_path, "log.jsonl", '{"a": 1}\nnot json\n')
    r = cli.probe_jsonl_wellformed(str(tmp_path), {"id": "p", "path": "log.jsonl"})
    assert r["status"] == "fail" and r["n_violations"] == 1 and r["n_checked"] == 2
    r = cli.probe_jsonl_wellformed(str(tmp_path), {"id": "p", "path": "missing.jsonl"})
    assert r["status"] == "skipped"


def test_probe_frontmatter_required(tmp_path):
    write(tmp_path, "facts/2026/ok.md", FACT_OK)
    write(tmp_path, "facts/2026/bad.md", "---\nid: f3\n---\nbody\n")
    spec = {"id": "p", "glob": "facts/**/*.md",
            "required": ["id", "confidence"], "severity": "warn"}
    r = cli.probe_frontmatter_required(str(tmp_path), spec)
    assert r["status"] == "warn" and r["n_violations"] == 1 and r["n_checked"] == 2


def test_probe_single_source_cap(tmp_path):
    write(tmp_path, "facts/2026/ok.md", FACT_OK)
    write(tmp_path, "facts/2026/bad.md", FACT_SINGLE_SOURCE_HIGH_CONF)
    spec = {"id": "p", "glob": "facts/**/*.md", "default_cap": 0.5,
            "class_caps": {"self_declarative": 0.6}}
    r = cli.probe_single_source_cap(str(tmp_path), spec)
    assert r["status"] == "warn" and r["n_violations"] == 1


def test_probe_field_value_rule(tmp_path):
    write(tmp_path, "insights/2026/a.md", "---\norigin: expert\n---\nx\n")
    write(tmp_path, "insights/2026/b.md", "---\norigin: hallucinated\n---\nx\n")
    write(tmp_path, "insights/2026/c.md", "---\nid: i3\n---\nx\n")
    spec = {"id": "p", "glob": "insights/**/*.md", "field": "origin",
            "allowed": ["expert", "user", "synthesis"], "severity": "warn"}
    r = cli.probe_field_value_rule(str(tmp_path), spec)
    assert r["status"] == "warn" and r["n_violations"] == 2  # bad enum + missing


def test_probe_bash_syntax(tmp_path):
    write(tmp_path, "scripts/ok.sh", "echo hi\n")
    write(tmp_path, "scripts/bad.sh", "if [ 1 -eq 1 ; then\n")
    r = cli.probe_bash_syntax(str(tmp_path), {"id": "p", "glob": "scripts/*.sh"})
    assert r["status"] == "fail" and r["n_violations"] == 1


def test_probe_csv_required_columns(tmp_path):
    write(tmp_path, "data/good.csv", "Date,Owner,Amount\n2026-07-01,x,1\n")
    write(tmp_path, "data/bad.csv", "Date,Amount\n2026-07-01,1\n")
    spec = {"id": "p", "glob": "data/*.csv", "columns": ["Owner"], "severity": "warn"}
    r = cli.probe_csv_required_columns(str(tmp_path), spec)
    assert r["status"] == "warn" and r["n_violations"] == 1


def test_probe_tsv_wellformed(tmp_path):
    write(tmp_path, "REGISTRY.tsv", "a\tb\tc\nshort\tline\n")
    r = cli.probe_tsv_wellformed(str(tmp_path), {"id": "p", "path": "REGISTRY.tsv",
                                                 "min_cols": 3})
    assert r["status"] == "fail" and r["n_violations"] == 1 and r["n_checked"] == 2


def test_probe_file_exists(tmp_path):
    write(tmp_path, "INDEX.md", "x\n")
    assert cli.probe_file_exists(str(tmp_path), {"id": "p", "path": "INDEX.md"})["status"] == "pass"
    assert cli.probe_file_exists(str(tmp_path), {"id": "p", "path": "nope.md"})["status"] == "fail"


# ------------------------------------------- allocation_compliance (R1/R2)

ALLOC = """
tiers: {cheap: haiku-class, mid: opus-class, top: fable-class}
tier_patterns:
  cheap: ["*haiku*"]
  mid: ["*opus*"]
  top: ["*fable*", "human"]
tasks:
  - {id: extract, tier: mid}
  - {id: admission, tier: top}
"""


def stamped(task, model):
    return f"---\ntitle: t\ntask: {task}\nproduced_by: {model}\n---\nbody\n"


def test_allocation_compliance(tmp_path):
    write(tmp_path, ".allocation.yaml", ALLOC)
    write(tmp_path, "reports/07/ok.md", stamped("extract", "claude-opus-4-8"))
    write(tmp_path, "reports/07/wrong_tier.md", stamped("extract", "claude-fable-5"))
    write(tmp_path, "reports/07/unknown_task.md", stamped("mystery", "claude-haiku-4-5"))
    write(tmp_path, "reports/07/no_model.md", "---\ntask: admission\n---\nbody\n")
    write(tmp_path, "reports/07/unstamped.md", "---\ntitle: plain\n---\nbody\n")
    spec = {"id": "p", "glob": "reports/**/*.md", "allocation_file": ".allocation.yaml"}
    r = cli.probe_allocation_compliance(str(tmp_path), spec)
    assert r["status"] == "warn"  # observe-first default
    assert r["n_checked"] == 4 and r["n_violations"] == 3
    assert "unstamped=1" in r["detail"]
    joined = "\n".join(r["violations"])
    assert "wrong_tier.md" in joined and "unknown_task.md" in joined and "no_model.md" in joined


def test_allocation_compliance_inline_and_skips(tmp_path):
    write(tmp_path, "reports/07/a.md", stamped("triage", "claude-haiku-4-5"))
    spec = {"id": "p", "glob": "reports/**/*.md",
            "tasks": {"triage": "cheap"}, "tier_patterns": {"cheap": ["*haiku*"]}}
    assert cli.probe_allocation_compliance(str(tmp_path), spec)["status"] == "pass"
    # no table at all -> skipped, never a crash
    assert cli.probe_allocation_compliance(
        str(tmp_path), {"id": "p", "glob": "reports/**/*.md"})["status"] == "skipped"


# ---------------------------------------------------------------- runner features

def make_manifest(tmp_path, project_root, extra=None):
    mf = {
        "project": "fixture-project",
        "root": str(project_root),
        "mode": "apply",
        "probes": [
            {"id": "fact-schema", "type": "frontmatter_required",
             "glob": "facts/**/*.md", "required": ["id", "confidence"],
             "severity": "warn", "source": "RULES.md §1"},
        ],
        "metrics": [{"id": "facts_total", "type": "frontmatter_count",
                     "glob": "facts/**/*.md"}],
    }
    if extra:
        mf.update(extra)
    p = tmp_path / "manifest.yaml"
    p.write_text(yaml.safe_dump(mf), encoding="utf-8")
    return str(p)


def run_check(manifest, out, state_dir, diff=False):
    return cli.run(manifest, out_override=str(out), diff=diff, state_dir=str(state_dir))


def test_diff_new_then_resolved(tmp_path):
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    manifest = make_manifest(tmp_path, proj)
    state = tmp_path / "state"

    out1 = run_check(manifest, tmp_path / "o1", state, diff=True)
    report1 = open(os.path.join(out1, "REPORT.md")).read()
    assert "first check" in report1

    # introduce a violation -> shows up as new
    write(proj, "facts/2026/bad.md", "---\nid: f9\n---\nbody\n")
    out2 = run_check(manifest, tmp_path / "o2", state, diff=True)
    report2 = open(os.path.join(out2, "REPORT.md")).read()
    assert "facts/2026/bad.md" in report2.split("## New violations")[1].split("## Resolved")[0]

    # unchanged -> suppressed
    out3 = run_check(manifest, tmp_path / "o3", state, diff=True)
    report3 = open(os.path.join(out3, "REPORT.md")).read()
    new_section = report3.split("## New violations")[1].split("## Resolved")[0]
    assert "facts/2026/bad.md" not in new_section
    assert "unchanged violations suppressed by --diff: 1" in report3

    # fixed -> shows up as resolved
    write(proj, "facts/2026/bad.md", FACT_OK.replace("id: f1", "id: f9"))
    out4 = run_check(manifest, tmp_path / "o4", state, diff=True)
    report4 = open(os.path.join(out4, "REPORT.md")).read()
    assert "facts/2026/bad.md" in report4.split("## Resolved since last check")[1]

    st = json.load(open(state / "state.json"))
    assert st["projects"]["fixture-project"]["violations"] == {}


def test_source_column_in_report(tmp_path):
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    manifest = make_manifest(tmp_path, proj)
    out = run_check(manifest, tmp_path / "o", tmp_path / "state")
    report = open(os.path.join(out, "REPORT.md")).read()
    assert "RULES.md §1" in report
    line = json.loads(open(os.path.join(out, "probe_results.jsonl")).readline())
    assert line["source"] == "RULES.md §1"


def test_rulebook_coverage_m5(tmp_path):
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    write(proj, "evals/test_x.py", "def test(): pass\n")
    rulebook = [
        {"rule": "facts have schema", "source": "RULES.md §1",
         "covered_by": ["fact-schema"]},
        {"rule": "prompt gated by eval", "source": "RULES.md §2",
         "form": "test", "covered_by": ["evals/test_x.py"]},
        {"rule": "insight quality", "source": "RULES.md §3", "judgment_only": True},
        {"rule": "no naked exceptions", "source": "RULES.md §4"},  # uncovered
        {"rule": "ghost pointer", "source": "RULES.md §5",
         "covered_by": ["nonexistent-probe"]},  # drift
    ]
    manifest = make_manifest(tmp_path, proj, {"rulebook": rulebook})
    out = run_check(manifest, tmp_path / "o", tmp_path / "state")
    metrics = json.load(open(os.path.join(out, "metrics.json")))["metrics"]
    assert metrics["rules_total"] == 5
    assert metrics["rules_covered"] == 2
    assert metrics["rules_judgment_only"] == 1
    assert metrics["rule_coverage"] == 0.4
    report = open(os.path.join(out, "REPORT.md")).read()
    assert "Rule coverage (M5)" in report
    assert "no naked exceptions" in report          # listed as uncovered
    assert "nonexistent-probe" in report            # listed as drift


def test_readonly_refuses_state_inside_target(tmp_path):
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    mf = make_manifest(tmp_path, proj, {"mode": "readonly"})
    with pytest.raises(SystemExit):
        cli.run(mf, out_override=str(tmp_path / "o"),
                state_dir=str(proj / ".steward"))


# ---------------------------------------------------------------- stamp (R1)

def test_stamp_existing_frontmatter(tmp_path):
    p = write(tmp_path, "artifact.md",
              "---\ntitle: keep me\nproduced_by: old-model\n---\n# body\n")
    cli.stamp_file(str(p), {"produced_by": "claude-haiku-4-5",
                            "task": "extract", "round": 3})
    text = p.read_text(encoding="utf-8")
    fm, err = cli.read_frontmatter(str(p))
    assert err is None
    assert fm["title"] == "keep me"            # untouched keys survive verbatim
    assert fm["produced_by"] == "claude-haiku-4-5"  # updated in place
    assert fm["task"] == "extract" and fm["round"] == 3
    assert text.endswith("# body\n")


def test_stamp_creates_frontmatter(tmp_path):
    p = write(tmp_path, "plain.md", "just text\n")
    cli.stamp_file(str(p), {"produced_by": "human", "task": "admission"})
    fm, err = cli.read_frontmatter(str(p))
    assert err is None and fm["produced_by"] == "human"
    assert p.read_text(encoding="utf-8").endswith("just text\n")


def test_stamp_cli_roundtrip_with_allocation_probe(tmp_path):
    """stamp output must be exactly what allocation_compliance consumes."""
    p = write(tmp_path, "reports/07/r.md", "# report\n")
    cli.stamp_file(str(p), {"produced_by": "claude-opus-4-8", "task": "extract"})
    spec = {"id": "p", "glob": "reports/**/*.md",
            "tasks": {"extract": "mid"}, "tier_patterns": {"mid": ["*opus*"]}}
    assert cli.probe_allocation_compliance(str(tmp_path), spec)["status"] == "pass"


def test_probe_ref_integrity(tmp_path):
    write(tmp_path, "facts/2026/a.md", "---\nid: F1\nedges:\n"
                                       "  - { to: F2, rel: contradicts }\n"
                                       "  - { to: GHOST, rel: corroborates }\n---\nbody\n")
    write(tmp_path, "facts/2026/b.md", "---\nid: F2\nbuilds_on: F1\n---\nbody\n")
    write(tmp_path, "insights/cap/i.md", "---\nid: I1\nbuilds_on: [F1, MISSING, TPL-0001]\n---\nbody\n")
    spec = {"id": "p", "glob": ["facts/**/*.md", "insights/**/*.md"],
            "field": ["edges.to", "builds_on"], "ignore": ["TPL-*"],
            "severity": "warn"}
    r = cli.probe_ref_integrity(str(tmp_path), spec)
    assert r["status"] == "warn" and r["n_checked"] == 3
    joined = "\n".join(r["violations"])
    assert "GHOST" in joined and "MISSING" in joined      # dangling refs flagged
    assert len(r["violations"]) == 2                       # TPL-0001 ignored, rest resolve
    # scalar field value + list-of-dict traversal both counted (2 + 1 + 3)
    assert "6 refs" in r["detail"]
    # no field configured / empty corpus degrade to skipped, never crash
    assert cli.probe_ref_integrity(str(tmp_path), {"id": "p", "glob": "facts/**/*.md"})["status"] == "skipped"
    assert cli.probe_ref_integrity(str(tmp_path), {"id": "p", "glob": "nope/**", "field": "x"})["status"] == "skipped"


def test_probe_cmd_missing_tool(tmp_path):
    r = cli.probe_cmd(str(tmp_path), {"id": "p", "cmd": "no-such-tool-xyz --check"})
    assert r["status"] == "skipped" and "missing tool" in r["detail"]
    # a real failure still fails — only rc=127 is a dependency problem
    r = cli.probe_cmd(str(tmp_path), {"id": "p", "cmd": "false"})
    assert r["status"] == "fail"


def test_source_quote_drift(tmp_path):
    write(tmp_path, "docs/rules.md", "## caps\nself_declarative cap is 0.9 here\n")
    mf = {"probes": [
        {"id": "ok", "type": "x", "source_file": "docs/rules.md",
         "source_quote": "self_declarative cap is 0.9"},
        {"id": "drifted", "type": "x", "source_file": "docs/rules.md",
         "source_quote": "self_declarative cap is 0.6"},
        {"id": "gone", "type": "x", "source_file": "docs/nope.md",
         "source_quote": "whatever"},
    ]}
    out = cli.check_source_quotes(mf, str(tmp_path))
    joined = "\n".join(out)
    assert len(out) == 2 and "ok" not in joined
    assert "'drifted'" in joined and "'gone'" in joined


def test_single_source_cap_class_field(tmp_path):
    """Caps keyed off a manifest-named field (insights use origin, not
    claim_class) — expert 0.6 passes at 0.55, bare 0.5 flags it."""
    write(tmp_path, "insights/x/e.md",
          "---\nid: I1\norigin: expert\nsources: [s]\nconfidence: 0.55\n---\nb\n")
    write(tmp_path, "insights/x/b.md",
          "---\nid: I2\nsources: [s]\nconfidence: 0.55\n---\nb\n")
    spec = {"id": "p", "glob": "insights/**/*.md", "default_cap": 0.5,
            "class_field": "origin", "class_caps": {"expert": 0.6},
            "severity": "warn"}
    r = cli.probe_single_source_cap(str(tmp_path), spec)
    assert r["n_violations"] == 1 and "origin=-" in r["violations"][0]


def test_single_source_cap_exempt(tmp_path):
    write(tmp_path, "insights/x/a.md",
          "---\nid: I1\nsources: [s1]\nconfidence: 0.8\n"
          "g1_exempt: founding methodology quote\n---\nbody\n")
    write(tmp_path, "insights/x/b.md",
          "---\nid: I2\nsources: [s1]\nconfidence: 0.8\n---\nbody\n")
    spec = {"id": "p", "glob": "insights/**/*.md", "default_cap": 0.5,
            "exempt_field": "g1_exempt", "severity": "warn"}
    r = cli.probe_single_source_cap(str(tmp_path), spec)
    assert r["n_violations"] == 1 and "I2" not in r["violations"][0]  # b flagged
    assert "1 exempted" in r["detail"]


def test_fixes_ledger_and_report(tmp_path):
    sdir = tmp_path / ".steward"
    sdir.mkdir()
    cli.record_fixes(str(sdir), "p", {"probe-a": ["v1", "v2", "v3", "v4"]})
    cli.record_fixes(str(sdir), "p", {})                    # no-op
    rows = [json.loads(x) for x in open(sdir / "fixes.jsonl")]
    assert len(rows) == 1 and rows[0]["n"] == 4 and len(rows[0]["examples"]) == 3
    # report surfaces the scoreboard + M4 rule feedback with notes
    (sdir / "queue.jsonl").write_text(json.dumps(
        {"id": "x1", "probe": "probe-a", "text": "t", "impact": 1, "score": 1,
         "status": "not_worth", "verdict_note": "rule mis-parameterised"}) + "\n",
        encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    r = subprocess.run([sys.executable, "-m", "agent_steward.cli", "report",
                        "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "Fixed so far: 4 violations resolved" in r.stdout
    assert "rule feedback 'probe-a'" in r.stdout
    assert "rule mis-parameterised" in r.stdout


def test_allocation_compliance_transition_semantics(tmp_path):
    """A stamp is compliant if it matches the table of ITS day OR today's —
    a promote must not criminalise yesterday's cheap work, nor early adoption
    the table later ratified. Matching NEITHER is the real violation."""
    alloc = {"tasks": [{"id": "extract", "tier": "mid"},
                       {"id": "verify", "tier": "cheap"}],
             "tier_patterns": {"cheap": ["*haiku*"], "mid": ["*sonnet*"]},
             "history": [{"at": "2026-07-07T11:20:27", "task": "extract",
                          "from": "cheap", "to": "mid"}]}
    write(tmp_path, ".allocation.yaml", yaml.safe_dump(alloc))
    write(tmp_path, "facts/x/haiku-then.md",     # matched table of its day
          "---\nid: F1\nproduced_by: claude-haiku-4-5\ntask: extract\n"
          "stamped_at: \"2026-07-07T09:42:00\"\n---\nb\n")
    write(tmp_path, "facts/x/sonnet-early.md",   # early adoption, later ratified
          "---\nid: F2\nproduced_by: claude-sonnet-5\ntask: extract\n"
          "stamped_at: \"2026-07-07T10:30:00\"\n---\nb\n")
    write(tmp_path, "facts/x/never-ok.md",       # matches neither -> violation
          "---\nid: F3\nproduced_by: claude-sonnet-5\ntask: verify\n"
          "stamped_at: \"2026-07-07T12:30:00\"\n---\nb\n")
    spec = {"id": "p", "type": "allocation_compliance", "glob": "facts/**/*.md",
            "allocation_file": ".allocation.yaml", "severity": "warn"}
    r = cli.probe_allocation_compliance(str(tmp_path), spec)
    assert r["n_violations"] == 1 and "never-ok.md" in r["violations"][0]


def test_route_false_probes_stay_out_of_queue():
    mf = {"probes": [
        {"id": "lint", "type": "cmd", "route": False},
        {"id": "real", "type": "x", "severity": "warn"},
    ]}
    items = route_mod.build_queue(mf, {"lint": ["noise1", "noise2"],
                                       "real": ["thing"]}, {}, "t0")
    assert len(items) == 1 and next(iter(items.values()))["probe"] == "real"


def test_validate_manifest_catches_author_mistakes():
    """The exact mistakes made during the final exam must be caught up front."""
    mf = {"probes": [
        {"id": "a", "type": "csv_required_columns", "glob": "x.csv",
         "required": ["date"]},                      # wrong param name (real incident)
        {"id": "b", "type": "jsonl_wellformd", "path": "x"},   # typo'd type
        {"id": "c", "type": "filename_pattern", "glob": "*", "patterns": ["[bad"]},
        {"id": "ok", "type": "file_exists", "path": "x", "risk_weight": 1.0,
         "source": "s"},
    ]}
    out = cli.validate_manifest(mf)
    joined = "\n".join(out)
    assert "missing required parameter 'columns'" in joined
    assert "unknown parameter 'required'" in joined
    assert "did you mean 'jsonl_wellformed'" in joined
    assert "bad regex" in joined
    assert "'ok'" not in joined                       # clean probe stays silent
    # every registered probe type has a parameter contract
    assert set(cli.PROBE_PARAMS) == set(cli.PROBES)


# ---------------------------------------------------------------- ledger (R1)

def test_log_task_appends_jsonl(tmp_path):
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    for i in range(2):
        r = subprocess.run(
            [sys.executable, "-m", "agent_steward.cli", "log-task",
             "--task", "extract", "--tier", "mid", "--model", "claude-opus-4-8",
             "--est-tokens", "1200", "--result", "pass",
             "--state-dir", str(tmp_path / ".steward")],
            capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
    lines = open(tmp_path / ".steward" / "usage_ledger.jsonl").read().splitlines()
    assert len(lines) == 2
    entry = json.loads(lines[0])
    assert entry["task"] == "extract" and entry["tier"] == "mid"
    assert entry["est_tokens"] == 1200 and entry["result"] == "pass" and entry["ts"]


def test_log_task_warns_on_tier_model_mismatch(tmp_path):
    """Observe-only: a tier/model contradiction warns on stderr but the entry
    is still appended (append-only ledger, exit 0)."""
    from agent_steward import allocate as am
    alloc = am.build_allocation({"tasks": [axes_task("condense", "med", "med", "med")]})
    apath = tmp_path / ".allocation.yaml"
    am.write_allocation(alloc, str(apath))
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli", "log-task",
            "--task", "condense", "--model", "claude-sonnet-5",
            "--allocation", str(apath), "--state-dir", str(tmp_path / ".steward")]
    r = subprocess.run(base + ["--tier", "top"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "matches tier(s) mid" in r.stderr
    r2 = subprocess.run(base + ["--tier", "mid"], capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and "warning" not in r2.stderr
    lines = open(tmp_path / ".steward" / "usage_ledger.jsonl").read().splitlines()
    assert len(lines) == 2  # both entries kept, mismatch included


# ------------------------------------------- allocation layer (R2, zero-manual)

from agent_steward import allocate as alloc_mod  # noqa: E402


def axes_task(tid, v, j, b, vol="med"):
    return {"id": tid, "verifiable": v, "judgment": j, "blast_radius": b,
            "volume": vol, "rationale": "test"}


def test_assess_matrix_all_branches():
    a = alloc_mod.assess(axes_task("adm", "low", "high", "high"))
    assert (a["tier"], a["floor"]) == ("top", "top")          # judgment high + blast high
    a = alloc_mod.assess(axes_task("condense", "med", "med", "med"))
    assert (a["tier"], a["floor"]) == ("mid", "cheap")        # judgment med
    a = alloc_mod.assess(axes_task("extract", "high", "low", "med"))
    assert (a["tier"], a["floor"], a["canary"]) == ("cheap", "cheap", 0)
    a = alloc_mod.assess(axes_task("fuzzy", "low", "low", "low"))
    assert a["tier"] == "mid" and a["escalate_on"] == "low_confidence"
    a = alloc_mod.assess(axes_task("money-mech", "high", "low", "high"))
    assert (a["tier"], a["floor"]) == ("mid", "mid")          # floor lifts tier
    assert a["canary"] == 0.05                                 # above cheap + verifiable
    with pytest.raises(ValueError):
        alloc_mod.assess({"id": "bad", "verifiable": "yes"})


def test_allocate_init_cli(tmp_path):
    axes = {"tasks": [axes_task("extract", "high", "low", "med", "high"),
                      axes_task("admission", "low", "high", "high")]}
    ax = tmp_path / "axes.yaml"
    ax.write_text(yaml.safe_dump(axes), encoding="utf-8")
    out = tmp_path / ".allocation.yaml"
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    r = subprocess.run([sys.executable, "-m", "agent_steward.cli", "allocate", "init",
                        "--axes", str(ax), "--out", str(out)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    alloc = yaml.safe_load(out.read_text(encoding="utf-8"))
    tiers = {t["id"]: t for t in alloc["tasks"]}
    assert tiers["extract"]["tier"] == "cheap"
    assert tiers["admission"]["tier"] == "top" and tiers["admission"]["floor"] == "top"
    assert tiers["extract"]["assessed"]["verifiable"] == "high"  # audit trail kept
    # refuses silent overwrite
    r2 = subprocess.run([sys.executable, "-m", "agent_steward.cli", "allocate", "init",
                         "--axes", str(ax), "--out", str(out)],
                        capture_output=True, text=True, env=env)
    assert r2.returncode == 1 and "already exists" in r2.stderr


def ledger_write(sdir, rows):
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "usage_ledger.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_tune_demote_promote_floor(tmp_path):
    alloc = alloc_mod.build_allocation({"tasks": [
        axes_task("condense", "high", "med", "med"),     # mid, floor cheap -> demotable
        axes_task("triage", "high", "low", "low"),       # cheap -> promotable on failures
        axes_task("staging", "high", "low", "high"),     # mid, floor mid -> NOT demotable
    ]})
    rows = ([{"task": "condense", "tier": "mid", "result": "pass"}] * 25
            + [{"task": "triage", "tier": "cheap", "result": "fail"}] * 3
            + [{"task": "triage", "tier": "cheap", "result": "pass"}] * 3
            + [{"task": "staging", "tier": "mid", "result": "pass"}] * 25
            + [{"task": "ghost", "tier": "mid", "result": "pass"}])
    proposals, unallocated = alloc_mod.tune_proposals(alloc, rows)
    moves = {p["task"]: (p["from"], p["to"]) for p in proposals}
    assert moves["condense"] == ("mid", "cheap")     # clean record -> demote
    assert moves["triage"] == ("cheap", "mid")       # 50% escalation -> promote
    assert "staging" not in moves                    # floor respected
    assert unallocated == ["ghost"]                  # recursive growth path
    alloc_mod.apply_proposals(alloc, proposals)
    assert {t["id"]: t["tier"] for t in alloc["tasks"]}["condense"] == "cheap"
    assert alloc["history"][0]["task"] in ("condense", "triage")


def test_ledger_mismatches_unit():
    alloc = {"tier_patterns": {"cheap": ["*haiku*"], "mid": ["*sonnet*", "*opus*"],
                               "top": ["*fable*", "human"]}}
    rows = [
        {"ts": "t1", "task": "condense", "tier": "top", "model": "claude-sonnet-5"},
        # self-report alias hits both mid patterns -> consistent, wording issue only
        {"ts": "t2", "task": "condense", "tier": "mid",
         "model": "claude-sonnet-alias-selfreport-opus-4-5"},
        {"ts": "t3", "task": "triage", "tier": "cheap", "model": "gpt-99"},
        {"ts": "t4", "task": "adm", "tier": "top", "model": "human"},
        {"ts": "t5", "task": "x", "tier": "mid"},  # no model -> skipped
    ]
    mism, unknown = alloc_mod.ledger_mismatches(alloc, rows)
    assert [m["ts"] for m in mism] == ["t1"] and mism[0]["matches_tiers"] == ["mid"]
    assert [u["ts"] for u in unknown] == ["t3"]
    assert alloc_mod.ledger_mismatches({}, rows) == ([], [])  # no patterns -> silent


def test_tune_reports_ledger_mismatch(tmp_path):
    alloc = alloc_mod.build_allocation({"tasks": [axes_task("condense", "med", "med", "med")]})
    apath = tmp_path / ".allocation.yaml"
    alloc_mod.write_allocation(alloc, str(apath))
    sdir = tmp_path / ".steward"
    ledger_write(str(sdir), [{"ts": "2026-01-01T00:00:00", "task": "condense",
                              "tier": "top", "model": "claude-sonnet-5", "result": "pass"}])
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    r = subprocess.run([sys.executable, "-m", "agent_steward.cli", "allocate", "tune",
                        "--allocation", str(apath), "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "ledger data-quality" in r.stdout and "claude-sonnet-5" in r.stdout


# ---------------------------------------------------------------- canary (R3)

def test_canary_decision_deterministic():
    alloc = alloc_mod.build_allocation({"tasks": [
        axes_task("extract", "high", "low", "med"),    # cheap -> nothing below
        axes_task("staging", "high", "low", "high"),   # mid, floor mid -> forbidden
        axes_task("condense", "high", "med", "med"),   # mid, floor cheap, canary .05
    ]})
    assert alloc_mod.canary_decision(alloc, [], "ghost")["run"] is False
    assert alloc_mod.canary_decision(alloc, [], "extract")["run"] is False
    d = alloc_mod.canary_decision(alloc, [], "staging")
    assert d["run"] is False and "floor" in d["reason"]
    d = alloc_mod.canary_decision(alloc, [], "condense")   # run #0 fires
    assert d["run"] is True and d["shadow_tier"] == "cheap" and d["interval"] == 20
    rows = [{"task": "condense", "tier": "mid"}] * 19
    assert alloc_mod.canary_decision(alloc, rows, "condense")["run"] is False
    rows += [{"task": "condense", "tier": "mid"}]           # 20th primary
    assert alloc_mod.canary_decision(alloc, rows, "condense")["run"] is True
    rows += [{"task": "condense", "tier": "cheap", "canary": "shadow"}]
    assert alloc_mod.canary_decision(alloc, rows, "condense")["run"] is True  # shadows don't count


def test_tune_canary_parity_and_veto():
    alloc = alloc_mod.build_allocation({"tasks": [
        axes_task("condense", "high", "med", "med"),   # mid, floor cheap
        axes_task("summar", "high", "med", "med"),     # mid, floor cheap
    ]})
    parity = ([{"task": "condense", "tier": "mid", "result": "pass"}] * 25
              + [{"task": "condense", "tier": "cheap", "canary": "shadow",
                  "quality": "same"}] * 5)
    gap = ([{"task": "summar", "tier": "mid", "result": "pass"}] * 25
           + [{"task": "summar", "tier": "cheap", "canary": "shadow",
               "quality": "worse"}] * 5)
    proposals, _ = alloc_mod.tune_proposals(alloc, parity + gap)
    moves = {p["task"]: p for p in proposals}
    assert moves["condense"]["to"] == "cheap"                  # parity -> demote
    assert "canary quality parity" in moves["condense"]["reason"]
    assert "summar" not in moves     # measured gap vetoes the esc-rate demote
    # apply recomputes canary from axes: cheap tier -> sampling off
    alloc_mod.apply_proposals(alloc, proposals)
    t = {t["id"]: t for t in alloc["tasks"]}["condense"]
    assert t["tier"] == "cheap" and t["canary"] == 0


def test_cpau_and_shadow_separation():
    alloc = alloc_mod.build_allocation({"tasks": [axes_task("x", "high", "med", "med")]})
    rows = [{"task": "x", "tier": "mid", "est_tokens": 1000, "result": "pass"},
            {"task": "x", "tier": "mid", "est_tokens": 1000, "result": "escalated"},
            {"task": "x", "tier": "cheap", "est_tokens": 1000,
             "canary": "shadow", "quality": "same"}]
    cpau = alloc_mod.cpau_by_task(rows, alloc)["x"]
    assert cpau["runs"] == 2 and cpau["accepted"] == 1      # shadow excluded
    assert cpau["cpau"] == 16000                             # (8000+8000)/1
    sav = alloc_mod.compute_savings(rows, alloc)
    assert sav["canary_runs"] == 1 and sav["canary_cost"] == 1000
    assert sav["actual_cost"] == 16000                       # shadow kept out


def test_canary_and_tune_only_cli(tmp_path):
    alloc = alloc_mod.build_allocation({"tasks": [
        axes_task("condense", "high", "med", "med"),
        axes_task("triage", "high", "low", "low")]})
    apath = tmp_path / ".allocation.yaml"
    alloc_mod.write_allocation(alloc, str(apath))
    sdir = tmp_path / ".steward"
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli"]
    r = subprocess.run(base + ["canary", "--task", "condense", "--allocation",
                               str(apath), "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "shadow-run tier 'cheap'" in r.stdout
    r = subprocess.run(base + ["canary", "--task", "triage", "--allocation",
                               str(apath), "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1                                  # cheap: nothing below
    # pair logging round-trips through the ledger
    r = subprocess.run(base + ["log-task", "--task", "condense", "--tier", "cheap",
                               "--canary", "shadow", "--pair", "r6-w1",
                               "--quality", "same", "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    entry = json.loads(open(sdir / "usage_ledger.jsonl").read().splitlines()[-1])
    assert (entry["canary"], entry["pair"], entry["quality"]) == ("shadow", "r6-w1", "same")
    # --only applies one proposal and leaves the rest printed but untouched
    ledger_write(str(sdir), [{"task": "condense", "tier": "mid", "result": "pass"}] * 25
                 + [{"task": "triage", "tier": "cheap", "result": "fail"}] * 5)
    r = subprocess.run(base + ["allocate", "tune", "--allocation", str(apath),
                               "--state-dir", str(sdir), "--apply", "--only", "triage"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "leaving condense untouched" in r.stdout
    after = {t["id"]: t for t in alloc_mod.load_allocation(str(apath))["tasks"]}
    assert after["triage"]["tier"] == "mid"                   # applied
    assert after["condense"]["tier"] == "mid"                 # untouched


# ------------------------------------------------------ ingest-usage (metering)

from agent_steward import ingest as ingest_mod  # noqa: E402


def tline(model, out_tok, ts="2026-07-07T10:00:00.000Z", typ="assistant", **kw):
    return json.dumps({"type": typ, "timestamp": ts,
                       "message": {"model": model, "usage": {
                           "input_tokens": 100, "output_tokens": out_tok,
                           "cache_read_input_tokens": 5000,
                           "cache_creation_input_tokens": 50}, **kw}}) + "\n"


def test_ingest_transcripts(tmp_path):
    tdir = tmp_path / "transcripts"
    (tdir / "sess1" / "subagents").mkdir(parents=True)
    # main session: two models (fable main + embedded sidechain)
    (tdir / "sess1.jsonl").write_text(
        tline("claude-fable-5", 10) + tline("claude-fable-5", 20)
        + json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
        + tline("<synthetic>", 99), encoding="utf-8")
    # worker with a [task=...] marker in the dispatch prompt
    (tdir / "sess1" / "subagents" / "agent-a1.jsonl").write_text(
        json.dumps({"type": "user", "message": {
            "content": "[task=extract] 你是 worker W1,抽取以下論文"}}) + "\n"
        + tline("claude-sonnet-5", 500), encoding="utf-8")
    # worker attributed via task-map regex
    (tdir / "sess1" / "subagents" / "agent-a2.jsonl").write_text(
        json.dumps({"type": "user", "message": {
            "content": "任務類型:second-source hunt"}}) + "\n"
        + tline("claude-haiku-4-5", 300), encoding="utf-8")
    alloc = {"tier_patterns": {"cheap": ["*haiku*"], "mid": ["*sonnet*"],
                               "top": ["*fable*"]}}
    sdir = tmp_path / ".steward"
    paths = ingest_mod.scan_transcripts(str(tdir))
    assert len(paths) == 3
    entries = ingest_mod.ingest(paths, str(sdir), alloc=alloc,
                                task_map={"verify_fact": ["second-source"]},
                                now="2026-07-07T12:00:00")
    by_task = {e["task"]: e for e in entries}
    assert by_task["_session"]["model"] == "claude-fable-5"
    assert by_task["_session"]["est_tokens"] == 230        # (100+10)+(100+20)
    assert by_task["_session"]["measured"]["cache_read"] == 10000
    assert by_task["extract"]["tier"] == "mid"             # marker + patterns
    assert by_task["verify_fact"]["tier"] == "cheap"       # task-map regex
    assert all(e["via"] == "transcript" for e in entries)
    assert "<synthetic>" not in {e["model"] for e in entries}
    # cursor: re-ingest adds nothing; appended lines add only the delta
    assert ingest_mod.ingest(paths, str(sdir), alloc=alloc) == []
    with open(tdir / "sess1.jsonl", "a", encoding="utf-8") as f:
        f.write(tline("claude-fable-5", 7))
    delta = ingest_mod.ingest(paths, str(sdir), alloc=alloc)
    assert len(delta) == 1 and delta[0]["est_tokens"] == 107
    lines = open(sdir / "usage_ledger.jsonl").read().splitlines()
    assert len(lines) == 4


def test_measured_entries_stay_out_of_quality_loops():
    alloc = alloc_mod.build_allocation({"tasks": [
        axes_task("condense", "high", "med", "med")]})   # mid, canary .05
    measured = [{"task": "condense", "tier": "mid", "est_tokens": 1000,
                 "via": "transcript"}] * 25
    proposals, _ = alloc_mod.tune_proposals(alloc, measured)
    assert proposals == []                    # no verdicts -> no tier changes
    d = alloc_mod.canary_decision(alloc, measured, "condense")
    assert d["n"] == 0                        # cadence counts explicit runs only
    sav = alloc_mod.compute_savings(measured, alloc)
    assert sav["actual_cost"] == 25 * 8000    # money view counts them
    cpau = alloc_mod.cpau_by_task(measured, alloc)["condense"]
    assert cpau["accepted"] == 25             # silence = accepted (measured)


# ---------------------------------------------------------------- route (V2)

from agent_steward import route as route_mod  # noqa: E402


ROUTE_MF = {"project": "p", "probes": [
    {"id": "hot", "type": "x", "severity": "fail", "risk_weight": 3.0,
     "source": "iron rule 1"},
    {"id": "cold", "type": "x", "severity": "warn"},
]}


def test_route_scoring_and_verdict_survival():
    viol = {"hot": ["money path broken"], "cold": ["cosmetic a", "cosmetic b"]}
    items = route_mod.build_queue(ROUTE_MF, viol, {}, "t0")
    ranked = sorted(items.values(), key=lambda x: -x["score"])
    assert ranked[0]["probe"] == "hot" and ranked[0]["score"] == 3.0  # fail 1.0 × risk 3.0
    assert ranked[1]["score"] == 0.6                                   # warn default risk
    iid = ranked[0]["id"]
    items[iid]["status"] = "worth"
    # violation fixed + one cosmetic fixed -> adjudicated history survives (M4 data)
    items2 = route_mod.build_queue(ROUTE_MF, {"cold": ["cosmetic a"]}, items, "t1")
    assert items2[iid]["status"] == "worth"
    assert sum(1 for i in items2.values() if i["status"] == "pending") == 1
    assert route_mod.m4_precision(items2) == (1.0, 1, 0)


def test_route_judge_failopen(monkeypatch):
    items = route_mod.build_queue(ROUTE_MF, {"cold": ["x"]}, {}, "t0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(route_mod.shutil, "which", lambda _: None)
    n, msg = route_mod.run_judge(items, ROUTE_MF)
    assert n == 0 and "deterministic order only" in msg   # no key, no CLI -> degrade
    # with a `claude` CLI on PATH the user's existing login is the backend
    fake = route_mod.build_queue(ROUTE_MF, {"cold": ["z"]}, {}, "t0")
    fid = next(iter(fake))
    monkeypatch.setattr(route_mod.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(route_mod.subprocess, "run", lambda *a, **k: type(
        "P", (), {"returncode": 0, "stdout": json.dumps(
            [{"id": fid, "score": 0.3, "reason": "cli judged"}]), "stderr": ""})())
    n, msg = route_mod.run_judge(fake, ROUTE_MF)
    assert n == 1 and "existing login" in msg and fake[fid]["judge"]["score"] == 0.3
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    iid = next(iter(items))
    ok_payload = {"content": [{"type": "text", "text": json.dumps(
        [{"id": iid, "score": 0.2, "reason": "mechanical noise"}])}]}
    n, msg = route_mod.run_judge(items, ROUTE_MF, _post=lambda body: ok_payload)
    assert n == 1 and items[iid]["judge"]["score"] == 0.2
    assert items[iid]["score"] == round(items[iid]["impact"] * 0.2, 4)

    def boom(body):
        raise OSError("no network")
    fresh = route_mod.build_queue(ROUTE_MF, {"cold": ["y"]}, {}, "t0")
    n, msg = route_mod.run_judge(fresh, ROUTE_MF, _post=boom)
    assert n == 0 and "failed" in msg                     # network error -> degrade
    assert route_mod.parse_judge_reply({"content": [{"type": "text",
                                                     "text": "not json"}]}, {iid}) == []


def test_route_and_approve_cli(tmp_path):
    sdir = tmp_path / ".steward"
    sdir.mkdir()
    mfp = tmp_path / "m.yaml"
    mfp.write_text(yaml.safe_dump(ROUTE_MF), encoding="utf-8")
    (sdir / "state.json").write_text(json.dumps({"projects": {"p": {
        "ran_at": "t", "violations": {"hot": ["broken thing"],
                                      "cold": ["meh"]}}}}), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli"]
    r = subprocess.run(base + ["route", "--manifest", str(mfp),
                               "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "2 pending" in r.stdout
    top_id = r.stdout.splitlines()[1].split()[0]          # highest score first
    assert "broken thing" in r.stdout.splitlines()[1]
    r = subprocess.run(base + ["approve", top_id, "--verdict", "worth",
                               "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "M4 residue precision: 1.0" in r.stdout
    # unknown item fails loudly, queue intact
    r = subprocess.run(base + ["approve", "nope", "--verdict", "not-worth",
                               "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1


def test_distill_reasons_and_queue():
    rows = ([{"reason": "no_signal: 已過期 — case A"}] * 4
            + [{"reason": "no_signal: 已過期 — case B"}]
            + [{"reason": "duplicate — seen before"}] * 3
            + [{"reason": "one-off oddity"}]
            + [{"nofield": 1}])
    clusters = route_mod.distill(rows, min_count=3)
    assert [(c["key"], c["n"]) for c in clusters] == [
        ("no_signal: 已過期", 5), ("duplicate", 3)]      # one-off dropped
    assert len(clusters[0]["examples"]) == 3
    items = {
        "a": {"probe": "p1", "status": "not_worth", "verdict_note": "noise"},
        "b": {"probe": "p1", "status": "not_worth", "verdict_note": None},
        "c": {"probe": "p2", "status": "worth", "verdict_note": "real"},
        "d": {"probe": "p2", "status": "worth", "verdict_note": "real too"},
        "e": {"probe": "p3", "status": "pending"},
    }
    noise, signal = route_mod.distill_queue(items)
    assert noise[0]["probe"] == "p1" and noise[0]["n"] == 2
    assert signal[0]["probe"] == "p2" and signal[0]["n"] == 2


def test_distill_cli_and_report_needs_you(tmp_path):
    log = tmp_path / "adm.jsonl"
    log.write_text("\n".join(
        [json.dumps({"verdict": "reject", "reason": "no_signal: benchmark 層 — x"})] * 3
        + [json.dumps({"verdict": "admit", "reason": "no_signal: benchmark 層 — y"})] * 5),
        encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    r = subprocess.run([sys.executable, "-m", "agent_steward.cli", "distill",
                        "--path", str(log), "--where", "verdict=reject"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "3 records, 1 recurring" in r.stdout and "no_signal: benchmark 層" in r.stdout
    # report leads with What needs you: conflicts + queue top surface there
    sdir = tmp_path / ".steward"
    sdir.mkdir()
    (sdir / "state.json").write_text(json.dumps({"projects": {"p": {
        "ran_at": "t", "violations": {"hot": ["boom"]},
        "conflicts": ["CONFLICT: probes 'a' and 'b' disagree"],
        "metrics": {}}}}), encoding="utf-8")
    (sdir / "queue.jsonl").write_text(json.dumps(
        {"id": "q1", "probe": "hot", "text": "boom", "impact": 1, "score": 1,
         "status": "pending"}) + "\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "agent_steward.cli", "report",
                        "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    head = r.stdout.split("## Savings")[0]
    assert "## What needs you" in head
    assert "rule problem" in head and "disagree" in head
    assert "queue top" in head and "boom" in head


def test_report_rule_check_and_fix_categories(tmp_path):
    """The authorize-per-category view: rule counts, then one row per failing
    category with its manifest-declared fix guidance."""
    proj = tmp_path / "proj"
    write(proj, "facts/2026/bad.md", "---\nid: f1\n---\nbody\n")
    extra = {"probes": [
        {"id": "schema-floor", "type": "frontmatter_required",
         "glob": "facts/**/*.md", "required": ["id", "confidence"],
         "severity": "warn", "fixable_by": "agent",
         "fix": "fill the missing fields from the source"},
        # two rules governing the same field with incompatible params ->
        # the conflict category must lead the authorization table
        {"id": "enum-a", "type": "field_value_rule", "glob": "facts/**/*.md",
         "field": "origin", "allowed": ["x"], "severity": "warn",
         "require_present": False},
        {"id": "enum-b", "type": "field_value_rule", "glob": "facts/**/*.md",
         "field": "origin", "allowed": ["x", "y"], "severity": "warn",
         "require_present": False},
    ]}
    manifest = make_manifest(tmp_path, proj, extra)
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    sdir = tmp_path / "st"
    subprocess.run([sys.executable, "-m", "agent_steward.cli", "check",
                    "--manifest", str(manifest), "--state-dir", str(sdir),
                    "--out", str(tmp_path / "o")],
                   capture_output=True, text=True, env=env)
    r = subprocess.run([sys.executable, "-m", "agent_steward.cli", "report",
                        "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "## Rule check — fixture-project" in r.stdout
    assert "rules checked" in r.stdout
    assert "authorize fixes per row" in r.stdout
    assert "fill the missing fields from the source | agent" in r.stdout
    assert "**rule conflicts** | 1" in r.stdout          # leads the table
    assert "only you can say which is right" in r.stdout
    assert "rule conflict detail: " in r.stdout          # both sides named
    assert "'enum-a' and 'enum-b'" in r.stdout


def test_init_and_baseline_cli(tmp_path):
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli"]
    out = tmp_path / "m.yaml"
    r = subprocess.run(base + ["init", "--out", str(out), "--project", "demo",
                               "--root", str(tmp_path)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "OBSERVE FIRST" in r.stdout
    mf = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert mf["project"] == "demo" and mf["mode"] == "apply"
    r2 = subprocess.run(base + ["init", "--out", str(out)],
                        capture_output=True, text=True, env=env)
    assert r2.returncode == 1 and "already exists" in r2.stderr
    # baseline = check that seeds diff state, with intent said out loud
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    manifest = make_manifest(tmp_path, proj)
    r3 = subprocess.run(base + ["baseline", "--manifest", str(manifest),
                                "--state-dir", str(tmp_path / "st"),
                                "--out", str(tmp_path / "o")],
                        capture_output=True, text=True, env=env)
    assert r3.returncode == 0, r3.stderr
    assert "seeding baseline" in r3.stdout
    assert (tmp_path / "st" / "state.json").exists()


def test_savings_math(tmp_path):
    alloc = alloc_mod.build_allocation({"tasks": [
        axes_task("extract", "high", "low", "med"),
        axes_task("admission", "low", "high", "high")]})
    rows = [{"task": "extract", "tier": "mid", "est_tokens": 1000, "result": "pass"},
            {"task": "extract", "tier": "mid", "est_tokens": 1000, "result": "escalated"},
            {"task": "admission", "tier": "top", "est_tokens": 500, "result": "pass"},
            {"task": "extract", "tier": "mid"}]  # no tokens -> counted separately
    sav = alloc_mod.compute_savings(rows, alloc)
    # weights 1:8:25 -> actual = 2*8000 + 12500 = 28500; all-top = 2*25000 + 12500 = 62500
    assert sav["actual_cost"] == 28500 and sav["top_cost"] == 62500
    assert sav["entries_by_tier"] == {"mid": 2, "top": 1}
    assert sav["cost_by_tier"] == {"mid": 16000, "top": 12500}
    m = alloc_mod.escalation_matrix(alloc, rows)
    assert len(m) == 1 and (m[0]["task"], m[0]["from"], m[0]["to"], m[0]["n"]) == \
        ("extract", "mid", "top", 1)
    assert m[0]["trigger"] == "vr_fail"              # from the allocation table
    assert sav["saved_vs_top"] == 34000 and sav["saved_vs_top_pct"] == 54.4
    assert sav["no_tokens"] == 1 and sav["escalations"] == 1
    assert sav["saved_vs_initial_pct"] is None       # no tuning history yet


def test_rule_conflict_detection(tmp_path):
    mf = {"probes": [
        {"id": "a", "type": "field_value_rule", "glob": "insights/**/*.md",
         "field": "origin", "allowed": ["expert", "user"]},
        {"id": "b", "type": "field_value_rule", "glob": "insights/**/*.md",
         "field": "origin", "allowed": ["expert", "user", "synthesis"]},
        {"id": "c", "type": "single_source_cap", "glob": "facts/**/*.md",
         "default_cap": 0.5},
        {"id": "d", "type": "single_source_cap", "glob": "facts/**/*.md",
         "default_cap": 0.7},
        {"id": "d", "type": "file_exists", "path": "x"},  # duplicate id
    ]}
    conflicts = cli.detect_rule_conflicts(mf)
    joined = "\n".join(conflicts)
    assert len(conflicts) == 3
    assert "'a' and 'b'" in joined and "origin" in joined
    assert "'c' and 'd'" in joined and "0.5" in joined
    assert "duplicate probe id 'd'" in joined


def test_check_report_surfaces_conflicts_and_spend(tmp_path):
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    extra = {"probes": [
        {"id": "a", "type": "field_value_rule", "glob": "facts/**/*.md",
         "field": "origin", "allowed": ["expert"], "severity": "warn",
         "require_present": False},
        {"id": "b", "type": "field_value_rule", "glob": "facts/**/*.md",
         "field": "origin", "allowed": ["expert", "user"], "severity": "warn",
         "require_present": False},
    ]}
    manifest = make_manifest(tmp_path, proj, extra)
    state = tmp_path / "state"
    ledger_write(str(state), [
        {"task": "extract", "tier": "cheap", "est_tokens": 1000, "result": "pass",
         "project": "fixture-project"}])
    out = run_check(manifest, tmp_path / "o", state)
    report = open(os.path.join(out, "REPORT.md")).read()
    assert "Rule problems" in report and "CONFLICT" in report
    assert "Spend (estimated savings so far)" in report
    assert "vs everything-on-top" in report
    st = json.load(open(state / "state.json"))
    assert st["projects"]["fixture-project"]["conflicts"]
    assert st["projects"]["fixture-project"]["metrics"]["rule_conflicts"] == 1


def test_cumulative_report_cli(tmp_path):
    alloc = alloc_mod.build_allocation({"tasks": [axes_task("extract", "high", "low", "med")]})
    apath = tmp_path / ".allocation.yaml"
    alloc_mod.write_allocation(alloc, str(apath))
    sdir = tmp_path / ".steward"
    ledger_write(str(sdir), [
        {"task": "extract", "tier": "cheap", "est_tokens": 2000, "result": "pass"},
        {"task": "extract", "tier": "cheap", "est_tokens": 2000, "result": "fail"}])
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    r = subprocess.run([sys.executable, "-m", "agent_steward.cli", "report",
                        "--allocation", str(apath), "--state-dir", str(sdir)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "## Savings (estimated)" in r.stdout
    assert "vs everything-on-top: saved 96,000 (96.0%)" in r.stdout  # 2*2k*(25-1)
    assert "escalations: 1 of 2" in r.stdout
    assert "Cadence" in r.stdout


# ------------------------------------------- staleness_flag (V1)

def test_probe_staleness_flag(tmp_path):
    import time
    old = write(tmp_path, "facts/2026/old_unverified.md",
                "---\nid: f1\nverification_status: unverified\n---\nx\n")
    write(tmp_path, "facts/2026/fresh_unverified.md",
          "---\nid: f2\nverification_status: unverified\n---\nx\n")
    write(tmp_path, "facts/2026/old_verified.md",
          "---\nid: f3\nverification_status: verified\n---\nx\n")
    write(tmp_path, "facts/2026/old_by_field.md",
                  "---\nid: f4\nverification_status: unverified\n"
                  "created: 2026-01-01\n---\nx\n")
    forty_days_ago = time.time() - 40 * 86400
    os.utime(old, (forty_days_ago, forty_days_ago))
    spec = {"id": "p", "glob": "facts/**/*.md", "max_age_days": 30,
            "where": {"verification_status": "unverified"},
            "date_field": "created"}
    r = cli.probe_staleness_flag(str(tmp_path), spec)
    assert r["status"] == "warn"
    joined = "\n".join(r["violations"])
    assert "old_unverified.md" in joined and "(by mtime)" in joined   # mtime fallback
    assert "old_by_field.md" in joined and "(by created)" in joined   # field wins
    assert "fresh_unverified.md" not in joined                        # young enough
    assert "old_verified.md" not in joined                            # where excludes
    assert r["n_checked"] == 3  # only files matching where


# ------------------------------------------- hook contract (V1)

def steward_cli(*argv, cwd=None):
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    return subprocess.run([sys.executable, "-m", "agent_steward.cli", *argv],
                          capture_output=True, text=True, env=env, cwd=cwd)


def test_exit_new_codes(tmp_path):
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    manifest = make_manifest(tmp_path, proj)
    state = str(tmp_path / "state")
    r1 = steward_cli("check", "--manifest", manifest, "--diff", "--exit-new",
                     "--state-dir", state, "--out", str(tmp_path / "o1"))
    assert r1.returncode == 0, r1.stderr          # clean project -> 0
    write(proj, "facts/2026/bad.md", "---\nid: f9\n---\nx\n")
    r2 = steward_cli("check", "--manifest", manifest, "--diff", "--exit-new",
                     "--state-dir", state, "--out", str(tmp_path / "o2"))
    assert r2.returncode == 2                      # new violation -> 2
    assert "facts/2026/bad.md" in r2.stderr        # fed back on stderr
    r3 = steward_cli("check", "--manifest", manifest, "--diff", "--exit-new",
                     "--state-dir", state, "--out", str(tmp_path / "o3"))
    assert r3.returncode == 0                      # unchanged -> 0 (not new)


def test_install_hook(tmp_path):
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    manifest = make_manifest(tmp_path, proj)
    r = steward_cli("install-hook", "--manifest", manifest)
    assert r.returncode == 0, r.stderr
    spath = proj / ".claude" / "settings.json"
    settings = json.loads(spath.read_text(encoding="utf-8"))
    cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "--diff --exit-new" in cmd and str(proj) in cmd
    # idempotent
    r2 = steward_cli("install-hook", "--manifest", manifest)
    assert "already installed" in r2.stdout
    assert len(json.loads(spath.read_text())["hooks"]["Stop"]) == 1
    # merges, never clobbers existing settings
    settings["permissions"] = {"allow": ["Bash(ls:*)"]}
    spath.write_text(json.dumps(settings), encoding="utf-8")
    steward_cli("install-hook", "--manifest", manifest)  # no-op, but re-check preserved
    assert json.loads(spath.read_text())["permissions"]["allow"] == ["Bash(ls:*)"]


def test_install_hook_refuses_readonly(tmp_path):
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    manifest = make_manifest(tmp_path, proj, {"mode": "readonly"})
    r = steward_cli("install-hook", "--manifest", manifest)
    assert r.returncode == 1 and "readonly" in r.stderr


# ------------------------------------------- filename_pattern

def test_probe_filename_pattern(tmp_path):
    write(tmp_path, "wt/2026/05/AAPL_2026-05-01_review.md", "x\n")
    write(tmp_path, "wt/2026/05/2026-05-02_event_playbook.md", "x\n")
    write(tmp_path, "wt/2026/05/notes without convention.md", "x\n")
    spec = {"id": "p", "glob": "wt/2026/**/*.md",
            "patterns": [r"^[A-Z]{1,6}_\d{4}-\d{2}-\d{2}_\w+\.md$",
                         r"^\d{4}-\d{2}-\d{2}_\w+\.md$"]}
    r = cli.probe_filename_pattern(str(tmp_path), spec)
    assert r["status"] == "warn" and r["n_violations"] == 1 and r["n_checked"] == 3
    assert "notes without convention.md" in r["violations"][0]
    # bad regex degrades gracefully, never crashes
    r2 = cli.probe_filename_pattern(str(tmp_path), {"id": "p", "glob": "wt/2026/**/*.md",
                                                    "patterns": ["["]})
    assert r2["status"] == "skipped"


# ------------------------------------------- report time-slicing

def test_slice_periods_day_vs_week():
    day_rows = [{"ts": f"2026-07-0{d}T10:00:00"} for d in (1, 1, 3)]
    gran, periods, no_ts = alloc_mod.slice_periods(day_rows + [{"ts": "garbage"}])
    assert gran == "day" and no_ts == 1
    assert [(lbl, len(r)) for lbl, r in periods] == [("2026-07-01", 2), ("2026-07-03", 1)]
    week_rows = [{"ts": "2026-06-01T10:00:00"}, {"ts": "2026-07-07T10:00:00"}]
    gran, periods, _ = alloc_mod.slice_periods(week_rows)
    assert gran == "week" and len(periods) == 2 and periods[0][0].startswith("2026-W")


def test_filter_window():
    rows = [{"ts": "2026-07-01T10:00:00"}, {"ts": "2026-07-05T10:00:00"}]
    import datetime as dtmod
    out = alloc_mod.filter_window(rows, since=dtmod.datetime(2026, 7, 3))
    assert len(out) == 1 and out[0]["ts"].startswith("2026-07-05")
    assert alloc_mod.filter_window(rows) == rows


def test_tune_effect_measured():
    alloc = alloc_mod.build_allocation({"tasks": [axes_task("condense", "high", "med", "med")]})
    alloc["history"] = [{"at": "2026-07-05T00:00:00", "task": "condense",
                         "from": "mid", "to": "cheap", "reason": "r", "n": 20,
                         "esc_rate": 0.0}]
    rows = ([{"ts": "2026-07-04T10:00:00", "task": "condense", "tier": "mid",
              "est_tokens": 1000, "result": "pass"}] * 2
            + [{"ts": "2026-07-06T10:00:00", "task": "condense", "tier": "cheap",
                "est_tokens": 1000, "result": "pass"},
               {"ts": "2026-07-06T11:00:00", "task": "condense", "tier": "cheap",
                "est_tokens": 1000, "result": "fail"}])
    effects = alloc_mod.tune_effect(alloc, rows)
    assert len(effects) == 1
    b, a = effects[0]["before"], effects[0]["after"]
    assert b["n"] == 2 and b["cost_per_1k"] == 8000.0 and b["esc_rate"] == 0.0
    assert a["n"] == 2 and a["cost_per_1k"] == 1000.0 and a["esc_rate"] == 0.5


def test_report_cli_trend_and_since(tmp_path):
    alloc = alloc_mod.build_allocation({"tasks": [axes_task("extract", "high", "low", "med")]})
    apath = tmp_path / ".allocation.yaml"
    alloc_mod.write_allocation(alloc, str(apath))
    sdir = tmp_path / ".steward"
    ledger_write(str(sdir), [
        {"ts": "2026-07-01T10:00:00", "task": "extract", "tier": "cheap",
         "est_tokens": 1000, "result": "pass"},
        {"ts": "2026-07-03T10:00:00", "task": "extract", "tier": "cheap",
         "est_tokens": 3000, "result": "pass"}])
    r = steward_cli("report", "--allocation", str(apath), "--state-dir", str(sdir))
    assert r.returncode == 0, r.stderr
    assert "## Trend (per day)" in r.stdout
    assert "| 2026-07-01 | 1 | 1,000" in r.stdout
    r2 = steward_cli("report", "--allocation", str(apath), "--state-dir", str(sdir),
                     "--since", "2026-07-02")
    assert "window: 2026-07-02" in r2.stdout
    assert "metered: 1 ledger entries" in r2.stdout
    assert "## Trend" not in r2.stdout          # single period -> no trend table
    r3 = steward_cli("report", "--state-dir", str(sdir), "--since", "not-a-date")
    assert r3.returncode == 1 and "cannot parse" in r3.stderr
