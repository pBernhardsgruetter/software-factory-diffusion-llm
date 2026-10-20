"""Turn raw Decide probabilities into per-question ``confident`` / uncertain bands."""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_BOOLEAN = 0.85
DEFAULT_RISK_BOOLEAN = 0.95
DEFAULT_CHOICE_SCORE = 0.75


@dataclass
class Banded:
    type: str
    value: bool | str | float | None
    p: float
    confident: bool


def _prob(x) -> float | None:
    """A finite number in [0, 1], else None (bools are not numbers here)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) and 0.0 <= x <= 1.0 else None


def _max_prob(probs, allowed=None):
    """Return ``(argmax_key, max_p)`` or None if ``probs`` is unusable."""
    if not isinstance(probs, dict) or not probs:
        return None
    best = None
    for key, raw in probs.items():
        p = _prob(raw)
        if p is None or (allowed is not None and key not in allowed):
            return None
        if best is None or p > best[1]:
            best = (key, p)
    return best


def _band_one(q: dict, a, thresholds: dict, name: str) -> Banded:
    qtype = q.get("type")
    default = {
        "boolean": DEFAULT_RISK_BOOLEAN if q.get("risk") else DEFAULT_BOOLEAN,
    }.get(qtype, DEFAULT_CHOICE_SCORE)
    t = thresholds.get(name, default)
    if not isinstance(a, dict) or a.get("type") != qtype:
        return Banded(qtype, None, 0.0, False)

    if qtype == "boolean":
        p = _prob(a.get("probability"))
        if p is None:
            return Banded(qtype, None, 0.0, False)
        return Banded(qtype, p >= 0.5, p, p >= t or p <= 1 - t)

    if qtype == "choice":
        criteria = q.get("criteria")
        allowed = criteria if isinstance(criteria, dict) else None
        best = _max_prob(a.get("probabilities"), allowed)
        if best is None:
            return Banded(qtype, None, 0.0, False)
        return Banded(qtype, best[0], best[1], best[1] >= t)

    if qtype == "score":
        score = a.get("score")
        best = _max_prob(a.get("probabilities"))
        if (best is None or isinstance(score, bool)
                or not isinstance(score, (int, float)) or not math.isfinite(score)):
            return Banded(qtype, None, 0.0, False)
        return Banded(qtype, float(score), best[1], best[1] >= t)

    return Banded(str(qtype), None, 0.0, False)


def band_answers(questions: dict, answers: dict, thresholds: dict[str, float]) -> dict[str, Banded]:
    """Band each question's answer; never raises on bad or missing answers.

    ``answers`` is the *normalised* shape produced by ``client.post_decision``:

    - boolean: ``{"type": "boolean", "probability": P(true)}``
    - choice:  ``{"type": "choice", "choice": key, "probabilities": {key: p}, "confidence": c}``
    - score:   ``{"type": "score", "score": float, "probabilities": {"0": p, ...}, "confidence": c}``

    Boolean: ``value = p >= 0.5``, confident if ``p >= t`` or ``p <= 1 - t`` (default
    t 0.85, risk questions 0.95). Choice: value is the argmax key, ``p`` its probability.
    Score: value is the returned ``score``, ``p`` the max rung probability. Choice and
    score are confident if ``p >= t`` (default 0.75). ``thresholds`` maps question name
    to an override. The API's own ``confidence`` field is ignored. A missing, malformed,
    wrong-typed or unknown-key answer gives ``Banded(type, None, 0.0, False)``.
    """
    answers = answers if isinstance(answers, dict) else {}
    thresholds = thresholds or {}
    out = {}
    for name, q in questions.items():
        try:
            out[name] = _band_one(q, answers.get(name), thresholds, name)
        except Exception:  # never let a weird answer break routing
            out[name] = Banded(q.get("type", "unknown") if isinstance(q, dict) else "unknown",
                               None, 0.0, False)
    return out
