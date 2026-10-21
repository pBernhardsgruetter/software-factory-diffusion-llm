"""Measure what the factory spent per job: phases, models, tokens, waits, complexity.

    metrics [--root DIR] [--claude-dir DIR] [--job ID ...] [--json] [--out DIR]

Sources, all already on disk (nothing is instrumented, so old runs can be measured):
Claude Code transcripts (orchestrator session + ``subagents/``), ``jobs/<id>/``
(``decisions.jsonl``, ``job.json``, ``spec.md``, ``fast-*.json``) and ``git`` (the
merge commit's diff). Costs are calibrated: Claude Code writes the true per-model
cost of every session (``cost-state``), so one price per model is fitted to those
and the fit error is reported next to every cost.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

JOB_RE = re.compile(r"factory/jobs/(\d{3}-[a-z0-9-]+)")
ROUND_RE = re.compile(r"(?:[Rr]ound[:\s-]+|\br)(\d+)")
KEYS = ("in", "out", "cr", "cw")
PHASE_OF = {"spec-writer": "spec", "builder": "build", "approver": "approve"}


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def project_dir(root: Path, claude_dir: Path | None = None) -> Path:
    if claude_dir:
        return claude_dir
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(root.resolve()))


def union_seconds(spans: list[tuple[float, float]]) -> float:
    total, end = 0.0, None
    for a, b in sorted(spans):
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


def _rows(path: Path):
    for line in path.read_text(errors="replace").splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _usage(d: dict) -> tuple[str, dict[str, int]] | None:
    m = d.get("message") or {}
    u = m.get("usage")
    if d.get("type") != "assistant" or not isinstance(u, dict):
        return None
    cw = u.get("cache_creation_input_tokens", 0)
    return m.get("model") or d.get("requestedModel") or "?", {
        "in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
        "cr": u.get("cache_read_input_tokens", 0), "cw": cw}


def _text(d: dict) -> str:
    c = (d.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def parse_file(path: Path) -> dict:
    """One transcript -> time span, per-model tokens (one usage per request), tool calls."""
    tokens: dict[str, Counter] = defaultdict(Counter)
    seen: dict[str, tuple[str, dict]] = {}
    stamps: list[float] = []
    first_prompt, calls, opened, cost_state, waits = "", [], {}, None, []
    prev = None
    for d in _rows(path):
        t = ts(d["timestamp"]) if d.get("timestamp") else None
        if d.get("type") == "cost-state":
            cost_state = d
        if t is None:
            continue
        kind = d.get("type")
        if kind in ("assistant", "user", "system"):
            stamps.append(t)
        u = _usage(d)
        if u:
            seen[d.get("requestId") or d.get("uuid")] = u
        msg = (d.get("message") or {}).get("content")
        for b in msg if isinstance(msg, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                opened[b["id"]] = (t, b.get("name"), b.get("input") or {})
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in opened:
                t0, name, inp = opened.pop(b["tool_use_id"])
                calls.append({"start": t0, "end": t, "tool": name, "input": inp})
        if kind == "user":
            text = _text(d)
            if not first_prompt and text:
                first_prompt = text
            human = bool(text) and "task-notification" not in text and not (isinstance(msg, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in msg))
            if human and prev is not None and first_prompt != text:
                waits.append((prev, t))
        if kind in ("assistant", "user"):
            prev = t
    for model, usage in seen.values():
        tokens[model].update(usage)
    return {"start": min(stamps, default=0.0), "end": max(stamps, default=0.0), "tokens": tokens,
            "first_prompt": first_prompt, "calls": calls, "cost_state": cost_state, "waits": waits}


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c and m[c][c]:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] if m[i][i] else 0.0 for i in range(n)]


def calibrate(sessions: list[dict]) -> dict[str, dict]:
    """Fit USD per million tokens (in, out, cache read, cache write) per model to Claude
    Code's own per-session, per-model cost by least squares, relative to each cost."""
    rows: dict[str, list[tuple[list[float], float]]] = defaultdict(list)
    for s in sessions:
        for model, mu in ((s["cost_state"] or {}).get("modelUsage") or {}).items():
            c = mu.get("costUSD", 0.0)
            if c > 0:
                t = s["all_tokens"].get(model, Counter())
                rows[model].append(([t[k] / 1e6 for k in KEYS], c))
    out = {}
    for model, data in rows.items():
        # Input tokens are too few to price on their own: tie input to output / 5.
        feats = [([x[1] + x[0] / 5, x[2], x[3]], c) for x, c in data]
        ata = [[sum(x[i] * x[j] / c ** 2 for x, c in feats) + (1e-9 if i == j else 0) for j in range(3)] for i in range(3)]
        atb = [sum(x[i] / c for x, c in feats) for i in range(3)]
        w_out, w_cr, w_cw = (max(v, 0.0) for v in _solve(ata, atb))
        w = [w_out / 5, w_out, w_cr, w_cw]
        errs = [abs(sum(wi * xi for wi, xi in zip(w, x)) - c) / c for x, c in data]
        out[model] = {"usd_per_mtok": dict(zip(KEYS, (round(v, 3) for v in w))), "max_rel_err": round(max(errs), 3),
                      "mean_rel_err": round(sum(errs) / len(errs), 3), "sessions": len(data)}
    return out


