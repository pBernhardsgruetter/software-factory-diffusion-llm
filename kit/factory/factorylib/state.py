"""Build the ``state`` dict for each Mercury Decide decision from job files and git.

``root`` is the MAIN checkout; the job folder is ``root/factory/jobs/<id>/`` and
git commands run in ``job.worktree``. Git failures never raise: a failed read
yields ``""`` (and for escalation fails closed, see ``_escalation``).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .config import load_config
from .paths import Job
from .policy import load_policy
from .redact import cap_text

REVIEWERS = ("security", "ux", "ui", "code")
OUTLINE_LINES = 400
GIT_TIMEOUT_S = 120
GIT_DIFF_FAILED = "<git diff failed>"
# State keys holding bulk text; the configured cap is split evenly across them.
LARGE_FIELDS = ("spec", "diff", "diff_stat", "repo_outline", "scout", "feature", "output_tail")

_BULLET = re.compile(r"^(?:[-*]|\d+\.)[ \t]+(.*)$")
_HEADING = re.compile(r"^#{1,2}[ \t]")
_VERDICT = re.compile(r"^VERDICT:[ \t]*(PASS|CHANGES)[ \t]*$", re.IGNORECASE)
_SHA = re.compile(r"^[0-9a-fA-F]{4,64}$")
_ROUND_DIR = re.compile(r"^round-(\d+)$")


# ----------------------------------------------------------------- markdown


def _section_items(md: str, heading: str) -> list[str]:
    """Bullet items directly under ``## <heading>``, up to the next ``#``/``##`` heading.

    A bullet is ``- ``, ``* `` or ``1. ``; an indented non-bullet line continues
    the open bullet (a blank line is allowed before it), an unindented one ends it.
    A section with no bullets keeps its prose as one item rather than losing it.
    ``None.`` (bare or as a bullet) means no items.
    """
    want = heading.casefold()
    body: list[str] | None = None
    for line in md.splitlines():
        if body is None:
            m = re.match(r"^##[ \t]+(.*?)[ \t]*$", line)
            if m and m.group(1).casefold() == want:
                body = []
        elif _HEADING.match(line):
            break
        else:
            body.append(line)
    if not body:
        return []

    items: list[list[str]] = []
    open_item = False
    prose: list[str] = []
    for line in body:
        m = _BULLET.match(line)
        if m:
            items.append([m.group(1).strip()])
            open_item = True
        elif not line.strip():
            continue
        elif open_item and line[0] in " \t":
            items[-1].append(line.strip())
        else:
            open_item = False
            prose.append(line.strip())
    texts = [" ".join(p for p in it if p) for it in items]
    if not texts and prose:
        texts = [" ".join(prose)]
    return [t for t in texts if t and t.rstrip(".").strip().casefold() != "none"]


def parse_findings(review_md: str) -> list[str]:
    """Bullet items under ``## Findings`` (``[]`` if missing or ``None.``)."""
    return _section_items(review_md, "Findings")


def parse_assumptions(spec_md: str) -> list[str]:
    """Bullet items under ``## Assumptions`` (``[]`` if missing or ``None.``)."""
    return _section_items(spec_md, "Assumptions")


def _criteria(spec_md: str) -> list[str]:
    return _section_items(spec_md, "Acceptance criteria")


