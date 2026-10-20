"""Secret redaction and size capping for state blobs sent to Mercury Decide."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Order matters: the .env line pattern runs first so a whole `KEY=value` line is
# one replacement; `sk-` runs before `sk-or-v1-` and already covers it.
_BUILTIN = [
    re.compile(r"^[A-Z][A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=.*$", re.MULTILINE),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-or-v1-[a-f0-9]{32,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]


def _compile_extra(extra_patterns: list[str]) -> list[re.Pattern]:
    compiled = []
    for p in extra_patterns or []:
        try:
            compiled.append(re.compile(p, re.MULTILINE))
        except re.error as e:
            raise ValueError(f"invalid redact pattern {p!r}: {e}") from e
    return compiled


def redact_obj(obj: Any, extra_patterns: list[str]) -> tuple[Any, int]:
    """Return a redacted copy of ``obj`` and the number of replacements made.

    Walks dicts, lists and tuples; only string values are redacted. Dict keys
    and non-string leaves are left untouched. The input is never mutated.
    """
    patterns = _BUILTIN + _compile_extra(extra_patterns)
    return _walk(obj, patterns)


def _walk(obj: Any, patterns: list[re.Pattern]) -> tuple[Any, int]:
    if isinstance(obj, str):
        total = 0
        for pat in patterns:
            obj, n = pat.subn(REDACTED, obj)
            total += n
        return obj, total
    if isinstance(obj, dict):
        out, total = {}, 0
        for k, v in obj.items():
            out[k], n = _walk(v, patterns)
            total += n
        return out, total
    if isinstance(obj, (list, tuple)):
        items, total = [], 0
        for v in obj:
            r, n = _walk(v, patterns)
            items.append(r)
            total += n
        return (tuple(items) if isinstance(obj, tuple) else items), total
    return obj, 0


def cap_text(s: str, max_chars: int) -> str:
    """Keep head and tail of ``s`` (summing to ``max_chars``) around a marker."""
    if len(s) <= max_chars:
        return s
    head = max_chars // 2
    tail = max_chars - head
    removed = len(s) - max_chars
    marker = f"\n…[truncated {removed} chars]…\n"
    return s[:head] + marker + (s[len(s) - tail:] if tail else "")
