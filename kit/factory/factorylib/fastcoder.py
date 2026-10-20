"""``fast-coder`` command line (``python -m factorylib.fastcoder``; wrapped by ``factory/bin/fast-coder``).

    fast-coder <scout|fast-builder|fast-fixer> --job <id> [--round N] [--port P]
               [--message TEXT] [--resume SESSION_ID]

Runs Mercury 3 in a headless ``claude -p`` child (or ``pi`` when
``fast_coder.harness`` is ``"pi"``) in the job worktree, with the flag set
recorded in the Phase-0 S2 spike. The child's raw stdout goes to
``factory/jobs/<id>/fast-<role>-round-<N>.json``. Prints one JSON object:

    {"role", "session_id", "exit_code", "cost_usd", "cost_estimate": true,
     "transcript", "error": null | str}

and exits with the child's exit code (124 on timeout). ``cost_usd`` is an
estimate from the usage token counts times ``fast_coder.price_per_mtok``;
claude's own ``total_cost_usd`` prices unknown models at the wrong rates and is
never used. Exit 2, without spawning anything, on a usage error, a bad job or a
missing ``OPENROUTER_API_KEY``. The OpenRouter credentials go only into the
child's environment; ``os.environ`` is never changed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .config import load_config
from .paths import Job, find_root, load_job

ROLES = ("scout", "fast-builder", "fast-fixer")
FAST_AGENTS = Path(__file__).resolve().parent.parent / "fast-agents"
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
KEY_ENV = "OPENROUTER_API_KEY"
TIMEOUT_EXIT = 124
KILLED_EXIT = 143
KILL_AFTER_S = 10
HOOK_TIMEOUT_S = 30  # a cold ``uv run`` start of the gate can take several seconds
REAP_WAIT_S = 5
GATED_ROLES = ("fast-builder", "fast-fixer")
# Inherited variables that would route the claude child away from OpenRouter.
ROUTING_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL")

_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

CLAUDE_TOOLS = {"scout": "Read,Grep,Glob"}
CLAUDE_TOOLS_DEFAULT = "Read,Grep,Glob,Edit,Write,Bash"
CLAUDE_DISALLOWED = "Agent,WebFetch,WebSearch"
PI_TOOLS = {"scout": "read,grep,find,ls"}
PI_TOOLS_DEFAULT = "read,bash,edit,write"


class UsageError(Exception):
    """Exit 2 with this message on stderr."""


class _Killed(Exception):
    """fast-coder got SIGTERM/SIGINT/SIGHUP; the child group has been killed."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(f"{self.prog}: {message}")


def _fast(cfg: dict) -> dict:
    fc = dict(cfg.get("fast_coder") or {})
    fc.setdefault("model", "inception/mercury-3")
    fc.setdefault("timeout_s", 1800)
    fc.setdefault("harness", "claude-p")
    return fc


def tool_gate_path(root: Path) -> Path:
    return Path(root) / "factory" / "bin" / "tool-gate"


def hook_settings(root: Path) -> str:
    """Inline ``--settings`` JSON: the main checkout's tool gate as PreToolUse hook."""
    command = shlex.quote(str(tool_gate_path(root)))
    return json.dumps({"hooks": {"PreToolUse": [{
        "matcher": "Bash|Edit|Write",
        "hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}],
    }]}})


def build_message(role: str, job: Job, round_n: int, port: int | None,
                  message: str | None) -> str:
    head = (f"Job folder: {job.folder}/. Worktree: {job.worktree}. Branch: {job.branch}. "
            f"Job id: {job.id}.")
    port_text = f" Port: {port}." if port is not None else ""
    if message is not None:
        body = message + port_text
    elif role == "scout":
        body = f"Scout for this feature: {job.feature}" + port_text
    elif role == "fast-builder":
        body = f"Build round {round_n}." + port_text
    else:
        body = f"Fix the findings listed in {job.folder}/round-{round_n}/fast-fix.md." + port_text
    return f"{head}\n{body}"


def build_command(cfg: dict, root: Path, role: str, session_id: str, *, resume: bool,
                  prompt: str, message: str) -> list[str]:
    """The full child argv, ``timeout`` wrapper included. Never holds the key."""
    fc = _fast(cfg)
    wrap = ["timeout", "-k", str(KILL_AFTER_S), str(fc["timeout_s"])]
    if fc["harness"] == "pi":
        # S2 "Backup: pi coding agent". pi resumes by reusing the exact session id.
        return wrap + [
            "pi", "-p", "--provider", "openrouter", "--model", fc["model"],
            "--session-id", session_id,
            "--tools", PI_TOOLS.get(role, PI_TOOLS_DEFAULT),
            "--no-skills", "--no-extensions", "--no-context-files",
            "-e", str(Path(root) / "factory" / "fast-agents" / "pi-tool-gate.ts"),
            "--append-system-prompt", prompt,
            "--mode", "json", message,
        ]
    # S2 "Exact working flag set".
    return wrap + [
        "claude", "-p", "--model", fc["model"],
        "--resume" if resume else "--session-id", session_id,
        "--permission-mode", "dontAsk",
        "--allowedTools", CLAUDE_TOOLS.get(role, CLAUDE_TOOLS_DEFAULT),
        "--disallowedTools", CLAUDE_DISALLOWED,
        "--setting-sources", "local",
        "--settings", hook_settings(root),
        "--append-system-prompt", prompt,
        "--output-format", "json",
        message,
    ]


