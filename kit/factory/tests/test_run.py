import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from factorylib.client import DecideError, RawResult
from factorylib.paths import load_job
from factorylib.run import append_jsonl, run_decision

KIT = Path(__file__).resolve().parent.parent
SECRET = "sk-or-v1-" + "0123456789abcdef" * 3


def make_root(tmp_path, *, modes=None, cap=None):
    root = tmp_path / "main"
    (root / "factory").mkdir(parents=True)
    cfg = json.loads((KIT / "config.json").read_text())
    for name, mode in (modes or {}).items():
        cfg["decisions"][name] = {"mode": mode}
    if cap is not None:
        cfg["decide"]["state_char_cap"] = cap
    (root / "factory" / "config.json").write_text(json.dumps(cfg))
    shutil.copytree(KIT / "decisions", root / "factory" / "decisions")
    folder = root / "factory" / "jobs" / "001-x"
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({
        "id": "001-x", "feature": "fix typo", "branch": "factory/001-x",
        "worktree": str(root), "review_commits": {},
    }))
    return root, load_job(root, "001-x")


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")


def b(p):
    return {"type": "boolean", "probability": p}


RISKS = ["auth", "payments", "dependencies", "ci_deploy", "schema_migration",
         "email", "prod_data", "build_scripts"]

TRIAGE_FAST = {
    "kind": {"type": "choice", "choice": "docs", "probabilities": {"docs": 0.9, "bugfix": 0.1}},
    "complexity": {"type": "score", "score": 0.0,
                   "probabilities": {"0": 0.9, "1": 0.1, "2": 0.0, "3": 0.0}},
    "ui_change": b(0.02),
    **{r: b(0.01) for r in RISKS},
}


class Stub:
    """Stand-in for ``post_decision``: records calls, returns or raises."""

    def __init__(self, answers=None, exc=None):
        self.answers = answers or {}
        self.exc = exc
        self.calls = []

    def __call__(self, state, questions, *, cfg, api_key):
        self.calls.append({"state": state, "questions": questions, "api_key": api_key})
        if self.exc:
            raise self.exc
        return RawResult(model="inception/mercury-decide:free", answers=self.answers,
                         usage={"input_tokens": 10, "cost": 0}, latency_ms=42)


def log_lines(job):
    return [json.loads(line) for line in (job.folder / "decisions.jsonl").read_text().splitlines()]


def test_shadow_applies_fallback_but_logs_route(tmp_path):
    root, job = make_root(tmp_path)
    post = Stub(TRIAGE_FAST)
    rec = run_decision("triage", root=root, job=job, state={"feature": "fix typo"}, post=post)
    assert rec["kind"] == "decision" and rec["decision"] == "triage" and rec["id"] == "D1"
    assert rec["job"] == "001-x" and rec["mode"] == "shadow"
    assert rec["band"] == "confident"
    assert rec["route"]["tier"] == "fast"
    assert rec["apply"] == {"tier": "heavy", "reviewers": ["security", "ux", "ui", "code"],
                            "risks": []}
    assert rec["error"] is None and rec["model"] == "inception/mercury-decide:free"
    assert rec["answers"]["complexity"] == {"type": "score", "value": 0.0, "p": 0.9,
                                            "confident": True}
    assert rec["ts"].endswith("+00:00")
    assert log_lines(job) == [rec]


def test_unknown_mode_is_shadow(tmp_path):
    root, job = make_root(tmp_path, modes={"triage": "Active"})
    rec = run_decision("triage", root=root, job=job, state={"feature": "x"},
                       post=Stub(TRIAGE_FAST))
    assert rec["mode"] == "shadow"
    assert rec["route"]["tier"] == "fast"
    assert rec["apply"]["tier"] == "heavy"


def test_active_confident_applies_route(tmp_path):
    root, job = make_root(tmp_path, modes={"triage": "active"})
    rec = run_decision("triage", root=root, job=job, state={"feature": "x"},
                       post=Stub(TRIAGE_FAST))
    assert rec["mode"] == "active" and rec["band"] == "confident"
    assert rec["apply"] == rec["route"]
    assert rec["apply"]["tier"] == "fast"
    assert rec["apply"]["reviewers"] == ["security", "code"]


def test_active_uncertain_band_still_applies_route(tmp_path):
    # Routes already fail safe on uncertain answers; active applies them as is.
    root, job = make_root(tmp_path, modes={"triage": "active"})
    answers = {**TRIAGE_FAST, "ui_change": b(0.5)}
    rec = run_decision("triage", root=root, job=job, state={"feature": "x"}, post=Stub(answers))
    assert rec["band"] == "uncertain"
    assert rec["apply"] == rec["route"]
    assert rec["apply"]["reviewers"] == ["security", "ux", "ui", "code"]