# -------------------------------------------------------------------- files


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _rounds(job: Job) -> list[int]:
    try:
        names = [p.name for p in job.folder.iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted(int(m.group(1)) for n in names if (m := _ROUND_DIR.match(n)))


def _round(job: Job, extra: dict) -> int:
    """``extra["round"]`` when it is an int, else the latest round on disk, else 1."""
    r = extra.get("round")
    if isinstance(r, int) and not isinstance(r, bool) and r >= 0:
        return r
    rounds = _rounds(job)
    return rounds[-1] if rounds else 1


def _review_path(job: Job, rnd: int, name: str) -> Path:
    return job.folder / f"round-{rnd}" / f"review-{name}.md"


def _verdict(review_md: str) -> str:
    first = review_md.lstrip("﻿").split("\n", 1)[0].strip()
    m = _VERDICT.match(first)
    return m.group(1).upper() if m else "UNKNOWN"


def _last_verdict(job: Job, name: str) -> str | None:
    """Verdict of the reviewer's highest-numbered round file; None if it never ran."""
    for rnd in reversed(_rounds(job)):
        path = _review_path(job, rnd, name)
        if path.is_file():
            return _verdict(_read(path))
    return None


def _round_reviews(job: Job, rnd: int) -> dict[str, str]:
    """``{reviewer: review text}`` for reviewers with a file in round ``rnd``."""
    out = {}
    for name in REVIEWERS:
        path = _review_path(job, rnd, name)
        if path.is_file():
            out[name] = _read(path)
    return out


def _round_findings(job: Job, rnd: int) -> list[dict]:
    """Findings of reviews in ``rnd`` that did not pass (CHANGES, or unparseable)."""
    out = []
    for name, text in _round_reviews(job, rnd).items():
        if _verdict(text) == "PASS":
            continue
        out += [{"reviewer": name, "text": f} for f in parse_findings(text)]
    return out


# ---------------------------------------------------------------------- git


def _git(job: Job, *args: str) -> str | None:
    """Run git in the worktree; stdout on success, ``None`` on any failure."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=job.worktree, capture_output=True, text=True,
            errors="replace", timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _diff_args() -> tuple[str, ...]:
    return ("diff", "--no-ext-diff", "--no-textconv", "--no-color")


def _delta_stat(job: Job, name: str) -> str:
    """Stat of changes since the reviewer's recorded commit, else the whole branch."""
    recorded = (job.raw.get("review_commits") or {}).get(name)
    if isinstance(recorded, str) and _SHA.match(recorded):
        out = _git(job, *_diff_args(), "--stat", f"{recorded}..HEAD")
        if out is not None:
            return out.strip()
    # Never ran, no usable recorded commit, or unknown sha: fail toward more review.
    return (_git(job, *_diff_args(), "--stat", "main...HEAD") or "").strip()


# ----------------------------------------------------------------- builders


def _triage(root, job, extra):
    out = _git(job, "ls-files") or ""
    return {"feature": job.feature, "repo_outline": "\n".join(out.splitlines()[:OUTLINE_LINES])}


def _clarify(root, job, extra):
    return {"feature": job.feature, "scout": _read(job.folder / "scout.md")}


def _assumptions(root, job, extra):
    return {"assumptions": parse_assumptions(_read(job.folder / "spec.md"))}


def _rereview(root, job, extra):
    return {"reviewers": [
        {"name": n, "last_verdict": _last_verdict(job, n), "delta_stat": _delta_stat(job, n)}
        for n in REVIEWERS
    ]}


def _finding(root, job, extra):
    return {"findings": _round_findings(job, _round(job, extra))}


def _promote(root, job, extra):
    rnd = _round(job, extra)
    return {
        "round": rnd,
        "verdicts": {n: _verdict(t) for n, t in _round_reviews(job, rnd).items()},
        "findings": _round_findings(job, rnd),
    }


def _stuck(root, job, extra):
    rnd = _round(job, extra)
    return {
        "previous": _round_findings(job, rnd - 1) if rnd > 1 else [],
        "current": _round_findings(job, rnd),
    }


def _escalation(root, job, extra):
    names = _git(job, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                 "--name-only", "-z", "main...HEAD")
    if names is None:
        # Cannot see what changed: fail closed so a human looks.
        paths, hits = [], [GIT_DIFF_FAILED]
    else:
        paths = [p for p in names.split("\0") if p]
        hits = load_policy(root).escalation_hits(paths)
    return {
        "spec": _read(job.folder / "spec.md"),
        "diff_stat": (_git(job, *_diff_args(), "--stat", "main...HEAD") or "").strip(),
        "changed_paths": paths,
        "diff": _git(job, *_diff_args(), "main...HEAD") or "",
        "rule_hits": hits,
    }


def _visual(root, job, extra):
    rnd = _round(job, extra)
    try:
        obs = json.loads(_read(job.folder / f"round-{rnd}" / "observations.json"))
    except ValueError:
        obs = []
    return {
        "criteria": _criteria(_read(job.folder / "spec.md")),
        "observations": obs if isinstance(obs, list) else [],
    }


_BUILDERS = {
    "triage": _triage,
    "clarify": _clarify,
    "assumptions": _assumptions,
    "failure": lambda root, job, extra: {},
    "rereview": _rereview,
    "finding": _finding,
    "promote": _promote,
    "stuck": _stuck,
    "escalation": _escalation,
    "toolgate": lambda root, job, extra: {},  # built by the hook; extra only
    "visual": _visual,
}


# --------------------------------------------------------------------- caps


def _json_len(state: dict) -> int:
    return len(json.dumps(state))


def _apply_caps(state: dict, cap: int) -> dict:
    """Cap the large text fields, splitting ``cap`` evenly, so the JSON fits ``cap``.

    JSON escaping (newlines, quotes) and the non-text fields add to the size, so
    the per-field budget is shrunk until the serialised state fits.
    """
    large = [k for k in LARGE_FIELDS if isinstance(state.get(k), str)]
    if not large:
        return state
    budget = max(cap // len(large), 1)
    out = state
    for _ in range(12):
        out = {**state, **{k: cap_text(state[k], budget) for k in large}}
        size = _json_len(out)
        if size <= cap or budget <= 1:
            break
        budget = max(int(budget * cap / size * 0.97) - 40, 1)
    return out


def build_state(name: str, root: Path, job: Job, extra: dict) -> dict:
    """State for decision ``name``; ``extra`` is merged last and overrides it."""
    builder = _BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"unknown decision {name}")
    extra = extra or {}
    state = {**builder(root, job, extra), **extra}
    return _apply_caps(state, int(load_config(root)["decide"]["state_char_cap"]))
