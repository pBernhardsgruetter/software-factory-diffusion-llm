"""Secret redaction and size capping for state blobs sent to Mercury Decide."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_WORDS = r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)"

# Each entry is (pattern, replacement). Order matters: whole-line env patterns
# and PEM blocks run first so a secret is one replacement; JWT runs before
# `Bearer` so `Bearer <jwt>` is one replacement.
_BUILTIN: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), REDACTED),
    # Column-0 env line, any value: whole line is redacted (`SECRET=abc`, `export X_KEY=v`).
    (re.compile(rf"^(?:export[ \t]+)?[A-Z0-9_]*{_WORDS}[A-Z0-9_]*=.*$", re.MULTILINE), REDACTED),
    # Indented / mixed-case assignment (`=` or `:`), value of at least 8 non-space chars.
    (
        re.compile(
            rf"^[ \t]*(?:export[ \t]+)?[\w.-]*{_WORDS}[\w.-]*[ \t]*[=:][ \t]*\S{{8,}}.*$",
            re.MULTILINE | re.IGNORECASE,
        ),
        REDACTED,
    ),
    # Quoted JSON/YAML key with a quoted value of at least 8 non-space, non-quote chars.
    (
        re.compile(
            rf"([\"'])[\w.-]*{_WORDS}[\w.-]*\1[ \t]*:[ \t]*([\"'])[^\s\"']{{8,}}\2",
            re.IGNORECASE,
        ),
        REDACTED,
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"sk-or-v1-[a-f0-9]{32,}"), REDACTED),
    (re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{16,}"), REDACTED),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), REDACTED),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"xox[abpors]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"AKIA[0-9A-Z]{16}"), REDACTED),
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+"), REDACTED),
    (re.compile(r"bearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE), REDACTED),
    # URL userinfo: keep scheme and user, redact only the password.
    (
        re.compile(r"(?P<pre>[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^/\s@]+@", re.IGNORECASE),
        r"\g<pre>" + REDACTED + "@",
    ),
]


def _compile_extra(extra_patterns: list[str]) -> list[tuple[re.Pattern, str]]:
    compiled = []
    for p in extra_patterns or []:
        try:
            compiled.append((re.compile(p, re.MULTILINE), REDACTED))
        except re.error as e:
            raise ValueError(f"invalid redact pattern {p!r}: {e}") from e
    return compiled


def redact_obj(obj: Any, extra_patterns: list[str]) -> tuple[Any, int]:
    """Return a redacted copy of ``obj`` and the number of replacements made.

    Walks dicts, lists, tuples, sets and frozensets; string leaves are redacted
    and bytes/bytearray leaves are decoded (utf-8, errors=replace) to str first.
    Dict keys and other leaves are left untouched. The input is never mutated.
    """
    patterns = _BUILTIN + _compile_extra(extra_patterns)
    return _walk(obj, patterns)


def _walk(obj: Any, patterns: list[tuple[re.Pattern, str]]) -> tuple[Any, int]:
    if isinstance(obj, (bytes, bytearray)):
        obj = bytes(obj).decode("utf-8", errors="replace")
    if isinstance(obj, str):
        total = 0
        for pat, repl in patterns:
            obj, n = pat.subn(repl, obj)
            total += n
        return obj, total
    if isinstance(obj, dict):
        out, total = {}, 0
        for k, v in obj.items():
            out[k], n = _walk(v, patterns)
            total += n
        return out, total
    if isinstance(obj, (list, tuple, set, frozenset)):
        items, total = [], 0
        for v in obj:
            r, n = _walk(v, patterns)
            items.append(r)
            total += n
        kind = list if isinstance(obj, list) else type(obj)
        return kind(items), total
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