def test_error_gives_uncertain_and_fallback(tmp_path, monkeypatch):
    # Review Focus 3: a missing key is a DecideError with no HTTP call at all.
    monkeypatch.delenv("OPENROUTER_API_KEY")
    root, job = make_root(tmp_path, modes={"triage": "active"})
    post = Stub(TRIAGE_FAST)
    rec = run_decision("triage", root=root, job=job, state={"feature": "x"}, post=post)
    assert post.calls == []
    assert "OPENROUTER_API_KEY" in rec["error"]
    assert rec["band"] == "uncertain"
    assert rec["model"] is None and rec["usage"] == {}
    assert all(not a["confident"] for a in rec["answers"].values())
    assert rec["route"]["tier"] == "heavy"
    assert rec["apply"]["tier"] == "heavy"
    assert rec["apply"]["reviewers"] == ["security", "ux", "ui", "code"]


def test_decide_error_from_post_gives_fallback(tmp_path):
    root, job = make_root(tmp_path, modes={"triage": "active"})
    rec = run_decision("triage", root=root, job=job, state={"feature": "x"},
                       post=Stub(exc=DecideError("all decide models failed: x: HTTP 503")))
    assert rec["error"] == "all decide models failed: x: HTTP 503"
    assert rec["band"] == "uncertain" and rec["apply"]["tier"] == "heavy"


def test_unexpected_exception_from_post_gives_fallback(tmp_path):
    root, job = make_root(tmp_path, modes={"triage": "active"})
    rec = run_decision("triage", root=root, job=job, state={"feature": "x"},
                       post=Stub(exc=RuntimeError("boom")))
    assert "RuntimeError" in rec["error"] and rec["apply"]["tier"] == "heavy"


ESC_CLEAN = {r: b(0.01) for r in RISKS}


def test_escalation_rules_force_escalate_in_shadow(tmp_path):
    root, job = make_root(tmp_path)
    rec = run_decision("escalation", root=root, job=job,
                       state={"rule_hits": ["src/auth/login.ts"], "diff": ""},
                       post=Stub(ESC_CLEAN))
    assert rec["mode"] == "shadow"
    assert rec["apply"] == {"escalate": True, "reasons": ["src/auth/login.ts"]}


