"""Deterministic safety policy: escalation paths, tool-call and file-path gates.

These rules run BEFORE any model-based decision and can never be overridden by
one. When in doubt a command is ``unknown`` (never ``allow``).
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

_MAX_CMD = 4096
# Sequence separators (``|`` is handled separately as a pipeline).
_SEQ = re.compile(r"&&|\|\||[;\r\n]")
# Anything that makes a command compound or opaque: operators, redirects,
# command substitution, backticks, backgrounding, newlines.
_COMPOUND = re.compile(r"&&|\|\||[;|&\r\n`<>]|\$\(")
# Characters bash would quote-strip, escape-process, brace/glob/tilde/param-expand.
_SHELL_META = re.compile(r"[\"'\\{}*?\[\]$~`()]")
# ``git commit -m MSG`` is the one quoted form that is allowed: a bare safe word
# or a quoted string with no expansion characters inside.
_COMMIT = re.compile(
    r"""git commit -m (?:"[^"$`\\]*"|'[^'$`\\]*'|[A-Za-z0-9_.:/@%+=,][A-Za-z0-9_.:/@%+=,-]*)"""
)
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_CLEAN = re.compile(r"^[$({`\"']+|[)}`\"';]+$")
_REV = re.compile(r"[A-Za-z0-9^][A-Za-z0-9._/^-]*")
_NUM = re.compile(r"[0-9]{1,6}")
_FORCE = re.compile(r"-[a-zA-Z]*f[a-zA-Z]*")


def _tokenize(seg: str, plain: bool) -> list[str]:
    """Split like a shell would; fall back to a plain split on bad quoting.

    ``plain`` skips shlex (pure Python, slow) for over-long input. Tokens that
    still contain spaces (``bash -c "git push"``) are expanded one level.
    """
    toks: list[str] | None = None
    if not plain:
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = None
    if toks is None:
        toks = [t.strip("\"'") for t in seg.split()]
    out: list[str] = []
    prev = ""
    for t in toks:
        # a ``-m`` message is data, not a command line
        if " " in t and prev != "-m":
            out.extend(x.strip("\"'") for x in t.split())
        else:
            out.append(t)
        prev = t
    return out


def _base(t: str) -> str:
    return _CLEAN.sub("", t).rsplit("/", 1)[-1]


def _cmd_denied(toks: list[str]) -> bool:
    """Token-level deny rules for one simple command. Linear in len(toks)."""
    clean = [_CLEAN.sub("", t) for t in toks]
    first: dict[str, int] = {}
    for i, t in enumerate(clean):
        first.setdefault(t.rsplit("/", 1)[-1], i)

    def rest(prog: str) -> list[str] | None:
        i = first.get(prog)
        return None if i is None else clean[i + 1:]

    if "sudo" in first:
        return True
    r = rest("git")
    if r is not None:
        rs = set(r)
        if "push" in rs or ("reset" in rs and "--hard" in rs):
            return True
        if "clean" in rs and ("--force" in rs or any(_FORCE.fullmatch(t) for t in r)):
            return True
        if any(t == "--output" or t.startswith("--output=") for t in r):
            return True
    r = rest("rm")
    if r is not None:
        raw = toks[first["rm"] + 1:]
        recursive = any(
            t == "--recursive" or (t[:1] == "-" and t[:2] != "--" and ("r" in t or "R" in t))
            for t in r
        )
        if recursive and any(
            t.lstrip("(`").startswith(("/", "~", "$HOME", "${HOME}", "..", "./.."))
            for t in raw
        ):
            return True
    r = rest("chmod")
    if r is not None and "777" in r and "-R" in r:
        return True
    r = rest("npm")
    if r is not None and "publish" in r:
        return True
    r = rest("firebase")
    if r is not None and "deploy" in r:
        return True
    r = rest("eslint")
    if r is not None and any(
        t == "--output-file" or t.startswith("--output-file=")
        or (t[:1] == "-" and t[:2] != "--" and "o" in t[1:])
        for t in r
    ):
        return True
    return False


def _repo_path(t: str) -> bool:
    return bool(t) and t[0] not in "-/" and "~" not in t and ".." not in t.split("/")


def _argv_allowed(argv: list[str], specs: list[dict]) -> bool:
    for spec in specs:
        pre = spec.get("argv", [])
        if not pre or argv[: len(pre)] != pre:
            continue
        extra = argv[len(pre):]
        if len(extra) < spec.get("min_extra", 0):
            continue
        flags = spec.get("flags", [])
        numflags = spec.get("number_flags", [])
        i, ok = 0, True
        while i < len(extra) and ok:
            t = extra[i]
            if t in flags:
                pass
            elif t in numflags:
                i += 1
                ok = i < len(extra) and bool(_NUM.fullmatch(extra[i]))
            elif spec.get("paths") and _repo_path(t):
                pass
            elif spec.get("revs") and _REV.fullmatch(t) and ".." not in t.split("/"):
                pass
            else:
                ok = False
            i += 1
        if ok:
            return True
    return False