def cost_of(tokens: Counter, model: str, rates: dict) -> float:
    r = rates.get(model)
    return sum(tokens[k] * r["usd_per_mtok"][k] for k in KEYS) / 1e6 if r else 0.0


def classify_call(call: dict) -> tuple[str, str] | None:
    cmd = (call["input"].get("command") or "") if call["tool"] == "Bash" else ""
    for pat, phase in ((r"factory/bin/decide (\w+)", "decide"), 
                       (r"git merge --no-ff", "merge"), (r"\bnpm (test|run)|\bnpx ", "check"),
                       (r"git worktree|clean-stubs", "cleanup")):
        m = re.search(pat, cmd)
        if m:
            return phase, (m.group(1) if m.groups() else phase)
    return None


def job_of(text: str) -> str | None:
    c = Counter(JOB_RE.findall(text))
    return c.most_common(1)[0][0] if c else None


def collect(pdir: Path, also: list[Path] = ()) -> tuple[list[dict], dict]:
    """All sessions with their subagents, then the calibrated price table. Sessions of
    ``also`` (other project dirs) only widen the calibration sample."""
    sessions = []
    for main in sorted(pdir.glob("*.jsonl")):
        s = parse_file(main)
        s["id"], s["path"] = main.stem, main
        s["all_tokens"] = defaultdict(Counter, {m: Counter(c) for m, c in s["tokens"].items()})
        s["subs"] = []
        for sub in sorted((pdir / main.stem / "subagents").glob("*.jsonl")):
            a = parse_file(sub)
            meta = sub.with_suffix(".meta.json")
            a["meta"] = json.loads(meta.read_text()) if meta.exists() else {}
            a["job"] = job_of(a["first_prompt"])
            for m, c in a["tokens"].items():
                s["all_tokens"][m].update(c)
            s["subs"].append(a)
        text = main.read_text(errors="replace")
        s["job"] = job_of(text)
        sessions.append(s)
    extra = [x for d in also for x in collect(d)[0]]
    return sessions, calibrate(sessions + extra)


def git_diff(root: Path, job: str) -> dict:
    def run(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=30).stdout
    sha = run("log", "--merges", "-1", "--format=%H", f"--grep=Merge factory/{job}").strip()
    if not sha:
        return {}
    files = {"src": 0, "test": 0, "other": 0}
    add = dele = 0
    for line in run("diff", "--numstat", f"{sha}^1", sha).splitlines():
        a, d, name = line.split("\t", 2)
        add += int(a) if a.isdigit() else 0
        dele += int(d) if d.isdigit() else 0
        files["test" if ".test." in name else "src" if name.startswith("src/") else "other"] += 1
    return {"files": files, "added": add, "deleted": dele}


