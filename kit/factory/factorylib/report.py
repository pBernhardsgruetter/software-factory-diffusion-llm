"""``decide report``: are the shadow-mode decisions calibrated enough to go active?

Reads every logged decision record (``factory/jobs/*/decisions.jsonl`` plus
``factory/tool-gate.jsonl``, whose ``kind: gate`` audit lines are ignored) and the outcome records (ground truth recorded later
by the orchestrator) and computes, per decision name:

* ``cases``: decision records logged (error records included).
* ``paired``: outcomes paired with a decision (see below).
* ``coverage``: share of ``band == "confident"`` among the decisions that asked
  the model something (``band != "n/a"``; a record that never reached the model
  and carries no error is skipped too). Error records stay in the denominator but
  never in the confident numerator. ``None`` when nothing qualifies.
* ``agreement``: share of paired outcomes where every key of ``truth`` equals the
  same key of ``route`` (lists compare as sets). ``None`` when nothing is paired.
* ``unsafe_misses`` (``triage`` and ``escalation``): paired cases where the truth
  escalates and the route does not. 0 for the other decisions.
* ``unpaired_outcomes``: latest outcomes per (job, decision) that found no eligible
  preceding decision (none logged, or the latest one errored).
* ``pairing``: ``"supported"`` (``triage``, ``escalation``, ``stuck``, ``finding``,
  ``failure``), or ``"unsupported"`` for the decisions this factory records no
  (or only one-sided) ground truth for: ``assumptions``, ``clarify``, ``promote``,
  ``rereview``, ``toolgate``, ``visual``. An unsupported decision is never
  ``ready``; its ``cases``, ``coverage`` and any pairs still count. ``toolgate``
  additionally never pairs (its decisions live in ``tool-gate.jsonl`` with no
  job) and its ``unsafe_misses`` is ``None`` (unknown, not 0).
* ``ready``: ``pairing == "supported"`` and ``cases >= min_cases`` and
  ``paired >= min_cases`` (the target is cases *with outcomes*) and
  ``agreement >= 0.95`` and ``unsafe_misses == 0``. It measures agreement on the
  keys the outcomes record only: for ``triage`` that is ``risks`` (the approver's
  categories), never the tier or the reviewers (``note`` says so).

Pairing: per (job, decision) the LATEST outcome by ``ts`` wins (human overrides come
later) and is paired once with the latest decision of that name in that job that
does not come after it. If that decision has an ``error`` or no usable ``route``
the outcome stays unpaired (a failed call is not a model answer, and an older
answer is not what ran). Decisions with ``band == "n/a"`` (deterministic routes)
can be paired.

The returned dict maps decision name -> stats, plus ``"skipped_lines"``: the number
of unreadable or malformed lines, outcomes included whose ``ts`` is missing or
unparseable (never an exception).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

AGREEMENT_TARGET = (95, 100)  # compared in integers: 19/20 must pass, never 0.9499999
UNSAFE_DECISIONS = ("triage", "escalation")
UNPAIRABLE_DECISIONS = ("toolgate",)  # no outcome source exists (controller ruling R26)
# No (or only one-sided) outcome source in this factory: never ``ready`` (final review I8).
UNSUPPORTED_DECISIONS = ("assumptions", "clarify", "promote", "rereview", "toolgate", "visual")
NOTES = {"triage": "ready measures risks only"}
_ESCALATING_PERMISSIONS = ("ask", "deny")
_MISSING = object()


def _ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read(path: Path):
    """Yield (parsed object or None) per non-blank line; None marks a bad line.

    Tool-gate audit lines (``kind == "gate"``) are not decisions: they are left
    out entirely, never counted as skipped.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        yield None
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            yield None
            continue
        if isinstance(obj, dict) and obj.get("kind") == "gate":
            continue
        yield obj if isinstance(obj, dict) else None


def _equal(truth, route) -> bool:
    if isinstance(truth, list) and isinstance(route, list):
        try:
            return set(truth) == set(route)
        except TypeError:  # unhashable items: compare canonical JSON instead
            key = lambda xs: {json.dumps(x, sort_keys=True, default=str) for x in xs}  # noqa: E731
            return key(truth) == key(route)
    return truth == route


def _escalates(d: dict) -> bool:
    risks = d.get("risks")
    return bool(risks) or d.get("escalate") is True or d.get("permission") in _ESCALATING_PERMISSIONS


def _latest(items):
    """Latest by (ts, file order); ``items`` are (ts, idx, record)."""
    return max(items, key=lambda it: (it[0], it[1]), default=None)