@dataclass
class Policy:
    escalate_paths: list[str]
    # Structured specs, e.g. {"argv": ["git", "status"], "flags": ["-s"], "paths": True}.
    # Keys: argv (required prefix), flags (exact tokens), number_flags (flag +
    # numeric token), paths / revs (accept repo-relative paths / revisions),
    # min_extra (minimum number of tokens after the prefix).
    tool_allow: list[dict]
    # Linear raw-text regexes searched on every segment and the whole command.
    tool_deny: list[str]
    guarded_paths: list[str]

    def escalation_hits(self, paths: list[str]) -> list[str]:
        """Return the paths needing a human, in input order.

        Input is REPO-RELATIVE paths (as in ``git diff --name-only``). A path
        that is absolute, starts with ``~`` or escapes the repo via ``..`` is
        not repo-relative; it fails closed as ``"<path> (non-repo-relative
        path)"``. Matching is on the normalised, casefolded path.
        """
        globs = [g.casefold() for g in self.escalate_paths]
        hits: list[str] = []
        for p in paths:
            n = posixpath.normpath(p)
            if n.startswith(("/", "~", "../")) or n == "..":
                hits.append(f"{p} (non-repo-relative path)")
            elif any(PurePosixPath(n.casefold()).full_match(g) for g in globs):
                hits.append(p)
        return hits

    def _strip_cd(self, cmd: str, worktree: Path | None) -> str:
        if worktree is None:
            return cmd
        wt = re.escape(str(worktree).rstrip("/") or "/")
        m = re.match(rf"""\s*cd\s+(["']?){wt}/?\1\s*&&\s*""", cmd)
        return cmd[m.end():] if m else cmd

    def _denied(self, cmd: str, plain: bool) -> bool:
        if any(re.search(d, c) for d in self.tool_deny
               for c in (*(x.strip() for x in _SEQ.split(cmd)), cmd)):
            return True
        for seq in _SEQ.split(cmd):
            fetched = False
            for idx, piece in enumerate(seq.split("|")):
                toks = _tokenize(piece, plain)
                if _cmd_denied(toks):
                    return True
                # ``curl x | sh``: a fetch piped into a shell, across the pipe.
                if idx and fetched and toks and _base(toks[0]) in _SHELLS:
                    return True
                if any(_base(t) in ("curl", "wget") for t in toks):
                    fetched = True
        return False

    def classify_command(
        self, cmd: str, worktree: Path | None
    ) -> Literal["allow", "deny", "unknown"]:
        # 1. Collapse space/tab runs so ``git  push`` / ``git<TAB>push`` cannot
        #    dodge matching.
        cmd = re.sub(r"[ \t]+", " ", self._strip_cd(cmd, worktree)).strip()
        # 2. Deny first, on shell-tokenised text (quotes and escapes removed).
        #    Over-long input uses a cheap plain split; it is rejected below anyway.
        if self._denied(cmd, plain=len(cmd) > _MAX_CMD):
            return "deny"
        # 3. Only short, simple commands can be allowed.
        if len(cmd) > _MAX_CMD or _COMPOUND.search(cmd):
            return "unknown"
        # 4. The one quoted form allowed: ``git commit -m MSG``.
        if _COMMIT.fullmatch(cmd):
            try:
                if len(shlex.split(cmd)) == 4:
                    return "allow"
            except ValueError:
                pass
            return "unknown"
        # 5. Bash would rewrite the text before running it (quotes, escapes,
        #    braces, globs, variables, ``~``): argv validation would be moot.
        if _SHELL_META.search(cmd):
            return "unknown"
        # 6. Structured argv allowlist.
        argv = cmd.split(" ") if cmd else []
        if argv and _argv_allowed(argv, self.tool_allow):
            return "allow"
        return "unknown"

    def classify_path(
        self, path: str, worktree: Path, job_folder: Path
    ) -> Literal["allow", "ask", "deny"]:
        if path.startswith("~"):  # never expand or resolve home-relative paths
            return "deny"
        wt = Path(worktree).resolve()
        job = Path(job_folder).resolve()
        p = Path(path)
        target = (p if p.is_absolute() else wt / p).resolve()
        if target.is_relative_to(job):
            return "allow"
        if not target.is_relative_to(wt):
            return "deny"
        rel = PurePosixPath(target.relative_to(wt).as_posix().casefold())
        if any(rel.full_match(g.casefold()) for g in self.guarded_paths):
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
