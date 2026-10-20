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

    def fake_run(name, *, root, job, state, images):
        seen.update(name=name, state=state, images=images, job=job)
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


def test_cli_report_not_implemented(root, capsys):
    code, out, err = run(capsys, "report")
    assert code == 2 and out == "" and "not implemented" in err


def test_bin_decide_wrapper_end_to_end(root):
    r = subprocess.run([str(KIT / "bin" / "decide"), "triage", "--job", "smoke"],
                       cwd=root, capture_output=True, text=True,
                       env={k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"})
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    assert rec["decision"] == "triage" and rec["apply"]["tier"] == "heavy"