def build_report(root: Path, min_cases: int = 20) -> dict[str, dict]:
    root = Path(root)
    skipped = 0
    # decision name -> {"decisions": [record], "paired": [(truth, route)]}
    decisions: dict[str, list[dict]] = {}
    # (source key, name) -> lists of (ts, idx, record) for pairing
    d_by_key: dict[tuple, list] = {}
    o_by_key: dict[tuple, list] = {}
    idx = 0

    files = [(p.parent.name, p) for p in sorted((root / "factory" / "jobs").glob("*/decisions.jsonl"))]
    files.append(("", root / "factory" / "tool-gate.jsonl"))
    for source, path in files:
        if not path.is_file():
            continue
        for obj in _read(path):
            idx += 1
            name = obj.get("decision") if obj else None
            kind = obj.get("kind") if obj else None
            if not isinstance(name, str) or kind not in ("decision", "outcome"):
                skipped += 1
                continue
            if kind == "outcome":
                if not isinstance(obj.get("truth"), dict) or not obj["truth"]:
                    skipped += 1
                    continue
                ts = _ts(obj.get("ts"))
                if ts is None:
                    skipped += 1
                else:
                    o_by_key.setdefault((source, name), []).append((ts, idx, obj))
                continue
            decisions.setdefault(name, []).append(obj)
            ts = _ts(obj.get("ts"))
            if ts is not None:
                d_by_key.setdefault((source, name), []).append((ts, idx, obj))

    pairs: dict[str, list[tuple[dict, dict]]] = {}
    unpaired: dict[str, int] = {}
    for key, outcomes in o_by_key.items():
        name = key[1]
        o_ts, _, outcome = _latest(outcomes)
        latest = _latest([d for d in d_by_key.get(key, []) if d[0] <= o_ts])
        dec = latest[2] if latest else None
        if (name in UNPAIRABLE_DECISIONS or dec is None or dec.get("error")
                or not isinstance(dec.get("route"), dict)):
            unpaired[name] = unpaired.get(name, 0) + 1
            continue
        pairs.setdefault(name, []).append((outcome["truth"], dec["route"]))

    report: dict = {}
    for name in sorted(set(decisions) | set(unpaired)):
        recs = decisions.get(name, [])
        denom = [r for r in recs if r.get("band") != "n/a"
                 and (r.get("model") is not None or r.get("error"))]
        confident = [r for r in denom if r.get("band") == "confident"
                     and not r.get("error") and r.get("model") is not None]
        named = pairs.get(name, [])
        agree = sum(all(_equal(v, route.get(k, _MISSING)) for k, v in truth.items())
                    for truth, route in named)
        unsafe = (sum(_escalates(truth) and not _escalates(route) for truth, route in named)
                  if name in UNSAFE_DECISIONS else 0)
        n_agree_ok = bool(named) and agree * AGREEMENT_TARGET[1] >= AGREEMENT_TARGET[0] * len(named)
        unsupported = name in UNSUPPORTED_DECISIONS
        report[name] = {
            "cases": len(recs),
            "paired": len(named),
            "unpaired_outcomes": unpaired.get(name, 0),
            "coverage": len(confident) / len(denom) if denom else None,
            "agreement": agree / len(named) if named else None,
            "unsafe_misses": None if name in UNPAIRABLE_DECISIONS else unsafe,
            "pairing": "unsupported" if unsupported else "supported",
            "ready": (not unsupported and len(recs) >= min_cases and len(named) >= min_cases
                      and n_agree_ok and unsafe == 0),
        }
        if name in NOTES:
            report[name]["note"] = NOTES[name]
    report["skipped_lines"] = skipped
    return report


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def format_report(report: dict, min_cases: int = 20) -> str:
    cols = ("decision", "cases", "paired", "unpaired", "coverage", "agreement", "unsafe", "ready")
    rows = [[n, str(r["cases"]), str(r["paired"]), str(r["unpaired_outcomes"]),
             _pct(r["coverage"]), _pct(r["agreement"]),
             "n/a" if r["unsafe_misses"] is None else str(r["unsafe_misses"]),
             "yes" if r["ready"] else "no"]
            for n, r in report.items() if n != "skipped_lines"]
    widths = [max(len(c), *(len(row[i]) for row in rows)) if rows else len(c)
              for i, c in enumerate(cols)]
    fmt = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    lines = [fmt(cols), *(fmt(r) for r in rows)]
    if not rows:
        lines.append("(no decisions logged)")
    lines.append(f"ready needs: >= {min_cases} cases with outcomes, >= 95% agreement, "
                 "0 unsafe misses (triage, escalation)")
    lines.append("unsupported (no outcome source, stay shadow): "
                 + ", ".join(UNSUPPORTED_DECISIONS))
    lines.extend(f"{n}: {note}" for n, note in NOTES.items())
    lines.append(f"skipped_lines: {report.get('skipped_lines', 0)}")
    return "\n".join(lines)
