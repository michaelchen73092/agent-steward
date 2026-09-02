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


def test_diff_violations_ignores_line_number_shift():
    # real-world shape (T-20260820-131): the same ruff context-frame line for
    # probe py-lint reported three times in one session, purely because the
    # target file grew above the flagged spot (14380 -> 14443 -> 14496) —
    # unchanged content, shifted position. Must not read as new+resolved.
    v14380 = "14380 |     # (39-守門b) 環境包白名單逐字鏡像(雲端抄的那份 == 這裡這份)"
    v14443 = "14443 |     # (39-守門b) 環境包白名單逐字鏡像(雲端抄的那份 == 這裡這份)"
    v14496 = "14496 |     # (39-守門b) 環境包白名單逐字鏡像(雲端抄的那份 == 這裡這份)"
    for prev_v, cur_v in ((v14380, v14443), (v14443, v14496)):
        new, resolved = cli.diff_violations({"py-lint": [prev_v]}, {"py-lint": [cur_v]})
        assert new == {}, new
        assert resolved == {}, resolved

    # ruff's `file:line:col:` header form shifts the same way — also stable
    h1 = "pkg/mod.py:14381:5: F841 local variable `x` assigned but never used"
    h2 = "pkg/mod.py:14444:5: F841 local variable `x` assigned but never used"
    new, resolved = cli.diff_violations({"py-lint": [h1]}, {"py-lint": [h2]})
    assert new == {} and resolved == {}


def test_diff_violations_still_catches_real_new_and_resolved():
    stale = "14380 |     # comment A — original finding"
    fresh = "9999 |     # comment B — a genuinely different finding"
    new, resolved = cli.diff_violations({"py-lint": [stale]}, {"py-lint": [fresh]})
    assert new == {"py-lint": [fresh]}
    assert resolved == {"py-lint": [stale]}


def test_diff_violations_counts_duplicate_fingerprints_by_multiset():
    # two identical-content violations at different line positions growing to
    # three occurrences: exactly one new (the extra occurrence), not zero
    # (fingerprint collapsed into a set) and not three (raw text compared).
    prev = {"py-lint": ["10 | dup", "20 | dup"]}
    cur = {"py-lint": ["11 | dup", "21 | dup", "31 | dup"]}
    new, resolved = cli.diff_violations(prev, cur)
    assert new == {"py-lint": ["31 | dup"]}
    assert resolved == {}


def test_diff_survives_growing_file_line_shift_end_to_end(tmp_path):
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    # a `cmd` probe shaped like the real py-lint one: nonzero exit + raw
    # grep -n output (line-numbered), so it fails exactly like ruff does
    # when it finds something to flag
    write(proj, "code.py", "x = 1\n" * 5 + "y = 2  # TODO real fix needed\n")
    manifest = make_manifest(tmp_path, proj, extra={"probes": [
        {"id": "fact-schema", "type": "frontmatter_required",
         "glob": "facts/**/*.md", "required": ["id", "confidence"],
         "severity": "warn", "source": "RULES.md §1"},
        {"id": "todo-scan", "type": "cmd", "cmd": "grep -n TODO code.py && exit 1",
         "on_fail": "warn", "source": "test fixture"},
    ]})
    state = tmp_path / "state"
    run_check(manifest, tmp_path / "o1", state, diff=True)

    # grow the file above the TODO line -> its line number shifts, content doesn't
    write(proj, "code.py", "z = 0\n" * 10 + "x = 1\n" * 5
          + "y = 2  # TODO real fix needed\n")
    out2 = run_check(manifest, tmp_path / "o2", state, diff=True)
    report2 = open(os.path.join(out2, "REPORT.md")).read()
    new_section2 = report2.split("## New violations")[1].split("## Resolved")[0]
    resolved_section2 = report2.split("## Resolved since last check")[1].split("(unchanged")[0]
    assert "TODO" not in new_section2      # pure position shift: not new
    assert "TODO" not in resolved_section2  # ...and not resolved either

    # a genuinely different TODO further down IS new
    write(proj, "code.py", "z = 0\n" * 10 + "x = 1\n" * 5
          + "y = 2  # TODO real fix needed\n"
          + "w = 3  # TODO a second, distinct issue\n")
    out3 = run_check(manifest, tmp_path / "o3", state, diff=True)
    report3 = open(os.path.join(out3, "REPORT.md")).read()
    new_section3 = report3.split("## New violations")[1].split("## Resolved")[0]
    assert "a second, distinct issue" in new_section3


def test_locked_state_serializes_concurrent_readers_writers(tmp_path):
    # T-20260821-54: the real bug wasn't _violation_fingerprint (that's
    # already correct and covered above) — it was that state.json's
    # read-diff-write in run() had no locking, and this file is hit by many
    # concurrent `steward check` processes (one per session's Stop hook,
    # see .claude/settings.json). Two processes can both read the same
    # prev snapshot before either writes; whichever writes last silently
    # discards the other's update (a classic lost-update race). This test
    # exercises the fix (locked_state()) directly with a plain
    # read-increment-write counter — without the lock this reliably loses
    # updates under thread interleaving; with it, none are lost.
    import threading
    import time

    from agent_steward import cli

    state_file = str(tmp_path / "state.json")
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"counter": 0}, f)

    iterations, n_threads = 25, 4

    def worker():
        for _ in range(iterations):
            with cli.locked_state(state_file):
                state = cli.load_state(state_file)
                state["counter"] = state.get("counter", 0) + 1
                time.sleep(0.001)  # widen the race window
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump(state, f)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = cli.load_state(state_file)
    assert final["counter"] == iterations * n_threads


