import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from factorylib import cli

KIT = Path(__file__).resolve().parent.parent


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "main"
    (root / "factory").mkdir(parents=True)
    git(root, "init", "-b", "main")
    (root / "README.md").write_text("hi\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "init")
    shutil.copy(KIT / "config.json", root / "factory" / "config.json")
    shutil.copy(KIT / "policy.json", root / "factory" / "policy.json")
    shutil.copytree(KIT / "decisions", root / "factory" / "decisions")
    folder = root / "factory" / "jobs" / "smoke"
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({
        "id": "smoke", "feature": "fix typo in footer", "branch": "main",
        "worktree": str(root), "review_commits": {},
    }))
    monkeypatch.chdir(root)
    # No key: run_decision errors before any HTTP, and the CLI still prints a record.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return root.resolve()


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def lines(root, name="smoke"):
    path = root / "factory" / "jobs" / name / "decisions.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_cli_decide_prints_record_and_exits_0_on_decide_error(root, capsys):
    code, out, err = run(capsys, "triage", "--job", "smoke")
    assert code == 0
    rec = json.loads(out)
    assert rec["kind"] == "decision" and rec["decision"] == "triage"
    assert rec["error"] and rec["band"] == "uncertain"
    assert rec["apply"]["tier"] == "heavy"
    assert lines(root) == [rec]


def test_cli_state_json_wins_over_state_file(root, capsys, monkeypatch):
    seen = {}

    def fake_run(name, *, root, job, state, images, state_error):
        seen.update(name=name, state=state, images=images, job=job, state_error=state_error)
        return {"kind": "decision"}

    monkeypatch.setattr(cli, "run_decision", fake_run)
    f = root / "extra.json"
    f.write_text(json.dumps({"a": 1, "b": 1}))
    code, out, _ = run(capsys, "failure", "--job", "smoke", "--state-file", str(f),
                       "--state-json", '{"b": 2, "output_tail": "boom"}')
    assert code == 0 and json.loads(out) == {"kind": "decision"}
    assert seen["state"]["a"] == 1 and seen["state"]["b"] == 2
    assert seen["state"]["output_tail"] == "boom"
    assert seen["job"].id == "smoke" and seen["images"] == []
    assert seen["state_error"] is None


def test_cli_outcome_appends(root, capsys):
    code, out, err = run(capsys, "outcome", "--job", "smoke", "--decision", "triage",
                         "--truth", '{"risks": []}', "--note", "approved")
    assert code == 0
    rec = json.loads(out)
    assert rec["kind"] == "outcome" and rec["decision"] == "triage"
    assert rec["truth"] == {"risks": []} and rec["note"] == "approved"
    assert rec["ts"]
    assert lines(root) == [rec]


def test_cli_outcome_note_optional(root, capsys):
    code, out, _ = run(capsys, "outcome", "--job", "smoke", "--decision", "stuck",
                       "--truth", "true")
    assert code == 0 and json.loads(out)["note"] is None


@pytest.mark.parametrize("argv", [
    ["nope", "--job", "smoke"],
    ["outcome", "--job", "smoke", "--decision", "nope", "--truth", "{}"],
])
def test_cli_unknown_decision_exit_2(root, capsys, argv):
    code, out, err = run(capsys, *argv)
    assert code == 2 and out == ""
    assert "unknown decision" in err


@pytest.mark.parametrize("argv", [
    ["triage", "--job", "missing"],
    ["triage", "--job", "../smoke"],
    ["outcome", "--job", "missing", "--decision", "triage", "--truth", "{}"],
])
def test_cli_bad_job_exit_2(root, capsys, argv):
    code, out, err = run(capsys, *argv)
    assert code == 2 and out == ""
    assert "job" in err


@pytest.mark.parametrize("argv", [
    ["triage", "--job", "smoke", "--state-json", "{not json"],
    ["triage", "--job", "smoke", "--state-json", "[1, 2]"],
    ["outcome", "--job", "smoke", "--decision", "triage", "--truth", "{bad"],
])
def test_cli_bad_json_exit_2(root, capsys, argv):
    code, out, err = run(capsys, *argv)
    assert code == 2 and out == "" and err


def test_cli_missing_state_file_exit_2(root, capsys):
    code, out, err = run(capsys, "triage", "--job", "smoke", "--state-file", "nope.json")
    assert code == 2 and out == "" and "nope.json" in err