def job_facts(root: Path, job: str) -> dict:
    jd = root / "factory" / "jobs" / job
    facts: dict = {"job": job}
    try:
        j = json.loads((jd / "job.json").read_text())
        facts["mercury_usd"] = (j.get("cost") or {}).get("mercury3_usd")
        facts.update(tier=j.get("tier"), stage=j.get("stage"), rounds=j.get("round"),
                     fix_rounds=j.get("fix_rounds"), feature=(j.get("feature") or "")[:70],
                     escalations=[e.get("source") for e in j.get("escalations", [])])
    except (OSError, ValueError):
        pass
    try:
        spec = (jd / "spec.md").read_text()
        sec = spec.split("## Acceptance criteria", 1)[1].split("\n## ", 1)[0]
        facts["criteria"] = sum(1 for ln in sec.splitlines() if ln.lstrip().startswith("- "))
    except (OSError, IndexError):
        pass
    decides = []
    try:
        for d in _rows(jd / "decisions.jsonl"):
            if d.get("kind") != "decision":
                continue
            decides.append({"name": d["decision"], "latency_ms": d.get("latency_ms"), "band": d.get("band"),
                            "model": d.get("model"), "mode": d.get("mode"), "cost": (d.get("usage") or {}).get("cost", 0)})
            if d["decision"] == "triage":
                a = d.get("answers") or {}
                facts["kind"] = (a.get("kind") or {}).get("value")
                facts["complexity"] = round((a.get("complexity") or {}).get("value") or 0, 2)
                facts["ui_change"] = (a.get("ui_change") or {}).get("value")
                facts["risks"] = (d.get("apply") or {}).get("risks")
    except OSError:
        pass
    facts["decides"] = decides
    fast = []
    for f in sorted(jd.glob("fast-*-round-*.json")):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        fast.append({"role": re.match(r"fast-(.+)-round", f.name).group(1), "secs": round((d.get("duration_ms") or 0) / 1000, 1),
                     "api_secs": round((d.get("duration_api_ms") or 0) / 1000, 1), "turns": d.get("num_turns"),
                     "tokens": {"in": (d.get("usage") or {}).get("input_tokens", 0), "out": (d.get("usage") or {}).get("output_tokens", 0)}})
    facts["fast"] = fast
    facts["diff"] = git_diff(root, job)
    return facts


def phases_for(session: dict, job: str, rates: dict) -> list[dict]:
    out = []
    for a in session["subs"]:
        if a["job"] != job:
            continue
        role = a["meta"].get("agentType", "?")
        m = ROUND_RE.search(a["first_prompt"]) or ROUND_RE.search(a["meta"].get("description", ""))
        phase = PHASE_OF.get(role) or ("review" if role.startswith("reviewer") else role)
        if phase == "build" and re.search(r"[Rr]ework", a["first_prompt"][:400]):
            phase = "rework"
        model = max(a["tokens"], key=lambda k: sum(a["tokens"][k].values()), default="?")
        tok = a["tokens"].get(model, Counter())
        out.append({"phase": phase, "actor": role, "model": model, "round": int(m.group(1)) if m else None,
                    "start": a["start"], "end": a["end"], "secs": round(a["end"] - a["start"], 1),
                    "tokens": dict(tok), "cost_est": round(cost_of(tok, model, rates), 4)})
    for c in session["calls"]:
        k = classify_call(c)
        if k and (JOB_RE.search(json.dumps(c["input"])) is None or job in json.dumps(c["input"])):
            out.append({"phase": k[0], "actor": k[1], "model": "-", "round": None, "start": c["start"], "end": c["end"],
                        "secs": round(c["end"] - c["start"], 1), "tokens": {}, "cost_est": 0.0})
    return sorted(out, key=lambda p: p["start"])


