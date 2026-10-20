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
from .redact import cap_text, redact_obj

REVIEWERS = ("security", "ux", "ui", "code")
OUTLINE_LINES = 400
GIT_TIMEOUT_S = 120
GIT_DIFF_FAILED = "<git diff failed>"
# State keys holding bulk text; the configured cap is split evenly across them.
DEFAULT_STATE_CAP = 200_000
ITEM_CAP_DIVISOR = 50  # one list item (finding, assumption, ...) gets at most cap // 50 chars
# Top-level lists whose string items (or dict items' "text") are capped one by one.
ITEM_LISTS = ("assumptions", "criteria", "observations", "findings", "previous", "current")
# Top-level string lists trimmed by size, with the noun used in the "…[N more X]" marker.
SIZED_LISTS = {"changed_paths": "paths", "rule_hits": "hits"}
LARGE_FIELDS = ("spec", "diff", "diff_stat", "repo_outline", "scout", "feature", "output_tail")

_BULLET = re.compile(r"^(?:[-*]|\d+\.)[ \t]+(.*)$")
_HEADING = re.compile(r"^#{1,2}[ \t]")
_VERDICT = re.compile(r"^VERDICT:[ \t]*(PASS|CHANGES)[ \t]*$", re.IGNORECASE)
_SHA = re.compile(r"[0-9a-fA-F]{4,64}")
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


def _delta_stat(job: Job, name: str) -> str | None:
    """Stat of changes since the reviewer's recorded commit, else the whole branch.

    ``None`` when git failed even for the whole-branch fallback (unknown delta).
    """
    recorded = (job.raw.get("review_commits") or {}).get(name)
    if isinstance(recorded, str) and _SHA.fullmatch(recorded):
        out = _git(job, *_diff_args(), "--stat", f"{recorded}..HEAD")
        if out is not None:
            return out.strip()
    # Never ran, no usable recorded commit, or unknown sha: fail toward more review.
    out = _git(job, *_diff_args(), "--stat", "main...HEAD")
    return None if out is None else out.strip()


# ----------------------------------------------------------------- builders


def _triage(root, job, extra):
    out = _git(job, "ls-files") or ""
    return {"feature": job.feature, "repo_outline": "\n".join(out.splitlines()[:OUTLINE_LINES])}


def _clarify(root, job, extra):
    return {"feature": job.feature, "scout": _read(job.folder / "scout.md")}


def _assumptions(root, job, extra):
    return {"assumptions": parse_assumptions(_read(job.folder / "spec.md"))}


def _rereview(root, job, extra):
    reviewers = []
    for n in REVIEWERS:
        delta = _delta_stat(job, n)
        if delta is None:
            # Delta unknown: "" would read as "nothing changed" and let a PASS
            # reviewer be skipped, so force a re-review.
            reviewers.append({"name": n, "last_verdict": "UNKNOWN", "delta_stat": GIT_DIFF_FAILED})
        else:
            reviewers.append({"name": n, "last_verdict": _last_verdict(job, n), "delta_stat": delta})
    return {"reviewers": reviewers}


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

_MARK = re.compile(r"…\[(\d+) more (\w+)\]")
_MIN_SHRINKABLE = 64


def _json_len(obj) -> int:
    return len(json.dumps(obj))


def _cap(s: str, n: int) -> str:
    """``cap_text`` that never makes a short string longer (its marker has a fixed size)."""
    r = cap_text(s, n)
    return r if len(r) < len(s) else s


def _marker_match(item):
    text = item if isinstance(item, str) else item.get("text") if isinstance(item, dict) else None
    return _MARK.fullmatch(text) if isinstance(text, str) else None


def _trim_list(items: list, keep: int, word: str) -> list:
    """First ``keep`` real items plus a final ``…[N more <word>]`` marker item."""
    items = list(items)
    prior = 0
    if items and (m := _marker_match(items[-1])):
        prior, word = int(m.group(1)), m.group(2)
        items.pop()
    text = f"…[{prior + len(items) - keep} more {word}]"
    marker = {"reviewer": "", "text": text} if items and isinstance(items[0], dict) else text
    return items[:keep] + [marker]


def _fit_list(items: list, budget: int, word: str) -> list:
    """Longest prefix of ``items`` whose JSON size fits ``budget``, plus a marker."""
    used = 2
    for i, item in enumerate(items):
        used += _json_len(item) + 2
        if used > budget:
            return _trim_list(items, i, word)
    return items


def _cap_items(items: list, item_cap: int) -> list:
    out = []
    for it in items:
        if isinstance(it, str):
            it = _cap(it, item_cap)
        elif isinstance(it, dict) and isinstance(it.get("text"), str):
            it = {**it, "text": _cap(it["text"], item_cap)}
        out.append(it)
    return out