def child_env(cfg: dict, root: Path, key: str) -> dict:
    """A copy of ``os.environ`` plus the child-only variables.

    The claude child gets the key only as ``ANTHROPIC_AUTH_TOKEN`` and runs with
    ``CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1``, so the model's Bash and the hooks
    never see it; inherited provider-routing variables are dropped. pi needs
    ``OPENROUTER_API_KEY`` itself.
    """
    fc = _fast(cfg)
    env = dict(os.environ)
    env["FACTORY_HEADLESS"] = "1"
    env["FACTORY_TOOL_GATE"] = str(tool_gate_path(root))
    if fc["harness"] == "pi":
        env[KEY_ENV] = key
        return env
    env.pop(KEY_ENV, None)
    for name in ROUTING_VARS:
        env.pop(name, None)
    env.update({
        "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
        "ANTHROPIC_BASE_URL": OPENROUTER_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": fc["model"],
        "CLAUDE_CODE_SUBAGENT_MODEL": fc["model"],
    })
    return env


def _num(d: dict, name: str) -> float:
    v = d.get(name)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def estimate_cost(out, prices: dict) -> float | None:
    """USD estimate from token counts; ``None`` when the output has none.

    Prefers ``modelUsage`` (summed over models), else top-level ``usage``.
    Cache creation is charged at the input rate.
    """
    if not isinstance(out, dict):
        return None
    rows = []
    mu = out.get("modelUsage")
    if isinstance(mu, dict) and mu:
        for m in mu.values():
            if isinstance(m, dict):
                rows.append((_num(m, "inputTokens"), _num(m, "outputTokens"),
                             _num(m, "cacheReadInputTokens"),
                             _num(m, "cacheCreationInputTokens")))
    elif isinstance(out.get("usage"), dict):
        u = out["usage"]
        rows.append((_num(u, "input_tokens"), _num(u, "output_tokens"),
                     _num(u, "cache_read_input_tokens"),
                     _num(u, "cache_creation_input_tokens")))
    if not rows:
        return None
    p_in = float(prices.get("input", 0))
    p_out = float(prices.get("output", 0))
    p_cached = float(prices.get("cached_input", 0))
    total = sum(i * p_in + o * p_out + r * p_cached + c * p_in for i, o, r, c in rows)
    return round(total / 1e6, 8)


def parse_pi(stdout: str) -> dict | None:
    """Reduce pi's JSONL events to the claude result shape we read."""
    events = []
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            events.append(e)
    if not events:
        return None
    messages = []
    for e in events:
        if e.get("type") == "agent_end" and isinstance(e.get("messages"), list):
            messages = e["messages"]
    if not messages:
        messages = [e["message"] for e in events
                    if e.get("type") == "message_end" and isinstance(e.get("message"), dict)]
    assistant = [m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"]
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    for m in assistant:
        u = m.get("usage") if isinstance(m.get("usage"), dict) else {}
        usage["input_tokens"] += _num(u, "input")
        usage["output_tokens"] += _num(u, "output")
        usage["cache_read_input_tokens"] += _num(u, "cacheRead")
        usage["cache_creation_input_tokens"] += _num(u, "cacheWrite")
    result = ""
    is_error = False
    if assistant:
        last = assistant[-1]
        content = last.get("content") if isinstance(last.get("content"), list) else []
        result = "".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text")
        is_error = last.get("stopReason") == "error"
    session = next((e.get("id") for e in events if e.get("type") == "session"), None)
    return {"session_id": session, "usage": usage, "result": result, "is_error": is_error}


def _parse(harness: str, stdout: str) -> dict | None:
    if harness == "pi":
        return parse_pi(stdout)
    try:
        out = json.loads(stdout)
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's process group, then SIGKILL it after ``REAP_WAIT_S``."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=REAP_WAIT_S)
            if sig == signal.SIGTERM:
                # The leader is gone; still sweep stragglers left in the group.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return
        except subprocess.TimeoutExpired:
            continue


def _drain(proc: subprocess.Popen) -> str:
    """Read what is left of stdout without blocking on a grandchild holding the pipe."""
    try:
        stdout, _ = proc.communicate(timeout=REAP_WAIT_S)
        return stdout or ""
    except (subprocess.TimeoutExpired, ValueError, OSError):
        if proc.stdout:
            proc.stdout.close()
        return ""


def _run_child(cmd: list[str], *, cwd: Path, env: dict, timeout_s: float):
    """Return ``(exit_code, stdout, timed_out)``; raise ``_Killed`` on a signal."""
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, text=True, start_new_session=True)

    def on_signal(signum, frame):
        _kill_group(proc)
        raise _Killed()

    sigs = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    old = {s: signal.signal(s, on_signal) for s in sigs}
    try:
        try:
            stdout, _ = proc.communicate(timeout=timeout_s + KILL_AFTER_S + 20)
        except subprocess.TimeoutExpired:
            # Backstop if ``timeout`` itself hangs: kill the whole process group.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return TIMEOUT_EXIT, _drain(proc), True
        except _Killed:
            if proc.stdout:
                proc.stdout.close()
            raise
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    rc = proc.returncode
    elapsed = time.monotonic() - start
    timed_out = rc == TIMEOUT_EXIT or (rc == 128 + signal.SIGKILL and elapsed >= timeout_s)
    return (TIMEOUT_EXIT if timed_out else rc), stdout or "", timed_out


