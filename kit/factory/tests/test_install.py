"""scripts/install.sh and the installed wrappers, end to end in scratch git repos."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent  # kit/factory
INSTALL = KIT.parent.parent / "scripts" / "install.sh"

pytestmark = pytest.mark.skipif(not INSTALL.is_file(), reason="not in the factory repo")
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")


def clean_env(**extra):
    e = {k: v for k, v in os.environ.items()
         if k not in ("OPENROUTER_API_KEY", "FACTORY_HEADLESS", "FACTORY_GATE_ROOT",
                      "PYTHONDONTWRITEBYTECODE", "VIRTUAL_ENV")}
    e.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    e.update(extra)
    return e


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=clean_env()).stdout


def new_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("app\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "init")
    return path


def install(target, *args):
    return subprocess.run(["bash", str(INSTALL), *args, str(target)], capture_output=True,
                          text=True, env=clean_env(), timeout=120)


# ------------------------------------------------------------------ C1


@needs_uv
def test_decide_leaves_the_main_checkout_clean(tmp_path):
    repo = new_repo(tmp_path / "app")
    r = install(repo)
    assert r.returncode == 0, r.stderr
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "install factory")
    assert git(repo, "status", "--porcelain") == ""
    job = repo / "factory" / "jobs" / "x"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({
        "id": "x", "feature": "f", "branch": "factory/x",
        "worktree": str(tmp_path / "app-factory" / "x")}))
    lock = (repo / "factory" / "uv.lock").read_text()
    r = subprocess.run([str(repo / "factory" / "bin" / "decide"), "triage", "--job", "x"],
                       cwd=repo, capture_output=True, text=True, env=clean_env(), timeout=300)
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert "OPENROUTER_API_KEY not set" in rec["error"]
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert not list((repo / "factory").rglob("__pycache__"))
    assert (repo / "factory" / "uv.lock").read_text() == lock


def test_wrappers_are_isolated_bytecode_free_and_frozen():
    for name in ("decide", "fast-coder", "tool-gate"):
        text = (KIT / "bin" / name).read_text()
        assert "uv run --frozen" in text, name
        assert "python -I -B -m factorylib." in text, name


def test_install_ignores_bytecode_and_pytest_cache(tmp_path):
    repo = new_repo(tmp_path / "app")
    r = install(repo)
    assert r.returncode == 0, r.stderr
    lines = (repo / ".gitignore").read_text().splitlines()
    for line in ("/factory/jobs/", "/factory/.venv/", "/factory/tool-gate.jsonl",
                 "/factory/**/__pycache__/", "/factory/.pytest_cache/"):
        assert lines.count(line) == 1, line
    # idempotent with --force
    assert install(repo, "--force").returncode == 0
    lines = (repo / ".gitignore").read_text().splitlines()
    assert lines.count("/factory/**/__pycache__/") == 1


# ------------------------------------------------------------------ I5


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(root).parts}


@pytest.mark.parametrize("rel", [".claude/settings.json", ".claude/agents/builder.md",
                                 ".claude/skills/factory/SKILL.md"])
def test_existing_claude_files_refuse_without_force(tmp_path, rel):
    repo = new_repo(tmp_path / "app")
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("mine\n")
    before = _tree(repo)
    r = install(repo)
    assert r.returncode == 1
    assert rel in r.stderr and "--force" in r.stderr
    assert _tree(repo) == before  # nothing written


def test_unrelated_claude_files_are_kept(tmp_path):
    repo = new_repo(tmp_path / "app")
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "agents" / "mine.md").write_text("mine\n")
    (repo / ".claude" / "settings.local.json").write_text("{}\n")
    r = install(repo)
    assert r.returncode == 0, r.stderr
    assert (repo / ".claude" / "agents" / "mine.md").read_text() == "mine\n"
    assert (repo / ".claude" / "settings.local.json").read_text() == "{}\n"
    assert not list((repo / ".claude").glob(".factory-backup-*"))


def test_force_backs_up_overwritten_claude_files(tmp_path):
    repo = new_repo(tmp_path / "app")
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text('{"mine": true}\n')
    (repo / ".claude" / "agents" / "builder.md").write_text("my builder\n")
    r = install(repo, "--force")
    assert r.returncode == 0, r.stderr
    kit_claude = KIT.parent / ".claude"
    assert ((repo / ".claude" / "settings.json").read_bytes()
            == (kit_claude / "settings.json").read_bytes())
    (backup,) = (repo / ".claude").glob(".factory-backup-*")
    assert (backup / "settings.json").read_text() == '{"mine": true}\n'
    assert (backup / "agents" / "builder.md").read_text() == "my builder\n"
    assert sorted(str(p.relative_to(backup)) for p in backup.rglob("*") if p.is_file()) == [
        "agents/builder.md", "settings.json"]
    assert str(backup.relative_to(repo)) in r.stdout
    # the backup never dirties the checkout the factory merges into
    git(repo, "add", "-A")
    assert ".factory-backup-" not in git(repo, "status", "--porcelain", "--ignored=no")
    assert ".factory-backup-" not in git(repo, "diff", "--cached", "--name-only")


def test_reinstall_with_force_keeps_backlog_and_backs_up(tmp_path):
    repo = new_repo(tmp_path / "app")
    assert install(repo).returncode == 0
    (repo / "factory" / "backlog.md").write_text("- [ ] mine\n")
    r = install(repo)
    assert r.returncode == 1 and "--force" in r.stderr
    r = install(repo, "--force")
    assert r.returncode == 0, r.stderr
    assert (repo / "factory" / "backlog.md").read_text() == "- [ ] mine\n"
    assert len(list((repo / ".claude").glob(".factory-backup-*"))) == 1


# ------------------------------------------------------------------ I6


def test_subdirectory_target_is_refused(tmp_path):
    repo = new_repo(tmp_path / "app")
    (repo / "sub").mkdir()
    r = install(repo / "sub")
    assert r.returncode == 1 and "top level" in r.stderr
    assert not (repo / "sub" / "factory").exists() and not (repo / "sub" / ".claude").exists()


def test_symlinked_target_is_accepted(tmp_path):
    repo = new_repo(tmp_path / "app")
    (tmp_path / "link").symlink_to(repo)
    r = install(tmp_path / "link")
    assert r.returncode == 0, r.stderr
    assert (repo / "factory" / "bin" / "decide").is_file()


def test_non_git_target_is_refused(tmp_path):
    (tmp_path / "plain").mkdir()
    r = install(tmp_path / "plain")
    assert r.returncode == 1 and "not a git repository" in r.stderr


def test_install_script_is_bash_3_2_clean():
    r = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = INSTALL.read_text()
    for bad in ("mapfile", "readarray", "declare -A", "local -n", ",,}", "^^}", "&>>", "|&",
                "${!", "coproc"):
        assert bad not in text, bad
    # under set -u bash 3.2 fails on "${a[@]}" of an empty array: always guard it
    import re
    unguarded = re.sub(r'\$\{(\w+)\[@\]\+"\$\{\1\[@\]\}"\}', "", text)
    assert "[@]}" not in unguarded


def test_install_without_rsync_matches_rsync(tmp_path):
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    a, b = new_repo(tmp_path / "a"), new_repo(tmp_path / "b")
    assert install(a).returncode == 0
    d = tmp_path / "norsync"
    d.mkdir()
    for p in os.environ["PATH"].split(os.pathsep):
        for exe in Path(p).glob("*") if Path(p).is_dir() else ():
            if exe.name != "rsync" and not (d / exe.name).exists():
                try:
                    (d / exe.name).symlink_to(exe)
                except OSError:
                    pass
    r = subprocess.run(["bash", str(INSTALL), str(b)], capture_output=True, text=True,
                       env=clean_env(PATH=str(d)), timeout=120)
    assert r.returncode == 0, r.stderr
    assert _tree(a) == _tree(b)
