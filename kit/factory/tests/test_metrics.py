import json
from collections import Counter, defaultdict

from factorylib import metrics as m


def test_union_seconds_counts_overlap_once():
    assert m.union_seconds([(0, 10), (5, 12), (20, 25)]) == 17
    assert m.union_seconds([]) == 0


def test_job_of_takes_the_most_mentioned_job():
    text = "factory/jobs/001-a-b x factory/jobs/002-c y factory/jobs/002-c"
    assert m.job_of(text) == "002-c"
    assert m.job_of("nothing") is None


def test_classify_call_names_decide_merge_and_checks():
    c = lambda cmd: {"tool": "Bash", "input": {"command": cmd}}
    assert m.classify_call(c("factory/bin/decide triage --job x")) == ("decide", "triage")
    assert m.classify_call(c("git merge --no-ff factory/x"))[0] == "merge"
    assert m.classify_call(c("npm test"))[0] == "check"
    assert m.classify_call(c("ls")) is None


def test_calibrate_recovers_prices_and_reports_error():
    price = {"in": 2.0, "out": 10.0, "cr": 0.2, "cw": 4.0}
    sessions = []
    for i in range(1, 6):
        t = Counter({"in": 100 * i, "out": 1000 * i * (i % 3 + 1), "cr": 50000 * i, "cw": 7000 * (6 - i)})
        cost = sum(t[k] * price[k] for k in price) / 1e6
        sessions.append({"all_tokens": defaultdict(Counter, {"mx": t}), "cost_state": {"modelUsage": {"mx": {"costUSD": cost}}}})
    r = m.calibrate(sessions)["mx"]
    assert r["max_rel_err"] < 0.05
    assert abs(r["usd_per_mtok"]["cw"] - 4.0) < 0.2
    assert abs(r["usd_per_mtok"]["cr"] - 0.2) < 0.05


def _line(**d):
    return json.dumps(d)


def test_parse_file_finds_human_wait_but_not_task_notifications(tmp_path):
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join([
        _line(type="user", timestamp="2026-01-01T00:00:00Z", message={"content": "go"}),
        _line(type="assistant", timestamp="2026-01-01T00:00:10Z", requestId="r1",
              message={"model": "mx", "usage": {"input_tokens": 1, "output_tokens": 2}}),
        _line(type="user", timestamp="2026-01-01T00:01:10Z", message={"content": "<task-notification>x</task-notification>"}),
        _line(type="user", timestamp="2026-01-01T00:03:10Z", message={"content": "confirmed"}),
        _line(type="assistant", timestamp="2026-01-01T00:03:20Z", requestId="r1",
              message={"model": "mx", "usage": {"input_tokens": 1, "output_tokens": 5}}),
    ]))
    s = m.parse_file(f)
    assert [round(b - a) for a, b in s["waits"]] == [120]  # 00:01:10 -> 00:03:10
    assert s["tokens"]["mx"]["out"] == 5  # one usage per request id, the last wins
    assert round(s["end"] - s["start"]) == 200