def test_cli_job_required_except_toolgate(root, capsys):
    code, out, err = run(capsys, "triage")
    assert code == 2 and out == "" and "--job" in err


def test_cli_toolgate_without_job(root, capsys):
    code, out, err = run(capsys, "toolgate", "--state-json", '{"command": "ls"}')
    assert code == 0
    rec = json.loads(out)
    assert rec["job"] is None and rec["apply"] == {"permission": "defer"}
    assert (root / "factory" / "tool-gate.jsonl").read_text().strip() == json.dumps(rec)


def test_cli_toolgate_rejects_job(root, capsys):
    code, out, err = run(capsys, "toolgate", "--job", "smoke")
    assert code == 2 and out == ""


def test_cli_bad_image_exit_2(root, capsys, tmp_path):
    img = tmp_path / "x.bmp"
    img.write_bytes(b"BM")
    code, out, err = run(capsys, "visual", "--job", "smoke", "--image", str(img))
    assert code == 2 and out == "" and "unsupported image type" in err


def test_cli_usage_error_exit_2(root, capsys):
    code, out, err = run(capsys)
    assert code == 2 and out == ""
    code, out, err = run(capsys, "outcome", "--job", "smoke")
    assert code == 2 and out == ""


def test_cli_report(root, capsys):
    code, out, err = run(capsys, "promote", "--job", "smoke")
    assert code == 0, err
    code, out, err = run(capsys, "report")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0].split() == ["decision", "cases", "paired", "unpaired", "coverage",
                                "agreement", "unsafe", "ready"]
    assert lines[1].split()[:3] == ["promote", "1", "0"]
    assert lines[-1] == "skipped_lines: 0"
    code, out, err = run(capsys, "report", "--json", "--min-cases", "1")
    assert code == 0
    rep = json.loads(out)
    assert rep["promote"]["cases"] == 1 and rep["skipped_lines"] == 0


def test_cli_report_bad_option_exit_2(root, capsys):
    code, out, err = run(capsys, "report", "--min-cases", "x")
    assert code == 2 and out == ""