def test_diff_violations_real_20260821_snapshots_stay_stable():
    # T-20260821-54: replay of the actual production snapshots that
    # triggered the false "new" report three times in one day (09:25 /
    # 09:47 / 10:06, pipeline/pwa_writeback_poller.py, same 39-守門b
    # comment line). Confirms diff_violations()/_violation_fingerprint()
    # were never the problem: fed the real consecutive snapshots directly
    # (bypassing the state.json race that actually caused the false
    # report), the diff is empty across every transition — the fix
    # belongs in the read-diff-write locking (see
    # test_locked_state_serializes_concurrent_readers_writers), not here.
    v_092501 = "15833 |     # (39-守門b) 環境包白名單逐字鏡像(雲端抄的那份 == 這裡這份)。雲端比這裡鬆 ="
    v_094736 = "15936 |     # (39-守門b) 環境包白名單逐字鏡像(雲端抄的那份 == 這裡這份)。雲端比這裡鬆 ="
    v_100639 = "15955 |     # (39-守門b) 環境包白名單逐字鏡像(雲端抄的那份 == 這裡這份)。雲端比這裡鬆 ="
    for prev_v, cur_v in ((v_092501, v_094736), (v_094736, v_100639)):
        new, resolved = cli.diff_violations({"py-lint": [prev_v]}, {"py-lint": [cur_v]})
        assert new == {}, new
        assert resolved == {}, resolved


def test_always_report_distinguishes_clean_from_never_ran(tmp_path):
    # a probe declaring `always_report: true` must leave a [] key in
    # `violations` when it's clean, so downstream readers can tell "checked,
    # 0 violations" apart from "never configured/ran" (both used to be a
    # missing key — see T-20260804-27). Probes that don't opt in keep the
    # old behavior: clean means the key is absent entirely.
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    manifest = make_manifest(tmp_path, proj, extra={"probes": [
        {"id": "fact-schema", "type": "frontmatter_required",
         "glob": "facts/**/*.md", "required": ["id", "confidence"],
         "severity": "warn", "source": "RULES.md §1", "always_report": True},
        {"id": "other-schema", "type": "frontmatter_required",
         "glob": "facts/**/*.md", "required": ["id", "confidence"],
         "severity": "warn", "source": "RULES.md §1"},
    ]})
    run_check(manifest, tmp_path / "o", tmp_path / "state")

    st = json.load(open(tmp_path / "state" / "state.json"))
    violations = st["projects"]["fixture-project"]["violations"]
    assert violations["fact-schema"] == []          # opted in, clean -> present as []
    assert "other-schema" not in violations         # not opted in -> old behavior

    problems = cli.validate_manifest(yaml.safe_load(open(manifest)))
    assert not any("always_report" in p for p in problems)  # not flagged as unknown


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


def test_rglob_matches_depth_one():
    """records/**/*.md must match records/a.md (zero-or-more dirs) — the
    single most common first-run confusion before v0.19.1."""
    import os as _os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        _os.makedirs(_os.path.join(d, "records", "deep"))
        for p in ("records/a.md", "records/deep/b.md", "top.md"):
            open(_os.path.join(d, p), "w").write("x")
        rels = [_os.path.relpath(p, d) for p in cli.rglob(d, "records/**/*.md")]
        assert sorted(rels) == ["records/a.md", "records/deep/b.md"]
        assert [_os.path.relpath(p, d) for p in cli.rglob(d, "**/*.md")] == \
            ["records/a.md", "records/deep/b.md", "top.md"]


def test_probe_scope_guard(tmp_path):
    """Over-delivery guard: files outside every expected area get flagged;
    ignore patterns and the required-param contract hold."""
    write(tmp_path, "facts/2026/ok.md", "x")
    write(tmp_path, "notes/expected.md", "x")
    write(tmp_path, "landing_page_draft.html", "nobody asked for this")
    write(tmp_path, ".steward/state.json", "{}")
    spec = {"id": "p", "expected": ["facts/**/*", "notes/**/*", "README.md"],
            "within": "**/*", "severity": "warn"}
    r = cli.probe_scope_guard(str(tmp_path), spec)
    assert r["status"] == "warn" and r["n_violations"] == 1
    assert "landing_page_draft.html" in r["violations"][0]
    assert ".steward" not in "\n".join(r["violations"])   # default ignore
    assert cli.probe_scope_guard(str(tmp_path), {"id": "p"})["status"] == "skipped"
    ok = cli.probe_scope_guard(str(tmp_path), {**spec, "expected": ["**/*"]})
    assert ok["status"] == "pass"


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


def test_allocation_compliance_uses_tier_patterns_of_the_stamps_day(tmp_path):
    """T-20260901-155: the model globs are time-dependent, not just the tiers.

    When a target split `mid: ['*opus*']` into `mid: ['*opus-4*']` /
    `top: ['*opus-5*']` (T-20260901-109/118), 48 stamps that were compliant
    under the table of their own day became violations against today's table
    -- a permanent red nobody could ever clear, which is precisely what
    `tier_patterns_history` / `allocate.patterns_at` exist to prevent. The
    ledger side (`ledger_mismatches`) already replayed that history; this
    probe judged every historical stamp against today's globs.
    """
    alloc = {"tasks": [{"id": "condense", "tier": "mid"}],
             "tier_patterns": {"cheap": ["*haiku*"],
                               "mid": ["*sonnet*", "*opus-4*"],
                               "top": ["*fable*", "*opus-5*"]},
             "tier_patterns_history": [
                 {"at": "2026-09-01T21:57:09",
                  "patterns": {"cheap": ["*haiku*"],
                               "mid": ["*sonnet*", "*opus*"],
                               "top": ["*fable*"]},
                  "reason": "split opus-5 out of the generic *opus* glob"}]}
    write(tmp_path, ".allocation.yaml", yaml.safe_dump(alloc))
    write(tmp_path, "facts/x/before-split.md",      # compliant under its own day
          "---\nid: F1\nproduced_by: claude-opus-5\ntask: condense\n"
          "stamped_at: \"2026-09-01T05:30:00\"\n---\nb\n")
    write(tmp_path, "facts/x/after-split.md",       # a genuinely new offender
          "---\nid: F2\nproduced_by: claude-opus-5\ntask: condense\n"
          "stamped_at: \"2026-09-02T01:00:00\"\n---\nb\n")
    write(tmp_path, "facts/x/undated.md",           # no stamped_at -> today's table
          "---\nid: F3\nproduced_by: claude-opus-5\ntask: condense\n---\nb\n")
    write(tmp_path, "facts/x/wrong-both.md",        # in neither table's mid tier
          "---\nid: F4\nproduced_by: claude-haiku-4-5\ntask: condense\n"
          "stamped_at: \"2026-09-01T05:30:00\"\n---\nb\n")
    spec = {"id": "p", "type": "allocation_compliance", "glob": "facts/**/*.md",
            "allocation_file": ".allocation.yaml", "severity": "warn"}
    r = cli.probe_allocation_compliance(str(tmp_path), spec)
    joined = "\n".join(r["violations"])
    assert "before-split.md" not in joined, joined
    assert "after-split.md" in joined       # the real new offender is still caught
    assert "undated.md" in joined           # undatable stamp -> today's rules apply
    assert "wrong-both.md" in joined
    assert r["n_violations"] == 3, joined
    # a stamp that matches neither era says so, instead of implying the
    # current table was the only one ever consulted
    assert "tier_patterns also differed at stamp time" in \
        next(v for v in r["violations"] if "wrong-both.md" in v)

    # negative control: without the history the pre-split stamp goes red again.
    # Without this the assertions above cannot tell "the fix works" from "this
    # fixture never had a violation to begin with".
    del alloc["tier_patterns_history"]
    write(tmp_path, ".allocation.yaml", yaml.safe_dump(alloc))
    r2 = cli.probe_allocation_compliance(str(tmp_path), spec)
    assert r2["n_violations"] == 4
    assert "before-split.md" in "\n".join(r2["violations"])


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


