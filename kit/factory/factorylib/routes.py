"""Pure routing: banded Decide answers + job state -> action dict.

Principle: System 1 may only speed things up when it is confident, or add an
escalation. It never overrides the heavy models. A missing or uncertain answer
always routes to the conservative / default path. Routes never raise on missing
keys and always return a fresh dict.
"""

from __future__ import annotations

import re
from typing import Callable

from .banding import Banded

RouteFn = Callable[[dict[str, Banded], dict], dict]

REVIEWERS = ["security", "ux", "ui", "code"]
RISKS = ["auth", "payments", "dependencies", "ci_deploy", "schema_migration",
         "email", "prod_data", "build_scripts"]


def _get(banded: dict, name: str, qtype: str = "boolean") -> Banded:
    b = (banded or {}).get(name)
    return b if isinstance(b, Banded) else Banded(qtype, None, 0.0, False)


def _is_true(b: Banded) -> bool:
    return b.confident and b.value is True


def _is_false(b: Banded) -> bool:
    return b.confident and b.value is False


def _flagged(b: Banded) -> bool:
    """Risk-style flag: True, or not confident enough to rule it out."""
    return b.value is True or not b.confident


def _count(banded: dict, state: dict, state_key: str, prefix: str) -> int:
    """Number of per-item questions: the state list length, or more if banded has more."""
    items = (state or {}).get(state_key)
    n = len(items) if isinstance(items, list) else 0
    pat = re.compile(re.escape(prefix) + r"(\d+)$")
    for key in banded or {}:
        m = pat.match(key) if isinstance(key, str) else None
        if m:
            n = max(n, int(m.group(1)) + 1)
    return n


def _risks(banded: dict) -> list[str]:
    return [r for r in RISKS if _flagged(_get(banded, r))]


def triage(banded, state):
    c = _get(banded, "complexity", "score")
    risks = _risks(banded)
    fast = (c.confident and isinstance(c.value, (int, float)) and c.value <= 1.0
            and not risks)
    ui = _get(banded, "ui_change")
    reviewers = ["security"] + (["ux", "ui"] if _flagged(ui) else []) + ["code"]
    return {"tier": "fast" if fast else "heavy", "reviewers": reviewers, "risks": risks}


def clarify(banded, state):
    return {"ask_human": _flagged(_get(banded, "needs_human_clarification"))}


def assumptions(banded, state):
    n = _count(banded, state, "assumptions", "a")
    return {"ask_human": [i for i in range(n) if _flagged(_get(banded, f"a{i}"))]}


_FAILURE = {"flaky": "retry", "env": "blocked", "spec_conflict": "blocked", "code": "fix"}


def failure(banded, state):
    c = _get(banded, "cause", "choice")
    return {"action": _FAILURE.get(c.value, "fix") if c.confident else "fix"}


def rereview(banded, state):
    """Fail closed: a reviewer is skipped only if it previously passed (or never ran)
    AND area_touched is confidently False. Missing reviewer state re-runs all four."""
    items = (state or {}).get("reviewers")
    if not isinstance(items, list) or not items:
        return {"reviewers": list(REVIEWERS)}
    out = []
    for i, item in enumerate(items):
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str):
            continue
        v = item.get("last_verdict")
        verdict = None if v is None else str(v).strip().upper()
        clean = verdict is None or verdict in ("PASS", "SKIPPED")
        if not (clean and _is_false(_get(banded, f"r{i}"))):
            out.append(name)
    return {"reviewers": out}


def finding(banded, state):
    n = _count(banded, state, "findings", "f")
    fast = [i for i in range(n) if _is_true(_get(banded, f"f{i}"))]
    fast_set = set(fast)
    return {"fast_fixer": fast, "builder": [i for i in range(n) if i not in fast_set]}


def promote(banded, state):
    return {"promote": not _is_false(_get(banded, "beyond_fast_tier"))}


def stuck(banded, state):
    p = _get(banded, "progress", "score")
    return {"escalate": (not p.confident) or not isinstance(p.value, (int, float)) or p.value < 1.0}


def escalation(banded, state):
    hits = (state or {}).get("rule_hits")
    hits = [str(h) for h in hits] if isinstance(hits, list) else []
    risks = _risks(banded)
    return {"escalate": bool(hits) or bool(risks), "reasons": hits + risks}


_TOOLGATE = {"safe": "allow", "needs_human": "ask", "destructive": "deny"}


def toolgate(banded, state):
    c = _get(banded, "class", "choice")
    return {"permission": _TOOLGATE.get(c.value, "ask") if c.confident else "ask"}


def visual(banded, state):
    n = _count(banded, state, "criteria", "c")
    met, unmet, unsure = [], [], []
    for i in range(n):
        b = _get(banded, f"c{i}")
        (met if _is_true(b) else unmet if _is_false(b) else unsure).append(i)
    return {"met": met, "unmet": unmet, "unsure": unsure}


ROUTES: dict[str, RouteFn] = {
    "triage": triage,
    "clarify": clarify,
    "assumptions": assumptions,
    "failure": failure,
    "rereview": rereview,
    "finding": finding,
    "promote": promote,
    "stuck": stuck,
    "escalation": escalation,
    "toolgate": toolgate,
    "visual": visual,
}