def fast_phases(root: Path, job: str) -> list[dict]:
    """fast-coder runs in the background, so its Bash call returns at once: take the
    child's own duration, ending at the transcript's mtime."""
    out = []
    for f in sorted((root / "factory" / "jobs" / job).glob("fast-*-round-*.json")):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        end, secs = f.stat().st_mtime, (d.get("duration_ms") or 0) / 1000
        out.append({"phase": "fast", "actor": re.match(r"fast-(.+)-round", f.name).group(1), "model": "mercury-3", "round": None,
                    "start": end - secs, "end": end, "secs": round(secs, 1), "tokens": {}, "cost_est": 0.0})
    return out


def measure_job(root: Path, job: str, sessions: list[dict], rates: dict) -> dict:
    facts = job_facts(root, job)
    mine = [s for s in sessions if s["job"] == job]
    phases = [p for s in mine for p in phases_for(s, job, rates)] + fast_phases(root, job)
    phases.sort(key=lambda p: p["start"])
    orch_tokens: dict[str, Counter] = defaultdict(Counter)
    wall = wait = 0.0
    spans = [(p["start"], p["end"]) for p in phases]
    for s in mine:
        wall += s["end"] - s["start"]
        wait += sum(b - a for a, b in s["waits"])
        for m, c in s["tokens"].items():
            orch_tokens[m].update(c)
    work = wall - wait
    busy = union_seconds(spans)
    by = defaultdict(float)
    for p in phases:
        by[p["phase"]] += p["secs"]
    facts.update(phases=phases, wall_s=round(wall, 1), human_wait_s=round(wait, 1), work_s=round(work, 1),
                 covered_s=round(busy, 1), orchestrator_s=round(max(work - busy, 0.0), 1),
                 phase_secs={k: round(v, 1) for k, v in sorted(by.items())},
                 orchestrator_cost=round(sum(cost_of(c, m, rates) for m, c in orch_tokens.items()), 4),
                 cost_true=round(sum(mu.get("costUSD", 0) for s in mine if s["cost_state"]
                                     for mu in (s["cost_state"].get("modelUsage") or {}).values()), 4))
    facts["cost_by_model"] = {m: round(cost_of(c, m, rates), 4) for s in mine for m, c in s["all_tokens"].items()}
    return facts


