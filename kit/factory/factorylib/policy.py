"""Deterministic safety policy: escalation paths, tool-call and file-path gates.

These rules run BEFORE any model-based decision and can never be overridden by
one. When in doubt a command is ``unknown`` (never ``allow``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

# Segment separators. ``&&`` / ``||`` must precede the single-char forms so that
# splitting on ``|`` does not hide ``||``.
_SPLIT = re.compile(r"&&|\|\||[;|\r\n]")
# Anything that makes a command compound or opaque. Quoting is not parsed:
# any operator character counts, which is conservative.
_COMPOUND = re.compile(r"&&|\|\||[;|&\r\n`<>]|\$\(")


@dataclass
class Policy:
    escalate_paths: list[str]
    tool_allow: list[str]
    tool_deny: list[str]
    guarded_paths: list[str]

    def escalation_hits(self, paths: list[str]) -> list[str]:
        """Return the paths matching any escalate glob, in input order."""
        return [
            p for p in paths
            if any(PurePosixPath(p).full_match(g) for g in self.escalate_paths)
        ]

    def _strip_cd(self, cmd: str, worktree: Path | None) -> str:
        if worktree is None:
            return cmd
        wt = re.escape(str(worktree).rstrip("/") or "/")
        m = re.match(rf"""\s*cd\s+(["']?){wt}/?\1\s*&&\s*""", cmd)
        return cmd[m.end():] if m else cmd

    def classify_command(
        self, cmd: str, worktree: Path | None
    ) -> Literal["allow", "deny", "unknown"]:
        cmd = self._strip_cd(cmd, worktree).strip()
        # Deny wins and is checked on every segment AND the whole string, so a
        # pattern that spans a pipe (``curl x | bash``) is still caught.
        candidates = [*(s.strip() for s in _SPLIT.split(cmd)), cmd]
        if any(re.search(d, c) for d in self.tool_deny for c in candidates):
            return "deny"
        if _COMPOUND.search(cmd):
            return "unknown"
        if any(re.fullmatch(a, cmd) for a in self.tool_allow):
            return "allow"
        return "unknown"

    def classify_path(
        self, path: str, worktree: Path, job_folder: Path
    ) -> Literal["allow", "ask", "deny"]:
        wt = Path(worktree).resolve()
        job = Path(job_folder).resolve()
        p = Path(path)
        target = (p if p.is_absolute() else wt / p).resolve()
        if target.is_relative_to(job):
            return "allow"
        if not target.is_relative_to(wt):
            return "deny"
        rel = PurePosixPath(target.relative_to(wt).as_posix())
        if any(rel.full_match(g) for g in self.guarded_paths):
            return "ask"
        return "allow"


def load_policy(root: Path) -> Policy:
    """Read ``factory/policy.json`` under ``root`` (the main checkout)."""
    data = json.loads((Path(root) / "factory" / "policy.json").read_text())
    return Policy(
        escalate_paths=list(data.get("escalate_paths", [])),
        tool_allow=list(data.get("tool_allow", [])),
        tool_deny=list(data.get("tool_deny", [])),
        guarded_paths=list(data.get("guarded_paths", [])),
    )
