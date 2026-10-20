"""Run one Mercury Decide decision end to end and log it.

``run_decision`` is the System-1 core: template -> redacted, capped state ->
Decide -> banding -> route -> ``apply``. System 1 may only speed things up when
it is confident, or add an escalation; it never overrides the heavy models. In
shadow mode (any mode other than exactly ``"active"``) and on any error,
``apply`` is the template fallback, i.e. the original factory behaviour. The
route is still computed and logged so shadow runs can be calibrated. For
``escalation`` the logged route ignores the deterministic rule hits (it is
Decide's own opinion); ``apply`` is still forced to escalate by them.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .banding import band_answers
from .client import DecideError, post_decision
from .config import decision_config, load_config
from .paths import Job
from .redact import REDACTED, cap_text, redact_obj
from .routes import ROUTES
from .templates import expand_questions, load_template, resolve_fallback

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp", ".gif": "gif"}
_MIN_CAP_LEAF = 64  # strings this short are never capped (the marker would not shrink them)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def append_jsonl(path: Path, record: dict) -> None:
    """Append ``record`` as one line with a single ``os.write`` on an O_APPEND fd.

    One write per record keeps concurrent writers from interleaving partial lines.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        n = os.write(fd, data)
    finally:
        os.close(fd)
    if n != len(data):
        raise OSError(f"{path}: short write ({n} of {len(data)} bytes)")


# ------------------------------------------------------------------- images


def image_part(path: Path) -> dict:
    """An ``image_url`` content part (base64 data URL) for one image file.

    Raises ``ValueError`` for an unsupported extension, a missing file or a file
    over 5 MB.
    """
    p = Path(path)
    kind = _IMAGE_TYPES.get(p.suffix.lower())
    if kind is None:
        raise ValueError(f"{p}: unsupported image type {p.suffix or '(none)'!r}; "
                         "use .png, .jpg/.jpeg, .webp or .gif")
    if not p.is_file():
        raise ValueError(f"{p}: image not found")
    if p.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"{p}: image is larger than 5 MB")
    data = p.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:  # grew since stat
        raise ValueError(f"{p}: image is larger than 5 MB")
    url = f"data:image/{kind};base64,{base64.b64encode(data).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": url}}


# -------------------------------------------------------------------- state


def _json_default(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=repr)
    return str(obj)


def _string_slots(obj, out: list) -> list:
    """``(container, key)`` for every string leaf of a JSON-shaped object."""
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
    for k, v in items:
        if isinstance(v, str):
            out.append((obj, k))
        else:
            _string_slots(v, out)
    return out


def cap_state(state, cap: int):
    """Shrink the largest string leaves (in place) until ``json.dumps(state)`` fits ``cap``.

    Strings of ``_MIN_CAP_LEAF`` chars or fewer are left alone, so a state made of
    many short strings may stay over the cap.
    """
    while True:
        size = len(json.dumps(state))
        if size <= cap:
            return state
        slots = [(c, k) for c, k in _string_slots(state, []) if len(c[k]) > _MIN_CAP_LEAF]
        if not slots:
            return state
        c, k = max(slots, key=lambda ck: len(json.dumps(ck[0][ck[1]])))
        s = c[k]
        enc = len(json.dumps(s))  # escaped length: \n, quotes and non-ASCII cost more
        keep = int(len(s) * (enc - (size - cap)) / enc) - _MIN_CAP_LEAF
        # Always strictly shrink: the marker is shorter than _MIN_CAP_LEAF.
        c[k] = cap_text(s, max(0, min(keep, len(s) - _MIN_CAP_LEAF)))


def prepare_state(state: dict, cfg: dict) -> tuple[dict, int]:
    """Redact FIRST, then cap, so a cap never splits a secret into an unredactable piece."""
    clean, n = redact_obj(state, cfg.get("redact_patterns") or [])
    clean = json.loads(json.dumps(clean, default=_json_default))  # plain JSON types, fresh copy
    return cap_state(clean, int(cfg["decide"]["state_char_cap"])), n


# --------------------------------------------------------------- escalation


def coerce_rule_hits(hits) -> list[str]:
    """``rule_hits`` as a list of strings: str -> [str], list/tuple/set -> list, falsy -> []."""
    if not hits:
        return []
    if isinstance(hits, str):
        return [hits]
    if isinstance(hits, (set, frozenset)):
        return sorted(str(h) for h in hits)
    if isinstance(hits, (list, tuple)):
        return [str(h) for h in hits]
    return [str(hits)]  # fail closed: anything else truthy still escalates


def _force_escalation(apply: dict, hits: list[str]) -> dict:
    """Deterministic rules win in every mode: escalate, rule hits first, no duplicates."""
    out = dict(apply)
    out["escalate"] = True
    reasons = list(dict.fromkeys(hits))
    others = apply.get("reasons")
    for r in others if isinstance(others, list) else []:
        if r not in reasons:
            reasons.append(r)
    out["reasons"] = reasons
    return out


STATE_BUILD_FAILED = "<state build failed>"


def _redact_hits(hits: list[str], cfg: dict) -> list[str]:
    """Rule hits go into logged ``reasons``: redact them like any state text."""
    try:
        return redact_obj(hits, cfg.get("redact_patterns") or [])[0]
    except Exception:  # a bad configured pattern: built-in patterns only
        return redact_obj(hits, [])[0]


# --------------------------------------------------------------------- main


def _log_path(root: Path, job: Job | None) -> Path:
    if job is None:
        return Path(root) / "factory" / "tool-gate.jsonl"
    return Path(job.folder) / "decisions.jsonl"


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _scrub(msg: str | None, cfg: dict) -> str | None:
    """Remove the API key's value from an error message, whatever raised it."""
    if not msg:
        return msg
    env = (cfg.get("decide") or {}).get("api_key_env") or "OPENROUTER_API_KEY"
    for key in {os.environ.get(env), os.environ.get("OPENROUTER_API_KEY")}:
        if key:
            msg = msg.replace(key, REDACTED)
    return msg


