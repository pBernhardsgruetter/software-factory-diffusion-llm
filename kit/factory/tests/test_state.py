import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from factorylib.config import load_config
from factorylib.paths import load_job
from factorylib.state import build_state, parse_assumptions, parse_findings

KIT = Path(__file__).resolve().parent.parent


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


SPEC = """# Spec

## Summary
Add a thing.

## Assumptions
- Users are logged in
- The list is short and
  fits on one screen
* Dates are UTC
1. Prices include tax

## Acceptance criteria
- The button is blue
- The page shows three cards

## Notes
- not an assumption
"""

SEC = """VERDICT: CHANGES

## Findings
- Missing CSRF token on form
- Cookie lacks Secure flag
  when served over http

## Notes
- ignore me
"""

CODE = "VERDICT: PASS\n\n## Findings\nNone.\n"


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "main"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "README.md").write_text("hi\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "init")
    (root / "factory").mkdir()
    shutil.copy(KIT / "config.json", root / "factory" / "config.json")
    shutil.copy(KIT / "policy.json", root / "factory" / "policy.json")
    wt = tmp_path / "wt-001"
    git(root, "worktree", "add", str(wt), "-b", "factory/001-x")
    (wt / "src").mkdir()
    (wt / "src" / "a.ts").write_text("export const a = 1;\n")
    git(wt, "add", "src/a.ts")
    git(wt, "commit", "-m", "a")
    c1 = git(wt, "rev-parse", "HEAD")
    (wt / "src" / "b.ts").write_text("export const b = 2;\n")
    git(wt, "add", "src/b.ts")
    git(wt, "commit", "-m", "b")
    folder = root / "factory" / "jobs" / "001-x"
    (folder / "round-1").mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({
        "id": "001-x", "feature": "Add a thing", "branch": "factory/001-x",
        "worktree": str(wt), "review_commits": {"security": c1, "code": c1},
    }))
    (folder / "spec.md").write_text(SPEC)
    (folder / "round-1" / "review-security.md").write_text(SEC)
    (folder / "round-1" / "review-code.md").write_text(CODE)
    job = load_job(root, "001-x")
    return root, job, c1


def test_parse_findings_none():
    assert parse_findings("VERDICT: PASS\n\n## Findings\nNone.\n") == []
    assert parse_findings("VERDICT: PASS\n\n## Findings\n\n- None.\n\n## Next\n") == []
    assert parse_findings("VERDICT: PASS\n\n## Other\n- x\n") == []


def test_parse_findings_bullets():
    assert parse_findings(SEC) == [
        "Missing CSRF token on form",
        "Cookie lacks Secure flag when served over http",
    ]


def test_parse_findings_prose_is_kept_not_dropped():
    assert parse_findings("VERDICT: CHANGES\n\n## Findings\nThe whole thing is wrong.\n") == [
        "The whole thing is wrong."]


def test_parse_assumptions_bullet_styles():
    assert parse_assumptions(SPEC) == [
        "Users are logged in",
        "The list is short and fits on one screen",
        "Dates are UTC",
        "Prices include tax",
    ]


def test_unknown_decision_raises(env):
    root, job, _ = env
    with pytest.raises(ValueError, match="unknown decision nope"):
        build_state("nope", root, job, {})


def test_extra_overrides_and_failure_is_extra_only(env):
    root, job, _ = env
    assert build_state("failure", root, job, {"command": "npm test", "output_tail": "boom"}) == {
        "command": "npm test", "output_tail": "boom"}
    assert build_state("toolgate", root, job, {"tool": "Bash"}) == {"tool": "Bash"}
    assert build_state("clarify", root, job, {"scout": "override"})["scout"] == "override"


def test_triage_and_clarify(env):
    root, job, _ = env
    s = build_state("triage", root, job, {})
    assert s["feature"] == "Add a thing"
    assert "README.md" in s["repo_outline"].splitlines()
    assert "src/a.ts" in s["repo_outline"].splitlines()
    assert build_state("clarify", root, job, {}) == {"feature": "Add a thing", "scout": ""}
    (job.folder / "scout.md").write_text("scouted")
    assert build_state("clarify", root, job, {})["scout"] == "scouted"


def test_triage_outline_limited_to_400_lines(env):
    root, job, _ = env
    wt = job.worktree
    for i in range(450):
        (wt / f"f{i:03d}.txt").write_text("x")
    git(wt, "add", "-A")
    git(wt, "commit", "-m", "many")
    assert len(build_state("triage", root, job, {})["repo_outline"].splitlines()) == 400


def test_assumptions_state(env):
    root, job, _ = env
    assert build_state("assumptions", root, job, {})["assumptions"][0] == "Users are logged in"


