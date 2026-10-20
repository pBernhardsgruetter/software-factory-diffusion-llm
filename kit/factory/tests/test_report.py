import json

from factorylib.report import build_report


def dec(name, route, *, ts, band="confident", model="m", error=None):
    return {"kind": "decision", "decision": name, "id": "D1", "mode": "shadow", "band": band,
            "answers": {}, "route": route, "apply": {}, "model": model, "error": error, "ts": ts}


def out(name, truth, *, ts, job="j1"):
    return {"kind": "outcome", "job": job, "decision": name, "truth": truth, "note": None, "ts": ts}


def write(root, job, records, raw_lines=()):
    folder = root / "factory" / "jobs" / job
    folder.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records] + list(raw_lines)
    (folder / "decisions.jsonl").write_text("\n".join(lines) + "\n")


def t(n):
    return f"2026-01-01T00:00:{n:02d}.000+00:00"


def test_agreement_and_coverage(tmp_path):
    write(tmp_path, "j1", [
        dec("promote", {"promote": True}, ts=t(1)),
        out("promote", {"promote": True}, ts=t(2)),
    ])
    write(tmp_path, "j2", [
        dec("promote", {"promote": False}, ts=t(1), band="uncertain"),
        out("promote", {"promote": True}, ts=t(2), job="j2"),
    ])
    write(tmp_path, "j3", [
        dec("promote", {"promote": True}, ts=t(1)),
        dec("promote", {"promote": True}, ts=t(3)),
        out("promote", {"promote": True}, ts=t(4), job="j3"),
    ])
    r = build_report(tmp_path)["promote"]
    assert r["cases"] == 4 and r["paired"] == 3
    assert r["coverage"] == 0.75
    assert abs(r["agreement"] - 2 / 3) < 1e-9
    assert r["unsafe_misses"] == 0 and r["ready"] is False


def test_unsafe_miss_blocks_ready(tmp_path):
    write(tmp_path, "j1", [dec("triage", {"tier": "fast", "risks": []}, ts=t(1)),
                           out("triage", {"risks": ["auth"]}, ts=t(2))])
    for i in range(25):
        job = f"k{i}"
        write(tmp_path, job, [dec("triage", {"tier": "heavy", "risks": ["auth"]}, ts=t(1)),
                              out("triage", {"risks": ["auth"]}, ts=t(2), job=job)])
    r = build_report(tmp_path)["triage"]
    assert r["unsafe_misses"] == 1
    assert r["agreement"] < 1.0 and r["ready"] is False
    assert r["cases"] >= 20 and r["paired"] >= 20


def test_unsafe_miss_blocks_ready_with_high_agreement(tmp_path):
    for i in range(40):
        job = f"k{i}"
        write(tmp_path, job, [dec("escalation", {"escalate": True, "reasons": []}, ts=t(1)),
                              out("escalation", {"escalate": True}, ts=t(2), job=job)])
    write(tmp_path, "bad", [dec("escalation", {"escalate": False, "reasons": []}, ts=t(1)),
                            out("escalation", {"escalate": True}, ts=t(2), job="bad")])
    r = build_report(tmp_path)["escalation"]
    assert r["agreement"] >= 0.95 and r["unsafe_misses"] == 1 and r["ready"] is False


def test_unsafe_misses_only_for_risk_decisions(tmp_path):
    write(tmp_path, "j1", [dec("stuck", {"escalate": False}, ts=t(1)),
                           out("stuck", {"escalate": True}, ts=t(2))])
    assert build_report(tmp_path)["stuck"]["unsafe_misses"] == 0


def test_ready_at_threshold(tmp_path):
    for i in range(20):
        job = f"j{i}"
        route = {"promote": True} if i else {"promote": False}
        write(tmp_path, job, [dec("promote", route, ts=t(1)),
                              out("promote", {"promote": True}, ts=t(2), job=job)])
    r = build_report(tmp_path)["promote"]
    assert r["cases"] == 20 and r["agreement"] == 0.95 and r["ready"] is True
    assert build_report(tmp_path, min_cases=21)["promote"]["ready"] is False


def test_just_below_agreement_threshold(tmp_path):
    for i in range(20):
        job = f"j{i}"
        route = {"promote": i > 1}
        write(tmp_path, job, [dec("promote", route, ts=t(1)),
                              out("promote", {"promote": True}, ts=t(2), job=job)])
    assert build_report(tmp_path)["promote"]["ready"] is False


def test_outcome_without_decision_ignored(tmp_path):
    write(tmp_path, "j1", [out("promote", {"promote": True}, ts=t(2)),
                           dec("stuck", {"escalate": False}, ts=t(1))])
    rep = build_report(tmp_path)
    assert "promote" not in rep or rep["promote"]["paired"] == 0
    assert rep["stuck"]["paired"] == 0 and rep["stuck"]["agreement"] is None


def test_outcome_pairs_only_preceding_decision(tmp_path):
    write(tmp_path, "j1", [dec("promote", {"promote": True}, ts=t(5)),
                           out("promote", {"promote": True}, ts=t(2))])
    assert build_report(tmp_path)["promote"]["paired"] == 0


def test_latest_outcome_supersedes(tmp_path):
    write(tmp_path, "j1", [
        dec("promote", {"promote": True}, ts=t(1)),
        out("promote", {"promote": False}, ts=t(2)),
        out("promote", {"promote": True}, ts=t(3)),
    ])
    r = build_report(tmp_path)["promote"]
    assert r["paired"] == 1 and r["agreement"] == 1.0