def _shrink(state: dict, budget: int, cap: int) -> dict:
    out = dict(state)
    for k in LARGE_FIELDS:
        if isinstance(state.get(k), str):
            out[k] = _cap(state[k], budget)
    item_cap = min(budget, max(cap // ITEM_CAP_DIVISOR, 200))
    for k in ITEM_LISTS:
        if isinstance(state.get(k), list):
            out[k] = _cap_items(state[k], item_cap)
    for k, word in SIZED_LISTS.items():
        if isinstance(state.get(k), list):
            out[k] = _fit_list(state[k], budget, word)
    if isinstance(state.get("reviewers"), list):
        out["reviewers"] = [
            {**r, "delta_stat": _cap(r["delta_stat"], budget)}
            if isinstance(r, dict) and isinstance(r.get("delta_stat"), str) else r
            for r in state["reviewers"]
        ]
    return out


def _force_fit(state: dict, cap: int) -> dict:
    """Last resort: repeatedly halve the largest top-level value until it fits."""
    out = dict(state)
    for _ in range(1000):
        if _json_len(out) <= cap:
            break
        key = max(out, key=lambda k: _json_len(out[k]))
        v = out[key]
        if _json_len(v) <= _MIN_SHRINKABLE:
            break  # nothing left to shrink (the cap itself is degenerate)
        if key == "reviewers" and isinstance(v, list):
            # Never drop a reviewer (a missing one is never re-reviewed): shorten the stats.
            out[key] = [
                {**r, "delta_stat": _cap(r["delta_stat"], len(r["delta_stat"]) // 2)}
                if isinstance(r, dict) and isinstance(r.get("delta_stat"), str) else r
                for r in v
            ]
        elif isinstance(v, str):
            out[key] = _cap(v, len(v) // 2)
        elif isinstance(v, list):
            real = len(v) - (1 if v and _marker_match(v[-1]) else 0)
            out[key] = _trim_list(v, real // 2, SIZED_LISTS.get(key, "items"))
        else:
            out[key] = f"…[removed {_json_len(v)} chars]"
    return out


def _apply_caps(state: dict, cap: int) -> dict:
    """Make ``len(json.dumps(state)) <= cap``: cap text fields, list items and lists.

    The cap is split evenly across the large text fields (top-level ``LARGE_FIELDS``
    strings and each reviewer's ``delta_stat``). JSON escaping and the other fields
    add to the size, so the per-field budget shrinks until the state fits; whatever
    is still over is then trimmed largest-first by ``_force_fit``.
    """
    n = sum(isinstance(state.get(k), str) for k in LARGE_FIELDS)
    if isinstance(state.get("reviewers"), list):
        n += sum(isinstance(r, dict) and isinstance(r.get("delta_stat"), str)
                 for r in state["reviewers"])
    budget = max(cap // max(n, 1), 1)
    out = state
    for _ in range(12):
        out = _shrink(state, budget, cap)
        size = _json_len(out)
        if size <= cap or budget <= 1:
            break
        budget = max(int(budget * cap / size * 0.97) - 40, 1)
    return out if _json_len(out) <= cap else _force_fit(out, cap)


def _load_cfg(root: Path) -> dict:
    try:
        cfg = load_config(root)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _redact(state: dict, patterns) -> dict:
    try:
        return redact_obj(state, patterns)[0]
    except ValueError:  # a bad configured pattern: built-in redaction only
        return redact_obj(state, [])[0]


def _union(built: list, extra) -> list:
    merged = list(built)
    seen = {repr(x) for x in merged}
    for x in extra if isinstance(extra, list) else []:
        if repr(x) not in seen:
            seen.add(repr(x))
            merged.append(x)
    return merged


def build_state(name: str, root: Path, job: Job, extra: dict) -> dict:
    """State for decision ``name``; ``extra`` is merged last and overrides it.

    Exception: for ``escalation`` ``extra`` may only ADD to ``rule_hits`` and
    ``changed_paths`` (de-duplicated union), never replace or clear them.
    The state is redacted first and capped after, so a secret cannot be cut
    into an unredactable fragment.
    """
    builder = _BUILDERS.get(name)
    if builder is None:
        raise ValueError(f"unknown decision {name}")
    extra = extra or {}
    built = builder(root, job, extra)
    state = {**built, **extra}
    if name == "escalation":
        for key in ("rule_hits", "changed_paths"):
            state[key] = _union(built[key], extra.get(key))
    cfg = _load_cfg(root)
    state = _redact(state, cfg.get("redact_patterns") or [])
    cap = int((cfg.get("decide") or {}).get("state_char_cap") or DEFAULT_STATE_CAP)
    return _apply_caps(state, cap)
