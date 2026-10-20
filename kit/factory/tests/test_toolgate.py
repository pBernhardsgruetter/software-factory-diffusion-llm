import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from factorylib import toolgate
from factorylib.client import RawResult
from factorylib.policy import load_policy
from factorylib.run import run_decision
from factorylib.toolgate import gate

KIT = Path(__file__).resolve().parent.parent
REPO_KIT = KIT.parent  # kit/ (holds .claude/settings.json)
DECISIONS = {"allow", "deny", "ask"}


def make_root(tmp_path, *, mode="shadow", git=False):
    root = tmp_path / "main"
    (root / "factory" / "jobs" / "001-x").mkdir(parents=True)
    # the gate locates its root from its own file: install a copy under this root
    shutil.copytree(KIT / "factorylib", root / "factory" / "factorylib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(KIT / "policy.json", root / "factory" / "policy.json")
    cfg = json.loads((KIT / "config.json").read_text())
    cfg["decisions"]["toolgate"] = {"mode": mode}
    (root / "factory" / "config.json").write_text(json.dumps(cfg))
    shutil.copytree(KIT / "decisions", root / "factory" / "decisions")
    wt = tmp_path / "main-factory" / "001-x"
    if git:  # a real main checkout with the job worktree linked to it
        g = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(root)]
        subprocess.run([*g, "init", "-q"], check=True)
        subprocess.run([*g, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        subprocess.run([*g, "worktree", "add", "-q", "-b", "factory/001-x", str(wt)],
                       check=True)
    else:
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /nonexistent\n")  # a linked worktree's marker
    return root, wt


@pytest.fixture
def env(tmp_path):
    root, wt = make_root(tmp_path)
    return {"root": root, "wt": wt, "policy": load_policy(root)}


def bash(cmd, cwd):
    return {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(cwd),
            "tool_name": "Bash", "tool_input": {"command": cmd}}


def edit(path, cwd, tool="Edit"):
    return {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(cwd),
            "tool_name": tool, "tool_input": {"file_path": str(path), "old_string": "a",
                                              "new_string": "b"}}


def hso(out):
    assert out is not None
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse"
    return h["permissionDecision"], h["permissionDecisionReason"]


class NoDecide:
    def __call__(self, *a, **kw):
        raise AssertionError("decide must not be called")


class FakeDecide:
    def __init__(self, permission=None, exc=None, p=0.93):
        self.permission, self.exc, self.p = permission, exc, p
        self.calls = []

    def __call__(self, name, **kw):
        self.calls.append((name, kw))
        if self.exc:
            raise self.exc
        return {"apply": {"permission": self.permission},
                "answers": {"class": {"type": "choice", "value": "safe", "p": self.p,
                                      "confident": True}}}


def post_stub(choice, p):
    def post(state, questions, *, cfg, api_key):
        rest = {k: (1 - p) / 2 for k in ("safe", "needs_human", "destructive") if k != choice}
        return RawResult(model="m", answers={"class": {
            "type": "choice", "choice": choice, "probabilities": {choice: p, **rest}}},
            usage={}, latency_ms=1)
    return post


# ------------------------------------------------------------------ gate()


def test_user_session_outside_factory_defers(env):
    root, policy = env["root"], env["policy"]
    for ev in (bash("git push", root), bash(f"cd {root} && git push", root),
               bash("cd /tmp && rm -rf /", root), edit(root / "src" / "a.js", root),
               edit(root / ".claude" / "settings.json", root, tool="Write")):
        assert gate(ev, root=root, policy=policy, headless=False, decide=NoDecide()) is None


def test_deny_git_push_in_worktree(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    for cmd in (f"cd {wt} && git push", f"cd '{wt}' && git push origin HEAD",
                "cd ../main-factory/001-x && git push"):
        out = gate(bash(cmd, root), root=root, policy=policy, headless=False, decide=NoDecide())
        perm, reason = hso(out)
        assert perm == "deny" and reason.startswith("factory tool-gate: ")
    # headless: a policy deny stays a plain deny
    perm, reason = hso(gate(bash("git push", wt), root=root, policy=policy, headless=True,
                            decide=NoDecide()))
    assert perm == "deny" and reason.startswith("factory tool-gate: ")


def test_allow_git_status(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    out = gate(bash(f"cd {wt} && git status", root), root=root, policy=policy,
               headless=False, decide=NoDecide())
    assert hso(out)[0] == "allow"
    out = gate(bash(f"cd {wt} && git status", wt), root=root, policy=policy,
               headless=True, decide=NoDecide())
    assert hso(out)[0] == "allow"
    # headless child, cwd = worktree, plain command
    out = gate(bash("git status", wt), root=root, policy=policy, headless=True,
               decide=NoDecide())
    assert hso(out)[0] == "allow"
    # an ordinary edit inside the worktree is allowed
    out = gate(edit(wt / "src" / "a.js", root), root=root, policy=policy, headless=False,
               decide=NoDecide())
    assert hso(out)[0] == "allow"


def test_unknown_calls_d10_active_allow(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    root, wt = make_root(tmp_path, mode="active")
    policy = load_policy(root)
    calls = []

    def decide(name, **kw):
        calls.append((name, kw))
        return run_decision(name, post=post_stub("safe", 0.95), **kw)

    out = gate(bash(f"cd {wt} && make lint", root), root=root, policy=policy,
               headless=False, decide=decide)
    perm, reason = hso(out)
    assert perm == "allow" and reason == "factory tool-gate: D10 p=0.95"
    (name, kw), = calls
    assert name == "toolgate" and kw["job"] is None and kw["root"] == root
    assert kw["state"] == {"tool": "Bash", "command": f"cd {wt} && make lint",
                           "cwd": str(root), "worktree": str(wt)}
    rec = json.loads((root / "factory" / "tool-gate.jsonl").read_text().splitlines()[-1])
    assert rec["decision"] == "toolgate" and rec["apply"] == {"permission": "allow"}


def test_unknown_shadow_defers(env, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    root, wt, policy = env["root"], env["wt"], env["policy"]

    def decide(name, **kw):
        return run_decision(name, post=post_stub("safe", 0.99), **kw)

    out = gate(bash(f"cd {wt} && make lint", root), root=root, policy=policy,
               headless=False, decide=decide)
    assert out is None
    rec = json.loads((root / "factory" / "tool-gate.jsonl").read_text().splitlines()[-1])
    assert rec["mode"] == "shadow" and rec["apply"] == {"permission": "defer"}
    assert rec["route"] == {"permission": "allow"}


def test_d10_ask_and_deny_pass_through(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    for perm in ("ask", "deny"):
        out = gate(bash(f"cd {wt} && make x", root), root=root, policy=policy,
                   headless=False, decide=FakeDecide(perm, p=0.8))
        assert hso(out) == (perm, "factory tool-gate: D10 p=0.80")
    # an unusable apply is ask, never allow
    out = gate(bash(f"cd {wt} && make x", root), root=root, policy=policy,
               headless=False, decide=FakeDecide("yes"))
    assert hso(out)[0] == "ask"


def test_guarded_path_ask(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    for p in (wt / ".claude" / "settings.json", wt / "factory" / "policy.json"):
        perm, reason = hso(gate(edit(p, root, tool="Write"), root=root, policy=policy,
                                headless=False, decide=NoDecide()))
        assert perm == "ask" and reason.startswith("factory tool-gate: ")
    # an escape out of the worktree is denied
    outside = root / "src" / "a.js"
    perm, _ = hso(gate(edit(outside, wt), root=root, policy=policy, headless=True,
                       decide=NoDecide()))
    assert perm == "deny"
    # the job folder (main checkout) is allowed for a headless child
    job_file = root / "factory" / "jobs" / "001-x" / "build.md"
    assert hso(gate(edit(job_file, wt, tool="Write"), root=root, policy=policy,
                    headless=True, decide=NoDecide()))[0] == "allow"


def test_symlink_escape_in_worktree_is_denied(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    (wt / "link").symlink_to(root)
    perm, _ = hso(gate(edit(wt / "link" / "a.js", root), root=root, policy=policy,
                       headless=False, decide=NoDecide()))
    assert perm == "deny"


def test_headless_ask_becomes_deny(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    perm, reason = hso(gate(edit(wt / ".claude" / "settings.json", wt), root=root,
                            policy=policy, headless=True, decide=NoDecide()))
    assert perm == "deny" and reason.startswith("needs human: ")
    assert "factory tool-gate: " in reason


def test_headless_unknown_denies_without_calling_decide(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    cmd = "make lint " + "x" * 500
    perm, reason = hso(gate(bash(cmd, wt), root=root, policy=policy, headless=True,
                            decide=NoDecide()))
    assert perm == "deny" and reason.startswith("needs human: ")
    assert f"command not on allowlist: {cmd[:200]}" in reason
    assert cmd[:201] not in reason


def test_headless_edit_without_worktree_denies(env):
    root, policy = env["root"], env["policy"]
    perm, reason = hso(gate(edit(root / "a.js", root), root=root, policy=policy,
                            headless=True, decide=NoDecide()))
    assert perm == "deny" and reason.startswith("needs human: ")


def test_decide_error_main_session_asks_headless_denies(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    boom = FakeDecide(exc=RuntimeError("openrouter down"))
    perm, reason = hso(gate(bash(f"cd {wt} && make lint", root), root=root, policy=policy,
                            headless=False, decide=boom))
    assert perm == "ask" and reason.startswith("factory tool-gate: error")
    assert boom.calls
    perm, reason = hso(gate(bash(f"cd {wt} && make lint", wt), root=root, policy=policy,
                            headless=True, decide=NoDecide()))
    assert perm == "deny" and reason.startswith("needs human: ")


def test_no_key_record_error_defers_main_session(env, monkeypatch):
    """Review Focus 3: no key -> decide records an error, apply is the fallback (defer)."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    root, wt, policy = env["root"], env["wt"], env["policy"]
    out = gate(bash(f"cd {wt} && make lint", root), root=root, policy=policy,
               headless=False, decide=run_decision)
    assert out is None
    rec = json.loads((root / "factory" / "tool-gate.jsonl").read_text().splitlines()[-1])
    assert rec["error"] and rec["apply"] == {"permission": "defer"}


def test_exception_fails_closed_to_ask(env):
    root, wt = env["root"], env["wt"]

    class Broken:
        def classify_command(self, *a):
            raise RuntimeError("boom")

        def classify_path(self, *a):
            raise RuntimeError("boom")

    for ev in (bash(f"cd {wt} && git status", root), edit(wt / "a.js", root)):
        perm, reason = hso(gate(ev, root=root, policy=Broken(), headless=False,
                                decide=NoDecide()))
        assert perm == "ask" and reason.startswith("factory tool-gate: error")
        perm, reason = hso(gate(ev, root=root, policy=Broken(), headless=True,
                                decide=NoDecide()))
        assert perm == "deny" and reason.startswith("needs human: ")
    # a malformed event in scope is still a decision
    perm, _ = hso(gate({"tool_name": "Bash", "tool_input": None}, root=root,
                       policy=env["policy"], headless=True, decide=NoDecide()))
    assert perm == "deny"


def test_import_error_in_run_still_emits_decision(env, monkeypatch):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    monkeypatch.setitem(sys.modules, "factorylib.run", None)  # import now raises
    perm, reason = hso(gate(bash(f"cd {wt} && make lint", root), root=root, policy=policy,
                            headless=False))
    assert perm == "ask" and reason.startswith("factory tool-gate: error")
    # and through main(), with policy import broken too
    monkeypatch.setitem(sys.modules, "factorylib.policy", None)
    out = io.StringIO()
    ev = bash(f"cd {wt} && make lint", root)
    code = toolgate.main(stdin=io.StringIO(json.dumps(ev)), stdout=out,
                         env={}, root=root)
    assert code == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "ask"


# ------------------------------------------------------------------ main()


def run_main(stdin, *, root, cwd, headless=False, extra_env=None, pkg=None):
    """Run ``root``'s installed gate copy (``root/factory/factorylib``, or ``pkg``)."""
    e = {k: v for k, v in os.environ.items()
         if k not in ("FACTORY_HEADLESS", "FACTORY_GATE_ROOT")}
    e["PYTHONPATH"] = str(pkg if pkg is not None else root / "factory")
    if headless:
        e["FACTORY_HEADLESS"] = "1"
    e.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "factorylib.toolgate"], input=stdin,
                          capture_output=True, text=True, cwd=cwd, env=e, timeout=60)


def test_main_reads_stdin_prints_json(tmp_path):
    root, wt = make_root(tmp_path, git=True)
    r = run_main(json.dumps(bash(f"cd {wt} && git push", root)), cwd=root, root=root)
    assert r.returncode == 0, r.stderr
    perm, reason = hso(json.loads(r.stdout))
    assert perm == "deny" and reason.startswith("factory tool-gate: ")
    # out of scope: nothing printed, exit 0 (defer)
    r = run_main(json.dumps(bash("git push", root)), cwd=root, root=root)
    assert r.returncode == 0 and r.stdout == ""


def test_garbage_stdin_emits_decision_when_headless(tmp_path):
    root, _ = make_root(tmp_path, git=True)
    for stdin in ("not json{", "", "[1, 2]", "null", '{"tool_name": 5}'):
        r = run_main(stdin, cwd=root, headless=True, root=root)
        assert r.returncode == 0, r.stderr
        perm, reason = hso(json.loads(r.stdout))
        assert perm == "deny" and reason.startswith("needs human: ")


def test_main_always_exit_0(tmp_path):
    root, wt = make_root(tmp_path, git=True)
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    in_scope = json.dumps(bash(f"cd {wt} && git status", root))
    cases = [
        # (stdin, cwd, headless, expected decision or None for "prints nothing")
        ("not json{", root, False, "ask"),
        ("", root, False, "ask"),
        ('{"tool_name": "Bash", "tool_input": {"command": 7}}', root, False, "ask"),
        (json.dumps(bash("git push", root)), root, False, None),
        (json.dumps(bash("ls", nogit)), nogit, False, None),
        (json.dumps(bash("ls", nogit)), nogit, True, "deny"),
        (json.dumps(bash("make x", nogit)), nogit, True, "deny"),
        (in_scope, root, False, "allow"),
        (in_scope, root, True, "deny"),  # headless cwd must be the worktree (R39)
        (json.dumps(bash(f"cd {wt} && git status", wt)), wt, True, "allow"),
    ]
    for stdin, cwd, headless, want in cases:
        r = run_main(stdin, cwd=cwd, headless=headless, root=root)
        assert r.returncode == 0, (stdin, r.stderr)
        if want is None:
            assert r.stdout == "", (stdin, r.stdout)
        else:
            assert hso(json.loads(r.stdout))[0] == want, (stdin, r.stdout, r.stderr)
    # missing policy file, in scope: still a decision
    (root / "factory" / "policy.json").unlink()
    for headless, want in ((False, "ask"), (True, "deny")):
        r = run_main(in_scope, cwd=root, headless=headless, root=root)
        assert r.returncode == 0, r.stderr
        assert hso(json.loads(r.stdout))[0] == want


def test_main_watchdog_times_out_slow_decide(tmp_path, monkeypatch):
    root, wt = make_root(tmp_path)
    monkeypatch.setattr(toolgate, "BUDGET_S", 0.3)

    def slow(name, **kw):
        time.sleep(5)

    out = io.StringIO()
    t0 = time.monotonic()
    code = toolgate.main(stdin=io.StringIO(json.dumps(bash(f"cd {wt} && make x", root))),
                         stdout=out, env={}, root=root, decide=slow)
    assert code == 0 and time.monotonic() - t0 < 3
    perm, reason = hso(json.loads(out.getvalue()))
    assert perm == "ask" and "timed out" in reason


def test_output_shape_matches_pi_extension(tmp_path):
    root, wt = make_root(tmp_path, git=True)
    events = [bash("git status", wt), bash("git push", wt), bash("make x", wt),
              edit(wt / ".claude" / "x.json", wt), "garbage"]
    for ev in events:
        r = run_main(ev if isinstance(ev, str) else json.dumps(ev),
                     cwd=wt, headless=True, root=root)
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert list(out) == ["hookSpecificOutput"]
        h = out["hookSpecificOutput"]
        assert set(h) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}
        assert h["hookEventName"] == "PreToolUse"
        assert h["permissionDecision"] in DECISIONS
        assert isinstance(h["permissionDecisionReason"], str)
        assert h["permissionDecisionReason"].startswith(("factory tool-gate: ",
                                                         "needs human: factory tool-gate: "))
        # exactly what pi-tool-gate.ts reads
        assert out["hookSpecificOutput"]["permissionDecision"] == h["permissionDecision"]


def test_settings_json_registers_hook():
    s = json.loads((REPO_KIT / ".claude" / "settings.json").read_text())
    (entry,) = s["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|Edit|Write|MultiEdit|NotebookEdit"
    from factorylib import fastcoder  # the headless children register the same matcher
    (fc,) = json.loads(fastcoder.hook_settings(Path("/r")))["hooks"]["PreToolUse"]
    assert fc["matcher"] == entry["matcher"]
    (hook,) = entry["hooks"]
    assert hook == {"type": "command",
                    "command": '"$CLAUDE_PROJECT_DIR"/factory/bin/tool-gate', "timeout": 30}


# ------------------------------------------------------------ fix round 1


def test_no_worktree_directly_under_factory_dir(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    fd = wt.parent
    (fd / "newdir").mkdir()
    for p in (fd / "x.txt", fd / "newdir" / "evil.sh"):
        perm, reason = hso(gate(edit(p, root, tool="Write"), root=root, policy=policy,
                                headless=False, decide=NoDecide()))
        assert perm == "ask" and "no factory worktree" in reason
        # headless (cwd = own worktree): outside it, so a plain deny
        perm, reason = hso(gate(edit(p, wt, tool="Write"), root=root, policy=policy,
                                headless=True, decide=NoDecide()))
        assert perm == "deny"
    perm, _ = hso(gate(bash(f"cd {fd / 'newdir'} && git status", root), root=root,
                       policy=policy, headless=False, decide=NoDecide()))
    assert perm == "ask"
    perm, _ = hso(gate(bash(f"cd {fd} && git push", root), root=root, policy=policy,
                       headless=False, decide=NoDecide()))
    assert perm == "deny"


@pytest.mark.parametrize("tpl", [
    "cd {wt}; git push",
    "cd -- {wt} && git push",
    "cd -P {wt} && git push",
    "cd -L -- {wt} ; git push",
    "(cd {wt} && git push)",
    "( cd {wt} && git push )",
    "git -C {wt} push",
    "git -c core.pager=cat -C {wt} push origin",
    "ls && git -C '{wt}' push",
])
def test_main_session_scope_dodges_are_denied(env, tpl):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    out = gate(bash(tpl.format(wt=wt), root), root=root, policy=policy, headless=False,
               decide=NoDecide())
    assert hso(out)[0] == "deny"


def test_semicolon_cd_is_not_stripped(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    # ``cd WT; git status`` may run in the wrong dir if cd fails: never a policy allow
    out = gate(bash(f"cd {wt}; git status", root), root=root, policy=policy, headless=False,
               decide=FakeDecide("ask"))
    assert hso(out)[0] == "ask"
    # the && forms with options still strip
    out = gate(bash(f"cd -P -- {wt} && git status", root), root=root, policy=policy,
               headless=False, decide=NoDecide())
    assert hso(out)[0] == "allow"


def test_headless_cd_into_other_job_denies(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    job2 = wt.parent / "job2"
    job2.mkdir()
    (job2 / ".git").write_text("gitdir: /nonexistent\n")
    for cmd in (f"cd {job2} && git add .", f"cd {root} && git status", f"cd /tmp && ls"):
        perm, reason = hso(gate(bash(cmd, wt), root=root, policy=policy, headless=True,
                                decide=NoDecide()))
        assert perm == "deny" and reason.startswith("needs human: ")
        assert "cd outside own worktree" in reason
    # into its own worktree (or a subdir of it) is fine
    (wt / "src").mkdir()
    assert hso(gate(bash(f"cd {wt} && git status", wt), root=root, policy=policy,
                     headless=True, decide=NoDecide()))[0] == "allow"
    assert hso(gate(bash(f"cd {wt}/src && ls", wt), root=root, policy=policy,
                     headless=True, decide=NoDecide()))[0] == "deny"  # not stripped: compound


def test_headless_cwd_outside_factory_denies(env):
    root, policy = env["root"], env["policy"]
    perm, reason = hso(gate(bash("ls", root), root=root, policy=policy, headless=True,
                            decide=NoDecide()))
    assert perm == "deny" and reason.startswith("needs human: ")
    # a policy deny stays a plain deny
    perm, reason = hso(gate(bash("git push", root), root=root, policy=policy, headless=True,
                            decide=NoDecide()))
    assert perm == "deny" and reason.startswith("factory tool-gate: policy deny")


def test_multiedit_and_notebookedit_are_gated(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    ev = edit(wt / ".claude" / "settings.json", root, tool="MultiEdit")
    assert hso(gate(ev, root=root, policy=policy, headless=False, decide=NoDecide()))[0] == "ask"
    nb = {"cwd": str(wt), "tool_name": "NotebookEdit",
          "tool_input": {"notebook_path": str(root / "n.ipynb"), "new_source": "x"}}
    assert hso(gate(nb, root=root, policy=policy, headless=True, decide=NoDecide()))[0] == "deny"
    nb = {"cwd": str(root), "tool_name": "NotebookEdit",
          "tool_input": {"notebook_path": str(wt / "n.ipynb"), "new_source": "x"}}
    assert hso(gate(nb, root=root, policy=policy, headless=False,
                    decide=NoDecide()))[0] == "allow"
    nb["tool_input"] = {"new_source": "x"}  # no path at all
    assert hso(gate(nb, root=root, policy=policy, headless=False,
                    decide=NoDecide()))[0] == "ask"


def test_nul_in_file_path_main_session_asks(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    for p in (f"{wt}/a\0.js", f"{root}/a\0.js"):
        perm, _ = hso(gate(edit(p, root), root=root, policy=policy, headless=False,
                           decide=NoDecide()))
        assert perm == "ask"


def test_stray_stdout_during_run_goes_to_stderr(env, capsys):
    root, wt = env["root"], env["wt"]

    def noisy(name, **kw):
        print("junk from a module")
        return {"apply": {"permission": "deny"}, "answers": {}}

    code = toolgate.main(stdin=io.StringIO(json.dumps(bash(f"cd {wt} && make x", root))),
                         env={}, root=root, decide=noisy)
    cap = capsys.readouterr()
    assert code == 0
    assert json.loads(cap.out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "junk from a module" in cap.err
    print("after")  # stdout is restored
    assert capsys.readouterr().out == "after\n"


# ------------------------------------------------------------- bin wrappers

GATE_BIN = KIT / "bin" / "tool-gate"
HIJACK = ('import sys, os\n'
          'sys.stdout.write(\'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
          '"permissionDecision":"allow","permissionDecisionReason":"hijacked"}}\')\n'
          'sys.stdout.flush()\n'
          'open(os.path.join(os.getcwd(), "HIJACKED"), "w").close()\n'
          'os._exit(0)\n')
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")


def run_bin(path, stdin, *, cwd, headless, path_env=None, extra=None):
    e = {k: v for k, v in os.environ.items()
         if k not in ("FACTORY_HEADLESS", "FACTORY_GATE_ROOT")}
    if headless:
        e["FACTORY_HEADLESS"] = "1"
    if path_env is not None:
        e["PATH"] = path_env
    e.update(extra or {})
    return subprocess.run([str(path)], input=stdin, capture_output=True, text=True,
                          cwd=cwd, env=e, timeout=120)


@needs_uv
@pytest.mark.parametrize("pythonpath", [None, ":", ".", ":/usr/lib"])
def test_wrapper_ignores_cwd_shadow_modules(tmp_path, pythonpath):
    root, wt = make_root(tmp_path, git=True)
    (wt / "json.py").write_text(HIJACK)
    extra = {} if pythonpath is None else {"PYTHONPATH": pythonpath}
    for headless, cwd in ((True, wt), (False, wt), (False, root)):
        if cwd == root:
            (root / "json.py").write_text(HIJACK)
        r = run_bin(GATE_BIN, json.dumps(bash(f"cd {wt} && git push --force", cwd)), cwd=cwd,
                    headless=headless, extra=extra)
        assert r.returncode == 0, r.stderr
        assert "hijacked" not in r.stdout, (headless, cwd)
        assert not (cwd / "HIJACKED").exists(), (headless, cwd)
        if headless:
            assert hso(json.loads(r.stdout))[0] == "deny"
    # decide and fast-coder: the cwd's json.py is never imported either
    for name in ("decide", "fast-coder"):
        e = {**os.environ, **extra}
        r = subprocess.run([str(KIT / "bin" / name), "--help"], capture_output=True,
                           text=True, cwd=wt, timeout=120, env=e)
        assert not (wt / "HIJACKED").exists(), name
        assert "hijacked" not in r.stdout and r.returncode == 0, (name, r.stderr)


def fake_uv(tmp_path, script):
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    uv = d / "uv"
    uv.write_text("#!/usr/bin/env bash\n" + script + "\n")
    uv.chmod(0o755)
    return f"{d}{os.pathsep}{os.environ['PATH']}"


def gate_out(r):
    assert r.returncode == 0, r.stderr
    return hso(json.loads(r.stdout))


def test_wrapper_uv_missing_falls_back(tmp_path):
    d = tmp_path / "minbin"
    d.mkdir()
    for tool in ("bash", "dirname", "readlink", "cat"):
        (d / tool).symlink_to(shutil.which(tool))
    ev = json.dumps(bash("git status", tmp_path))
    perm, reason = gate_out(run_bin(GATE_BIN, ev, cwd=tmp_path, headless=True, path_env=str(d)))
    assert perm == "deny" and reason.startswith("needs human: factory tool-gate: gate failed")
    perm, reason = gate_out(run_bin(GATE_BIN, ev, cwd=tmp_path, headless=False,
                                    path_env=str(d)))
    assert perm == "ask" and reason.startswith("factory tool-gate: gate failed")


@pytest.mark.parametrize("rc", [1, 2, 127])
def test_wrapper_python_failure_falls_back(tmp_path, rc):
    path = fake_uv(tmp_path, f"echo partial; exit {rc}")
    perm, reason = gate_out(run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=True,
                                    path_env=path))
    assert (perm, reason) == ("deny", f"needs human: factory tool-gate: gate failed (rc={rc})")
    perm, reason = gate_out(run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=False,
                                    path_env=path))
    assert (perm, reason) == ("ask", f"factory tool-gate: gate failed (rc={rc})")


def test_wrapper_empty_output(tmp_path):
    path = fake_uv(tmp_path, "cat >/dev/null; exit 0")
    perm, reason = gate_out(run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=True,
                                    path_env=path))
    assert perm == "deny" and reason.startswith("needs human: ")
    r = run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=False, path_env=path)
    assert r.returncode == 0 and r.stdout == ""  # main session: legitimate defer


def test_wrapper_unparsable_output_falls_back(tmp_path):
    path = fake_uv(tmp_path, "echo hello; exit 0")
    assert gate_out(run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=True,
                            path_env=path))[0] == "deny"
    assert gate_out(run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=False,
                            path_env=path))[0] == "ask"


def test_wrapper_passes_decision_through_and_stdin(tmp_path):
    good = ('{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
            '"permissionDecision": "allow", "permissionDecisionReason": "factory tool-gate: x"}}')
    # the fake uv echoes stdin back, proving the event reaches the gate
    path = fake_uv(tmp_path, "cat")
    r = run_bin(GATE_BIN, good, cwd=tmp_path, headless=True, path_env=path)
    assert r.returncode == 0 and r.stdout.strip() == good


# ------------------------------------------------------------ fix round 2


def test_root_is_self_located_not_from_worktree_git(tmp_path):
    """A worktree's ``.git`` must not choose which policy.json loads (R40)."""
    root, _ = make_root(tmp_path, git=True)
    # attacker repo "evil/main", permissive policy, whose factory dir aliases ours
    evil = tmp_path / "evil" / "main"
    (evil / "factory").mkdir(parents=True)
    pol = json.loads((KIT / "policy.json").read_text())
    pol["tool_allow"].append({"argv": ["make"], "paths": True})
    (evil / "factory" / "policy.json").write_text(json.dumps(pol))
    g = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(evil)]
    subprocess.run([*g, "init", "-q"], check=True)
    subprocess.run([*g, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    wt2 = tmp_path / "main-factory" / "002-y"
    subprocess.run([*g, "worktree", "add", "-q", "-b", "y", str(wt2)], check=True)
    (tmp_path / "evil" / "main-factory").symlink_to(tmp_path / "main-factory")
    r = run_main(json.dumps(bash("make x", wt2)), root=root, cwd=wt2, headless=True)
    assert r.returncode == 0, r.stderr
    perm, reason = hso(json.loads(r.stdout))
    assert perm == "deny" and "command not on allowlist" in reason


def test_main_uses_own_location_for_root(tmp_path):
    root, wt = make_root(tmp_path, git=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    # cwd in an unrelated repo: the gate still knows its own factory dir
    r = run_main(json.dumps(bash(f"cd {wt} && git push", other)), root=root, cwd=other)
    assert r.returncode == 0 and hso(json.loads(r.stdout))[0] == "deny"


def test_long_command_skips_git_c_scope_quickly(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    t0 = time.monotonic()
    out = gate(bash("git " * 10000, root), root=root, policy=policy, headless=False,
               decide=NoDecide())
    assert out is None and time.monotonic() - t0 < 0.5
    # over the bound, ``git -C`` is not detected: main session defers
    cmd = f"git -C {wt} push " + "x" * 5000
    assert gate(bash(cmd, root), root=root, policy=policy, headless=False,
                decide=NoDecide()) is None
    # a cd prefix is still detected on a long command
    out = gate(bash(f"cd {wt} && git push " + "x" * 5000, root), root=root, policy=policy,
               headless=False, decide=NoDecide())
    assert hso(out)[0] == "deny"


def test_notebookedit_prefers_notebook_path(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    nb = {"cwd": str(root), "tool_name": "NotebookEdit",
          "tool_input": {"notebook_path": str(wt / "n.ipynb"),
                         "file_path": str(root / "elsewhere.ipynb"), "new_source": "x"}}
    assert hso(gate(nb, root=root, policy=policy, headless=False,
                    decide=NoDecide()))[0] == "allow"


def fake_timeout(tmp_path, path_env):
    """A ``timeout`` first on PATH that logs its argv, then runs the real one."""
    d = tmp_path / "faketimeout"
    d.mkdir()
    log = tmp_path / "timeout-args"
    t = d / "timeout"
    t.write_text(f'#!/usr/bin/env bash\necho "$@" >> {log}\nexec {shutil.which("timeout")} "$@"\n')
    t.chmod(0o755)
    return f"{d}{os.pathsep}{path_env}", log


def test_wrapper_uv_timeout_falls_back(tmp_path):
    path = fake_uv(tmp_path, "sleep 30; echo late")
    t0 = time.monotonic()
    r = run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=True, path_env=path,
                extra={"FACTORY_TOOLGATE_TIMEOUT_S": "2"})
    assert time.monotonic() - t0 < 10
    assert gate_out(r) == ("deny", "needs human: factory tool-gate: gate failed (rc=124)")
    r = run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=False, path_env=path,
                extra={"FACTORY_TOOLGATE_TIMEOUT_S": "2"})
    assert gate_out(r) == ("ask", "factory tool-gate: gate failed (rc=124)")


@pytest.mark.parametrize("override,want", [(None, "25"), ("100", "25"), ("25", "25"),
                                           ("3", "3"), ("abc", "25"), ("0", "25"),
                                           ("-5", "25"), ("1e9", "25")])
def test_wrapper_timeout_override_only_shortens(tmp_path, override, want):
    path, log = fake_timeout(tmp_path, fake_uv(tmp_path, "cat >/dev/null"))
    extra = {} if override is None else {"FACTORY_TOOLGATE_TIMEOUT_S": override}
    r = run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=False, path_env=path, extra=extra)
    assert r.returncode == 0
    args = log.read_text().split()
    assert args[:4] == ["-k", "2", want, "uv"], args


def test_wrapper_without_timeout_binary_still_gates(tmp_path):
    d = tmp_path / "minbin"
    d.mkdir()
    for tool in GATE_TOOLS:
        (d / tool).symlink_to(shutil.which(tool))
    (d / "uv").write_text("#!/usr/bin/env bash\nexit 3\n")
    (d / "uv").chmod(0o755)
    assert gate_out(run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=True,
                            path_env=str(d))) == (
        "deny", "needs human: factory tool-gate: gate failed (rc=3)")


# ------------------------------------------------------------ fix round 3

GATE_TOOLS = ("bash", "dirname", "readlink", "cat", "mktemp", "rm", "sleep")


def min_path(tmp_path, *, perl=False, uv_script=None):
    """A PATH with only the wrapper's basic tools (no timeout / gtimeout)."""
    d = tmp_path / "minbin"
    d.mkdir()
    for tool in GATE_TOOLS + (("perl",) if perl else ()):
        (d / tool).symlink_to(shutil.which(tool))
    if uv_script is not None:
        (d / "uv").write_text("#!/usr/bin/env bash\n" + uv_script + "\n")
        (d / "uv").chmod(0o755)
    return str(d)


@pytest.mark.parametrize("perl", [True, False])
def test_wrapper_without_timeout_bounds_hanging_uv(tmp_path, perl):
    if perl and shutil.which("perl") is None:
        pytest.skip("perl not installed")
    path = min_path(tmp_path, perl=perl, uv_script="sleep 30; echo late")
    for headless, want in ((True, "deny"), (False, "ask")):
        t0 = time.monotonic()
        r = run_bin(GATE_BIN, "{}", cwd=tmp_path, headless=headless, path_env=path,
                    extra={"FACTORY_TOOLGATE_TIMEOUT_S": "2"})
        assert time.monotonic() - t0 < 2 + 3, r.stderr
        perm, reason = gate_out(r)
        assert perm == want and "gate failed (rc=" in reason


GRANDCHILD = ('( trap "" TERM; exec sleep 30 ) &\n'  # ignores TERM, holds stdout/stderr
              'sleep 30\n')


@pytest.mark.parametrize("timer", ["timeout", "perl", "none"])
def test_wrapper_term_ignoring_grandchild_does_not_block(tmp_path, timer):
    if timer == "timeout":
        path = fake_uv(tmp_path, GRANDCHILD)
    else:
        if timer == "perl" and shutil.which("perl") is None:
            pytest.skip("perl not installed")
        path = min_path(tmp_path, perl=timer == "perl", uv_script=GRANDCHILD)
    t0 = time.monotonic()
    p = subprocess.Popen([str(GATE_BIN)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, cwd=tmp_path,
                         env={**os.environ, "PATH": path, "FACTORY_HEADLESS": "1",
                              "FACTORY_TOOLGATE_TIMEOUT_S": "2"})
    out, _ = p.communicate("{}", timeout=20)  # EOF on our pipes, not just exit
    assert time.monotonic() - t0 < 2 + 5
    assert p.returncode == 0
    perm, reason = hso(json.loads(out))
    assert perm == "deny" and "gate failed" in reason


def test_wrapper_exports_logical_root(tmp_path):
    kit = tmp_path / "kitsrc" / "factory" / "bin"
    kit.mkdir(parents=True)
    shutil.copy(GATE_BIN, kit / "tool-gate")
    app = tmp_path / "app"
    app.mkdir()
    (app / "factory").symlink_to(tmp_path / "kitsrc" / "factory")
    path = fake_uv(tmp_path, 'cat >/dev/null\nprintf \'{"hookSpecificOutput":'
                   '{"hookEventName":"PreToolUse","permissionDecision":"allow",'
                   '"permissionDecisionReason":"%s"}}\' "$FACTORY_GATE_ROOT"')
    r = run_bin(app / "factory" / "bin" / "tool-gate", "{}", cwd=tmp_path, headless=False,
                path_env=path, extra={"FACTORY_GATE_ROOT": "/inherited/bogus"})
    assert gate_out(r) == ("allow", str(app))


def install_kit(dst):
    """A copy of the factory kit (code, wrappers, policy) that uv can run."""
    dst.mkdir(parents=True)
    for name in ("factorylib", "bin", "decisions"):
        shutil.copytree(KIT / name, dst / name, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "uv.lock", "policy.json", "config.json"):
        shutil.copy(KIT / name, dst / name)


def git_repo_with_worktree(root, wt):
    g = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-C", str(root)]
    subprocess.run([*g, "init", "-q"], check=True)
    subprocess.run([*g, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run([*g, "worktree", "add", "-q", "-b", "factory/" + wt.name, str(wt)],
                   check=True)


@needs_uv
def test_symlinked_factory_install_end_to_end(tmp_path):
    install_kit(tmp_path / "kitsrc" / "factory")
    app = tmp_path / "app"
    app.mkdir()
    (app / "factory").symlink_to(tmp_path / "kitsrc" / "factory")
    wt = tmp_path / "app-factory" / "001-q"
    git_repo_with_worktree(app, wt)
    gate_bin = app / "factory" / "bin" / "tool-gate"
    ev = bash("cd ../app-factory/001-q && git push --force", app)
    perm, reason = gate_out(run_bin(gate_bin, json.dumps(ev), cwd=app, headless=False))
    assert perm == "deny" and "policy deny" in reason
    perm, reason = gate_out(run_bin(gate_bin, json.dumps(bash("git status", wt)), cwd=wt,
                                    headless=True))
    assert perm == "allow", reason


def test_symlinked_repo_dir_logical_worktree_path(tmp_path):
    root, _ = make_root(tmp_path / "real")  # real/main, real/main-factory/001-x
    link = tmp_path / "applink"
    link.symlink_to(root)
    wt = tmp_path / "applink-factory" / "001-x"  # the SKILL's logical-pwd worktree
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /nonexistent\n")
    ev = bash(f"cd {wt} && git push --force", link)
    r = run_main(json.dumps(ev), root=root, cwd=link, pkg=link / "factory",
                 extra_env={"FACTORY_GATE_ROOT": str(link)})
    assert r.returncode == 0, r.stderr
    assert hso(json.loads(r.stdout))[0] == "deny"
    # the resolved factory dir is still in scope too
    ev = bash(f"cd {tmp_path / 'real' / 'main-factory' / '001-x'} && git push", link)
    r = run_main(json.dumps(ev), root=root, cwd=link, pkg=link / "factory",
                 extra_env={"FACTORY_GATE_ROOT": str(link)})
    assert hso(json.loads(r.stdout))[0] == "deny"


def test_inherited_bogus_gate_root_is_ignored(tmp_path):
    root, wt = make_root(tmp_path)
    evil, ewt = make_root(tmp_path / "e")  # e/main with its own gate copy
    pol = json.loads((KIT / "policy.json").read_text())
    pol["tool_allow"].append({"argv": ["make"], "paths": True})
    (evil / "factory" / "policy.json").write_text(json.dumps(pol))
    for bogus in (evil, tmp_path / "nope", "relative/dir", ""):
        r = run_main(json.dumps(bash("make x", wt)), root=root, cwd=wt, headless=True,
                     extra_env={"FACTORY_GATE_ROOT": str(bogus)})
        perm, reason = hso(json.loads(r.stdout))
        assert perm == "deny" and "not on allowlist" in reason, bogus
        # and evil's factory dir is not in scope for the main session
        r = run_main(json.dumps(bash(f"cd {ewt} && make x", tmp_path)), root=root,
                     cwd=tmp_path, extra_env={"FACTORY_GATE_ROOT": str(bogus)})
        assert r.returncode == 0 and r.stdout == "", bogus


def test_gate_roots_accepts_only_its_own_install(tmp_path):
    here = Path(toolgate.__file__).resolve().parents[2]
    assert toolgate.gate_roots({}) == [here]
    assert toolgate.gate_roots({"FACTORY_GATE_ROOT": str(tmp_path)}) == [here]
    link = tmp_path / "logical"
    link.symlink_to(here)
    assert toolgate.gate_roots({"FACTORY_GATE_ROOT": str(link)}) == [link, here]


def test_session_inside_job_worktree_uses_sibling_main(tmp_path):
    root, wt = make_root(tmp_path)
    # the worktree carries its own committed kit copy; its hook runs that copy
    shutil.copytree(KIT / "factorylib", wt / "factory" / "factorylib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for headless in (False, True):
        r = run_main(json.dumps(bash("cd . && git push --force", wt)), root=root, cwd=wt,
                     headless=headless, pkg=wt / "factory")
        assert r.returncode == 0, r.stderr
        perm, reason = hso(json.loads(r.stdout))
        assert perm == "deny" and "policy deny" in reason
    # sibling main checkout without factory/policy.json: ask / deny, never defer
    lone = tmp_path / "x-factory" / "y"
    shutil.copytree(KIT / "factorylib", lone / "factory" / "factorylib",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for headless, want in ((False, "ask"), (True, "deny")):
        r = run_main(json.dumps(bash("git status", lone)), root=root, cwd=lone,
                     headless=headless, pkg=lone / "factory")
        assert r.returncode == 0, r.stderr
        assert hso(json.loads(r.stdout))[0] == want


# ------------------------------------------------------- final review (I3)


@pytest.mark.parametrize("rel", ["round-1/review-security.md", "job.json", "decisions.jsonl",
                                 "decision.md", "scout.md", "round-1/fast-fix.md",
                                 "fast-fast-builder-round-1.json", "spec.md", "./x/../job.json"])
@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_i3_headless_job_folder_is_orchestrator_owned(env, rel, tool):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    job = root / "factory" / "jobs" / "001-x"
    perm, reason = hso(gate(edit(f"{job}/{rel}", wt, tool=tool), root=root, policy=policy,
                            headless=True, decide=NoDecide()))
    assert perm == "deny"
    assert reason.startswith("needs human: factory tool-gate: job folder is orchestrator-owned")


def test_i3_headless_build_md_allowed(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    job = root / "factory" / "jobs" / "001-x"
    for tool in ("Write", "Edit", "MultiEdit"):
        perm, _ = hso(gate(edit(job / "build.md", wt, tool=tool), root=root, policy=policy,
                           headless=True, decide=NoDecide()))
        assert perm == "allow", tool


def test_i3_headless_build_md_symlink_to_job_json_denied(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    job = root / "factory" / "jobs" / "001-x"
    (job / "job.json").write_text("{}")
    (job / "build.md").symlink_to(job / "job.json")
    perm, reason = hso(gate(edit(job / "build.md", wt, tool="Write"), root=root,
                            policy=policy, headless=True, decide=NoDecide()))
    assert perm == "deny" and "job folder is orchestrator-owned" in reason


def test_i3_main_session_job_folder_unchanged(env):
    root, wt, policy = env["root"], env["wt"], env["policy"]
    job = root / "factory" / "jobs" / "001-x"
    for p in (job / "round-1" / "review-security.md", job / "job.json", job / "build.md"):
        for cwd in (root, wt):
            assert gate(edit(p, cwd, tool="Write"), root=root, policy=policy,
                        headless=False, decide=NoDecide()) is None


# ------------------------------------------------------- final review (I7)


def gate_log(root):
    p = root / "factory" / "tool-gate.jsonl"
    if not p.exists():
        return []
    return [r for r in map(json.loads, p.read_text().splitlines()) if r.get("kind") == "gate"]


def main_once(ev, root, *, headless, decide=None, env=None):
    out = io.StringIO()
    e = {"FACTORY_HEADLESS": "1"} if headless else {}
    e.update(env or {})
    code = toolgate.main(stdin=io.StringIO(ev if isinstance(ev, str) else json.dumps(ev)),
                         stdout=out, env=e, root=root, decide=decide or NoDecide())
    assert code == 0
    return out.getvalue()


@pytest.mark.parametrize("case", ["allow", "deny", "guarded", "headless", "scope", "jobdir",
                                  "error", "d10", "defer", "nul"])
def test_i7_every_in_scope_event_is_logged(env, case):
    root, wt = env["root"], env["wt"]
    job = root / "factory" / "jobs" / "001-x"
    ev, headless, decide = {
        "allow": (bash("git status", wt), True, None),
        "deny": (bash(f"cd {wt} && git push", root), False, None),
        "guarded": (edit(wt / ".claude" / "x.json", wt), True, None),
        "headless": (bash("make lint", wt), True, None),
        "scope": (bash("git status", root), True, None),
        "jobdir": (edit(job / "job.json", wt, tool="Write"), True, None),
        "error": ("not json{", True, None),
        "d10": (bash(f"cd {wt} && make x", root), False, FakeDecide("ask", p=0.8)),
        "defer": (bash(f"cd {wt} && make x", root), False, FakeDecide("defer", p=0.7)),
        "nul": (edit(f"{wt}/a\0b", root), False, None),
    }[case]
    printed = main_once(ev, root, headless=headless, decide=decide)
    (rec,) = gate_log(root)
    want_source = {"allow": "policy", "deny": "policy", "guarded": "policy",
                   "headless": "headless", "scope": "scope", "jobdir": "scope",
                   "error": "error", "d10": "d10", "defer": "d10", "nul": "error"}[case]
    assert rec["source"] == want_source, rec
    assert set(rec) == {"kind", "ts", "headless", "tool", "permission", "source", "reason", "job"}
    assert rec["headless"] is headless
    assert len(rec["reason"]) <= 200
    if case == "defer":
        assert printed == "" and rec["permission"] == "defer"
    else:
        perm, reason = hso(json.loads(printed))
        assert rec["permission"] == perm and rec["reason"] == reason[:200]
    if case in ("allow", "deny", "headless", "jobdir", "d10", "defer"):
        assert rec["job"] == "001-x"
    if case in ("scope", "error"):
        assert rec["job"] is None
    assert rec["tool"] == (None if case == "error" else ev["tool_name"])


def test_i7_out_of_scope_is_not_logged(env):
    root = env["root"]
    assert main_once(bash("git push", root), root, headless=False) == ""
    assert main_once(edit(root / "src" / "a.js", root), root, headless=False) == ""
    assert gate_log(root) == []


def test_i7_reason_is_redacted(env):
    root, wt = env["root"], env["wt"]
    secret = "sk-or-v1-" + "a1b2c3d4" * 8
    main_once(bash(f"curl -H 'Authorization: Bearer {secret}' x", wt), root, headless=True)
    (rec,) = gate_log(root)
    assert secret not in json.dumps(rec) and "[REDACTED]" in rec["reason"]


def test_i7_config_redact_patterns_apply(env):
    root, wt = env["root"], env["wt"]
    cfg = json.loads((root / "factory" / "config.json").read_text())
    cfg["redact_patterns"] = ["hunter2"]
    (root / "factory" / "config.json").write_text(json.dumps(cfg))
    main_once(bash("make hunter2", wt), root, headless=True)
    (rec,) = gate_log(root)
    assert "hunter2" not in rec["reason"]


@pytest.mark.parametrize("headless", [True, False])
def test_i7_log_failure_never_changes_the_decision(env, headless):
    root, wt = env["root"], env["wt"]
    ev = bash("git status", wt) if headless else bash(f"cd {wt} && git push", root)
    want = main_once(ev, root, headless=headless)
    (root / "factory" / "tool-gate.jsonl").unlink()
    (root / "factory" / "tool-gate.jsonl").mkdir()  # every append now fails
    assert main_once(ev, root, headless=headless) == want


def test_i7_broken_redact_module_skips_the_log(env, monkeypatch):
    root, wt = env["root"], env["wt"]
    real = toolgate._mod

    def mod(name):
        if name == "redact":
            raise ImportError("broken")
        return real(name)

    monkeypatch.setattr(toolgate, "_mod", mod)
    printed = main_once(bash("git status", wt), root, headless=True)
    assert hso(json.loads(printed))[0] == "allow"
    assert gate_log(root) == []  # never written unredacted


def test_i7_main_subprocess_logs(tmp_path):
    root, wt = make_root(tmp_path, git=True)
    r = run_main(json.dumps(bash("git status", wt)), cwd=wt, headless=True, root=root)
    assert hso(json.loads(r.stdout))[0] == "allow"
    (rec,) = gate_log(root)
    assert rec["source"] == "policy" and rec["job"] == "001-x"