def test_latest_preceding_decision_used(tmp_path):
    write(tmp_path, "j1", [
        dec("promote", {"promote": False}, ts=t(1)),
        dec("promote", {"promote": True}, ts=t(2)),
        out("promote", {"promote": True}, ts=t(3)),
    ])
    r = build_report(tmp_path)["promote"]
    assert r["cases"] == 2 and r["paired"] == 1 and r["agreement"] == 1.0


def test_error_records_not_paired_and_not_confident(tmp_path):
    write(tmp_path, "j1", [
        dec("promote", None, ts=t(1), band="uncertain", model=None, error="boom"),
        out("promote", {"promote": True}, ts=t(2)),
    ])
    write(tmp_path, "j2", [dec("promote", {"promote": True}, ts=t(1), error="late failure"),
                           out("promote", {"promote": True}, ts=t(2), job="j2")])
    write(tmp_path, "j3", [dec("promote", {"promote": True}, ts=t(1)),
                           out("promote", {"promote": True}, ts=t(2), job="j3")])
    r = build_report(tmp_path)["promote"]
    assert r["cases"] == 3 and r["paired"] == 1
    assert abs(r["coverage"] - 1 / 3) < 1e-9
    assert r["agreement"] == 1.0


def test_unsafe_miss_ignores_error_records(tmp_path):
    write(tmp_path, "j1", [dec("toolgate", None, ts=t(1), band="uncertain", model=None, error="x"),
                           out("toolgate", {"permission": "deny"}, ts=t(2))])
    assert build_report(tmp_path)["toolgate"]["unsafe_misses"] == 0


def test_na_band_skipped_in_coverage_but_paired(tmp_path):
    write(tmp_path, "j1", [
        dec("escalation", {"escalate": True, "reasons": ["r"]}, ts=t(1), band="n/a", model=None),
        out("escalation", {"escalate": True}, ts=t(2)),
    ])
    write(tmp_path, "j2", [dec("escalation", {"escalate": False, "reasons": []}, ts=t(1))])
    r = build_report(tmp_path)["escalation"]
    assert r["cases"] == 2 and r["paired"] == 1 and r["agreement"] == 1.0
    assert r["coverage"] == 1.0  # the n/a record is not in the coverage denominator


def test_list_compared_as_set(tmp_path):
    write(tmp_path, "j1", [dec("finding", {"fast_fixer": [2, 0], "builder": [1]}, ts=t(1)),
                           out("finding", {"fast_fixer": [0, 2]}, ts=t(2))])
    assert build_report(tmp_path)["finding"]["agreement"] == 1.0


def test_all_truth_keys_must_match(tmp_path):
    write(tmp_path, "j1", [dec("triage", {"tier": "fast", "risks": []}, ts=t(1)),
                           out("triage", {"tier": "heavy", "risks": []}, ts=t(2))])
    assert build_report(tmp_path)["triage"]["agreement"] == 0.0


def test_missing_route_key_is_disagreement(tmp_path):
    write(tmp_path, "j1", [dec("triage", {"tier": "fast"}, ts=t(1)),
                           out("triage", {"risks": []}, ts=t(2))])
    assert build_report(tmp_path)["triage"]["agreement"] == 0.0


def test_toolgate_log_counts_cases_and_coverage(tmp_path):
    (tmp_path / "factory").mkdir(parents=True)
    recs = [dec("toolgate", {"permission": "allow"}, ts=t(1)),
            dec("toolgate", {"permission": "ask"}, ts=t(2), band="uncertain")]
    (tmp_path / "factory" / "tool-gate.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n")
    r = build_report(tmp_path)["toolgate"]
    assert r["cases"] == 2 and r["coverage"] == 0.5 and r["paired"] == 0
    assert r["agreement"] is None and r["ready"] is False


def test_toolgate_unsafe_miss_from_outcome(tmp_path):
    write(tmp_path, "j1", [dec("toolgate", {"permission": "allow"}, ts=t(1)),
                           out("toolgate", {"permission": "ask"}, ts=t(2))])
    assert build_report(tmp_path)["toolgate"]["unsafe_misses"] == 1


def test_corrupt_lines_skipped_and_counted(tmp_path):
    write(tmp_path, "j1", [dec("promote", {"promote": True}, ts=t(1)),
                           out("promote", {"promote": True}, ts=t(2))],
          raw_lines=['{"kind": "decision", "dec', "not json", "[1, 2]", '{"kind": "mystery"}'])
    (tmp_path / "factory").mkdir(exist_ok=True)
    (tmp_path / "factory" / "tool-gate.jsonl").write_text("garbage\n")
    rep = build_report(tmp_path)
    assert rep["skipped_lines"] == 5
    assert rep["promote"]["cases"] == 1 and rep["promote"]["agreement"] == 1.0


def test_empty_root(tmp_path):
    assert build_report(tmp_path) == {"skipped_lines": 0}


def test_malformed_records_do_not_crash(tmp_path):
    write(tmp_path, "j1", [
        {"kind": "decision", "decision": "promote", "band": "confident", "model": "m",
         "route": "not a dict", "error": None, "ts": t(1)},
        {"kind": "decision", "decision": "promote", "ts": "not a time", "route": {"promote": True},
         "band": "confident", "model": "m", "error": None},
        {"kind": "outcome", "decision": "promote", "truth": "nope", "ts": t(2)},
        {"kind": "outcome", "decision": "promote", "truth": {"promote": True}, "ts": 5},
    ])
    rep = build_report(tmp_path)
    assert rep["promote"]["paired"] == 0