def test_escalation_rules_force_escalate_on_error(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    root, job = make_root(tmp_path, modes={"escalation": "active"})
    rec = run_decision("escalation", root=root, job=job,
                       state={"rule_hits": ["<git diff failed>"]}, post=Stub())
    assert rec["error"]
    assert rec["apply"] == {"escalate": True, "reasons": ["<git diff failed>"]}


def test_escalation_rule_hits_deduplicated(tmp_path):
    root, job = make_root(tmp_path, modes={"escalation": "active"})
    answers = {**ESC_CLEAN, "auth": b(0.99)}
    rec = run_decision("escalation", root=root, job=job,
                       state={"rule_hits": ["a.env", "infra/x.tf"]}, post=Stub(answers))
    assert rec["route"]["reasons"] == ["a.env", "infra/x.tf", "auth"]
    assert rec["apply"] == {"escalate": True, "reasons": ["a.env", "infra/x.tf", "auth"]}


@pytest.mark.parametrize("hits,expected", [
    ("infra/x.tf", ["infra/x.tf"]),
    (("a.env", "b.env"), ["a.env", "b.env"]),
    ({"only.env"}, ["only.env"]),
])
def test_escalation_non_list_rule_hits_coerced(tmp_path, hits, expected):
    root, job = make_root(tmp_path)
    rec = run_decision("escalation", root=root, job=job, state={"rule_hits": hits},
                       post=Stub(ESC_CLEAN))
    assert rec["apply"] == {"escalate": True, "reasons": expected}
    assert rec["route"]["reasons"] == expected


@pytest.mark.parametrize("hits", [None, "", [], ()])
def test_escalation_falsy_rule_hits_no_override(tmp_path, hits):
    root, job = make_root(tmp_path)
    rec = run_decision("escalation", root=root, job=job, state={"rule_hits": hits},
                       post=Stub(ESC_CLEAN))
    assert rec["apply"] == {"escalate": False, "reasons": []}


def test_finding_fallback_resolves_builder_all(tmp_path):
    root, job = make_root(tmp_path)
    state = {"findings": [{"reviewer": "code", "text": t} for t in ("a", "b", "c")]}
    post = Stub({"f0": b(0.99), "f1": b(0.99), "f2": b(0.01)})
    rec = run_decision("finding", root=root, job=job, state=state, post=post)
    assert rec["apply"] == {"fast_fixer": [], "builder": [0, 1, 2]}
    assert rec["route"] == {"fast_fixer": [0, 1], "builder": [2]}
    assert set(post.calls[0]["questions"]) == {"f0", "f1", "f2"}


def test_no_questions_skips_post(tmp_path):
    root, job = make_root(tmp_path, modes={"finding": "active"})
    post = Stub()
    rec = run_decision("finding", root=root, job=job, state={"findings": []}, post=post)
    assert post.calls == []
    assert rec["error"] is None and rec["apply"] == {"fast_fixer": [], "builder": []}
    assert rec["band"] == "n/a"


def test_log_has_hash_not_state(tmp_path):
    root, job = make_root(tmp_path)
    state = {"feature": f"OPENROUTER_API_KEY={SECRET}", "diff": f"PLANTED_MARKER {SECRET} here",
             "findings": [f"leaked {SECRET}"]}
    rec = run_decision("triage", root=root, job=job, state=state, post=Stub(TRIAGE_FAST))
    text = (job.folder / "decisions.jsonl").read_text()
    assert SECRET not in text and "0123456789abcdef" not in text
    assert "test-key-not-real" not in text
    assert "PLANTED_MARKER" not in text and "leaked" not in text  # no state content at all
    assert len(rec["state_sha256"]) == 64 and rec["redactions"] >= 3


def test_state_hash_is_of_redacted_sorted_state(tmp_path):
    import hashlib
    root, job = make_root(tmp_path)
    post = Stub(TRIAGE_FAST)
    rec = run_decision("triage", root=root, job=job, state={"b": 1, "feature": SECRET},
                       post=post)
    sent = post.calls[0]["state"]
    assert sent == {"b": 1, "feature": "[REDACTED]"}
    assert rec["state_sha256"] == hashlib.sha256(
        json.dumps(sent, sort_keys=True).encode()).hexdigest()


def test_secret_in_items_not_sent_in_questions(tmp_path):
    root, job = make_root(tmp_path)
    post = Stub()
    run_decision("finding", root=root, job=job,
                 state={"findings": [{"reviewer": "code", "text": f"key {SECRET}"}]}, post=post)
    assert SECRET not in json.dumps(post.calls[0])


def test_redact_before_cap(tmp_path):
    # Capping first would cut a secret in half and leave an unredactable fragment.
    root, job = make_root(tmp_path, cap=10_000)
    post = Stub(TRIAGE_FAST)
    rec = run_decision("triage", root=root, job=job,
                       state={"feature": "x", "diff": (SECRET + " ") * 2000}, post=post)
    sent = json.dumps(post.calls[0]["state"])
    assert len(sent) <= 10_000
    assert "sk-or-v1" not in sent and "abcdef" not in sent
    assert "[REDACTED]" in sent and "truncated" in sent
    assert rec["redactions"] == 2000


def test_cap_shrinks_largest_fields_until_fit(tmp_path):
    root, job = make_root(tmp_path, cap=5_000)
    post = Stub(TRIAGE_FAST)
    state = {"feature": "keep me", "spec": "s\n" * 3000,
             "nested": {"items": ["é" * 4000, "short"]}}
    run_decision("triage", root=root, job=job, state=state, post=post)
    sent = post.calls[0]["state"]
    assert len(json.dumps(sent)) <= 5_000
    assert sent["feature"] == "keep me" and sent["nested"]["items"][1] == "short"


def test_images_sent_as_content_parts(tmp_path):
    root, job = make_root(tmp_path)
    img = tmp_path / "shot.PNG"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    post = Stub()
    rec = run_decision("visual", root=root, job=job,
                       state={"criteria": ["button is blue"], "note": SECRET},
                       images=[img], post=post)
    sent = post.calls[0]["state"]
    assert isinstance(sent, list) and sent[0]["role"] == "user"
    parts = sent[0]["content"]
    assert parts[0]["type"] == "text"
    assert json.loads(parts[0]["text"]) == {"criteria": ["button is blue"], "note": "[REDACTED]"}
    assert parts[1] == {"type": "image_url", "image_url": {
        "url": "data:image/png;base64,iVBORw0KGgpmYWtl"}}
    assert rec["error"] is None


@pytest.mark.parametrize("fname,size,msg", [
    ("shot.bmp", 10, "unsupported image type"),
    ("big.jpg", 5 * 1024 * 1024 + 1, "larger than 5 MB"),
    ("missing.png", None, "not found"),
])
def test_bad_images_raise(tmp_path, fname, size, msg):
    root, job = make_root(tmp_path)
    img = tmp_path / fname
    if size is not None:
        with open(img, "wb") as f:
            f.truncate(size)
    post = Stub()
    with pytest.raises(ValueError, match=msg):
        run_decision("visual", root=root, job=job, state={}, images=[img], post=post)
    assert post.calls == []


def test_toolgate_without_job_logs_to_tool_gate(tmp_path):
    root, _ = make_root(tmp_path)
    answers = {"class": {"type": "choice", "choice": "safe",
                         "probabilities": {"safe": 0.9, "needs_human": 0.1}}}
    rec = run_decision("toolgate", root=root, job=None, state={"command": "ls"},
                       post=Stub(answers))
    assert rec["job"] is None
    lines = (root / "factory" / "tool-gate.jsonl").read_text().splitlines()
    assert json.loads(lines[-1]) == rec


def test_unknown_decision_raises(tmp_path):
    root, job = make_root(tmp_path)
    with pytest.raises(ValueError, match="unknown decision"):
        run_decision("nope", root=root, job=job, state={}, post=Stub())


def test_append_jsonl_creates_parent_dirs(tmp_path):
    p = tmp_path / "a" / "b" / "log.jsonl"
    append_jsonl(p, {"x": 1})
    append_jsonl(p, {"x": 2})
    assert p.read_text() == '{"x": 1}\n{"x": 2}\n'


WRITER = """
import sys
from pathlib import Path
from factorylib.run import append_jsonl
path, n = Path(sys.argv[1]), int(sys.argv[2])
for i in range(200):
    append_jsonl(path, {"writer": n, "i": i, "pad": str(n) * 3000})
"""


def test_concurrent_appends_are_whole_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(path), str(n)], cwd=KIT)
             for n in range(8)]
    assert [p.wait() for p in procs] == [0] * 8
    lines = path.read_text().splitlines()
    assert len(lines) == 1600
    recs = [json.loads(line) for line in lines]
    assert all(r["pad"] == str(r["writer"]) * 3000 for r in recs)
    assert {(r["writer"], r["i"]) for r in recs} == {(n, i) for n in range(8) for i in range(200)}


