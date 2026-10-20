"""PreToolUse hook: gate Bash, Edit and Write calls made in factory worktrees.

Order: deterministic policy (deny / ask / allow), then for an ``unknown``
command Mercury Decide D10 (``run_decision("toolgate", job=None, ...)``), whose
``apply.permission`` ``defer`` hands the call to Claude Code's normal
permission flow (print nothing, exit 0).

Scope: always when headless (env ``FACTORY_HEADLESS=1``, the fast-coder
children); otherwise only a Bash command starting ``cd <path> &&`` or an
Edit/Write ``file_path`` that lies under ``<root>/../<root.name>-factory/``.
Everything else (the user's own sessions) is out of scope and defers.

Never fail open (controller ruling R27a): Claude Code treats a hook that exits
non-zero or prints garbage as non-blocking, so ``main`` ALWAYS exits 0 and, for
any in-scope or unparseable event, ALWAYS prints a PreToolUse decision. Errors
answer ``ask`` in the main session and ``deny`` (reason ``needs human: ...``)
headless. Only stdlib is imported at module level; ``policy``, ``paths`` and
``run`` are imported lazily, so a broken sibling module still yields a decision.
Headless, an ``unknown`` command is denied without calling Decide (R31): the
child has no API key.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import signal
import sys
from pathlib import Path

GATED_TOOLS = ("Bash", "Edit", "Write")
TAG = "factory tool-gate: "
NEEDS_HUMAN = "needs human: "
SHOW_CHARS = 200  # command / path text quoted in a reason
# Wall-clock budget for one hook run. The hook is registered with a 30 s
# timeout; a cold ``uv run`` start takes a few seconds and Decide at most 2 x 5 s.
BUDGET_S = 20.0

# ``cd <path> &&`` with a path the shell takes literally: double-quoted without
# expansions, single-quoted, or bare without globs, ``~``, braces or operators.
_CD = re.compile(
    r"""[ \t]*cd[ \t]+(?:"([^"$`\\!]*)"|'([^']*)'|([^\s;&|<>()`$"'\\~*?\[\]{}!#]+))"""
    r"""[ \t]*&&[ \t]*"""
)


class GateTimeout(BaseException):
    """The hook ran out of time. BaseException, so no ``except Exception`` eats it."""


def _mod(name: str):
    """Import a sibling module lazily (a broken one must not stop the gate)."""
    return importlib.import_module(f"{__package__ or 'factorylib'}.{name}")


def _err(exc: BaseException) -> str:
    try:
        return f"{type(exc).__name__}: {str(exc)[:SHOW_CHARS]}"
    except Exception:  # an exception whose __str__ raises
        return "unprintable exception"


def decision(permission: str, reason: str, *, headless: bool) -> dict:
    """The PreToolUse hook output. Headless, ``ask`` becomes ``deny`` ("needs human: ")."""
    if headless and permission == "ask":
        permission, reason = "deny", NEEDS_HUMAN + TAG + reason
    else:
        reason = TAG + reason
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": permission,
        "permissionDecisionReason": reason,
    }}


# -------------------------------------------------------------------- scope