def test_rereview_never_run_uses_full_branch_diff(env):
    root, job, c1 = env
    s = build_state("rereview", root, job, {})
    by = {r["name"]: r for r in s["reviewers"]}
    assert [r["name"] for r in s["reviewers"]] == ["security", "ux", "ui", "code"]
    assert by["ux"]["last_verdict"] is None
    assert by["ui"]["last_verdict"] is None
    assert by["security"]["last_verdict"] == "CHANGES"
    assert by["code"]["last_verdict"] == "PASS"
    full = git(job.worktree, "diff", "--stat", "main...HEAD")
    assert by["ux"]["delta_stat"] == full
    assert "src/a.ts" in full and "src/b.ts" in full
    # ran reviewers see only the delta since their recorded commit
    assert "src/b.ts" in by["security"]["delta_stat"]
    assert "src/a.ts" not in by["security"]["delta_stat"]


def test_rereview_uses_highest_round_and_normalises_verdict(env):
    root, job, _ = env
    (job.folder / "round-2").mkdir()
    (job.folder / "round-10").mkdir()
    (job.folder / "round-2" / "review-security.md").write_text("verdict: pass\n")
    (job.folder / "round-10" / "review-code.md").write_text("VERDICT: CHANGES\n")
    (job.folder / "round-10" / "review-ux.md").write_text("looks fine to me\n")
    by = {r["name"]: r for r in build_state("rereview", root, job, {})["reviewers"]}
    assert by["security"]["last_verdict"] == "PASS"
    assert by["code"]["last_verdict"] == "CHANGES"
    assert by["ux"]["last_verdict"] == "UNKNOWN"


def test_rereview_unknown_recorded_sha_falls_back_to_full_diff(env):
    root, job, _ = env
    job.raw["review_commits"]["security"] = "deadbeef" * 5
    by = {r["name"]: r for r in build_state("rereview", root, job, {})["reviewers"]}
    assert by["security"]["delta_stat"] == git(job.worktree, "diff", "--stat", "main...HEAD")
    job.raw["review_commits"]["security"] = "--output=/tmp/x"
    by = {r["name"]: r for r in build_state("rereview", root, job, {})["reviewers"]}
    assert "src/a.ts" in by["security"]["delta_stat"]


def test_finding_state(env):
    root, job, _ = env
    s = build_state("finding", root, job, {"round": 1})
    assert s["findings"] == [
        {"reviewer": "security", "text": "Missing CSRF token on form"},
        {"reviewer": "security", "text": "Cookie lacks Secure flag when served over http"},
    ]
    assert build_state("finding", root, job, {"round": 2})["findings"] == []


def test_promote_and_stuck_state(env):
    root, job, _ = env
    p = build_state("promote", root, job, {"round": 1})
    assert p["round"] == 1
    assert p["verdicts"] == {"security": "CHANGES", "code": "PASS"}
    assert len(p["findings"]) == 2
    (job.folder / "round-2").mkdir()
    (job.folder / "round-2" / "review-security.md").write_text(
        "VERDICT: CHANGES\n\n## Findings\n- Still no CSRF token\n")
    s = build_state("stuck", root, job, {"round": 2})
    assert [f["text"] for f in s["previous"]] == [
        "Missing CSRF token on form", "Cookie lacks Secure flag when served over http"]
    assert s["current"] == [{"reviewer": "security", "text": "Still no CSRF token"}]
    assert build_state("stuck", root, job, {"round": 1})["previous"] == []