def test_bin_decide_wrapper_end_to_end(root):
    r = subprocess.run([str(KIT / "bin" / "decide"), "triage", "--job", "smoke"],
                       cwd=root, capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"})
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["decision"] == "triage" and rec["apply"]["tier"] == "heavy"


def fallback_checks(code, out, err):
    assert code == 0, err
    rec = json.loads(out)
    assert rec["error"] and rec["band"] == "uncertain"
    return rec


def test_cli_config_error_prints_fallback(root, capsys):
    (root / "factory" / "config.json").write_text("{corrupt")
    rec = fallback_checks(*run(capsys, "triage", "--job", "smoke"))
    assert rec["error"].startswith("JSONDecodeError: ")
    assert rec["apply"] == {"tier": "heavy", "reviewers": ["security", "ux", "ui", "code"],
                            "risks": []}


def test_cli_policy_error_escalation_fails_closed(root, capsys):
    (root / "factory" / "policy.json").write_text("{corrupt")
    rec = fallback_checks(*run(capsys, "escalation", "--job", "smoke"))
    assert rec["error"].startswith("JSONDecodeError: ")
    assert rec["apply"]["escalate"] is True
    assert rec["apply"]["reasons"] == ["<state build failed>"]
    assert lines(root) == [rec]


def test_cli_route_exception_prints_fallback(root, capsys, monkeypatch):
    from factorylib.routes import ROUTES

    def boom(banded, state):
        raise RuntimeError("route broke")

    monkeypatch.setitem(ROUTES, "triage", boom)
    rec = fallback_checks(*run(capsys, "triage", "--job", "smoke"))
    assert rec["error"]  # missing key comes first; the route still must not crash
    assert rec["route"] is None and rec["apply"]["tier"] == "heavy"


def test_cli_route_exception_with_key_prints_fallback(root, capsys, monkeypatch):
    import factorylib.run as run_mod
    from factorylib.client import RawResult
    from factorylib.routes import ROUTES

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    monkeypatch.setattr(run_mod, "post_decision",
                        lambda *a, **k: RawResult("m", {}, {}, 1))
    monkeypatch.setitem(ROUTES, "triage", lambda b, s: 1 / 0)
    rec = fallback_checks(*run(capsys, "triage", "--job", "smoke"))
    assert rec["error"].startswith("ZeroDivisionError: ")
    assert rec["apply"]["tier"] == "heavy"


def test_cli_log_append_failure_still_prints(root, capsys):
    (root / "factory" / "jobs" / "smoke" / "decisions.jsonl").mkdir()
    rec = fallback_checks(*run(capsys, "triage", "--job", "smoke"))
    assert "decision log" in rec["error"]
    assert rec["apply"]["tier"] == "heavy"


def test_cli_log_append_failure_overrides_active_route(root, capsys, monkeypatch):
    import factorylib.run as run_mod
    from factorylib.client import RawResult

    cfg = json.loads((root / "factory" / "config.json").read_text())
    cfg["decisions"]["promote"] = {"mode": "active"}
    (root / "factory" / "config.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    answers = {"beyond_fast_tier": {"type": "boolean", "probability": 0.01}}
    monkeypatch.setattr(run_mod, "post_decision",
                        lambda *a, **k: RawResult("m", answers, {}, 1))
    (root / "factory" / "jobs" / "smoke" / "decisions.jsonl").mkdir()
    rec = fallback_checks(*run(capsys, "promote", "--job", "smoke"))
    assert rec["error"].startswith("decision log write failed: IsADirectoryError")
    assert rec["route"] == {"promote": False}
    assert rec["apply"] == {"promote": True}


@pytest.mark.parametrize("target,exc", [
    ("cli.build_state", KeyError("x")),
    ("run.band_answers", AttributeError("no attr")),
])
def test_cli_unexpected_exception_type_prints_fallback(root, capsys, monkeypatch, target, exc):
    import factorylib.run as run_mod
    from factorylib.client import RawResult

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    monkeypatch.setattr(run_mod, "post_decision", lambda *a, **k: RawResult("m", {}, {}, 1))
    mod = {"cli": cli, "run": run_mod}[target.split(".")[0]]

    def raiser(*a, **k):
        raise exc

    monkeypatch.setattr(mod, target.split(".")[1], raiser)
    rec = fallback_checks(*run(capsys, "triage", "--job", "smoke"))
    assert type(exc).__name__ in rec["error"]
    assert rec["apply"]["tier"] == "heavy"


def test_cli_outcome_write_failure_exit_1(root, capsys):
    (root / "factory" / "jobs" / "smoke" / "decisions.jsonl").mkdir()
    code, out, err = run(capsys, "outcome", "--job", "smoke", "--decision", "triage",
                         "--truth", "{}")
    assert code == 1 and out == "" and "decisions.jsonl" in err


def test_cli_secret_at_cut_point_not_sent(root, capsys, monkeypatch):
    import hashlib

    import factorylib.run as run_mod
    from factorylib.client import RawResult
    from factorylib.state import _apply_caps

    hexkey = hashlib.sha256(b"planted").hexdigest()
    secret = "sk-or-v1-" + hexkey
    diff_body = "\n".join([secret] * 4500) + "\n"  # ~333 KB
    git(root, "checkout", "-b", "feat")
    (root / "notes.txt").write_text(diff_body)
    git(root, "add", "notes.txt")
    git(root, "commit", "-m", "add notes")
    assert len(diff_body) > 300_000

    # Precondition: capping the raw text first WOULD leave a hex fragment behind,
    # so this test can tell redact-then-cap from cap-then-redact.
    raw_capped = json.dumps(_apply_caps({"spec": "", "diff_stat": "", "diff": diff_body}, 200_000))
    assert any(hexkey[i:i + 8] in raw_capped.replace(secret, "") for i in range(57))

    seen = []

    def fake_post(state, questions, *, cfg, api_key):
        seen.append(json.dumps({"state": state, "questions": questions}))
        return RawResult("m", {}, {}, 1)

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    monkeypatch.setattr(run_mod, "post_decision", fake_post)
    code, out, err = run(capsys, "escalation", "--job", "smoke")
    assert code == 0, err
    assert len(seen) == 1
    payload = seen[0]
    assert "[REDACTED]" in payload
    assert "sk-or-v1" not in payload
    assert not any(hexkey[i:i + 8] in payload for i in range(57))
