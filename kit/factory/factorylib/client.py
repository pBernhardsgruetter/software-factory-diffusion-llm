"""HTTP transport for Mercury Decide (OpenRouter ``/systemone``).

Wire format, per the S1 spike: the boolean question type is ``"noul"`` on the
wire (``"boolean"`` is rejected with HTTP 400) and a boolean answer carries
P(true) under the key ``noul``. Everything inside the factory uses the internal
type name ``"boolean"``; the mapping lives in ``to_wire_questions`` and
``_normalise_answers`` and nowhere else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

REFERER = "https://github.com/pBernhardsgruetter/software-factory-diffusion-llm"
TITLE = "mercury-factory"

# Internal question type -> wire question type.
_WIRE_TYPE = {"boolean": "noul"}
# Template keys sent on the wire; factory-only keys (``risk``) are dropped.
_WIRE_FIELDS = ("instructions", "criteria")


def _boolean_instructions(q: dict) -> str:
    """Boolean ``criteria`` is not sent (wire shape unverified); fold it into the instructions."""
    text = q.get("instructions", "")
    crit = q.get("criteria")
    if isinstance(crit, dict):
        if crit.get("true"):
            text += f" Answer true if: {crit['true']}."
        if crit.get("false"):
            text += f" Answer false if: {crit['false']}."
    return text


class DecideError(Exception):
    """Decide call failed in a way the caller must handle (never contains the key)."""


@dataclass
class RawResult:
    model: str
    answers: dict
    usage: dict
    latency_ms: int


def to_wire_questions(questions: dict) -> dict:
    """Map internal question templates to the wire format (boolean -> noul)."""
    wire = {}
    for name, q in questions.items():
        qtype = q["type"]
        out = {"type": _WIRE_TYPE.get(qtype, qtype)}
        if qtype == "boolean":
            if "instructions" in q or "criteria" in q:
                out["instructions"] = _boolean_instructions(q)
            wire[name] = out
            continue
        for field in _WIRE_FIELDS:
            if field in q:
                out[field] = q[field]
        wire[name] = out
    return wire


def _normalise_answers(answers: dict) -> dict:
    """Map wire answers back: ``noul`` -> ``{"type": "boolean", "probability": p}``."""
    out = {}
    for name, a in answers.items():
        if isinstance(a, dict) and a.get("type") == "noul":
            out[name] = {"type": "boolean", "probability": a.get("noul")}
        else:
            out[name] = a
    return out


def _error_detail(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    except Exception:
        pass
    return (resp.text or "")[:200]


def post_decision(
    state,
    questions: dict,
    *,
    cfg: dict,
    api_key: str | None,
    http: httpx.Client | None = None,
) -> RawResult:
    """POST one decision request, trying ``cfg["decide"]["models"]`` in order.

    429, 5xx, timeouts, connection errors and unusable 200 bodies fall straight
    through to the next model (no same-model retry). Any other 4xx raises
    ``DecideError`` immediately. If every model fails, the error lists each attempt.
    """
    d = cfg["decide"]
    if not api_key:
        raise DecideError(f"{d.get('api_key_env', 'OPENROUTER_API_KEY')} not set")
    if http is None:
        with httpx.Client(timeout=d["timeout_s"]) as client:
            return _post(state, questions, d, api_key, client)
    return _post(state, questions, d, api_key, http)


def _post(state, questions, d, api_key, http) -> RawResult:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": REFERER,
        "X-Title": TITLE,
    }
    wire_questions = to_wire_questions(questions)
    attempts: list[str] = []
    for model in d["models"]:
        body = {"model": model, "state": state, "questions": wire_questions}
        t0 = time.monotonic()
        try:
            resp = http.post(d["endpoint"], json=body, headers=headers, timeout=d["timeout_s"])
        except httpx.TransportError as exc:  # timeout, connect error, ...
            attempts.append(f"{model}: {type(exc).__name__}")
            continue
        latency_ms = int((time.monotonic() - t0) * 1000)
        status = resp.status_code
        if status == 429 or status >= 500:
            attempts.append(f"{model}: HTTP {status}")
            continue
        if status != 200:
            raise DecideError(f"{model}: HTTP {status}: {_error_detail(resp)}")
        try:
            data = resp.json()
            answers = data["answers"]
            if not isinstance(answers, dict):
                raise TypeError("answers is not an object")
        except (ValueError, KeyError, TypeError) as exc:
            attempts.append(f"{model}: unusable 200 body ({type(exc).__name__})")
            continue
        usage = data.get("usage")
        return RawResult(
            model=model,
            answers=_normalise_answers(answers),
            usage=usage if isinstance(usage, dict) else {},
            latency_ms=latency_ms,
        )
    raise DecideError("all decide models failed: " + "; ".join(attempts))