def test_append_jsonl_short_write_raises(tmp_path, monkeypatch):
    import factorylib.run as run_mod
    real = os.write
    monkeypatch.setattr(run_mod.os, "write", lambda fd, data: real(fd, data[:3]))
    with pytest.raises(OSError, match="short write"):
        append_jsonl(tmp_path / "log.jsonl", {"x": 1})


def test_escalation_rule_hits_redacted_in_reasons(tmp_path):
    root, job = make_root(tmp_path)
    rec = run_decision("escalation", root=root, job=job,
                       state={"rule_hits": [f"OPENROUTER_API_KEY={SECRET}", "a.env"]},
                       post=Stub(ESC_CLEAN))
    assert rec["apply"] == {"escalate": True, "reasons": ["[REDACTED]", "a.env"]}
    assert SECRET not in json.dumps(rec)
    assert SECRET not in (job.folder / "decisions.jsonl").read_text()


def test_state_error_escalation_fails_closed(tmp_path):
    root, job = make_root(tmp_path, modes={"escalation": "active"})
    post = Stub(ESC_CLEAN)
    rec = run_decision("escalation", root=root, job=job, state={},
                       state_error="JSONDecodeError: bad policy", post=post)
    assert post.calls == []
    assert rec["error"] == "JSONDecodeError: bad policy" and rec["band"] == "uncertain"
    assert rec["apply"] == {"escalate": True, "reasons": ["<state build failed>"]}


def test_config_error_after_template_gives_fallback(tmp_path):
    root, job = make_root(tmp_path, modes={"triage": "active"})
    (root / "factory" / "config.json").write_text("{corrupt")
    post = Stub(TRIAGE_FAST)
    rec = run_decision("triage", root=root, job=job, state={"feature": "x"}, post=post)
    assert post.calls == []
    assert rec["error"].startswith("JSONDecodeError: ")
    assert rec["band"] == "uncertain" and rec["mode"] == "shadow"
    assert rec["apply"]["tier"] == "heavy"


def test_error_message_scrubs_key(tmp_path):
    root, job = make_root(tmp_path, modes={"triage": "active"})
    rec = run_decision("triage", root=root, job=job, state={"feature": "x"},
                       post=Stub(exc=RuntimeError("bad header test-key-not-real")))
    assert "test-key-not-real" not in rec["error"] and "[REDACTED]" in rec["error"]