def _fallback(tpl: dict, state: dict, hits: list[str]) -> dict:
    try:
        apply = resolve_fallback(tpl, state)
    except Exception:
        apply = resolve_fallback(tpl, {})
    return _force_escalation(apply, hits) if hits else apply


def run_decision(
    name: str,
    *,
    root: Path,
    job: Job | None,
    state: dict,
    images: list[Path] = [],  # noqa: B006 (never mutated)
    post=None,
    state_error: str | None = None,
) -> dict:
    """Run decision ``name`` on ``state``, append the record to the log and return it.

    Raises ``ValueError`` only for caller mistakes and an unusable template: an
    unknown decision, a template that does not load, or a bad image (nothing is
    sent or logged then). Once the template has loaded, every other failure
    (config, state preparation, Decide, banding, route, log write) is caught and
    carried in ``record["error"]`` with ``band: "uncertain"`` and ``apply`` set to
    the fallback.

    ``state_error`` says the caller could not build the state (``state`` is then
    best effort): nothing is sent, and for ``escalation`` the fail-closed rule hit
    ``"<state build failed>"`` forces an escalation. ``post`` defaults to
    ``client.post_decision`` (looked up at call time).
    """
    if name not in ROUTES:
        raise ValueError(f"unknown decision {name!r}; known: {', '.join(ROUTES)}")
    root = Path(root)
    tpl = load_template(root, name)
    image_parts = [image_part(p) for p in images or []]
    post = post or post_decision
    state = state if isinstance(state, dict) else {}

    error: str | None = state_error
    cfg: dict = {}
    mode, thresholds = "shadow", {}
    try:
        cfg = load_config(root)
        dc = decision_config(cfg, name)
        mode = "active" if dc["mode"] == "active" else "shadow"
        thresholds = dc["thresholds"]
    except Exception as exc:  # unreadable config: shadow, fallback, no call
        cfg, error = {}, error or _err(exc)

    raw = None
    latency_ms = None
    redactions = 0
    clean = None
    sha = None
    if error is None:
        try:
            clean, redactions = prepare_state(state, cfg)
            sha = hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()
        except Exception as exc:  # never send a state we could not redact
            clean, error = None, _err(exc)

    # Expanded from the redacted state: per-item text goes into the instructions.
    try:
        questions = expand_questions(tpl, clean if clean is not None else state)
    except Exception as exc:
        questions, error = {}, error or _err(exc)

    if error is None and questions:
        env = cfg["decide"].get("api_key_env", "OPENROUTER_API_KEY")
        api_key = os.environ.get(env)
        if not api_key:
            error = f"{env} not set"
        else:
            wire = clean
            if image_parts:
                wire = [{"role": "user", "content": [
                    {"type": "text", "text": json.dumps(clean)}, *image_parts]}]
            t0 = time.monotonic()
            try:
                raw = post(wire, questions, cfg=cfg, api_key=api_key)
                latency_ms = raw.latency_ms
            except DecideError as exc:
                error = str(exc)
            except Exception as exc:  # any failure falls back, never crashes the caller
                error = _err(exc)
            if error is not None:
                raw = None
                latency_ms = int((time.monotonic() - t0) * 1000)

    try:
        banded = band_answers(questions, raw.answers if raw else {}, thresholds)
    except Exception as exc:
        banded, error = {}, error or _err(exc)

    hits: list[str] = []
    if name == "escalation":
        hits = _redact_hits(coerce_rule_hits(state.get("rule_hits")), cfg)
        if state_error and STATE_BUILD_FAILED not in hits:
            hits.append(STATE_BUILD_FAILED)
    rstate = {**state, "rule_hits": hits} if name == "escalation" else state
    # Calibration honesty: the LOGGED route is Decide's own opinion, so for
    # ``escalation`` it is computed without the rule hits. The rules still
    # force ``apply`` below, in every mode.
    ostate = {**state, "rule_hits": []} if name == "escalation" else state
    try:
        route = ROUTES[name](banded, ostate)
    except Exception as exc:
        route, error = None, error or _err(exc)

    if mode == "active" and error is None and route is not None:
        apply = _force_escalation(route, hits) if hits else route
    else:
        apply = _fallback(tpl, rstate, hits)

    if error is not None:
        band = "uncertain"
    elif not questions:
        band = "n/a"  # nothing was asked; the route is deterministic
    else:
        band = "confident" if all(b.confident for b in banded.values()) else "uncertain"
    record = {
        "kind": "decision",
        "decision": name,
        "id": tpl.get("id"),
        "job": job.id if job is not None else None,
        "mode": mode,
        "band": band,
        "answers": {q: dataclasses.asdict(b) for q, b in banded.items()},
        "route": route,
        "apply": apply,
        "model": raw.model if raw else None,
        "latency_ms": latency_ms,
        "usage": raw.usage if raw else {},
        "error": _scrub(error, cfg),
        "redactions": redactions,
        "state_sha256": sha,
        "ts": now_iso(),
    }
    try:
        append_jsonl(_log_path(root, job), record)
    except Exception as exc:
        # The decision is unrecorded, so it cannot be calibrated or audited:
        # do not act on it. The caller still gets a full fallback record.
        note = _scrub(f"decision log write failed: {_err(exc)}", cfg)
        record["error"] = f"{note}; {record['error']}" if record["error"] else note
        record["band"] = "uncertain"
        record["apply"] = _fallback(tpl, rstate, hits)
        print(f"decide: {note}", file=sys.stderr)
    return record
