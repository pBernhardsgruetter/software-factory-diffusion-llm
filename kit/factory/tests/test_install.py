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
