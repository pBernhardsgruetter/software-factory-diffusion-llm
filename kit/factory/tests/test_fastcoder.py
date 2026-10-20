import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from factorylib import fastcoder
from factorylib.paths import load_job

KIT = Path(__file__).resolve().parent.parent
KEY = "sk-or-v1-test-not-real-0123456789"
ROUTING_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL", "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL")

# S2-shaped ``claude -p --output-format json`` result (run 1 values).
S2_RESULT = {
    "is_error": False,
    "num_turns": 7,
    "stop_reason": "end_turn",
    "session_id": None,  # filled from argv by the stub
    "total_cost_usd": 0.2242005,
    "usage": {"input_tokens": 29066, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 115091, "output_tokens": 853},
    "modelUsage": {"inception/mercury-3": {
        "inputTokens": 29066, "outputTokens": 853, "cacheReadInputTokens": 115091,
        "cacheCreationInputTokens": 0, "costUSD": 0.2242005}},
    "permission_denials": [],
    "subtype": "success",
    "result": "# Scout\n\n## Relevant files\n- src/main.ts: entry point\n",
    "type": "result",
}

STUB = r'''#!{python}
import json, os, sys, time
log = os.environ["STUB_LOG"]
if os.environ.get("STUB_PIDFILE"):
    with open(os.environ["STUB_PIDFILE"], "w") as f:
        f.write(str(os.getpid()))
rec = {{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
       "stdin": sys.stdin.read()}}
with open(log, "a") as f:
    f.write(json.dumps(rec) + "\n")
if os.environ.get("STUB_SLEEP"):
    time.sleep(float(os.environ["STUB_SLEEP"]))
out = json.loads(os.environ["STUB_RESULT"])
argv = sys.argv[1:]
for flag in ("--session-id", "--resume"):
    if flag in argv:
        out["session_id"] = argv[argv.index(flag) + 1]
print(json.dumps(out))
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
'''


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A main checkout with a job whose worktree is a linked git worktree."""
    root = tmp_path / "main"
    (root / "factory").mkdir(parents=True)
    git(root, "init", "-b", "main")
    (root / "README.md").write_text("hi\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "init")
    shutil.copy(KIT / "config.json", root / "factory" / "config.json")
    wt = tmp_path / "main-factory" / "001-x"
    git(root, "worktree", "add", str(wt), "-b", "factory/001-x")
    folder = root / "factory" / "jobs" / "001-x"
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({
        "id": "001-x", "feature": "add a dark mode toggle", "branch": "factory/001-x",
        "worktree": str(wt.resolve()),
    }))
    gate = root / "factory" / "bin" / "tool-gate"
    gate.parent.mkdir(parents=True)
    gate.write_text("#!/bin/sh\nexit 0\n")
    gate.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "claude"
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)
    for dep in ("socat", "bwrap"):  # the sandbox preflight sees them (subprocess tests too)
        (bindir / dep).write_text("#!/bin/sh\nexit 0\n")
        (bindir / dep).chmod(0o755)
    log = tmp_path / "stub.jsonl"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_RESULT", json.dumps(S2_RESULT))
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
                 "FACTORY_HEADLESS", "STUB_SLEEP", "STUB_EXIT", "STUB_PIDFILE",
                 "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", *ROUTING_VARS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(wt)
    return {"root": root.resolve(), "wt": wt.resolve(), "log": log,
            "folder": folder.resolve(), "tmp": tmp_path, "gate": gate.resolve(),
            "bindir": bindir}


def run(capsys, *argv):
    code = fastcoder.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def calls(env):
    if not env["log"].exists():
        return []
    return [json.loads(x) for x in env["log"].read_text().splitlines()]


def flag(argv, name):
    return argv[argv.index(name) + 1]


def set_cfg(env, **fast):
    path = env["root"] / "factory" / "config.json"
    cfg = json.loads(path.read_text())
    cfg["fast_coder"].update(fast)
    path.write_text(json.dumps(cfg))


# --- brief tests -------------------------------------------------------------

def test_child_env_isolated(env, capsys):
    code, out, _ = run(capsys, "fast-builder", "--job", "001-x", "--port", "5174")
    assert code == 0
    assert "ANTHROPIC_BASE_URL" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    assert "FACTORY_HEADLESS" not in os.environ
    (call,) = calls(env)
    child = call["env"]
    assert child["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert child["ANTHROPIC_AUTH_TOKEN"] == KEY
    assert child["ANTHROPIC_API_KEY"] == ""
    assert child["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "inception/mercury-3"
    assert child["CLAUDE_CODE_SUBAGENT_MODEL"] == "inception/mercury-3"
    assert child["FACTORY_HEADLESS"] == "1"
    # The key never reaches argv or our stdout/stderr.
    assert not any(KEY in a for a in call["argv"])
    assert KEY not in out


def test_scout_tools_readonly(env, capsys):
    run(capsys, "scout", "--job", "001-x")
    run(capsys, "fast-builder", "--job", "001-x")
    run(capsys, "fast-fixer", "--job", "001-x")
    scout, builder, fixer = calls(env)
    assert flag(scout["argv"], "--allowedTools") == "Read,Grep,Glob"
    assert flag(builder["argv"], "--allowedTools") == "Read,Grep,Glob,Edit,Write,Bash"
    assert flag(fixer["argv"], "--allowedTools") == "Read,Grep,Glob,Edit,Write,Bash"
    for c in (scout, builder, fixer):
        assert flag(c["argv"], "--disallowedTools") == "Agent,WebFetch,WebSearch"


def test_new_session_uses_session_id_resume_uses_resume(env, capsys):
    code, out, _ = run(capsys, "fast-builder", "--job", "001-x")
    assert code == 0
    first = json.loads(out)
    code, out, _ = run(capsys, "fast-builder", "--job", "001-x", "--round", "2",
                       "--resume", first["session_id"])
    second = json.loads(out)
    new, resumed = calls(env)
    sid = flag(new["argv"], "--session-id")
    assert "--resume" not in new["argv"]
    import uuid
    assert str(uuid.UUID(sid)) == sid
    assert first["session_id"] == sid
    assert flag(resumed["argv"], "--resume") == sid
    assert "--session-id" not in resumed["argv"]
    assert second["session_id"] == sid


def test_settings_hook_points_to_main_root_toolgate(env, capsys):
    run(capsys, "fast-builder", "--job", "001-x")
    (call,) = calls(env)
    settings = json.loads(flag(call["argv"], "--settings"))
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash|Edit|Write|MultiEdit|NotebookEdit"
    (hook,) = entry["hooks"]
    gate = env["root"] / "factory" / "bin" / "tool-gate"
    assert hook == {"type": "command", "command": shlex.quote(str(gate)), "timeout": 30}
    # The hook is the main checkout's, not one under the worktree we ran from.
    assert str(env["wt"]) not in hook["command"]


def test_settings_hook_command_quotes_paths_with_spaces(tmp_path):
    root = tmp_path / "my repo"
    s = json.loads(fastcoder.hook_settings(root))
    cmd = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert shlex.split(cmd) == [str(root / "factory" / "bin" / "tool-gate")]


def test_output_json_and_transcript_written(env, capsys):
    code, out, err = run(capsys, "fast-builder", "--job", "001-x", "--round", "3")
    assert code == 0
    rec = json.loads(out)
    assert set(rec) == {"role", "session_id", "exit_code", "cost_usd",
                        "cost_estimate", "transcript", "error"}
    assert rec["role"] == "fast-builder"
    assert rec["exit_code"] == 0 and rec["error"] is None
    assert rec["cost_estimate"] is True
    transcript = env["folder"] / "fast-fast-builder-round-3.json"
    assert rec["transcript"] == str(transcript)
    data = json.loads(transcript.read_text())
    assert data["session_id"] == rec["session_id"]
    assert data["total_cost_usd"] == 0.2242005


def test_missing_key_exits_2_without_spawning(env, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    code, out, err = run(capsys, "scout", "--job", "001-x")
    assert code == 2 and out == ""
    assert "OPENROUTER_API_KEY" in err
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    code, out, err = run(capsys, "scout", "--job", "001-x")
    assert code == 2 and out == ""
    assert calls(env) == []


# --- controller extras -------------------------------------------------------

def test_exact_s2_flag_set_and_invocation(env, capsys):
    run(capsys, "fast-builder", "--job", "001-x")
    (call,) = calls(env)
    argv = call["argv"]
    assert argv[0] == "-p"
    assert flag(argv, "--model") == "inception/mercury-3"
    assert flag(argv, "--permission-mode") == "dontAsk"
    assert flag(argv, "--setting-sources") == "local"
    assert flag(argv, "--output-format") == "json"
    prompt = (KIT / "fast-agents" / "fast-builder.md").read_text()
    assert flag(argv, "--append-system-prompt") == prompt
    assert "--max-turns" not in argv
    assert call["stdin"] == ""
    assert Path(call["cwd"]).resolve() == env["wt"]


def test_child_runs_under_timeout(env, capsys, monkeypatch):
    seen = {}
    real = subprocess.Popen

    def spy(cmd, **kw):
        seen["cmd"] = cmd
        return real(cmd, **kw)

    monkeypatch.setattr(fastcoder.subprocess, "Popen", spy)
    run(capsys, "scout", "--job", "001-x")
    cmd = seen["cmd"]
    assert cmd[0] == "timeout"
    assert cmd[cmd.index("claude") - 1] == "1800"


def test_first_message_per_role(env, capsys):
    run(capsys, "scout", "--job", "001-x")
    run(capsys, "fast-builder", "--job", "001-x", "--round", "2", "--port", "5174")
    run(capsys, "fast-fixer", "--job", "001-x", "--round", "4")
    run(capsys, "fast-builder", "--job", "001-x", "--message", "Do the thing.")
    scout, builder, fixer, custom = (c["argv"][-1] for c in calls(env))
    folder, wt = env["folder"], env["wt"]
    head = (f"Job folder: {folder}/. Worktree: {wt}. Branch: factory/001-x. "
            f"Job id: 001-x.")
    assert scout == f"{head}\nScout for this feature: add a dark mode toggle"
    assert builder == f"{head}\nBuild round 2. Port: 5174."
    assert fixer == f"{head}\nFix the findings listed in {folder}/round-4/fast-fix.md."
    assert custom == f"{head}\nDo the thing."


def test_scout_result_written_to_scout_md(env, capsys):
    code, out, _ = run(capsys, "scout", "--job", "001-x")
    assert code == 0
    assert (env["folder"] / "scout.md").read_text() == S2_RESULT["result"]


def test_builder_does_not_write_scout_md(env, capsys):
    run(capsys, "fast-builder", "--job", "001-x")
    assert not (env["folder"] / "scout.md").exists()


def test_cost_estimate_math():
    prices = {"input": 0.10, "output": 0.35, "cached_input": 0.01}
    out = {"usage": {"input_tokens": 1_000_000, "output_tokens": 2_000_000,
                     "cache_read_input_tokens": 3_000_000,
                     "cache_creation_input_tokens": 4_000_000},
           "total_cost_usd": 999.0}
    # 1*0.10 + 2*0.35 + 3*0.01 + 4*0.10 (cache creation at the input rate)
    assert fastcoder.estimate_cost(out, prices) == pytest.approx(1.23)
    # modelUsage takes precedence and is summed over models.
    mu = {"modelUsage": {
        "a": {"inputTokens": 1_000_000, "outputTokens": 0,
              "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
        "b": {"inputTokens": 0, "outputTokens": 1_000_000,
              "cacheReadInputTokens": 1_000_000, "cacheCreationInputTokens": 1_000_000}},
        "usage": {"input_tokens": 5}, "total_cost_usd": 999.0}
    assert fastcoder.estimate_cost(mu, prices) == pytest.approx(0.10 + 0.35 + 0.01 + 0.10)
    assert fastcoder.estimate_cost({"total_cost_usd": 1.0}, prices) is None
    assert fastcoder.estimate_cost(None, prices) is None


def test_cost_usd_is_estimate_not_total_cost(env, capsys):
    code, out, _ = run(capsys, "scout", "--job", "001-x")
    rec = json.loads(out)
    expected = (29066 * 0.10 + 853 * 0.35 + 115091 * 0.01) / 1e6
    assert rec["cost_usd"] == pytest.approx(expected)
    assert rec["cost_usd"] != S2_RESULT["total_cost_usd"]


def test_timeout_exits_124_with_error(env, capsys, monkeypatch):
    set_cfg(env, timeout_s=1)
    monkeypatch.setenv("STUB_SLEEP", "30")
    t0 = time.monotonic()
    code, out, _ = run(capsys, "fast-builder", "--job", "001-x")
    assert time.monotonic() - t0 < 15
    assert code == 124
    rec = json.loads(out)
    assert rec["error"] == "timeout" and rec["exit_code"] == 124
    assert rec["cost_usd"] is None and rec["cost_estimate"] is True
    assert Path(rec["transcript"]).exists()


def test_child_failure_exit_code_passed_through(env, capsys, monkeypatch):
    monkeypatch.setenv("STUB_EXIT", "3")
    code, out, _ = run(capsys, "fast-builder", "--job", "001-x")
    assert code == 3
    rec = json.loads(out)
    assert rec["exit_code"] == 3 and rec["error"]


def test_is_error_result_sets_error(env, capsys, monkeypatch):
    bad = dict(S2_RESULT, is_error=True, subtype="error_during_execution", result="boom")
    monkeypatch.setenv("STUB_RESULT", json.dumps(bad))
    code, out, _ = run(capsys, "scout", "--job", "001-x")
    rec = json.loads(out)
    assert rec["error"] and code == 0
    assert not (env["folder"] / "scout.md").exists()


def test_usage_errors_exit_2(env, capsys):
    assert run(capsys, "reviewer", "--job", "001-x")[0] == 2
    assert run(capsys, "scout")[0] == 2
    assert run(capsys, "scout", "--job", "../etc")[0] == 2
    assert run(capsys, "scout", "--job", "nope")[0] == 2
    assert run(capsys, "fast-builder", "--job", "001-x", "--resume", "not-a-uuid")[0] == 2
    assert run(capsys, "fast-builder", "--job", "001-x", "--resume", "--x")[0] == 2
    assert calls(env) == []


def test_pi_command_construction(env):
    cfg = json.loads((env["root"] / "factory" / "config.json").read_text())
    cfg["fast_coder"]["harness"] = "pi"
    job = load_job(env["root"], "001-x")
    root = env["root"]
    cmd = fastcoder.build_command(cfg, root, "fast-builder", "SID-1", resume=False,
                                  prompt="PROMPT", message="MSG")
    assert cmd == [
        "timeout", "-k", "10", "1800",
        "pi", "-p", "--provider", "openrouter", "--model", "inception/mercury-3",
        "--session-id", "SID-1",
        "--tools", "read,bash,edit,write",
        "--no-skills", "--no-extensions", "--no-context-files",
        "-e", str(root / "factory" / "fast-agents" / "pi-tool-gate.ts"),
        "--append-system-prompt", "PROMPT",
        "--mode", "json", "MSG",
    ]
    scout = fastcoder.build_command(cfg, root, "scout", "SID-2", resume=True,
                                    prompt="P", message="M")
    assert scout[scout.index("--tools") + 1] == "read,grep,find,ls"
    # pi resumes by reusing the exact session id.
    assert scout[scout.index("--session-id") + 1] == "SID-2"
    env_ = fastcoder.child_env(cfg, root, KEY)
    assert env_["OPENROUTER_API_KEY"] == KEY
    assert env_["FACTORY_HEADLESS"] == "1"
    assert env_["FACTORY_TOOL_GATE"] == str(root / "factory" / "bin" / "tool-gate")
    assert "ANTHROPIC_BASE_URL" not in env_
    assert not any(KEY in a for a in cmd + scout)
    assert job.worktree == env["wt"]


def test_pi_output_parsed_for_cost_and_result():
    lines = [
        {"type": "session", "id": "abc"},
        {"type": "agent_end", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "toolCall"}],
             "usage": {"input": 1_000_000, "output": 0, "cacheRead": 0, "cacheWrite": 0}},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}],
             "usage": {"input": 0, "output": 1_000_000, "cacheRead": 1_000_000,
                       "cacheWrite": 1_000_000}},
        ]},
    ]
    parsed = fastcoder.parse_pi("\n".join(json.dumps(x) for x in lines))
    assert parsed["result"] == "done"
    prices = {"input": 0.10, "output": 0.35, "cached_input": 0.01}
    assert fastcoder.estimate_cost(parsed, prices) == pytest.approx(0.10 + 0.35 + 0.01 + 0.10)


def test_prompts_exist_and_carry_rules():
    d = KIT / "fast-agents"
    scout = (d / "scout.md").read_text()
    assert "absolute paths" in scout.lower() and "never edit" in scout.lower()
    for role in ("fast-builder", "fast-fixer"):
        text = (d / f"{role}.md").read_text()
        assert "cd <worktree> && " in text and "absolute paths" in text.lower()
        assert "needs human:" in text and "STATUS: BLOCKED" in text
        # R30 + final review I4: the keyless child cannot run decide (not on the
        # allowlist either), so the prompts never ask it to
        assert "decide" not in text and "apply.action" not in text
        assert "factory tool-gate: policy deny" in text
        assert "build.md" in text
    assert "### Round" in (d / "fast-fixer.md").read_text()
    assert (d / "pi-tool-gate.ts").is_file()


def test_wrapper_runs_module():
    """``factory/bin/fast-coder`` reaches ``python -m factorylib.fastcoder``."""
    if shutil.which("uv") is None:
        pytest.skip("uv not installed")
    r = subprocess.run([str(KIT / "bin" / "fast-coder"), "--help"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "scout" in r.stdout


def test_missing_worktree_exits_2_without_spawning(env, capsys):
    job = env["folder"] / "job.json"
    raw = json.loads(job.read_text())
    raw["worktree"] = str(env["tmp"] / "gone")
    job.write_text(json.dumps(raw))
    code, out, err = run(capsys, "scout", "--job", "001-x")
    assert code == 2 and out == "" and "worktree" in err
    assert calls(env) == []


def test_unstartable_child_prints_record(env, capsys, monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(fastcoder, "_run_child", boom)
    code, out, _ = run(capsys, "scout", "--job", "001-x")
    rec = json.loads(out)
    assert code == 127 and rec["exit_code"] == 127
    assert rec["error"].startswith("could not start child")


# --- fix round 1 -------------------------------------------------------------

def test_builder_refuses_without_gate(env, capsys):
    env["gate"].unlink()
    for role in ("fast-builder", "fast-fixer"):
        code, out, err = run(capsys, role, "--job", "001-x")
        assert code == 2 and out == ""
        assert f"tool-gate missing or not executable: {env['gate']}" in err
    assert calls(env) == []


def test_builder_refuses_non_executable_gate(env, capsys):
    env["gate"].chmod(0o644)
    code, out, err = run(capsys, "fast-builder", "--job", "001-x")
    assert code == 2 and out == ""
    assert "tool-gate missing or not executable" in err
    assert calls(env) == []


def test_scout_runs_without_gate(env, capsys):
    env["gate"].unlink()
    code, out, _ = run(capsys, "scout", "--job", "001-x")
    assert code == 0 and json.loads(out)["error"] is None
    assert len(calls(env)) == 1


def test_claude_child_env_has_no_openrouter_key(env, capsys):
    code, _, _ = run(capsys, "fast-builder", "--job", "001-x")
    assert code == 0
    (call,) = calls(env)
    assert "OPENROUTER_API_KEY" not in call["env"]
    assert call["env"]["ANTHROPIC_AUTH_TOKEN"] == KEY
    assert os.environ["OPENROUTER_API_KEY"] == KEY  # parent untouched


def test_claude_child_env_scrub_flag_and_no_routing_vars(env, capsys, monkeypatch):
    for name in ROUTING_VARS:
        monkeypatch.setenv(name, "inherited")
    run(capsys, "fast-builder", "--job", "001-x")
    (call,) = calls(env)
    assert call["env"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    for name in ROUTING_VARS:
        assert name not in call["env"], name
        assert os.environ[name] == "inherited"


def test_resume_valid_uuid_accepted(env, capsys):
    sid = "0cc6b702-e60c-4ede-8311-339a3c6d763c"
    code, out, _ = run(capsys, "fast-builder", "--job", "001-x", "--resume", sid)
    assert code == 0
    assert flag(calls(env)[0]["argv"], "--resume") == sid


def _gone(pid):
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def test_sigterm_kills_child_group(env):
    import signal
    pidfile = env["tmp"] / "stub.pid"
    child_env = dict(os.environ, STUB_SLEEP="60", STUB_PIDFILE=str(pidfile),
                     PYTHONPATH=str(KIT))
    proc = subprocess.Popen([sys.executable, "-m", "factorylib.fastcoder", "fast-builder",
                             "--job", "001-x"], cwd=env["wt"], env=child_env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not (pidfile.exists() and pidfile.read_text()):
            assert time.monotonic() < deadline, "stub never started"
            time.sleep(0.05)
        stub_pid = int(pidfile.read_text())
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=15)
        deadline = time.monotonic() + 10
        while not _gone(stub_pid):
            assert time.monotonic() < deadline, "stub claude still alive after SIGTERM"
            time.sleep(0.05)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 143
    rec = json.loads(out)
    assert rec["error"] == "killed" and rec["exit_code"] == 143


def test_child_uses_its_own_config_dir(tmp_path, monkeypatch):
    # A stored claude.ai login in the shared ~/.claude shadowed
    # ANTHROPIC_AUTH_TOKEN and every OpenRouter call went out unauthenticated.
    root = tmp_path / "app"
    (root / "factory").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/someone/.claude")
    monkeypatch.setenv("CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "1")
    env_ = fastcoder.child_env({"fast_coder": {"model": "inception/mercury-3",
                                              "harness": "claude-p"}}, root, "k")
    cdir = root / "factory" / fastcoder.FAST_CONFIG_DIR
    assert env_["CLAUDE_CONFIG_DIR"] == str(cdir) and cdir.is_dir()
    assert (cdir.stat().st_mode & 0o777) == 0o700
    assert "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH" not in env_
    # stable across calls, so --resume finds the session
    assert fastcoder.child_env({"fast_coder": {"model": "m", "harness": "claude-p"}},
                               root, "k")["CLAUDE_CONFIG_DIR"] == str(cdir)


@pytest.fixture(autouse=True)
def _sandbox_deps_present(request, monkeypatch):
    # Hermetic: the preflight must not depend on whether this machine has socat.
    if "sandbox_missing" not in request.node.name:
        monkeypatch.setattr(fastcoder, "sandbox_missing", lambda *a, **k: [])


def test_sandbox_missing_blocks_bash_role_with_clear_error(env, capsys, monkeypatch):
    monkeypatch.setattr(fastcoder.shutil, "which", lambda b: None if b == "socat" else "/usr/bin/" + b)
    monkeypatch.setattr(fastcoder.sys, "platform", "linux")
    code, _, err = run(capsys, "fast-builder", "--job", "001-x")
    assert code == 2 and "socat" in err and calls(env) == []


def test_sandbox_missing_only_for_bash_roles_on_linux(monkeypatch):
    monkeypatch.setattr(fastcoder.shutil, "which", lambda b: None if b == "socat" else "/usr/bin/" + b)
    assert fastcoder.sandbox_missing("fast-builder", "claude-p", "linux") == ["socat"]
    assert fastcoder.sandbox_missing("fast-fixer", "claude-p", "linux") == ["socat"]
    assert fastcoder.sandbox_missing("scout", "claude-p", "linux") == []
    assert fastcoder.sandbox_missing("fast-builder", "claude-p", "darwin") == []
    assert fastcoder.sandbox_missing("fast-builder", "pi", "linux") == []
    monkeypatch.setattr(fastcoder.shutil, "which", lambda b: "/usr/bin/" + b)
    assert fastcoder.sandbox_missing("fast-builder", "claude-p", "linux") == []


def test_sandbox_placeholders_are_removed_but_real_files_kept(env):
    wt = env["wt"]
    (wt / "keep.txt").write_text("")              # pre-existing untracked: kept
    before = fastcoder._untracked(wt)
    for n in (".env", ".npmrc", "yarn.lock", "bunfig.toml"):
        (wt / n).write_text("")                   # sandbox stubs: removed
    (wt / ".env.local").write_text("SECRET=1\n")  # non-empty: kept
    (wt / "notes.md").write_text("")              # not a stub name: kept
    removed = fastcoder._remove_sandbox_stubs(wt, before)
    assert removed == [".env", ".npmrc", "bunfig.toml", "yarn.lock"]
    for n in ("keep.txt", ".env.local", "notes.md"):
        assert (wt / n).exists()
    assert not (wt / ".env").exists()


def test_clean_stubs_needs_no_key_and_removes_all_empty_stubs(env, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    wt = env["wt"]
    (wt / ".env").write_text("")
    (wt / "pnpm-lock.yaml").write_text("")
    (wt / ".env.local").write_text("X=1\n")
    code, out, _ = run(capsys, "clean-stubs", "--job", "001-x")
    assert code == 0 and json.loads(out) == {"removed": [".env", "pnpm-lock.yaml"]}
    assert (wt / ".env.local").exists() and calls(env) == []
