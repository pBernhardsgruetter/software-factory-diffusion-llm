"""Locate the main checkout and load factory job records."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


def find_root(cwd: Path | None = None) -> Path:
    """Return the main checkout, even when ``cwd`` is inside a linked worktree."""
    cwd = Path.cwd() if cwd is None else Path(cwd)
    out = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    common = Path(out)
    if not common.is_absolute():
        common = cwd / common
    return common.resolve().parent


@dataclass
class Job:
    id: str
    folder: Path
    worktree: Path
    branch: str
    feature: str
    raw: dict


def load_job(root: Path, job_id: str) -> Job:
    """Read ``factory/jobs/<id>/job.json`` under ``root``."""
    folder = Path(root) / "factory" / "jobs" / job_id
    path = folder / "job.json"
    if not path.is_file():
        raise FileNotFoundError(str(path))
    raw = json.loads(path.read_text())
    return Job(
        id=raw.get("id", job_id),
        folder=folder,
        worktree=Path(raw["worktree"]),
        branch=raw["branch"],
        feature=raw["feature"],
        raw=raw,
    )
