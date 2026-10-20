"""PreToolUse hook: gate Bash, Edit, Write, MultiEdit and NotebookEdit in factory worktrees.

Order: deterministic policy (deny / ask / allow), then for an ``unknown``
command Mercury Decide D10 (``run_decision("toolgate", job=None, ...)``), whose
``apply.permission`` ``defer`` hands the call to Claude Code's normal
permission flow (print nothing, exit 0).

Scope: always when headless (env ``FACTORY_HEADLESS=1``, the fast-coder
children). In the main session only when the path in one of these lies under
``<root>/../<root.name>-factory/``: a leading ``cd [-P|-L|--] <path>`` followed
by ``&&`` or ``;`` (also inside a leading ``(`` subshell), any ``git -C <path>``,
or an edit tool's ``file_path`` / ``notebook_path``. Everything else (the user's
own sessions) is out of scope and defers.

Root: the main checkout the gate is installed in, located from this file
(``<root>/factory/factorylib/toolgate.py``), never from the event (R40). The
wrapper also passes its LOGICAL root (``FACTORY_GATE_ROOT``, for symlinked
installs), accepted only when it holds this very file; the factory dirs of both
roots are in scope (R41). A gate copy inside a job worktree (a session opened
there) uses the sibling main checkout ``<x>-factory/<job>`` -> ``<x>``.

A worktree is the first directory below the factory dir that holds a ``.git``
entry; anything else under the factory dir has no worktree (ask / deny).
Headless, the worktree is the cwd's; a ``cd`` anywhere else, or a cwd outside
the factory dir, needs a human (ruling R39).

Never fail open (controller ruling R27a): Claude Code treats a hook that exits
non-zero or prints garbage as non-blocking, so ``main`` ALWAYS exits 0 and, for
any in-scope or unparseable event, ALWAYS prints a PreToolUse decision. Errors
answer ``ask`` in the main session and ``deny`` (reason ``needs human: ...``)
headless. Only stdlib is imported at module level; ``policy`` and
``run`` are imported lazily, so a broken sibling module still yields a decision.
Headless, an ``unknown`` command is denied without calling Decide (R31): the
child has no API key. Headless, the job folder is orchestrator-owned: a child may
write only ``<job>/build.md`` there; any other job-folder path needs a human.

Audit log: ``main`` appends one ``{"kind": "gate", ...}`` line per in-scope event
(every printed decision, and a D10 ``defer``) to ``<root>/factory/tool-gate.jsonl``
with the decision's source (``policy``, ``d10``, ``scope``, ``headless``,
``error``) and its reason, redacted. Logging never changes the decision or the
exit code; a reason that cannot be redacted is not logged at all.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

GATED_TOOLS = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit")
TAG = "factory tool-gate: "
NEEDS_HUMAN = "needs human: "
SHOW_CHARS = 200  # command / path text quoted in a reason
_SCOPE_MAX = 4096  # longest command scanned for ``git -C`` scope
# Wall-clock budget for one hook run. The hook is registered with a 30 s
# timeout; a cold ``uv run`` start takes a few seconds and Decide at most 2 x 5 s.
BUDGET_S = 20.0
AUDIT_S = 2.0  # wall-clock budget for the audit-log append

# A path the shell takes literally: double-quoted without expansions,
# single-quoted, or bare without globs, ``~``, braces or operators.
_PATH = r"""(?:"([^"$`\\!]*)"|'([^']*)'|([^\s;&|<>()`$"'\\~*?\[\]{}!#]+))"""
# Leading ``[(] cd [-P|-L|--]... <path> (&&|;)``. Groups: 1 subshell, 2-4 path, 5 separator.
_CD = re.compile(
    r"""[ \t]*(\([ \t]*)?cd(?:[ \t]+(?:-P|-L|--))*[ \t]+""" + _PATH + r"""[ \t]*(&&|;)[ \t]*"""
)
# ``git ... -C <path>`` anywhere in the command (scope only; groups 1-3 path).
# Its lazy scan is quadratic in the worst case, so it only runs on commands up
# to _SCOPE_MAX chars; longer ones are out of scope in the main session (defer).
_GIT_C = re.compile(
    r"""(?:^|[\s;&|(`])git[ \t]+(?:[^;&|\n`()]*?[ \t])?-C[ \t]*"""
    r"""(?:"([^"]*)"|'([^']*)'|([^\s;&|<>()`'"]+))"""
)


class GateTimeout(BaseException):
    """The hook ran out of time. BaseException, so no ``except Exception`` eats it."""


def _mod(name: str):
    """Import a sibling module lazily (a broken one must not stop the gate)."""
    return importlib.import_module(f"{__package__ or 'factorylib'}.{name}")


def _redact_text(text: str, root=None) -> str:
    """Redact ``text`` in full (built-in plus the root's configured
    ``redact_patterns``) so a later cut never leaves a secret fragment that is
    too short to match (R20: redaction before the cap)."""
    redact_obj = _mod("redact").redact_obj
    if root is not None:
        try:
            cfg = json.loads((Path(root) / "factory" / "config.json").read_text())
            patterns = [x for x in cfg.get("redact_patterns") or [] if isinstance(x, str)]
            return redact_obj(str(text), patterns)[0]
        except Exception:  # unreadable config or a bad configured pattern
            pass
    return redact_obj(str(text), [])[0]


def _show(text: str, root=None) -> str:
    """Redacted, then capped, text for a reason; fails safe to a placeholder."""
    try:
        return _redact_text(text, root)[:SHOW_CHARS]
    except Exception:  # redaction module unavailable
        return "[text withheld]"


def _err(exc: BaseException) -> str:
    try:
        return f"{type(exc).__name__}: {_show(str(exc))}"
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


def _factory_dirs(roots: list[Path]) -> list[Path]:
    """``<root>/../<root.name>-factory`` for each root, lexical and resolved forms."""
    out: list[Path] = []
    for root in roots:
        root = Path(os.path.normpath(Path(root).absolute()))
        forms = [root]
        try:
            forms.append(root.resolve())
        except (OSError, RuntimeError, ValueError):
            pass
        for r in forms:
            fd = r.parent / f"{r.name}-factory"
            for d in (fd, _try_resolve(fd)):
                if d is not None and d not in out:
                    out.append(d)
    return out


def _try_resolve(p: Path) -> Path | None:
    try:
        return p.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _locate(p: str, cwd: Path, fdirs: list[Path],
            real_roots: tuple = ()) -> tuple[bool, Path | None]:
    """``(under_factory_dir, worktree)`` for path text ``p`` (relative to ``cwd``).

    In scope when either the lexically normalised or the resolved path lies under
    the factory dir (fail closed on symlinks). The worktree is the first path
    component below it when that is a real directory (not a symlink) holding a
    ``.git`` entry; otherwise ``None`` (the factory dir itself, a stray file or
    directory).

    A factory dir is also any ancestor ``<x>-factory`` whose sibling ``<x>``
    resolves to one of ``real_roots`` (the roots' realpaths): the logical and the
    resolved form of a symlinked repo dir name the same factory, whichever form
    the gate was started with (T11-F3).
    """
    if not p or p.startswith("~") or "\0" in p:
        return False, None
    path = Path(p) if os.path.isabs(p) else cwd / p
    cands = [Path(os.path.normpath(path))]
    try:
        cands.append(path.resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    for c in cands:
        for fd in fdirs:
            if c == fd or c.is_relative_to(fd):
                parts = c.relative_to(fd).parts
                return True, _worktree(fd / parts[0]) if parts else None
    if real_roots:
        for c in cands:
            for fd in (c, *c.parents):
                name = fd.name
                if (name.endswith(_SUFFIX) and len(name) > len(_SUFFIX)
                        and _try_resolve(fd.parent / name[: -len(_SUFFIX)]) in real_roots):
                    parts = c.relative_to(fd).parts
                    return True, _worktree(fd / parts[0]) if parts else None
    return False, None


def _worktree(cand: Path) -> Path | None:
    try:
        if (not cand.is_symlink() and cand.is_dir()
                and os.path.lexists(cand / ".git")):
            return cand
    except (OSError, ValueError):
        pass
    return None


def _within(a: str, cwd: Path, wt: Path) -> bool:
    """``a`` (relative to ``cwd``) resolves to ``wt`` or a path inside it."""
    try:
        pa = Path(a) if os.path.isabs(a) else cwd / a
        return pa.resolve().is_relative_to(wt.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _same(a: str, cwd: Path, b: Path) -> bool:
    try:
        pa = Path(a) if os.path.isabs(a) else cwd / a
        return pa.resolve() == b.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


class _Scope:
    """``text``: the command / path as given; ``pcmd``: what the policy classifies;
    ``problem``: why the call needs a human even when the policy would allow it."""

    def __init__(self, tool: str, cwd: Path, worktree: Path | None, text: str, pcmd: str,
                 problem: str | None = None):
        self.tool, self.cwd, self.worktree, self.text, self.pcmd = tool, cwd, worktree, text, pcmd
        self.problem = problem if problem is not None else (
            None if worktree is not None else "no factory worktree")


def _first(groups) -> str:
    return next(g for g in groups if g is not None)


def _resolve(event, root: Path, headless: bool, roots: list[Path] | None = None) -> _Scope | None:
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
    fdirs = _factory_dirs(roots or [root])
    real_roots = tuple(r for r in map(_try_resolve, roots or [root]) if r is not None)
    cwd_in, cwd_wt = _locate(str(cwd), cwd, fdirs, real_roots)
    if not cwd_in:
        cwd_wt = None

    if tool == "Bash":
        cmd = tin.get("command")
        if not isinstance(cmd, str):
            raise ValueError("Bash command missing or not a string")
        m = _CD.match(cmd)
        target = _first(m.group(2, 3, 4)) if m else None
        # Strip only ``cd <worktree> &&`` (any spelling resolving to exactly the
        # worktree): after ``;`` or in a subshell the rest may run elsewhere.
        strip = m is not None and m.group(1) is None and m.group(5) == "&&"

        def rest(wt):
            if strip and wt is not None and _same(target, cwd, wt):
                return cmd[m.end():]
            return cmd

        if headless:
            if cwd_wt is None:
                return _Scope(tool, cwd, None, cmd, cmd, "cwd is not a factory worktree")
            if m and not _within(target, cwd, cwd_wt):
                return _Scope(tool, cwd, cwd_wt, cmd, cmd, "cd outside own worktree")
            return _Scope(tool, cwd, cwd_wt, cmd, rest(cwd_wt))
        if m:
            inside, wt = _locate(target, cwd, fdirs, real_roots)
            if inside:
                return _Scope(tool, cwd, wt, cmd, rest(wt))
        if len(cmd) <= _SCOPE_MAX:
            for gm in _GIT_C.finditer(cmd):
                inside, wt = _locate(_first(gm.groups()), cwd, fdirs, real_roots)
                if inside:
                    return _Scope(tool, cwd, wt, cmd, cmd)
        return None

    fp = tin.get("notebook_path") if tool == "NotebookEdit" else None
    if not isinstance(fp, str) or not fp:
        fp = tin.get("file_path")
    if not isinstance(fp, str) or not fp:
        raise ValueError(f"{tool} file_path / notebook_path missing or not a string")
    if "\0" in fp:
        raise ValueError(f"{tool} path contains a NUL byte")
    inside, wt = _locate(fp, cwd, fdirs, real_roots)
    if headless:
        wt = cwd_wt  # the child's worktree is its cwd
    elif not inside:
        return None
    if not fp.startswith("~") and not os.path.isabs(fp):
        fp = str(cwd / fp)
    return _Scope(tool, cwd, wt, fp, fp)


# --------------------------------------------------------------------- gate


# The one job-folder file a headless child may write (``scout.md`` and the
# transcripts are written by fast-coder itself, never by the child).
CHILD_JOB_FILES = ("build.md",)


def _orchestrator_owned(path: str, job_folder: Path) -> bool:
    """``path`` resolves into the job folder but is not a file a child may write.

    Fails closed: a path that cannot be resolved counts as orchestrator-owned.
    ``~`` paths are left to the policy (which denies them).
    """
    if path.startswith("~"):
        return False
    try:
        target = Path(path).resolve()
        job = job_folder.resolve()
    except (OSError, RuntimeError, ValueError):
        return True
    if not target.is_relative_to(job):
        return False
    # ``target`` is fully resolved: a symlinked build.md resolves elsewhere
    return target not in [job / name for name in CHILD_JOB_FILES]


def _d10_p(rec: dict) -> str:
    try:
        p = rec["answers"]["class"]["p"]
        return f"{float(p):.2f}"
    except Exception:
        return "n/a"


def _gate(event, root: Path, policy, headless: bool, decide, roots, meta: dict) -> dict | None:
    sc = _resolve(event, root, headless, roots)
    if sc is None:
        return None
    meta["job"] = sc.worktree.name if sc.worktree is not None else None
    show = _show(sc.text, root)

    def out(permission, reason, source="policy"):
        meta["source"] = source
        return decision(permission, reason, headless=headless)

    if sc.tool != "Bash":
        if sc.problem is not None:
            return out("ask", f"{sc.problem} for {sc.tool}: {show}", "scope")
        job_folder = Path(root) / "factory" / "jobs" / sc.worktree.name
        if headless and _orchestrator_owned(sc.pcmd, job_folder):
            return out("ask", f"job folder is orchestrator-owned: {show}", "scope")
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
    if sc.problem is not None:
        return out("ask", f"{sc.problem}: {show}", "scope")
    if verdict == "ask":
        return out("ask", f"policy ask: {show}")
    if verdict == "allow":
        return out("allow", f"policy allow: {show}")
    if verdict != "unknown":
        raise ValueError(f"unexpected command verdict {verdict!r}")
    if headless:  # R31: no key in the child, never call Decide
        return out("ask", f"command not on allowlist: {show}", "headless")

    if decide is None:
        decide = _mod("run").run_decision
    state = {"tool": "Bash", "command": sc.text, "cwd": str(sc.cwd),
             "worktree": str(sc.worktree) if sc.worktree is not None else None}
    rec = decide("toolgate", root=root, job=None, state=state)
    apply = rec.get("apply") if isinstance(rec, dict) else None
    perm = apply.get("permission") if isinstance(apply, dict) else None
    if perm == "defer":
        # shadow mode or fallback: Claude Code's normal flow
        meta.update(source="d10", deferred=True, reason=f"D10 p={_d10_p(rec)}: defer")
        return None
    if perm in ("allow", "ask", "deny"):
        return out(perm, f"D10 p={_d10_p(rec)}", "d10")
    return out("ask", f"D10 gave no usable permission ({str(perm)[:40]!r})", "d10")


def gate(event: dict, *, root: Path, policy, headless: bool, decide=None,
         scope_roots: list[Path] | None = None, meta: dict | None = None) -> dict | None:
    """The hook decision for one PreToolUse ``event``; ``None`` means defer.

    ``decide`` defaults to ``run.run_decision`` (imported lazily).
    ``scope_roots`` (default ``[root]``) are the roots whose ``-factory`` dirs are
    in scope. Any exception is a decision too: ``ask`` (main session) or ``deny``
    with ``needs human:`` (headless). ``meta`` (optional) receives the audit
    fields: ``source``, ``job`` and, for a D10 defer, ``deferred`` / ``reason``.
    """
    meta = {} if meta is None else meta
    try:
        return _gate(event, Path(root), policy, headless, decide, scope_roots, meta)
    except Exception as exc:
        meta["source"] = "error"
        return decision("ask", f"error: {_err(exc)}", headless=headless)


# --------------------------------------------------------------------- main


def self_root() -> Path:
    """The main checkout this gate is installed in.

    ``<root>/factory/factorylib/toolgate.py`` -> ``<root>``. Never derived from
    the event's cwd: a worktree's ``.git`` file must not be able to choose which
    ``policy.json`` loads (ruling R40).
    """
    return Path(__file__).resolve().parents[2]


class GateRootError(Exception):
    """This gate copy sits in a job worktree with no main checkout beside it."""


_SUFFIX = "-factory"


def _main_of(r: Path) -> Path:
    """``<x>-factory/<job>`` (a gate copy inside a job worktree) -> ``<x>``.

    Only by directory names, never by parsing ``.git``. A main checkout that
    happens to live in a ``*-factory`` folder (``.git`` is a directory there)
    and has no such sibling stays itself.
    """
    name = r.parent.name
    if not name.endswith(_SUFFIX) or len(name) == len(_SUFFIX):
        return r
    main = r.parent.parent / name[: -len(_SUFFIX)]
    if (main / "factory" / "policy.json").is_file():
        return main
    if (r / ".git").is_dir():
        return r
    raise GateRootError(f"gate copy in {r} but no main checkout at {main}")


def gate_roots(env) -> list[Path]:
    """The roots this gate serves, primary first (the primary supplies the policy).

    The wrapper's LOGICAL root ``FACTORY_GATE_ROOT`` (absolute, and
    ``<it>/factory/factorylib/toolgate.py`` must be this very file, so an
    inherited value cannot redirect the gate), then the self-located one.

    Each is mapped through ``_main_of`` in its given and its resolved form. When
    either form is a job worktree whose main checkout exists, that main checkout
    replaces the root and comes FIRST: a session opened through a symlink to a
    worktree (``~/wlink -> app-factory/001-d``) must never load the worktree's
    own ``policy.json`` (T11-F3).
    """
    roots: list[Path] = []
    raw = env.get("FACTORY_GATE_ROOT") or ""
    if raw and os.path.isabs(raw):
        logical = Path(os.path.normpath(raw))
        try:
            if os.path.samefile(logical / "factory" / "factorylib" / "toolgate.py", __file__):
                roots.append(logical)
        except (OSError, ValueError):
            pass
    roots.append(self_root())
    mains: list[Path] = []
    plain: list[Path] = []
    for r in roots:
        m = _main_of(r)
        if m == r:
            rr = _try_resolve(r)
            if rr is not None and rr != r:
                m2 = _main_of(rr)
                if m2 != rr:
                    m = m2  # r is an alias of a job worktree
        (mains if m != r else plain).append(m)
    out: list[Path] = []
    for m in mains + plain:
        if m not in out:
            out.append(m)
    return out


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


def _run(raw: str, headless: bool, root: Path | None, decide, env, meta: dict) -> dict | None:
    roots = [Path(root)] if root is not None else gate_roots(env)
    root = roots[0]
    meta["root"] = root
    event = json.loads(raw)
    if isinstance(event, dict) and isinstance(event.get("tool_name"), str):
        meta["tool"] = event["tool_name"]
    if not headless and _resolve(event, root, headless, roots) is None:
        return None  # out of scope: the user's own session
    policy = _mod("policy").load_policy(root)
    return gate(event, root=root, policy=policy, headless=headless, decide=decide,
                scope_roots=roots, meta=meta)


def _audit(out: dict | None, meta: dict, headless: bool) -> None:
    """Append one ``kind: gate`` line to ``<root>/factory/tool-gate.jsonl``.

    Only for in-scope events (a printed decision, or a D10 defer). Never raises
    and never blocks for long (its own ``AUDIT_S`` alarm). The reason is redacted
    (built-in plus configured ``redact_patterns``) and capped at SHOW_CHARS; if
    the redaction module cannot load, nothing is written.
    """
    armed = None
    try:
        root = meta.get("root")
        if root is None or (out is None and not meta.get("deferred")):
            return
        armed = _arm(AUDIT_S)
        if out is not None:
            h = out["hookSpecificOutput"]
            perm, reason = h["permissionDecision"], h["permissionDecisionReason"]
        else:
            perm, reason = "defer", meta.get("reason") or "defer"
        reason = _redact_text(reason, root)  # quoted text was redacted before its cut
        rec = {"kind": "gate",
               "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "headless": headless, "tool": meta.get("tool"), "permission": perm,
               "source": meta.get("source") or "error", "reason": reason[:SHOW_CHARS],
               "job": meta.get("job")}
        data = (json.dumps(rec) + "\n").encode()
        fd = os.open(Path(root) / "factory" / "tool-gate.jsonl",
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, data)  # one write: concurrent gates never interleave a line
        finally:
            os.close(fd)
    except BaseException:  # noqa: BLE001 (the log never changes the decision)
        pass
    finally:
        _disarm(armed)


def main(stdin=None, stdout=None, env=None, root: Path | None = None, decide=None) -> int:
    """Read one PreToolUse event on stdin, print the decision (or nothing). Always 0."""
    headless = False
    out = None
    meta: dict = {}
    real_stdout = stdout or sys.stdout
    saved_stdout = sys.stdout
    try:
        # Anything a module prints while the gate runs must not reach Claude Code.
        sys.stdout = sys.stderr
        env = os.environ if env is None else env
        headless = env.get("FACTORY_HEADLESS") == "1"
        try:
            armed = _arm(BUDGET_S)
            try:
                out = _run((stdin or sys.stdin).read(), headless, root, decide, env, meta)
            finally:
                _disarm(armed)
        except GateTimeout:
            meta["source"] = "error"
            out = decision("ask", f"error: timed out after {BUDGET_S:g}s", headless=headless)
        except BaseException as exc:  # noqa: BLE001 (never fail open, R27a)
            meta["source"] = "error"
            out = decision("ask", f"error: {_err(exc)}", headless=headless)
    except BaseException as exc:  # noqa: BLE001 (e.g. a late GateTimeout)
        meta["source"] = "error"
        out = decision("ask", f"error: {_err(exc)}", headless=headless)
    finally:
        sys.stdout = saved_stdout
    try:
        sys.stdout = sys.stderr
        _audit(out, meta, headless)
    except BaseException:  # noqa: BLE001 (e.g. a GateTimeout racing _disarm)
        pass
    finally:
        sys.stdout = saved_stdout
    if out is not None:
        try:
            stream = real_stdout
            stream.write(json.dumps(out))
            stream.flush()
        except BaseException:  # noqa: BLE001 (nothing more we can do; still exit 0)
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