def _factory_dirs(root: Path) -> list[Path]:
    """``<root>/../<root.name>-factory``, lexically and resolved (resolved last)."""
    fd = Path(os.path.normpath(Path(root).absolute().parent / f"{Path(root).name}-factory"))
    out = [fd]
    try:
        out.append(fd.resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    return out


def _locate(p: str, cwd: Path, fdirs: list[Path]) -> tuple[bool, Path | None]:
    """``(under_factory_dir, worktree)`` for path text ``p`` (relative to ``cwd``).

    In scope when either the lexically normalised or the resolved path lies under
    the factory dir (fail closed on symlinks). The worktree is the first path
    component below it, or ``None`` for the factory dir itself.
    """
    if not p or p.startswith("~") or "\0" in p:
        return False, None
    path = Path(p) if os.path.isabs(p) else cwd / p
    cands = [Path(os.path.normpath(path))]
    try:
        cands.append(path.resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    base = fdirs[-1]
    for c in cands:
        for fd in fdirs:
            if c == fd or c.is_relative_to(fd):
                parts = c.relative_to(fd).parts
                return True, (base / parts[0]) if parts else None
    return False, None


def _same(a: str, cwd: Path, b: Path) -> bool:
    try:
        pa = Path(a) if os.path.isabs(a) else cwd / a
        return pa.resolve() == b.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


class _Scope:
    def __init__(self, tool: str, cwd: Path, worktree: Path | None, text: str, pcmd: str):
        self.tool, self.cwd, self.worktree, self.text, self.pcmd = tool, cwd, worktree, text, pcmd


def _resolve(event, root: Path, headless: bool) -> _Scope | None:
    """The gate's view of ``event``, or ``None`` when it is out of scope.

    Raises ``ValueError`` for a malformed event (the caller answers ask / deny).
    """
    if not isinstance(event, dict):
        raise ValueError("event is not a JSON object")
    tool = event.get("tool_name")
    if not isinstance(tool, str):
        raise ValueError("tool_name missing or not a string")
    if tool not in GATED_TOOLS:
        return None  # the matcher only routes Bash|Edit|Write here
    tin = event.get("tool_input")
    if not isinstance(tin, dict):
        raise ValueError("tool_input missing or not an object")
    raw_cwd = event.get("cwd")
    cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and os.path.isabs(raw_cwd) else Path.cwd()
    fdirs = _factory_dirs(root)
    cwd_in, cwd_wt = _locate(str(cwd), cwd, fdirs)
    if not cwd_in:
        cwd_wt = None

    if tool == "Bash":
        cmd = tin.get("command")
        if not isinstance(cmd, str):
            raise ValueError("Bash command missing or not a string")
        m = _CD.match(cmd)
        if m:
            target = next(g for g in m.groups() if g is not None)
            inside, wt = _locate(target, cwd, fdirs)
            if inside:
                # The policy strips only a literal ``cd <worktree> &&``; do it here
                # for any spelling that resolves to exactly the worktree.
                rest = cmd[m.end():] if wt is not None and _same(target, cwd, wt) else cmd
                return _Scope(tool, cwd, wt, cmd, rest)
        if not headless:
            return None
        return _Scope(tool, cwd, cwd_wt, cmd, cmd)

    fp = tin.get("file_path")
    if not isinstance(fp, str) or not fp:
        raise ValueError(f"{tool} file_path missing or not a string")
    inside, wt = _locate(fp, cwd, fdirs)
    if headless:
        wt = cwd_wt  # the child's worktree is its cwd
    elif not inside:
        return None
    if not fp.startswith("~") and not os.path.isabs(fp):
        fp = str(cwd / fp)
    return _Scope(tool, cwd, wt, fp, fp)


# --------------------------------------------------------------------- gate


def _d10_p(rec: dict) -> str:
    try:
        p = rec["answers"]["class"]["p"]
        return f"{float(p):.2f}"
    except Exception:
        return "n/a"


def _gate(event, root: Path, policy, headless: bool, decide) -> dict | None:
    sc = _resolve(event, root, headless)
    if sc is None:
        return None
    show = sc.text[:SHOW_CHARS]

    def out(permission, reason):
        return decision(permission, reason, headless=headless)

    if sc.tool != "Bash":
        if sc.worktree is None:
            return out("ask", f"no factory worktree for {sc.tool}: {show}")
        job_folder = Path(root) / "factory" / "jobs" / sc.worktree.name
        verdict = policy.classify_path(sc.pcmd, sc.worktree, job_folder)
        if verdict == "deny":
            return out("deny", f"path outside the worktree: {show}")
        if verdict == "ask":
            return out("ask", f"guarded path: {show}")
        if verdict == "allow":
            return out("allow", f"path allowed: {show}")
        raise ValueError(f"unexpected path verdict {verdict!r}")

    verdict = policy.classify_command(sc.pcmd, sc.worktree)
    if verdict == "deny":
        return out("deny", f"policy deny: {show}")
    if verdict == "ask":
        return out("ask", f"policy ask: {show}")
    if verdict == "allow":
        return out("allow", f"policy allow: {show}")
    if verdict != "unknown":
        raise ValueError(f"unexpected command verdict {verdict!r}")
    if headless:  # R31: no key in the child, never call Decide
        return out("ask", f"command not on allowlist: {show}")

    if decide is None:
        decide = _mod("run").run_decision
    state = {"tool": "Bash", "command": sc.text, "cwd": str(sc.cwd),
             "worktree": str(sc.worktree) if sc.worktree is not None else None}
    rec = decide("toolgate", root=root, job=None, state=state)
    apply = rec.get("apply") if isinstance(rec, dict) else None
    perm = apply.get("permission") if isinstance(apply, dict) else None
    if perm == "defer":
        return None  # shadow mode or fallback: Claude Code's normal flow
    if perm in ("allow", "ask", "deny"):
        return out(perm, f"D10 p={_d10_p(rec)}")
    return out("ask", f"D10 gave no usable permission ({str(perm)[:40]!r})")


def gate(event: dict, *, root: Path, policy, headless: bool, decide=None) -> dict | None:
    """The hook decision for one PreToolUse ``event``; ``None`` means defer.

    ``decide`` defaults to ``run.run_decision`` (imported lazily). Any exception
    is a decision too: ``ask`` (main session) or ``deny`` with ``needs human:``
    (headless).
    """
    try:
        return _gate(event, Path(root), policy, headless, decide)
    except Exception as exc:
        return decision("ask", f"error: {_err(exc)}", headless=headless)


# --------------------------------------------------------------------- main


def _find_root(event) -> Path:
    cwd = event.get("cwd") if isinstance(event, dict) else None
    try:
        paths = _mod("paths")
        if isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd):
            try:
                return paths.find_root(Path(cwd))
            except Exception:
                pass
        return paths.find_root()
    except Exception:
        # <root>/factory/factorylib/toolgate.py -> <root>
        return Path(__file__).resolve().parents[2]


def _on_alarm(signum, frame):
    raise GateTimeout()


def _arm(seconds: float):
    try:
        prev = signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        return (prev,)
    except (ValueError, OSError, AttributeError):  # not the main thread / no SIGALRM
        return None


def _disarm(armed) -> None:
    if armed is None:
        return
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, armed[0] if armed[0] is not None else signal.SIG_DFL)
    except (ValueError, OSError, AttributeError, TypeError):
        pass


