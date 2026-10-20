import json
import subprocess
from pathlib import Path

import pytest

from factorylib.paths import Job, find_root, load_job


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_find_root_from_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-b", "main")
    (main / "f.txt").write_text("x")
    git(main, "add", "f.txt")
    git(main, "commit", "-m", "init")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", str(wt), "-b", "b")
    assert find_root(wt).resolve() == main.resolve()
    assert find_root(main).resolve() == main.resolve()


def test_load_job_reads_fields(tmp_path):
    folder = tmp_path / "factory" / "jobs" / "001-x"
    folder.mkdir(parents=True)
    raw = {
        "id": "001-x",
        "worktree": str(tmp_path / "wt"),
        "branch": "factory/001-x",
        "feature": "Do x",
        "extra": 1,
    }
    (folder / "job.json").write_text(json.dumps(raw))
    job = load_job(tmp_path, "001-x")
    assert isinstance(job, Job)
    assert job.id == "001-x"
    assert job.folder == folder
    assert job.worktree == tmp_path / "wt"
    assert job.branch == "factory/001-x"
    assert job.feature == "Do x"
    assert job.raw == raw


def test_load_job_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError) as e:
        load_job(tmp_path, "nope")
    assert "factory/jobs/nope/job.json" in str(e.value)