def test_escalation_includes_rule_hits_for_lockfile(env):
    root, job, _ = env
    wt = job.worktree
    (wt / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    git(wt, "add", "package-lock.json")
    git(wt, "commit", "-m", "lock")
    s = build_state("escalation", root, job, {})
    assert "package-lock.json" in s["changed_paths"]
    assert s["rule_hits"] == ["package-lock.json"]
    assert "package-lock.json" in s["diff_stat"]
    assert "lockfileVersion" in s["diff"]
    assert "## Assumptions" in s["spec"]


def test_escalation_flags_both_sides_of_a_rename(env):
    root, job, _ = env
    wt = job.worktree
    (root / "package.json").write_text('{"name": "x", "version": "1.0.0", "k": 1}\n' * 3)
    git(root, "add", "package.json")
    git(root, "commit", "-m", "pkg on main")
    git(wt, "merge", "-q", "--no-edit", "main")
    git(wt, "mv", "package.json", "src/pkg.txt")
    git(wt, "commit", "-m", "rename")
    s = build_state("escalation", root, job, {})
    assert "package.json" in s["changed_paths"]
    assert "package.json" in s["rule_hits"]


def test_escalation_git_failure_fails_closed(env):
    root, job, _ = env
    job.worktree = job.worktree / "does-not-exist"
    s = build_state("escalation", root, job, {})
    assert s["changed_paths"] == []
    assert s["rule_hits"] == ["<git diff failed>"]
    assert s["diff"] == "" and s["diff_stat"] == ""
    assert build_state("triage", root, job, {})["repo_outline"] == ""


def test_visual_state_reads_observations(env):
    root, job, _ = env
    s = build_state("visual", root, job, {"round": 1})
    assert s["criteria"] == ["The button is blue", "The page shows three cards"]
    assert s["observations"] == []
    (job.folder / "round-1" / "observations.json").write_text(
        json.dumps(["blue button visible", "two cards"]))
    s = build_state("visual", root, job, {"round": 1})
    assert s["observations"] == ["blue button visible", "two cards"]
    (job.folder / "round-1" / "observations.json").write_text("{not json")
    assert build_state("visual", root, job, {"round": 1})["observations"] == []


def test_huge_binary_diff_is_capped_and_json_serialisable(env):
    root, job, _ = env
    wt = job.worktree
    lines = (f'    "node_modules/pkg-{i}": {{"version": "1.0.{i}", '
             f'"integrity": "sha512-abcdefghijklmnopqrstuvwxyz{i}"}},\n' for i in range(70000))
    (wt / "package-lock.json").write_text("".join(lines))
    assert (wt / "package-lock.json").stat().st_size > 5_000_000
    (wt / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00" + os.urandom(4000))
    git(wt, "add", "package-lock.json", "logo.png")
    git(wt, "commit", "-m", "big")
    cap = load_config(root)["decide"]["state_char_cap"]
    s = build_state("escalation", root, job, {})
    blob = json.dumps(s)
    assert len(blob) <= cap
    assert "package-lock.json" in s["rule_hits"]
    assert "truncated" in s["diff"]


def test_small_binary_diff_says_binary_files_differ(env):
    root, job, _ = env
    wt = job.worktree
    (wt / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00" + os.urandom(500))
    git(wt, "add", "logo.png")
    git(wt, "commit", "-m", "png")
    s = build_state("escalation", root, job, {})
    assert "Binary files" in s["diff"] and "logo.png" in s["diff"]
    assert "truncated" not in s["diff"]


def test_cap_is_split_across_large_fields(env):
    root, job, _ = env
    cap = load_config(root)["decide"]["state_char_cap"]
    s = build_state("failure", root, job, {"command": "x", "output_tail": "y" * (cap * 3)})
    assert len(s["output_tail"]) <= cap + 100
    s = build_state("clarify", root, job, {"scout": "z\n" * cap})
    assert len(json.dumps(s)) <= cap
    assert len(s["scout"]) < cap


def _many_files(wt, n=6000, tag="f"):
    d = wt / "src" / "generated"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / (f"{tag}-{i:05d}-" + ("a" * 40) + ".txt")).write_text(f"{i}\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-m", "many files")


def test_rereview_git_failure_forces_unknown(env):
    root, job, _ = env
    job.worktree = job.worktree / "does-not-exist"
    s = build_state("rereview", root, job, {})
    assert [r["name"] for r in s["reviewers"]] == ["security", "ux", "ui", "code"]
    for r in s["reviewers"]:
        assert r["last_verdict"] == "UNKNOWN"
        assert r["delta_stat"] == "<git diff failed>"


def test_sha_must_fullmatch(env):
    root, job, c1 = env
    job.raw["review_commits"]["security"] = c1 + "\n"
    by = {r["name"]: r for r in build_state("rereview", root, job, {})["reviewers"]}
    assert "src/a.ts" in by["security"]["delta_stat"]  # fell back to the full diff


def test_escalation_many_paths_within_cap(env):
    root, job, _ = env
    _many_files(job.worktree)
    cap = load_config(root)["decide"]["state_char_cap"]
    s = build_state("escalation", root, job, {})
    assert len(json.dumps(s)) <= cap
    assert re.fullmatch(r"…\[\d+ more paths\]", s["changed_paths"][-1])
    kept = len(s["changed_paths"]) - 1
    assert kept + int(re.search(r"\d+", s["changed_paths"][-1]).group()) >= 6000


def test_rereview_many_paths_within_cap(env):
    root, job, _ = env
    _many_files(job.worktree)
    cap = load_config(root)["decide"]["state_char_cap"]
    s = build_state("rereview", root, job, {})
    assert len(json.dumps(s)) <= cap
    assert [r["name"] for r in s["reviewers"]] == ["security", "ux", "ui", "code"]
    assert all(r["delta_stat"] for r in s["reviewers"])


def test_rule_hits_computed_before_path_truncation(env):
    root, job, _ = env
    wt = job.worktree
    _many_files(wt, n=5999)
    (wt / "zzz").mkdir()
    (wt / "zzz" / "package-lock.json").write_text("{}\n")  # sorts last: path #5999
    git(wt, "add", "zzz/package-lock.json")
    git(wt, "commit", "-m", "lock")
    cap = load_config(root)["decide"]["state_char_cap"]
    s = build_state("escalation", root, job, {})
    assert len(json.dumps(s)) <= cap
    assert "zzz/package-lock.json" in s["rule_hits"]
    assert "zzz/package-lock.json" not in s["changed_paths"]  # truncated away
    assert re.fullmatch(r"…\[\d+ more paths\]", s["changed_paths"][-1])


def test_list_item_strings_are_capped(env):
    root, job, _ = env
    cap = load_config(root)["decide"]["state_char_cap"]
    big = "w" * (cap * 2)
    s = build_state("visual", root, job, {"round": 1, "observations": [big, "ok"]})
    assert len(json.dumps(s)) <= cap and s["observations"][1] == "ok"
    s = build_state("finding", root, job, {"findings": [{"reviewer": "code", "text": big}]})
    assert len(json.dumps(s)) <= cap
    s = build_state("assumptions", root, job, {"assumptions": [big] * 10})
    assert len(json.dumps(s)) <= cap


def test_state_never_exceeds_cap_for_many_list_items(env):
    root, job, _ = env
    cap = load_config(root)["decide"]["state_char_cap"]
    items = [{"reviewer": "code", "text": "x" * 3000} for _ in range(500)]
    s = build_state("finding", root, job, {"findings": items})
    assert len(json.dumps(s)) <= cap
    assert len(s["findings"]) == 500  # all kept, each shortened
    many = [{"reviewer": "code", "text": "x" * 40} for _ in range(20000)]
    s = build_state("finding", root, job, {"findings": many})
    assert len(json.dumps(s)) <= cap
    assert re.fullmatch(r"…\[\d+ more items\]", s["findings"][-1]["text"])


def test_secret_at_cut_point_is_redacted(env):
    root, job, _ = env
    wt = job.worktree
    cap = load_config(root)["decide"]["state_char_cap"]
    key = "sk-or-v1-" + "0123456789abcdef" * 4
    n = 400_000

    def commit(content):
        (wt / "big.txt").write_text(content)
        git(wt, "add", "big.txt")
        git(wt, "commit", "-m", "big")

    commit("x" * n)  # probe: same-length content gives the same diff header
    diff = git(wt, "diff", "main...HEAD")
    header = diff.index("+" + "x" * 50)
    git(wt, "reset", "-q", "--hard", "HEAD~1")
    cut = (cap // 3) // 2  # escalation: spec, diff_stat, diff share the cap; head = half
    start = cut - header - 1 - 30  # key straddles the cut point
    commit("x" * start + " " + key + " " + "x" * (n - start - len(key) - 2))
    s = build_state("escalation", root, job, {})
    blob = json.dumps(s)
    assert "truncated" in s["diff"]
    assert "sk-or-v1" not in blob
    assert "0123456789abcdef"[:8] + "0123456789ab" not in blob
    assert "0123456789abcdef0123" not in blob


def test_redact_patterns_from_config_apply(env):
    root, job, _ = env
    cfg = json.loads((root / "factory" / "config.json").read_text())
    cfg["redact_patterns"] = ["INTERNAL-[0-9]+"]
    (root / "factory" / "config.json").write_text(json.dumps(cfg))
    s = build_state("failure", root, job, {"command": "x", "output_tail": "ref INTERNAL-12345 here"})
    assert "INTERNAL-12345" not in s["output_tail"]


def test_extra_cannot_clear_rule_hits(env):
    root, job, _ = env
    wt = job.worktree
    (wt / "package-lock.json").write_text("{}\n")
    git(wt, "add", "package-lock.json")
    git(wt, "commit", "-m", "lock")
    s = build_state("escalation", root, job, {"rule_hits": [], "changed_paths": []})
    assert s["rule_hits"] == ["package-lock.json"]
    assert "package-lock.json" in s["changed_paths"]
    s = build_state("escalation", root, job, {
        "rule_hits": ["manual", "package-lock.json"], "changed_paths": ["extra.txt"]})
    assert s["rule_hits"] == ["package-lock.json", "manual"]
    assert s["changed_paths"][-1] == "extra.txt" and "package-lock.json" in s["changed_paths"]
    s = build_state("escalation", root, job, {"rule_hits": "nonsense"})
    assert s["rule_hits"] == ["package-lock.json"]