def _parse_args(argv):
    p = _Parser(prog="fast-coder", description="Run Mercury 3 headless for a factory job.")
    p.add_argument("role", choices=ROLES)
    p.add_argument("--job", required=True)
    p.add_argument("--round", type=int, default=1, dest="round_n")
    p.add_argument("--port", type=int)
    p.add_argument("--message")
    p.add_argument("--resume", metavar="SESSION_ID")
    args = p.parse_args(argv)
    if not _JOB_ID.match(args.job):
        raise UsageError(f"bad job id {args.job!r}")
    if args.resume is not None:
        try:
            uuid.UUID(args.resume)
        except ValueError as exc:
            raise UsageError(f"--resume: not a session uuid: {args.resume!r}") from exc
    if args.round_n < 1:
        raise UsageError("--round must be at least 1")
    return args


def _run(argv) -> int:
    args = _parse_args(argv)
    try:
        root = find_root()
    except (subprocess.CalledProcessError, OSError) as exc:
        raise UsageError(f"not inside a git checkout: {exc}") from exc
    try:
        job = load_job(root, args.job)
    except FileNotFoundError as exc:
        raise UsageError(f"unknown job {args.job!r}: {exc} not found") from exc
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise UsageError(f"bad job {args.job!r}: unreadable job.json ({exc})") from exc
    if not job.worktree.is_dir():
        raise UsageError(f"job {args.job!r}: worktree {job.worktree} does not exist")
    try:
        cfg = load_config(root)
    except (ValueError, OSError) as exc:
        raise UsageError(f"unreadable factory/config.json: {exc}") from exc
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise UsageError(f"{KEY_ENV} is not set; export it in the shell that runs the factory")
    gate = tool_gate_path(root)
    if args.role in GATED_ROLES and not (gate.is_file() and os.access(gate, os.X_OK)):
        raise UsageError(f"tool-gate missing or not executable: {gate}")
    fc = _fast(cfg)
    prompt = (FAST_AGENTS / f"{args.role}.md").read_text()
    message = build_message(args.role, job, args.round_n, args.port, args.message)
    session_id = args.resume or str(uuid.uuid4())
    cmd = build_command(cfg, root, args.role, session_id, resume=bool(args.resume),
                        prompt=prompt, message=message)

    start_error = None
    try:
        rc, stdout, timed_out = _run_child(cmd, cwd=job.worktree, env=child_env(cfg, root, key),
                                           timeout_s=float(fc["timeout_s"]))
    except _Killed:
        rc, stdout, timed_out = KILLED_EXIT, "", False
        start_error = "killed"
    except OSError as exc:
        rc, stdout, timed_out = 127, "", False
        start_error = f"could not start child: {exc.strerror or exc}"
    transcript = job.folder / f"fast-{args.role}-round-{args.round_n}.json"
    transcript.write_text(stdout)

    out = _parse(fc["harness"], stdout)
    error = None
    if start_error:
        error = start_error
    elif timed_out:
        error = "timeout"
    elif out is None:
        error = f"child exited {rc} with no parsable output" if rc else "unparsable child output"
    elif out.get("is_error"):
        error = f"child reported error ({out.get('subtype', 'error')}): {str(out.get('result', ''))[:300]}"
    elif rc:
        error = f"child exited {rc}"
    if error is None and args.role == "scout" and str(out.get("result") or "").strip():
        (job.folder / "scout.md").write_text(out["result"])

    sid = out.get("session_id") if out else None
    print(json.dumps({
        "role": args.role,
        "session_id": sid or session_id,
        "exit_code": rc,
        "cost_usd": estimate_cost(out, fc.get("price_per_mtok") or {}),
        "cost_estimate": True,
        "transcript": str(transcript),
        "error": error,
    }))
    return rc


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(sys.argv[1:] if argv is None else argv)
    except UsageError as exc:
        print(f"fast-coder: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