def render(jobs: list[dict], rates: dict) -> str:
    L = ["# Factory metrics", "", "## Calibrated prices (fitted to Claude Code's own session costs)", "",
         "| model | in | out | cache read | cache write | mean err | max err | sessions |", "|---|---|---|---|---|---|---|---|"]
    L += [f"| {m} | " + " | ".join(str(r["usd_per_mtok"][k]) for k in KEYS) + f" | {r['mean_rel_err']:.1%} | {r['max_rel_err']:.1%} | {r['sessions']} |"
          for m, r in sorted(rates.items())]
    L[2] = "## Effective prices, USD per million tokens (fitted to Claude Code's own session costs; not list prices, only their sums are meaningful)"
    L += ["", "## Jobs", "",
          "| job | tier | kind | cx | crit | files s/t | +/- | rounds | esc | work s | human wait s | orch s | claude $ | mercury $ |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for j in jobs:
        f = (j.get("diff") or {}).get("files") or {}
        L.append(f"| {j['job']} | {j.get('tier')} | {j.get('kind')} | {j.get('complexity')} | {j.get('criteria')} | "
                 f"{f.get('src', '-')}/{f.get('test', '-')} | +{(j.get('diff') or {}).get('added', '-')}/-{(j.get('diff') or {}).get('deleted', '-')} | "
                 f"{j.get('rounds')} | {','.join(j.get('escalations') or []) or '-'} | {j['work_s']} | {j['human_wait_s']} | "
                 f"{j['orchestrator_s']} | {j['cost_true']} | {j.get('mercury_usd')} |")
    sub = sum(p["cost_est"] for j in jobs for p in j["phases"])
    orch = sum(j["orchestrator_cost"] for j in jobs)
    true = sum(j["cost_true"] for j in jobs)
    merc = sum(j.get("mercury_usd") or 0 for j in jobs)
    dec = sum(x.get("cost") or 0 for j in jobs for x in j["decides"])
    L += ["", "## Cost split", "", "| part | $ | share of Claude |", "|---|---|---|",
          f"| orchestrator session (Opus, `/factory`) | {orch:.2f} | {orch / true:.0%} |",
          f"| subagents (spec, build, review, approve) | {sub:.2f} | {sub / true:.0%} |",
          f"| Claude total (Claude Code's own number) | {true:.2f} | 100% |",
          f"| Mercury 3 (scout, fast builder/fixer) | {merc:.3f} | |", f"| Mercury Decide | {dec:.3f} | |"]
    agg: dict = defaultdict(lambda: {"n": 0, "secs": 0.0, "cost": 0.0, "out": 0, "in": 0})
    for j in jobs:
        for p in j["phases"]:
            a = agg[(p["phase"], p["actor"], p["model"])]
            a["n"] += 1
            a["secs"] += p["secs"]
            a["cost"] += p["cost_est"]
            a["out"] += p["tokens"].get("out", 0)
            a["in"] += p["tokens"].get("in", 0) + p["tokens"].get("cr", 0) + p["tokens"].get("cw", 0)
    L += ["", "## Where the time and money go (all jobs)", "",
          "| phase | actor | model | n | total s | mean s | out tok | in tok (incl. cache) | est $ |", "|---|---|---|---|---|---|---|---|---|"]
    for (ph, ac, mo), a in sorted(agg.items(), key=lambda kv: -kv[1]["secs"]):
        L.append(f"| {ph} | {ac} | {mo} | {a['n']} | {a['secs']:.0f} | {a['secs'] / a['n']:.1f} | {a['out']} | {a['in']} | {a['cost']:.3f} |")
    tot = defaultdict(float)
    for j in jobs:
        for k, v in j["phase_secs"].items():
            tot[k] += v
        tot["orchestrator (uncovered)"] += j["orchestrator_s"]
        tot["human wait"] += j["human_wait_s"]
    grand = sum(tot.values()) or 1.0
    L += ["", "## Share of summed wall time (parallel phases count in full)", "", "| bucket | s | share |", "|---|---|---|"]
    L += [f"| {k} | {v:.0f} | {v / grand:.0%} |" for k, v in sorted(tot.items(), key=lambda kv: -kv[1])]
    d = defaultdict(lambda: Counter())
    for j in jobs:
        for x in j["decides"]:
            d[x["name"]]["n"] += 1
            d[x["name"]]["ms"] += x.get("latency_ms") or 0
            d[x["name"]]["uncertain"] += x.get("band") == "uncertain"
    L += ["", "## Mercury Decide", "", "| decision | calls | mean ms | uncertain |", "|---|---|---|---|"]
    L += [f"| {k} | {c['n']} | {c['ms'] / c['n']:.0f} | {c['uncertain']} |" for k, c in sorted(d.items())]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="metrics", description=__doc__.split("\n")[0])
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--claude-dir", type=Path)
    p.add_argument("--also-root", type=Path, action="append", default=[],
                   help="another repo whose sessions only widen the price calibration")
    p.add_argument("--job", action="append")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", type=Path, help="write <out>/<job>.json and report.md")
    a = p.parse_args(argv)
    root = a.root.resolve()
    sessions, rates = collect(project_dir(root, a.claude_dir), [project_dir(r) for r in a.also_root])
    known = sorted(p.name for p in (root / "factory" / "jobs").glob("[0-9][0-9][0-9]-*") if p.is_dir())
    jobs = [measure_job(root, j, sessions, rates) for j in (a.job or known)]
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        for j in jobs:
            (a.out / f"{j['job']}.json").write_text(json.dumps(j, indent=1, default=str))
        (a.out / "report.md").write_text(render(jobs, rates))
    sys.stdout.write(json.dumps({"rates": rates, "jobs": jobs}, indent=1, default=str) if a.json else render(jobs, rates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