def _run(raw: str, headless: bool, root: Path | None, decide) -> dict | None:
    event = json.loads(raw)
    root = Path(root) if root is not None else _find_root(event)
    if not headless and _resolve(event, root, headless) is None:
        return None  # out of scope: the user's own session
    policy = _mod("policy").load_policy(root)
    return gate(event, root=root, policy=policy, headless=headless, decide=decide)


def main(stdin=None, stdout=None, env=None, root: Path | None = None, decide=None) -> int:
    """Read one PreToolUse event on stdin, print the decision (or nothing). Always 0."""
    headless = False
    out = None
    try:
        env = os.environ if env is None else env
        headless = env.get("FACTORY_HEADLESS") == "1"
        try:
            armed = _arm(BUDGET_S)
            try:
                out = _run((stdin or sys.stdin).read(), headless, root, decide)
            finally:
                _disarm(armed)
        except GateTimeout:
            out = decision("ask", f"error: timed out after {BUDGET_S:g}s", headless=headless)
        except BaseException as exc:  # noqa: BLE001 (never fail open, R27a)
            out = decision("ask", f"error: {_err(exc)}", headless=headless)
    except BaseException as exc:  # noqa: BLE001 (e.g. a late GateTimeout)
        out = decision("ask", f"error: {_err(exc)}", headless=headless)
    if out is not None:
        try:
            stream = stdout or sys.stdout
            stream.write(json.dumps(out))
            stream.flush()
        except BaseException:  # noqa: BLE001 (nothing more we can do; still exit 0)
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
