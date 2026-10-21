import json
import subprocess
from types import SimpleNamespace

from factorylib import judge as j


def test_parse_json_takes_first_balanced_object_from_noisy_text():
    text = 'Sure! {"winner": "X", "reason": "has {braces} inside"} trailing {"x": 1}'
    assert j.parse_json(text) == {"winner": "X", "reason": "has {braces} inside"}
    assert j.parse_json("no json here") is None


def _repo(tmp_path, name, body):
    r = tmp_path / name
    (r / "factory" / "jobs" / "001-x").mkdir(parents=True)
    (r / "factory" / "jobs" / "001-x" / "job.json").write_text(json.dumps({"feature": "add a faq page"}))
    (r / "factory" / "jobs" / "001-x" / "spec.md").write_text("## Acceptance criteria\n- shows faq\n\n## Other\nx\n")
    g = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    g("init", "-q", "-b", "main"); g("config", "user.email", "a@b.c"); g("config", "user.name", "t")
    (r / "f.txt").write_text("base\n"); g("add", "-A"); g("commit", "-qm", "base")
    g("checkout", "-qb", "factory/001-x"); (r / "f.txt").write_text(body); g("commit", "-qam", "work")
    g("checkout", "-q", "main"); g("merge", "--no-ff", "-q", "factory/001-x", "-m", "Merge factory/001-x")
    return r


def test_gather_collects_request_criteria_and_merged_diff_only(tmp_path):
    g = j.gather(_repo(tmp_path, "r", "feature\n"), "001-x")
    assert g["request"] == "add a faq page"
    assert g["criteria"] == "- shows faq"
    assert "+feature" in g["diff"] and "-base" in g["diff"]


def _runner(answers):
    seen = []

    def run(cmd, cwd):
        seen.append(cmd[2])
        return SimpleNamespace(stdout=json.dumps({"result": json.dumps(answers.pop(0)), "total_cost_usd": 0.1}))
    run.seen = seen
    return run


def test_judge_job_prompt_has_no_factory_verdicts_and_counts_criteria(tmp_path):
    root = _repo(tmp_path, "r", "feature\n")
    run = _runner([{"criteria": [{"met": "yes"}, {"met": "no"}], "defects": [{"severity": "high"}]}])
    res = j.judge_job(root, "001-x", "m", run)
    assert (res["criteria_met"], res["criteria_total"], res["defects_high"]) == (1, 2, 1)
    prompt = run.seen[0]
    assert "add a faq page" in prompt and "shows faq" in prompt
    for forbidden in ("PASS", "APPROVE", "decision.md", "decisions.jsonl"):
        assert forbidden not in prompt.split("Answer with ONE JSON")[0]


def test_pair_names_a_winner_only_when_both_orders_agree(tmp_path):
    a, b = _repo(tmp_path, "a", "good\n"), _repo(tmp_path, "b", "worse\n")
    # order ab: X=a wins; order ba: Y=a wins -> a wins in both
    res = j.judge_pair((a, "001-x"), (b, "001-x"), "m", _runner([{"winner": "X"}, {"winner": "Y"}]))
    assert res["winner"] == "a" and not res["position_biased"]
    # always answers X: a in order ab, b in order ba -> position bias, no winner
    res = j.judge_pair((a, "001-x"), (b, "001-x"), "m", _runner([{"winner": "X"}, {"winner": "X"}]))
    assert res["winner"] == "tie" and res["position_biased"]
