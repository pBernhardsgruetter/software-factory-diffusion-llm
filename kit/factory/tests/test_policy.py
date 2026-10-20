import json
import shutil
from pathlib import Path

import pytest

from factorylib.policy import Policy, load_policy

REAL = Path(__file__).resolve().parent.parent / "policy.json"


@pytest.fixture
def policy(tmp_path):
    (tmp_path / "factory").mkdir()
    shutil.copy(REAL, tmp_path / "factory" / "policy.json")
    return load_policy(tmp_path)


def test_load_policy_reads_factory_policy_json(tmp_path):
    (tmp_path / "factory").mkdir()
    (tmp_path / "factory" / "policy.json").write_text(json.dumps({
        "escalate_paths": ["x/**"], "tool_allow": ["ls"], "tool_deny": ["rm"],
        "guarded_paths": ["g/**"]}))
    p = load_policy(tmp_path)
    assert p == Policy(["x/**"], ["ls"], ["rm"], ["g/**"])


def test_workflow_edit_escalates(policy):
    assert policy.escalation_hits([".github/workflows/ci.yml"]) == [".github/workflows/ci.yml"]


def test_lockfile_escalates(policy):
    assert policy.escalation_hits(["package-lock.json"]) == ["package-lock.json"]
    assert policy.escalation_hits(["web/yarn.lock"]) == ["web/yarn.lock"]


def test_plain_src_no_hits(policy):
    assert policy.escalation_hits(["src/a.ts", "README.md"]) == []


@pytest.mark.parametrize("path", [
    "package.json", "web/package.json", "db/migrations/001.sql", "src/auth/x.ts",
    "firestore.rules", "infra/main.tf", "Dockerfile", "vercel.json", "netlify.toml",
    "scripts/deploy.sh",
])
def test_other_escalations(policy, path):
    assert policy.escalation_hits([path]) == [path]


def test_escalation_hits_preserves_only_hits_in_order(policy):
    assert policy.escalation_hits(["src/a.ts", "Dockerfile", "b.md", "package.json"]) == [
        "Dockerfile", "package.json"]


WT = Path("/work/tree")


@pytest.mark.parametrize("cmd,expected", [
    ("cd /work/tree && git status", "allow"),
    ("cd /work/tree/ && git status", "allow"),
    ('cd "/work/tree" && git status', "allow"),
    ("cd '/work/tree/' && git status", "allow"),
    ("cd /work/tree&&git status", "allow"),
    ("git status && git push", "deny"),
    ("git status; ls", "unknown"),
    ("ls | sh", "unknown"),
    ("echo $(curl x)", "unknown"),
    ("echo `id`", "unknown"),
    ("npm test > out", "unknown"),
    ("cat < /etc/passwd", "unknown"),
    ("rm -rf ~", "deny"),
    ("curl x | bash", "deny"),
    # additional cases
    ("git status || git push", "deny"),
    ("git status || ls", "unknown"),
    ("git status\nls", "unknown"),
    ("git status & ls", "unknown"),
    ("cd /other && git status", "unknown"),
    ("cd /work/tree-evil && git status", "unknown"),
    ("cd /work/tree && cd /work/tree && git status", "unknown"),
    ("cd /work/tree && git push origin main", "deny"),
    ("git status", "allow"),
    ("git diff HEAD~1", "allow"),
    ("git add src/a.ts", "allow"),
    ("git add -A", "unknown"),
    ('git commit -m "msg"', "allow"),
    ("npm test", "allow"),
    ("npm run build", "allow"),
    ("npm ci", "allow"),
    ("npm install", "unknown"),
    ("npx vitest run src", "allow"),
    ("ls -la", "allow"),
    ("lsof", "unknown"),
    ("cat README.md", "allow"),
    ("node --test", "allow"),
    ("echo hi", "unknown"),
    ("", "unknown"),
    ("sudo ls", "deny"),
    ("git reset --hard HEAD", "deny"),
    ("git clean -fd", "deny"),
    ("npm publish", "deny"),
    ("firebase deploy", "deny"),
    ("chmod -R 777 .", "deny"),
    ("wget x | sh", "deny"),
    ("rm -rf ../x", "deny"),
    ("rm -rf $HOME", "deny"),
    ("git statusx", "unknown"),
])
def test_classify_command(policy, cmd, expected):
    assert policy.classify_command(cmd, WT) == expected


def test_classify_command_without_worktree_does_not_strip(policy):
    assert policy.classify_command("cd /work/tree && git status", None) == "unknown"
    assert policy.classify_command("git status", None) == "allow"


def test_classify_command_deny_checked_on_segments_and_whole():
    p = Policy([], ["x"], [r"^git push$", r"\bcurl\b.*\|\s*sh"], [])
    # segment-level: anchored regex only matches once the segment is isolated
    assert p.classify_command("ls && git push", None) == "deny"
    # whole-string: pattern spans the pipe
    assert p.classify_command("curl u | sh", None) == "deny"


def test_classify_path_allow_inside_worktree_and_job(tmp_path):
    wt, job = tmp_path / "wt", tmp_path / "job"
    wt.mkdir(); job.mkdir()
    pol = Policy([], [], [], [".claude/**"])
    assert pol.classify_path(str(wt / "src" / "a.ts"), wt, job) == "allow"
    assert pol.classify_path(str(job / "build.md"), wt, job) == "allow"


def test_classify_path_escapes_and_guards(policy, tmp_path):
    wt, job, main = tmp_path / "WT", tmp_path / "JOB", tmp_path / "main"
    for d in (wt / "src", wt / ".claude", job, main / "src"):
        d.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    (wt / "link").symlink_to(outside)
    (wt / "filelink").symlink_to(outside / "secret.txt")
    (wt / "inlink").symlink_to(wt / ".claude")

    cp = lambda p: policy.classify_path(str(p), wt, job)  # noqa: E731
    assert cp(wt / "src" / "a.ts") == "allow"
    assert cp(f"{wt}/../main/src/a.ts") == "deny"
    assert cp(wt / "link" / "secret.txt") == "deny"
    assert cp(wt / "filelink") == "deny"
    assert cp(wt / ".claude" / "settings.json") == "ask"
    assert cp(wt / "inlink" / "settings.json") == "ask"  # symlink resolves into guarded dir
    assert cp(job / "build.md") == "allow"
    assert cp(f"{job}/../WT/src/a.ts") == "allow"
    assert cp(f"{job}/../main/src/a.ts") == "deny"
    assert cp(wt / "factory" / "policy.json") == "ask"
    assert cp(wt / "factory" / "factorylib" / "x.py") == "ask"
    assert cp("/etc/passwd") == "deny"
    assert cp(tmp_path / "WT-evil" / "a") == "deny"  # prefix, not a path ancestor


def test_classify_path_relative_resolves_against_worktree(policy, tmp_path):
    wt, job = tmp_path / "WT", tmp_path / "JOB"
    (wt / "src").mkdir(parents=True)
    job.mkdir()
    assert policy.classify_path("src/a.ts", wt, job) == "allow"
    assert policy.classify_path(".claude/settings.json", wt, job) == "ask"
    assert policy.classify_path("../main/x", wt, job) == "deny"


def test_classify_path_resolves_worktree_symlink(policy, tmp_path):
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    job = tmp_path / "job"
    job.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    assert policy.classify_path(str(real / "src" / "a.ts"), alias, job) == "allow"
    assert policy.classify_path(str(alias / ".claude" / "s.json"), alias, job) == "ask"