def test_log_task_person_id_written_and_omitted(tmp_path):
    """T-20260814-120 正反案 — `--person` is the identity write-end.

    正案: given, the row carries `person_id` so spend can be grouped by人.
    反案: omitted, the key is *absent* (not "" / not "unknown") — an
    un-attributed row must stay visibly un-attributed, otherwise a per-person
    reading silently invents a bucket (rule 7: declaring is not measuring).
    """
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli", "log-task",
            "--task", "extract", "--tier", "mid", "--model", "claude-opus-4-8",
            "--est-tokens", "1200", "--result", "pass",
            "--state-dir", str(tmp_path / ".steward")]
    r = subprocess.run(base + ["--person", "owner"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(base, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    rows = [json.loads(x) for x in
            open(tmp_path / ".steward" / "usage_ledger.jsonl").read().splitlines()]
    assert rows[0]["person_id"] == "owner"          # 正案
    assert "person_id" not in rows[1]               # 反案:缺 = 缺,不是空字串


def test_log_task_rejects_tier_model_mismatch(tmp_path):
    """T-20260824-91: `.allocation.yaml` tier_patterns is the single SSoT —
    a tier/model contradiction is REJECTED (nothing written, exit 1, correct
    tier printed), not merely warned. A matching declaration still logs
    clean (exit 0, no warning)."""
    from agent_steward import allocate as am
    alloc = am.build_allocation({"tasks": [axes_task("condense", "med", "med", "med")]})
    apath = tmp_path / ".allocation.yaml"
    am.write_allocation(alloc, str(apath))
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli", "log-task",
            "--task", "condense", "--model", "claude-sonnet-5",
            "--allocation", str(apath), "--state-dir", str(tmp_path / ".steward")]
    r = subprocess.run(base + ["--tier", "top"], capture_output=True, text=True, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REJECTED" in r.stderr and "belongs to tier(s) mid" in r.stderr
    assert not (tmp_path / ".steward" / "usage_ledger.jsonl").exists()
    r2 = subprocess.run(base + ["--tier", "mid"], capture_output=True, text=True, env=env)
    assert r2.returncode == 0 and "REJECTED" not in r2.stderr and "warning" not in r2.stderr
    lines = open(tmp_path / ".steward" / "usage_ledger.jsonl").read().splitlines()
    assert len(lines) == 1  # only the matching entry landed


def test_log_task_tier_model_guard_walks_up_for_allocation_file(tmp_path):
    """T-20260901-123: guard 2 must find `.allocation.yaml` the same way from
    repo root and from a subdirectory two levels deep — before the fix,
    `os.path.exists(".allocation.yaml")` only ever saw the exact cwd, so a
    contradictory tier/model pair written from a subdirectory (no --allocation
    given) silently skipped the guard and landed in the ledger unrejected."""
    from agent_steward import allocate as am
    alloc = am.build_allocation({"tasks": [axes_task("condense", "med", "med", "med")]})
    am.write_allocation(alloc, str(tmp_path / ".allocation.yaml"))
    deep = tmp_path / "sub" / "deep"
    deep.mkdir(parents=True)
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    ledger = tmp_path / ".steward" / "usage_ledger.jsonl"
    base = [sys.executable, "-m", "agent_steward.cli", "log-task",
            "--task", "condense", "--model", "claude-sonnet-5", "--tier", "top",
            "--state-dir", str(tmp_path / ".steward")]
    # no --allocation given in either case: the guard must resolve
    # `.allocation.yaml` by walking up from cwd, not by a bare relative name.
    r_root = subprocess.run(base, capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r_root.returncode == 1, r_root.stdout + r_root.stderr
    assert "REJECTED" in r_root.stderr
    assert not ledger.exists()
    r_deep = subprocess.run(base, capture_output=True, text=True, env=env, cwd=str(deep))
    assert r_deep.returncode == 1, r_deep.stdout + r_deep.stderr  # was 0 pre-fix
    assert "REJECTED" in r_deep.stderr
    assert not ledger.exists()  # nothing written from either directory


def test_log_task_rejects_unknown_model_with_ruling_ref(tmp_path):
    """T-20260901-109/118: an unknown model (matches no tier_patterns entry
    at all) is now REJECTED, not merely warned — the original warn-only
    behaviour let 1,181 `claude-opus-5` rows land silently at the nearest
    wildcard once `*opus*` stopped separating two cost tiers. When the table
    carries `tier_patterns_ref` (the ruling this table implements), the
    REJECT message must echo it so the very first collision has a place to
    go, not just a dead end (Owner rule 2: no gate without a destination)."""
    from agent_steward import allocate as am
    alloc = am.build_allocation({"tasks": [axes_task("condense", "med", "med", "med")]})
    # post-split table (T-20260901-109 §②): no generic `*opus*` catch-all left,
    # so a brand-new opus generation matches neither `*opus-4*` nor `*opus-5*`
    alloc["tier_patterns"] = {"cheap": ["*haiku*"],
                              "mid": ["*sonnet*", "*opus-4*", "opus"],
                              "top": ["*fable*", "*mythos*", "*opus-5*", "human"]}
    alloc["tier_patterns_ref"] = "state/gm/rulings/T-20260901-109-opus5-tier-ruling.md"
    apath = tmp_path / ".allocation.yaml"
    am.write_allocation(alloc, str(apath))
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    r = subprocess.run(
        [sys.executable, "-m", "agent_steward.cli", "log-task",
         "--task", "condense", "--tier", "mid", "--model", "claude-opus-9",
         "--allocation", str(apath), "--state-dir", str(tmp_path / ".steward")],
        capture_output=True, text=True, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REJECTED" in r.stderr and "matches no" in r.stderr
    assert "state/gm/rulings/T-20260901-109-opus5-tier-ruling.md" in r.stderr
    assert not (tmp_path / ".steward" / "usage_ledger.jsonl").exists()


def test_log_task_opus5_top_accepted_opus5_mid_rejected(tmp_path):
    """T-20260901-109 §②: `*opus*` is split so `claude-opus-5` (top, the
    judgment/adjudication model) and `claude-opus-4-8` (mid, the condense
    work-horse named in CLAUDE.md autorun rule 2) are no longer the same
    cost tier. `--tier top --model claude-opus-5` must log clean; the same
    model declared `--tier mid` must REJECT (money followed the wrong tier
    for 1,181 historical rows — the write-time guard is what stops row
    1,182)."""
    from agent_steward import allocate as am
    alloc = am.build_allocation({"tasks": [axes_task("adjudicate", "high", "high", "high")]})
    alloc["tier_patterns"] = {"cheap": ["*haiku*"],
                              "mid": ["*sonnet*", "*opus-4*", "opus"],
                              "top": ["*fable*", "*mythos*", "*opus-5*", "human"]}
    apath = tmp_path / ".allocation.yaml"
    am.write_allocation(alloc, str(apath))
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli", "log-task",
            "--task", "adjudicate", "--model", "claude-opus-5",
            "--allocation", str(apath), "--state-dir", str(tmp_path / ".steward")]
    r_top = subprocess.run(base + ["--tier", "top"], capture_output=True, text=True, env=env)
    assert r_top.returncode == 0 and "REJECTED" not in r_top.stderr, r_top.stderr
    r_mid = subprocess.run(base + ["--tier", "mid"], capture_output=True, text=True, env=env)
    assert r_mid.returncode == 1, r_mid.stdout + r_mid.stderr
    assert "REJECTED" in r_mid.stderr and "belongs to tier(s) top" in r_mid.stderr
    lines = open(tmp_path / ".steward" / "usage_ledger.jsonl").read().splitlines()
    assert len(lines) == 1  # only the --tier top call landed


def test_log_task_rejects_ambiguous_tie_between_tiers(tmp_path):
    """T-20260901-109 §②: a model tied for the longest matching glob across
    two tiers is a genuine table conflict `classify_model` cannot resolve by
    specificity — REJECT, don't guess (this is the other new leg alongside
    'unknown', both added without touching the direction of the existing
    mismatch REJECT, T-20260824-91)."""
    from agent_steward import allocate as am
    alloc = am.build_allocation({"tasks": [axes_task("condense", "med", "med", "med")]})
    alloc["tier_patterns"] = {"mid": ["*opusx*"], "top": ["*opusx*"]}
    apath = tmp_path / ".allocation.yaml"
    am.write_allocation(alloc, str(apath))
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    r = subprocess.run(
        [sys.executable, "-m", "agent_steward.cli", "log-task",
         "--task", "condense", "--tier", "mid", "--model", "claude-opusx-1",
         "--allocation", str(apath), "--state-dir", str(tmp_path / ".steward")],
        capture_output=True, text=True, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "REJECTED" in r.stderr and "ambiguous" in r.stderr
    assert not (tmp_path / ".steward" / "usage_ledger.jsonl").exists()


def test_classify_model_longest_glob_wins_over_a_generic_catchall():
    """The mechanism behind the opus split: a specific glob in one tier beats
    a generic catch-all in another, so the table does not need every old
    catch-all removed to add a new specific tier."""
    from agent_steward import allocate as am
    pats = {"mid": ["*opus*"], "top": ["*opus-5*"]}
    assert am.classify_model("claude-opus-5", pats) == ("top", "ok")
    assert am.classify_model("claude-opus-4-8", pats) == ("mid", "ok")
    assert am.classify_model("claude-haiku-4-5", pats) == (None, "unknown")
    assert am.classify_model("x", {"a": ["*x*"], "b": ["*x*"]}) == (None, "ambiguous")


def test_allocate_tune_patterns_spec_propose_then_apply(tmp_path):
    """The pattern-level counterpart of a tier proposal (T-20260901-109/118):
    `.allocation.yaml` `tier_patterns` must never be hand-edited — propose
    prints a diff and changes nothing; `--apply` writes the new table AND
    snapshots the old one into `tier_patterns_history` so `patterns_at` keeps
    judging every past ledger row against the table of its own day."""
    from agent_steward import allocate as am
    alloc = am.build_allocation({"tasks": [axes_task("condense", "med", "med", "med")]})
    old_patterns = dict(alloc["tier_patterns"])
    apath = tmp_path / ".allocation.yaml"
    am.write_allocation(alloc, str(apath))
    spec = {"patterns": {"cheap": ["*haiku*"], "mid": ["*sonnet*", "*opus-4*", "opus"],
                        "top": ["*fable*", "*mythos*", "*opus-5*", "human"]},
            "reason": "T-20260901-109 ruling: opus-5 is its own cost tier",
            "ref": "state/gm/rulings/T-20260901-109-opus5-tier-ruling.md"}
    spath = tmp_path / "spec.yaml"
    spath.write_text(yaml.safe_dump(spec), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli", "allocate", "tune",
            "--allocation", str(apath), "--patterns-spec", str(spath)]
    r_propose = subprocess.run(base, capture_output=True, text=True, env=env)
    assert r_propose.returncode == 0, r_propose.stderr
    assert "propose" in r_propose.stdout
    assert am.load_allocation(str(apath))["tier_patterns"] == old_patterns  # unchanged
    r_apply = subprocess.run(base + ["--apply"], capture_output=True, text=True, env=env)
    assert r_apply.returncode == 0, r_apply.stderr
    written = am.load_allocation(str(apath))
    assert written["tier_patterns"] == spec["patterns"]
    assert written["tier_patterns_ref"] == spec["ref"]
    hist = written["tier_patterns_history"][-1]
    assert hist["patterns"] == old_patterns and "T-20260901-109" in hist["reason"]
    # the snapshot lets a historical row still be judged by the table of its day
    assert am.patterns_at(written, "2026-01-01T00:00:00") == old_patterns


def test_log_task_dedup_same_note_is_idempotent(tmp_path):
    """T-20260824-91 guard 1 — same (task, note) twice does not produce a
    second row; the second call is a no-op (exit 0, unchanged line count).
    A different note for the same task still appends normally."""
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli", "log-task",
            "--task", "extract", "--tier", "mid", "--model", "claude-opus-4-8",
            "--est-tokens", "1200", "--result", "pass",
            "--note", "headless-dispatch card=T-1 t=2026-08-24T00:00:00",
            "--state-dir", str(tmp_path / ".steward")]
    r1 = subprocess.run(base, capture_output=True, text=True, env=env)
    assert r1.returncode == 0, r1.stderr
    r2 = subprocess.run(base, capture_output=True, text=True, env=env)
    assert r2.returncode == 0, r2.stderr
    assert "already logged" in r2.stderr and "skipping duplicate" in r2.stderr
    lines = open(tmp_path / ".steward" / "usage_ledger.jsonl").read().splitlines()
    assert len(lines) == 1  # second call was a no-op
    r3 = subprocess.run(base[:-2] + ["--note", "headless-dispatch card=T-1 t=2026-08-24T00:00:01",
                                      "--state-dir", str(tmp_path / ".steward")],
                         capture_output=True, text=True, env=env)
    assert r3.returncode == 0, r3.stderr
    lines = open(tmp_path / ".steward" / "usage_ledger.jsonl").read().splitlines()
    assert len(lines) == 2  # a genuinely new note still appends


def test_log_task_no_note_never_dedups(tmp_path):
    """Rows without --note carry no dedup key and are always appended —
    unchanged from pre-T-20260824-91 behaviour (see test_log_task_appends_jsonl)."""
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli", "log-task",
            "--task", "extract", "--tier", "mid", "--model", "claude-opus-4-8",
            "--state-dir", str(tmp_path / ".steward")]
    for _ in range(2):
        r = subprocess.run(base, capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
    lines = open(tmp_path / ".steward" / "usage_ledger.jsonl").read().splitlines()
    assert len(lines) == 2


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


def test_extract_freestanding_comments_ignores_header_and_key_trailing():
    header = alloc_mod.ALLOCATION_HEADER % alloc_mod.RUBRIC_VERSION
    body = ("version: 1\n"
            "history:\n"
            "- {at: '2026-01-01T00:00:00', task: x}\n"
            "# 2026-08-18 GM 回滾註:this is a free-standing note\n"
            "# spanning two lines\n"
            "tier_patterns_history: []\n"
            "trailing_key: value  # this is attached to a key, out of scope\n")
    blocks = alloc_mod.extract_freestanding_comments(header + body)
    assert blocks == ["# 2026-08-18 GM 回滾註:this is a free-standing note\n"
                       "# spanning two lines"]
    # the regenerated header itself must never be captured as a "human note"
    assert not any("generated by `steward allocate init`" in b for b in blocks)


def test_write_allocation_preserves_freestanding_comment_across_tune_apply(tmp_path):
    """T-20260901-125: write_allocation used to re-serialize .allocation.yaml
    with yaml.safe_dump on the *parsed* dict — yaml.safe_load drops comments on
    read, so any free-standing human note (not attached to a key) vanished
    silently on the very next `allocate tune --apply`, with no error or
    warning (real incident: a 2026-08-18 GM rollback note, T-20260901-118).
    Fixed by folding forward any free-standing comment found on disk into a
    `preserved_comments` field before every write (extract_freestanding_
    comments + write_allocation)."""
    alloc = alloc_mod.build_allocation({"tasks": [axes_task("condense", "med", "med", "med")]})
    apath = tmp_path / ".allocation.yaml"
    alloc_mod.write_allocation(alloc, str(apath))
    note = ("# 2026-08-18 GM 回滾註:tune apply once demoted this task on thin\n"
            "# evidence; rolled back to mid by hand, kept here for the next reader.")
    with open(apath, "a", encoding="utf-8") as f:
        f.write("\n" + note + "\n")
    assert note in apath.read_text(encoding="utf-8")   # sanity: fixture really has it

    sdir = tmp_path / ".steward"
    ledger_write(str(sdir), [{"ts": "2026-01-01T00:00:00", "task": "condense",
                              "tier": "mid", "result": "pass"}] * 25)
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src")}
    base = [sys.executable, "-m", "agent_steward.cli", "allocate", "tune",
            "--allocation", str(apath), "--state-dir", str(sdir), "--apply"]

    r1 = subprocess.run(base, capture_output=True, text=True, env=env)
    assert r1.returncode == 0, r1.stderr
    assert "applied 1 change" in r1.stdout                     # the demote really ran
    after1 = apath.read_text(encoding="utf-8")
    assert note in after1                                      # the note survived apply #1
    reloaded = alloc_mod.load_allocation(str(apath))
    assert {t["id"]: t["tier"] for t in reloaded["tasks"]}["condense"] == "cheap"

    # a second apply (no-op tune, nothing left to propose) must not duplicate
    # the note nor lose it — the fold-forward has to be a stable fixed point.
    r2 = subprocess.run(base, capture_output=True, text=True, env=env)
    assert r2.returncode == 0, r2.stderr
    after2 = apath.read_text(encoding="utf-8")
    assert after2.count(note) == 1


def test_render_allocation_round_trips_real_allocation_file_without_loss():
    """Compatibility check (T-20260901-125 acceptance ③): the shape change
    (popping/re-appending `preserved_comments`) must not corrupt a real,
    already-in-production .allocation.yaml — load -> write -> load must be a
    no-op on every ordinary (non-comment) field."""
    real_path = os.environ.get("T125_REAL_ALLOCATION_YAML")
    if not real_path or not os.path.exists(real_path):
        pytest.skip("real .allocation.yaml not provided via T125_REAL_ALLOCATION_YAML")
    before = alloc_mod.load_allocation(real_path)
    rendered = alloc_mod.render_allocation(before)
    after = yaml.safe_load(rendered)
    assert after == before                      # one write/read cycle, zero data drift


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
    stop = settings["hooks"]["Stop"]
    cmds = [h["command"] for e in stop for h in e["hooks"]]
    # two hooks now: check (self-repair) + report (auto cumulative report)
    check_cmd = next(c for c in cmds if "--diff --exit-new" in c)
    report_cmd = next(c for c in cmds if " report " in f" {c} ")
    assert str(proj) in check_cmd
    assert report_cmd.endswith("REPORT.md || true") and "REPORT.md" in report_cmd
    # idempotent: re-running adds nothing
    r2 = steward_cli("install-hook", "--manifest", manifest)
    assert "already installed" in r2.stdout
    cmds2 = [h["command"] for e in json.loads(spath.read_text())["hooks"]["Stop"]
             for h in e["hooks"]]
    assert len(cmds2) == 2
    # merges, never clobbers existing settings
    settings["permissions"] = {"allow": ["Bash(ls:*)"]}
    spath.write_text(json.dumps(settings), encoding="utf-8")
    steward_cli("install-hook", "--manifest", manifest)  # no-op, but re-check preserved
    assert json.loads(spath.read_text())["permissions"]["allow"] == ["Bash(ls:*)"]


def test_install_hook_upgrades_old_single_hook(tmp_path):
    # a project installed before 0.20 has only the check hook; re-running
    # install-hook must ADD the report hook without touching the check one.
    proj = tmp_path / "proj"
    write(proj, "facts/2026/ok.md", FACT_OK)
    manifest = make_manifest(tmp_path, proj)
    spath = proj / ".claude" / "settings.json"
    spath.parent.mkdir(parents=True, exist_ok=True)
    old_check = (f"steward check --manifest {manifest} --root {proj} "
                 f"--state-dir {proj}/.steward --diff --exit-new")
    spath.write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": old_check}]}]}}))
    r = steward_cli("install-hook", "--manifest", manifest)
    assert r.returncode == 0 and "report" in r.stdout
    cmds = [h["command"] for e in json.loads(spath.read_text())["hooks"]["Stop"]
            for h in e["hooks"]]
    assert len(cmds) == 2
    assert any("--diff --exit-new" in c for c in cmds)      # check untouched
    assert any(c.endswith("REPORT.md || true") for c in cmds)  # report added


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


# ---- state-dir discovery: a ledger fork is a silent loss of history --------
# Evidence that motivated this: agent-steward's own repo grew a second ledger
# at src/.steward/usage_ledger.jsonl (2 entries, 180k tokens) because a
# `log-task` ran one directory too deep. No error was raised — the spend just
# left the books.

def test_find_state_dir_walks_up_to_nearest_existing(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".steward").mkdir()
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert cli.find_state_dir(None, str(deep)) == str(tmp_path / ".steward")


def test_find_state_dir_explicit_wins(tmp_path):
    (tmp_path / ".steward").mkdir()
    explicit = tmp_path / "elsewhere"
    assert cli.find_state_dir(str(explicit), str(tmp_path)) == str(explicit)


def test_find_state_dir_nearest_beats_ancestor(tmp_path):
    """A sub-project with its own books keeps them."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".steward").mkdir()
    sub = tmp_path / "newsletters"
    (sub / ".steward").mkdir(parents=True)
    inner = sub / "outbox"
    inner.mkdir()
    assert cli.find_state_dir(None, str(inner)) == str(sub / ".steward")


def test_find_state_dir_stops_at_git_boundary(tmp_path):
    """A nested repo must not write into its parent's ledger."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".steward").mkdir()
    nested = tmp_path / "vendor" / "other-repo"
    (nested / ".git").mkdir(parents=True)
    work = nested / "src"
    work.mkdir()
    assert cli.find_state_dir(None, str(work)) == str(work / ".steward")


def test_find_state_dir_falls_back_to_cwd_when_none_exists(tmp_path):
    (tmp_path / ".git").mkdir()
    assert cli.find_state_dir(None, str(tmp_path)) == str(tmp_path / ".steward")


def test_find_allocation_file_walks_up_to_nearest_existing(tmp_path):
    """T-20260901-123: mirrors test_find_state_dir_walks_up_to_nearest_existing
    — guard 2 must find `.allocation.yaml` from a subdirectory the same way
    find_state_dir already finds `.steward/`."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".allocation.yaml").write_text("tasks: {}\n")
    deep = tmp_path / "src" / "pkg"
    deep.mkdir(parents=True)
    assert cli.find_allocation_file(None, str(deep)) == str(tmp_path / ".allocation.yaml")


def test_find_allocation_file_explicit_wins(tmp_path):
    (tmp_path / ".allocation.yaml").write_text("tasks: {}\n")
    explicit = tmp_path / "elsewhere.yaml"
    assert cli.find_allocation_file(str(explicit), str(tmp_path)) == str(explicit)


def test_find_allocation_file_stops_at_git_boundary(tmp_path):
    """A nested repo must not read its parent's allocation table."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".allocation.yaml").write_text("tasks: {}\n")
    nested = tmp_path / "vendor" / "other-repo"
    (nested / ".git").mkdir(parents=True)
    work = nested / "src"
    work.mkdir()
    assert cli.find_allocation_file(None, str(work)) == str(work / ".allocation.yaml")


def test_find_allocation_file_falls_back_to_cwd_when_none_exists(tmp_path):
    (tmp_path / ".git").mkdir()
    assert cli.find_allocation_file(None, str(tmp_path)) == str(tmp_path / ".allocation.yaml")


def test_log_task_from_subdir_appends_to_the_repo_ledger(tmp_path):
    """End-to-end: the regression that orphaned real spend."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".steward").mkdir()
    sub = tmp_path / "src"
    sub.mkdir()
    r = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "..", "src",
                                      "agent_steward", "cli.py"),
         "log-task", "--task", "t1", "--tier", "mid", "--model", "claude-x",
         "--est-tokens", "100"],
        cwd=str(sub), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert not (sub / ".steward").exists(), "log-task forked a second ledger"
    lines = (tmp_path / ".steward" / "usage_ledger.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[-1])["task"] == "t1"


def test_scope_guard_ignores_vendored_trees_at_any_depth(tmp_path):
    """Regression: the built-in ignore list was root-anchored, so a sub-app's
    node_modules/ produced one 'agent over-delivery' finding per vendored
    README — 140 of 241 open findings in a real target."""
    root = tmp_path / "proj"
    write(root, "docs/real.md", "kept")
    write(root, "pwa/node_modules/left-pad/README.md", "vendored")
    write(root, "pwa/node_modules/x/nested/node_modules/y/readme.md", "vendored")
    write(root, "pipeline/__pycache__/notes.md", "cache")
    write(root, "stray.md", "genuinely out of scope")
    spec = {"id": "sg", "type": "scope_guard", "within": "**/*.md",
            "expected": ["docs/**"]}
    r = cli.probe_scope_guard(str(root), spec)
    assert r["violations"] == [
        v for v in r["violations"] if v.startswith("stray.md")], r["violations"]
    assert len(r["violations"]) == 1


def test_scope_guard_explicit_ignore_is_taken_literally(tmp_path):
    """A manifest-supplied ignore: means exactly what it says — broadening the
    defaults must not start rewriting the human's globs."""
    root = tmp_path / "proj"
    write(root, "docs/real.md", "kept")
    write(root, "pwa/node_modules/left-pad/README.md", "vendored")
    spec = {"id": "sg", "type": "scope_guard", "within": "**/*.md",
            "expected": ["docs/**"], "ignore": ["nothing-matches/**"]}
    r = cli.probe_scope_guard(str(root), spec)
    assert any("node_modules" in v for v in r["violations"])


def test_savings_line_names_a_negative_tuning_effect_honestly():
    from agent_steward import allocate as alloc_mod
    sav = {"metered": 3, "actual_cost": 300.0, "top_cost": 500.0,
           "saved_vs_top": 200.0, "saved_vs_top_pct": 40.0,
           "initial_cost": 200.0, "saved_vs_initial": -100.0,
           "saved_vs_initial_pct": -50.0, "no_tokens": 0, "unknown_tier": 0,
           "tokens_by_tier": {}, "entries_by_tier": {}, "cost_by_tier": {},
           "canary_runs": 0, "canary_cost": 0.0}
    line = [x for x in alloc_mod.spend_summary_lines(sav) if "cold-start" in x][0]
    assert "costs 100 MORE (50.0%)" in line
    assert "saved -" not in line


# ---- real money: price from the model, judge compliance by its own day -----

OLD_PATS = {"cheap": ["*haiku*"], "mid": ["*sonnet*", "*opus*"],
            "top": ["*fable*"]}
NEW_ALLOC = {
    "tiers": ["cheap", "mid", "high", "top"],
    "cost_weights": {"cheap": 1.8, "mid": 3.6, "high": 9.0, "top": 18.0},
    "cost_unit": "usd_per_mtok",
    "tier_patterns": {"cheap": ["*haiku*"], "mid": ["*sonnet*"],
                      "high": ["*opus*"], "top": ["*fable*"]},
    "tier_patterns_history": [{"at": "2026-07-30T21:00:00",
                               "patterns": OLD_PATS, "reason": "split opus out"}],
}


def test_patterns_at_replays_the_table_of_the_day():
    from agent_steward import allocate as a
    assert a.patterns_at(NEW_ALLOC, "2026-07-15T00:00:00") == OLD_PATS
    assert a.patterns_at(NEW_ALLOC, "2026-08-01T00:00:00")["high"] == ["*opus*"]
    assert a.patterns_at(NEW_ALLOC, None) == NEW_ALLOC["tier_patterns"]
    assert a.patterns_at({"tier_patterns": OLD_PATS}, "2026-01-01") == OLD_PATS


def test_restructuring_the_table_does_not_rewrite_the_past():
    """849 of 1111 real entries would have flipped to 'mis-logged' overnight."""
    from agent_steward import allocate as a
    old = [{"ts": "2026-07-15T00:00:00", "task": "t", "tier": "mid",
            "model": "claude-opus-4-8", "est_tokens": 1000}]
    new = [{"ts": "2026-08-05T00:00:00", "task": "t", "tier": "mid",
            "model": "claude-opus-4-8", "est_tokens": 1000}]
    assert a.ledger_mismatches(NEW_ALLOC, old) == ([], [])
    mism, _ = a.ledger_mismatches(NEW_ALLOC, new)
    assert len(mism) == 1 and mism[0]["matches_tiers"] == ["high"]


def test_cost_follows_the_model_not_the_declaration():
    from agent_steward import allocate as a
    e = [{"ts": "2026-08-05T00:00:00", "task": "t", "tier": "mid",
          "model": "claude-opus-4-8", "est_tokens": 1_000_000}]
    sav = a.compute_savings(e, NEW_ALLOC)
    assert sav["actual_cost"] == 9_000_000    # priced as high (opus), not mid
    assert sav["declared_cost"] == 0          # task has no tune history
    assert sav["priced_by_model"] == 1
    assert sav["tokens_by_tier"] == {"high": 1_000_000}


def test_price_tier_falls_back_when_the_model_says_nothing():
    from agent_steward import allocate as a
    assert a.price_tier(NEW_ALLOC, {"tier": "mid"}) == ("mid", False)
    assert a.price_tier(NEW_ALLOC, {"tier": "mid", "model": "gpt-9"}) == ("mid", False)
    amb = {"tier_patterns": {"mid": ["*sonnet*"], "high": ["*opus*"]}}
    assert a.price_tier(amb, {"tier": "high",
                              "model": "sonnet-alias-opus"})[0] == "high"


def test_money_prints_dollars_only_when_the_unit_says_so():
    from agent_steward import allocate as a
    assert a.money(NEW_ALLOC, 931_057_246) == "$931.06"
    assert a.money(NEW_ALLOC, 2_589_198_700) == "$2,589"
    assert a.money(NEW_ALLOC, 1_500_000) == "$1.50"
    assert a.money({}, 931_057_246) == "931,057,246"
    assert a.cost_label(NEW_ALLOC) == "cost (USD)"
    assert a.cost_label({}) == "cost index"


def test_tuning_effect_only_counts_tasks_tuning_actually_moved(tmp_path):
    """Relabelling a tier for unrelated reasons must not move this number.
    Real case: the 3->4 tier restructure swung it from -15.6% to +18.8%
    without a single dispatch changing."""
    from agent_steward import allocate as a
    alloc = dict(NEW_ALLOC)
    alloc = {**alloc,
             "tasks": [{"id": "tuned", "tier": "high"}, {"id": "never", "tier": "high"}],
             "history": [{"at": "2026-07-01", "task": "tuned",
                          "from": "mid", "to": "high", "reason": "x"}]}
    e = [{"ts": "2026-08-05", "task": "tuned", "tier": "high",
          "model": "claude-opus-4-8", "est_tokens": 1_000_000},
         {"ts": "2026-08-05", "task": "never", "tier": "high",
          "model": "claude-opus-4-8", "est_tokens": 5_000_000}]
    sav = a.compute_savings(e, alloc)
    assert sav["tuned_tasks"] == 1
    # only the tuned task feeds the comparison: 1M @ mid(3.6) vs 1M @ high(9)
    assert sav["initial_cost"] == 3_600_000
    assert sav["declared_cost"] == 9_000_000
    # ...while the untuned 5M tokens still count in real spend
    assert sav["actual_cost"] == 54_000_000
    assert ", across 1 tuned task(s)" in \
        [x for x in a.spend_summary_lines(sav, alloc=alloc) if "cold-start" in x][0]


# ---------------------------------------------------- source/install parity
# T-20260821-66:兩份副本漂移(改了 source、忘記 pip install --user .)必須被機器
# 抓到。兩側路徑一律注入 tmp_path,**不碰真 site-packages 也不碰真 source**
# (公司規則 9:selftest 不得寫真產線帳;memory
# `selftest-sandbox-arg-and-write-path-must-be-same-source`——讀哪一份就斷言哪一份)。

def _mk_pkg(root, name, body):
    """在 root 下造一份假 package 目錄(source 側走 src/agent_steward 佈局)。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "cli.py").write_text(body, encoding="utf-8")
    (d / "allocate.py").write_text("ALLOC = 1\n", encoding="utf-8")
    return d


def test_source_parity_matches(tmp_path):
    """正案:兩份內容一致 → ok,detail 帶得出雜湊前綴(讀數不是空話)。"""
    from agent_steward.cli import check_source_parity
    src_root = tmp_path / "repo"
    _mk_pkg(src_root / "src", "agent_steward", "X = 1\n")
    inst = _mk_pkg(tmp_path / "site-packages", "agent_steward", "X = 1\n")
    ok, detail = check_source_parity(installed_dir=str(inst),
                                     source_root=str(src_root))
    assert ok, detail
    assert "parity ok" in detail


def test_source_parity_detects_drift(tmp_path):
    """反案(goal_anchor ②):source 已改、安裝副本還是舊的 → 必紅且指路重裝。

    這就是 08-15→08-21 那七天的形狀:安裝副本停在舊碼,產線照跑,沒有任何訊號。
    """
    from agent_steward.cli import check_source_parity
    src_root = tmp_path / "repo"
    _mk_pkg(src_root / "src", "agent_steward", "X = 2  # 改過的 source\n")
    inst = _mk_pkg(tmp_path / "site-packages", "agent_steward", "X = 1  # 舊安裝副本\n")
    ok, detail = check_source_parity(installed_dir=str(inst),
                                     source_root=str(src_root))
    assert not ok
    assert "Reinstall" in detail and "pip install --user" in detail


def test_source_parity_drift_in_sibling_module(tmp_path):
    """反案二:漂移不在 cli.py 而在 allocate.py(今天真的被改的就是這支)——
    雜湊涵蓋全部 .py,所以照樣紅;只比 cli.py 的實作會漏掉這型。"""
    from agent_steward.cli import check_source_parity
    src_root = tmp_path / "repo"
    src_pkg = _mk_pkg(src_root / "src", "agent_steward", "X = 1\n")
    (src_pkg / "allocate.py").write_text("ALLOC = 2\n", encoding="utf-8")
    inst = _mk_pkg(tmp_path / "site-packages", "agent_steward", "X = 1\n")
    ok, _ = check_source_parity(installed_dir=str(inst), source_root=str(src_root))
    assert not ok


def test_source_parity_na_without_source_checkout(tmp_path):
    """射程:machine 上沒有 source repo(純消費端)→ ok=True 且說明 n/a,
    不對只裝了 wheel 的機器誤紅(fail-open 的邊界寫在函式裡,不留給呼叫端猜)。"""
    from agent_steward.cli import check_source_parity
    inst = _mk_pkg(tmp_path / "site-packages", "agent_steward", "X = 1\n")
    ok, detail = check_source_parity(installed_dir=str(inst),
                                     source_root=str(tmp_path / "no-such-repo"))
    assert ok
    assert "n/a" in detail
